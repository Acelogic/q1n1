# SPDX-License-Identifier: MIT
"""X2-90 identity and configuration (Snapintosh agents/gpu-x290, FAILURE-MODES.md X1-X11).

The question (turnip-audit FINDINGS item 4): is the A16's Adreno brought up as an X2-85 when it is an X2-90? Linux has
one catalog entry for the family (chip id 0x44070001, max_slices 4, 21 MiB GMEM, the x285_* register lists) and folds
the active slice count into the chip id at runtime; Mesa calls 0x44070041 (4 slices, 8 CCUs) X2-90 and 0x44070031
(3 slices, 6 CCUs) X2-85. q1n1 programs that one list, so on a 4-slice part it is the X2-90's. What differs between the
two parts is the GMEM size (5.25 MiB per active slice: Windows qcdxkmbase8480_185.bin vs _ma_185.bin, linux-next
a8xx_gpu.c:115-116) and the CCU count userspace sizes its GMEM carve-out for. These commands show the hardware's side.

  a16gpu.py x290-survey                 READ-ONLY. After cp-fence (GX on): the slice fuse and the chip id it implies,
                                        whether TrustZone's GPU init ran first, the speed-bin fuse, the global
                                        (PIPE_NONE) registers hw_init wrote, CP_PROTECT_GLOBAL, the GMEM VA range and
                                        trap bases, the UBWC/NC_MODE state, the clock-gating state, and a verdict.
  a16gpu.py x290-slices --write         NOT read-only: selects each BR/BV pipe and each enabled slice through
                                        CP_APERTURE_CNTL_HOST (PIPEID, SLICEID, USESLICEID; a8xx_read_pipe_slice) and
                                        reads the pipe registers (RB_GC_GMEM_PROTECT, the RB/GRAS NC_MODE, RB_CCU_CNTL,
                                        RB_CCU_CACHE_CNTL, CP_PROTECT_PIPE, the BR/BV nonctxt list); writes nothing
                                        but that routing register and puts back the value it found.
  a16gpu.py cp-fence --write --x290     opt-in hw_init for the X2-90: refuses unless the slice fuse reads 0xf after this
                                        boot's TZ GPU init (chip id 0x44070041) and the GMEM protect computed from it
                                        is the catalog's 0x15000000; adds a8xx_set_ubwc_config for glymur (UBWC 5.0,
                                        level 2/3 swizzle off, highest bank bit 16), which the default skips. Give it
                                        to every cp-fence of a GMU boot (a resumed one checks the block is there).
                                        It never touches HW clock gating (a8xx_set_hwcg): power, not correctness.

Gates (as adopt): Stage 1's prologue (GCC/GPUCC from reads, the CX GDSC on and the GMU clocks running), the arena,
the GMU running from it, OOB GPU_SET acknowledged this boot and GMU_PWR_CLK_STATUS GX on, and the CX AHB timeouts
armed, before the first GX read; run() arms the strict SError guard. No register reports the CCU count: it is
reported as inferred (2 per slice).
"""
from __future__ import annotations

import adreno_x2 as R
import a16gpu_stage3 as S3
from a16gpu_stage3 import Stop, hx

SCHEMA = 'q1n1-x290-survey-v1'
NC_GLOBAL = ('SP_NC_MODE_CNTL', 'TPL1_NC_MODE_CNTL')
UBWC_WRITES = ((R.PIPE_BV, 'GRAS_NC_MODE_CNTL'), (R.PIPE_BR, 'GRAS_NC_MODE_CNTL'), (R.PIPE_BR, 'RB_CCU_NC_MODE_CNTL'),
               (R.PIPE_BR, 'RB_CMP_NC_MODE_CNTL'))
HWCG = ('RBBM_CLOCK_CNTL_GLOBAL', 'RBBM_CGC_GLOBAL_LOAD_CMD', 'RBBM_CGC_0_PC', 'RBBM_CGC_P2S_CNTL', 'RBBM_CGC_P2S_STATUS',
        'GMU_AO_GMU_CGC_MODE_CNTL', 'GMU_AO_GMU_CGC_DELAY_CNTL', 'GMU_AO_GMU_CGC_HYST_CNTL')
