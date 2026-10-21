#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for Stage 4's hand-off: tools/a16gpu_adopt.py (`a16gpu.py adopt`).

Runs against test-a16gpu-p6p7.py's FakeP67 (the Stage 3 model plus the executing CP): after the proven
sequence (gmu-smmu .. cp-fence) `adopt` must succeed read-only and describe exactly the state the
Snapintosh monitor adopts; with any Stage 3 gate broken it must refuse before writing a record.

  python3 tools/test-a16gpu-adopt.py
"""
from __future__ import annotations

import contextlib
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
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_adopt as A  # noqa: E402

_spec = importlib.util.spec_from_file_location('p67_test', TOOLS / 'test-a16gpu-p6p7.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
T3 = P.T3
checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(label)


def adopt(fake, record=None):
    return S3.run(fake, 'adopt', S3.Args(record=record))


def writes(fake, since):
    return [e for e in fake.log[since:] if e[0] in ('w', 'mw', 'memset', 'store32', 'call')]


def test_layout():
    pool_off, pool_size = R.ARENA_LAYOUT['snap_smmu']
    check(pool_size >= (3 + (3 << 30 >> 21)) * 0x1000, 'the pool holds 3 L2 + 1536 L3 tables (ADRENO_POOL_BYTES)')
    check(pool_off % 0x1000 == 0 and pool_off + pool_size <= R.ARENA_SIZE, 'the pool is page aligned inside the arena')
    spans = sorted(R.ARENA_LAYOUT.values())
    check(all(a[0] + a[1] <= b[0] for a, b in zip(spans, spans[1:])), 'no arena region overlaps another')
    check(R.MONITOR_FENCE_OFF % 64 == 0 and R.MONITOR_FENCE_OFF >= R.CP_IB_FENCE_OFF + 8 and
          R.MONITOR_FENCE_OFF + 4 <= R.ARENA_LAYOUT['cp_memptrs'][1],
          'the monitor fence has its own 64-byte line in memptrs, after every q1n1 fence slot')
    check(R.CP_RB_VA < 1 << 30 and R.CP_MEMPTRS_VA < 1 << 30 and R.P67_VA < 1 << 30 and
          R.FB_VA + R.FB_VA_LIMIT <= 1 << 30, 'every q1n1 CB0 mapping sits in root entry 0 (VA < 1 GiB)')


def test_adopt():
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = P.FakeP67()
        P.ready(fake, fw)
        since = len(fake.log)
        path = str(Path(tmp) / 'adopt.json')
        status, out = adopt(fake, path)
        check(status == 0 and out['order'][-1] == 'adopt_ready', f'adopt after cp-fence ({out["stopped"]})')
        check(not writes(fake, since), f'adopt writes nothing: {writes(fake, since)[:4]}')
        record = json.loads(Path(path).read_text())
        host = T3.host_record(fake)
        base = fake.gpu_rec['arena_base']
        tables = S3.build_tables(base, 'gpu')
        check(record['schema'] == A.SCHEMA and record['ready'] is True, 'schema and verdict')
        check(record['arena'] == {'base': base, 'size': R.ARENA_SIZE}, 'arena from gpu_state')
        check(record['cb0']['ttbr0'] == S3.ttbr_for(tables, R.GPU_CB) and record['cb0']['tcr'] == R.CB_TCR and
              (record['cb0']['sctlr'] ^ R.CB_SCTLR) & ~R.SCTLR_ASIDPNE == 0, 'CB0 as cp-fence programmed it')
        check(record['ring'] == {'pa': base + R.ARENA_LAYOUT['cp_rb'][0], 'va': R.CP_RB_VA, 'dwords': 8192,
                                 'wptr': host['rb_wptr']}, 'the ring and its idle WPTR')
        check(record['fence']['pa'] - record['memptrs']['pa'] == 0x80 == record['fence']['va'] - R.CP_MEMPTRS_VA,
              'the monitor fence slot')
        check(record['pool']['pa'] == base + 0x1400000 and record['pool']['size'] == 0x603000, 'the mirror pool')
        check(record['tz_init_done'] is True and record['gicd'] == 0x17000000 and record['stage_generation'] == 1,
              'TrustZone init made this boot; GIC and stage from bootinfo')
        check(record['cx_ahb_cntl'] == fake.cx_ahb[0] and record['cx_ahb_cntl'],
              'review: the armed CX AHB timeout value is recorded')
        # The CP still takes q1n1's own work afterwards (nothing was changed).
        status, out = S3.run(fake, 'blit', S3.Args())
        check(status == 0, f'a blit still runs after adopt ({out["stopped"]})')


def test_refusals():
    with T3.synthetic_firmware() as fw:
        fake = P.FakeP67()
        T3.happy_path(fake, fw, upto='oob')
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD2' and 'cp-fence' in out['stopped']['reason'],
              'no CP yet: refused at AD2')
        fake = P.FakeP67()
        T3.happy_path(fake, fw, upto='hfi')
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD1', 'no OOB GPU_SET: refused at AD1')
        fake = P.FakeP67()
        P.ready(fake, fw)
        fake.rptr += 4
        status, out = adopt(fake)
        check(status == 2 and 'disagree' in out['stopped']['reason'], 'ring pointers that disagree')
        fake = P.FakeP67()
        P.ready(fake, fw)
        T3.host_record_set(fake, rb_wptr=8192 - 100)
        fake.rptr = fake.gx[R.REGS['CP_RB_WPTR'].addr] = 8192 - 100
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD2' and 'wrap' in out['stopped']['reason'],
              'no room for the monitor fences')
        fake = P.FakeP67()
        P.ready(fake, fw)
        T3.host_record_set(fake, fb_mapped=0x12345000)
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD3' and 'framebuffer' in out['stopped']['reason'],
              'framebuffer rows left mapped')
        fake = P.FakeP67()
        P.ready(fake, fw)
        fake.rbbm_gbif_halt = 1
        fake.gx[R.REGS['RBBM_GBIF_HALT'].addr] = 1
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD2' and 'halted' in out['stopped']['reason'],
              'review: a GX-side GBIF halt left by a monitor quiesce')
        fake = P.FakeP67()
        P.ready(fake, fw)
        fake.cx_ahb[2] = 0
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD2' and 'AHB' in out['stopped']['reason'],
              'review: a CX AHB timeout port not armed')
        fake = P.FakeP67()
        P.ready(fake, fw)
        root = S3.build_tables(fake.gpu_rec['arena_base'], 'gpu').root
        fake.dev_write(root + 8, struct.pack('<Q', 0x9000_0003))
        status, out = adopt(fake)
        check(status == 2 and out['stopped']['step'] == 'AD3' and 'root entries' in out['stopped']['reason'],
              'CB0 root entries 1..3 in use (a monitor that never quiesced)')


def cp_fence(fake, fw, **kw):
    return S3.run(fake, 'cp-fence', S3.Args(fw_dir=fw, **kw))


def test_va_wide():
    """--va-wide (Snapintosh ADRENO_VA_WIDE; agents/gpu-va-wide FAILURE-MODES.md V1-V3, V13): CB0, and only CB0,
    gets T0SZ 30; adopt records it, reads 16 root entries, and both halves refuse a mismatched other half."""
    check(R.CB_TCR_WIDE == 0x80201E and R.CB_TCR == 0x802020 and R.CB_TCR_WIDE ^ R.CB_TCR == 0x3E,
          'the wide TCR differs from CB_TCR in T0SZ only')
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = P.FakeP67()
        T3.happy_path(fake, fw, upto='oob')
        status, out = cp_fence(fake, fw, va_wide=True)
        check(status == 0 and int(out['va_wide']['idr2'], 16) == 0x5522, f'cp-fence --va-wide ({out["stopped"]})')
        check(fake.cb[R.GPU_CB][0x30] == R.CB_TCR_WIDE and fake.cb[R.GMU_CB][0x30] == R.CB_TCR,
              'CB0 T0SZ 30, CB1 (GMU) still T0SZ 32')
        P.no_violation(fake, 'wide cp-fence')
        status, out = cp_fence(fake, fw, va_wide=True)
        check(status == 0, f'a second cp-fence --va-wide resumes on the wide CB0 ({out["stopped"]})')
        status, out = adopt(fake)
        check(status == 2 and 'context bank 0' in out['stopped']['reason'], 'the default adopt refuses a wide CB0')
        since = len(fake.log)
        path = str(Path(tmp) / 'wide.json')
        status, out = S3.run(fake, 'adopt', S3.Args(record=path, va_wide=True))
        check(status == 0 and not writes(fake, since), f'adopt --va-wide, read-only ({out["stopped"]})')
        record = json.loads(Path(path).read_text())
        check(record['cb0']['tcr'] == R.CB_TCR_WIDE and record['pool']['size'] == 0x603000,
              'the record carries the wide TCR (the arena pool stays named, unused by the wide monitor)')
        # The model walks T0SZ 30: a mirror entry at root 5 (GPU VA 5 GiB) reaches DRAM; T0SZ 32 cannot reach it.
        base = fake.gpu_rec['arena_base']
        root = S3.build_tables(base, 'gpu').root
        l2, l3, page = base + 0x1400000, base + 0x1401000, base + 0x1402000
        fake.dev_write(l3, struct.pack('<Q', page | S3.UNPRIV_RW | R.PTE_TYPE_PAGE))
        fake.dev_write(l2, struct.pack('<Q', l3 | R.PTE_TYPE_TABLE))
        fake.dev_write(root + 8 * 5, struct.pack('<Q', l2 | R.PTE_TYPE_TABLE))
        check(fake.translate(R.GPU_SID, 0x1_4000_0010, quiet=True) == page + 0x10, 'GPU VA 5 GiB translates')
        check(fake.translate(R.GPU_SID, 0x4_0000_0000, quiet=True) is None, '16 GiB is past T0SZ 30')
        status, out = S3.run(fake, 'adopt', S3.Args(va_wide=True))
        check(status == 2 and 'root entries 1..15' in out['stopped']['reason'], 'a live root entry 5 is refused')
        fake.cb[R.GPU_CB][0x30] = R.CB_TCR
        check(fake.translate(R.GPU_SID, 0x1_4000_0010, quiet=True) is None, 'T0SZ 32 never reaches 5 GiB')
        # A narrow CB0 with the wide adopt, and a wide resume on a narrow CB0, are refused.
        fake = P.FakeP67()
        P.ready(fake, fw)
        status, out = S3.run(fake, 'adopt', S3.Args(va_wide=True))
        check(status == 2 and out['stopped']['step'] == 'AD2' and '--va-wide' in out['stopped']['reason'],
              'adopt --va-wide refuses the default CB0')
        status, out = cp_fence(fake, fw, va_wide=True)
        check(status == 2 and out['stopped']['step'] == 'C0' and 'not the --va-wide' in out['stopped']['reason'],
              'cp-fence --va-wide refuses a CP this GMU boot started with the default CB0')
        # UBS 32 bits: refused before anything is written.
        fake = P.FakeP67(idr2=0x5022)
        T3.happy_path(fake, fw, upto='oob')
        since = len(fake.log)
        status, out = cp_fence(fake, fw, va_wide=True)
        check(status == 2 and out['stopped']['step'] == 'C0' and 'UBS' in out['stopped']['reason'] and
              not writes(fake, since), 'IDR2.UBS 32 bits: --va-wide refused before any write')
        status, out = cp_fence(fake, fw)
        check(status == 0, f'the default cp-fence never reads IDR2 ({out["stopped"]})')


def test_cli():
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = P.FakeP67()
        P.ready(fake, fw)
        real = P.Q.connect
        P.Q.connect = lambda device=None, wait=0.0, **_: fake
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rec, log = Path(tmp) / 'r.json', Path(tmp) / 'o.json'
                status = a16gpu.main(['--out', str(log), 'adopt', '--record', str(rec)])
                check(status == 0 and json.loads(rec.read_text())['schema'] == A.SCHEMA and
                      json.loads(log.read_text())['adopt']['record'] == str(rec), 'CLI adopt writes both files')
        finally:
            P.Q.connect = real


def main():
    for test in (test_layout, test_adopt, test_refusals, test_va_wide, test_cli):
        test()
    print(f'PASS: {checks} a16gpu adopt model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
