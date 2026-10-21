# SPDX-License-Identifier: MIT
"""DDR / interconnect bandwidth votes for the CPU and GPU paths to DDR (Snapintosh agents/dvfs, lever 1).

Nothing in the chain votes CPU/system DDR bandwidth today; the GMU votes the GPU path at its OPP's row
(2.1 GB/s at 310 MHz). On glymur every interconnect provider votes through one BCM voter, the apps RSC's
drv-2 (Linux glymur.dtsi apps_bcm_voter; agents/dvfs/analysis/linux-interconnect.md), the DRV this tool's
B1 rail votes already use (a16gpu_stage3.step_rpmh / rsc_send). RPMh keeps one vote per DRV per BCM and
aggregates across DRVs, so a drv-2 vote raises DDR whatever the GMU's own DRV asks.

Paths (Linux drivers/interconnect/qcom/glymur.c):
  CPU  chm_apps (SH1, on/off BIT(0)) -> qns_llcc (SH0, 12 ch x 16 B) -> llcc_mc -> ebi (MC0, 12 ch x 4 B; ACV BIT(3))
  GPU  qnm_gpu  (SH1)                -> qns_llcc (SH0)               -> llcc_mc -> ebi (MC0; ACV)
Only SH0 and MC0 carry bandwidth; SH1 and ACV are enable masks. The same four BCMs serve both paths.

  a16gpu.py ddr-survey [--llcc] [--ddr-stats]   READ-ONLY: cmd-db BCM entries, apps RSC TCSes, optional
                                                 LLCC slice status and the rpmh-stats DDR block
  a16gpu.py ddr-vote --write [--level max | --kbps N]
                                                 one active-only TCS: SH0, SH1, MC0, ACV
  a16gpu.py ddr-restore --write [--kbps N | --remove]
                                                 the explicit restore vote (default 2136719 kBps)

--level max is x = y = 0x3FFF on SH0 and MC0 (bcm-voter's clamp), SH1 and ACV on: the vote Linux itself
holds on every glymur node until icc_sync_state (drivers/interconnect/core.c), so a level the SoC runs
whole boots at. --kbps N uses bcm_aggregate's math with cmd-db unit/width and the path's node widths.

Rollback. The value drv-2 held before is invisible: firmware leaves every TCS empty (A16-ADRENO-STAGE3.md)
and an active TCS clears CMD_ENABLE when it completes. So the restore is an explicit vote, never a
"previous" one: by default the GPU OPP-1 row (2136719 kBps, x = y as the GMU votes it, SH1/ACV kept on),
or --remove: valid = 0, what bcm-voter sends for a zero vote. A cold boot resets RPMh entirely.

Failure modes (agents/dvfs/FAILURE-MODES.md D1-D10): addresses and VCDs only from the A16's cmd-db, sorted
by VCD with commit on each VCD's last command (bcm-voter tcs_list_gen); never below a vote the survey can
see for the same address; refused when the active TCS is busy; bounded completion polls with the TCS
disarmed after a timeout (rsc_send); every access through the guarded Session.
"""
from __future__ import annotations

import struct

import adreno_x2 as R
import a16gpu as S1
import a16gpu_stage3 as S3
from a16gpu_stage3 import Stop, hx

SCHEMA = 'q1n1-ddr-vote-v1'
VOTE_MASK = 0x3FFF                  # include/soc/qcom/tcs.h BCM_TCS_CMD_VOTE_MASK
RESTORE_KBPS = 2136719              # glymur GPU OPP 1 row (adreno_x2.GPU_OPPS[0]): what the GMU votes at 310 MHz
# BCM -> (kind, node buswidth, node channels, enable mask). Nodes per glymur.c (G:354, 589, 733, 1671, 1796).
DDR_BCMS = {
    'SH0': ('bw', 16, 12, 0),       # qns_llcc
    'MC0': ('bw', 4, 12, 0),        # ebi
    'SH1': ('mask', 0, 0, 0x1),     # chm_apps / qnm_gpu: enable_mask BIT(0)
    'ACV': ('mask', 0, 0, 0x8),     # ebi: enable_mask BIT(3) (apps)
}
LLCC_SLICES = {'CPUSS': 1, 'GPU': 9, 'GPUHTW': 11}   # llcc-qcom.c glymur table (LL:185 ...)
DDR_STATS_MAGIC = 0xA1157A75


