#!/usr/bin/env python3
"""Versioned, hash-bound EFI preload manifests and resident cache validation."""
import argparse
import hashlib
from pathlib import Path
import struct

MANIFEST_MAGIC = 0x31444C50314E3151
CACHE_MAGIC = 0x31484343314E3151
MAX_FILES = 16
LIMIT = 1 << 32
FILE = struct.Struct('<Q32s256s')
ENTRY = struct.Struct('<QQQ32s256s')
HEADER = struct.Struct('<4Q')
CACHE_SIZE = HEADER.size + MAX_FILES * ENTRY.size
DIAGNOSTICS_FIELDS = ('version size phase status block_devices connect_successes '
                      'filesystems roots manifests asset_index read_bytes expected_bytes').split()


def diagnostics(proxy, info):
    address = info.get('preload_diagnostics', 0)
    if not address:
        return None
    regions = memory_regions(proxy, info)
    if not _covered(regions, 1, address, len(DIAGNOSTICS_FIELDS)*8):
        raise ValueError('preload diagnostics are not in reserved EFI LoaderCode')
    raw = proxy.readmem(address, len(DIAGNOSTICS_FIELDS)*8)
    result = dict(zip(DIAGNOSTICS_FIELDS, struct.unpack('<12Q', raw)))
    if result['version'] != 1 or result['size'] != len(raw):
        raise ValueError('unsupported preload diagnostics')
    return result


def encode_path(path):
    if (not path.startswith('\\') or any(c in path for c in '/:*?"<>|') or
            any(not part or part in ('.', '..') for part in path[1:].split('\\'))):
        raise ValueError('preload path must be an absolute, canonical EFI file path')
    raw = path.encode('ascii')
    if len(raw) >= 256 or any(c < 32 or c > 126 for c in raw):
        raise ValueError('preload path must contain fewer than 256 printable ASCII bytes')
    return raw.ljust(256, b'\0')


def manifest(files):
    """files contains (EFI path, byte size, SHA256 hex) tuples, with no host paths."""
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError('manifest needs 1..16 files')
    total = 0
    seen = set()
    records = []
    for path, size, digest in files:
        raw = encode_path(path)
        if raw.lower() in seen or not 0 < size <= LIMIT - total:
            raise ValueError('duplicate path or preload exceeds 4 GiB')
        hashed = bytes.fromhex(digest)
        if len(hashed) != 32:
            raise ValueError('preload SHA256 must contain 32 bytes')
        seen.add(raw.lower())
        total += size
        records.append(FILE.pack(size, hashed, raw))
    return HEADER.pack(MANIFEST_MAGIC, 1, len(files), FILE.size) + b''.join(records)


def memory_regions(proxy, info):
    base, size, stride = (info.get(k, 0) for k in
                          ('memory_map', 'memory_map_size', 'memory_map_stride'))
    if not base or not 40 <= stride <= 4096 or not 0 < size <= 1 << 20 or size % stride:
        raise ValueError('invalid EFI memory map for preload validation')
    raw = proxy.readmem(base, size)
    regions = []
    for off in range(0, size, stride):
        kind, _, start, _, pages, _ = struct.unpack_from('<IIQQQQ', raw, off)
        end = start + pages * 4096
        if start % 4096 or end > 1 << 64:
            raise ValueError('invalid EFI memory descriptor')
        regions.append((kind, start, end))
    return regions


def _covered(regions, kind, base, size):
    end = base + size
    if not base or size <= 0 or end > 1 << 64:
        return False
    cursor = base
    for lo, hi in sorted((lo, hi) for k, lo, hi in regions if k == kind):
        if lo <= cursor < hi:
            cursor = hi
            if cursor >= end:
                return True
    return False


def resident_cache(proxy, info):
    """Fail closed on malformed advertised caches; older q1n1 returns no cache."""
    address, size = info.get('preload_table', 0), info.get('preload_size', 0)
    if not address and not size:
        return []
    if info.get('preload_status') or size != CACHE_SIZE or address % 4096:
        raise ValueError('invalid preload table/status')
    regions = memory_regions(proxy, info)
    if not _covered(regions, 2, address, (size+4095)&~4095):
        raise ValueError('preload table is not reserved EFI LoaderData')
    verify = info.get('preload_verify', 0)
    if verify % 4 or not _covered(regions, 1, verify, 4):
        raise ValueError('preload verifier is not in reserved EFI LoaderCode')
    raw = proxy.readmem(address, size)
    magic, version, count, stride = HEADER.unpack_from(raw)
    if magic != CACHE_MAGIC or version != 1 or not 1 <= count <= MAX_FILES or stride != ENTRY.size:
        raise ValueError('unsupported preload cache header')
    ranges = [(address, address+((size+4095)&~4095))]
    files, paths, total = [], set(), 0
    for i in range(count):
        base, allocation, length, digest, path = ENTRY.unpack_from(raw, HEADER.size+i*stride)
        if (base % 4096 or not 0 < length <= LIMIT-total or
                allocation != (length+4095)&~4095 or
                not _covered(regions, 2, base, allocation)):
            raise ValueError('preload data is outside its reserved allocation')
        if any(base < hi and lo < base+allocation for lo, hi in ranges):
            raise ValueError('overlapping preload allocations')
        name = path.split(b'\0', 1)[0].decode('ascii')
        if encode_path(name) != path or name.lower() in paths:
            raise ValueError('invalid or duplicate preload path')
        total += length
        ranges.append((base, base+allocation))
        paths.add(name.lower())
        files.append(dict(index=i, base=base, allocation=allocation,
                          size=length, sha256=digest.hex(), path=name))
    return files


def copy_cached(proxy, info, entry, destination, size):
    """Fresh whole-file hash, then copy in bounded requests into free guest RAM."""
    if size != entry['size'] or destination % 4096:
        raise ValueError('cached copy length/alignment mismatch')
    regions = memory_regions(proxy, info)
    if not _covered(regions, 7, destination, size):
        raise ValueError('cached copy destination is not EFI conventional memory')
    if proxy.call(info['preload_verify'], entry['index'], timeout=180) != 1:
        raise ValueError('resident preload SHA256 no longer matches its manifest')
    for offset in range(0, size, 8 << 20):
        amount = min(8 << 20, size-offset)
        proxy.memcpy8(destination+offset, entry['base']+offset, amount)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--file', nargs=2, action='append', required=True,
                    metavar=('EFI_PATH', 'LOCAL_FILE'))
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    files = []
    for efi, local in args.file:
        p = Path(local)
        with p.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append((efi, p.stat().st_size, digest))
    data = manifest(files)
    args.output.write_bytes(data)
    print(f'{args.output}: {len(files)} files, {sum(f[1] for f in files)} bytes, manifest {len(data)} bytes')


if __name__ == '__main__':
    main()
