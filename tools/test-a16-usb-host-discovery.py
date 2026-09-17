#!/usr/bin/env python3
"""Offline fixtures for the Mac-side A16 discovery tools. No USB or serial access."""
import copy
import importlib.util
import plistlib
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), TOOLS / f'{name}.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watcher = load('test-a16-usb-serial')
capture = load('capture-a16-usb-host')
CALLOUT = '/dev/cu.usbmodemA16Q1N1UEFI1'


def node(cls, name=None, children=(), **props):
    return {'IOObjectClass': cls, 'IORegistryEntryName': name or cls, 'IORegistryEntryChildren': list(children), **props}


def interface(number, triple, drivers):
    return node('IOUSBHostInterface', children=drivers, bInterfaceNumber=number, bInterfaceClass=triple[0],
                bInterfaceSubClass=triple[1], bInterfaceProtocol=triple[2])


def a16(serial: 'str | None' = 'A16-Q1N1-UEFI', data_driver='acm', configured=True):
    tty = node('IOSerialBSDClient', IOCalloutDevice=CALLOUT, IODialinDevice=CALLOUT.replace('cu.', 'tty.'))
    data = {'acm': [node('AppleUSBACMData', children=[node('IOModemSerialStreamSync', children=[tty])],
                         CFBundleIdentifier='com.apple.driver.usb.cdc.acm')],
            'ecm': [node('IOUserService', 'AppleUserECMData', IOUserServerName='com.apple.DriverKit.AppleUserECM.data')],
            'none': []}[data_driver]
    interfaces = [interface(0, (2, 2, 0), [node('AppleUSBACMControl')]), interface(1, (10, 0, 0), data)]
    device = node('IOUSBHostDevice', 'q1n1 A16 serial', [node('AppleUSBCDCCompositeDevice')] + (interfaces if configured else []),
                  idVendor=0x1209, idProduct=0x316d, locationID=0x112000, bDeviceClass=2)
    if serial is not None:
        device['USB Serial Number'] = serial
    # Nest below a dock hub as observed (IOUSBHostDevice > hub driver > port > device).
    return [node('IOUSBHostDevice', 'Dell dock', [node('AppleUSB20Hub', children=[node('AppleUSB20HubPort', children=[device])])],
                 idVendor=0xbda, idProduct=0x5487, locationID=0x110000)]


def without_descendant_properties(tree):
    """Emulate `ioreg -a -r -c IOUSBHostDevice` without -l."""
    def strip(n):
        if n['IOObjectClass'] != 'IOUSBHostDevice':
            for key in [k for k in n if k not in ('IOObjectClass', 'IORegistryEntryName', 'IORegistryEntryChildren')]:
                del n[key]
        for child in n['IORegistryEntryChildren']:
            strip(child)
    tree = copy.deepcopy(tree)
    for root in tree:
        strip(root)
    return tree


def el2_device():
    """q1n1 after ExitBootServices: two ACM functions, data interfaces 1 and 3."""
    ports = {1: '/dev/cu.usbmodemA16_Q1N1_EL21', 3: '/dev/cu.usbmodemA16_Q1N1_EL23'}
    interfaces = []
    for number in range(4):
        if number % 2:
            tty = node('IOSerialBSDClient', IOCalloutDevice=ports[number])
            interfaces.append(interface(number, (10, 0, 0), [node('AppleUSBACMData', children=[node('IOModemSerialStreamSync', children=[tty])])]))
        else:
            interfaces.append(interface(number, (2, 2, 0), [node('AppleUSBACMControl')]))
    device = node('IOUSBHostDevice', 'q1n1 A16 EL2 serial', [node('AppleUSBCDCCompositeDevice')] + interfaces,
                  idVendor=0x1209, idProduct=0x316d, locationID=0x112000, **{'USB Serial Number': 'A16-Q1N1-EL2'})
    return [device], ports


def run_watcher(tree, *args, existing=(CALLOUT,)):
    calls = []

    def check_output(command):
        calls.append(command)
        return plistlib.dumps(tree)
    with mock.patch.object(watcher.subprocess, 'check_output', check_output), \
            mock.patch.object(watcher.os.path, 'exists', lambda path: path in existing):
        result = watcher.inventory(*args)
    assert calls == [['ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice']], calls
    return result


