#!/usr/bin/env python3
"""Wait for the A16 test CDC device, disable local echo, and verify USB echo."""
import argparse
import fcntl
import glob
import os
import plistlib
import select
import struct
import subprocess
import termios
import time


def descendants(node):
    yield node
    for child in node.get('IORegistryEntryChildren', []):
        yield from descendants(child)


def inventory(expected_serial='A16-Q1N1-UEFI', data_interface=1):
    # -l is required: without it ioreg omits properties of descendant nodes,
    # so IOSerialBSDClient children never show IOCalloutDevice (v3 run blind spot).
    command = ['ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice']
    for _ in range(4):
        try:
            raw = subprocess.check_output(command)
            break
        except subprocess.CalledProcessError:
            # The registry can change mid-walk ("ioreg: error: can't obtain child").
            time.sleep(0.1)
    else:
        raw = subprocess.check_output(command)
    # ioreg succeeds with no output when the Mac has no host USB devices.
    # This is expected while the A16 still holds the host role.
    if not raw.strip():
        return [], []
    found = set()
    seen = []
    for root in plistlib.loads(raw):
        for device in descendants(root):
            if device.get('IOObjectClass') != 'IOUSBHostDevice' or (device.get('idVendor'), device.get('idProduct')) != (0x1209, 0x316d):
                continue
            serial = device.get('USB Serial Number')
            classes = sorted({node.get('IOObjectClass', '?') for node in descendants(device)} - {'IOUSBHostDevice'})
            paths = sorted(node['IOCalloutDevice'] for node in descendants(device) if 'IOCalloutDevice' in node)
            seen.append(f"location {device.get('locationID', 0):#x} serial {serial!r} classes {classes} callouts {paths}")
            if serial != expected_serial:
                continue
            # Open only the ACM port whose data interface was requested; the
            # post-ExitBootServices device exposes two ports (interfaces 1 and 3).
            for interface in device.get('IORegistryEntryChildren', []):
                if interface.get('IOObjectClass') != 'IOUSBHostInterface' or interface.get('bInterfaceNumber') != data_interface:
                    continue
                for node in descendants(interface):
                    path = node.get('IOCalloutDevice', '')
                    if path.startswith('/dev/cu.usbmodem') and os.path.exists(path):
                        found.add(path)
    return sorted(found), seen


def exchange(fd, payload, timeout=15):
    sent = 0
    received = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read, write, _ = select.select([fd], [fd] if sent < len(payload) else [], [], 0.2)
        if write:
            sent += os.write(fd, payload[sent:])
        if read:
            data = os.read(fd, 8192)
            if not data:
                raise RuntimeError('Serial device disconnected')
            received.extend(data)
            if payload in received:
                return len(payload), b'hello from q1n1' in received
            if len(received) > 65536:
                raise RuntimeError('Unexpected excess data from serial device')
    raise RuntimeError(f'Echo timeout: sent {sent}, received {len(received)} bytes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait', type=float, default=180, help='Seconds to wait for the exact A16 USB identity')
    parser.add_argument('--list', action='store_true', help='Only list matched serial ports; do not open')
    parser.add_argument('--serial', default='A16-Q1N1-UEFI', choices=['A16-Q1N1-UEFI', 'A16-Q1N1-EL2'],
                        help='Exact USB serial string: firmware UEFI app or q1n1 after ExitBootServices')
    parser.add_argument('--data-interface', type=int, default=1, choices=[1, 3],
                        help='CDC data interface whose port to open (EL2 device: 1 or 3)')
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait
    print(f'Looking for 1209:316d / {args.serial} data interface {args.data_interface}', flush=True)
    previous = None
    while True:
        matched, seen = inventory(args.serial, args.data_interface)
        if args.list:
            print('Matched ports:', matched)
            print('1209:316d devices:', seen)
            print('All USB modem paths:', glob.glob('/dev/cu.usbmodem*'))
            return
        state = (seen, sorted(glob.glob('/dev/cu.usbmodem*')))
        if state != previous:
            # Report identity/binding changes so silence is never mistaken for absence.
            stamp = time.strftime('%H:%M:%SZ', time.gmtime())
            print(f'{stamp} 1209:316d devices: {seen or "none"}; modem paths: {state[1] or "none"}', flush=True)
            previous = state
        if len(matched) > 1:
            raise RuntimeError('More than one matching A16 serial device; refusing ambiguous selection')
        if matched:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('A16 serial port did not appear')
        time.sleep(1)
    path = matched[0]
    print('Opening verified device:', path, flush=True)
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    old = None
    try:
        old = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0  # No line conversion, flow control, or local echo.
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack('I', termios.TIOCM_DTR | termios.TIOCM_RTS))
        for length in (317, 1024, 1537):
            payload = b'Q1N1-ECHO:' + os.urandom(length - 10)
            count, greeting = exchange(fd, payload)
            print(f'PASS exact binary echo: {count} bytes; greeting observed: {greeting}', flush=True)
        if args.serial == 'A16-Q1N1-EL2':
            print('PASS: physical USB serial echo from q1n1 after ExitBootServices.', flush=True)
        else:
            print('PASS: physical USB serial echo in UEFI. Post-ExitBootServices serial remains untested.', flush=True)
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except OSError:
                pass
        os.close(fd)


if __name__ == '__main__':
    main()
