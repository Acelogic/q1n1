#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Adreno X2-90: replay a captured command bundle on the CP `cp-fence` started -- no macOS, no monitor.

  python3 tools/a16gpu_replay.py --bundle DIR --dry-run           validate and plan; opens nothing, writes nothing
  python3 tools/a16gpu_replay.py --bundle DIR --model             the same command against q1n1's CP/SMMU model
  python3 tools/a16gpu_replay.py --bundle DIR --write [--device D] the A16, after `a16gpu.py cp-fence --write`
  python3 tools/a16gpu_replay.py --self-test [--bundle DIR]       model runs: the bundle, then every refusal

What it runs (Snapintosh design S2/S5, plan R3): an IB and the {IOVA, bytes} it references, all inside q1n1's
render window (a16gpu_render: r_misc, r_code, r_data, r_tex read-only to the GPU, r_rt writable; the IB goes into
r_ib), through the same steps `render` takes: RP0 the stage/arena/GMU/GX/idle-CP gates (p67_prologue: the CP must
be the idle one cp-fence left for this GMU boot, and q1n1 never wraps the ring), RP1 the render window on CB0
(tables written, TLB invalidated, read back), r_misc/r_code/r_data zeroed, every upload and readback seed written
and read back, RP2 every declared reference walked through CB0 as read back from DRAM (both ends, writable where
the GPU writes), the IB into r_ib, q1n1's a7xx ring words (the 35 of every hardware-proven P7/render draw; the
ones Snapintosh's monitor emits for ADRENO_GUEST_IB), the bounded fence poll, BV fence, idle, CB0 FSR and the
sticky error bits, RP3 every readback region read (saved raw next to the JSON), compared with its seed.

