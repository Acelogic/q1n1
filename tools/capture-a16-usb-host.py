#!/usr/bin/env python3
"""Read-only Mac capture of A16 USB enumeration and CDC driver binding.

Polls the IOService plane with properties for every node (`ioreg -l`), records
each change in the 1209:316d device subtree, all IOSerialBSDClient callout
paths, and a filtered unified-log stream. It never opens a serial port.
Run it alongside tools/test-a16-usb-serial.py, before the A16 reboots.
"""
import argparse
import glob
import json
import plistlib
import signal
import subprocess
import time
from pathlib import Path

VID, PID = 0x1209, 0x316d
SERIALS = ('A16-Q1N1-UEFI', 'A16-Q1N1-EL2', 'A16-Q1N1-BOOT')  # firmware UEFI app, q1n1 after ExitBootServices
LOG_PREDICATE = (
    '(process == "kernel" AND NOT senderImagePath CONTAINS "IOSurface" AND '
    '(senderImagePath CONTAINS[c] "usb" OR senderImagePath CONTAINS[c] "serial" OR '
    'senderImagePath CONTAINS "IOPort" OR eventMessage CONTAINS[c] "usb" OR '
    'eventMessage CONTAINS[c] "dext" OR eventMessage CONTAINS "matching" OR '
    'eventMessage CONTAINS "IOUserServer")) OR '
    'process IN {"kernelmanagerd", "usbpowerd", "icdd", "com.apple.ifdreader"} OR '
    'eventMessage CONTAINS "AppleUserECM" OR eventMessage CONTAINS[c] "usbmodem"')
DEVICE_KEYS = ('IORegistryEntryID', 'locationID', 'sessionID', 'USB Address', 'Device Speed',
               'USB Product Name', 'USB Vendor Name', 'USB Serial Number', 'idVendor', 'idProduct',
               'bcdUSB', 'bcdDevice', 'bDeviceClass', 'bDeviceSubClass', 'bDeviceProtocol',
               'bMaxPacketSize0', 'bNumConfigurations', 'kUSBCurrentConfiguration')
INTERFACE_KEYS = ('bInterfaceNumber', 'bAlternateSetting', 'bInterfaceClass', 'bInterfaceSubClass',
                  'bInterfaceProtocol', 'bNumEndpoints', 'bConfigurationValue')
DRIVER_KEYS = ('CFBundleIdentifier', 'IOClass', 'IOUserClass', 'IOUserServerName', 'IOProbeScore',
               'IOMatchCategory', 'IOTTYBaseName', 'IOTTYSuffix', 'IOCalloutDevice', 'IODialinDevice')


def utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def walk(node):
    yield node
    for child in node.get('IORegistryEntryChildren', []):
        yield from walk(child)


def ioreg_once(args):
    raw = subprocess.check_output(['ioreg', '-a', *args], stderr=subprocess.PIPE)
    return raw, plistlib.loads(raw) if raw.strip() else []


def ioreg(*args, attempts=5):
    for _ in range(attempts - 1):
        try:
            return ioreg_once(args)
        except (subprocess.CalledProcessError, plistlib.InvalidFileException):
            # The registry can change mid-walk ("ioreg: error: can't obtain child").
            time.sleep(0.1)
    return ioreg_once(args)


def pick(node, keys):
    return {key: node[key] for key in keys if key in node}


def driver_chain(node):
    """Every service below an interface (drivers, dext proxies, serial nubs)."""
    chain = []
    for child in node.get('IORegistryEntryChildren', []):
        entry = {'class': child.get('IOObjectClass'), 'name': child.get('IORegistryEntryName'),
                 **pick(child, DRIVER_KEYS)}
        below = driver_chain(child)
        if below:
            entry['children'] = below
        chain.append(entry)
    return chain


