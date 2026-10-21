# SPDX-License-Identifier: MIT
"""LLCC (system cache) slices: per-SCID configuration and activation, as Linux llcc-qcom.c does on glymur
(Snapintosh agents/dvfs FAILURE-MODES.md section 6).

The A16's read-only survey (installed-fresh-desktop-20261021-b12v1-ddr-survey.json) found CPUSS (SCID 1), GPU (9)
and GPUHTW (11) deactivated in every bank: the system cache is off for the CPU and the GPU after UEFI. Under Windows
the system-cache driver (qcsyscache8480.sys) activates CPUSS at boot and the GPU driver activates GPU/GPUHTW; Linux
does the same in llcc-qcom.c's probe (CPUSS) and the GPU driver (a8xx_llc_activate).

  a16gpu.py llcc-survey                                   READ-ONLY: version, bank count, slice status and config
  a16gpu.py llcc-activate --write [--slices cpuss,gpu,gpuhtw] [--table linux|windows]
  a16gpu.py llcc-deactivate --write [--slices ...]        the rollback (a cold boot also leaves them deactivated)
  a16gpu.py llcc-gpu --write [--clear]                    GBIF_SCACHE_CNTL0/1 = the GPU slice's SCID, after cp-fence

Programming (llcc-qcom.c _qcom_llcc_cfg_program_v6, :4888-5019), through the broadcast base 0x20400000:
  ATTR0 = reserved ways, ATTR1 = bonus ways, ATTR2 = cache mode | probe-target << 4 | fixed-size << 8 | priority << 12,
  ATTR3 = max_cap KiB * 1024 / banks / 64 (lines per bank), at 0x41000/08/10/14 + 64 * SCID; then one bit per
  slice in STALE_EN, STALE_CAP_EN, MRU0, MRU1, ALLOC0..3 (vict_prio is ALLOC3), WRS_EN, WRS_CACHEABLE_EN.
Activation (llcc_update_act_ctrl, :4567-4613): ACT_CTRL(n) = opcode << 1 | trigger, then without the trigger; poll
the broadcast-and STATUS(n) for ACT_COMPLETE (bit 4, every bank); poll the broadcast STATUS(n) until the opposite
state bit clears (DEACTIVE bit 1 for activate, ACTIVE bit 0 for deactivate); ACT_CLEAR(n) = 1.

Tables (per slice: SCID, max_cap KiB, priority, fixed, bonus ways, reserved ways, cache mode, probe-target, the eight
algorithm bits in v6 order, WRS_EN, WRS_CACHEABLE_EN):
  linux    glymur_data, llcc-qcom.c:184-373 (CPUSS :186, GPUHTW :231, GPU :240) -- the default;
  windows  the A16's own qcsyscache8480.inf, LLCI12CH (12 banks) or LLCI8CH (8): same fields, larger max_cap.

GPU side (a8xx_llc_activate): GBIF_SCACHE_CNTL1 (0x03d0f008) = the GPU SCID in five 6-bit fields [29:24..5:0];
GBIF_SCACHE_CNTL0 (0x03d0f004) = the SCID in [27:22], [21:16], [15:10] and BIT(8). GPUHTW has no GPU register on
a8xx: the SMMU walker's SCID is the boot loader's (a6xx_gpu.c:2060-2062 says so for MMU-500 targets), and Linux
asks for outer write-back walks only then (IO_PGTABLE_QUIRK_ARM_OUTER_WBWA, a6xx_gpu.c:2457-2459); q1n1's CB0/CB1
walks stay non-cacheable (adreno_x2.CB_TCR), so GPUHTW is activated but inert until that changes. GPU data PTEs need
no special attribute: Linux maps GPU buffers non-cacheable unless coherent (msm_gem.c:459-470), and the SCID
decides allocation.
"""
from __future__ import annotations

import adreno_x2 as R
import a16gpu_stage3 as S3
from a16gpu_stage3 import Stop, hx

SCHEMA = 'q1n1-llcc-v1'
ACTIVE, DEACTIVE, ACT_COMPLETE = 1 << 0, 1 << 1, 1 << 4
OPCODE_ACTIVATE, OPCODE_DEACTIVATE, ACT_TRIGGER = 1, 2, 1
ALGO = ('ALGO_STALE_EN', 'ALGO_STALE_CAP_EN', 'ALGO_MRU0', 'ALGO_MRU1', 'ALGO_ALLOC0', 'ALGO_ALLOC1', 'ALGO_ALLOC2',
        'ALGO_ALLOC3')
