#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Decode and print the A16 NVMe GPT that q1n1 read before ExitBootServices.

The q1n1 EFI payload (platform/uefi/gpt-survey.c) copies LBA 0, both GPT
headers and both entry arrays of every whole fixed disk into one EfiLoaderData
record, using only firmware Block I/O ReadBlocks. This tool fetches that
record over the proxy with memory *reads* only, checks every CRC and bound on
the Mac, and prints disk size, block size, every partition and the
unallocated extents.

  python3 tools/gpt_survey.py proxy [--device udp://en5] [--save-record F] [--json F]
  python3 tools/gpt_survey.py record F                 # a saved record
  python3 tools/gpt_survey.py raw --first F --last L --disk-bytes N [--block-size 512]

`raw` decodes the first/last MiB captured from Windows (the storage backups),
so the same checks run against both readers. `--windows staging-final.json`
compares every partition with Windows' own layout record by partition GUID;
`--expect-*` pin the disk identity. Any error exits 1.
"""
import argparse
import json
from pathlib import Path
import struct
import sys
import uuid
import zlib

MAGIC = 0x31545047314E3151  # "Q1N1GPT1"
VERSION = 1
BLOCK, ARRAY, PATH, MAX_DISKS = 4096, 65536, 256, 4
HEADER_FIELDS = ('magic version size disk_size max_disks off_lba0 off_primary off_backup '
                 'off_primary_array off_backup_array off_path off_scalars off_disks status phase '
                 'handles examined candidates recorded reads skipped_absent skipped_logical '
                 'skipped_removable skipped_geometry skipped_no_media').split()
DISK_FIELDS = ('status flags handle_index revision media_id block_size io_align last_block '
               'removable present logical_partition read_only reads path_bytes '
               'primary_array_lba primary_array_bytes backup_lba backup_array_lba '
               'backup_array_bytes').split()
LAYOUT = dict(off_lba0=0, off_primary=BLOCK, off_backup=2 * BLOCK, off_primary_array=3 * BLOCK,
              off_backup_array=3 * BLOCK + ARRAY, off_path=3 * BLOCK + 2 * ARRAY,
              off_scalars=3 * BLOCK + 2 * ARRAY + PATH, off_disks=8 * len(HEADER_FIELDS))
DISK_SIZE = LAYOUT['off_scalars'] + 8 * len(DISK_FIELDS)
RECORD_SIZE = LAYOUT['off_disks'] + MAX_DISKS * DISK_SIZE
READ_LIMIT = 2 << 20  # the proxy fetch never asks for more than this
FLAGS = {1: 'lba0', 2: 'primary', 4: 'primary-array', 8: 'backup', 16: 'backup-array',
         32: 'array-too-large', 64: 'path-truncated', 128: 'nvme', 256: 'primary-not-gpt',
         512: 'backup-not-gpt', 1024: 'array-out-of-range'}
MIB = 1 << 20

TYPES = {
    'c12a7328-f81f-11d2-ba4b-00a0c93ec93b': 'EFI system',
    'e3c9e316-0b5c-4db8-817d-f92df00215ae': 'Microsoft reserved',
    'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7': 'Basic data',
    'de94bba4-06d1-4d40-a16a-bfd50179d6ac': 'Windows recovery',
    '7c3457ef-0000-11aa-aa11-00306543ecac': 'Apple APFS',
    '69646961-6700-11aa-aa11-00306543ecac': 'Apple iBootSystemContainer',
    '52637672-7900-11aa-aa11-00306543ecac': 'Apple RecoveryOSContainer',
}


class SurveyError(ValueError):
    pass


def guid(raw):
    return str(uuid.UUID(bytes_le=bytes(raw)))


# ---------------------------------------------------------------- record ---

def parse_record(raw):
    """Validate the q1n1 record layout and return (header, disks)."""
    if len(raw) < 8 * len(HEADER_FIELDS):
        raise SurveyError('record shorter than its header')
    header = dict(zip(HEADER_FIELDS, struct.unpack_from(f'<{len(HEADER_FIELDS)}Q', raw)))
    if header['magic'] != MAGIC or header['version'] != VERSION:
        raise SurveyError(f"not a q1n1 GPT survey record (magic {header['magic']:#x}, version {header['version']})")
    if (header['size'] != RECORD_SIZE or len(raw) != RECORD_SIZE or header['disk_size'] != DISK_SIZE
            or header['max_disks'] != MAX_DISKS or any(header[k] != v for k, v in LAYOUT.items())):
        raise SurveyError('record layout differs from this tool; rebuild both from one tree')
    if header['recorded'] > MAX_DISKS or header['recorded'] > header['candidates']:
        raise SurveyError('record claims more disks than it can hold')
    disks = []
    for n in range(header['recorded']):
        base = LAYOUT['off_disks'] + n * DISK_SIZE
        d = dict(zip(DISK_FIELDS, struct.unpack_from(f'<{len(DISK_FIELDS)}Q', raw, base + LAYOUT['off_scalars'])))
        bs = d['block_size']
        if bs not in (512, 1024, 2048, 4096) or d['path_bytes'] > PATH or d['reads'] > 5:
            raise SurveyError(f'disk {n}: implausible scalars')
        for key in ('primary_array_bytes', 'backup_array_bytes'):
            if d[key] > ARRAY or d[key] % bs:
                raise SurveyError(f'disk {n}: {key} out of bounds')
        blob = lambda off, length: bytes(raw[base + LAYOUT[off]: base + LAYOUT[off] + length])
        d['lba0'] = blob('off_lba0', bs)
        d['primary'] = blob('off_primary', bs)
        d['backup'] = blob('off_backup', bs)
        d['primary_array'] = blob('off_primary_array', d['primary_array_bytes'])
        d['backup_array'] = blob('off_backup_array', d['backup_array_bytes'])
        d['path'] = blob('off_path', d['path_bytes'])
        d['flag_names'] = [name for bit, name in FLAGS.items() if d['flags'] & bit]
        disks.append(d)
    return header, disks


def device_path(raw):
    """Decode the nodes the survey keeps; unknown nodes are shown as type/subtype."""
    nodes, info, off = [], {}, 0
    while off + 4 <= len(raw):
        kind, sub, length = raw[off], raw[off + 1], struct.unpack_from('<H', raw, off + 2)[0]
        if length < 4 or off + length > len(raw):
            nodes.append('<truncated>')
            break
        body = raw[off + 4: off + length]
        if (kind, sub) == (0x7F, 0xFF):
            break
        if (kind, sub) == (2, 1) and len(body) >= 8:
            hid, uid = struct.unpack_from('<II', body)
            nodes.append(f'Acpi({hid:#x},{uid})')
            info.setdefault('acpi_uid', uid)
        elif (kind, sub) == (1, 1) and len(body) >= 2:
            nodes.append(f'Pci({body[1]:#x},{body[0]:#x})')
            info.setdefault('pci', []).append((body[1], body[0]))
        elif (kind, sub) == (3, 0x17) and len(body) >= 12:
            nsid, = struct.unpack_from('<I', body)
            eui = body[4:12]
            nodes.append(f'NVMe({nsid:#x},{eui.hex("-").upper()})')
            info.update(nvme_nsid=nsid, nvme_eui_bytes=eui.hex().upper())
        elif (kind, sub) == (3, 5):
            nodes.append('USB(...)')
            info['usb'] = True
        else:
            nodes.append(f'Node({kind},{sub:#x})')
        off += length
    return '/'.join(nodes), info


# ------------------------------------------------------------------- gpt ---

def _header(block, block_size, name, errors):
    if block[:8] != b'EFI PART':
        errors.append(f'{name} header: no EFI PART signature')
        return None
    (rev, size, crc, _, mine, other, first, last) = struct.unpack_from('<IIIIQQQQ', block, 8)
    entries, count, esize, array_crc = struct.unpack_from('<QIII', block, 72)
    h = dict(revision=rev, header_size=size, header_crc=crc, my_lba=mine, alternate_lba=other,
             first_usable=first, last_usable=last, disk_guid=guid(block[56:72]),
             entries_lba=entries, count=count, entry_size=esize, array_crc=array_crc)
    if not 92 <= size <= block_size:
        errors.append(f'{name} header: size {size} out of range')
        return h
    raw = bytearray(block[:size])
    raw[16:20] = bytes(4)
    h['header_crc_ok'] = zlib.crc32(raw) == crc
    if not h['header_crc_ok']:
        errors.append(f'{name} header: CRC mismatch')
    if esize < 128 or esize & (esize - 1) or not count:
        errors.append(f'{name} header: entry size {esize} / count {count} invalid')
    return h


def _entries(array, h, name, errors):
    need = h['count'] * h['entry_size']
    if len(array) < need:
        errors.append(f'{name} array: {len(array)} of {need} bytes recorded')
        return None
    h['array_crc_ok'] = zlib.crc32(array[:need]) == h['array_crc']
    if not h['array_crc_ok']:
        errors.append(f'{name} array: CRC mismatch')
    parts = []
    for i in range(h['count']):
        e = array[i * h['entry_size']:(i + 1) * h['entry_size']]
        if e[:16] == bytes(16):
            continue
        first, last, attrs = struct.unpack_from('<QQQ', e, 32)
        name_text = e[56:128].decode('utf-16le', 'replace').split('\0', 1)[0]
        t = guid(e[:16])
        parts.append(dict(index=i, type=t, type_name=TYPES.get(t, ''), guid=guid(e[16:32]),
                          first_lba=first, last_lba=last, attributes=attrs, name=name_text))
    return parts


def decode_gpt(block_size, last_block, lba0, primary, primary_array, backup, backup_array):
    """Return a dict describing the disk; problems land in result['errors']."""
    errors, warnings = [], []
    r = dict(block_size=block_size, last_block=last_block, blocks=last_block + 1,
             disk_bytes=(last_block + 1) * block_size, errors=errors, warnings=warnings)
    if len(lba0) < 512 or lba0[510:512] != b'\x55\xaa' or lba0[450] != 0xEE:
        errors.append('LBA 0 is not a protective MBR (0x55AA with an 0xEE entry)')
    p = _header(primary, block_size, 'primary', errors) if primary else None
    b = _header(backup, block_size, 'backup', errors) if backup else None
    if p is None:
        errors.append('primary GPT header unavailable')
    if b is None:
        errors.append('backup GPT header unavailable')
    r['primary'], r['backup'] = p, b
    pp = _entries(primary_array, p, 'primary', errors) if p and primary_array else None
    bp = _entries(backup_array, b, 'backup', errors) if b and backup_array else None
    if p:
        if p['my_lba'] != 1:
            errors.append(f"primary header claims LBA {p['my_lba']}")
        if p['alternate_lba'] != last_block:
            errors.append(f"primary AlternateLBA {p['alternate_lba']} is not the last LBA {last_block}")
        if not (2 <= p['entries_lba'] and p['first_usable'] <= p['last_usable'] < last_block):
            errors.append('primary usable range is inconsistent')
    if b:
        if b['my_lba'] != last_block or b['alternate_lba'] != 1:
            errors.append('backup header LBAs are inconsistent')
    if p and b:
        for key in ('disk_guid', 'first_usable', 'last_usable', 'count', 'entry_size', 'array_crc'):
            if p[key] != b[key]:
                errors.append(f'primary/backup differ in {key}: {p[key]} vs {b[key]}')
    if pp is not None and bp is not None and pp != bp:
        errors.append('primary and backup partition entries differ')
    parts = pp if pp is not None else (bp or [])
    r['partitions'] = sorted(parts, key=lambda x: x['first_lba'])
    hdr = p or b
    if hdr:
        r['disk_guid'] = hdr['disk_guid']
        r['first_usable'], r['last_usable'] = hdr['first_usable'], hdr['last_usable']
        seen = set()
        for x in r['partitions']:
            x['bytes'] = (x['last_lba'] - x['first_lba'] + 1) * block_size if x['last_lba'] >= x['first_lba'] else 0
            x['offset'] = x['first_lba'] * block_size
            if x['first_lba'] > x['last_lba']:
                errors.append(f"entry {x['index']}: first LBA after last LBA")
            if x['first_lba'] < hdr['first_usable'] or x['last_lba'] > hdr['last_usable']:
                errors.append(f"entry {x['index']}: outside the usable LBA range")
            if x['guid'] in seen:
                errors.append(f"entry {x['index']}: duplicate partition GUID {x['guid']}")
            seen.add(x['guid'])
        ordered = r['partitions']
        for a, c in zip(ordered, ordered[1:]):
            if c['first_lba'] <= a['last_lba']:
                errors.append(f"entries {a['index']} and {c['index']} overlap")
        r['unallocated'] = unallocated(ordered, hdr['first_usable'], hdr['last_usable'], block_size)
    return r


def unallocated(parts, first_usable, last_usable, block_size, align=MIB):
    """Gaps in [first_usable, last_usable], plus their 1 MiB-aligned interior."""
    extents, cursor = [], first_usable
    for x in sorted(parts, key=lambda x: x['first_lba']):
        if x['first_lba'] > cursor:
            extents.append((cursor, x['first_lba'] - 1))
        cursor = max(cursor, x['last_lba'] + 1)
    if cursor <= last_usable:
        extents.append((cursor, last_usable))
    out = []
    for first, last in extents:
        start, end = first * block_size, (last + 1) * block_size
        a_start, a_end = -(-start // align) * align, end // align * align
        out.append(dict(first_lba=first, last_lba=last, blocks=last - first + 1, offset=start,
                        end=end, bytes=end - start, aligned_offset=a_start,
                        aligned_bytes=max(0, a_end - a_start)))
    return out


# ------------------------------------------------------------ comparisons ---

def compare_windows(decoded, record):
    """Compare with a Windows layout record (Get-Disk/Get-Partition JSON)."""
    problems = []
    disk = record.get('Disk', {})
    if disk.get('Size') not in (None, decoded['disk_bytes']):
        problems.append(f"Windows disk size {disk['Size']} != {decoded['disk_bytes']}")
    if disk.get('LogicalSectorSize') not in (None, decoded['block_size']):
        problems.append(f"Windows sector size {disk['LogicalSectorSize']} != {decoded['block_size']}")
    ours = {p['guid']: p for p in decoded.get('partitions', [])}
    theirs = {str(uuid.UUID(p['Guid'])): p for p in record.get('Partitions', [])}
    for g in sorted(set(ours) | set(theirs)):
        if g not in ours:
            problems.append(f'{g}: in Windows record, not on disk')
            continue
        if g not in theirs:
            problems.append(f'{g}: on disk, not in Windows record')
            continue
        a, w = ours[g], theirs[g]
        if a['offset'] != w['Offset'] or a['bytes'] != w['Size'] or a['type'] != str(uuid.UUID(w['GptType'])):
            problems.append(f"{g}: disk {a['offset']}+{a['bytes']} {a['type']} vs Windows "
                            f"{w['Offset']}+{w['Size']} {w['GptType']}")
    if 'LargestFreeExtent' in disk:
        # Windows reports 1 MiB-aligned free space; sub-MiB slivers count as 0.
        largest = max((u['aligned_bytes'] for u in decoded.get('unallocated', [])), default=0)
        if largest != disk['LargestFreeExtent']:
            problems.append(f"largest free extent {largest} != Windows {disk['LargestFreeExtent']}")
    return problems


def check_expectations(decoded, info, args):
    problems = []
    if args.expect_bytes is not None and decoded['disk_bytes'] != args.expect_bytes:
        problems.append(f"disk is {decoded['disk_bytes']} bytes, expected {args.expect_bytes}")
    if args.expect_disk_guid and decoded.get('disk_guid') != str(uuid.UUID(args.expect_disk_guid)):
        problems.append(f"disk GUID {decoded.get('disk_guid')} != {args.expect_disk_guid}")
    if args.expect_eui:
        want = args.expect_eui.upper().removeprefix('EUI.')
        have = (info or {}).get('nvme_eui_bytes')
        # Windows prints the Identify Namespace EUI64 bytes in order; firmware
        # stores the same bytes. Accept the reversed order too, but say which.
        if have == want:
            decoded['eui_order'] = 'identify'
        elif have and bytes.fromhex(have)[::-1].hex().upper() == want:
            decoded['eui_order'] = 'reversed'
        else:
            problems.append(f'NVMe EUI64 {have} != {want}')
    return problems


# ------------------------------------------------------------------ fetch ---

class ReadOnlyProxy:
    """The only proxy surface this tool can reach: bootinfo and bounded reads."""

    def __init__(self, proxy):
        self._proxy = proxy

    def bootinfo(self):
        return self._proxy.bootinfo()

    def readmem(self, address, size):
        if not 0 < size <= READ_LIMIT:
            raise SurveyError(f'refusing a {size}-byte read')
        return self._proxy.readmem(address, size)


def fetch(proxy):
    """Read the record through a ReadOnlyProxy; refuse anything not in LoaderData."""
    import preload  # q1n1 tools: memory-map parsing shared with the preload
    ro = proxy if isinstance(proxy, ReadOnlyProxy) else ReadOnlyProxy(proxy)
    info = ro.bootinfo()
    address, size = info.get('gpt_survey', 0), info.get('gpt_survey_size', 0)
    if not address:
        raise SurveyError('this q1n1 image publishes no GPT survey (older payload, or allocation failed)')
    if size != RECORD_SIZE or address % 4096:
        raise SurveyError(f'advertised record {address:#x}+{size} does not match this tool')
    regions = preload.memory_regions(ro, info)
    if not preload._covered(regions, 2, address, (size + 4095) & ~4095):
        raise SurveyError('GPT survey record is not in reserved EFI LoaderData')
    return ro.readmem(address, size)


# ------------------------------------------------------------------ print ---

def report(decoded, header=None, disk=None, path_text=None, out=sys.stdout):
    w = out.write
    if header:
        w(f"survey: phase {header['phase']} status {header['status']:#x}; {header['handles']} Block I/O handles, "
          f"{header['candidates']} whole fixed disks, {header['recorded']} recorded, {header['reads']} reads\n")
    if disk:
        w(f"disk handle {disk['handle_index']}: media {disk['media_id']}, status {disk['status']:#x}, "
          f"flags {','.join(disk['flag_names']) or '-'}, {disk['reads']} reads, io_align {disk['io_align']}\n")
    if path_text:
        w(f'device path: {path_text}\n')
    bs = decoded['block_size']
    w(f"disk: {decoded['disk_bytes']} bytes ({decoded['disk_bytes'] / (1 << 30):.3f} GiB), "
      f"{decoded['blocks']} blocks of {bs} bytes, last LBA {decoded['last_block']}\n")
    if decoded.get('disk_guid'):
        p, b = decoded['primary'], decoded['backup']
        w(f"GPT: disk GUID {decoded['disk_guid']}, usable LBA {decoded['first_usable']}..{decoded['last_usable']}, "
          f"{p['count'] if p else b['count']} entries x {p['entry_size'] if p else b['entry_size']} bytes; "
          f"primary {'ok' if p and p.get('header_crc_ok') and p.get('array_crc_ok') else 'BAD'}, "
          f"backup {'ok' if b and b.get('header_crc_ok') and b.get('array_crc_ok') else 'BAD'}\n")
    w(f"{'#':>3} {'first LBA':>12} {'last LBA':>12} {'offset':>15} {'bytes':>15} {'GiB':>9}  "
      f"{'type':36}  {'partition GUID':36}  attrs              name\n")
    for x in decoded.get('partitions', []):
        w(f"{x['index']:>3} {x['first_lba']:>12} {x['last_lba']:>12} {x['offset']:>15} {x['bytes']:>15} "
          f"{x['bytes'] / (1 << 30):>9.3f}  {x['type']:36}  {x['guid']:36}  {x['attributes']:#018x} "
          f"{x['name']}{' [' + x['type_name'] + ']' if x['type_name'] else ''}\n")
    for u in decoded.get('unallocated', []):
        w(f"unallocated: LBA {u['first_lba']}..{u['last_lba']} ({u['blocks']} blocks), bytes "
          f"[{u['offset']}, {u['end']}) = {u['bytes']} ({u['bytes'] / (1 << 30):.3f} GiB); "
          f"1 MiB-aligned interior {u['aligned_offset']} + {u['aligned_bytes']}\n")
    for e in decoded['errors']:
        w(f'ERROR: {e}\n')
    for e in decoded['warnings']:
        w(f'warning: {e}\n')


def decode_record(raw):
    header, disks = parse_record(raw)
    results = []
    for d in disks:
        decoded = decode_gpt(d['block_size'], d['last_block'], d['lba0'],
                             d['primary'] if d['flags'] & 2 else b'',
                             d['primary_array'], d['backup'] if d['flags'] & 8 else b'', d['backup_array'])
        text, info = device_path(d['path'])
        if d['status']:
            decoded['errors'].append(f"firmware read failed with status {d['status']:#x}")
        if d['flags'] & 32:
            decoded['errors'].append('entry array larger than 64 KiB was not read')
        if d['flags'] & 64:
            decoded['warnings'].append('device path truncated')
        results.append((d, decoded, text, info))
    if header['handles'] > header['examined']:
        for _, decoded, _, _ in results[:1]:
            decoded['warnings'].append(f"only {header['examined']} of {header['handles']} handles examined")
    return header, results


def raw_windows(first, last, disk_bytes, block_size):
    if disk_bytes % block_size or len(first) < 2 * block_size or len(last) < block_size:
        raise SurveyError('raw capture geometry is inconsistent')
    last_block = disk_bytes // block_size - 1
    window_start = disk_bytes - len(last)

    def at(lba, nbytes):
        off = lba * block_size
        if off + nbytes <= len(first):
            return first[off:off + nbytes]
        if off >= window_start:
            return last[off - window_start: off - window_start + nbytes]
        raise SurveyError(f'LBA {lba} is outside the captured windows')

    def array_for(header_block):
        if header_block[:8] != b'EFI PART':
            return b''
        entries, count, esize = struct.unpack_from('<QII', header_block, 72)
        need = -(-count * esize // block_size) * block_size
        return at(entries, need) if 0 < need <= ARRAY else b''
    primary, backup = at(1, block_size), at(last_block, block_size)
    return decode_gpt(block_size, last_block, at(0, block_size), primary, array_for(primary),
                      backup, array_for(backup))


def _eui_matches(info, want):
    have = (info or {}).get('nvme_eui_bytes')
    return bool(have) and want in (have, bytes.fromhex(have)[::-1].hex().upper())


def pinned_disk(results, args):
    """Which result the pins and --windows apply to.

    Without --expect-eui/--expect-disk-guid every recorded disk is checked
    (the original behaviour). With either pin, exactly one recorded disk must
    match it; the comparisons apply to that disk only, and the other whole
    fixed disks the firmware exposes (the A16 lists 78 Block I/O handles) are
    printed but cannot fail the survey. Returns (index or None, problems)."""
    if not (args.expect_eui or args.expect_disk_guid):
        return None, []
    want_eui = args.expect_eui.upper().removeprefix('EUI.') if args.expect_eui else None
    want_guid = str(uuid.UUID(args.expect_disk_guid)) if args.expect_disk_guid else None
    hits = [n for n, (_, decoded, _, info) in enumerate(results)
            if (want_eui is None or _eui_matches(info, want_eui))
            and (want_guid is None or decoded.get('disk_guid') == want_guid)]
    if len(hits) != 1:
        return None, [f'expected exactly one recorded disk matching the pins, found {len(hits)} '
                      f'of {len(results)}']
    return hits[0], []


def evaluate(results, args, header=None, out=sys.stdout):
    target, problems = pinned_disk(results, args)
    failed = bool(problems)
    for p in problems:
        out.write(f'ERROR: {p}\n')
    windows = json.loads(args.windows.read_text(encoding='utf-8-sig')) if args.windows else None
    summary = []
    for n, (disk, decoded, text, info) in enumerate(results):
        checked = target is None or n == target
        if checked:
            extra = check_expectations(decoded, info, args)
            if windows is not None:
                extra += compare_windows(decoded, windows)
            decoded['errors'].extend(extra)
            failed |= bool(decoded['errors'])
        elif not problems:
            decoded['warnings'].append('not the pinned disk: printed only, its problems do not fail this survey')
        decoded['pinned'] = target is not None and n == target
        report(decoded, header if n == 0 else None, disk, text, out=out)
        summary.append(dict({k: v for k, v in decoded.items()}, device_path=text, path_info=info,
                            survey_flags=disk['flag_names'] if disk else None,
                            handle_index=disk['handle_index'] if disk else None))
        if len(results) > 1:
            out.write('\n')
    return failed, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    px = sub.add_parser('proxy', help='fetch the record from a running q1n1 (reads only)')
    px.add_argument('--device', help='tty, socket or udp://enN (default: discover)')
    px.add_argument('--save-record', type=Path)
    rc = sub.add_parser('record', help='decode a saved record')
    rc.add_argument('file', type=Path)
    rw = sub.add_parser('raw', help='decode captured first/last MiB of the disk')
    rw.add_argument('--first', type=Path, required=True)
    rw.add_argument('--last', type=Path, required=True)
    rw.add_argument('--disk-bytes', type=int, required=True)
    rw.add_argument('--block-size', type=int, default=512)
    for p in (px, rc, rw):
        p.add_argument('--json', type=Path)
        p.add_argument('--windows', type=Path, help='Windows layout JSON to compare by partition GUID')
        p.add_argument('--expect-bytes', type=int)
        p.add_argument('--expect-disk-guid')
        p.add_argument('--expect-eui', help='e.g. eui.002538F261C3C291 (proxy/record only)')
    args = ap.parse_args(argv)

    if args.mode == 'raw':
        decoded = raw_windows(args.first.read_bytes(), args.last.read_bytes(), args.disk_bytes, args.block_size)
        results, header = [(None, decoded, None, {})], None
    else:
        if args.mode == 'proxy':
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import q1n1proxy
            raw = fetch(q1n1proxy.connect(args.device))
            if args.save_record:
                args.save_record.write_bytes(raw)
        else:
            raw = args.file.read_bytes()
        header, results = decode_record(raw)
        if not results:
            print('no whole fixed disk was recorded', file=sys.stderr)
            return 1
    failed, summary = evaluate(results, args, header)
    if args.json:
        args.json.write_text(json.dumps(dict(survey=header, disks=summary), indent=1, default=str) + '\n')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