Refused before anything is written (exit 6 for the tool's own rules): an IOVA outside the window, an upload into
r_ib or memptrs, overlapping uploads/readbacks, a GPU-written reference in a read-only region (a readback there
is allowed: it is seeded and compared, never written by the GPU), an IB larger than r_ib, and any IB dword that looks like a render-window address no declared reference covers.

The IB is replayed as captured with one exception: a trailing IB fence (the CACHE_FLUSH_TS | WRITE_ENABLED into
memptrs + CP_IB_FENCE_OFF that q1n1's builders end every batch with) gets this run's fence value, so a stale value
from an earlier run cannot pass for completion; both IB hashes are recorded. A bundle without one completes on
the ring and BV fences plus idle.

Bundle: DIR/bundle.json (format snapintosh-adreno-replay-v1):
  {"format": ..., "ib": {"file": "ib.bin"},
   "uploads":    [{"va": IOVA, "file": "x.bin", "what": "..."}],             bytes the IB reads
   "references": [{"va": IOVA, "size": N, "write": bool, "what": "..."}],    every address the IB/descriptors carry
   "readback":   [{"va": IOVA, "size": N, "seed_file": "y.bin" (optional), "what": "..."}]}
or a draw-plan directory of Snapintosh tools/plan-adreno-native-triangle.py (draw.ib.bin, scene.json,
references.json, upload-*.bin, target.before.bin), converted in place of a bundle.json.

After an A16 replay the render window stays mapped on CB0 (as after `render`); Snapintosh's `a16gpu.py adopt`
must still find the CP idle at its record's WPTR: run the replay before `adopt`, or `quiesce --write` and a fresh
cp-fence before a guest launch.

Exit status: that of a16gpu_stage3.run (0 done, 2 a stop criterion, 3 SError, 5 link, 6 refused), and for
--dry-run 0 valid / 6 refused.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a8xx_regs as X  # noqa: E402
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_p6p7 as M  # noqa: E402
import a16gpu_render as RD  # noqa: E402

Stop, Refused, hx = S3.Stop, S3.Refused, S3.hx
FORMAT = 'snapintosh-adreno-replay-v1'
PAINT = bytes((0x42, 0xEE, 0xFF, 0xC0))          # what the model's draw writes: not a pixel value
WINDOW_REGIONS = [name for name, _attrs in RD.RENDER_REGIONS]
WRITABLE = {name: not attrs & R.PTE_AP_RDONLY for name, attrs in RD.RENDER_REGIONS}
IB_MAX_DWORDS = R.ARENA_LAYOUT['r_ib'][1] // 4


def sha(data):
    return hashlib.sha256(data).hexdigest()


def region_of(va, size):
    """(region, offset) of [va, va + size) inside one render-window region, or None."""
    for name in WINDOW_REGIONS:
        base, length = RD.region_va(name), R.ARENA_LAYOUT[name][1]
        if base <= va and size > 0 and va + size <= base + length:
            return name, va - base
    return None


def in_memptrs(va, size):
    return R.CP_MEMPTRS_VA <= va and size > 0 and va + size <= R.CP_MEMPTRS_VA + R.ARENA_LAYOUT['cp_memptrs'][1]


# ============================================================================================================
# Bundles: load, convert a draw plan, validate (pure; the same for --dry-run, --model and --write)
# ============================================================================================================
def convert_draw_plan(d):
    scene = json.loads((d / 'scene.json').read_text())
    refs = json.loads((d / 'references.json').read_text())
    uploads = [{'va': RD.region_va(u['region'], u['offset']), 'file': u['file'], 'what': u['label']}
               for u in scene['uploads']]
    seed = d / 'target.before.bin'
    readback = [{'va': RD.region_va('r_rt', 0), 'size': seed.stat().st_size, 'seed_file': seed.name,
                 'what': 'render targets with their guard bands (r_rt)'}]
    return {'format': FORMAT, 'source': 'draw-plan', 'ib': {'file': 'draw.ib.bin'}, 'uploads': uploads,
            'references': [{'va': r['va'], 'size': r['size'], 'write': r['write'], 'what': r['what']} for r in refs],
            'readback': readback}


def load_bundle(path):
    """The validated plan of a bundle directory. Refused (never partially accepted) on any rule."""
    d = Path(path)
    if (d / 'bundle.json').exists():
        spec = json.loads((d / 'bundle.json').read_text())
    elif (d / 'draw.ib.bin').exists() and (d / 'scene.json').exists():
        spec = convert_draw_plan(d)
    else:
        raise Refused(f'{d}: neither bundle.json nor a draw plan')
    if spec.get('format') != FORMAT:
        raise Refused(f'bundle format {spec.get("format")!r} is not {FORMAT}')
    ib = (d / spec['ib']['file']).read_bytes()
    if not ib or len(ib) % 4 or len(ib) // 4 > IB_MAX_DWORDS:
        raise Refused(f'IB of {len(ib)} bytes: 1..{IB_MAX_DWORDS} dwords (r_ib)')
    plan = {'dir': str(d), 'source': spec.get('source', 'bundle'), 'ib': ib, 'ib_sha256': sha(ib),
            'ib_dwords': len(ib) // 4, 'uploads': [], 'references': [], 'readback': []}
    spans = []
    for u in spec.get('uploads', []):
        data = (d / u['file']).read_bytes()
        where = region_of(u['va'], len(data))
        if where is None:
            raise Refused(f'upload {u["what"]!r}: IOVA {u["va"]:#x}+{len(data):#x} is outside the replay window')
        if where[0] == 'r_ib':
            raise Refused(f'upload {u["what"]!r}: r_ib holds the replayed IB only')
        spans.append((u['va'], u['va'] + len(data), u['what']))
        plan['uploads'].append({'va': u['va'], 'region': where[0], 'offset': where[1], 'data': data,
                                'sha256': sha(data), 'what': u['what']})
    for r in spec.get('references', []):
        if in_memptrs(r['va'], r['size']):
            plan['references'].append(dict(r, region='memptrs', offset=r['va'] - R.CP_MEMPTRS_VA))
            continue
        where = region_of(r['va'], r['size'])
        if where is None:
            raise Refused(f'reference {r["what"]!r}: IOVA {r["va"]:#x}+{r["size"]:#x} is outside the replay window')
        if r['write'] and not WRITABLE[where[0]]:
            raise Refused(f'reference {r["what"]!r}: the GPU writes it but {where[0]} is read-only to the GPU')
        plan['references'].append(dict(r, region=where[0], offset=where[1]))
    for b in spec.get('readback', []):
        where = region_of(b['va'], b['size'])
        if where is None:
            raise Refused(f'readback {b["what"]!r}: IOVA {b["va"]:#x}+{b["size"]:#x} is outside the replay window')
        seed = (d / b['seed_file']).read_bytes() if b.get('seed_file') else bytes(b['size'])
        if len(seed) != b['size']:
            raise Refused(f'readback {b["what"]!r}: the seed is {len(seed)} bytes, the region {b["size"]}')
        spans.append((b['va'], b['va'] + b['size'], b['what']))
        plan['readback'].append({'va': b['va'], 'region': where[0], 'offset': where[1], 'size': b['size'],
                                 'seed': seed, 'seed_sha256': sha(seed), 'what': b['what']})
    spans.sort()
    for (a0, a1, aw), (b0, _b1, bw) in zip(spans, spans[1:]):
        if b0 < a1:
            raise Refused(f'{aw!r} and {bw!r} overlap at {b0:#x}')
    # Every IB dword that looks like a window address must be one the bundle declares (the IB is not parsed
    # for meaning; this only keeps a stale or foreign address from reaching the GPU unwalked).
    words = struct.unpack(f'<{plan["ib_dwords"]}I', ib)
    lo, hi = RD.region_va('r_ib'), RD.region_va('r_rt') + R.ARENA_LAYOUT['r_rt'][1]
    addresses = [(i, w) for i, w in enumerate(words) if lo <= w & ~0x3F < hi]
    uncovered = [(i, hex(w)) for i, w in addresses
                 if not any(r['va'] <= (w & ~0x3) < r['va'] + r['size'] for r in plan['references'])]
    if uncovered:
        raise Refused(f'IB dwords {uncovered[:4]} look like window addresses no declared reference covers')
    plan['ib_window_addresses'] = len(addresses)
    plan['ib_fence'] = ib_fence_index(words)
    return plan


def ib_fence_index(words):
    """Index of the value dword of a trailing IB fence (q1n1 emit_ib_fence), or None."""
    if len(words) < 5:
        return None
    head, w0, lo, hi, _value = words[-5:]
    want = M.pkt_word('CP_EVENT_WRITE7_0', event='CACHE_FLUSH_TS', write_enabled=True)
    if head == R.pkt7(R.CP_EVENT_WRITE7, 4) and w0 == want and (lo | hi << 32) == R.CP_MEMPTRS_VA + R.CP_IB_FENCE_OFF:
        return len(words) - 1
    return None


def plan_json(plan):
    """What --dry-run prints and every run records: placement, hashes, the ring words (fence value 0 here)."""
    ib_va = RD.region_va('r_ib')
    fence_va, bv_va = R.CP_MEMPTRS_VA + R.CP_FENCE_OFF, R.CP_MEMPTRS_VA + R.CP_BV_FENCE_OFF
    return {'source': plan['source'], 'dir': plan['dir'], 'ib': {'va': hx(ib_va), 'dwords': plan['ib_dwords'],
            'sha256': plan['ib_sha256'], 'ib_fence_dword': plan['ib_fence'],
            'window_addresses': plan['ib_window_addresses']},
            'uploads': [{'va': hx(u['va']), 'region': u['region'], 'offset': hx(u['offset']), 'bytes': len(u['data']),
                         'sha256': u['sha256'], 'what': u['what']} for u in plan['uploads']],
            'references': [{'va': hx(r['va']), 'size': r['size'], 'write': r['write'], 'region': r['region'],
                            'what': r['what']} for r in plan['references']],
            'readback': [{'va': hx(b['va']), 'region': b['region'], 'offset': hx(b['offset']), 'bytes': b['size'],
                          'seed_sha256': b['seed_sha256'], 'what': b['what']} for b in plan['readback']],
            'ring_words': [hx(w) for w in M.ring_words(ib_va, plan['ib_dwords'], fence_va, bv_va, 0)]}


# ============================================================================================================
# The command (RP0..RP3), registered with a16gpu_stage3 so run() gives it the session, the guard and the log
# ============================================================================================================
def submit(s, out, host, win, ib, value, ib_fence):
    """render_submit for a captured IB: IB into r_ib (read back), the tables as installed, every reference
    walked, the a7xx ring words, the bounded fence poll, BV (and IB) fence, idle, faults."""
    ib_va = win.addr('r_ib', 0, len(ib), False, 'replayed IB')
    fence_va = win.addr('memptrs', R.CP_FENCE_OFF, 4, True, 'ring fence')
    bv_va = win.addr('memptrs', R.CP_BV_FENCE_OFF, 4, True, 'BV fence')
    image, table_off = RD.tables_as_installed(s, 'replay')
    refs = RD.verify_refs(s, win, image, table_off)
    at = R.ARENA_LAYOUT['r_ib'][0]
    s.mem_write(at, ib, 'replayed IB')
    if s.mem_read(at, len(ib), 'replayed IB (verify)') != ib:
        raise Stop(s.step, 'the replayed IB did not read back')
    dwords = len(ib) // 4
    ring = M.ring_words(ib_va, dwords, fence_va, bv_va, value)
    wptr = S3.ring_submit(s, host, ring, f'replay (IB {dwords} dwords, fence {value:#x})')
    S3.write_host(s, host)
    record = {'ib': {'va': hx(ib_va), 'dwords': dwords, 'sha256': sha(ib)}, 'refs_walked': refs,
              'fence': hx(value), 'wptr': wptr, 'ring_words': [hx(w) for w in ring]}
    out.setdefault('submissions', []).append(record)
    memptrs = R.ARENA_LAYOUT['cp_memptrs'][0]
    limit, seconds = S3.POLL['fence']
    deadline, seen, attempt = s.clock() + seconds, None, 0
    for attempt in range(1, limit + 1):
        seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_FENCE_OFF, 4, 'fence'))[0]
        if seen == value:
            break
        if attempt >= 2 and s.clock() > deadline:
            break
    s.polls.append({'reg': 'fence replay (DRAM)', 'reads': attempt, 'ok': seen == value})
    record['fence_read'] = hx(seen)
    record['cb0'] = S3.cb_faults(s, R.GPU_CB)[1]
    if seen != value:
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'replay: the GPU did not write the fence (read {seen:#x}, want {value:#x}): '
                           '`quiesce --write`', submission=record)
    bv_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_BV_FENCE_OFF, 4, 'BV fence'))[0]
    ib_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_IB_FENCE_OFF, 4, 'IB fence'))[0]
    record['bv_fence'], record['ib_fence'] = hx(bv_seen), hx(ib_seen)
    record['idle'] = S3.cp_idle(s, wptr)
    host['fence_value'] = value
    S3.write_host(s, host)
    fault, report = S3.cb_faults(s, R.GPU_CB)
    int0, irq, int0_bad, irq_bad = M.interrupt_faults(s)
    record['cp_interrupt_status_global'], record['rbbm_int_0_status'] = hx(irq), hx(int0)
    if fault or irq_bad or int0_bad:
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'replay: fence written but CB0 FSR {report["fsr"]} / CP_INTERRUPT_STATUS_GLOBAL {irq:#x} '
                           f'/ RBBM_INT_0_STATUS {int0:#x} report a fault', submission=record)
    if bv_seen != value or (ib_fence and ib_seen != value):
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'replay: ring fence {value:#x} written but the BV fence reads {bv_seen:#x} and the IB '
                           f'fence {ib_seen:#x}: the IB did not run to its end', submission=record)
    return record


