#!/usr/bin/env python3
"""Boot the real EFI binary in QEMU; inspect serial and capture its framebuffer.

QEMU validates the UEFI target, not Qualcomm Secure Launch or the GENI driver.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import socket
import struct
import subprocess
import time


def run_proxy_checks(socket_path):
    """Drive the post-ExitBootServices proxy over QEMU's PL011 with the real client."""
    spec = importlib.util.spec_from_file_location('q1n1proxy', Path(__file__).resolve().parent / 'q1n1proxy.py')
    assert spec and spec.loader
    q1n1proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q1n1proxy)
    done = []

    def check(condition, label):
        if not condition:
            raise RuntimeError(f'proxy check failed: {label}')
        done.append(label)

    for attempt in range(40):
        try:
            link = q1n1proxy.Link(socket_path, timeout=10)
            break
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.25)
    else:
        raise RuntimeError('could not connect to the QEMU proxy socket')
    proxy = q1n1proxy.Proxy(link)
    link.nop(0)
    check(proxy.nop() is not None, 'P_NOP')
    info = proxy.bootinfo()
    check(info['magic'] == q1n1proxy.MAGIC, 'bootinfo magic')
    check(info['current_el'] == 2 and info['entry_el'] == 2, 'proxy runs at EL2 after ExitBootServices')
    check(info['mode'] == 2, 'UART proxy mode reported')
    check(info['heap_base'] and info['memory_map'] and info['fb_base'] and info['timer_hz'],
          'bootinfo heap, memory map, framebuffer and timer')

    heap = info['heap_base']
    payload = random.randbytes(128 * 1024)
    proxy.writemem(heap, payload)
    check(proxy.readmem(heap, len(payload)) == payload, '128 KiB memory round trip')
    proxy.write32(heap, 0xA5A5A5A5)
    check(proxy.read32(heap) == 0xA5A5A5A5, 'register write/read')

    # An unmapped VA above the 48-bit range: the guard must convert the fault.
    bad = 1 << 48
    proxy.set_exc_guard(q1n1proxy.GUARD_MARK)
    check(proxy.read32(bad) & 0xffffffff == q1n1proxy.GUARD_MARKER & 0xffffffff, 'GUARD_MARK marks a faulting read')
    check(proxy.get_exc_count() == 1, 'guarded fault counted')
    proxy.set_exc_guard(q1n1proxy.GUARD_OFF)
    try:
        proxy.readmem(bad, 64)
        check(False, 'memread of a bad address is refused')
    except q1n1proxy.ProxyRemoteError:
        check(True, 'memread of a bad address is refused')
    check(proxy.nop() is not None, 'proxy survives the guarded memread fault')

    # Upload and run code, then upload code that faults with no guard: the
    # target reports the exception over the same link and resumes.
    code = struct.pack('<3I', 0xD2824680, 0xD65F03C0, 0xD503201F)  # movz x0,#0x1234; ret; nop
    proxy.writemem(heap + 0x1000, code)
    proxy.flush_code(heap + 0x1000, len(code))
    check(proxy.call(heap + 0x1000) == 0x1234, 'P_CALL runs uploaded code')
    faulting = struct.pack('<3I', 0xD2E00020, 0xF9400000, 0xD65F03C0)  # movz x0,#1,lsl#48; ldr x0,[x0]; ret
    proxy.writemem(heap + 0x2000, faulting)
    proxy.flush_code(heap + 0x2000, len(faulting))
    returned = proxy.call(heap + 0x2000, timeout=20)
    check(len(proxy.exceptions) == 1, 'unguarded exception reported to the host')
    check(proxy.exceptions[0]['el'] == 2 and proxy.exceptions[0]['far'] == 1 << 48,
          'exception report carries EL and FAR')
    check(returned == 1 << 48, 'target resumed past the faulting instruction')

    # The EL2 timer tick: interrupts taken at EL2 through the GICv3, which is
    # what will service the link once a guest owns the CPU.
    def gic_stats():
        fields = struct.unpack('<9Q5I', proxy.readmem(info['gic_stats'], 92, live=True))
        names = 'distributor redistributor sgi interval ticks spurious other serviced skipped'.split()
        names += 'intid last_intid rate ready status'.split()
        return dict(zip(names, fields))

    before = gic_stats()
    check(info['gic_distributor'] and info['gic_redistributor'],
          f'GIC bases from the MADT (init status {before["status"]}, rsdp {info["acpi_rsdp"]:#x}, '
          f'gicd {info["gic_distributor"]:#x}, gicr {info["gic_redistributor"]:#x})')
    check(before['ready'] == 1 and before['intid'] == 26, 'EL2 physical timer PPI armed (INTID 26)')
    time.sleep(1.0)
    after = gic_stats()
    delta = after['ticks'] - before['ticks']
    check(delta > 100, f'timer interrupts are firing at EL2 ({delta} ticks in ~1 s, rate {after["rate"]})')
    check(after['last_intid'] == 26 and after['spurious'] == 0 and after['other'] == 0,
          f'only the timer PPI arrives (last {after["last_intid"]}, spurious {after["spurious"]}, other {after["other"]})')

    # Chainload: upload a flat stage, jump to it, and prove the new code is the
    # one answering (it inherits bootinfo and increments stage_generation).
    stage = Path(__file__).resolve().parents[1] / 'build' / 'uefi' / 'q1n1-stage-qemu.bin'
    check(info['stage_base'] and info['stage_size'], f'stage region reserved at {info["stage_base"]:#x}')
    check(stage.exists(), 'stage binary built')
    check(info['stage_generation'] == 0, 'running payload is generation 0')
    # Chainload twice: the second one must land in the other slot, because a
    # stage cannot be overwritten while it is the code executing.
    seen = []
    for generation in (1, 2):
        chosen, data, link = proxy.pick_stage(stage)
        check(link != proxy.bootinfo()['image_base'], f'slot {link:#x} is not the running image')
        proxy.chainload(data)
        time.sleep(3)
        proxy.nop()
        after = proxy.bootinfo()
        check(after['stage_generation'] == generation,
              f'chainload {generation}: stage answers (generation {after["stage_generation"]})')
        check(after['image_base'] == link,
              f'chainload {generation}: stage reports its real load address {after["image_base"]:#x} == {link:#x}')
        check(after['heap_base'] == info['heap_base'] and after['memory_map'] == info['memory_map'],
              f'chainload {generation}: inherited the heap and memory map')
        check(proxy.read32(after['heap_base']) is not None, f'chainload {generation}: memory still readable')
        stage_gic = struct.unpack('<9Q5I', proxy.readmem(after['gic_stats'], 92, live=True))
        check(stage_gic[13] == 0 and stage_gic[9] == 26, f'chainload {generation}: own GIC tick running')
        seen.append(link)
    check(seen[0] != seen[1], f'the two chainloads used different slots ({seen[0]:#x}, {seen[1]:#x})')

    tables = proxy.acpi_tables()
    check('SPCR' in tables and 'DSDT' in tables or 'FACP' in tables, f'ACPI tables readable: {sorted(tables)}')
    stats = struct.unpack('<12Q', proxy.readmem(info['proxy_stats'], 96))
    check(stats[9] == 1, 'target exception counter')
    return done, proxy