def summarize(device):
    interfaces, device_drivers = [], []
    for child in device.get('IORegistryEntryChildren', []):
        if child.get('IOObjectClass') == 'IOUSBHostInterface':
            interfaces.append({**pick(child, INTERFACE_KEYS), 'drivers': driver_chain(child)})
        else:
            device_drivers.append({'class': child.get('IOObjectClass'), 'name': child.get('IORegistryEntryName'),
                                   **pick(child, DRIVER_KEYS)})
    callouts = sorted(node['IOCalloutDevice'] for node in walk(device) if 'IOCalloutDevice' in node)
    classes = {node.get('IOObjectClass') for node in walk(device)}
    names = {node.get('IORegistryEntryName') for node in walk(device)}
    if not interfaces:
        state = 'present-unconfigured'
    elif callouts:
        state = 'tty-present'
    elif 'AppleUSBACMData' in classes or 'AppleUSBACMData' in names:
        state = 'acm-data-bound-no-tty'
    elif 'AppleUSBACMControl' in classes or 'AppleUSBACMControl' in names:
        state = 'acm-control-only'
    else:
        state = 'interfaces-without-acm'
    return {'state': state, 'identity_exact': device.get('USB Serial Number') in SERIALS,
            'serial': device.get('USB Serial Number'),
            'device': pick(device, DEVICE_KEYS), 'device_drivers': device_drivers,
            'interfaces': sorted(interfaces, key=lambda i: i.get('bInterfaceNumber', -1)),
            'callouts': callouts}


def snapshot():
    raw, roots = ioreg('-l', '-r', '-c', 'IOUSBHostDevice')
    devices = [node for root in roots for node in walk(root)
               if node.get('IOObjectClass') == 'IOUSBHostDevice'
               and (node.get('idVendor'), node.get('idProduct')) == (VID, PID)]
    _, clients = ioreg('-r', '-c', 'IOSerialBSDClient')
    serial = sorted(node.get('IOCalloutDevice', '?') for node in clients)
    return raw, {'a16': [summarize(device) for device in devices], 'serial_clients': serial,
                 'dev_cu': sorted(glob.glob('/dev/cu.*'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True, help='New directory for this capture (must not exist)')
    parser.add_argument('--duration', type=float, default=900, help='Seconds to capture (default 900)')
    parser.add_argument('--interval', type=float, default=0.5, help='Poll interval in seconds')
    parser.add_argument('--no-log-stream', action='store_true', help='Skip the unified-log stream')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)  # Never overwrite a prior experiment.
    events = (args.out / 'events.jsonl').open('a')
    stream = None
    if not args.no_log_stream:
        stream = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'ndjson', '--level', 'debug',
                                   '--predicate', LOG_PREDICATE],
                                  stdout=(args.out / 'unified-log.ndjson').open('wb'),
                                  stderr=(args.out / 'unified-log.stderr').open('wb'))
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print(f'{utc()} capturing to {args.out} for {args.duration:.0f}s; read-only, no serial opens', flush=True)
    previous, sequence, reached = None, 0, set()
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            try:
                raw, state = snapshot()
            except (subprocess.CalledProcessError, plistlib.InvalidFileException) as error:
                events.write(json.dumps({'utc': utc(), 'ioreg_error': str(error)}) + '\n')
                events.flush()
                time.sleep(args.interval)
                continue
            if state != previous:
                sequence += 1
                name = f'{sequence:03d}-ioreg-usb.plist'
                (args.out / name).write_bytes(raw)  # Raw full-property USB tree at this change.
                record = {'utc': utc(), 'sequence': sequence, 'snapshot': name, **state}
                events.write(json.dumps(record, default=str) + '\n')
                events.flush()
                summary = [f"{d['state']} loc={d['device'].get('locationID', 0):#x} serial={d['serial']!r} exact={d['identity_exact']} "
                           f"ifaces={[(i.get('bInterfaceClass'), i.get('bInterfaceSubClass'), i.get('bInterfaceProtocol'), [x['name'] for x in i['drivers']]) for i in d['interfaces']]} "
                           f"callouts={d['callouts']}" for d in state['a16']]
                reached.update(d['state'] for d in state['a16'])
                added = sorted(set(state['serial_clients']) - set(previous['serial_clients'] if previous else []))
                removed = sorted(set(previous['serial_clients']) - set(state['serial_clients'])) if previous else []
                print(f"{record['utc']} #{sequence} A16: {summary or 'absent'}; serial +{added} -{removed}", flush=True)
                previous = state
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if stream:
            stream.terminate()
            try:
                stream.wait(5)
            except subprocess.TimeoutExpired:
                stream.kill()
        final = {'utc': utc(), 'final': True, 'changes': sequence, 'states_reached': sorted(reached)}
        events.write(json.dumps(final) + '\n')
        events.close()
        print(f"{final['utc']} done: {sequence} changes; A16 states reached: {final['states_reached'] or 'none'}", flush=True)


if __name__ == '__main__':
    main()
