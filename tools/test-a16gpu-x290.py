#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for the X2-90 identity survey and `cp-fence --x290`: tools/a16gpu_x290.py
(Snapintosh agents/gpu-x290 FAILURE-MODES.md X1-X11). Written before the module.

Runs against test-a16gpu-p6p7.py's FakeP67 (the Stage 3 model with the executing CP) after the proven sequence
(gmu-smmu .. cp-fence). Nothing here touches hardware.

  python3 tools/test-a16gpu-x290.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_x290 as X  # noqa: E402

_spec = importlib.util.spec_from_file_location('p67_test', TOOLS / 'test-a16gpu-p6p7.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
T3 = P.T3
checks = 0
WRITES = ('w', 'mw', 'memset', 'store32', 'call')
GPU_BLOCK = R.block_of(R.REGS['RBBM_STATUS'].addr).name


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(label)


def run(fake, command, **kw):
    return S3.run(fake, command, S3.Args(**kw))


def path_to(fake, fw, x290=False, upto='cp-fence', cp_fence_kw=None):
    """The happy path, with cp-fence given --x290 (or other cp-fence arguments)."""
    for command in ('gmu-smmu', 'gmu-boot', 'hfi', 'oob', 'cp-fence')[:('gmu-smmu', 'gmu-boot', 'hfi', 'oob',
                                                                        'cp-fence').index(upto) + 1]:
        kw = dict(fw_dir=fw)
        if command == 'cp-fence':
            kw.update(x290=x290, **(cp_fence_kw or {}))
        status, out = run(fake, command, **kw)
        check(status == 0, f'{command} completes ({out["stopped"]})')
    return out


def writes_since(fake, since):
    return [e for e in fake.log[since:] if e[0] in WRITES]


def gx_accesses(fake, since, ops=('r', 'w')):
    return [(op, a) for op, a, _v in fake.log[since:] if op in ops and (R.block_of(a) or R.BLOCKS[0]).name == GPU_BLOCK]


def reg_name(addr):
    for name, reg in R.REGS.items():
        for i in range(reg.count):
            if reg.at(i) == addr:
                return name if reg.count == 1 else f'{name}[{i}]'
    return hex(addr)


# --- X2: GMEM size and protect; X7: the UBWC block's values (recomputed here from Linux's formulas) -------------
def linux_ubwc(version, flags, hbb):
    """a8xx_set_ubwc_config (linux-next a8xx_gpu.c) for one qcom_ubwc_cfg_data, written out again."""
    swizzle = 0 if version == 0 else 7 if version == 0x10000000 else 6 & ~((flags & 1) << 1) & ~((flags & 2) << 1)
    l2, l3 = int(not swizzle & 2), int(not swizzle & 4)
    amsbc, ubwc_mode, min_acc = int(version >= 0x30000000), int(version == 0x10000000), 0
    yuv = int(version in (0x60000000, 0x70000000))
    rgba_lossless = int(0x40000000 <= version < 0x50000000)
    fp16 = int(version < 0x40030000)
    rgb565 = int(version >= 0x40000000)
    mode = 5 if version >= 0x60000000 else 4 if version >= 0x50000000 else 3 if version >= 0x40030000 else \
        2 if version >= 0x40000000 else 1 if version >= 0x30000000 else 0
    h = hbb - 13
    hi, lo = h >> 2, h & 3
    return {'GRAS_NC_MODE_CNTL': h << 5 | l3 << 4 | l2 << 3,
            'RB_CCU_NC_MODE_CNTL': yuv << 6 | l3 << 5 | l2 << 4 | hi << 3 | lo << 1,
            'RB_CMP_NC_MODE_CNTL': mode << 15 | yuv << 6 | rgba_lossless << 4 | fp16 << 3 | rgb565 << 2 | amsbc << 1 |
            min_acc,
            'SP_NC_MODE_CNTL': l3 << 13 | l2 << 12 | hi << 10 | 2 << 4 | min_acc << 3 | lo << 1 | ubwc_mode,
            'TPL1_NC_MODE_CNTL': l3 << 7 | l2 << 6 | hi << 4 | min_acc << 3 | lo << 1 | ubwc_mode}


def test_constants():
    check(R.X290_CHIP_ID == 0x44070041 == R.runtime_chip_id(0xF), 'X2-90 = 0x44070001 | 4 slices << 4')
    check(R.X285_CHIP_ID == 0x44070031 == R.runtime_chip_id(0x7), 'X2-85 = 0x44070001 | 3 slices << 4')
    check(R.x2_gmem_bytes(0xF) == 21 << 20 and R.x2_gmem_bytes(0x7) == (15 << 20) + (768 << 10),
          'GMEM 5.25 MiB per active slice (Windows GmemSizePerSlice 0x540000; linux-next a8xx_gpu.c:116)')
    check(R.x2_gmem_bytes(0x1F) == 21 << 20, 'only the four slice bits count (GENMASK(3, 0))')
    # The catalog's RB_GC_GMEM_PROTECT values follow gmem << 4 (a8xx_catalog.c a830/a840/x285/a810).
    for gmem, protect in ((12 << 20, 0x0C000000), (18 << 20, 0x12000000), (21 << 20, 0x15000000),
                          ((512 + 64) << 10, 0x00900000)):
        check(R.gmem_protect_for(gmem) == protect, f'GMEM_PROTECT({gmem:#x}) == {protect:#x}')
    check(R.gmem_protect_for(R.x2_gmem_bytes(0xF)) == S3.x285_gmem_protect() == 0x15000000,
          'X2-90: the computed protect is the catalog (x285_nonctxt) value q1n1 already writes')
    check(R.gmem_protect_for(R.x2_gmem_bytes(0x7)) == 0x0FC00000, 'X2-85 would need 0x0fc00000')
    want = linux_ubwc(0x50000000, 0b11, 16)
    check(R.ubwc_nc_mode() == want, f'glymur UBWC NC_MODE values {R.ubwc_nc_mode()} == {want}')
    check(want == {'GRAS_NC_MODE_CNTL': 0x78, 'RB_CCU_NC_MODE_CNTL': 0x36, 'RB_CMP_NC_MODE_CNTL': 0x20006,
                   'SP_NC_MODE_CNTL': 0x3026, 'TPL1_NC_MODE_CNTL': 0xC6}, 'the values in FAILURE-MODES X7')
    for name in ('GRAS_NC_MODE_CNTL', 'RB_CCU_NC_MODE_CNTL', 'RB_CMP_NC_MODE_CNTL', 'SP_NC_MODE_CNTL',
                 'TPL1_NC_MODE_CNTL', 'RB_GC_GMEM_PROTECT', 'RB_CCU_CNTL', 'RB_CCU_CACHE_CNTL', 'RBBM_CLOCK_CNTL_GLOBAL',
                 'RBBM_CGC_0_PC', 'GMU_AO_GMU_CGC_MODE_CNTL', 'CP_APERTURE_CNTL_HOST', 'RB_UNSLICE_STATUS'):
        check(name in R.REGS, f'{name} is a named register')
    xml = {'GRAS_NC_MODE_CNTL': 0x8700, 'RB_CCU_NC_MODE_CNTL': 0x8E08, 'RB_CMP_NC_MODE_CNTL': 0x8F00,
           'SP_NC_MODE_CNTL': 0xAE02, 'TPL1_NC_MODE_CNTL': 0xB604, 'RB_CCU_CNTL': 0x8E07, 'RB_CCU_CACHE_CNTL': 0x88E5,
           'RB_LPAC_GMEM_PROTECT': 0x8E0A, 'RB_UNSLICE_STATUS': 0x8F03, 'RBBM_CLOCK_CNTL_GLOBAL': 0x09A,
           'RBBM_CGC_GLOBAL_LOAD_CMD': 0x09B, 'RBBM_CGC_P2S_STATUS': 0x09F, 'RBBM_CGC_0_PC': 0x10B,
           'GMU_AO_GMU_CGC_MODE_CNTL': 0x23B09}
    for name, dword in xml.items():
        check(R.REGS[name].addr == R.GPU_BASE + 4 * dword, f'{name} at a6xx.xml dword {dword:#x}')
    check(R.aperture_slice(R.PIPE_BR, 2) == (1 << 12) | (2 << 16) | (1 << 23), 'aperture with USESLICEID (bit 23)')
    check('x290-survey' in S3.COMMANDS and S3.COMMANDS['x290-survey'][1] is False,
          'x290-survey is registered read-only (the Session refuses every write: X3)')
    check('x290-slices' in S3.COMMANDS and S3.COMMANDS['x290-slices'][1] is True, 'x290-slices needs --write')


# --- X3/X4/X5/X10: the read-only survey ---------------------------------------------------------------------
def test_survey_default():
    with T3.synthetic_firmware() as fw:
        fake = P.FakeP67()
        path_to(fake, fw)
        since = len(fake.log)
        status, out = run(fake, 'x290-survey')
        check(status == 0, f'survey completes ({out["stopped"]})')
        check(not writes_since(fake, since), f'X3: the survey wrote nothing ({writes_since(fake, since)[:4]})')
        P.no_violation(fake, 'survey')
        x = out['x290']
        check(x['schema'] == X.SCHEMA and x['identity']['slice_mask'] == '0x0000000f' and
              x['identity']['slices'] == 4 and x['identity']['chip_id'] == '0x44070041', 'identity: 4 slices, X2-90')
        check(x['identity']['tz_init_done'] is True, 'the mask was read after this boot\'s TZ GPU init')
        check(x['identity']['ccu_count'] == 8 and x['identity']['ccu_count_source'].startswith('inferred'),
              'X10: the CCU count is reported as inferred')
        check(x['identity']['gmem_bytes'] == hex(21 << 20) and x['identity']['gmem_protect_expected'] == '0x15000000',
              'GMEM and the protect value for 4 slices')
        check(x['verdict']['part'] == 'X2-90' and x['verdict']['x290_list_ready'] is True,
              f'verdict X2-90 ({x["verdict"]})')
        check(x['aperture']['read'] == S3.hx(R.aperture(R.PIPE_NONE)) and x['aperture']['pipe_registers'] == 'not read',
              'X5: the aperture is recorded; no pipe register is read through it')
        check(x['protect']['matches_x285'] is True, 'CP_PROTECT_GLOBAL is the x285 list cp-fence wrote')
        nonctxt = x['nonctxt']
        check(nonctxt and all(row['match'] for row in nonctxt.values()),
              f'every PIPE_NONE nonctxt register reads what hw_init wrote ({nonctxt})')
        check(x['ubwc']['programmed'] is False, 'default: the UBWC block was not written (reset values)')
        check(x['hwcg']['enabled'] is False, 'HW clock gating reads off (q1n1 never enables it)')
        check(x['gmem_range']['UCHE_CCHE_GC_GMEM_RANGE_MIN'] == hex(R.GMEM_RANGE_MIN), 'the GMEM VA range')
        # nothing before the GX proof touches GX: the first GX access comes after GMU_PWR_CLK_STATUS
        first_gx = next(i for i, (op, a, _v) in enumerate(fake.log[since:]) if op == 'r' and
                        (R.block_of(a) or R.BLOCKS[0]).name == GPU_BLOCK)
        pwr = R.REGS['GMU_PWR_CLK_STATUS'].addr
        check(any(op == 'r' and a == pwr for op, a, _v in fake.log[since:since + first_gx]),
              'X4: GX is proven on before the first GX read')


def test_survey_after_x290():
    with T3.synthetic_firmware() as fw:
        fake = P.FakeP67()
        path_to(fake, fw, x290=True)
        status, out = run(fake, 'x290-survey')
        check(status == 0 and out['x290']['ubwc']['programmed'] is True,
              f'after cp-fence --x290 the survey sees the UBWC block ({out["x290"].get("ubwc")})')


def test_survey_gates():
    with T3.synthetic_firmware() as fw:
        # GX off: the OOB token says this boot, but the GMU reports GX off now.
        fake = P.FakeP67()
        path_to(fake, fw)
        fake.gx_on = False
        since = len(fake.log)
        status, out = run(fake, 'x290-survey')
        check(status == 2 and 'GX' in out['stopped']['reason'], f'GX off: refused ({out["stopped"]})')
        check(not gx_accesses(fake, since), 'X4: no GX access with GX off')
        check(out['x290']['identity']['slices'] == 4, 'the CX identity is still reported')
        # CX AHB timeouts unarmed: a stuck GX read would be a NoC timeout, not a bus error.
        fake = P.FakeP67()
        path_to(fake, fw)
        fake.cx_ahb = [0] * 5
        since = len(fake.log)
        status, out = run(fake, 'x290-survey')
        check(status == 2 and 'AHB' in out['stopped']['reason'] and not gx_accesses(fake, since),
              f'AHB timeouts off: refused before any GX access ({out["stopped"]})')
        # No OOB GPU_SET this boot (oob never ran): refused.
        fake = P.FakeP67()
        path_to(fake, fw, upto='hfi')
        since = len(fake.log)
        status, out = run(fake, 'x290-survey')
        check(status == 2 and 'OOB' in out['stopped']['reason'] and not gx_accesses(fake, since),
              f'no OOB this boot: refused ({out["stopped"]})')


def test_survey_three_slices():
    with T3.synthetic_firmware() as fw:
        fake = P.FakeP67(slice_mask=0x7)
        path_to(fake, fw)
        status, out = run(fake, 'x290-survey')
        x = out['x290']
        check(status == 0 and x['identity']['slices'] == 3 and x['identity']['chip_id'] == '0x44070031',
              'a 3-slice part reads as X2-85')
        check(x['verdict']['part'] == 'X2-85' and x['verdict']['x290_list_ready'] is False and
              any('15.75' in reason or '0x0fc00000' in reason for reason in x['verdict']['reasons']),
              f'X1/X9: not ready, with the 3-slice GMEM named ({x["verdict"]})')


# --- X1/X6/X7/X8/X11: cp-fence --x290 ----------------------------------------------------------------------
def test_x290_writes():
    with T3.synthetic_firmware() as fw:
        base = P.FakeP67()
        path_to(base, fw)
        wide = P.FakeP67()
        path_to(wide, fw, x290=True)
        P.no_violation(wide, 'cp-fence --x290')
        a = [(reg_name(addr), v) for op, addr, v in base.log if op == 'w']
        b = [(reg_name(addr), v) for op, addr, v in wide.log if op == 'w']
        # the only difference: the UBWC block, in Linux's place (after CP_PERFCTR_CP_SEL, before the bicubic table)
        at = next(i for i, (n, _v) in enumerate(a) if n == 'TPL1_BICUBIC_WEIGHTS_TABLE[0]')
        check(a[at - 1][0] == 'CP_PERFCTR_CP_SEL[0]', f'the default writes CP_PERFCTR_CP_SEL before the bicubic table')
        nc = R.ubwc_nc_mode()
        block = [('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_BV)), ('GRAS_NC_MODE_CNTL', nc['GRAS_NC_MODE_CNTL']),
                 ('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_BR)), ('GRAS_NC_MODE_CNTL', nc['GRAS_NC_MODE_CNTL']),
                 ('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_BR)), ('RB_CCU_NC_MODE_CNTL', nc['RB_CCU_NC_MODE_CNTL']),
                 ('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_BR)), ('RB_CMP_NC_MODE_CNTL', nc['RB_CMP_NC_MODE_CNTL']),
                 ('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_NONE)), ('SP_NC_MODE_CNTL', nc['SP_NC_MODE_CNTL']),
                 ('TPL1_NC_MODE_CNTL', nc['TPL1_NC_MODE_CNTL'])]
        check(b == a[:at] + block + a[at:], 'X7: --x290 adds exactly the UBWC block, in a8xx_set_ubwc_config\'s order')
        protects = lambda log: [v for n, v in log if n == 'RB_GC_GMEM_PROTECT']
        check(protects(a) == protects(b) == [0x15000000, 0x15000000], 'X2: the same GMEM_PROTECT writes (hw_init, C5)')
        clocks = lambda log: [(n, v) for n, v in log if 'CGC' in n or 'CLOCK_CNTL' in n]
        check(clocks(b) == clocks(a) == [('RBBM_CGC_P2S_CNTL', 0x40)],
              f'X11: --x290 adds no clock-gating write (only x285_nonctxt\'s P2S_CNTL, as the default: {clocks(b)})')


def test_x290_refusals():
    with T3.synthetic_firmware() as fw:
        # X1: three slices -> stop at C0, before anything is written.
        fake = P.FakeP67(slice_mask=0x7)
        path_to(fake, fw, upto='oob')
        since = len(fake.log)
        status, out = run(fake, 'cp-fence', fw_dir=fw, x290=True)
        check(status == 2 and out['stopped']['step'] == 'C0' and '0x7' in out['stopped']['reason'],
              f'X1: 3 slices refused ({out["stopped"]})')
        check(not writes_since(fake, since), f'X1: nothing written ({writes_since(fake, since)[:3]})')
        # X1: TZ GPU init not done this boot (the mask would be a reset value).
        fake = P.FakeP67()
        path_to(fake, fw, upto='oob')
        real = S3.arena_prove

        def no_tz(s, out, *a, **k):
            host = real(s, out, *a, **k)
            host['tz_ok'] = 0
            return host
        S3.arena_prove = no_tz
        try:
            since = len(fake.log)
            status, out = run(fake, 'cp-fence', fw_dir=fw, x290=True)
        finally:
            S3.arena_prove = real
        check(status == 2 and 'TrustZone' in out['stopped']['reason'] and not writes_since(fake, since),
              f'X1: no TZ init this boot: refused before any write ({out["stopped"]})')
        # X2: a protect value other than the catalog's would be refused (forced through the function).
        check(X.protect_or_stop(0xF) == 0x15000000, '4 slices: 0x15000000')
        try:
            X.protect_or_stop(0x7)
            check(False, '3 slices must stop')
        except S3.Stop as stop:
            check('0xfc00000' in stop.reason or '0x0fc00000' in stop.reason, f'X2: named ({stop.reason})')


def test_x290_mixed():
    with T3.synthetic_firmware() as fw:
        # C3 and the SQE start without --x290, then C5 with it: the block was never written -> stop.
        fake = P.FakeP67()
        path_to(fake, fw, upto='oob')
        for stop_after, x290 in (('C3', False), ('C3S', False)):
            status, out = run(fake, 'cp-fence', fw_dir=fw, x290=x290, stop_after=stop_after)
            check(status == 0, f'cp-fence --stop-after {stop_after} ({out["stopped"]})')
        status, out = run(fake, 'cp-fence', fw_dir=fw, x290=True, stop_after='C5')
        check(status == 2 and 'x290' in out['stopped']['reason'], f'X8: mixed --x290 refused ({out["stopped"]})')
        # Every call with --x290: runs through.
        fake = P.FakeP67()
        path_to(fake, fw, upto='oob')
        for stop_after in ('C3', 'C3S', 'C5', None):
            status, out = run(fake, 'cp-fence', fw_dir=fw, x290=True, stop_after=stop_after)
            check(status == 0, f'cp-fence --x290 --stop-after {stop_after} ({out["stopped"]})')
        P.no_violation(fake, 'stepwise --x290')


# --- X5: the per-slice reads (a separate, --write command) -----------------------------------------------------
def test_slices():
    with T3.synthetic_firmware() as fw:
        fake = P.FakeP67(slice_mask=0x5)
        path_to(fake, fw)
        found = fake.aperture
        since = len(fake.log)
        status, out = run(fake, 'x290-slices')
        check(status == 0, f'x290-slices completes ({out["stopped"]})')
        aperture = R.REGS['CP_APERTURE_CNTL_HOST'].addr
        written = [v for op, a, v in fake.log[since:] if op == 'w' and a == aperture]
        slices = sorted({v >> 16 & 7 for v in written if v >> 23 & 1})
        check(slices == [0, 2], f'X5: only the enabled slices are selected ({slices})')
        check(written[-1] == found and fake.aperture == found, 'X5: the aperture ends as it was found')
        other = [reg_name(a) for op, a, _v in fake.log[since:] if op == 'w' and a != aperture]
        check(not other, f'x290-slices writes only the aperture ({other})')
        rows = out['x290_slices']['rows']
        check({(r['pipe'], r['slice']) for r in rows if r['slice'] is not None} ==
              {('BR', 0), ('BR', 2), ('BV', 0), ('BV', 2)}, 'one row per pipe and enabled slice')
        check(all('RB_GC_GMEM_PROTECT' in r['registers'] for r in rows if r['pipe'] == 'BR'), 'BR rows hold the protect')
        P.no_violation(fake, 'x290-slices')
        # The CLI refuses it without --write.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                a16gpu.main(['x290-slices'])
                check(False, 'x290-slices without --write must be refused')
            except SystemExit as exit_:
                check(exit_.code == 2 and '--write' in err.getvalue(), 'CLI: x290-slices needs --write')


def test_cli():
    parser_ok = True
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            a16gpu.main(['x290-survey', '--help'])
        except SystemExit as exit_:
            parser_ok = exit_.code == 0
    check(parser_ok and 'read-only' in out.getvalue().lower(), 'CLI: x290-survey --help')
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            a16gpu.main(['cp-fence', '--help'])
        except SystemExit:
            pass
    check('--x290' in out.getvalue(), 'CLI: cp-fence --x290')


def main():
    for test in (test_constants, test_survey_default, test_survey_after_x290, test_survey_gates,
                 test_survey_three_slices, test_x290_writes, test_x290_refusals, test_x290_mixed, test_slices, test_cli):
        test()
    print(f'PASS: {checks} a16gpu X2-90 survey / --x290 model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