BITS = ALGO + ('WRS_EN', 'WRS_CACHEABLE_EN')
S3.POLL.setdefault('llcc', (200, 0.5))   # Linux: 100 us, regmap reads; here each read is a proxy round trip


def row(scid, max_cap, priority, fixed, algo, wren=0, wrc=0, bonus=0xFFF, res=0, mode=0, probe=0):
    return dict(scid=scid, max_cap=max_cap, priority=priority, fixed=fixed, bonus=bonus, res=res, mode=mode,
                probe=probe, bits=tuple(algo) + (wren, wrc))


VICT = (0, 0, 0, 0, 0, 0, 0, 1)
TABLES = {
    # llcc-qcom.c glymur_data: CPUSS :186-193, GPUHTW :231-238, GPU :240-249 (vict_prio = ALLOC3 for every slice).
    'linux': {'cpuss': row(1, 7680, 1, 0, VICT), 'gpu': row(9, 7680, 1, 0, (1, 0, 0, 0, 0, 0, 0, 1), 1, 1),
              'gpuhtw': row(11, 512, 1, 1, VICT)},
    # qcsyscache8480.inf (A16 Windows driver store) LLCI12CH :175 cpuss, :178 gpu, :186 gpuhtw; LLCI8CH :102, :105, :113.
    'windows-12': {'cpuss': row(1, 10240, 1, 0, VICT), 'gpu': row(9, 10240, 1, 0, (1, 0, 0, 0, 0, 0, 0, 1), 1, 1),
                   'gpuhtw': row(11, 682, 1, 1, VICT)},
    'windows-8': {'cpuss': row(1, 9728, 1, 0, VICT), 'gpu': row(9, 10240, 1, 0, (1, 0, 0, 0, 0, 0, 0, 1), 1, 1),
                  'gpuhtw': row(11, 1024, 1, 1, VICT)},
}
DEFAULT_SLICES = ('cpuss', 'gpu', 'gpuhtw')


