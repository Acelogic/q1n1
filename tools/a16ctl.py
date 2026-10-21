#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Control the A16 from the Mac: get it into q1n1, Windows or the UEFI shell.

This is the q1n1 answer to macvdmtool. There is no hardware side channel on
this machine (the Dell dock terminates USB-PD, and the Qualcomm PD stack does
not speak Apple VDMs), so every transition rides a software channel:

  in the EL2 proxy   -> P_REBOOT over USB, then answer the next boot window
  in the boot window -> send "proxy" / "windows" / "shell" over USB CDC ACM
  in Windows         -> ssh: arm BootNext for the q1n1 shell entry and restart
  powered off/hung   -> wait for the boot window (press the power button)

A reset from q1n1 lands back in the q1n1 boot window because the window armed
BootNext before leaving firmware, so Windows is no longer on the path.

  python3 tools/a16ctl.py status
  python3 tools/a16ctl.py boot q1n1
  python3 tools/a16ctl.py boot windows
  python3 tools/a16ctl.py console
  python3 tools/a16ctl.py battery        # boot window, or the EL2 proxy's own GLINK
  python3 tools/a16ctl.py battery --survey   # EL2: locate SMEM/GLINK/IPCC, write nothing
  python3 tools/a16ctl.py battery --release  # EL2: hand the SoCCP link back as the firmware does
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), TOOLS / f'{name}.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


q1n1proxy = _load('q1n1proxy')
watcher = _load('test-a16-usb-serial')

PROXY_SERIAL = 'A16-Q1N1-EL2'
BOOT_SERIAL = 'A16-Q1N1-BOOT'
UEFI_SERIAL = 'A16-Q1N1-UEFI'
BANNER = 'Q1N1 BOOT WINDOW'
CHOICES = {'q1n1': 'proxy', 'q1n1-once': 'proxy once', 'windows': 'windows',
           'shell': 'shell', 'hold': 'hold'}
# Where the A16 is on the network, for the Windows-side half of a reboot. These
# describe one particular machine, so they come from the environment rather than
# being baked in: set A16_SSH_TARGET=user@host (and A16_SSH_ALIAS when the host
# key was recorded under a different address, e.g. after a Wi-Fi move).
DEFAULT_TARGET = os.environ.get('A16_SSH_TARGET', '')
DEFAULT_ALIAS = os.environ.get('A16_SSH_ALIAS', '')
DEFAULT_CONTROL = Path.home() / '.ssh' / 'a16-q1n1.sock'
REMOTE_DIR = os.environ.get('A16_REMOTE_DIR')


def ports(serial, interface=1):
    try:
        if serial == PROXY_SERIAL:
            return q1n1proxy.discover_endpoints(serial, interface)
        found, _ = watcher.inventory(serial, interface)
        return found
    except Exception:
        return []


def ssh_base(args):
    if not args.ssh_target:
        raise SystemExit('no SSH target: pass --ssh-target user@host or set A16_SSH_TARGET '
                         '(only the Windows-side half of a reboot needs it)')
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5']
    control = Path(args.ssh_control) if args.ssh_control else DEFAULT_CONTROL
    if control.exists():
        command += ['-S', str(control)]
    if args.host_key_alias:
        command += ['-o', f'HostKeyAlias={args.host_key_alias}']
    return command + [args.ssh_target]


def ssh_reachable(args, timeout=3.0):
    if not args.ssh_target:
        return False
    host = args.ssh_target.split('@')[-1]
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except OSError:
        return False


def run_powershell(args, script, timeout=120):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    command = ssh_base(args) + ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-EncodedCommand', encoded]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def state(args):
    """Where is the A16 right now?"""
    candidates = ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE)
    if any(not path.startswith('udp://') for path in candidates):
        return 'proxy'
    if candidates:
        # An active Mac NCM interface alone does not identify q1n1.
        try:
            proxy = q1n1proxy.connect()
            try:
                proxy.bootinfo()
            finally:
                proxy.link.close()
            return 'proxy'
        except (OSError, q1n1proxy.ProxyError):
            pass
    if ports(BOOT_SERIAL):
        return 'boot-window'
    if ports(UEFI_SERIAL):
        return 'uefi-serial-test'
    if ssh_reachable(args):
        return 'windows'
    return 'absent'