RANGES = ('UCHE_CCHE_GC_GMEM_RANGE_MIN', 'SP_HLSQ_GC_GMEM_RANGE_MIN', 'UCHE_CCHE_LPAC_GMEM_RANGE_MIN',
          'SP_HLSQ_LPAC_GMEM_RANGE_MIN', 'UCHE_TRAP_BASE', 'UCHE_WRITE_THRU_BASE', 'UCHE_CCHE_TRAP_BASE',
          'UCHE_CCHE_WRITE_THRU_BASE')
RANGE_EXPECT = {'UCHE_CCHE_GC_GMEM_RANGE_MIN': R.GMEM_RANGE_MIN, 'SP_HLSQ_GC_GMEM_RANGE_MIN': R.GMEM_RANGE_MIN,
                'UCHE_TRAP_BASE': R.UCHE_TRAP_BASE, 'UCHE_WRITE_THRU_BASE': R.UCHE_TRAP_BASE,
                'UCHE_CCHE_TRAP_BASE': R.UCHE_TRAP_BASE, 'UCHE_CCHE_WRITE_THRU_BASE': R.UCHE_TRAP_BASE}
# hw_init writes these again after the nonctxt pass (a8xx_gpu.c hw_init "Enable fault detection"): the last write counts.
FINAL_WRITE = {'RBBM_INTERFACE_HANG_INT_CNTL': (1 << 30) | 0xCFFFFF}
PIPE_REGS = {
    'BR': ('RB_GC_GMEM_PROTECT', 'RB_LPAC_GMEM_PROTECT', 'RB_CCU_NC_MODE_CNTL', 'RB_CMP_NC_MODE_CNTL', 'RB_CCU_CNTL',
           'RB_CCU_CACHE_CNTL', 'GRAS_NC_MODE_CNTL', 'RB_CONTEXT_SWITCH_GMEM_SAVE_RESTORE_ENABLE', 'CP_PROTECT_CNTL_PIPE',
           ('CP_PROTECT_PIPE', 15), 'CP_APRIV_CNTL_PIPE'),
    'BV': ('GRAS_NC_MODE_CNTL', 'CP_PROTECT_CNTL_PIPE', ('CP_PROTECT_PIPE', 15), 'CP_APRIV_CNTL_PIPE'),
}


def identity(mask, tz_ok, freq_limit=None):
    slices = bin(mask & R.SLICE_MASK).count('1')
    gmem = R.x2_gmem_bytes(mask)
    return {'slice_enable_final': hx(mask), 'slice_mask': hx(mask & R.SLICE_MASK), 'slices': slices,
            'chip_id': hx(R.runtime_chip_id(mask)), 'tz_init_done': tz_ok == 1,
            'ccu_count': R.X2_CCU_PER_SLICE * slices,
            'ccu_count_source': 'inferred (Mesa 2 per slice: X2-85 6/3, X2-90 8/4); no register reports it',
            'gmem_bytes': hex(gmem), 'gmem_protect_expected': hx(R.gmem_protect_for(gmem)),
            'freq_limit': freq_limit}


def protect_or_stop(mask, step='C0'):
    """X2: the protect value from the active slices must be the catalog's (q1n1's x285_nonctxt) value."""
    protect = R.gmem_protect_for(R.x2_gmem_bytes(mask))
    want = S3.x285_gmem_protect()
    if protect != want:
        raise Stop(step, f'slice mask {mask & R.SLICE_MASK:#x}: GMEM {R.x2_gmem_bytes(mask):#x} needs RB_GC_GMEM_PROTECT '
                         f'{protect:#010x}, not the {want:#010x} q1n1 writes (and the plug-in carves 21 MiB for 8 CCUs)')
    return protect


