#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Which stage build the native hash helper binds (tools/nvme_hash.py bind_stage).

The helper runs from the unused tail of the running stage's slot, so it must
know the running stage exactly: its resident .text has to equal a local
build's ELF .text in every byte but the word the chainload client patches.
This checks that against this checkout's real make-uefi output (run `make uefi`
first) and against consistent copies of it that stand in for a second stage
pair built elsewhere (another q1n1 worktree): each pair binds to its own build
and nothing else, the binary a build chainloads must be that build's ELF, and
two different builds never bind at once. No hardware; the target's memory is
a model of the resident text.

    python3 tools/test-nvme-hash-stage.py
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import nvme_hash as nh                                        # noqa: E402
from nvme_m3 import M3_LAYOUT, Refused                        # noqa: E402

LOCAL = ROOT / 'build/uefi'
STAGE_BASE, STAGE_SIZE = 0xb0000000, 0x1000000               # the A16's two 8 MiB slots
SLOTS = {'': STAGE_BASE, '-alt': STAGE_BASE + STAGE_SIZE // 2}


def info_for(image_base, stage_base=STAGE_BASE):
    return dict(stage_base=stage_base, stage_size=STAGE_SIZE, image_base=image_base)


class Target:
    """Resident memory: a build's text as chainloaded, its configuration word
    patched the way the chainload client does, plus whatever gets written."""

    def __init__(self, build, suffix, word=b'\x5a' * 8):
        text = bytearray(nh.stage_image(build, suffix)['text'])
        text[nh.CONFIG_WORD] = word
        self.base, self.memory, self.reads = SLOTS[suffix], {}, 0
        self.text = text

    def read(self, address, size):
        self.reads += 1
        if address == self.base and size <= len(self.text):
            return bytes(self.text[:size])
        if address in self.memory:
            return self.memory[address][:size]
        return bytes(size)


def clone(into, text_flip=None, rodata_flip=None, bin_flip=None):
    """A copy of the local build changed consistently (ELF and binary alike),
    as a second build of different sources would be; `bin_flip` breaks only
    the binary."""
    for suffix in ('', '-alt'):
        elf = bytearray((LOCAL / f'stage/hardware{suffix}.elf').read_bytes())
        image = bytearray((LOCAL / f'q1n1-stage{suffix}.bin').read_bytes())
        sections = nh.elf_sections(bytes(elf))
        base = sections['.text'][3]
        for name, offset in (('.text', text_flip), ('.rodata', rodata_flip)):
            if offset is not None:
                section = sections[name]
                elf[section[4] + offset] ^= 0xff
                image[section[3] - base + offset] ^= 0xff
        if bin_flip is not None:
            image[bin_flip] ^= 0xff
        (into / 'stage').mkdir(parents=True, exist_ok=True)
        (into / f'stage/hardware{suffix}.elf').write_bytes(elf)
        (into / f'q1n1-stage{suffix}.bin').write_bytes(image)
    return into


@unittest.skipUnless((LOCAL / 'stage/hardware-alt.elf').exists(), 'run make uefi first')
class Binding(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix='stage-binding-'))
        self.addCleanup(shutil.rmtree, self.scratch)
        self.env = mock.patch.dict(os.environ, {nh.STAGE_BUILDS: ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.other = clone(self.scratch / 'other/build/uefi', text_flip=0x400)

    def test_local_build_binds_in_both_slots(self):
        for suffix, base in SLOTS.items():
            target = Target(LOCAL, suffix)
            bound = nh.bind_stage(target.read, info_for(base))
            self.assertEqual(bound['build'], LOCAL.resolve())
            self.assertEqual(bound['bin_path'].name, f'q1n1-stage{suffix}.bin')
            self.assertEqual(bound['slot'], STAGE_SIZE // 2)
            self.assertGreater(bound['allocated_end'], base + len(bound['text']))

    def test_another_pair_binds_only_to_its_own_build(self):
        target = Target(self.other, '-alt')
        with self.assertRaisesRegex(Refused, 'does not match any local stage build.*text differs'):
            nh.bind_stage(target.read, info_for(SLOTS['-alt']))
        bound = nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other])
        self.assertEqual(bound['build'], self.other.resolve())
        with mock.patch.dict(os.environ, {nh.STAGE_BUILDS: os.pathsep.join(['/nonexistent', str(self.other)])}):
            self.assertEqual(nh.bind_stage(target.read, info_for(SLOTS['-alt']))['build'], self.other.resolve())
        # Naming the other pair does not stop the local one from binding.
        local = Target(LOCAL, '-alt')
        self.assertEqual(nh.bind_stage(local.read, info_for(SLOTS['-alt']), [self.other])['build'], LOCAL.resolve())

    def test_every_text_byte_but_the_configuration_word_counts(self):
        for offset in (0, 23, 32, 0x400):
            target = Target(self.other, '-alt')
            target.text[offset] ^= 0x01
            with self.assertRaises(Refused, msg=offset):
                nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other])
        for word in (bytes(8), b'\xff' * 8):
            target = Target(self.other, '-alt', word)
            self.assertEqual(nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other])['build'],
                             self.other.resolve())

    def test_the_binary_must_be_its_elf(self):
        broken = clone(self.scratch / 'broken/build/uefi', text_flip=0x400, bin_flip=0x20000)
        target = Target(self.other, '-alt')
        with self.assertRaisesRegex(Refused, 'is not the image of'):
            nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other, broken])

    def test_two_different_builds_never_bind_at_once(self):
        # Same text, different read-only data: which one runs is unknowable.
        twin = clone(self.scratch / 'twin/build/uefi', text_flip=0x400, rodata_flip=0x10)
        target = Target(self.other, '-alt')
        with self.assertRaisesRegex(Refused, 'matches different stage builds'):
            nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other, twin])
        # The same build named twice is one candidate.
        again = nh.bind_stage(target.read, info_for(SLOTS['-alt']), [self.other, self.other / '.'])
        self.assertEqual(again['build'], self.other.resolve())

    def test_slot_and_link_address(self):
        target = Target(LOCAL, '')
        with self.assertRaisesRegex(Refused, 'no unused executable tail'):
            nh.bind_stage(target.read, info_for(STAGE_BASE + 0x1000))
        # The primary slot is bound with hardware.elf, never hardware-alt.elf.
        with self.assertRaises(Refused):
            nh.bind_stage(Target(LOCAL, '-alt').read, info_for(STAGE_BASE))
        # A reservation elsewhere (QEMU's) finds no build linked for it.
        with self.assertRaisesRegex(Refused, 'linked for 0xb0000000'):
            nh.bind_stage(target.read, info_for(0x4c000000, stage_base=0x4c000000))