def cmd_replay(s, out, args):
    s.step = 'RP0'
    try:
        plan = load_bundle(args.bundle)          # Refused propagates: exit 6, step RP0, nothing touched
    except (OSError, ValueError, KeyError) as problem:
        raise Stop('RP0', f'bundle: {problem!r}')
    out['replay'] = plan_json(plan)
    host = M.p67_prologue(s, out, 'RP0', 1)
    if getattr(args, 'stop_after', None) == 'RP0':
        out['stopped_after'] = 'RP0'
        return
    s.step = 'RP1'
    win = RD.Window(s.arena[0])
    RD.install_tables(s, out, 'replay window')
    host['p67_token'] = host['boot_token']
    S3.write_host(s, host)
    s.mem_fill(*R.ARENA_LAYOUT['r_misc'], 'misc page (border colours, empty descriptor sets)')
    for name in ('r_code', 'r_data'):
        s.mem_fill(*R.ARENA_LAYOUT[name], f'{name} region')
    for u in plan['uploads']:
        M.upload(s, u['region'], u['offset'], u['data'], u['what'])
    for b in plan['readback']:
        M.upload(s, b['region'], b['offset'], b['seed'], f'{b["what"]} (seed)')
    out['order'].append('replay_uploaded')
    if getattr(args, 'stop_after', None) == 'RP1':
        out['stopped_after'] = 'RP1'
        return
    s.step = 'RP2'
    for r in plan['references']:
        win.addr(r['region'], r['offset'], r['size'], r['write'], r['what'])
    value = M.next_fence(host)
    ib = bytearray(plan['ib'])
    if plan['ib_fence'] is not None:
        struct.pack_into('<I', ib, 4 * plan['ib_fence'], value)
    out['replay']['ib_replayed_sha256'] = sha(bytes(ib))
    record = submit(s, out, host, win, bytes(ib), value, plan['ib_fence'] is not None)
    out['order'].append('replay_retired')
    s.step = 'RP3'
    results = out['replay']['results'] = []
    writes = [r for r in plan['references'] if r['write'] and r['region'] != 'memptrs']
    for b in plan['readback']:
        blob = s.mem_read(R.ARENA_LAYOUT[b['region']][0] + b['offset'], b['size'], f'{b["what"]} (readback)')
        inside = outside = 0
        for i in range(0, b['size'], 4):
            if blob[i:i + 4] == b['seed'][i:i + 4]:
                continue
            va = b['va'] + i
            if any(r['va'] <= va < r['va'] + r['size'] for r in writes):
                inside += 1
            else:
                outside += 1
        result = {'what': b['what'], 'va': hx(b['va']), 'bytes': b['size'], 'sha256': sha(blob),
                  'changed_words_inside_writes': inside, 'changed_words_outside_writes': outside,
                  'painted_by_model': blob.count(PAINT) if getattr(args, 'model', False) else None}
        results.append(result)
        M.save_image(args, f'replay.{b["region"]}-{b["offset"]:x}.raw', blob, out, f'{b["region"]}+{b["offset"]:#x}')
        if outside:
            raise Stop('RP3', f'{b["what"]}: {outside} words changed outside every GPU-written reference',
                       result=result)
    out['replay']['fence'] = record['fence']
    out['order'].append('replay_read_back')


