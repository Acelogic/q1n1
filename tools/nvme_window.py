#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check, qualify, apply or restore a bounded SSD delta directly through q1n1.

Failure analysis and qualification plan: docs/A16-NVME-WRITES.md.
Check is the default. Probe restores its saved scratch bytes. Apply/Restore
require a matching physical probe record and an explicitly pinned package.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import nvme_m3 as m3
from nvme_rw import WritableNvme
from nvme_hash import NativeHash

ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources():
    names = ['tools/nvme_m3.py', 'tools/nvme_rw.py', 'tools/nvme_hash.py', 'tools/nvme_window.py',
             'platform/uefi/nvme-hash.c', 'platform/uefi/sha256.c', 'platform/uefi/sha256.h']
    return {n: digest((ROOT / n).read_bytes()) for n in names}


def require(condition, message):
    if not condition:
        raise m3.Refused(message)


def package(path, pin):
    raw = (path / 'package.json').read_bytes()
    require(digest(raw) == pin, 'package.json differs from the explicitly supplied SHA-256')
    p = json.loads(raw)
    require(p['result'] == 'PACKAGED_LOCAL_ONLY', 'incomplete package')
    raw = (path / 'manifest.json').read_bytes()
    require(digest(raw) == p['manifest_sha256'], 'manifest digest')
    m = json.loads(raw)
    require(m['schema'] == 'snapintosh-window-delta/1' and m['block_bytes'] == 4096, 'package schema')
    before, after = [(path / n).read_bytes() for n in ['before.bin', 'after.bin']]
    hbefore, hafter = [(path / n).read_bytes() for n in ['header-before.bin', 'header-after.bin']]
    require(0 < len(before) == len(after) == m['payload_bytes'] <= 64 * MIB, 'package byte bound')
    require(digest(before) == m['before_pack_sha256'] and digest(after) == m['after_pack_sha256'], 'pack digest')
    require(len(hbefore) == len(hafter) == 4096 and digest(hbefore) == p['header_before_sha256']
            and digest(hafter) == p['header_after_sha256'], 'header digest')
    packed = end = 0
    for e in m['extents']:
        off, size, po = e['container_offset'], e['bytes'], e['pack_offset']
        require(off >= end and size > 0 and not (off % 4096 or size % 4096)
                and off + size <= m['container']['bytes'] and po == packed, 'extent layout')
        require(digest(before[po:po + size]) == e['before_sha256']
                and digest(after[po:po + size]) == e['after_sha256'], 'extent digest')
        packed += size
        end = off + size
    require(packed == len(before) and len(m['extents']) == p['extents'], 'extent coverage')
    return p, m, before, after, hbefore, hafter