class BootWindow:
    """Line protocol of q1n1.efi --boot over the firmware CDC ACM port."""

    def __init__(self, path, verbose=True):
        self.transport = q1n1proxy.Transport(path)
        self.verbose = verbose
        self.buffer = b''

    def close(self):
        self.transport.close()

    def lines(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.buffer += self.transport.read(1, max(0.05, deadline - time.monotonic()))
            except q1n1proxy.ProxyTimeout:
                continue
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                text = line.decode('utf-8', 'replace').strip()
                if text:
                    if self.verbose:
                        print(f'  a16: {text}')
                    yield text

    def choose(self, choice, timeout=60):
        for text in self.lines(timeout):
            if BANNER in text:
                break
        else:
            raise SystemExit('boot window banner not seen')
        self.transport.write(choice.encode() + b'\r')
        for text in self.lines(15):
            if text.startswith('OK '):
                return text
            if text.startswith('ERR'):
                raise SystemExit(f'boot window rejected {choice!r}: {text}')
        raise SystemExit(f'no confirmation for {choice!r}')

    def ask(self, command, prefixes, timeout=20):
        """Send a query that does not end the window; return its answer line."""
        self.transport.write(command.encode() + b'\r')
        for text in self.lines(timeout):
            if text.startswith(prefixes) or text.startswith('ERR'):
                return text
        raise SystemExit(f'no answer to {command!r}')


def wait_for(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise SystemExit(f'timed out after {timeout:.0f} s: {message}')


def claim_window(args, choice, timeout=180):
    # With no dock, --auto can enter EL2 without exposing the USB0 firmware
    # boot window. The USB1 CDC fallback is the first reachable serial port.
    # Accept that outcome only for the normal proxy choice, whose armed return
    # matches --auto. Other choices still require an actual boot-window reply.
    def reachable():
        paths = ports(BOOT_SERIAL)
        if paths:
            return 'window', paths
        if choice == 'proxy':
            paths = ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE)
            if paths:
                return 'proxy', paths
        return None

    kind, paths = wait_for(reachable, timeout, 'neither the q1n1 boot window nor the requested proxy appeared')
    if kind == 'proxy':
        proxy = q1n1proxy.connect(wait=15)
        try:
            info = proxy.bootinfo()
            if info['current_el'] != 2 or not info['return_armed']:
                raise SystemExit('direct proxy appeared but did not confirm EL2 and an armed return')
        finally:
            proxy.link.close()
        print(f'--auto reached the EL2 proxy directly on {proxy.endpoint}')
        return 'OK proxy return=armed (automatic direct USB boot)'
    print(f'boot window on {paths[0]}; sending {choice!r}')
    window = BootWindow(paths[0], verbose=args.verbose)
    try:
        confirmation = window.choose(choice)
    finally:
        window.close()
    print(f'boot window answered: {confirmation}')
    return confirmation


def reboot_from_windows(args):
    if REMOTE_DIR:
        directory = REMOTE_DIR.replace("'", "''")
        select_dir = f"$q1n1Dir = '{directory}'"
    else:
        select_dir = r"""
$q1n1Dir = 'C:\q1n1-bringup'
if (-not (Test-Path -LiteralPath (Join-Path $q1n1Dir 'boot-a16-q1n1.ps1'))) {
    $candidates = @(Get-ChildItem -LiteralPath $env:USERPROFILE -Directory -Filter 'q1n1-bringup*' |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'boot-a16-q1n1.ps1') })
    if ($candidates.Count -ne 1) {
        throw "Expected one q1n1 bring-up directory under $env:USERPROFILE; found $($candidates.Count). Set A16_REMOTE_DIR to select one."
    }
    $q1n1Dir = $candidates[0].FullName
}
"""
    script = f"""
$ErrorActionPreference='Stop'
{select_dir}
Set-Location -LiteralPath $q1n1Dir
.\\boot-a16-q1n1.ps1
"""
    print('asking Windows to arm BootNext for the q1n1 shell entry and restart')
    result = run_powershell(args, script)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode:
        raise SystemExit(f'remote boot request failed: {result.stderr.strip() or result.returncode}')


def reboot_from_proxy(_args):
    paths = ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE)
    if not paths:
        return False
    proxy = q1n1proxy.connect()
    print(f'asking the EL2 proxy on {proxy.endpoint} to reset')
    try:
        info = proxy.bootinfo()
        if not info['return_armed']:
            print('warning: BootNext is not armed, so this reset goes to Windows')
        proxy.reboot()
        # The reset request has no reply. Keep the tty open until the target
        # disconnects, both to let queued bytes leave and to avoid claiming the
        # old proxy as a successful automatic boot if the reset never happened.
        wait_for(lambda: proxy.endpoint not in ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE),
                 15, 'the proxy did not disconnect after the reset request')
    finally:
        proxy.link.close()
    return True


