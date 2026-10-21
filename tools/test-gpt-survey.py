#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""GPT survey: C model (ASan/UBSan), cross-language record decode, GPT checks,
read-only proxy fetch, and a source + object audit that the EFI side has no
write path.

  python3 tools/test-gpt-survey.py

When Snapintosh's storage backups are present (SNAP_STORAGE_BACKUP, default
../Snapintosh/build/a16-storage-backup-20260921), the real A16 GPT captured
from Windows before partitioning is decoded, and a GPT reconstructed from the
final Windows layout record is run through the C survey and decoded.
"""
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
import zlib

import gpt_survey as gs

ROOT = Path(__file__).resolve().parents[1]
UEFI = ROOT / 'platform/uefi'
BACKUP = Path(os.environ.get('SNAP_STORAGE_BACKUP',
                             ROOT.parent / 'Snapintosh/build/a16-storage-backup-20260921'))
DISK_BYTES = 1024209543168
TMP = Path(tempfile.mkdtemp(prefix='gpt-survey-'))
HARNESS = TMP / 'test-gpt-survey'
LLVM = Path('/opt/homebrew/opt/llvm/bin')


def build_harness():
    subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror', f'-I{UEFI}',
                    str(ROOT / 'tools/test-gpt-survey.c'), str(UEFI / 'gpt-survey.c'), '-o', str(HARNESS)],
                   check=True)


# ---------------------------------------------------------- GPT building ---

def entry(type_guid, part_guid, first, last, attrs=0, name=''):
    return (uuid.UUID(type_guid).bytes_le + uuid.UUID(part_guid).bytes_le +
            struct.pack('<QQQ', first, last, attrs) + name.encode('utf-16le').ljust(72, b'\0'))


def header(mine, other, first_usable, last_usable, disk_guid, entries_lba, count, esize, array_crc):
    h = bytearray(struct.pack('<8sIIIIQQQQ16sQIII', b'EFI PART', 0x10000, 92, 0, 0, mine, other,
                              first_usable, last_usable, uuid.UUID(disk_guid).bytes_le,
                              entries_lba, count, esize, array_crc))
    struct.pack_into('<I', h, 16, zlib.crc32(h))
    return bytes(h)


def disk_windows(entries, blocks=DISK_BYTES // 512, bs=512, count=128, esize=128,
                 disk_guid='1c98f6ba-8bab-4cae-8c35-bf82870d8cf2'):
    """First and last MiB of a disk holding this GPT (both copies)."""
    array = b''.join(entries).ljust(count * esize, b'\0')
    array_blocks = -(-len(array) // bs)
    last = blocks - 1
    first_usable, last_usable = 2 + array_blocks, last - array_blocks - 1
    crc = zlib.crc32(array)
    first_win, last_win = bytearray(1 << 20), bytearray(1 << 20)
    first_win[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF, 1, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF])
    first_win[510:512] = b'\x55\xaa'
    first_win[bs:bs + 92] = header(1, last, first_usable, last_usable, disk_guid, 2, count, esize, crc)
    first_win[2 * bs:2 * bs + len(array)] = array
    base = blocks * bs - len(last_win)
    at = (last - array_blocks) * bs - base
    last_win[at:at + len(array)] = array
    last_win[last * bs - base:last * bs - base + 92] = header(last, 1, first_usable, last_usable, disk_guid,
                                                             last - array_blocks, count, esize, crc)
    return bytes(first_win), bytes(last_win)


def survey_fixture(first, last, disk_bytes=DISK_BYTES, bs=512):
    f, l, out = TMP / 'first.bin', TMP / 'last.bin', TMP / 'record.bin'
    f.write_bytes(first); l.write_bytes(last)
    subprocess.run([str(HARNESS), '--fixture', str(f), str(l), str(disk_bytes), str(bs), str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    return out.read_bytes()


def windows_record(name):
    path = BACKUP / name
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None


def gpt_from_windows(record):
    """Rebuild a GPT from a Windows layout record (names are not in the record)."""
    ents = []
    for p in sorted(record['Partitions'], key=lambda p: p['Offset']):
        first = p['Offset'] // 512
        ents.append(entry(p['GptType'].strip('{}'), p['Guid'].strip('{}'), first,
                          first + p['Size'] // 512 - 1, name='rebuilt'))
    return disk_windows(ents)


class ModelTest(unittest.TestCase):
    def test_c_model(self):
        subprocess.run([str(HARNESS)], check=True, stdout=subprocess.DEVNULL)


class RecordTest(unittest.TestCase):
    def test_layout_matches_c(self):
        first, last = disk_windows([entry('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', str(uuid.uuid4()), 2048, 4095)])
        raw = survey_fixture(first, last)
        self.assertEqual(len(raw), gs.RECORD_SIZE)
        header, results = gs.decode_record(raw)
        self.assertEqual((header['recorded'], header['reads'], header['phase']), (1, 5, 4))
        disk, decoded, text, info = results[0]
        self.assertEqual(decoded['errors'], [])
        self.assertIn('NVMe(0x1,00-25-38-F2-61-C3-C2-91)', text)
        self.assertEqual(info['nvme_eui_bytes'], '002538F261C3C291')

    def test_rejects_other_layouts(self):
        first, last = disk_windows([entry('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', str(uuid.uuid4()), 2048, 4095)])
        raw = bytearray(survey_fixture(first, last))
        for offset, value in [(0, 1), (8, 2), (16, 12), (24, 99), (40, 8), (8 * 18, 9)]:
            bad = bytearray(raw)
            struct.pack_into('<Q', bad, offset, value)
            with self.subTest(offset=offset), self.assertRaises(gs.SurveyError):
                gs.parse_record(bytes(bad))
        with self.assertRaises(gs.SurveyError):
            gs.parse_record(bytes(raw[:-1]))


class DecodeTest(unittest.TestCase):
    PARTS = [entry('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', '939a0a66-c61e-42b7-b975-5be3c2b80280', 2048, 4095),
             entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', '419baed0-542c-4292-a670-caaabe34e2fa', 4096, 1000000)]

    def decode(self, first, last):
        return gs.raw_windows(first, last, DISK_BYTES, 512)

    def test_clean(self):
        d = self.decode(*disk_windows(self.PARTS))
        self.assertEqual(d['errors'], [])
        self.assertEqual([p['first_lba'] for p in d['partitions']], [2048, 4096])
        tail = d['unallocated'][-1]
        self.assertEqual((tail['first_lba'], tail['last_lba']), (1000001, DISK_BYTES // 512 - 34))

    def corrupt(self, where, offset, value):
        first, last = map(bytearray, disk_windows(self.PARTS))
        (first if where == 'first' else last)[offset] ^= value
        return self.decode(bytes(first), bytes(last))['errors']

    def test_detects_corruption(self):
        backup_header = (1 << 20) - 512
        cases = {'primary header': ('first', 512 + 40, 1), 'primary array': ('first', 1024 + 40, 1),
                 'backup header': ('last', backup_header + 40, 1), 'backup array': ('last', backup_header - 16384 + 40, 1),
                 'mbr': ('first', 510, 0xff)}
        for name, case in cases.items():
            with self.subTest(name=name):
                self.assertTrue(self.corrupt(*case), name)

    def test_detects_bad_layouts(self):
        bad = {
            'overlap': self.PARTS + [entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', str(uuid.uuid4()), 999999, 1000100)],
            'beyond usable': [entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', str(uuid.uuid4()), 4096, DISK_BYTES // 512)],
            'reversed': [entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', str(uuid.uuid4()), 9000, 8000)],
            'duplicate guid': [entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', '419baed0-542c-4292-a670-caaabe34e2fa', 4096, 5000),
                               entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', '419baed0-542c-4292-a670-caaabe34e2fa', 6000, 7000)],
        }
        for name, parts in bad.items():
            with self.subTest(name=name):
                self.assertTrue(self.decode(*disk_windows(parts))['errors'])

    def test_primary_backup_disagree(self):
        first, _ = disk_windows(self.PARTS)
        _, last = disk_windows(self.PARTS[:1])
        self.assertIn('primary/backup differ in array_crc', ' '.join(self.decode(first, last)['errors']))

    def test_4k_disk(self):
        first, last = disk_windows(self.PARTS, blocks=250051158, bs=4096)
        d = gs.raw_windows(first, last, 250051158 * 4096, 4096)
        self.assertEqual(d['errors'], [])
        raw = survey_fixture(first, last, 250051158 * 4096, 4096)
        self.assertEqual(gs.decode_record(raw)[1][0][1]['partitions'], d['partitions'])

    def test_unallocated_alignment(self):
        u = gs.unallocated([dict(first_lba=2048, last_lba=4095)], 34, 10000, 512)
        self.assertEqual([(x['first_lba'], x['last_lba']) for x in u], [(34, 2047), (4096, 10000)])
        self.assertEqual(u[0]['aligned_bytes'], 0)
        self.assertEqual((u[1]['aligned_offset'], u[1]['aligned_bytes']), (2097152, 2097152))


@unittest.skipUnless((BACKUP / 'gpt-first-1MiB.bin').exists(), 'A16 storage backups not present')
class A16CaptureTest(unittest.TestCase):
    """The real disk: Windows' raw capture, and Windows' final layout record."""

    def test_pre_partitioning_capture_matches_windows(self):
        first = (BACKUP / 'gpt-first-1MiB.bin').read_bytes()
        last = (BACKUP / 'gpt-last-1MiB.bin').read_bytes()
        d = gs.raw_windows(first, last, DISK_BYTES, 512)
        self.assertEqual(d['errors'], [])
        self.assertEqual(d['disk_guid'], '1c98f6ba-8bab-4cae-8c35-bf82870d8cf2')
        self.assertEqual(len(d['partitions']), 16)
        self.assertEqual(gs.compare_windows(d, windows_record('before.json')), [])
        # Same bytes through the C survey: firmware-path decode equals raw decode.
        _, results = gs.decode_record(survey_fixture(first, last))
        self.assertEqual(results[0][1]['partitions'], d['partitions'])

    def test_current_layout_free_extent(self):
        record = windows_record('staging-final.json')
        d = gs.decode_record(survey_fixture(*gpt_from_windows(record)))[1][0][1]
        self.assertEqual(d['errors'], [])
        self.assertEqual(gs.compare_windows(d, record), [])
        big = max(d['unallocated'], key=lambda u: u['bytes'])
        self.assertEqual((big['first_lba'], big['last_lba'], big['offset'], big['end'], big['bytes']),
                         (1170661376, 1996189695, 599378624512, 1022049124352, 422670499840))
        self.assertEqual(big['aligned_bytes'], 422670499840)


class PinnedDiskTest(unittest.TestCase):
    """The A16 firmware exposes other whole fixed disks besides the NVMe: the
    pins and the Windows comparison apply to the pinned disk only."""

    class Args:
        expect_bytes = None
        expect_disk_guid = None
        expect_eui = 'eui.002538F261C3C291'
        windows = None

    def results(self):
        nvme = gs.decode_record(survey_fixture(*disk_windows(DecodeTest.PARTS)))[1][0]
        other = gs.decode_record(survey_fixture(*disk_windows(
            [entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', str(uuid.uuid4()), 2048, 4095)],
            blocks=65536, disk_guid=str(uuid.uuid4())), disk_bytes=65536 * 512))[1][0]
        d, decoded, text, info = other
        return [(d, decoded, text, {}), nvme]          # the other disk has no NVMe node

    def run_eval(self, results, **kw):
        import io
        args = self.Args()
        for k, v in kw.items():
            setattr(args, k, v)
        return gs.evaluate(results, args, out=io.StringIO())

    def test_other_disk_does_not_fail_the_pinned_survey(self):
        failed, summary = self.run_eval(self.results())
        self.assertFalse(failed)
        self.assertEqual([d['pinned'] for d in summary], [False, True])

    def test_pinned_disk_problems_still_fail(self):
        failed, _ = self.run_eval(self.results(), expect_disk_guid=str(uuid.uuid4()))
        self.assertTrue(failed)
        failed, _ = self.run_eval(self.results(), expect_eui='eui.0000000000000001')
        self.assertTrue(failed)

    def test_without_pins_every_disk_is_checked(self):
        failed, _ = self.run_eval(self.results(), expect_eui=None, expect_bytes=DISK_BYTES)
        self.assertTrue(failed)                         # the 32 MiB disk fails --expect-bytes


class FetchTest(unittest.TestCase):
    """The proxy path: bootinfo and bounded memory reads, nothing else."""

    class StrictProxy:
        def __init__(self, record, kind=2):
            self.record = record
            self.base = 0x80000000
            self.map = b''.join(struct.pack('<IIQQQQ8x', k, 0, b, 0, pages, 0)
                                for k, b, pages in [(1, 0x1000000, 16), (kind, self.base, 256)])
            self.reads = []

        def bootinfo(self):
            return dict(memory_map=0x2000, memory_map_size=len(self.map), memory_map_stride=48,
                        gpt_survey=self.base, gpt_survey_size=len(self.record))

        def readmem(self, address, size):
            self.reads.append((address, size))
            return self.map if address == 0x2000 else self.record

        def __getattr__(self, name):
            raise AssertionError(f'gpt_survey reached proxy.{name}; only bootinfo/readmem are allowed')

    def test_fetch_reads_only(self):
        first, last = disk_windows(DecodeTest.PARTS)
        raw = survey_fixture(first, last)
        proxy = self.StrictProxy(raw)
        self.assertEqual(gs.fetch(proxy), raw)
        self.assertEqual(proxy.reads, [(0x2000, 96), (0x80000000, len(raw))])

    def test_refusals(self):
        raw = survey_fixture(*disk_windows(DecodeTest.PARTS))
        with self.assertRaises(gs.SurveyError):
            gs.fetch(self.StrictProxy(raw, kind=7))           # record in Conventional memory
        old = self.StrictProxy(raw)
        old.bootinfo = lambda: dict(memory_map=0x2000)          # payload without the survey
        with self.assertRaises(gs.SurveyError):
            gs.fetch(old)
        with self.assertRaises(gs.SurveyError):
            gs.ReadOnlyProxy(self.StrictProxy(raw)).readmem(0, gs.READ_LIMIT + 1)


class AuditTest(unittest.TestCase):
    """The EFI side cannot write: checked in source and in the compiled object."""
    SOURCE = (UEFI / 'gpt-survey.c').read_text()
    GUIDS = {  # protocol GUIDs whose presence would mean a write-capable path
        'Disk I/O': 'ce345171-ba0b-11d2-8e4f-00a0c969723b',
        'Disk I/O 2': '151c8eae-7f2c-472c-9e54-9828194f6a88',
        'Block I/O 2': 'a77b2472-e282-4e9f-a245-c2c0e27bbcc1',
        'NVMe Pass Thru': '52c78312-8edc-4233-98f2-1a1aa5e388a5',
        'Erase Block': '95a9a93e-a86e-4926-aaef-9918e772d987',
        'Storage Security': 'c88b0b6d-0dfc-49a7-9cb4-49074b4c3f78',
        'SCSI Pass Thru Ext': '143b7632-b81b-4cb7-abd3-b625a5b9bffe',
        'ATA Pass Thru': '1d3de7f0-0807-424f-aa69-11a54e19a46f',
        'Simple File System': '964e5b22-6459-11d2-8e39-00a0c969723b',
        'Partition Info': '8cf2f62c-bc9b-4821-808d-ec9ec421a1a0',
    }
    ALLOWED = {'Block I/O': '964e5b21-6459-11d2-8e39-00a0c969723b',
               'Device Path': '09576e91-6d3f-11d2-8e39-00a0c969723b'}

    def test_source_surface(self):
        code = re.sub(r'/\*.*?\*/', '', self.SOURCE, flags=re.S)
        self.assertEqual(set(re.findall(r'\bbs->(\w+)', code)),
                         {'allocate_pages', 'free_pages', 'free_pool', 'handle_protocol', 'locate_handle_buffer'})
        self.assertEqual(set(re.findall(r'\bio->(\w+)', code)), {'media', 'read_blocks', 'revision'})
        for word in ('not_used', 'write', 'flush', 'connect', 'erase', 'pass_thru', 'set_variable',
                     'exit_boot', 'open_protocol', 'locate_protocol', 'install', 'start_image', 'asm'):
            self.assertNotIn(word, code.lower(), word)
        self.assertNotRegex(code, r'\(\s*efi_status\s*\(\s*\*', 'function-pointer cast')

    def test_header_offsets(self):
        header = (UEFI / 'gpt-survey.h').read_text()
        for field, offset in [('read_blocks', 24), ('write_not_used', 32), ('media', 8)]:
            self.assertIn(f'offsetof(struct q1n1_block_io, {field}) == {offset}', header)

    @unittest.skipUnless((LLVM / 'clang').exists() and (LLVM / 'llvm-objdump').exists(), 'Homebrew LLVM absent')
    def test_object(self):
        obj = TMP / 'gpt-survey.obj'
        subprocess.run([str(LLVM / 'clang'), '--target=aarch64-pc-windows-msvc', '-O2', '-ffreestanding',
                        '-fno-builtin', '-fno-stack-protector', '-fshort-wchar', '-mgeneral-regs-only',
                        '-mno-outline-atomics', '-Wall', '-Wextra', '-Werror', f'-I{UEFI}', '-c',
                        str(UEFI / 'gpt-survey.c'), '-o', str(obj)], check=True)
        data = obj.read_bytes()
        for name, g in self.GUIDS.items():
            self.assertNotIn(uuid.UUID(g).bytes_le, data, name)
        for name, g in self.ALLOWED.items():
            self.assertIn(uuid.UUID(g).bytes_le, data, name)
        undefined = subprocess.run([str(LLVM / 'llvm-nm'), '-u', str(obj)], check=True,
                                   capture_output=True, text=True).stdout.split()
        self.assertEqual(undefined, [], 'the survey object imports nothing')
        # Every indirect call goes through a slot this file is allowed to use:
        # Boot Services AllocatePages 0x28, FreePages 0x30, FreePool 0x48,
        # HandleProtocol 0x98, LocateHandleBuffer 0x138; Block I/O ReadBlocks
        # 0x18. Reset (0x10) and WriteBlocks (0x20) never appear. FlushBlocks
        # shares 0x28 with AllocatePages and is excluded by the source audit.
        text = subprocess.run([str(LLVM / 'llvm-objdump'), '-d', '--no-show-raw-insn', str(obj)],
                              check=True, capture_output=True, text=True).stdout.splitlines()
        calls = 0
        for i, line in enumerate(text):
            m = re.search(r'\bblr\s+(x\d+)', line)
            if not m:
                continue
            calls += 1
            reg = m.group(1)
            for back in range(i - 1, max(-1, i - 24), -1):
                w = re.search(rf'\s(\w+)\s+{reg}, (.*)$', text[back])
                if w:
                    self.assertEqual(w.group(1), 'ldr', text[back])
                    slot = re.fullmatch(r'\[x\d+, #0x([0-9a-f]+)\]', w.group(2).strip())
                    self.assertTrue(slot, text[back])
                    self.assertIn(int(slot.group(1), 16), {0x18, 0x28, 0x30, 0x48, 0x98, 0x138}, text[back])
                    break
            else:
                self.fail(f'no load feeds {line}')
        self.assertGreater(calls, 5)

    def test_linked_object_if_built(self):
        built = ROOT / 'build/uefi/gpt-survey.obj'
        if not built.exists() or not (LLVM / 'llvm-objdump').exists():
            self.skipTest('build/uefi/gpt-survey.obj not built')
        data = built.read_bytes()
        for name, g in self.GUIDS.items():
            self.assertNotIn(uuid.UUID(g).bytes_le, data, name)
        self.assertEqual(subprocess.run([str(LLVM / 'llvm-nm'), '-u', str(built)], check=True,
                                        capture_output=True, text=True).stdout.split(), [])
        text = subprocess.run([str(LLVM / 'llvm-objdump'), '-d', '--no-show-raw-insn', str(built)],
                              check=True, capture_output=True, text=True).stdout.splitlines()
        for i, line in enumerate(text):
            m = re.search(r'\bblr\s+(x\d+)', line)
            if not m:
                continue
            for back in range(i - 1, max(-1, i - 24), -1):
                w = re.search(rf'\s(\w+)\s+{m.group(1)}, (.*)$', text[back])
                if w:
                    slot = re.fullmatch(r'\[x\d+, #0x([0-9a-f]+)\]', w.group(2).strip())
                    self.assertTrue(w.group(1) == 'ldr' and slot, text[back])
                    self.assertIn(int(slot.group(1), 16), {0x18, 0x28, 0x30, 0x48, 0x98, 0x138}, text[back])
                    break
            else:
                self.fail(f'no load feeds {line}')

    def test_linked_only_into_the_efi_payload(self):
        make = (UEFI / 'Makefile').read_text()
        stage = re.search(r'STAGE_OBJS :=(.*?)\n\n', make, re.S).group(1)
        self.assertNotIn('gpt-survey', stage)
        self.assertIn('$(OUT)/gpt-survey.obj', make)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    build_harness()
    unittest.main()