parser = argparse.ArgumentParser()
parser.add_argument('--el', choices=['1', '2'], default='2')
mode_args = parser.add_mutually_exclusive_group()
mode_args.add_argument('--preflight', action='store_true')
mode_args.add_argument('--fault', action='store_true')
mode_args.add_argument('--usb-probe', action='store_true')
mode_args.add_argument('--usb-role-probe', action='store_true')
mode_args.add_argument('--usb-role-change', action='store_true', help='Check active request refuses an unsupported firmware in QEMU')
mode_args.add_argument('--usb-controller-probe', action='store_true', help='Check RAM inventory refuses unsupported firmware')
mode_args.add_argument('--usb-serial', action='store_true', help='Check serial test refuses unsupported firmware')
mode_args.add_argument('--usb-serial-v3', action='store_true', help='Check inactive-start serial test refuses unsupported firmware')
mode_args.add_argument('--usb-typec-probe', action='store_true', help='Check passive Type-C probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-probe', action='store_true', help='Check UCSI buffer probe refuses unsupported firmware')
mode_args.add_argument('--usb-usbc-probe', action='store_true', help='Check Qualcomm USB-C buffer probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-reg-probe', action='store_true', help='Check UCSI register probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-status', action='store_true', help='Check connector queries refuse unsupported firmware')
mode_args.add_argument('--usb-ucsi-status-v2', action='store_true', help='Check full-block connector queries refuse unsupported firmware')
mode_args.add_argument('--usb-ucsi-device0', action='store_true', help='Check data-role request and serial branch refuse unsupported firmware')
mode_args.add_argument('--usb-ebs', action='store_true', help='Check EL2 USB takeover prep and takeover refuse unsupported firmware')
mode_args.add_argument('--usb-ebs-fixture', action='store_true', help='Run the QEMU-only takeover fixture through ExitBootServices and ResetSystem')
mode_args.add_argument('--proxy', action='store_true', help='Run the EL2 proxy over a PL011 and drive it with tools/q1n1proxy.py')
mode_args.add_argument('--boot-window', action='store_true', help='Check the A16 boot window refuses unsupported firmware and continues to Windows')
parser.add_argument('--firmware', default='/opt/homebrew/share/qemu/edk2-aarch64-code.fd')
parser.add_argument('--build-dir', type=Path, help='Use binaries from an isolated build directory')
args = parser.parse_args()
if args.usb_serial_v3:
    args.usb_serial = True