def command_status(args):
    where = state(args)
    print(f'A16 state: {where}')
    if where == 'proxy':
        try:
            proxy = q1n1proxy.connect()
            try:
                info = proxy.bootinfo()
                endpoint = proxy.endpoint
            finally:
                proxy.link.close()
        except (q1n1proxy.ProxyError, OSError) as problem:
            print(f'  port present but the target is not answering ({problem})')
            print('  no proxy handshake succeeded; check cable and port status')
            return 1
        print(f"  EL{info['current_el']} proxy, image {info['image_base']:#x}, heap {info['heap_base']:#x}")
        print(f"  BootCurrent {info['boot_current']:#06x}, reset returns to q1n1: {bool(info['return_armed'])}")
        console = ports(PROXY_SERIAL, q1n1proxy.CONSOLE_INTERFACE)
        print(f"  proxy endpoint {endpoint}"
              + (f", console port {console[0]}" if console else ''))
    elif where == 'boot-window':
        print(f'  waiting for a choice on {ports(BOOT_SERIAL)[0]} (default: Windows)')
    elif where == 'windows':
        result = run_powershell(args, '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")')
        print(f'  ssh reachable, booted {result.stdout.strip()}')
    else:
        print('  no q1n1 USB port and no ssh; press the power button or check the USB cable')
    return 0


def command_boot(args):
    target = args.target
    choice = CHOICES[target]
    where = state(args)
    print(f'A16 is in: {where}; target: {target}')
    if where == 'boot-window':
        claim_window(args, choice, args.wait)
    elif where == 'proxy':
        if target == 'q1n1' and args.skip_if_there:
            print('already in the q1n1 proxy')
            return 0
        reboot_from_proxy(args)
        time.sleep(2)
        claim_window(args, choice, args.wait)
    elif where == 'windows':
        if target == 'windows':
            print('already in Windows')
            return 0
        reboot_from_windows(args)
        claim_window(args, choice, args.wait)
    else:
        print('waiting for the A16 to reach a q1n1 boot window (press the power button if it is off)')
        claim_window(args, choice, args.wait)

    if target == 'hold':
        print('boot window held open; drive it with tools/a16ucsi.py, then: a16ctl.py claim windows')
        return 0
    if target.startswith('q1n1'):
        paths = wait_for(lambda: ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE), 120,
                         'the EL2 proxy port did not appear')
        proxy = q1n1proxy.connect(wait=30)
        try:
            info = proxy.bootinfo()
        finally:
            proxy.link.close()
        print(f"q1n1 proxy ready on {proxy.endpoint}: EL{info['current_el']}, "
              f"heap {info['heap_base']:#x}, reset returns to q1n1: {bool(info['return_armed'])}")
    elif target == 'windows':
        wait_for(lambda: ssh_reachable(args), 300, 'Windows did not come back on the network')
        print('Windows is back (ssh reachable)')
    return 0


def command_claim(args):
    claim_window(args, CHOICES[args.target], args.wait)
    return 0