class FakeNvme:
    """What NativeHash.__init__ touches: an idle M3 controller and the proxy."""

    class Queue:
        outstanding = 0

    def __init__(self, target):
        self.l, self.io, self.admin, self.w, self.log = M3_LAYOUT, self.Queue(), self.Queue(), 0x90000000, {}
        self.target = target
        info = info_for(target.base)
        test = self

        class Proxy:
            def bootinfo(self):
                return info

            def flush_code(self, address, size):
                pass

            def set_exc_guard(self, mode):
                pass

            def call(self, address, *args, timeout=None):
                test.called = address
                return 0

            def get_exc_count(self):
                return 0

        class Hw:
            p = Proxy()

            def read(self, address, size):
                return target.read(address, size)

            def write(self, address, data):
                target.memory[address] = bytes(data)

        self.hw = Hw()


@unittest.skipUnless((LOCAL / 'stage/hardware-alt.elf').exists(), 'run make uefi first')
class Helper(unittest.TestCase):
    def test_native_hash_places_its_helper_beside_the_bound_pair(self):
        nh.build()
        with tempfile.TemporaryDirectory(prefix='stage-binding-') as scratch, \
                mock.patch.dict(os.environ, {nh.STAGE_BUILDS: ''}):
            other = clone(Path(scratch) / 'other/build/uefi', text_flip=0x400)
            nvme = FakeNvme(Target(other, '-alt'))
            with self.assertRaises(Refused):
                nh.NativeHash(nvme)
            nh.NativeHash(nvme, stage_builds=[other])
            placement = nvme.log['native_hash_placement']
            code = SLOTS['-alt'] + STAGE_SIZE // 2 - 65536
            self.assertEqual((placement['code'], nvme.called), (hex(code), code))
            self.assertEqual(Path(placement['stage_build']), other.resolve())
            self.assertEqual(placement['stage_bin_sha256'], nh.stage_image(other, '-alt')['bin_sha256'])
            self.assertEqual(nvme.target.memory[code], (nh.BUILD / 'nvme-hash.bin').read_bytes())


if __name__ == '__main__':
    unittest.main()