COMMANDS = {'replay': (cmd_replay, True)}
STEP_IDS = {'replay': ('RP0', 'RP1')}
S3.COMMANDS.update(COMMANDS)
S3.STEP_IDS.update(STEP_IDS)


# ============================================================================================================
# The model: q1n1's CP/SMMU model (tools/test-a16gpu-p6p7.py FakeP67) with a generic draw
# ============================================================================================================
def _load_model():
    spec = importlib.util.spec_from_file_location('p6p7_test_for_replay', TOOLS / 'test-a16gpu-p6p7.py')
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


ADDRESS_REGS = [(name, off, stride, length, any(f[4] == 'waddress' for f in fields))
                for name, (off, bits, fields, stride, length) in X.REGS.items()
                if bits == 64 and any(f[4] in ('address', 'waddress') for f in fields)]


def model_class(T67):
    class FakeReplay(T67.FakeP67):
        """FakeP67 runs the ring, the IB packets, LOAD_STATE6 and the 2D engine as the A16 would, through CB0.
        Its draw is P7's; this one is any pipeline's: every address register walks through CB0 (writable where
        the GPU writes), the code at SP_VS_BASE / SP_PS_BASE is the uploaded code, and then -- the model cannot
        execute ir3 -- it paints the window scissor into RB_MRT_BASE[0] through CB0 and the colour cache."""
        uploads = []

        def draw(self, body):
            problems = []
            for name, off, stride, length, written in ADDRESS_REGS:
                for i in range(length):
                    va = self.r64(name, i) & (~0x3 if name == 'SP_GFX_BINDLESS_BASE_DESCRIPTOR' else ~0)
                    if va and self.translate(R.GPU_SID, va, write=written, priv=False, quiet=True) is None:
                        problems.append(f'{name}[{i}] = {va:#x} does not translate')
            for stage in ('SP_VS_BASE', 'SP_PS_BASE'):
                base = self.r64(stage)
                code = next((u['data'] for u in self.uploads if u['va'] == base), None)
                if code is not None and self.gpu_read(base, len(code), f'{stage} code') != code:
                    problems.append(f'{stage}: not the uploaded code')
            if problems:
                self.violations.append(('draw', 'replay', '; '.join(problems)))
                return
            tl, br = self.r('GRAS_SC_WINDOW_SCISSOR_TL'), self.r('GRAS_SC_WINDOW_SCISSOR_BR')
            base, pitch = self.r64('RB_MRT_BASE', 0), self.r('RB_MRT_PITCH', 0) << 6
            x0, y0, x1, y1 = tl & 0x3FFF, tl >> 16 & 0x3FFF, br & 0x3FFF, br >> 16 & 0x3FFF
            for y in range(y0, y1 + 1):
                self.gpu_write(base + y * pitch + 4 * x0, PAINT * (x1 - x0 + 1), 'replay draw (model paint)')
            self.draws.append('replay')
    return FakeReplay