def command_console(args):
    paths = wait_for(lambda: ports(PROXY_SERIAL, q1n1proxy.CONSOLE_INTERFACE), args.wait or 1,
                     'no q1n1 console port')
    print(f'q1n1 console on {paths[0]} (ctrl-c to stop)')
    transport = q1n1proxy.Transport(paths[0])
    try:
        while True:
            try:
                sys.stdout.write(transport.read(1, 1.0).decode('utf-8', 'replace'))
                sys.stdout.flush()
            except q1n1proxy.ProxyTimeout:
                continue
    except KeyboardInterrupt:
        print()
    finally:
        transport.close()
    return 0


def live_battery_flags(args):
    flags = 0
    if args.survey:
        flags |= q1n1proxy.LIVE_SURVEY
    if args.restart:
        flags |= q1n1proxy.LIVE_RESTART
    flags |= {'auto': 0, 'linux': q1n1proxy.LIVE_LINUX, 'drain': q1n1proxy.LIVE_DRAIN,
              'resync': q1n1proxy.LIVE_RESYNC}[args.mode]
    if args.ucsi_version:
        flags |= q1n1proxy.LIVE_UCSI_VERSION
    if args.attr_unknown_ok:
        flags |= q1n1proxy.LIVE_ATTR_UNKNOWN_OK
    if args.release:
        flags |= q1n1proxy.LIVE_RELEASE
    if args.force:
        flags |= q1n1proxy.LIVE_FORCE
    if args.refresh == 'on':
        flags |= q1n1proxy.LIVE_REFRESH_ON
    elif args.refresh == 'off':
        flags |= q1n1proxy.LIVE_REFRESH_OFF
    return flags


def descriptor_note(r):
    """What the four words mean for a restart (docs/A16-LIVE-BATTERY.md)."""
    if not (r['desc0'] | r['desc1'] | r['desc2'] | r['desc3']):
        return 'all 0: as ExitBootServices leaves it; a restart is automatic'
    if r['desc0'] | r['desc3']:
        return ('words 0/3 not 0: a live q1n1 session continues; otherwise the SoCCP never cleaned up, '
                'and a restart needs --mode (linux/drain also --force)')
    return 'words 0/3 are 0 but 1/2 are not: not a state the firmware leaves; investigate before any restart'


def el2_only(args):
    """The flags that mean nothing to the boot window's firmware reading."""
    return (args.survey or args.restart or args.mode != 'auto' or args.ucsi_version or args.attr_unknown_ok
            or args.release or args.refresh or args.timeout or args.host or args.smem_base or args.smem_size
            or args.force)


