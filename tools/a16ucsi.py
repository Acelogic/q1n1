#!/usr/bin/env python3
"""Drive UCSI on the A16 interactively through the q1n1 boot window.

The boot window keeps firmware alive, so PmicGlinkDxe's UCSI transport is
reachable while a USB CDC link to the Mac is up: one command per line, no
rebuild and no reboot per attempt. Get there with:

    python3 tools/a16ctl.py boot hold

then, for example:

    python3 tools/a16ucsi.py status
    python3 tools/a16ucsi.py swap 1 --to ufp --accept --handshake
    python3 tools/a16ucsi.py ccom 1 --mode rd
    python3 tools/a16ucsi.py raw 0x10012
    python3 tools/a16ucsi.py leave windows

Field decoding follows the UCSI 1.2/2.0 layout, but every reply also prints raw
bytes: the bit positions used by this platform's PPM are what we are testing.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
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

BOOT_SERIAL = 'A16-Q1N1-BOOT'

# UCSI commands (UCSI 2.0 table 4-1).
PPM_RESET = 0x01
CONNECTOR_RESET = 0x03
ACK_CC_CI = 0x04
SET_NOTIFICATION_ENABLE = 0x05
GET_CAPABILITY = 0x06
GET_CONNECTOR_CAPABILITY = 0x07
SET_CCOM = 0x08
SET_UOR = 0x09
SET_PDR = 0x0B
GET_ALTERNATE_MODES = 0x0C
GET_CABLE_PROPERTY = 0x11
GET_CONNECTOR_STATUS = 0x12
GET_ERROR_STATUS = 0x13

CCI_BITS = {25: 'not-supported', 26: 'cancel-complete', 27: 'reset-complete',
            28: 'busy', 29: 'ack-complete', 30: 'ERROR', 31: 'complete'}
CCOM = {'rp': 1, 'dfp': 1, 'rd': 2, 'ufp': 2, 'drp': 4}
UOR = {'dfp': 1 << 7, 'ufp': 1 << 8}
PARTNER_TYPE = {0: 'none', 1: 'DFP (partner is host -> A16 is device)',
                2: 'UFP (partner is device -> A16 is host)', 3: 'cable-no-Ra',
                4: 'cable-with-Ra', 5: 'debug', 6: 'audio'}
POWER_MODE = {0: 'none', 1: 'default-USB', 2: 'BC', 3: 'PD', 4: 'Type-C 1.5A', 5: 'Type-C 3.0A'}
ERROR_FLAGS = ['unrecognized-command', 'non-existent-connector', 'invalid-parameters',
               'incompatible-partner', 'CC-communication-error', 'command-failed-due-to-dead-battery',
               'contract-negotiation-failed', 'overcurrent', 'undefined', 'port-partner-rejected-swap',
               'hard-reset', 'PPM-policy-conflict', 'swap-rejected', 'reverse-current-protection']


def control(opcode, data=0, length=0):
    return (opcode & 0xFF) | ((length & 0xFF) << 8) | ((data & 0xFFFFFFFFFFFF) << 16)


def describe_cci(cci):
    names = [name for bit, name in CCI_BITS.items() if cci & (1 << bit)]
    connector = (cci >> 1) & 0x7F
    return f'{cci:#010x} [{", ".join(names) or "idle"}]' + (f' change-on-connector {connector}' if connector else '')


class Window:
    """Line protocol of q1n1.efi --boot."""

    def __init__(self, path, verbose=False, timeout=20.0):
        self.transport = q1n1proxy.Transport(path)
        self.verbose = verbose
        self.timeout = timeout
        self.buffer = b''

    def close(self):
        self.transport.close()

    def lines(self, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
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

    def send(self, text, expect, timeout=None):
        self.transport.write(text.encode() + b'\r')
        for line in self.lines(timeout):
            if any(line.startswith(prefix) for prefix in expect):
                return line
            if line.startswith('ERR'):
                raise SystemExit(f'A16 rejected {text!r}: {line}')
        raise SystemExit(f'no reply to {text!r}')

    def hold(self):
        return self.send('hold', ('OK hold',))

    def init(self):
        line = self.send('ucsi init', ('UCSI init',))
        fields = dict(part.split('=', 1) for part in line.split() if '=' in part)
        if int(fields.get('ready', '0'), 16) != 1:
            raise SystemExit(f'UCSI transport unavailable: {line}')
        return line

    def command(self, value, timeout=30.0):
        line = self.send(f'ucsi cmd {value:#x}', ('UCSI cci=',), timeout)
        fields = dict(part.split('=', 1) for part in line.split() if '=' in part)
        data = bytes.fromhex(fields.get('data', ''))
        return {'cci': int(fields['cci'], 16), 'length': int(fields['len'], 16),
                'stable': int(fields['stable'], 16), 'status': int(fields['status'], 16),
                'data': data, 'line': line}

    def run(self, opcode, data=0, label=None, timeout=30.0):
        result = self.command(control(opcode, data), timeout)
        name = label or f'cmd {opcode:#04x}'
        print(f'{name}: CCI {describe_cci(result["cci"])}'
              + (f' status {result["status"]:#x}' if result['status'] else '')
              + (f'\n    data {result["data"].hex()}' if result['data'] else '')
              + ('' if result['stable'] or not result['data'] else '   (samples disagreed)'))
        if result['cci'] & (1 << 30):
            print('    -> the PPM rejected this command')
        if result['cci'] & (1 << 25):
            print('    -> the PPM does not support this command')
        return result


def decode_connector_status(data):
    if len(data) < 4:
        return
    value = int.from_bytes(data[:4], 'little')
    print(f'    raw32 {value:#010x}')
    print(f'    change {value & 0xFFFF:#06x}  power-mode {POWER_MODE.get((value >> 16) & 7, (value >> 16) & 7)}'
          f'  connected {(value >> 19) & 1}  power-direction {"source" if (value >> 20) & 1 else "sink"}')
    flags = (value >> 21) & 3
    print(f'    partner-flags {flags:#b} (bit0 USB, bit1 alt-mode)')
    print(f'    partner-type[23:25] {PARTNER_TYPE.get((value >> 23) & 7, (value >> 23) & 7)}')
    print(f'    partner-type[29:31] {PARTNER_TYPE.get((value >> 29) & 7, (value >> 29) & 7)}   (legacy reading)')


def decode_connector_capability(data):
    if len(data) < 4:
        return
    caps = int.from_bytes(data[:4], 'little')
    print(f'    raw32 {caps:#010x}  DFP-capable {caps & 1}  UFP-capable {(caps >> 1) & 1}'
          f'  DRP {(caps >> 2) & 1}')
    print(f'    swap-to-DFP {(caps >> 10) & 1}  swap-to-UFP {(caps >> 11) & 1}'
          f'  swap-to-src {(caps >> 8) & 1}  swap-to-snk {(caps >> 9) & 1}')


def decode_error(data):
    if len(data) < 2:
        return
    flags = int.from_bytes(data[:2], 'little')
    names = [name for bit, name in enumerate(ERROR_FLAGS) if flags & (1 << bit)]
    print(f'    error flags {flags:#06x} [{", ".join(names) or "none reported"}]')


def connect(args, notify=0x1):
    """The PPM only raises the Command Completed indicator once notifications
    are enabled, so every session starts with SET_NOTIFICATION_ENABLE, exactly
    as the physically tested connector-query app did."""
    found, _ = watcher.inventory(BOOT_SERIAL, 1)
    if not found:
        raise SystemExit('no A16-Q1N1-BOOT port; run: python3 tools/a16ctl.py boot hold')
    window = Window(found[0], verbose=args.verbose)
    window.hold()
    window.init()
    if notify is not None:
        window.run(SET_NOTIFICATION_ENABLE, notify, label=f'SET_NOTIFICATION_ENABLE {notify:#x}')
    return window


def command_status(args):
    window = connect(args)
    try:
        capability = window.run(GET_CAPABILITY, label='GET_CAPABILITY')
        connectors = capability['data'][4] if len(capability['data']) > 4 else 0
        print(f'  connectors reported: {connectors}')
        for connector in range(1, max(connectors, args.connectors) + 1):
            print(f'-- connector {connector}')
            result = window.run(GET_CONNECTOR_CAPABILITY, connector, label='  GET_CONNECTOR_CAPABILITY')
            decode_connector_capability(result['data'])
            result = window.run(GET_CONNECTOR_STATUS, connector, label='  GET_CONNECTOR_STATUS')
            decode_connector_status(result['data'])
    finally:
        window.close()
    return 0


def command_swap(args):
    window = connect(args)
    try:
        if args.handshake:
            print('-- PPM handshake')
            window.run(PPM_RESET, label='PPM_RESET', timeout=60)
            window.run(SET_NOTIFICATION_ENABLE, args.notify, label=f'SET_NOTIFICATION_ENABLE {args.notify:#x}')
            window.run(GET_CONNECTOR_CAPABILITY, args.connector, label='GET_CONNECTOR_CAPABILITY')
        before = window.run(GET_CONNECTOR_STATUS, args.connector, label='status before')
        decode_connector_status(before['data'])
        data = args.connector | UOR.get(args.to, 0) | (1 << 9 if args.accept else 0)
        print(f'-- SET_UOR connector {args.connector} to {args.to}'
              + (' (accepting incoming swaps)' if args.accept else ''))
        result = window.run(SET_UOR, data, label='SET_UOR')
        if result['cci'] & ((1 << 30) | (1 << 25)):
            error = window.run(GET_ERROR_STATUS, args.connector, label='GET_ERROR_STATUS')
            decode_error(error['data'])
        after = window.run(GET_CONNECTOR_STATUS, args.connector, label='status after')
        decode_connector_status(after['data'])
        changed = before['data'][:4] != after['data'][:4]
        print(f'connector status {"CHANGED" if changed else "unchanged"}')
    finally:
        window.close()
    return 0


def command_ccom(args):
    window = connect(args)
    try:
        before = window.run(GET_CONNECTOR_STATUS, args.connector, label='status before')
        decode_connector_status(before['data'])
        data = args.connector | (CCOM[args.mode] << 7)
        print(f'-- SET_CCOM connector {args.connector} to {args.mode} (CC operation mode)')
        result = window.run(SET_CCOM, data, label='SET_CCOM')
        if result['cci'] & ((1 << 30) | (1 << 25)):
            error = window.run(GET_ERROR_STATUS, args.connector, label='GET_ERROR_STATUS')
            decode_error(error['data'])
        time.sleep(args.settle)
        after = window.run(GET_CONNECTOR_STATUS, args.connector, label='status after')
        decode_connector_status(after['data'])
    finally:
        window.close()
    return 0


def command_raw(args):
    window = connect(args)
    try:
        value = int(args.control, 0)
        result = window.run(value & 0xFF, value >> 16, label=f'raw {value:#x}')
        if args.decode == 'status':
            decode_connector_status(result['data'])
        elif args.decode == 'capability':
            decode_connector_capability(result['data'])
        elif args.decode == 'error':
            decode_error(result['data'])
    finally:
        window.close()
    return 0


def command_leave(args):
    found, _ = watcher.inventory(BOOT_SERIAL, 1)
    if not found:
        raise SystemExit('no A16-Q1N1-BOOT port')
    window = Window(found[0], verbose=args.verbose)
    try:
        print(window.send(args.target, ('OK ',)))
    finally:
        window.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verbose', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status', help='capabilities and status of every connector')
    status.add_argument('--connectors', type=int, default=2, help='probe at least this many')
    swap = sub.add_parser('swap', help='SET_UOR: ask for a data-role swap')
    swap.add_argument('connector', type=int)
    swap.add_argument('--to', choices=sorted(UOR), default='ufp')
    swap.add_argument('--accept', action='store_true', help='also accept incoming swaps')
    swap.add_argument('--handshake', action='store_true', help='PPM_RESET and SET_NOTIFICATION_ENABLE first')
    swap.add_argument('--notify', type=lambda v: int(v, 0), default=0xFFFF)
    ccom = sub.add_parser('ccom', help='SET_CCOM: force the CC operation mode (Rp/Rd/DRP)')
    ccom.add_argument('connector', type=int)
    ccom.add_argument('--mode', choices=sorted(CCOM), default='rd')
    ccom.add_argument('--settle', type=float, default=2.0)
    raw = sub.add_parser('raw', help='send one allowed UCSI control value')
    raw.add_argument('control')
    raw.add_argument('--decode', choices=['status', 'capability', 'error'])
    leave = sub.add_parser('leave', help='end the window: proxy | windows | shell')
    leave.add_argument('target', choices=['proxy', 'proxy once', 'windows', 'shell'])
    args = parser.parse_args()
    return {'status': command_status, 'swap': command_swap, 'ccom': command_ccom,
            'raw': command_raw, 'leave': command_leave}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