def run(args, record, save):
    p, m, before, after, hbefore, hafter = package(args.payload, args.package_sha256)
    if args.mode in ('apply', 'restore'):
        require(args.probe_record is not None, 'a completed physical probe record is required')
        probe = json.loads(args.probe_record.read_text())
        require(probe['result'] == 'PASS' and probe['mode'] == 'probe' and probe['scratch_restored']
                and probe['package_sha256'] == args.package_sha256 and probe['sources'] == record['sources'],
                'probe record is incomplete, foreign or from different sources')
    proxy = m3.q1n1proxy.connect(args.device)
    proxy.resume_skip = False  # Stop at a native fault; never walk past it.
    guard = None
    nv = None
    try:
        guard = proxy.serror_state()
        proxy.serror_reset()
        proxy.serror_strict(True)
        hw = m3.Hw(proxy)
        info = proxy.bootinfo()
        require(info['current_el'] == 2 and info['stage_generation'] >= 1, 'chainload the guarded q1n1 stage first')
        record['bootinfo'] = info
        bar, sid = m3.locate(hw, info, record)
        smmu = m3.Smmu(hw, sid)
        record['smmu'] = dict(mode=smmu.mode, **smmu.info)
        require(smmu.mode in ('bypass', 's2'), 'unsupported SSD SMMU mode')
        window = m3.pick_window(hw, info, smmu, record)
        nv = WritableNvme(hw, bar, window, record)
        record['stage'] = 'takeover'
        save()
        nv.takeover(record)
        nv.identify(record)
        nv.features(record)
        nv.io_queues(record)
        bs = nv.lba_bytes
        require(bs in (512, 4096) and nv.nsze * bs == m['disk']['bytes'], 'namespace byte size')
        require('eui.' + record['identify_namespace']['eui64'].lower() == m['disk']['unique_id'].lower(), 'package namespace EUI')
        gpt = m3.read_gpt(nv, nv.nsze - 1)
        require(not gpt['errors'] and gpt['disk_guid'] == m['disk']['guid'].strip('{}').lower(), 'GPT identity/CRC')
        parts = gpt['partitions']
        require(len(parts) == 19, 'expected the provisioned 19-partition layout')
        matches = [v for v in parts if v['guid'] == m['partition']['guid'].strip('{}').lower()]
        require(len(matches) == 1, 'partition GUID missing or ambiguous')
        part = matches[0]
        require(part['offset'] == m['partition']['offset'] and part['bytes'] == m['partition']['size']
                and part['type'] == m['partition']['type'].strip('{}').lower(), 'partition type/range')
        record['partition'] = part
        po = part['offset']
        co = po + m['layout']['container_offset']
        cb = m['container']['bytes']
        ho = po + m['layout']['header_offset']
        require(m['layout']['header_offset'] == 524288 and m['layout']['container_offset'] == 537919488
                and co + cb < po + part['bytes'] - MIB, 'window layout')

        def read(absolute, size):
            require(not (absolute % bs or size % bs), 'unaligned disk read')
            return nv.read(absolute // bs, size // bs)

        def write(absolute, data):
            require(absolute % bs == 0, 'unaligned disk write')
            nv.write(absolute // bs, data)

        left = max((v for v in parts if v['offset'] + v['bytes'] <= po), key=lambda v: v['offset'])
        right = min((v for v in parts if v['offset'] >= po + part['bytes']), key=lambda v: v['offset'])
        edge_ranges = {'gpt_primary': (0, MIB), 'gpt_backup': (nv.nsze * bs - MIB, MIB),
                       'left': (left['offset'] + left['bytes'] - MIB, MIB), 'right': (right['offset'], MIB)}

        def edges():
            return {name: digest(read(*region)) for name, region in edge_ranges.items()}

        original_edges = edges()
        record['edges_before'] = original_edges
        current_header = read(ho, 4096)
        accepted = (hbefore, hafter, bytes(4096)) if args.mode == 'restore' else (hbefore,)
        require(current_header in accepted, 'SSD window header differs from the permitted baseline')
        nh = NativeHash(nv)
        record['stage'] = 'hash-sample'
        save()
        sample = read(co, 4 * MIB)
        native = nh.digest(co // bs, len(sample) // bs)
        require(native == digest(sample), 'native hash disagrees with host SHA-256 on SSD sample')
        record['hash_sample'] = dict(bytes=len(sample), sha256=native, host_equal=True)
        print('Native SSD hash equals host SHA-256 on the 4 MiB physical sample.', flush=True)

        def hash_range(label, absolute, size, overlays=()):
            require(not (absolute % bs or size % bs), 'unaligned hash range')
            record['stage'] = label
            save()
            last = [time.monotonic()]
            def progress(done, total, seconds):
                if time.monotonic() - last[0] >= 10 or done == total:
                    print(f'{label}: {done / MIB:.0f}/{total / MIB:.0f} MiB, {done / max(seconds, .001) / MIB:.1f} MiB/s', flush=True)
                    last[0] = time.monotonic()
            value = nh.digest(absolute // bs, size // bs, progress, overlays)
            record.setdefault('hashes', {})[label] = value
            save()
            return value

        if args.mode == 'probe':
            absolute = co + cb + MIB
            size = nv.max_transfer + 4096
            require(absolute + size < po + part['bytes'] - MIB, 'scratch would leave unused window space')
            original = read(absolute, size)
            backup = args.json.with_suffix('') / 'scratch-before.bin'
            with backup.open('xb') as f:
                f.write(original)
                f.flush()
                os.fsync(f.fileno())
            record['scratch'] = dict(absolute=absolute, bytes=size, before_sha256=digest(original), backup=str(backup))
            pattern = hashlib.shake_256(b'q1n1 native NVMe write qualification 20260925').digest(size)
            nv.enable_writes([(absolute // bs, size // bs)])
            record['stage'] = 'scratch-write'
            save()
            write(absolute, pattern)
            nv.flush()
            require(read(absolute, size) == pattern, 'scratch write read-back differs')
            require(nh.digest(absolute // bs, size // bs) == digest(pattern), 'scratch native hash differs')
            masked = nh.digest(absolute // bs, size // bs, overlays=[(absolute, original)])
            require(masked == digest(original), 'native rollback-overlay hash differs')
            record['stage'] = 'scratch-restore'
            save()
            write(absolute, original)
            nv.flush()
            require(read(absolute, size) == original, 'scratch restoration differs')
            record['scratch_restored'] = True
            require(read(ho, 4096) == current_header, 'header changed during scratch probe')
        else:
            io = po + m['layout']['isc_offset']
            ib = m['layout']['isc_image']
            require(hash_range('isc', io, ib) == m['isc']['sha256'], 'ISC image differs')
            require(hash_range('isc-tail', io + ib, m['layout']['isc_region'] - ib) == m['isc']['zero_tail_sha256'], 'ISC tail differs')
            overlays = []
            if args.mode == 'restore':
                overlays = [(co + e['container_offset'], before[e['pack_offset']:e['pack_offset'] + e['bytes']]) for e in m['extents']]
            require(hash_range('container-before', co, cb, overlays) == m['container']['before_sha256'], 'complete container baseline differs')
            if args.mode != 'check':
                ranges = [(ho // bs, 4096 // bs)] + [((co + e['container_offset']) // bs, e['bytes'] // bs) for e in m['extents']]
                nv.enable_writes(ranges)
                record['stage'] = 'invalidate-header'
                save()
                write(ho, bytes(4096))
                nv.flush()
                require(read(ho, 4096) == bytes(4096), 'header invalidation read-back')
                data = before if args.mode == 'restore' else after
                for i, e in enumerate(m['extents']):
                    record['stage'] = f'extent-{i}'
                    save()
                    block = data[e['pack_offset']:e['pack_offset'] + e['bytes']]
                    write(co + e['container_offset'], block)
                    require(read(co + e['container_offset'], e['bytes']) == block, f'extent {i} read-back')
                nv.flush()
                wanted = m['container']['before_sha256' if args.mode == 'restore' else 'after_sha256']
                require(hash_range('container-after', co, cb) == wanted, 'complete container read-back differs; header remains invalid')
                require(hash_range('isc-after', io, ib) == m['isc']['sha256'], 'ISC changed; header remains invalid')
                require(hash_range('isc-tail-after', io + ib, m['layout']['isc_region'] - ib) == m['isc']['zero_tail_sha256'], 'ISC tail changed')
                require(edges() == original_edges, 'GPT or neighbour edge changed; header remains invalid')
                header = hbefore if args.mode == 'restore' else hafter
                record['stage'] = 'publish-header'
                save()
                write(ho, header)
                nv.flush()
                require(read(ho, 4096) == header, 'published header read-back')
                record['container_after'] = wanted
                record['header_after'] = digest(header)
        nv.disable_writes()
        require(edges() == original_edges, 'GPT or neighbour edge changed')
        record['edges_unchanged'] = True
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error = None
        try:
            if nv is not None:
                nv.disable_writes()
                try:
                    nv.delete_io_queues()
                finally:
                    nv.shutdown(record)
                record['proxy_ops'] = nv.hw.ops
        except BaseException as error:
            cleanup_error = error
            record['cleanup_error'] = str(error)
        try:
            if guard is not None:
                after_guard = proxy.serror_state()
                record['serror'] = {k: after_guard[k] for k in ('before', 'during', 'unconsumed')}
                proxy.serror_strict(bool(guard['flags'] & m3.q1n1proxy.SERROR_STRICT))
                require(not any(record['serror'].values()), 'SError counters are not zero')
        except BaseException as error:
            cleanup_error = cleanup_error or error
            record['guard_cleanup_error'] = str(error)
        finally:
            proxy.link.close()
        if cleanup_error is not None and active_error is None:
            raise cleanup_error


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--device')
    ap.add_argument('--mode', choices=['check', 'probe', 'apply', 'restore'], default='check')
    ap.add_argument('--payload', required=True, type=Path)
    ap.add_argument('--package-sha256', required=True)
    ap.add_argument('--probe-record', type=Path)
    ap.add_argument('--json', required=True, type=Path)
    args = ap.parse_args()
    require(not args.json.exists(), 'preserve existing evidence')
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.with_suffix('').mkdir()  # Refuse partial old attempts too.
    record = dict(tool='q1n1-nvme-window/1', mode=args.mode, package_sha256=args.package_sha256,
                  sources=sources(), started=datetime.datetime.now(datetime.timezone.utc).isoformat(), result='RUNNING')
    def save():
        with args.json.open('w') as f:
            json.dump(record, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
    save()
    status = 0
    try:
        run(args, record, save)
        record['result'] = 'PASS'
    except BaseException as error:
        record['result'] = 'FAIL'
        record['error'] = str(error)
        record['error_type'] = type(error).__name__
        if isinstance(error, m3.Stop):
            record['detail'] = error.detail
        status = 1
        print(f'FAILED at {record.get("stage")}: {error}', file=sys.stderr, flush=True)
    finally:
        record['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
    print(json.dumps({k: record[k] for k in ['result', 'mode', 'stage'] if k in record}), flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