if args.usb_ucsi_status_v2 or args.usb_ucsi_device0:
    args.usb_ucsi_status = True
root = Path(__file__).resolve().parents[1]
build_dir = args.build_dir.resolve() if args.build_dir else root / 'build' / 'uefi'
probe = args.usb_probe or args.usb_role_probe
active_usb = args.usb_role_change or args.usb_serial
mode = 'usb-role-change' if args.usb_role_change else 'usb-role-probe' if args.usb_role_probe else 'usb-probe' if probe else 'preflight' if args.preflight else 'fault' if args.fault else 'boot'
if args.usb_serial:
    mode = 'usb-serial-v3' if args.usb_serial_v3 else 'usb-serial'
if args.usb_typec_probe:
    mode = 'usb-typec-probe'
if args.usb_ucsi_probe:
    mode = 'usb-ucsi-probe'
if args.usb_usbc_probe:
    mode = 'usb-usbc-probe'
if args.usb_ucsi_reg_probe:
    mode = 'usb-ucsi-reg-probe'
if args.usb_ucsi_status:
    mode = 'usb-ucsi-status-v2' if args.usb_ucsi_status_v2 else 'usb-ucsi-status'
if args.usb_ucsi_device0:
    mode = 'usb-ucsi-device0'
if args.usb_controller_probe:
    mode = 'usb-controller-probe'
if args.usb_ebs or args.usb_ebs_fixture:
    mode = 'usb-ebs-fixture' if args.usb_ebs_fixture else 'usb-ebs'
if args.proxy:
    mode = 'proxy'
if args.boot_window:
    mode = 'boot-window'
out = build_dir / f'qemu-el{args.el}-{mode}'
esp = out / 'esp'
esp.mkdir(parents=True, exist_ok=True)
binary = 'q1n1-usb-role-change.efi' if args.usb_role_change else 'q1n1-usb-role-probe.efi' if args.usb_role_probe else 'q1n1-usb-probe.efi' if probe else 'q1n1.efi'
if args.usb_serial:
    binary = 'q1n1-usb-serial-v3.efi' if args.usb_serial_v3 else 'q1n1-usb-serial.efi'
if args.usb_typec_probe:
    binary = 'q1n1-usb-typec-probe.efi'
if args.usb_ucsi_probe:
    binary = 'q1n1-usb-ucsi-probe.efi'
if args.usb_usbc_probe:
    binary = 'q1n1-usb-usbc-probe.efi'
if args.usb_ucsi_reg_probe:
    binary = 'q1n1-usb-ucsi-reg-probe.efi'
if args.usb_ucsi_status:
    binary = 'q1n1-usb-ucsi-status-v2.efi' if args.usb_ucsi_status_v2 else 'q1n1-usb-ucsi-status.efi'
if args.usb_ucsi_device0:
    binary = 'q1n1-usb-ucsi-device0.efi'
if args.usb_controller_probe:
    binary = 'q1n1-usb-controller-probe.efi'
if args.usb_ebs:
    binary = 'q1n1-usb-ebs.efi'
    shutil.copyfile(build_dir / 'q1n1-usb-ebs-prep.efi', esp / 'q1n1-usb-ebs-prep.efi')
if args.usb_ebs_fixture:
    binary = 'usb-ebs-fixture.efi'
shutil.copyfile(build_dir / binary, esp / binary)
options = '' if args.preflight or probe else '--el2 --uart'
if active_usb:
    options = '--device0'
if args.usb_controller_probe:
    options = ''
if args.usb_typec_probe or args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
    options = ''
if args.fault:
    options += ' --fault-test'
if args.usb_ucsi_device0:
    options = '--device0'
if args.usb_ebs or args.usb_ebs_fixture:
    options = '--takeover'
if args.proxy:
    options = '--uart-proxy'
if args.boot_window:
    options = '--boot'
setup = ''
if args.usb_role_probe:
    shutil.copyfile(root / 'build/uefi/usb-role-fixture.efi', esp / 'usb-role-fixture.efi')
    setup = 'load usb-role-fixture.efi\n'
