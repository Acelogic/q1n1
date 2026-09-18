#!/usr/bin/env python3
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
REMOTE_DIR = os.environ.get('A16_REMOTE_DIR', r'C:\q1n1-bringup')


def ports(serial, interface=1):
    try:
        if serial == PROXY_SERIAL:
            return q1n1proxy.find_ports(serial, interface)
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
    if ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE):
        return 'proxy'
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
        proxy = q1n1proxy.connect(paths[0], wait=15)
        try:
            info = proxy.bootinfo()
            if info['current_el'] != 2 or not info['return_armed']:
                raise SystemExit('direct proxy appeared but did not confirm EL2 and an armed return')
        finally:
            proxy.link.close()
        print(f'--auto reached the EL2 proxy directly on {paths[0]}')
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
    script = f"""
$ErrorActionPreference='Stop'
Set-Location '{REMOTE_DIR}'
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
    print(f'asking the EL2 proxy on {paths[0]} to reset')
    proxy = q1n1proxy.connect(paths[0])
    try:
        info = proxy.bootinfo()
        if not info['return_armed']:
            print('warning: BootNext is not armed, so this reset goes to Windows')
        proxy.reboot()
        # The reset request has no reply. Keep the tty open until the target
        # disconnects, both to let queued bytes leave and to avoid claiming the
        # old proxy as a successful automatic boot if the reset never happened.
        wait_for(lambda: paths[0] not in ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE),
                 15, 'the proxy did not disconnect after the reset request')
    finally:
        proxy.link.close()
    return True


def command_status(args):
    where = state(args)
    print(f'A16 state: {where}')
    if where == 'proxy':
        try:
            proxy = q1n1proxy.connect(ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE)[0])
            info = proxy.bootinfo()
        except (q1n1proxy.ProxyError, OSError) as problem:
            print(f'  port present but the target is not answering ({problem})')
            print('  the payload is wedged: hold the power button, then a16ctl.py boot q1n1')
            return 1
        print(f"  EL{info['current_el']} proxy, image {info['image_base']:#x}, heap {info['heap_base']:#x}")
        print(f"  BootCurrent {info['boot_current']:#06x}, reset returns to q1n1: {bool(info['return_armed'])}")
        console = ports(PROXY_SERIAL, q1n1proxy.CONSOLE_INTERFACE)
        print(f"  proxy port {ports(PROXY_SERIAL, q1n1proxy.PROXY_INTERFACE)[0]}"
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
        proxy = q1n1proxy.connect(paths[0], wait=30)
        info = proxy.bootinfo()
        print(f"q1n1 proxy ready on {paths[0]}: EL{info['current_el']}, "
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
    args = parser.parse_args()
    return {'status': command_status, 'boot': command_boot,
            'claim': command_claim, 'console': command_console}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
