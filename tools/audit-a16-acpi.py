#!/usr/bin/env python3
"""Read the captured ACPI tables; report verified SPCR/DBG2 fields without MMIO."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

parser = argparse.ArgumentParser()
parser.add_argument('capture', type=Path)
parser.add_argument('--output', type=Path)
args = parser.parse_args()
acpi = args.capture / 'evidence/acpi'
report = {'capture': str(args.capture), 'hardware_execution': False, 'tables': {}, 'usb_debug': []}

def table(name, signature):
    path = acpi / name
    data = path.read_bytes()
    if len(data) < 36 or data[:4] != signature or struct.unpack_from('<I', data, 4)[0] != len(data):
        raise ValueError(f'Invalid ACPI table: {path}')
    if sum(data) & 255:
        raise ValueError(f'Invalid ACPI checksum: {path}')
    report['tables'][signature.decode()] = {'file': name, 'sha256': hashlib.sha256(data).hexdigest(),
                                           'length': len(data), 'checksum_valid': True}
    return data

spcr = table('012-SPCR.bin', b'SPCR')
if len(spcr) < 80:
    raise ValueError('Truncated SPCR')
report['uart'] = {'interface': hex(spcr[36]), 'base': hex(struct.unpack_from('<Q', spcr, 44)[0]),
                  'address_space': spcr[40], 'bit_width': spcr[41], 'bit_offset': spcr[42],
                  'gas_access_size_raw': spcr[43], 'gsi': struct.unpack_from('<I', spcr, 54)[0],
                  'baud_code': spcr[58], 'baud': {3:9600,4:19200,6:57600,7:115200}.get(spcr[58])}
dbg = table('009-DBG2.bin', b'DBG2')
offset, count = struct.unpack_from('<II', dbg, 36)
for _ in range(count):
    if offset + 22 > len(dbg):
        raise ValueError('DBG2 entry header outside table')
    revision, length, regs, nlen, noff, olen, ooff, kind, subtype, reserved, boff, soff = struct.unpack_from('<BHBHHHHHHHHH', dbg, offset)
    if length < 22 or offset + length > len(dbg) or noff + nlen > length or boff + regs*12 > length or soff + regs*4 > length:
        raise ValueError('DBG2 entry data outside table')
    entry = {'namespace': dbg[offset+noff:offset+noff+nlen].rstrip(b'\0').decode(),
             'type': hex(kind), 'subtype': hex(subtype), 'registers': []}
    for i in range(regs):
        entry['registers'].append({'base':hex(struct.unpack_from('<Q',dbg,offset+boff+i*12+4)[0]),
                                   'size':hex(struct.unpack_from('<I',dbg,offset+soff+i*4)[0])})
    report['usb_debug' if kind == 0x8003 else 'uart_debug'] = (
        report['usb_debug'] + [entry] if kind == 0x8003 else entry)
    offset += length
result = json.dumps(report, indent=2) + '\n'
if args.output:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result)
print(result)
