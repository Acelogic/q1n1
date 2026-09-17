#!/usr/bin/env python3
"""Validate BIOS312 PMIC GLINK UCSI read ABI; never execute captured code."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid
import pefile
import capstone

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--driver',type=Path,default=Path('build/private/a16-typec-drivers/PmicGlinkDxe.efi'))
p.add_argument('--output',type=Path,default=Path('build/private/a16-typec-drivers/ucsi-evidence.json'))
p.add_argument('--mapped-output',type=Path)
a=p.parse_args()
raw=a.driver.read_bytes()
sha=hashlib.sha256(raw).hexdigest()
assert sha=='d9eb488f75923db967a05f081313864157e74f6b72d9b369be238921c7a3b87e'
pe=pefile.PE(data=raw)
assert pe.FILE_HEADER.Machine==0xaa64 and pe.OPTIONAL_HEADER.ImageBase==0 and pe.OPTIONAL_HEADER.SizeOfImage==0x6000
b=pe.get_memory_mapped_image().ljust(0x6000,b'\0')
h=0xcbf29ce484222325
for v in b[0x1000:0x4000]: h=((h^v)*0x100000001b3)&0xffffffffffffffff
assert h==0x391a635ac9656c89
assert not any(e.type and 0x1000<=e.rva<0x4000 for block in pe.DIRECTORY_ENTRY_BASERELOC for e in block.entries)
guid=str(uuid.UUID(bytes_le=b[0x4088:0x4098]))
assert guid=='7429eb07-f2e3-4a6b-aa96-c909cd956ef7'
callbacks=(0x19e4,0x1a70,0x1b1c,0x1ba8,0x1c4c,0x1c58,0x1d18,0x1d70,0x1f34,0x1f5c,0x2118,0x2140,
           0x21b4,0x220c,0x2210,0x2270,0x2334,0x23f8,0x245c,0x2460,0x24b8,0x24bc,0x2548,0x25f8)
assert struct.unpack_from('<25Q',b,0x40b0)==(0x1000c,)+callbacks
d=capstone.Cs(capstone.CS_ARCH_ARM64,capstone.CS_MODE_ARM)
def dis(lo,hi): return {hex(i.address):f'{i.mnemonic} {i.op_str}' for i in d.disasm(b[lo:hi],lo)}
read=dis(0x19e4,0x1a70)
assert read['0x1a1c']=='mov w8, #0x800b' and read['0x1a24']=='mov w10, #0x11'
assert read['0x1a40']=='ldr x0, [x19]' and read['0x1a48']=='add x1, x1, #0x408'
assert dis(0x37f8,0x3800)['0x37f8']=='mov w2, #0x30'
usbc=dis(0x1b1c,0x1ba8)
assert usbc['0x1b54']=='mov w8, #0x800c' and usbc['0x1b5c']=='mov w10, #0x14'
assert usbc['0x1b64']=='mov w1, #0x94'
assert usbc['0x1b74']=='ldr x0, [x19]' and usbc['0x1b7c']=='add x1, x1, #0x438'
assert usbc['0x1b80']=='mov w2, #0x84' and usbc['0x1b84']=='bl #0x1858'
ucsi_receive=dis(0x2bdc,0x2c14)
usbc_receive=dis(0x2b5c,0x2b8c)
assert ucsi_receive['0x2bf8']=='cmp x4, #0x40' and ucsi_receive['0x2c10']=='b #0x2d8c'
assert usbc_receive['0x2b70']=='cmp x4, #0x94' and usbc_receive['0x2b88']=='b #0x2d8c'
report=dict(source=str(a.driver),sha256=sha,image_size='0x6000',guid=guid,protocol_rva='0x40b0',revision='0x1000c',
    code=dict(rva='0x1000',length='0x3000',fnv1a64=hex(h),relocations=0),callbacks=[hex(x) for x in callbacks],
    read_abi='EFI_STATUS read(UINT8 **caller_owned_buffer, UINT8 size); implementation copies exactly 48 bytes',
    glink_message=dict(owner='0x800b',opcode='0x11',size=64),cache_rva='0x4408',link_ready_rva='0x43e1',boot_services_pointer_rva='0x4238',
    read_disassembly=read,copy_tail=dis(0x37f8,0x3800),transport=dis(0x3020,0x321c),
    usbc_read_abi='EFI_STATUS read(UINT8 **caller_owned_buffer, UINT8 size); implementation copies exactly 132 bytes',
    usbc_glink_message=dict(owner='0x800c',opcode='0x14',size=148),usbc_cache_rva='0x4438',
    usbc_read_disassembly=usbc,ucsi_receive=ucsi_receive,usbc_receive=usbc_receive,
    receive_tail=dis(0x2d8c,0x2dd0),
    receive_limitation='These handlers require exact packet length, copy payload, and signal generic receive completion. They do not validate trailing remote return code; unrelated or wrong-length messages can also signal completion.',
    limitation='Static ABI evidence only; does not prove live UCSI availability or support for SET_UOR.')
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
if a.mapped_output:
    a.mapped_output.parent.mkdir(parents=True,exist_ok=True);a.mapped_output.write_bytes(b)
print(json.dumps(dict(output=str(a.output),read_rva='0x19e4',read_bytes=48,fnv1a64=hex(h))))
