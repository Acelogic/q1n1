#!/usr/bin/env python3
"""Audit the captured BIOS312 power driver; no firmware code is executed."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid
import capstone
import pefile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--driver', type=Path, default=Path('build/private/a16-typec-drivers/UsbPwrCtrlDxe.efi'))
parser.add_argument('--output', type=Path, default=Path('build/private/a16-typec-drivers/typec-evidence.json'))
parser.add_argument('--mapped-output', type=Path, help='Optional native-test data fixture; never executable')
args = parser.parse_args()
raw = args.driver.read_bytes()
sha = hashlib.sha256(raw).hexdigest()
if sha != '7e51cca7441ba299e222c53a3a9e0a6ce1613387b0f016b24113f0d8df334fcb':
    raise SystemExit('Not the captured BIOS312 UsbPwrCtrlDxe; refusing fixed-RVA analysis')
pe = pefile.PE(data=raw)
assert pe.FILE_HEADER.Machine == 0xaa64 and pe.OPTIONAL_HEADER.ImageBase == 0
assert pe.OPTIONAL_HEADER.SizeOfImage == 0x5000
mapped = pe.get_memory_mapped_image().ljust(0x5000, b'\0')
code = mapped[0x1000:0x3000]
fnv = 0xcbf29ce484222325
for v in code:
    fnv = ((fnv ^ v) * 0x100000001b3) & 0xffffffffffffffff
assert fnv == 0xa599916bef31a4c9
assert not any(e.type and 0x1000 <= e.rva < 0x3000
               for block in pe.DIRECTORY_ENTRY_BASERELOC for e in block.entries)
guid = str(uuid.UUID(bytes_le=mapped[0x3054:0x3064]))
assert guid == 'e07df17e-e79e-4150-9378-50623a14994a'
callbacks = (0x1bac,0x1be4,0x1be8,0x1d08,0x1df8,0x1e18,0x1e1c,
             0x1ef4,0x1f84,0x2004,0x2008,0x209c,0x20a4,0x20ac)
assert struct.unpack_from('<15Q', mapped, 0x30c0) == (0x10004,) + callbacks
engine = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
def disasm(start, end):
    return {hex(i.address): f'{i.mnemonic} {i.op_str}' for i in engine.disasm(mapped[start:end], start)}
getter = disasm(0x2398, 0x2788)
checks = {'0x23e8': 'mov w25, #1', '0x23ec': 'mov w26, #0xc8',
          '0x2404': 'ldr w8, [x8, #0x30]', '0x2408': 'cmp w8, #6',
          '0x248c': 'ldrb w8, [x8, #0x5fc]',
          '0x24e0': 'add x9, x9, #0x5f8', '0x24e4': 'add x8, x9, x22, lsl #4',
          '0x24e8': 'ldp w8, w10, [x8, #0xa0]', '0x24f0': 'tbnz w8, #0x18, #0x2538',
          '0x2540': 'tbnz w8, #0x19, #0x257c', '0x26c0': 'ldrb w9, [x9, #0x10]'}
assert all(getter.get(k) == v for k, v in checks.items())
report = dict(source=str(args.driver), sha256=sha, firmware='UX3607OA.312',
    code_fingerprint=dict(rva='0x1000', length='0x2000', fnv1a64=hex(fnv), relocations=0),
    protocol=dict(guid=guid, rva='0x30c0', revision='0x10004', callbacks=[hex(x) for x in callbacks]),
    cache=dict(ready_rva='0x35fc', count_rva='0x3138', config_pointer_rva='0x3640',
               port_ids='config pointer + 1, byte entries', maximum_id=5, config_stride='0xc8',
               status_backends=['record+0x30','record+0x34'], cached_rva='0x3698', cached_stride=16,
               connected_bit=24, source_power_bit=25, orientation_bytes_rva='0x3608'),
    limitations=['Static evidence only; runtime probe has not yet run on hardware.',
                 'Cache may be stale or refreshed during snapshot.',
                 'Port numbering has not been mapped to physical connectors.',
                 'No independently verified data-role swap operation; no setters are called.'],
    installation=disasm(0x1910,0x1970), hw_info=disasm(0x1bac,0x1be4),
    port_validation=disasm(0x223c,0x2290), typec_status=getter,
    unsupported_slots=disasm(0x209c,0x20ac))
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2) + '\n')
if args.mapped_output:
    args.mapped_output.parent.mkdir(parents=True, exist_ok=True)
    args.mapped_output.write_bytes(mapped)
print(json.dumps(dict(output=str(args.output), sha256=sha, protocol=guid, fingerprint=hex(fnv))))