checks = 0
matched, seen = run_watcher(a16())
assert matched == [CALLOUT] and len(seen) == 1 and CALLOUT in seen[0]; checks += 1
matched, seen = run_watcher(without_descendant_properties(a16()))
assert matched == [] and 'callouts []' in seen[0]; checks += 1  # The v3-run blind spot, now reported.
matched, seen = run_watcher(a16(serial='OTHER'))
assert matched == [] and "'OTHER'" in seen[0]; checks += 1
matched, seen = run_watcher(a16(serial=None))
assert matched == [] and 'serial None' in seen[0]; checks += 1
assert run_watcher([node('IOUSBHostDevice', idVendor=0x5ac, idProduct=0x12a8)]) == ([], []); checks += 1
tree, ports = el2_device()
assert run_watcher(tree, 'A16-Q1N1-EL2', 1, existing=tuple(ports.values()))[0] == [ports[1]]; checks += 1
assert run_watcher(tree, 'A16-Q1N1-EL2', 3, existing=tuple(ports.values()))[0] == [ports[3]]; checks += 1
matched, seen = run_watcher(tree, existing=tuple(ports.values()))  # default UEFI identity
assert matched == [] and "'A16-Q1N1-EL2'" in seen[0] and ports[1] in seen[0]; checks += 1


def states(tree):
    devices = [n for root in tree for n in capture.walk(root)
               if n.get('IOObjectClass') == 'IOUSBHostDevice' and (n.get('idVendor'), n.get('idProduct')) == (0x1209, 0x316d)]
    return [capture.summarize(d) for d in devices]


summary = states(a16())[0]
assert summary['state'] == 'tty-present' and summary['callouts'] == [CALLOUT] and summary['identity_exact']
assert [i['bInterfaceClass'] for i in summary['interfaces']] == [2, 10]; checks += 1
assert states(a16(data_driver='ecm'))[0]['state'] == 'acm-control-only'; checks += 1
ecm = states(a16(data_driver='ecm'))[0]['interfaces'][1]['drivers'][0]
assert ecm['name'] == 'AppleUserECMData' and ecm['IOUserServerName'].endswith('ECM.data'); checks += 1
assert states(a16(data_driver='none'))[0]['state'] == 'acm-control-only'; checks += 1
assert states(a16(configured=False))[0]['state'] == 'present-unconfigured'; checks += 1
assert not states(a16(serial='x'))[0]['identity_exact']; checks += 1
el2 = capture.summarize(el2_device()[0][0])
assert el2['identity_exact'] and el2['serial'] == 'A16-Q1N1-EL2' and len(el2['callouts']) == 2 and el2['state'] == 'tty-present'; checks += 1


def flaky(failures, tree):
    """ioreg that races a registry change ("can't obtain child") a few times first."""
    calls = []

    def check_output(args, **_):
        calls.append(args)
        if len(calls) <= failures:
            raise watcher.subprocess.CalledProcessError(1, args)
        return plistlib.dumps(tree)
    return calls, check_output


calls, check_output = flaky(2, a16())
with mock.patch.object(watcher.subprocess, 'check_output', check_output), \
        mock.patch.object(watcher.os.path, 'exists', lambda path: path == CALLOUT), \
        mock.patch.object(watcher.time, 'sleep', lambda _: None):
    assert watcher.inventory()[0] == [CALLOUT] and len(calls) == 3; checks += 1
calls, check_output = flaky(5, a16())
with mock.patch.object(watcher.subprocess, 'check_output', check_output), \
        mock.patch.object(watcher.time, 'sleep', lambda _: None):
    try:
        watcher.inventory()
        raise AssertionError('persistent ioreg failure was hidden')
    except watcher.subprocess.CalledProcessError:
        assert len(calls) == 5; checks += 1
calls, check_output = flaky(4, a16())
with mock.patch.object(capture.subprocess, 'check_output', check_output), \
        mock.patch.object(capture.time, 'sleep', lambda _: None):
    raw, roots = capture.ioreg('-l', '-r', '-c', 'IOUSBHostDevice')
    assert len(calls) == 5 and plistlib.loads(raw) == roots; checks += 1
print(f'PASS: {checks} Mac discovery fixture checks (synthetic IORegistry only)')