# --- cp-fence --x290 -----------------------------------------------------------------------------------------------
def cp_fence_check(s, out, host, already):
    """C0, before anything is written (X1, X2, X8). already: this GMU boot's hw_init ran in an earlier call."""
    if host.get('tz_ok') != 1:
        raise Stop('C0', 'TrustZone GPU init (qcom_scm_gpu_init_regs, gmu-boot B3) is not recorded for this boot: '
                         'the slice fuse may read its reset value (--x290)')
    mask = s.rd('CX_MISC_SLICE_ENABLE_FINAL')
    out['x290'] = {'identity': identity(mask, host.get('tz_ok'))}
    if mask & R.SLICE_MASK != R.SLICE_MASK:
        raise Stop('C0', f'CX_MISC_SLICE_ENABLE_FINAL {mask:#x}: not the 4-slice X2-90 (chip id '
                         f'{R.runtime_chip_id(mask):#x}); --x290 refuses', x290=out['x290'])
    out['x290']['gmem_protect'] = hx(protect_or_stop(mask))
    if already:
        resume_check(s, out)


def resume_check(s, out):
    """X8: a GMU boot whose hw_init ran in an earlier call must have had --x290 there too."""
    want = R.ubwc_nc_mode()
    got = {name: s.rd(name) for name in NC_GLOBAL}
    out['x290']['resume'] = {name: hx(value) for name, value in got.items()}
    if any(got[name] != want[name] for name in NC_GLOBAL):
        raise Stop('C0', f'this GMU boot\'s hw_init ran without --x290 ({ {n: hx(v) for n, v in got.items()} } != '
                         f'{ {n: hx(want[n]) for n in NC_GLOBAL} }): `quiesce --write`, then gmu-boot, hfi, oob and '
                         'every cp-fence with --x290', x290=out['x290'])


def ubwc_config(s, out):
    """a8xx_set_ubwc_config (linux-next a8xx_gpu.c:277-365) for glymur, in its order; hw_init calls it where Linux
    does (after CP_PERFCTR_CP_SEL(0), before the bicubic table)."""
    values = R.ubwc_nc_mode()
    for pipe, name in UBWC_WRITES:
        S3.pipe_write(s, pipe, name, values[name])
    S3.aperture_clear(s)
    for name in NC_GLOBAL:
        s.wr(name, values[name])
    out.setdefault('x290', {})['ubwc'] = {name: hx(value) for name, value in values.items()}


# --- the survey (read-only) -----------------------------------------------------------------------------------------
def gx_proven(s, out, host, step):
    """X4: the same proofs adopt makes before its first GX read (the GMU-running proof is prologue()'s)."""
    if host['oob_token'] != host['boot_token']:
        raise Stop(step, 'OOB GPU_SET was not acknowledged for this GMU boot: run oob (and cp-fence) first')
    status, on = S3.gx_on_proven(s, 'OOB GPU_SET held for this boot')
    if not on:
        raise Stop(step, f'GMU_PWR_CLK_STATUS {status:#x}: GX is off; no GX register is read')
    hub_hz = R.HUB_HZ.get(s.rd('GPU_CC_HUB_CFG_RCGR') & R.RCG_CFG_SRC_MASK)
    want = R.ahb_timeout_cntl(hub_hz) if hub_hz else None
    ahb = [s.rd('CX_MISC_CX_AHB_CNTL', n) for n in range(len(R.CX_AHB_PORTS))]
    if want is None or ahb != [want] * len(ahb):
        raise Stop(step, f'CX AHB timeouts read {[hx(v) for v in ahb]}, not {hx(want) if want else "a sized value"}: '
                         'a stuck GX read would be a NoC timeout, not a bus error (cp-fence arms them)')
    return status


def read64(s, name):
    reg = R.REGS[name]
    if reg.count == 2:
        return s.rd(name, 0) | s.rd(name, 1) << 32
    return s.rd(name) | s.rd(name + '_HI') << 32