# --- cmd-db: every entry (Stage 1's parser keeps only the GPU keys) -----------------------------------
def cmd_db_entries(blob):
    """drivers/soc/qcom/cmd-db.c layout, as a16gpu.parse_cmd_db, returning every entry."""
    if blob[4:8] != R.CMD_DB_MAGIC:
        return {}
    data = 8 + 16 * 8 + 8
    entries = {}
    for slave in range(8):
        slv_id, header_offset, data_offset, count, _version = struct.unpack_from('<5H', blob, 8 + 16 * slave)
        if not slv_id:
            break
        for index in range(count):
            at = data + header_offset + 24 * index
            if at + 24 > len(blob):
                break
            ident = blob[at:at + 8].rstrip(b'\0').decode('ascii', 'replace')
            addr, length, offset = struct.unpack_from('<IHH', blob, at + 16)
            aux = blob[data + data_offset + offset:data + data_offset + offset + length]
            entries[ident] = {'slave': slv_id, 'addr': hex(addr), 'aux': aux.hex()}
    return entries


def ddr_cmd_db(s, out):
    """S0 (the EFI-map checks), then every BCM entry this tool votes, with its aux data decoded."""
    S1.step_cmd_db(s, out)
    db = out['cmd_db']
    if not db.get('reserved') or not db.get('valid'):
        raise Stop('DD0', f'cmd-db not readable as reserved memory: {db.get("skipped") or db.get("unreadable")}')
    entries = cmd_db_entries(s.dram(*R.CMD_DB))
    bcms = {}
    for name in DDR_BCMS:
        entry = entries.get(name)
        if not entry or entry['slave'] != 5 or len(bytes.fromhex(entry['aux'])) < 8:
            raise Stop('DD0', f'cmd-db has no BCM {name} (slave 5, 8-byte aux): {entry}')
        aux = S3.aux_bcm(entry)
        bcms[name] = dict(entry, **aux)
    out['ddr_cmd_db'] = {'bcms': bcms, 'bcm_names': sorted(k for k, v in entries.items() if v['slave'] == 5)}
    return bcms


# --- votes ------------------------------------------------------------------------------------------------
def bcm_div(num, base):
    """bcm-voter.c bcm_div: a nonzero quotient below 1 rounds up to 1."""
    if num and num < base:
        return 1
    return num // base


def plan_votes(bcms, level=None, kbps=None, remove=False):
    """{bcm: (x, y)}: max (the clamp), N kBps on both avg and peak (bcm_aggregate), or zero."""
    plan = {}
    for name, (kind, buswidth, channels, mask) in DDR_BCMS.items():
        aux = bcms[name]
        if remove:
            plan[name] = (0, 0)
        elif kind == 'mask':
            plan[name] = (0, mask)        # bcm_aggregate: x = 0, y = enable_mask while any node votes
        elif level == 'max':
            plan[name] = (VOTE_MASK, VOTE_MASK)
        else:
            if not aux['unit'] or not aux['width']:
                raise Stop('DD1', f'cmd-db {name} unit/width zero: {aux}')
            avg = bcm_div(bcm_div(kbps * aux['width'], buswidth * channels) * 1000, aux['unit'])
            peak = bcm_div(bcm_div(kbps * aux['width'], buswidth) * 1000, aux['unit'])
            plan[name] = (min(avg, VOTE_MASK), min(peak, VOTE_MASK))
    return plan


