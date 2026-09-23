#!/usr/bin/env python3
"""Sanitized EFI loader and host-side allocation/content guards."""
from pathlib import Path
import hashlib
import struct
import subprocess
import unittest
import preload

ROOT = Path(__file__).resolve().parents[1]


class Proxy:
    def __init__(self):
        self.data = b'base system image'
        self.info = dict(memory_map=0x1000, memory_map_size=3*48, memory_map_stride=48,
                         preload_table=0x20000, preload_size=preload.CACHE_SIZE,
                         preload_status=0, preload_verify=0x10000)
        self.map = b''.join(struct.pack('<IIQQQQ8x', kind, 0, base, 0, pages, 0)
                            for kind, base, pages in [(1, 0x10000, 16), (2, 0x20000, 32), (7, 0x40000, 32)])
        entry = preload.ENTRY.pack(0x30000, 4096, len(self.data), hashlib.sha256(self.data).digest(),
                                   preload.encode_path('\\asset.bin'))
        self.table = (preload.HEADER.pack(preload.CACHE_MAGIC, 1, 1, preload.ENTRY.size)+entry).ljust(preload.CACHE_SIZE,b'\0')
        self.calls = []
        self.valid = True

    def readmem(self, base, size):
        result = self.map if base == 0x1000 else self.table
        assert size == len(result)
        return result

    def call(self, address, index, timeout):
        self.calls.append(('verify', address, index))
        return int(self.valid)

    def memcpy8(self, dst, src, size):
        self.calls.append(('copy', dst, src, size))


class Tests(unittest.TestCase):
    def test_manifest_paths_and_limits(self):
        good = ('\\asset.bin', 123, '12'*32)
        self.assertEqual(len(preload.manifest([good])), 328)
        for entries in [[], [good]*2, [('\\ASSET.bin',123,'12'*32),good],
                        [('\\..\\asset',1,'12'*32)], [('x',1,'12'*32)],
                        [('\\asset',0,'12'*32)], [('\\asset',(1<<32)+1,'12'*32)],
                        [('\\asset',1,'12')]]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):preload.manifest(entries)

    def test_old_bootinfo(self):
        self.assertEqual(preload.resident_cache(None, {}), [])

    def test_copy_and_fresh_hash(self):
        p=Proxy();f=preload.resident_cache(p,p.info)[0]
        preload.copy_cached(p,p.info,f,0x40000,len(p.data))
        self.assertEqual([c[0] for c in p.calls],['verify','copy'])
        p.calls.clear();p.valid=False
        with self.assertRaises(ValueError):preload.copy_cached(p,p.info,f,0x40000,len(p.data))
        self.assertEqual([c[0] for c in p.calls],['verify'])

    def test_refuse_copy_into_cache_or_code(self):
        p=Proxy();f=preload.resident_cache(p,p.info)[0]
        for dst in [0x10000,0x20000,0x30000,0x40001,0x60000]:
            with self.subTest(dst=dst), self.assertRaises(ValueError):
                preload.copy_cached(p,p.info,f,dst,len(p.data))
        self.assertFalse(p.calls)

    def test_cache_bounds(self):
        for offset,value in [(0,0),(8,2),(16,17),(24,999),
                             (32,0x40000),(40,8192),(48,1<<33)]:
            p=Proxy();raw=bytearray(p.table);struct.pack_into('<Q',raw,offset,value);p.table=bytes(raw)
            with self.subTest(offset=offset), self.assertRaises(ValueError):preload.resident_cache(p,p.info)

    def test_cache_header_and_verifier(self):
        for key,value in [('preload_status',1),('preload_table',0x20001),('preload_size',32),
                          ('preload_verify',0x20000),('memory_map_stride',1)]:
            p=Proxy();p.info[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):preload.resident_cache(p,p.info)


if __name__ == '__main__':
    binary=ROOT/'build/test-preload'
    subprocess.run(['clang','-std=gnu11','-O1','-g','-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all','-Wall','-Wextra','-Werror',
                    '-I'+str(ROOT/'platform/uefi'),str(ROOT/'tools/test-preload.c'),
                    str(ROOT/'platform/uefi/preload.c'),str(ROOT/'platform/uefi/sha256.c'),
                    '-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
    unittest.main()