def print_live_record(record, full):
    r = record
    status = q1n1proxy.LIVE_STATUS.get(r['status'], 'unknown')
    phase = q1n1proxy.LIVE_PHASES.get(r['phase'], str(r['phase']))
    print(f"  status {r['status']} ({status}), phase {phase}, {r['elapsed_us'] / 1000:.1f} ms, "
          f"call {r['calls']} ({r['successes']} ok)")
    target = 'faulted' if r['target_pointer'] == 0xFFFFFFFFFFFFFFFF else f"{r['target_pointer']:#x}"
    print(f"  target info @0x01fd4000 -> {target}, status {r['target_status']}"
          + (f", base {r['target_base']:#x} size {r['target_size']:#x}" if r['target_base'] else ''))
    attr = lambda v: 'unknown' if v == 0xFFFFFFFFFFFFFFFF else f'{v:#04x}'
    print(f"  SMEM {r['smem_base']:#x}+{r['smem_size']:#x}: status {r['smem_status']}, heap v{r['smem_version']}, "
          f"{r['smem_items']} items, {r['ptable_entries']} partitions, MAIR attr {attr(r['smem_attr'])}")
    if full:
        for row in r['partition']:
            print(f"    partition hosts {row[0] & 0xffff}/{row[0] >> 16} @ {row[1]:#x} size {row[2]:#x} "
                  f"cacheline {row[3]}")
    print(f"  host {r['remote_host']} partition: status {r['partition_status']}, @ {r['partition_offset']:#x} "
          f"size {r['partition_size']:#x}")
    if r['desc_offset']:
        print(f"  items: 478 @ {r['desc_offset']:#x}; 479 @ {r['tx_offset']:#x} len {r['tx_length']:#x}"
              f"{' (cached list)' if r['tx_cached'] else ''}; 480 @ {r['rx_offset']:#x} len {r['rx_length']:#x}"
              f"{' (cached list)' if r['rx_cached'] else ''}")
        print(f"  descriptor before this call: tx tail {r['desc0']:#x} tx head {r['desc1']:#x} "
              f"rx tail {r['desc2']:#x} rx head {r['desc3']:#x} (stable: {r['sample_status'] == 0}; "
              f"{descriptor_note(r)})")
        print(f"  descriptor after this call: {r['desc_end0']:#x} {r['desc_end1']:#x} {r['desc_end2']:#x} "
              f"{r['desc_end3']:#x}; MAIR attr descriptor {attr(r['desc_attr'])} tx fifo {attr(r['tx_attr'])} "
              f"rx fifo {attr(r['rx_attr'])}")
    print(f"  SMP2P 617 slave-kernel: status {r['smp2p_status']}, value {r['smp2p_value']:#x}")
    ipcc = 'faulted' if r['ipcc_version'] == 0xFFFFFFFFFFFFFFFF else f"{r['ipcc_version']:#x}"
    print(f"  IPCC version {ipcc}, MAIR attr {attr(r['ipcc_attr'])}, doorbell {r['doorbell_value']:#x} -> "
          f"{r['doorbell_address']:#x}")
    if r['restarts'] or r['adoptions'] or full:
        print(f"  GLINK state {r['glink_state']} error {r['glink_error']} (cmd {r['glink_command']}), "
              f"mode {r['glink_mode']}, lcid {r['lcid']} rcid {r['rcid']:#x}, features {r['features']:#x}, "
              f"version acked {r['version_acked']}/remote {r['remote_version']}, open sent {r['open_sent']} "
              f"acked {r['open_acked']} remote {r['remote_opened']}, remote intents {r['remote_intents']}")
        print(f"  restarts {r['restarts']}, continued {r['adoptions']}, tx {r['tx_frames']} rx {r['rx_frames']} "
              f"frames, {r['doorbells']} doorbells, {r['messages']} messages, dropped {r['dropped']}, "
              f"foreign opens {r['foreign_opens']}, protocol errors {r['protocol_errors']}")
        print(f"  serviced {r['service_polls']} times ({r['service_frames']} frames), version-0 acks ignored "
              f"{r['version_zero_acks']}, remote closes {r['remote_closes']}, glink_ssr opened by SoCCP "
              f"{r['ssr_opened']}; releases {r['releases']} (DO_CLEANUP sent {r['cleanup_sent']}, "
              f"cleanup_done {r['cleanup_done']}, status {r['release_status']})"
              + ('; previous stage session not adopted' if r['adopt_refused'] else ''))
        print(f"  PMIC: {r['pmic_requests']} requests, {r['pmic_replies']} replies, {r['pmic_stale']} stale, "
              f"{r['pmic_unsolicited']} unsolicited, {r['pmic_foreign']} other owners, {r['pmic_bad']} bad; "
              f"last reply owner {r['reply_owner']:#x} type {r['reply_type']} opcode {r['reply_opcode']:#x} "
              f"length {r['reply_length']} error {r['reply_error']:#x}")