def prologue(s, out, step):
    S3.prologue(s, out)                 # clocks verified first: Stage 1 S0, S2-S4 and the CX gates from reads
    host = S3.arena_prove(s, out)
    s.step = step
    S3.gmu_running_proven(s, out, host)
    mask = s.rd('CX_MISC_SLICE_ENABLE_FINAL')
    out['x290'] = {'schema': SCHEMA, 'identity': identity(mask, host.get('tz_ok'), out.get('freq_limit'))}
    out['x290']['identity']['sw_fuse_value'] = hx(s.rd('CX_MISC_SW_FUSE_VALUE'))
    return host, mask


def verdict(x):
    """x290_list_ready depends on the identity only; read-back differences are findings, reported separately."""
    ident, reasons, findings = x['identity'], [], []
    part = {4: 'X2-90', 3: 'X2-85'}.get(ident['slices'], f'{ident["slices"]} slices')
    if ident['slices'] != 4:
        reasons.append(f'{ident["slices"]} active slices: GMEM {ident["gmem_bytes"]} (15.75 MiB for 3), '
                       f'RB_GC_GMEM_PROTECT {ident["gmem_protect_expected"]}, {ident["ccu_count"]} CCUs; q1n1 writes '
                       '0x15000000 and the plug-in carves 21 MiB for 8 CCUs')
    if not ident['tz_init_done']:
        reasons.append('the slice fuse was read without this boot\'s TZ GPU init recorded')
    if x.get('protect', {}).get('matches_x285') is False:
        findings.append(f'CP_PROTECT_GLOBAL differs from the x285 list: {x["protect"]["differ"]}')
    bad = [n for n, r in x.get('nonctxt', {}).items() if not r['match']]
    if bad:
        findings.append(f'PIPE_NONE nonctxt registers read back differently: {bad}')
    bad = [n for n, ok in x.get('gmem_range_match', {}).items() if not ok]
    if bad:
        findings.append(f'GMEM range / trap base differ: {bad}')
    return {'part': part, 'x290_list_ready': not reasons, 'reasons': reasons, 'findings': findings}


def cmd_x290_survey(s, out, args):
    host, mask = prologue(s, out, 'XS1')
    x = out['x290']
    gx_proven(s, out, host, 'XS1')
    s.step = 'XS2'
    aperture = s.rd('CP_APERTURE_CNTL_HOST')
    x['aperture'] = {'read': hx(aperture), 'pipe_registers': 'not read',
                     'note': 'pipe/slice registers are read only by x290-slices, through their own aperture'}
    x['status'] = {name: hx(s.rd(name)) for name in ('RBBM_STATUS', 'RB_UNSLICE_STATUS', 'CP_SQE_CNTL',
                                                     'RBBM_SECVID_TRUST_CNTL', 'RBBM_SECVID_TSB_CNTL')}
    x['nonctxt'] = {}
    for name, value, pipes in R.X285_NONCTXT:
        if pipes == 1 << R.PIPE_NONE:
            value = FINAL_WRITE.get(name, value)
            got = s.rd(name)
            x['nonctxt'][name] = {'read': hx(got), 'wrote': hx(value), 'match': got == value}
    protect = [s.rd('CP_PROTECT_GLOBAL', n) for n in range(R.REGS['CP_PROTECT_GLOBAL'].count)]
    want = list(R.X285_PROTECT) + [0] * (len(protect) - len(R.X285_PROTECT))
    x['protect'] = {'entries': len(R.X285_PROTECT), 'matches_x285': protect == want,
                    'differ': {n: [hx(a), hx(b)] for n, (a, b) in enumerate(zip(protect, want)) if a != b}}
    x['gmem_range'] = {name: hex(read64(s, name)) for name in RANGES}
    x['gmem_range_match'] = {name: read64(s, name) == value for name, value in RANGE_EXPECT.items()}
    nc_want = R.ubwc_nc_mode()
    nc_got = {name: s.rd(name) for name in NC_GLOBAL}
    x['ubwc'] = {'read': {n: hx(v) for n, v in nc_got.items()}, 'linux_glymur': {n: hx(nc_want[n]) for n in NC_GLOBAL},
                 'programmed': all(nc_got[n] == nc_want[n] for n in NC_GLOBAL),
                 'pipe_copies': 'GRAS (BV, BR), RB_CCU and RB_CMP (BR): x290-slices'}
    hw = {name: s.rd(name) for name in HWCG}
    x['hwcg'] = {'read': {n: hx(v) for n, v in hw.items()},
                 'enabled': hw['RBBM_CLOCK_CNTL_GLOBAL'] == 0 and hw['RBBM_CGC_GLOBAL_LOAD_CMD'] == 1,
                 'linux_on': 'RBBM_CLOCK_CNTL_GLOBAL 0, RBBM_CGC_GLOBAL_LOAD_CMD 1, RBBM_CGC_0_PC 0x702 (a8xx_set_hwcg)'}
    x['gbif_cx_config'] = hx(s.rd('GBIF_CX_CONFIG'))
    x['verdict'] = verdict(x)
    out['order'].append('x290_survey')