def run_model(bundle, image_prefix=None, stop_after=None, spoil=None):
    """The whole command against the model after cp-fence (synthetic firmware), as test-a16gpu-render.py runs
    `render`. spoil(fake) runs after cp-fence (the self-test's fault injection)."""
    T67 = _load_model()
    T3 = T67.T3
    try:
        uploads = load_bundle(bundle)['uploads']
    except Refused:
        uploads = []                             # the command itself refuses it (the self-test's cases)
    with T3.synthetic_firmware() as fw, contextlib.redirect_stdout(io.StringIO()):
        fake = model_class(T67)()
        fake.uploads = uploads
        T3.happy_path(fake, fw)
        if spoil:
            spoil(fake)
        fake.replay_mark = len(fake.log)         # everything after cp-fence is the replay's
        status, out = S3.run(fake, 'replay', S3.Args(bundle=str(bundle), image_prefix=image_prefix,
                                                      stop_after=stop_after, model=True))
    return status, out, fake


# ============================================================================================================
# Self-test: the bundle through the model, then the refusals (each before any write)
# ============================================================================================================
def self_test(bundle):
    results, failures = [], []

    def check(ok, label):
        results.append({'check': label, 'ok': bool(ok)})
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        status, out, fake = run_model(bundle, image_prefix=str(Path(tmp) / 'r'))
        rep = out.get('replay', {})
        res = (rep.get('results') or [{}])[0]
        sub = (out.get('submissions') or [{}])[0]
        check(status == 0 and out['order'][-1] == 'replay_read_back', f'model replay completes ({out.get("stopped")})')
        check(not fake.violations, f'the model saw no violation ({fake.violations[:3]})')
        check(fake.draws == ['replay'], 'one draw, validated by the model')
        check(sub.get('fence') == sub.get('fence_read') == sub.get('bv_fence') == sub.get('ib_fence'),
              'ring, BV and IB fences carry this run\'s value')
        check(res.get('changed_words_outside_writes') == 0 and res.get('changed_words_inside_writes', 0) > 0,
              'only GPU-written references changed')
        check(sub.get('ring_words') == [hx(w) for w in M.ring_words(int(sub['ib']['va'], 16), sub['ib']['dwords'],
              R.CP_MEMPTRS_VA + R.CP_FENCE_OFF, R.CP_MEMPTRS_VA + R.CP_BV_FENCE_OFF, int(sub['fence'], 16))],
              'the ring words are q1n1 ring_words')
        check(len(list(Path(tmp).glob('r.replay.*.raw'))) == len(rep.get('readback', [])), 'readbacks saved')
        check(any(e[0] in ('mw', 'memset') for e in fake.log[fake.replay_mark:]),
              'the replay writes the arena (so the refusals\' "nothing written" is not vacuous)')
        model_summary = {'status': status, 'order': out['order'], 'fence': sub.get('fence'),
                         'refs_walked': sub.get('refs_walked'), 'readback': res}
        # A fence that never lands: stop with diagnostics (exit 2), nothing read back.
        def no_progress(fake):
            fake.s3['cp_executes'] = False
        status, out, _ = run_model(bundle, spoil=no_progress)
        check(status == 2 and out['stopped']['step'] == 'RP2' and 'did not write the fence' in out['stopped']['reason'],
              f'no GPU progress stops at RP2 ({out.get("stopped", {}).get("reason", "")[:80]})')
        # Refusals: each spoils a copy of the bundle; the model's arena must see no write.
        src = Path(bundle)
        base_spec = json.loads((src / 'bundle.json').read_text()) if (src / 'bundle.json').exists() \
            else convert_draw_plan(src)
        cases = {
            'upload outside the window': lambda b: b['uploads'][0].update(va=0x13000000),
            'upload into r_ib': lambda b: b['uploads'][0].update(va=RD.region_va('r_ib')),
            'reference outside the window': lambda b: b['references'][0].update(va=0x40000000),
            'written reference in a read-only region': lambda b: b['references'][0].update(
                va=RD.region_va('r_code'), size=64, write=True),
            'readback outside the window': lambda b: b['readback'][0].update(va=0xFFFF0000),
            'overlapping upload and readback': lambda b: b['uploads'][0].update(va=b['readback'][0]['va']),
            'undeclared window address in the IB': lambda b: b['references'].pop(
                next(i for i, r in enumerate(b['references']) if 'render target' in r['what'])),
        }
        for label, spoil in cases.items():
            spec = json.loads(json.dumps(base_spec))
            spoil(spec)
            d = Path(tmp) / label.replace(' ', '-')
            d.mkdir()
            for f in src.iterdir():
                if f.is_file() and f.suffix == '.bin':
                    (d / f.name).write_bytes(f.read_bytes())
            (d / 'bundle.json').write_text(json.dumps(spec))
            try:
                load_bundle(d)
                check(False, f'refused: {label}')
            except Refused as refusal:
                check(True, f'refused: {label} ({str(refusal)[:60]})')
            status, out, fake = run_model(d)
            wrote = [e for e in fake.log[fake.replay_mark:] if e[0] in ('mw', 'memset')]
            check(status == 6 and out['stopped']['step'] == 'RP0' and 'refused' in out['stopped']['reason'],
                  f'{label}: the command refuses at RP0 ({out.get("stopped")})')
            check(not wrote and not out.get('submissions') and not fake.draws,
                  f'{label}: nothing written to the arena, nothing submitted ({wrote[:2]})')
    return {'bundle': str(bundle), 'checks': len(results), 'failures': failures, 'model': model_summary,
            'results': results}


