#!/usr/bin/env python3
"""Record binding and configuration evidence from captured UX3607OA.312 PE files.

Run with the local firmware-tools venv (pefile and capstone installed).
This reads extracted files only; it neither modifies firmware nor accesses hardware.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid

import capstone
import pefile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--drivers', type=Path, default=Path('build/private/a16-usb-drivers'))
parser.add_argument('--output', type=Path, default=Path('build/private/a16-usb-drivers/binding-evidence.json'))
args = parser.parse_args()
specs = [
    dict(name='UsbfnDwc3Dxe', sha256='424e7089d82fa00dda6cd91a2546d9ff26c47feb8cc2ba0bd94da67cbad63881',
         binding=0x9160, supported=0x2898, end=0x2948, guid=0x90a4, mode=4),
    dict(name='XhciPciEmulation', sha256='33ada521f151add5684e83a26c0b5e13b4cc2e007436fedf328add1313385a6e',
         binding=0x30b8, supported=0x1868, end=0x18e8, guid=0x3060, mode=1),
]
engine = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
report = []
for spec in specs:
    path = args.drivers / (spec['name'] + '.efi')
    binary = path.read_bytes()
    if hashlib.sha256(binary).hexdigest() != spec['sha256']:
        raise SystemExit(f'{path}: expected the captured BIOS312 binary; refusing fixed-RVA analysis')
    pe = pefile.PE(data=binary)
    if pe.FILE_HEADER.Machine != 0xaa64 or pe.OPTIONAL_HEADER.ImageBase != 0:
        raise SystemExit(f'{path}: unexpected PE architecture/base')
    binding = struct.unpack('<QQQIIQQ', pe.get_data(spec['binding'], 48))
    if binding[0] != spec['supported'] or binding[3] != 0x30:
        raise SystemExit(f'{path}: driver-binding structure mismatch')
    guid = str(uuid.UUID(bytes_le=pe.get_data(spec['guid'], 16)))
    if guid != 'e722b03f-b250-42ce-8ebd-5bd51812d037':
        raise SystemExit(f'{path}: configuration GUID mismatch')
    instructions = list(engine.disasm(pe.get_data(spec['supported'], spec['end'] - spec['supported']), spec['supported']))
    text = [f'{i.mnemonic} {i.op_str}' for i in instructions]
    checks = ['ldr w9, [x8, #0x88]', 'cmp w9, #5',
              'ldr w8, [x8, #0x8c]', f"cmp w8, #{spec['mode']}"]
    if any(check not in text for check in checks):
        raise SystemExit(f'{path}: expected data checks were not found')
    report.append(dict(name=spec['name'], source=str(path), sha256=spec['sha256'],
                       binding_rva=hex(spec['binding']), binding_version=hex(binding[3]),
                       supported_rva=hex(binding[0]), start_rva=hex(binding[1]), stop_rva=hex(binding[2]),
                       configuration_protocol=guid, selector_offset='0x88', selector_max=5,
                       mode_offset='0x8c', accepted_mode=spec['mode'],
                       supported_disassembly=[dict(rva=hex(i.address), instruction=f'{i.mnemonic} {i.op_str}') for i in instructions]))
    if spec['name'] == 'UsbfnDwc3Dxe':
        start, size = 0x1000, 0x7400
        fnv = 0xcbf29ce484222325
        for byte in pe.get_data(start, size):
            fnv = ((fnv ^ byte) * 0x100000001b3) & 0xffffffffffffffff
        relocations = [entry.rva for block in pe.DIRECTORY_ENTRY_BASERELOC for entry in block.entries
                       if entry.type and start <= entry.rva < start + size]
        if fnv != 0x30f3db4034710f10 or relocations:
            raise SystemExit('USB Function code fingerprint or relocation assumptions changed')
        report[-1]['runtime_code_fingerprint'] = dict(rva=hex(start), size=hex(size),
            algorithm='FNV-1a-64 (version mismatch check, not authentication)',
            expected=hex(fnv), relocation_count=0)
        report[-1]['usb_function_abi'] = dict(revision='0x10003', constructor_rva='0x2948',
            protocol_offset_in_allocation=8,
            callback_rvas=[hex(x) for x in (0x2e10,0x2e70,0x31e0,0x3218,0x3600,0x365c,0x367c,
                0x36dc,0x36ec,0x38d4,0x3978,0x399c,0x39f0,0x3a04,0x3a08,0x3a2c,0x3a74,0x3b58,0x3bc4)],
            note='Callback order recorded from constructor disassembly; compared with live protocol by serial app.')
config_path = args.drivers / 'UsbConfigDxe.efi'
config_hash = '5b440b75bcda81a0c21cd6a82aa1ede1b6b060bdf23b1969575cb512e2992a4c'
config_binary = config_path.read_bytes()
if hashlib.sha256(config_binary).hexdigest() != config_hash:
    raise SystemExit(f'{config_path}: expected the captured BIOS312 binary; refusing fixed-RVA analysis')
config_pe = pefile.PE(data=config_binary)
if config_pe.FILE_HEADER.Machine != 0xaa64 or config_pe.OPTIONAL_HEADER.ImageBase != 0:
    raise SystemExit(f'{config_path}: unexpected PE architecture/base')
config_guid = str(uuid.UUID(bytes_le=config_pe.get_data(0xfe40, 16)))
if config_guid != report[0]['configuration_protocol']:
    raise SystemExit(f'{config_path}: configuration GUID mismatch')
template = struct.unpack('<32Q', config_pe.get_data(0xc458, 0x100))
if template[0] != 0x20001 or template[0x50 // 8] != 0x3ac8 or template[0x58 // 8] != 0x3ad4:
    raise SystemExit(f'{config_path}: configuration template mismatch')

def config_disassembly(start, end):
    return [dict(rva=hex(i.address), instruction=f'{i.mnemonic} {i.op_str}')
            for i in engine.disasm(config_pe.get_data(start, end - start), start)]

global_install = config_disassembly(0x173c, 0x1794)
expected_install = {
    '0x173c': 'adrp x23, #0xe000', '0x1740': 'add x23, x23, #0x570',
    '0x1744': 'add x24, x23, #0x20', '0x1748': 'adrp x1, #0xc000',
    '0x174c': 'add x1, x1, #0x458', '0x1750': 'mov x0, x24',
    '0x1754': 'mov w2, #0x100', '0x1758': 'bl #0x2248',
    '0x175c': 'mov x8, #5', '0x1764': 'movk x8, #1, lsl #48',
    '0x176c': 'adrp x1, #0xf000', '0x1770': 'add x1, x1, #0xe40',
    '0x1774': 'mov x2, x24', '0x177c': 'str x8, [x23, #0xa8]',
    '0x1784': 'ldr x8, [x9, #0x148]', '0x178c': 'blr x8',
}
observed_install = {i['rva']: i['instruction'] for i in global_install}
if any(observed_install.get(rva) != instruction for rva, instruction in expected_install.items()):
    raise SystemExit(f'{config_path}: global interface installation mismatch')
configuration = dict(
    source=str(config_path), sha256=config_hash, configuration_protocol=config_guid,
    template_rva='0xc458', revision=hex(template[0]), interface_size='0x100',
    template_words=[dict(offset=hex(i * 8), value=hex(v)) for i, v in enumerate(template)],
    global_interface=dict(rva='0xe590', selector=5, mode='0x10000',
                          explanation='Initializer copies the template, overwrites interface +0x88/+0x8c with 5/0x10000, and installs the configuration GUID.',
                          installation_disassembly=global_install,
                          copy_helper_disassembly=config_disassembly(0x2248, 0x2264)),
    role_selection_candidate=dict(
        protocol_offset='0x50', wrapper_rva='0x3ac8', implementation_rva='0x4ca8',
        inferred_arguments=['This (ignored by wrapper)', '32-bit controller selector', '32-bit requested mode'],
        invoked_on_hardware=False,
        interpretation='Wrapper passes w1/w2 to a stateful path that can disconnect drivers, install a controller configuration, and call ConnectController. Cable policy and lower-level effects are not fully traced; this is not a qualified role-switch API.',
        wrapper_disassembly=config_disassembly(0x3ac8, 0x3ae0),
        implementation_disassembly=config_disassembly(0x4ca8, 0x4f44),
        stop_path_disassembly=config_disassembly(0x48c4, 0x49c8),
        stop_argument_disassembly=config_disassembly(0xabac, 0xabb4),
        disconnect_helper_disassembly=config_disassembly(0xaa08, 0xaa20),
        argument_check_disassembly=config_disassembly(0xad00, 0xad14),
        installation_argument_disassembly=config_disassembly(0xab34, 0xab4c)))
# The role-change app checks this relocation-free region in the loaded image
# before accepting any private layout or invoking the firmware callback.
code_rva, code_size = 0x1000, 0xb000
if config_pe.OPTIONAL_HEADER.SizeOfImage != 0x13000:
    raise SystemExit('Unexpected UsbConfigDxe image size')
if any(entry.type and code_rva <= entry.rva < code_rva + code_size
       for block in config_pe.DIRECTORY_ENTRY_BASERELOC for entry in block.entries):
    raise SystemExit('Runtime fingerprint region contains relocations')
fnv = 0xcbf29ce484222325
for byte in config_pe.get_data(code_rva, code_size):
    fnv = ((fnv ^ byte) * 0x100000001b3) & 0xffffffffffffffff
if fnv != 0x5a151951eae7fefb:
    raise SystemExit('Role-change app fingerprint mismatch')
configuration['runtime_code_fingerprint'] = dict(
    algorithm='FNV-1a-64 (version mismatch check, not authentication)',
    rva=hex(code_rva), size=hex(code_size), expected=hex(fnv), relocation_count=0)
configuration['usb0_layout'] = dict(
    controller_array_rva='0xdf00', stride='0x148', configuration_offset='0x28',
    phy_pointer_offset='0x18', phy_base_getter_offset='0x50', phy_base_getter_rva='0x9d50',
    phy_base_offset='0x88', expected_base='0xa600000',
    inference='Configuration is controller+0x28 from the installation path; PHY getter reads the 64-bit base without MMIO.',
    base_query_disassembly=config_disassembly(0x38b0, 0x392c),
    core_to_selector_disassembly=config_disassembly(0x4804, 0x482c),
    phy_lookup_disassembly=config_disassembly(0xa8f8, 0xa910),
    phy_base_disassembly=config_disassembly(0x9d50, 0x9d74))
result = dict(firmware='Captured UX3607OA.312 update package', runtime_bytes_verified=False,
              interpretation='Supported also needs a successful OpenProtocol(BY_DRIVER); matching fields alone does not establish binding or transport.',
              drivers=report, configuration=configuration)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(dict(output=str(args.output), configuration_protocol=report[0]['configuration_protocol'],
                      checked_drivers=[r['name'] for r in report], accepted_modes=[r['accepted_mode'] for r in report],
                      global_configuration=[5, '0x10000'], configuration_revision=hex(template[0]))))