def attrs(cfg, banks):
    """ATTR0..3 for one slice (_qcom_llcc_cfg_program_v6)."""
    attr2 = cfg['mode'] | (cfg['probe'] & 1) << 4 | (cfg['fixed'] & 1) << 8 | (cfg['priority'] & 7) << 12
    return (cfg['res'], cfg['bonus'], attr2, cfg['max_cap'] * 1024 // banks >> 6)


def identity(s, out):
    """L1: the v6 register map and the bank count, before anything is written."""
    s.step = 'LL0'
    version = s.rd('LLCC_BCAST_COMMON_HW_INFO')
    banks = s.rd('LLCC0_COMMON_STATUS0') >> 28 & 0xF
    out['llcc'] = {'schema': SCHEMA, 'version': hx(version), 'banks': banks}
    if version >> 24 != 6:
        raise Stop('LL0', f'LLCC version {version:#x} is not 6.x: the v6 offsets would be wrong; nothing written')
    if banks not in (8, 12):
        raise Stop('LL0', f'LLCC reports {banks} banks (COMMON_STATUS0 [31:28]); expected 12 (or 8); nothing written')
    return version, banks


def table(name, banks):
    if name == 'windows':
        name = f'windows-{banks}'
    if name not in TABLES:
        raise Stop('LL0', f'no slice table {name!r}')
    return name, TABLES[name]


def slice_state(s, scid):
    any_, all_ = s.rd('LLCC_BCAST_TRP_STATUS', scid), s.rd('LLCC_BCAST_AND_TRP_STATUS', scid)
    return {'or': hx(any_), 'and': hx(all_), 'active_all_banks': bool(all_ & ACTIVE),
            'active_any_bank': bool(any_ & ACTIVE), 'deactive_any_bank': bool(any_ & DEACTIVE)}


def slice_config(s, scid):
    """The slice's ATTR words and bits as every bank holds them (OR) and as all agree (AND)."""
    word, bit = scid // 32, 1 << (scid % 32)
    cfg = {'attr_or': [hx(s.rd(f'LLCC_BCAST_TRP_ATTR{n}', scid)) for n in range(4)],
           'attr_and': [hx(s.rd(f'LLCC_BCAST_AND_TRP_ATTR{n}', scid)) for n in range(4)]}
    cfg['bits_or'] = {n: int(bool(s.rd(f'LLCC_BCAST_{n}', word) & bit)) for n in BITS}
    return cfg


def program(s, scid, cfg, banks):
    """_qcom_llcc_cfg_program_v6 for one slice (its own words and bits only)."""
    words = attrs(cfg, banks)
    for n, value in enumerate(words):
        s.wr(f'LLCC_BCAST_TRP_ATTR{n}', value, scid)
    word, bit = scid // 32, 1 << (scid % 32)
    changed = {}
    for name, want in zip(BITS, cfg['bits']):
        any_, all_ = s.rd(f'LLCC_BCAST_{name}', word), s.rd(f'LLCC_BCAST_AND_{name}', word)
        if any_ != all_:
            # L5: a broadcast write of the OR would set other slices' bits in the banks that lack them.
            raise Stop(s.step, f'LLCC {name}[{word}] differs between banks (OR {any_:#x}, AND {all_:#x}): '
                               'refusing a broadcast read-modify-write', register=name)
        new = (any_ & ~bit) | (bit if want else 0)
        if new != any_:
            s.wr(f'LLCC_BCAST_{name}', new, word)
            changed[name] = hx(new)
    readback = [s.rd(f'LLCC_BCAST_AND_TRP_ATTR{n}', scid) for n in range(4)]
    if readback != list(words):
        raise Stop(s.step, f'LLCC slice {scid}: ATTR read back {[hx(w) for w in readback]} for '
                           f'{[hx(w) for w in words]} (not every bank took the write)')
    return {'attr': [hx(w) for w in words], 'bits_changed': changed}


def act(s, scid, opcode, clears):
    """llcc_update_act_ctrl: trigger, release, ACT_COMPLETE in every bank, the old state bit clear, ACT_CLEAR."""
    value = opcode << 1
    s.wr('LLCC_BCAST_TRP_ACT_CTRL', value | ACT_TRIGGER, scid)
    s.wr('LLCC_BCAST_TRP_ACT_CTRL', value, scid)
    s.poll3('LLCC_BCAST_AND_TRP_STATUS', ACT_COMPLETE, ACT_COMPLETE, 'llcc', index=scid)
    s.poll3('LLCC_BCAST_TRP_STATUS', clears, 0, 'llcc', index=scid)
    s.wr('LLCC_BCAST_TRP_ACT_CLEAR', 1, scid)


def selected(args):
    names = tuple(n.strip() for n in (getattr(args, 'slices', None) or ','.join(DEFAULT_SLICES)).split(',') if n.strip())
    if not names or any(n not in DEFAULT_SLICES for n in names) or len(set(names)) != len(names):
        raise Stop('LL0', f'--slices takes distinct names from {",".join(DEFAULT_SLICES)}')
    return names


def cmd_llcc_survey(s, out, args):
    _version, banks = identity(s, out)
    s.step = 'LL1'
    out['llcc']['slices'] = {}
    for name in DEFAULT_SLICES:
        scid = TABLES['linux'][name]['scid']
        out['llcc']['slices'][name] = dict(scid=scid, status=slice_state(s, scid), config=slice_config(s, scid))
    out['llcc']['plans'] = {t: {n: [hx(w) for w in attrs(c, banks)] for n, c in rows.items()}
                            for t, rows in TABLES.items() if not t.startswith('windows') or t.endswith(str(banks))}
    out['order'].append('llcc_survey')


def cmd_llcc_activate(s, out, args):
    _version, banks = identity(s, out)
    name, rows = table(getattr(args, 'table', None) or 'linux', banks)
    out['llcc'].update(mode='activate', table=name, slices={})
    for slice_name in selected(args):
        cfg = rows[slice_name]
        scid = cfg['scid']
        s.step = f'LL1:{slice_name}'
        entry = out['llcc']['slices'][slice_name] = dict(scid=scid, before=slice_state(s, scid),
                                                         config_before=slice_config(s, scid))
        if entry['before']['active_any_bank']:
            entry['skipped'] = 'already active: left as found (L4)'
            continue
        entry['programmed'] = program(s, scid, cfg, banks)
        s.step = f'LL2:{slice_name}'
        act(s, scid, OPCODE_ACTIVATE, DEACTIVE)
        entry['after'] = slice_state(s, scid)
        entry['activated'] = True
        if not entry['after']['active_all_banks']:
            raise Stop(s.step, f'LLCC slice {scid} not active in every bank after activation: {entry["after"]}')
    out['order'].append('llcc_activate')


def cmd_llcc_deactivate(s, out, args):
    identity(s, out)
    out['llcc'].update(mode='deactivate', slices={})
    for slice_name in selected(args):
        scid = TABLES['linux'][slice_name]['scid']
        s.step = f'LL3:{slice_name}'
        entry = out['llcc']['slices'][slice_name] = dict(scid=scid, before=slice_state(s, scid))
        if not entry['before']['active_any_bank']:
            entry['skipped'] = 'not active'
            continue
        act(s, scid, OPCODE_DEACTIVATE, ACTIVE)
        entry['after'] = slice_state(s, scid)
        entry['deactivated'] = True
        if entry['after']['active_any_bank']:
            raise Stop(s.step, f'LLCC slice {scid} still active in a bank after deactivation: {entry["after"]}')
    out['order'].append('llcc_deactivate')


def gpu_scid_words(scid):
    """a8xx_llc_activate (a8xx_gpu.c:1276-1290): (CNTL0, CNTL1)."""
    scid &= 0x3F
    cntl1 = scid << 24 | scid << 18 | scid << 12 | scid << 6 | scid
    cntl0 = scid << 22 | scid << 16 | scid << 10 | 1 << 8
    return cntl0, cntl1


def cmd_llcc_gpu(s, out, args):
    S3.prologue(s, out)
    s.step = 'LL4'
    _status, on = S3.gx_on_proven(s, 'llcc-gpu after cp-fence (OOB GPU_SET held)')
    if not on:
        raise Stop('LL4', 'GX is off: GBIF_SCACHE_CNTL is written only with the GPU up (after cp-fence)')
    scid = TABLES['linux']['gpu']['scid']
    clear = getattr(args, 'clear', False)
    state = slice_state(s, scid)
    if not clear and not state['active_all_banks']:
        raise Stop('LL4', f'the GPU slice {scid} is not active in every bank: run llcc-activate first ({state})')
    before = {n: s.rd(n) for n in ('GBIF_SCACHE_CNTL0', 'GBIF_SCACHE_CNTL1')}
    cntl0, cntl1 = (0, 0) if clear else gpu_scid_words(scid)
    s.wr('GBIF_SCACHE_CNTL1', cntl1)
    s.wr('GBIF_SCACHE_CNTL0', cntl0)
    after = {n: s.rd(n) for n in ('GBIF_SCACHE_CNTL0', 'GBIF_SCACHE_CNTL1')}
    out['llcc_gpu'] = {'scid': scid, 'clear': clear, 'slice': state, 'before': {k: hx(v) for k, v in before.items()},
                       'wrote': {'GBIF_SCACHE_CNTL0': hx(cntl0), 'GBIF_SCACHE_CNTL1': hx(cntl1)},
                       'after': {k: hx(v) for k, v in after.items()}}
    if after != {'GBIF_SCACHE_CNTL0': cntl0, 'GBIF_SCACHE_CNTL1': cntl1}:
        raise Stop('LL4', f'GBIF_SCACHE_CNTL read back {out["llcc_gpu"]["after"]}')
    out['order'].append('llcc_gpu')


COMMANDS = {'llcc-survey': (cmd_llcc_survey, False), 'llcc-activate': (cmd_llcc_activate, True),
            'llcc-deactivate': (cmd_llcc_deactivate, True), 'llcc-gpu': (cmd_llcc_gpu, True)}
S3.COMMANDS.update(COMMANDS)


def add_parsers(sub):
    sub.add_parser('llcc-survey', help='LLCC: read-only version, bank count, CPUSS/GPU/GPUHTW status and config')
    for name, text in (('llcc-activate', 'program and activate LLCC slices (llcc-qcom.c)'),
                       ('llcc-deactivate', 'deactivate LLCC slices (the rollback)')):
        p = sub.add_parser(name, help=f'LLCC: {text}')
        p.add_argument('--write', action='store_true', help='required: this writes LLCC registers')
        p.add_argument('--slices', default=','.join(DEFAULT_SLICES), help='cpuss,gpu,gpuhtw (any subset)')
        if name == 'llcc-activate':
            p.add_argument('--table', choices=('linux', 'windows'), default='linux',
                           help='slice values: Linux glymur_data (default) or the A16 Windows driver\'s table')
    g = sub.add_parser('llcc-gpu', help='LLCC: GBIF_SCACHE_CNTL0/1 = the GPU slice SCID (after cp-fence)')
    g.add_argument('--write', action='store_true', help='required: this writes GPU GBIF registers')
    g.add_argument('--clear', action='store_true', help='write 0 to both (the reset value)')
