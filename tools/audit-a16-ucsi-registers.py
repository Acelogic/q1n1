#!/usr/bin/env python3
"""Check captured BIOS312/Windows register-read ABI without executing either binary."""
import hashlib
import json
from pathlib import Path
import struct
import capstone
import pefile

firmware = Path('build/private/a16-typec-drivers/PmicGlinkDxe.efi')
windows = Path('build/private/a16-windows-usbc/qcpmicglink8480.sys')
dsdt = Path('build/private/a16-windows-usbc/API-DSDT.bin')
expected = {
    firmware: 'd9eb488f75923db967a05f081313864157e74f6b72d9b369be238921c7a3b87e',
    windows: '2c7161093c74a830569cf6897a141cb5a0dffa2e4a2f155d2b123413e4f9a95e',
    dsdt: 'ea4dbd66cad51e2eff837e4f97d70a8216d2f66c10792300a4816add360e98d6',
}
for path, sha in expected.items():
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, path
f = pefile.PE(str(firmware))
w = pefile.PE(str(windows))
b = f.get_memory_mapped_image().ljust(0x6000, b'\0')
wb = w.get_memory_mapped_image()
assert f.FILE_HEADER.Machine == w.FILE_HEADER.Machine == 0xaa64
assert f.OPTIONAL_HEADER.ImageBase == 0 and f.OPTIONAL_HEADER.SizeOfImage == 0x6000
callbacks = (0x19e4,0x1a70,0x1b1c,0x1ba8,0x1c4c,0x1c58,0x1d18,0x1d70,
             0x1f34,0x1f5c,0x2118,0x2140,0x21b4,0x220c,0x2210,0x2270,
             0x2334,0x23f8,0x245c,0x2460,0x24b8,0x24bc,0x2548,0x25f8,
             0x263c,0x2710,0x2804,0x28e8,0x2910)
assert struct.unpack_from('<30Q', b, 0x40b0) == (0x1000c,) + callbacks
assert not any(e.type and 0x1000 <= e.rva < 0x4000
               for block in f.DIRECTORY_ENTRY_BASERELOC for e in block.entries)
d = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
def dis(data, lo, hi):
    return {hex(i.address): f'{i.mnemonic} {i.op_str}' for i in d.disasm(data[lo:hi], lo)}
read = dis(b, 0x263c, 0x2710)
for address, instruction in {
    '0x265c':'cbz x1, #0x26f8', '0x2664':'cmp w2, #0x400',
    '0x266c':'mov w8, #0x8011', '0x2670':'mov w9, #1',
    '0x2674':'mov w10, #0x90', '0x2678':'mov x20, x1',
    '0x267c':'stp w0, w21, [sp, #0x18]', '0x2690':'mov w1, #0x18',
    '0x2694':'bl #0x3020', '0x26a0':'ldr w8, [x8, #0x950]',
    '0x26a4':'cbnz w8, #0x26dc', '0x26bc':'add x1, x1, #0x550',
    '0x26c0':'mov x0, x20', '0x26c4':'mov x2, x21', '0x26c8':'bl #0x1858',
}.items():
    assert read[address] == instruction, (address, read[address])
receive = dis(b, 0x2b8c, 0x2bdc)
write = dis(b, 0x2710, 0x2800)
for address, instruction in {
    '0x2730':'mov w20, w2', '0x2734':'mov x19, x1', '0x2738':'mov w21, w0',
    '0x2758':'cbz x19, #0x27e0', '0x275c':'cmp w20, #0x400',
    '0x2764':'mov w8, #0x8011', '0x276c':'mov w10, #0x91',
    '0x2770':'stp w21, w20, [sp, #0x10]', '0x2788':'add x22, x8, #0x18',
    '0x2798':'mov w2, w20', '0x27a0':'mov x1, x19',
    '0x27ac':'mov w1, #0x418', '0x27b0':'bl #0x3020',
    '0x27b8':'ldr w8, [x8, #0x950]', '0x27c0':'ccmp x0, #0, #0, eq',
}.items():
    assert write[address] == instruction, (address, write[address])
assert receive['0x2b98'] == 'cmp w8, #0x90'
assert receive['0x2ba0'] == 'cmp x4, #0x410'
assert receive['0x2bc4'] == 'mov w2, #0x400'
assert receive['0x2bcc'] == 'ldr w8, [x19, #0x40c]'
assert receive['0x2bd4'] == 'str w8, [x20, #0x400]'
win_read = dis(wb, 0x98f0, 0x99e4)
assert struct.unpack_from('<2I', wb, 0x99e8) == (0x8011, 1)
assert win_read['0x9924'] == 'mov x8, #0x90'
assert win_read['0x9934'] == 'mov x3, #0x18'
assert win_read['0x9940'] == 'stp w9, w8, [sp, #0x10]'
assert struct.unpack_from('<I', wb, 0xd9e0)[0] == 0x80332050
aml = dsdt.read_bytes()
assert aml[:4] == b'DSDT' and sum(aml) % 256 == 0
assert struct.unpack_from('<I', aml, 4)[0] == len(aml)
report = {
    'sources': {str(p): sha for p, sha in expected.items()},
    'firmware_protocol_rva': '0x40b0', 'revision': '0x1000c',
    'callback_rvas': [hex(v) for v in callbacks],
    'read_abi': 'EFI_STATUS read(uint32_t register, void *caller_buffer, uint32_t bytes)',
    'request': {'owner':'0x8011','type':1,'opcode':'0x90','size':24,
                'reserved_offset':12,'register_offset':16,'bytes_offset':20},
    'response': {'size':1040,'data_offset':12,'data_bytes':1024,'remote_error_offset':1036},
    'probe_registers': {'VERSION':'0x20100','CCI':'0x20104'}, 'probe_bytes_per_read':4,
    'register_source': 'Exact DSDT _SB.PMGK GSB offsets; corroborated by live Windows GIO reads',
    'windows': {'read_rva':'0x98f0','dispatch_rva':'0xd110','handler_rva':'0xce88',
                'ioctl':'0x80332050','input_bytes':8,'output_capacity':1024},
    'firmware_read': read, 'firmware_receive': receive, 'windows_read': win_read,
    'firmware_write': write,
    'write_abi': 'EFI_STATUS write(uint32_t register, const void *caller_buffer, uint32_t bytes)',
    'write_request': {'owner':'0x8011','type':1,'opcode':'0x91','size':1048,
                      'register_offset':16,'bytes_offset':20,'payload_offset':24},
    'limitation': 'Firmware checks cached remote error, but generic RX completion can be triggered by unrelated messages; not a response freshness guarantee.',
}
output = Path('build/private/a16-typec-drivers/ucsi-register-evidence.json')
output.write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps({'passed':True,'firmware_callbacks':len(callbacks),'output':str(output)}))