branch = ''
if args.usb_ebs:
    setup = 'q1n1-usb-ebs-prep.efi --device0\necho Q1N1_PREP_RETURNED\n'
if args.usb_ucsi_device0:
    branch = 'if %lasterror% == 0 then\n  echo Q1N1_UNEXPECTED_SERIAL_BRANCH\nelse\n  echo Q1N1_ROLE_REJECT_BRANCH_OK\nendif\n'
(esp / 'startup.nsh').write_text(f'fs0:\n{setup}{binary} {options}\n{branch}echo Q1N1_SHELL_RETURN_OK\n')
qmp_path = f'/tmp/q1n1-qmp-{os.getpid()}.sock'
serial = out / 'serial.log'
# Popen can return before QEMU truncates its serial file. Never accept a boot
# marker or a success report left behind by an earlier run in this directory.
for stale in (serial, out / 'result.json', out / 'screen.ppm'):
    stale.unlink(missing_ok=True)
proxy_socket = f'/tmp/q1n1-proxy-{os.getpid()}.sock'
# The proxy needs a bidirectional console; the log file still records everything.
serial_args = (['-chardev', f'socket,id=q1n1serial,path={proxy_socket},server=on,wait=off,logfile={serial}',
                '-serial', 'chardev:q1n1serial'] if args.proxy else ['-serial', f'file:{serial}'])
cmd = ['qemu-system-aarch64', '-machine',
       f'virt,virtualization={"on" if args.el == "2" else "off"},gic-version=3',
       '-accel', 'tcg', '-cpu', 'max', '-smp', '1', '-m', '512M',
       '-bios', args.firmware, '-drive', f'format=raw,file=fat:rw:{esp}',
       '-device', 'ramfb', '-display', 'none', '-monitor', 'none', '-nic', 'none',
       *serial_args, '-qmp', f'unix:{qmp_path},server=on,wait=off',
       '-no-reboot']
if probe:
    cmd += ['-device', 'qemu-xhci']
expected = ('REFUSED: Matching BIOS312 UsbConfigDxe code not found.' if active_usb else
            'Discovery complete. Returning to shell.' if probe else
            'Preflight only.' if args.preflight else
            'EL2 CONFIRMED AFTER EXITBOOTSERVICES' if args.el == '2' else
            'EL2 NOT REACHED - STILL IN EL1')
if args.usb_typec_probe:
    expected = 'REFUSED: Matching BIOS312 UsbPwrCtrlDxe code not found.'
if args.usb_controller_probe:
    expected = 'REFUSED: Loaded UsbConfigDxe is not the inspected BIOS312 image.'
if args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
    expected = 'REFUSED: Matching BIOS312 PmicGlinkDxe code not found.'
if args.usb_ebs:
    expected = 'REFUSED: no valid prep snapshot from this boot. Firmware not exited.'
if args.usb_ebs_fixture:
    expected = 'Snapshot valid. Leaving firmware'
if args.proxy:
    expected = 'Leaving firmware for the q1n1 proxy.'
if args.boot_window:
    expected = 'REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.'
