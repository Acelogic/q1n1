#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build/upload a RAM-only NVMe hash helper; preserve Python queue cursors.

The helper can only read namespace 1. Caller owns an idle M3-layout controller.
Failures and the physical qualification plan are in docs/A16-NVME-WRITES.md.

The helper runs from the unused tail of the running stage's slot, so the
running stage is first bound to the local build it came from (bind_stage):
its resident .text must equal that build's ELF .text byte for byte, except the
one word the chainload client patches. This checkout's build/uefi is always a
candidate; another stage pair (for example one built in a second q1n1
worktree) is bound through its own build directory, named by the caller or in
$Q1N1_STAGE_BUILDS.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time

from nvme_m3 import M3_LAYOUT, Misbehaved, Refused

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/nvme-hash'
LLVM = Path('/opt/homebrew/opt/llvm/bin')
MAGIC = 0x31485341484D564E
HEADER = struct.Struct('<20Q')
WORK_BYTES = 296


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    sources = [ROOT / 'platform/uefi' / n for n in ['nvme-hash.c', 'sha256.c', 'sha256.h']]
    BUILD.mkdir(parents=True, exist_ok=True)
    flags = ['--target=aarch64-unknown-none-elf', '-O3', '-ffreestanding', '-fno-builtin',
             '-fno-stack-protector', '-fpie', '-fvisibility=hidden', '-mgeneral-regs-only',
             '-Wall', '-Wextra', '-Werror', '-I' + str(ROOT / 'platform/uefi')]
    objects = []
    for p in sources[:2]:
        obj = BUILD / (p.stem + '.o')
        subprocess.run([str(LLVM / 'clang'), *flags, '-c', str(p), '-o', str(obj)], check=True)
        objects.append(obj)
    script = BUILD / 'link.ld'
    script.write_text('ENTRY(nvme_hash_run)\nSECTIONS { . = 0; .text : { KEEP(*(.text.entry)) *(.text*) } '
                      '.rodata : { *(.rodata*) } .data : { *(.data*) } .bss : { *(.bss*) } '
                      '/DISCARD/ : { *(.comment) *(.eh_frame*) } }\n')
    elf, binary = BUILD / 'nvme-hash.elf', BUILD / 'nvme-hash.bin'
    linker = shutil.which('ld.lld')
    if not linker:
        raise RuntimeError('ld.lld is required, as for the q1n1 stage build')
    subprocess.run([linker, '-T', str(script), '-o', str(elf), *map(str, objects)], check=True)
    assert not subprocess.check_output([str(LLVM / 'llvm-nm'), '-u', str(elf)]).strip()
    symbols = subprocess.check_output([str(LLVM / 'llvm-nm'), str(elf)], text=True)
    entry, = [line.split() for line in symbols.splitlines() if line.split()[-1] == 'nvme_hash_run']
    assert int(entry[0], 16) == 0 and entry[1].lower() == 't'
    subprocess.run([str(LLVM / 'llvm-objcopy'), '-O', 'binary', str(elf), str(binary)], check=True)
    assert binary.stat().st_size < 65536
    record = dict(binary_sha256=sha(binary), bytes=binary.stat().st_size,
                  sources={str(p.relative_to(ROOT)): sha(p) for p in sources}, flags=flags)
    (BUILD / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


STAGE_BUILDS = 'Q1N1_STAGE_BUILDS'   # more make-uefi output directories, os.pathsep separated
SHF_ALLOC, SHT_NOBITS = 2, 8
CONFIG_WORD = slice(24, 32)          # text bytes the chainload client patches after upload


def candidate_builds(extra=()):
    """Candidate stage builds, each a make-uefi output directory holding
    q1n1-stage[-alt].bin and stage/hardware[-alt].elf: this checkout's first,
    then the caller's, then $Q1N1_STAGE_BUILDS, each once."""
    named = [ROOT / 'build/uefi', *extra, *(p for p in os.environ.get(STAGE_BUILDS, '').split(os.pathsep) if p)]
    found = []
    for path in named:
        path = Path(path).resolve()
        if path not in found:
            found.append(path)
    return found


def elf_sections(elf):
    if elf[:6] != b'\x7fELF\x02\x01':
        raise Refused('stage ELF format')
    shoff = struct.unpack_from('<Q', elf, 40)[0]
    entsize, count, names_index = struct.unpack_from('<3H', elf, 58)
    sections = [struct.unpack_from('<IIQQQQIIQQ', elf, shoff + i * entsize) for i in range(count)]
    names = sections[names_index]
    strings = elf[names[4]:names[4] + names[5]]
    return {strings[s[0]:].split(b'\0', 1)[0].decode(): s for s in sections}


def stage_image(build, suffix):
    """One build's ELF for a slot, tied to the stage binary that build
    chainloads: the binary must be exactly the ELF's loaded sections laid out
    flat from .text, which is how make uefi's llvm-objcopy produces it."""
    elf_path, bin_path = build / f'stage/hardware{suffix}.elf', build / f'q1n1-stage{suffix}.bin'
    elf, image = elf_path.read_bytes(), bin_path.read_bytes()
    sections = elf_sections(elf)
    text = sections.get('.text')
    if text is None:
        raise Refused(f'{elf_path}: no .text')
    loaded = [s for s in sections.values() if s[2] & SHF_ALLOC and s[1] != SHT_NOBITS]
    base = text[3]
    if min(s[3] for s in loaded) != base:
        raise Refused(f'{elf_path}: .text is not the first loaded section')
    flat = bytearray(max(s[3] + s[5] for s in loaded) - base)
    for s in loaded:
        flat[s[3] - base:s[3] - base + s[5]] = elf[s[4]:s[4] + s[5]]
    if image != bytes(flat):
        raise Refused(f'{bin_path} is not the image of {elf_path}')
    return dict(build=build, elf_path=elf_path, bin_path=bin_path, elf_sha256=hashlib.sha256(elf).hexdigest(),
                bin_sha256=hashlib.sha256(image).hexdigest(), text=elf[text[4]:text[4] + text[5]],
                text_address=text[3], allocated_end=max(s[3] + s[5] for s in sections.values() if s[2] & SHF_ALLOC))


def bind_stage(read, info, extra=()):
    """The build the running stage came from, or Refused.

    `read(address, size)` reads target memory; `info` is q1n1's bootinfo. The
    slot picks each build's ELF (hardware.elf at stage_base, hardware-alt.elf
    in the upper half). A build binds only when that ELF is linked at the
    running image and its .text equals the resident text in every byte but
    CONFIG_WORD; a build that is absent or linked elsewhere is passed over.
    Two builds that both match must be the same image, or nothing binds."""
    slot = info['stage_size'] // 2
    if info['image_base'] not in (info['stage_base'], info['stage_base'] + slot):
        raise Refused('no unused executable tail in the current q1n1 stage slot')
    suffix = '' if info['image_base'] == info['stage_base'] else '-alt'
    tried, matched, resident = [], [], {}
    for build in candidate_builds(extra):
        try:
            image = stage_image(build, suffix)
        except FileNotFoundError:
            tried.append(f'{build}: no hardware{suffix} build')
            continue
        if image['text_address'] != info['image_base']:
            tried.append(f'{build}: linked for {image["text_address"]:#x}')
            continue
        size = len(image['text'])
        if size not in resident:
            resident[size] = read(info['image_base'], size)
        live = resident[size]
        expected = bytearray(image['text'])
        expected[CONFIG_WORD] = live[CONFIG_WORD]  # Named host-patched stage configuration word.
        if live != expected:
            tried.append(f'{build}: text differs')
            continue
        matched.append(dict(image, slot=slot, resident_text_sha256=hashlib.sha256(live).hexdigest()))
    if not matched:
        raise Refused('running stage text does not match any local stage build (' + '; '.join(tried) + ')')
    if len({m['bin_sha256'] for m in matched}) > 1 or len({m['allocated_end'] for m in matched}) > 1:
        raise Refused('running stage text matches different stage builds: ' + ', '.join(str(m['build']) for m in matched))
    return matched[0]


class NativeHash:
    def __init__(self, nvme, stage_builds=()):
        if nvme.l is not M3_LAYOUT or nvme.io.outstanding or nvme.admin.outstanding:
            raise Refused('native hash requires idle M3-layout queues')
        self.nvme = nvme
        record = json.loads((BUILD / 'build.json').read_text())
        for name, wanted in record['sources'].items():
            if sha(ROOT / name) != wanted:
                raise Refused('native hash source changed after build')
        binary = (BUILD / 'nvme-hash.bin').read_bytes()
        if hashlib.sha256(binary).hexdigest() != record['binary_sha256']:
            raise Refused('native hash binary changed after build')
        info = nvme.hw.p.bootinfo()
        # image_size describes the complete reservation, not the linked image.
        # Bind a local build's allocated extent to the live immutable text.
        stage = bind_stage(nvme.hw.read, info, stage_builds)
        slot, allocated_end = stage['slot'], stage['allocated_end']
        # Firmware maps high Conventional RAM as data/XN. The reserved stage
        # slot is executable, and its unused final page is outside this image.
        self.code, self.state = info['image_base'] + slot - 65536, nvme.w + 0x60000
        if allocated_end > self.code:
            raise Refused('helper code would overlap the linked stage/BSS/stack')
        leaf = bytes.fromhex('000080d2c0035fd6')  # mov x0, #0; ret
        nvme.hw.write(self.code, leaf)
        nvme.hw.p.flush_code(self.code, len(leaf))
        nvme.hw.p.set_exc_guard(3)  # RETURN is safe for this leaf admission.
        try:
            result = nvme.hw.p.call(self.code, timeout=3)
            faults = nvme.hw.p.get_exc_count()
            if result != 0 or faults:
                raise Refused('reserved stage tail is not executable')
        finally:
            nvme.hw.p.set_exc_guard(0)
        nvme.hw.write(self.code, binary)
        nvme.hw.p.flush_code(self.code, len(binary))
        if nvme.hw.read(self.code, len(binary)) != binary:
            raise Refused('native hash upload differs')
        nvme.log['native_hash_helper'] = record
        nvme.log['native_hash_placement'] = dict(code=hex(self.code), state=hex(self.state), executable_leaf_passed=True,
                                               stage_elf_sha256=stage['elf_sha256'],
                                               stage_text_sha256=stage['resident_text_sha256'],
                                               stage_allocated_end=hex(allocated_end),
                                               stage_build=str(stage['build']), stage_bin=str(stage['bin_path']),
                                               stage_bin_sha256=stage['bin_sha256'])

    def digest(self, lba, count, progress=None, overlays=()):
        nv = self.nvme
        if lba < 0 or count <= 0 or lba + count > nv.nsze or nv.io.outstanding:
            raise Refused('native hash range/queue invalid')
        q = nv.io
        done = 0
        state = bytes(136)  # SHA state plus digest, following 20 u64 fields.
        if len(overlays) > 256 or sum(len(data) for _, data in overlays) > 1 << 20:
            raise Refused('restore overlay exceeds the hash helper RAM reservation')
        table = bytearray()
        payload = bytearray()
        for absolute, data in overlays:
            if not data or absolute < 0 or absolute + len(data) > nv.nsze * nv.lba_bytes:
                raise Refused('restore overlay is outside namespace 1')
            table.extend(struct.pack('<3Q', absolute, nv.w + 0x70000 + len(payload), len(data)))
            payload.extend(data)
        if table:
            nv.hw.write(nv.w + 0x61000, table)
            nv.hw.write(nv.w + 0x70000, payload)
        per = (16 << 20) // nv.lba_bytes
        t0 = time.monotonic()
        commands = 0
        while done < count:
            n = min(per, count - done)
            fields = [MAGIC, nv.bar, nv.w, lba + done, n, nv.lba_bytes, nv.max_transfer, nv.nsze,
                      q.tail, q.head, q.phase, q.next_cid, nv.stride, q.entries, 0, 0, 0, commands,
                      nv.w + 0x61000 if overlays else 0, len(overlays)]
            nv.hw.write(self.state, HEADER.pack(*fields) + state)
            result = nv.hw.p.call(self.code, self.state, int(done == 0), int(done + n == count), timeout=30)
            output = nv.hw.read(self.state, WORK_BYTES)
            after = HEADER.unpack_from(output)
            q.tail, q.head, q.phase, q.next_cid = after[8:12]
            commands = after[17]
            if result != 0 or after[14] != 0 or after[15] != n:
                raise Misbehaved('native NVMe hash failed', result=result, status=after[14],
                                 done_blocks=after[15], completion=hex(after[16]))
            state = output[HEADER.size:]
            done += n
            if progress:
                progress(done * nv.lba_bytes, count * nv.lba_bytes, time.monotonic() - t0)
        result = state[-32:].hex()
        nv.log.setdefault('native_hashes', []).append(dict(lba=lba, blocks=count, sha256=result,
                                                         commands=commands, seconds=round(time.monotonic() - t0, 3)))
        return result


if __name__ == '__main__':
    print(json.dumps(build(), indent=2))