def command_battery(args):
    """Battery and AC state from the boot window or from the EL2 proxy.

    In the boot window the reading uses the firmware's PMIC GLINK driver. In
    the EL2 proxy (after ExitBootServices) q1n1 runs its own GLINK channel to
    the SoCCP (platform/uefi/live-battery.h); --survey only locates it."""
    paths = ports(BOOT_SERIAL)
    if paths and not args.live and el2_only(args):
        # Sending these to the boot window would silently run the firmware's
        # PMIC read instead of what was asked (a read-only survey, a release).
        raise SystemExit('these battery flags are for the EL2 proxy, but the boot window is up; '
                         'boot q1n1 first (or pass --live if the proxy is up too)')
    if paths and not args.live:
        window = BootWindow(paths[0], verbose=args.verbose)
        try:
            answer = window.ask('battery', ('BATTERY',))
        finally:
            window.close()
        print(answer)
        return 0 if answer.startswith('BATTERY ') else 1
    if not ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE):
        raise SystemExit('battery status needs the q1n1 boot window or the EL2 proxy; found neither')
    proxy = q1n1proxy.connect()
    try:
        status, record = proxy.live_battery(live_battery_flags(args), args.timeout, args.host,
                                            args.smem_base, args.smem_size)
    finally:
        proxy.link.close()
    if status == 0 and not (args.survey or args.ucsi_version or args.release):
        print(f"BATTERY {record['text']}")
    elif status == 0 and args.ucsi_version:
        print(f"UCSI VERSION {record['ucsi_version']:#06x} (0x0210 expected)")
    elif status == 0 and args.release:
        print(f"RELEASED {record['text']}")
    else:
        print('SURVEY' if args.survey else f"FAILED: {record['text']}")
    if status or args.survey or args.verbose:
        print_live_record(record, args.verbose or args.survey)
    return 0 if status == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ssh-target', default=DEFAULT_TARGET)
    parser.add_argument('--ssh-control', help=f'ssh ControlPath (default: {DEFAULT_CONTROL} when it exists)')
    parser.add_argument('--host-key-alias', default=DEFAULT_ALIAS)
    parser.add_argument('--wait', type=float, default=240, help='seconds to wait for the boot window')
    parser.add_argument('--verbose', action='store_true', help='print every boot-window line')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='report where the A16 is')
    boot = sub.add_parser('boot', help='bring the A16 into a state')
    boot.add_argument('target', nargs='?', default='q1n1', choices=sorted(CHOICES))
    boot.add_argument('--skip-if-there', action='store_true')
    claim = sub.add_parser('claim', help='answer the next boot window without rebooting')
    claim.add_argument('target', nargs='?', default='q1n1', choices=sorted(CHOICES))
    sub.add_parser('console', help='stream the q1n1 EL2 console port')
    battery = sub.add_parser('battery', help='battery charge and AC state (boot window or EL2 proxy)')
    battery.add_argument('--live', action='store_true', help='use the EL2 path even if a boot window is present')
    battery.add_argument('--survey', action='store_true',
                         help='EL2: locate SMEM, the SoCCP partition, GLINK items and IPCC; write nothing')
    battery.add_argument('--restart', action='store_true',
                         help='EL2: restart the GLINK link even if it could continue (needs --mode unless all four '
                              'words are 0)')
    battery.add_argument('--mode', choices=('auto', 'linux', 'drain', 'resync'), default='auto',
                         help='EL2: how a restart sets our index words (glink-smem.h); auto restarts only '
                              'when all four words are 0, and then Linux\'s way')
    battery.add_argument('--ucsi-version', action='store_true', help='EL2: read UCSI VERSION (known answer 0x0210)')
    battery.add_argument('--attr-unknown-ok', action='store_true',
                         help='EL2: proceed when a page attribute cannot be read (never when it is cacheable)')
    battery.add_argument('--force', action='store_true',
                         help="EL2: with --mode linux/drain, restart even though the SoCCP's words 0/3 are not 0 "
                              '(it never cleaned up; the plan says stop)')
    battery.add_argument('--release', action='store_true',
                         help="EL2: send the firmware's glink_ssr DO_CLEANUP and leave all four GLINK words 0")
    battery.add_argument('--refresh', choices=('on', 'off'), help='EL2: panel re-reads every 30 s')
    battery.add_argument('--timeout', type=int, default=0, help='EL2: milliseconds (default 1500)')
    battery.add_argument('--host', type=int, default=0, help='EL2: SMEM host id of the SoCCP (default 19)')
    battery.add_argument('--smem-base', type=lambda v: int(v, 0), default=0, help='EL2: SMEM base override')
    battery.add_argument('--smem-size', type=lambda v: int(v, 0), default=0, help='EL2: SMEM size override')
    args = parser.parse_args()
    return {'status': command_status, 'boot': command_boot,
            'claim': command_claim, 'console': command_console,
            'battery': command_battery}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