def cmd_x290_slices(s, out, args):
    host, mask = prologue(s, out, 'XP1')
    gx_proven(s, out, host, 'XP1')
    s.step = 'XP2'
    found = s.rd('CP_APERTURE_CNTL_HOST')
    expect = {name: value for name, value, _pipes in R.X285_NONCTXT}
    expect.update(R.ubwc_nc_mode())
    expect.update(CP_PROTECT_PIPE=R.X285_PROTECT[-1], CP_PROTECT_CNTL_PIPE=R.CP_PROTECT_CNTL,
                  RB_CONTEXT_SWITCH_GMEM_SAVE_RESTORE_ENABLE=1)
    rows = []
    for pipe, label in ((R.PIPE_BR, 'BR'), (R.PIPE_BV, 'BV')):
        names = list(PIPE_REGS[label]) + [n for n, _v, pipes in R.X285_NONCTXT
                                          if pipes >> pipe & 1 and n not in PIPE_REGS[label]]
        for slice_id in [None] + [n for n in range(R.MAX_SLICES) if mask >> n & 1]:
            value = R.aperture(pipe) if slice_id is None else R.aperture_slice(pipe, slice_id)
            s.wr('CP_APERTURE_CNTL_HOST', value)
            regs = {}
            for entry in names:
                name, index = entry if isinstance(entry, tuple) else (entry, 0)
                regs[name] = hx(s.rd(name, index))
            rows.append({'pipe': label, 'slice': slice_id, 'aperture': hx(value), 'registers': regs,
                         'differ': {n: v for n, v in regs.items() if n in expect and int(v, 16) != expect[n]}})
    s.wr('CP_APERTURE_CNTL_HOST', found)
    back = s.rd('CP_APERTURE_CNTL_HOST')
    out['x290_slices'] = {'slice_mask': hx(mask), 'aperture_found': hx(found), 'aperture_restored': hx(back),
                          'rows': rows,
                          'expected': {n: hx(v) for n, v in expect.items()},
                          'note': 'differ: against what hw_init writes (x285_nonctxt, CP_PROTECT, and the --x290 UBWC '
                                  'values); without --x290 the NC_MODE registers keep their reset values'}
    if back != found:
        raise Stop('XP2', f'CP_APERTURE_CNTL_HOST reads {back:#x} after restoring {found:#x}')
    out['order'].append('x290_slices')


COMMANDS = {'x290-survey': (cmd_x290_survey, False), 'x290-slices': (cmd_x290_slices, True)}
S3.COMMANDS.update(COMMANDS)


def add_parsers(sub):
    text = ('X2-90: READ-ONLY slice fuse, chip id, GMEM/protect/UBWC/HWCG state and verdict (after cp-fence; '
            'writes nothing)')
    sub.add_parser('x290-survey', help=text, description=text)
    text = ('X2-90: per pipe and enabled slice, the pipe registers through CP_APERTURE_CNTL_HOST (NOT read-only: '
            'writes only that register and restores it)')
    sl = sub.add_parser('x290-slices', help=text, description=text)
    sl.add_argument('--write', action='store_true', help='required: this writes CP_APERTURE_CNTL_HOST')