def commands(bcms, plan):
    """(addr, data) sorted by VCD (bcm-voter list_sort), commit on each VCD's last BCM (tcs_list_gen)."""
    order = sorted(plan, key=lambda n: (bcms[n]['vcd'], int(bcms[n]['addr'], 16)))
    cmds = []
    for i, name in enumerate(order):
        x, y = plan[name]
        commit = i == len(order) - 1 or bcms[order[i + 1]]['vcd'] != bcms[name]['vcd']
        cmds.append((name, int(bcms[name]['addr'], 16), S3.bcm_cmd(commit, bool(x or y), x, y)))
    return cmds


def vote_value(data):
    """(x, y) of a BCM command word."""
    return data >> 14 & VOTE_MASK, data & VOTE_MASK


def send(s, out, cmds, raise_only=True):
    s.step = 'DD2'
    layout = S3.rsc_layout(s)
    survey = S3.rsc_survey(s, layout)
    result = {'layout': layout, 'survey_before': survey, 'sent': [], 'kept_higher': []}
    final = []
    for name, addr, data in cmds:
        existing = S3.rsc_existing(survey, addr)
        if raise_only and existing is not None:
            ex, ey = vote_value(existing)
            x, y = vote_value(data)
            if ex > x or ey > y:
                # D3: never below a vote the survey shows for the same address (RPMh: one per DRV).
                data = (data & ~(VOTE_MASK << 14 | VOTE_MASK)) | max(ex, x) << 14 | max(ey, y) | R.BCM_VALID
                result['kept_higher'].append({'bcm': name, 'existing': hx(existing)})
        final.append((name, addr, data))
    S3.rsc_send(s, layout, R.RSC_ACTIVE_TCS[0], [(a, d) for _n, a, d in final])
    result['sent'] = [{'bcm': n, 'addr': hx(a), 'data': hx(d), 'commit': bool(d & R.BCM_COMMIT),
                       'valid': bool(d & R.BCM_VALID), 'x': vote_value(d)[0], 'y': vote_value(d)[1]}
                      for n, a, d in final]
    result['irq_status'] = hx(getattr(s, 'rsc_irq', 0))
    result['survey_after'] = S3.rsc_survey(s, layout)
    return result


# --- commands ---------------------------------------------------------------------------------------------
def llcc_status(s):
    out = {}
    for name, n in LLCC_SLICES.items():
        orr, andd = s.rd('LLCC_BCAST_TRP_STATUS', n), s.rd('LLCC_BCAST_AND_TRP_STATUS', n)
        out[name] = {'slice': n, 'or': hx(orr), 'and': hx(andd), 'active_all_banks': bool(andd & 1),
                     'active_any_bank': bool(orr & 1), 'deactive_any_bank': bool(orr & 2)}
    return out


def ddr_stats(s):
    words = [s.rd('RPMH_DDR_STATS', i) for i in range(2)]
    if words[0] != DDR_STATS_MAGIC or not 0 < words[1] <= 32:
        return {'magic': hx(words[0]), 'count': words[1], 'valid': False}
    entries = []
    for e in range(words[1]):
        name, count, lo, hi = (s.rd('RPMH_DDR_STATS', 2 + 4 * e + k) for k in range(4))
        entry = {'name': hx(name), 'count': count, 'duration': lo | hi << 32}
        if name >> 8 & 0xFF == 1:         # qcom_stats.c: a frequency entry, MHz in [31:16], plan index [4:0]
            entry.update(mhz=name >> 16, plan_index=name & 0x1F)
        entries.append(entry)
    return {'magic': hx(words[0]), 'count': words[1], 'valid': True, 'entries': entries}


