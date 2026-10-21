#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only: the native hash helper against the one it replaces, on the A16's SSD.

    python3 tools/nvme_hash_compare.py --device udp://en5 \\
        --partition-guid ffea9120-5c2e-47ff-a459-3f645a59654e \\
        --json build/nvme-hash-compare-YYYYMMDD-v1.json [--chunks 256|all] \\
        [--stream [--expect-container SHA256] [--stream-old]] [--stage-build DIR]

In order (docs/FAST-NATIVE-HASH-20260928.md):
 1. build this checkout's helper (tools/nvme_hash.py) and rebuild the replaced
    one from commit 3f2966d; refuse unless that is byte for byte the production
    helper 3ac12420...;
 2. arm the SError guard; take the SSD over read-only as nvme_m3.py and the
    Snapintosh snapshot do (locate, SMMU, DMA window, takeover, Identify, one
    I/O queue pair); find the Snapintosh partition and check its header;
 3. place the new helper with NativeHash (same stage binding and executable
    tail admission), then upload the replaced helper at the same address;
 4. replaced helper, then new helper: a 4 MiB container sample (compared with
    the host's SHA-256 of the same bytes) and the chosen 16 MiB container
    chunks, one digest per chunk as the snapshot takes them; the new helper's
    telemetry says which SHA-256 path ran and where each call's time went;
 5. --stream: the whole container as one SHA-256 with the new helper, as
    nvme_window.py's check does (--stream-old also with the replaced helper),
    compared with --expect-container when given;
 6. delete the I/O queues and shut the controller down normally.

Nothing here can write the SSD: nvme_m3.Nvme builds only allow-listed admin
commands and Reads, and both helpers build only Reads. PASS needs every
compared digest equal, zero SErrors and a normal shutdown. Use a new JSON path
for every run.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

import nvme_m3 as m3
import nvme_hash
from nvme_hash import NativeHash

ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20
CHUNK = 16 * MIB                     # the snapshot's chunk
SAMPLE = 4 * MIB                     # host-read sample, as nvme_window.py and the snapshot take
REPLACED_COMMIT = '3f2966d'
REPLACED_SHA256 = '3ac12420be5e214904ace343b1d508d9f9756528f02660fbd1e1acdfdff5dd51'
TELEMETRY = struct.Struct('<16Q')    # platform/uefi/nvme-hash.c struct telemetry
TELEMETRY_OFF = 0x400
TELEMETRY_MAGIC = 0x314D4C5448534148
FIELDS = ('magic', 'sha_path', 'isar0', 'pfr0', 'el', 'hcr', 'cptr', 'frequency', 'depth', 'commands',
          'total', 'wait', 'invalidate', 'hash', 'submit', 'status')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise m3.Refused(message)


def replaced_helper():
    """The production helper, rebuilt from its commit with this checkout's flags and link script."""
    record = json.loads((nvme_hash.BUILD / 'build.json').read_text())
    with tempfile.TemporaryDirectory(prefix='nvme-hash-replaced-') as scratch:
        src = Path(scratch) / 'platform/uefi'
        src.mkdir(parents=True)
        for name in ('nvme-hash.c', 'sha256.c', 'sha256.h'):
            (src / name).write_bytes(subprocess.check_output(
                ['git', '-C', str(ROOT), 'show', f'{REPLACED_COMMIT}:platform/uefi/{name}']))
        for name in ('sha256.c', 'sha256.h'):
            require((src / name).read_bytes() == (ROOT / 'platform/uefi' / name).read_bytes(),
                    f'{name} changed since {REPLACED_COMMIT}')
        flags = [f for f in record['flags'] if not f.startswith('-I')] + ['-I' + str(src)]
        objects = []
        for name in ('nvme-hash.c', 'sha256.c'):
            obj = Path(scratch) / (name[:-2] + '.o')
            subprocess.run([str(nvme_hash.LLVM / 'clang'), *flags, '-c', str(src / name), '-o', str(obj)], check=True)
            objects.append(str(obj))
        elf, binary = Path(scratch) / 'replaced.elf', Path(scratch) / 'replaced.bin'
        subprocess.run(['ld.lld', '-T', str(nvme_hash.BUILD / 'link.ld'), '-o', str(elf), *objects], check=True)
        subprocess.run([str(nvme_hash.LLVM / 'llvm-objcopy'), '-O', 'binary', str(elf), str(binary)], check=True)
        code = binary.read_bytes()
    require(digest(code) == REPLACED_SHA256, f'{REPLACED_COMMIT} does not rebuild to the production helper')
    return code


def upload(nh, code, size):
    """Put `code` at the helper address; zero the rest of what was there."""
    hw = nh.nvme.hw
    hw.write(nh.code, code + bytes(max(0, size - len(code))))
    hw.p.flush_code(nh.code, max(size, len(code)))
    require(hw.read(nh.code, len(code)) == code, 'helper upload differs')


def telemetry(nh):
    raw = nh.nvme.hw.read(nh.state + TELEMETRY_OFF, TELEMETRY.size)
    return dict(zip(FIELDS, TELEMETRY.unpack(raw)))


def pick_chunks(total, wanted):
    if wanted == 'all' or int(wanted) >= total:
        return list(range(total))
    n = int(wanted)
    if n <= 1:
        return [0][:n]
    return sorted({round(i * (total - 1) / (n - 1)) for i in range(n)})


def run(args, record, save):
    record['helpers'] = dict(new=nvme_hash.build())
    replaced = replaced_helper()
    record['helpers']['replaced'] = dict(commit=REPLACED_COMMIT, binary_sha256=REPLACED_SHA256, bytes=len(replaced))
    new_code = (nvme_hash.BUILD / 'nvme-hash.bin').read_bytes()
    save()
    proxy = m3.q1n1proxy.connect(args.device)
    proxy.resume_skip = False  # Stop at a native fault; never walk past it.
    guard = nv = None
    try:
        info = proxy.bootinfo()
        require(info['current_el'] == 2 and info['stage_generation'] >= 1, 'chainload the guarded q1n1 stage first')
        record['bootinfo'] = info
        status, battery = proxy.live_battery()
        record['battery'] = battery
        require(status == 0 and battery['plausible'] and (battery['ac'] or battery['percent'] >= 30),
                f'battery: {battery.get("text")}')
        guard = proxy.serror_state()
        proxy.serror_reset()
        proxy.serror_strict(True)
        hw = m3.Hw(proxy)
        bar, sid = m3.locate(hw, info, record)
        smmu = m3.Smmu(hw, sid)
        record['smmu'] = dict(mode=smmu.mode, **smmu.info)
        require(smmu.mode in ('bypass', 's2'), 'unsupported SSD SMMU mode')
        window = m3.pick_window(hw, info, smmu, record)
        nv = m3.Nvme(hw, bar, window, record)
        record['stage'] = 'takeover'
        save()
        nv.takeover(record)
        nv.identify(record)
        nv.io_queues(record)
        first, _ = m3.arm_window(nv, args.partition_guid, record)
        bs = nv.lba_bytes
        header = record['window_armed']['header']
        absolute, size = first * bs + header['container_offset'], header['container_image']
        require(not (absolute % bs or size % bs), 'container is not LBA aligned')
        chunks = [(i * CHUNK, min(CHUNK, size - i * CHUNK)) for i in pick_chunks((size + CHUNK - 1) // CHUNK, args.chunks)]
        record['container'] = dict(absolute=absolute, bytes=size, chunks=len(chunks), chunk_bytes=CHUNK)
        record['stage'] = 'place'
        save()
        nh = NativeHash(nv, args.stage_build)
        sample = nv.read(absolute // bs, SAMPLE // bs)
        record['sample'] = dict(bytes=len(sample), host_sha256=digest(sample))
        rows = record['chunks'] = [dict(offset=off, bytes=n) for off, n in chunks]
        last = [time.monotonic()]

        def report(label, done, total, started, unit='chunks'):
            if time.monotonic() - last[0] >= 10 or done == total:
                print(f'{label}: {done}/{total} {unit}, {time.monotonic() - started:.0f} s', flush=True)
                save()
                last[0] = time.monotonic()

        def pass_over(label, code, key, with_telemetry):
            record['stage'] = label
            save()
            upload(nh, code, max(len(new_code), len(replaced)))
            if not with_telemetry:
                nv.hw.write(nh.state + TELEMETRY_OFF, bytes(TELEMETRY.size))
            record['sample'][key] = nh.digest(absolute // bs, len(sample) // bs)
            require(record['sample'][key] == record['sample']['host_sha256'], f'{label} sample differs from the host')
            started = time.monotonic()
            for i, row in enumerate(rows):
                t0 = time.monotonic()
                row[key] = nh.digest((absolute + row['offset']) // bs, row['bytes'] // bs)
                row[key + '_seconds'] = round(time.monotonic() - t0, 4)
                if with_telemetry:
                    t = telemetry(nh)
                    require(t['magic'] == TELEMETRY_MAGIC and t['status'] == 0, 'new helper telemetry')
                    row['telemetry'] = {k: t[k] for k in FIELDS[1:]}
                if 'replaced' in row and 'new' in row:
                    require(row['new'] == row['replaced'], f'chunk at {row["offset"]:#x} differs between the helpers')
                report(label, i + 1, len(rows), started)
            return time.monotonic() - started

        replaced_seconds = pass_over('replaced-chunks', replaced, 'replaced', False)
        save()
        new_seconds = pass_over('new-chunks', new_code, 'new', True)
        record['chunk_passes'] = dict(replaced_seconds=round(replaced_seconds, 3), new_seconds=round(new_seconds, 3),
                                      bytes=sum(r['bytes'] for r in rows),
                                      all_equal=all(r['new'] == r['replaced'] for r in rows))
        save()
        streams = [('new', new_code)] + ([('replaced', replaced)] if args.stream_old else []) if args.stream else []
        record['stream'] = {}
        for key, code in streams:
            record['stage'] = f'stream-{key}'
            save()
            upload(nh, code, max(len(new_code), len(replaced)))
            t0 = time.monotonic()
            value = nh.digest(absolute // bs, size // bs,
                              lambda d, t, s: report(f'stream-{key}', d // MIB, t // MIB, t0, 'MiB'))
            seconds = time.monotonic() - t0
            record['stream'][key] = dict(sha256=value, seconds=round(seconds, 3), mib_per_s=round(size / seconds / MIB, 1))
            if key == 'new':
                record['stream'][key]['last_call_telemetry'] = {k: v for k, v in telemetry(nh).items() if k != 'magic'}
            save()
        if len(streams) == 2:
            require(record['stream']['new']['sha256'] == record['stream']['replaced']['sha256'],
                    'whole-container digests differ between the helpers')
        if args.expect_container:
            require(record['stream']['new']['sha256'] == args.expect_container.lower(),
                    'whole-container digest differs from --expect-container')
        record['summary'] = summarise(record, rows, size)
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error = None
        try:
            if nv is not None:
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
                after = proxy.serror_state()
                record['serror'] = {k: after[k] for k in ('before', 'during', 'unconsumed')}
                proxy.serror_strict(bool(guard['flags'] & m3.q1n1proxy.SERROR_STRICT))
                require(not any(record['serror'].values()), 'SError counters are not zero')
        except BaseException as error:
            cleanup_error = cleanup_error or error
            record['guard_cleanup_error'] = str(error)
        finally:
            proxy.link.close()
        if cleanup_error is not None and active_error is None:
            raise cleanup_error


def summarise(record, rows, container_bytes):
    """Where the new helper's time went, and the whole-container pass it implies."""
    tel = [r['telemetry'] for r in rows if 'telemetry' in r]
    out = dict(chunks=len(rows))
    if tel:
        hz = tel[0]['frequency']
        ticks = {k: sum(t[k] for t in tel) for k in ('total', 'wait', 'invalidate', 'hash', 'submit')}
        moved = sum(r['bytes'] for r in rows)
        out['new_helper'] = dict(
            sha_paths=sorted({hex(t['sha_path']) for t in tel}), isar0=hex(tel[0]['isar0']), pfr0=hex(tel[0]['pfr0']),
            el=tel[0]['el'], hcr_el2=hex(tel[0]['hcr']), cptr_el2=hex(tel[0]['cptr']), depth=tel[0]['depth'],
            seconds={k: round(v / hz, 3) for k, v in ticks.items()},
            share={k: round(v / ticks['total'], 3) for k, v in ticks.items() if k != 'total'},
            helper_mib_per_s=round(moved / (ticks['total'] / hz) / MIB, 1),
            sha256_mib_per_s=round(moved / (ticks['hash'] / hz) / MIB, 1) if ticks['hash'] else None)
    for key in ('replaced', 'new'):
        seconds = [r[key + '_seconds'] for r in rows if key + '_seconds' in r and r['bytes'] == CHUNK]
        if seconds:
            mean = sum(seconds) / len(seconds)
            out[key] = dict(mean_chunk_seconds=round(mean, 4), mib_per_s=round(16 / mean, 1),
                            projected_container_pass_seconds=round(mean * container_bytes / CHUNK, 1))
    if 'replaced' in out and 'new' in out:
        out['speedup'] = round(out['replaced']['mean_chunk_seconds'] / out['new']['mean_chunk_seconds'], 2)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--device', required=True)
    ap.add_argument('--partition-guid', required=True)
    ap.add_argument('--json', required=True, type=Path)
    ap.add_argument('--chunks', default='256', help="16 MiB container chunks to compare, spread evenly, or 'all'")
    ap.add_argument('--stream', action='store_true', help='also hash the whole container once with the new helper')
    ap.add_argument('--stream-old', action='store_true', help='with --stream: and once with the replaced helper')
    ap.add_argument('--expect-container', help='with --stream: the container SHA-256 the SSD should hold')
    ap.add_argument('--stage-build', action='append', default=[], type=Path,
                    help='another make-uefi output directory the running stage may come from')
    args = ap.parse_args()
    if (args.stream_old or args.expect_container) and not args.stream:
        ap.error('--stream-old and --expect-container need --stream')
    if args.chunks != 'all' and not args.chunks.isdigit():
        ap.error("--chunks takes a count or 'all'")
    require(not args.json.exists(), 'preserve existing evidence')
    args.json.parent.mkdir(parents=True, exist_ok=True)
    record = dict(tool='q1n1-nvme-hash-compare/1', started=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  device=args.device, partition_guid=args.partition_guid.lower(), chunks_requested=args.chunks,
                  stream=args.stream, stream_old=args.stream_old, expect_container=args.expect_container,
                  sources={n: digest((ROOT / n).read_bytes()) for n in (
                      'tools/nvme_m3.py', 'tools/nvme_hash.py', 'tools/nvme_hash_compare.py', 'tools/q1n1proxy.py',
                      'platform/uefi/nvme-hash.c', 'platform/uefi/sha256.c', 'platform/uefi/sha256.h')},
                  result='RUNNING')

    def save():
        with args.json.open('w') as f:
            json.dump(record, f, indent=1)
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
    print(json.dumps({k: record.get(k) for k in ('result', 'stage', 'summary', 'stream')}, indent=1), flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