start = time.monotonic()
with (out / 'qemu.log').open('w') as err:
    proc = subprocess.Popen(cmd, stdout=err, stderr=err)
    try:
        deadline = start + 55
        found = False
        while time.monotonic() < deadline and proc.poll() is None:
            if serial.exists() and expected in serial.read_text(errors='replace'):
                found = True
                break
            time.sleep(0.2)
        if not found:
            raise RuntimeError(f'Boot marker missing: {expected}; inspect {serial}')
        proxy_checks, proxy = (None, None)
        if args.proxy:
            proxy_checks, proxy = run_proxy_checks(proxy_socket)
        # Give the console, logo and optional exception screen time to finish.
        # The fixture needs ExitBootServices plus the driver's 1 s reset timeout.
        time.sleep(6 if args.usb_ebs_fixture else 1)
        with socket.socket(socket.AF_UNIX) as qmp:
            qmp.connect(qmp_path)
            stream = qmp.makefile('rwb', buffering=0)
            json.loads(stream.readline())
            for request in [dict(execute='qmp_capabilities'),
                            dict(execute='screendump', arguments={'filename': str(out / 'screen.ppm')})]:
                stream.write(json.dumps(request).encode() + b'\n')
                while True:
                    reply = json.loads(stream.readline())
                    if 'error' in reply:
                        raise RuntimeError(reply)
                    if 'return' in reply:
                        break
        if args.proxy:
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            # No status bar any more: check the console actually rendered text.
            lit = sum(1 for n in range(0, len(ppm[3]), 3) if ppm[3][n] or ppm[3][n+1] or ppm[3][n+2])
            assert lit > 20000, f'proxy console drew almost nothing ({lit} lit pixels)'
            assert proxy is not None
            proxy.reboot()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise RuntimeError('P_REBOOT did not reset QEMU')
            print(f'{len(proxy_checks)} proxy checks: ' + '; '.join(proxy_checks))
        elif args.boot_window:
            log = serial.read_text(errors='replace')
            assert 'Q1N1_SHELL_RETURN_OK' in log, 'boot window did not return to the shell'
            assert 'Leaving firmware' not in log
            assert 'q1n1 boot window v1' in log
            assert 'REQUESTING USB0 DEVICE MODE' not in log
        elif args.usb_ebs_fixture:
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            assert tuple(ppm[3][:3]) == (53, 227, 139), 'Post-EBS fixture did not draw the EL2 status bar'
            log = serial.read_text(errors='replace')
            assert 'QEMU FIXTURE: fake controller in RAM' in log and 'Q1N1_SHELL_RETURN_OK' not in log
            # The fixture's driver times out, stops, waits 20 s, then calls ResetSystem;
            # QEMU runs with -no-reboot, so a working reset ends the process.
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise RuntimeError('ResetSystem after ExitBootServices did not end QEMU')
        elif args.usb_ebs:
            log = serial.read_text(errors='replace')
            assert 'REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.' in log
            assert 'Q1N1_PREP_RETURNED' in log and 'Q1N1_SHELL_RETURN_OK' in log
            assert 'REQUESTING USB0 DEVICE MODE' not in log and 'Snapshot valid' not in log
            assert 'Prep complete' not in log
        elif args.preflight or probe or active_usb or args.usb_controller_probe or args.usb_typec_probe or args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
            log = serial.read_text(errors='replace')
            assert 'Leaving firmware now.' not in log
            assert 'Q1N1_SHELL_RETURN_OK' in log, 'EFI application did not return to shell'
            if args.usb_controller_probe:
                assert 'No role change called.' in log
                assert 'CONTROLLER:' not in log
            if active_usb:
                assert 'No role change called.' in log
                assert 'REQUESTING USB0 DEVICE MODE' not in log
            if args.usb_typec_probe:
                assert 'No vendor callbacks called.' in log
                assert 'Snapshot complete.' not in log
            if args.usb_ucsi_probe:
                assert 'No UCSI read requested.' in log
                assert 'Requesting one 48-byte UCSI buffer read' not in log
            if args.usb_usbc_probe:
                assert 'No USB-C read requested.' in log
                assert 'Requesting one 132-byte Qualcomm USB-C status read' not in log
            if args.usb_ucsi_reg_probe or args.usb_ucsi_status:
                assert 'No UCSI register read requested.' in log
                assert 'Reading register:' not in log
                assert 'UCSI query/control:' not in log
                if args.usb_ucsi_device0:
                    assert 'Q1N1_ROLE_REJECT_BRANCH_OK' in log
                    assert 'Q1N1_UNEXPECTED_SERIAL_BRANCH' not in log
            if probe:
                assert 'USB FUNCTION: not installed' in log
                assert 'USB2 HOST handles: 0x0000000000000001' in log
                assert 'SERIAL IO handles: 0x0000000000000001' in log
                assert 'Revision: 0x0000000000010000' in log
                assert 'Path:' in log, 'Device-path conversion was not exercised'
                assert 'enumeration failed' not in log and 'Interface query failed' not in log
                if args.usb_role_probe:
                    assert 'QCOM USB CONFIG handles: 0x0000000000000003' in log
                    assert "Matches USB function driver's data checks." in log
                    assert "Matches XHCI host driver's data checks." in log
                    assert "Outside both drivers' selector range." in log
        else:
            # QEMU's P6 screenshot preserves the actual GOP scanout.
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            expected_rgb = (255, 48, 64) if args.fault else (53, 227, 139) if args.el == '2' else (255, 179, 71)
            assert tuple(ppm[3][:3]) == expected_rgb, 'Incorrect exception-level/fault framebuffer status'
        report = {'passed': True, 'el': int(args.el), 'mode': mode,
                  'expected': expected, 'seconds': round(time.monotonic()-start, 2),
                  'hardware_test': False}
        (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        print(out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        Path(qmp_path).unlink(missing_ok=True)
        if args.proxy:
            Path(proxy_socket).unlink(missing_ok=True)