def cmd_ddr_survey(s, out, args):
    bcms = ddr_cmd_db(s, out)
    s.step = 'DD1'
    layout = S3.rsc_layout(s)
    out['ddr'] = {'schema': SCHEMA, 'mode': 'survey', 'layout': layout, 'survey': S3.rsc_survey(s, layout),
                  'max_plan': [{'bcm': n, 'addr': hx(a), 'data': hx(d)}
                               for n, a, d in commands(bcms, plan_votes(bcms, level='max'))]}
    if getattr(args, 'llcc', False):
        out['ddr']['llcc'] = llcc_status(s)
    if getattr(args, 'ddr_stats', False):
        out['ddr']['ddr_stats'] = ddr_stats(s)
    out['order'].append('ddr_survey')


def cmd_ddr_vote(s, out, args):
    level, kbps = getattr(args, 'level', None), getattr(args, 'kbps', None)
    if (level is None) == (kbps is None):
        raise Stop('DD1', 'give exactly one of --level max or --kbps N')
    if kbps is not None and not 0 < kbps <= 64_000_000:
        raise Stop('DD1', f'--kbps {kbps} outside 1..64000000')
    bcms = ddr_cmd_db(s, out)
    s.step = 'DD1'
    plan = plan_votes(bcms, level=level, kbps=kbps)
    out['ddr'] = {'schema': SCHEMA, 'mode': 'vote', 'level': level, 'kbps': kbps,
                  'plan': {n: list(v) for n, v in plan.items()}}
    out['ddr'].update(send(s, out, commands(bcms, plan), raise_only=True))
    out['ddr']['live'] = 'drv-2 holds this vote until ddr-restore or a cold boot'
    out['order'].append('ddr_vote')


def cmd_ddr_restore(s, out, args):
    remove, kbps = getattr(args, 'remove', False), getattr(args, 'kbps', None)
    if remove and kbps is not None:
        raise Stop('DD1', 'give --remove or --kbps, not both')
    kbps = None if remove else (kbps or RESTORE_KBPS)
    bcms = ddr_cmd_db(s, out)
    s.step = 'DD1'
    plan = plan_votes(bcms, kbps=kbps, remove=remove)
    if not remove:
        # The GMU's own form for the GPU OPP-1 row: x = y = the peak vote (a6xx_gmu_rpmh_bw_votes_init).
        plan = {n: ((v[1], v[1]) if DDR_BCMS[n][0] == 'bw' else v) for n, v in plan.items()}
    out['ddr'] = {'schema': SCHEMA, 'mode': 'restore', 'remove': remove, 'kbps': kbps,
                  'plan': {n: list(v) for n, v in plan.items()}}
    out['ddr'].update(send(s, out, commands(bcms, plan), raise_only=False))
    out['order'].append('ddr_restore')


COMMANDS = {'ddr-survey': (cmd_ddr_survey, False), 'ddr-vote': (cmd_ddr_vote, True),
            'ddr-restore': (cmd_ddr_restore, True)}
S3.COMMANDS.update(COMMANDS)


def add_parsers(sub):
    sv = sub.add_parser('ddr-survey', help='DDR: read-only cmd-db BCMs and apps RSC TCSes')
    sv.add_argument('--llcc', action='store_true', help='also read the LLCC CPUSS/GPU slice status (broadcast)')
    sv.add_argument('--ddr-stats', action='store_true', help='also read the rpmh-stats DDR block')
    vt = sub.add_parser('ddr-vote', help='DDR: vote SH0/SH1/MC0/ACV from apps drv-2 (active TCS)')
    vt.add_argument('--write', action='store_true', help='required: this writes an RPMh TCS')
    vt.add_argument('--level', choices=('max',))
    vt.add_argument('--kbps', type=int)
    rs = sub.add_parser('ddr-restore', help='DDR: the explicit restore vote')
    rs.add_argument('--write', action='store_true', help='required: this writes an RPMh TCS')
    rs.add_argument('--kbps', type=int, help=f'restore to this bandwidth (default {RESTORE_KBPS})')
    rs.add_argument('--remove', action='store_true', help='valid = 0: withdraw drv-2\'s vote on these BCMs')