# ============================================================================================================
# CLI
# ============================================================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='validate and plan the bundle; no proxy')
    mode.add_argument('--model', action='store_true', help='run against q1n1\'s CP/SMMU model')
    mode.add_argument('--write', action='store_true', help='the A16 (after cp-fence): writes the arena, submits')
    mode.add_argument('--self-test', action='store_true', help='model runs of the bundle and every refusal')
    ap.add_argument('--bundle', type=Path, help='bundle directory (bundle.json or a draw plan)')
    ap.add_argument('--device', help='tty, UNIX socket or udp://enN (default: discover)')
    ap.add_argument('--wait', type=float, default=0.0)
    ap.add_argument('--stop-after', choices=STEP_IDS['replay'])
    ap.add_argument('--out', type=Path, help='JSON output (default build/a16-gpu/replay-<mode>-<time>.json)')
    args = ap.parse_args(argv)
    if not args.bundle:
        ap.error('--bundle is required')
    stamp = f'{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}'
    kind = 'dry-run' if args.dry_run else 'model' if args.model else 'self-test' if args.self_test else 'a16'
    path = args.out or (TOOLS.parent / 'build' / 'a16-gpu' / f'replay-{kind}-{stamp}.json')
    if args.dry_run:
        try:
            result, status = {'tool': 'a16gpu_replay', 'mode': 'dry-run', 'plan': plan_json(load_bundle(args.bundle))}, 0
        except Refused as refusal:
            result, status = {'tool': 'a16gpu_replay', 'mode': 'dry-run', 'refused': str(refusal)}, 6
    elif args.self_test:
        result = dict(self_test(args.bundle), tool='a16gpu_replay', mode='self-test')
        status = 1 if result['failures'] else 0
    elif args.model:
        status, out, fake = run_model(args.bundle, image_prefix=str(path.with_suffix('')), stop_after=args.stop_after)
        result = dict(out, tool='a16gpu_replay', mode='model', model_violations=fake.violations[:8])
        status = status or (1 if fake.violations else 0)
    else:
        proxy = a16gpu.q1n1proxy.connect(args.device, wait=args.wait)
        status, out = S3.run(proxy, 'replay', S3.Args(bundle=str(args.bundle), image_prefix=str(path.with_suffix('')),
                                                      stop_after=args.stop_after, model=False))
        result = dict(out, tool='a16gpu_replay', mode='a16', endpoint=getattr(proxy, 'endpoint', None))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str) + '\n')
    brief = {k: result[k] for k in ('mode', 'order', 'stopped', 'stopped_after', 'refused', 'failures', 'checks')
             if k in result}
    print(json.dumps(brief, indent=2, default=str))
    print(f'wrote {path}', file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.modules.setdefault('a16gpu_replay', sys.modules['__main__'])
    sys.exit(main())
