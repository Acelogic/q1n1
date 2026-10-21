#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for the DDR/interconnect votes (tools/a16gpu_ddr.py: ddr-survey, ddr-vote, ddr-restore).

Runs the real commands through a16gpu_stage3.run against FakeStage3 (tools/test-a16gpu-stage3.py): the A16's
cmd-db with its recorded BCM aux words (installed-fresh-20261020-v16-gmu.json: SH0, MC0, ACV, CN0; SH1 is
INFERRED here: an enable-mask BCM in its own VCD, as glymur.c describes it), and the apps RSC drv-2 model that
completes active-TCS commands and keeps RPMh's one vote per resource. Checks the failure modes of
agents/dvfs/FAILURE-MODES.md D1-D8 and D10's command shapes.

  python3 tools/test-a16gpu-ddr.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_ddr as D  # noqa: E402

_spec = importlib.util.spec_from_file_location('stage3_test', TOOLS / 'test-a16gpu-stage3.py')
T3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T3)

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


# The A16's own cmd-db rows (PROVEN, v16 gmu record), plus SH1 (INFERRED address/aux).
ARC = {
    'gfx.lvl': ('0x30050', '0000380040008000c000e000000140018001a001c001c401c801d00100000000'),
    'gmxc.lvl': ('0x300d0', '00001000380040008000c000e000000140018001a001c001c401c801d0010000'),
    'mx.lvl': ('0x30010', '0000100040008000c000000140018001a001c001c40100000000000000000000'),
    'cx.lvl': ('0x30000', '0000100040008000c000000140018001a001c001c40100000000000000000000'),
}
BCM = {
    'MC0': ('0x50000', '809fd50030000000'),    # unit 14000000, width 48, vcd 0
    'SH0': ('0x50004', 'c0acd800c0000100'),    # unit 14200000, width 192, vcd 1
    'CN0': ('0x50030', 'e022020004000500'),    # unit 140000, width 4, vcd 5
    'SH1': ('0x5000c', '0100000000000200'),    # INFERRED: enable mask, vcd 2
    'ACV': ('0x50068', '0100000000000d00'),    # unit 1, width 0, vcd 13
}


def cmd_db_blob(bcm=BCM):
    """Two slaves (3: ARC, 5: BCM) in cmd-db.c's layout: header, eight 16-byte rsc_hdrs, checksum word, then
    every entry (24 bytes) and one shared aux area; each slave's header_offset points at its first entry and
    its data_offset at the aux area, entry offsets are relative to that area."""
    groups = [(3, ARC), (5, bcm)]
    total = sum(len(rows) for _s, rows in groups)
    header = struct.pack('<I4s', 1, R.CMD_DB_MAGIC)
    entries, auxes, before = b'', b'', 0
    for slv, rows in groups:
        header += struct.pack('<5H3H', slv, 24 * before, 24 * total, len(rows), 1, 0, 0, 0)
        before += len(rows)
        for name, (addr, aux) in rows.items():
            raw = bytes.fromhex(aux)
            entries += name.encode().ljust(8, b'\0') + bytes(8) + struct.pack('<IHH', int(addr, 16), len(raw),
                                                                             len(auxes))
            auxes += raw
    header += bytes(16 * (8 - len(groups))) + bytes(8)
    return (header + entries + auxes).ljust(R.CMD_DB[1], b'\0')


class DdrFake(T3.FakeStage3):
    def __init__(self, llcc=None, stats=None, **kw):
        super().__init__(**kw)
        self.llcc = llcc or {}
        self.stats = stats or {}

    def _read(self, addr):
        block = R.block_of(addr)
        if block and block.name == 'LLCC_BCAST':
            return self.llcc.get(addr, 0)
        if block and block.name == 'RPMH_STATS':
            return self.stats.get(addr, 0)
        return super()._read(addr)

    def _write(self, addr, value):
        block = R.block_of(addr)
        if block and block.name in ('LLCC_BCAST', 'RPMH_STATS'):
            self.violate('write', addr, f'{block.name} is read-only for ddr-survey')
            return
        return super()._write(addr, value)


def fresh(**kw):
    T3.cmd_db_blob = cmd_db_blob
    return DdrFake(**kw)


def run(fake, command, **kw):
    return S3.run(fake, command, S3.Args(**kw))


def writes(fake, block):
    return [(a, v) for o, a, v in fake.log if o == 'w' and R.block_of(a) and R.block_of(a).name == block]


def no_violation(fake, label):
    check(not fake.violations, f'{label}: no model violation ({fake.violations[:3]})')


def test_cmd_db_and_math():
    entries = D.cmd_db_entries(cmd_db_blob())
    check(entries['MC0']['slave'] == 5 and entries['MC0']['addr'] == '0x50000' and entries['gfx.lvl']['slave'] == 3,
          'cmd-db: both slaves parsed with their own addresses')
    check(a16gpu.parse_cmd_db(cmd_db_blob())['gpu']['SH0']['addr'] == '0x50004', 'Stage 1 parser reads the same blob')
    bcms = {n: dict(entries[n], **S3.aux_bcm(entries[n])) for n in D.DDR_BCMS}
    check(bcms['SH0']['unit'] == 14200000 and bcms['SH0']['width'] == 192 and bcms['SH0']['vcd'] == 1,
          'SH0 aux: unit 14200000, width 192, vcd 1 (A16 cmd-db)')
    check(bcms['MC0']['unit'] == 14000000 and bcms['MC0']['width'] == 48 and bcms['MC0']['vcd'] == 0,
          'MC0 aux: unit 14000000, width 48, vcd 0')
    # D2 golden: the GMU's OPP-1 BW row for 2136719 kBps is SH0 1805 / MC0 1831 (A16-ADRENO-STAGE3.md H2).
    peak = D.plan_votes(bcms, kbps=D.RESTORE_KBPS)
    check(peak['SH0'][1] == 1805 and peak['MC0'][1] == 1831, f'peak votes = the GMU row (got {peak})')
    check(peak['SH0'][0] == 1805 // 12 or peak['SH0'][0] == D.bcm_div(D.bcm_div(D.RESTORE_KBPS * 192, 16 * 12) * 1000,
                                                                      14200000),
          'avg vote divides by the node channels (bcm_aggregate)')
    gpu_row = S3.bw_votes({k: v for k, v in entries.items()}, D.RESTORE_KBPS)
    check([S3.bcm_cmd(False, True, 0, 0) & 0 or w & 0x3FFF for w in gpu_row[:2]] == [1805, 1831],
          'a16gpu_stage3.bw_votes agrees on the same cmd-db')
    mx = D.plan_votes(bcms, level='max')
    check(mx['SH0'] == (0x3FFF, 0x3FFF) and mx['MC0'] == (0x3FFF, 0x3FFF) and mx['SH1'] == (0, 1)
          and mx['ACV'] == (0, 8), 'max: SH0/MC0 saturated, SH1 BIT(0), ACV BIT(3) (glymur.c)')
    huge = D.plan_votes(bcms, kbps=64_000_000)
    check(all(v <= 0x3FFF for x in huge.values() for v in x), 'votes clamp at 0x3FFF')
    cmds = D.commands(bcms, mx)
    names = [n for n, _a, _d in cmds]
    check(names == ['MC0', 'SH0', 'SH1', 'ACV'], f'sorted by VCD (bcm-voter list_sort): {names}')
    check(all(d & R.BCM_COMMIT for _n, _a, d in cmds), 'every VCD differs here: commit on each')
    words = {n: d for n, _a, d in cmds}
    check(words['MC0'] == 0x6FFFFFFF and words['SH0'] == 0x6FFFFFFF and words['ACV'] == 0x60000008
          and words['SH1'] == 0x60000001, 'command words as Linux sends them (0x6FFFFFFF, 0x60000008, 0x60000001)')
    same = dict(bcms, SH1=dict(bcms['SH1'], vcd=1))
    names2 = [(n, bool(d & R.BCM_COMMIT)) for n, _a, d in D.commands(same, mx)]
    check(names2 == [('MC0', True), ('SH0', False), ('SH1', True), ('ACV', True)],
          f'two BCMs in one VCD: commit only on its last ({names2})')
    zero = D.commands(bcms, D.plan_votes(bcms, remove=True))
    check(all(not d & R.BCM_VALID and d & R.BCM_COMMIT and not d & 0x0FFFFFFF for _n, _a, d in zero),
          'remove: valid = 0, commit, x = y = 0 (tcs_cmd_gen for a zero vote)')


def test_survey():
    fake = fresh(llcc={0x20401004: 0x11, 0x20601004: 0x11, 0x20409004: 0x0, 0x20609004: 0x0},
                 stats={0x0C30F0B8: D.DDR_STATS_MAGIC, 0x0C30F0BC: 1, 0x0C30F0C0: 0x08A10101,
                        0x0C30F0C4: 7, 0x0C30F0C8: 1000, 0x0C30F0CC: 0})
    status, out = run(fake, 'ddr-survey', llcc=True, ddr_stats=True)
    check(status == 0 and out['ddr']['mode'] == 'survey', f'ddr-survey completes ({out["stopped"]})')
    check(not [e for e in fake.log if e[0] == 'w'], 'ddr-survey writes nothing')
    check(out['ddr']['llcc']['CPUSS']['active_all_banks'] and not out['ddr']['llcc']['GPU']['active_any_bank'],
          'LLCC slice status decoded (CPUSS active, GPU not)')
    e = out['ddr']['ddr_stats']['entries'][0]
    check(e['mhz'] == 0x08A1 and e['plan_index'] == 1 and e['duration'] == 1000, 'DDR stats entry decoded')
    check([p['bcm'] for p in out['ddr']['max_plan']] == ['MC0', 'SH0', 'SH1', 'ACV'], 'survey shows the max plan')
    no_violation(fake, 'survey')
    fake = fresh()
    status, out = run(fake, 'ddr-survey')
    check(status == 0 and 'llcc' not in out['ddr'] and not [e for e in fake.log if R.block_of(e[1]) and
                                                            R.block_of(e[1]).name in ('LLCC_BCAST', 'RPMH_STATS')],
          'without --llcc/--ddr-stats those blocks are not read')


def test_vote_and_restore():
    fake = fresh()
    status, out = run(fake, 'ddr-vote', level='max')
    check(status == 0, f'ddr-vote --level max completes ({out["stopped"]})')
    sent = {p['bcm']: int(p['data'], 16) for p in out['ddr']['sent']}
    check(sent == {'MC0': 0x6FFFFFFF, 'SH0': 0x6FFFFFFF, 'SH1': 0x60000001, 'ACV': 0x60000008},
          f'max vote words ({out["ddr"]["sent"]})')
    check(fake.rpmh.get(0x50000) == 0x6FFFFFFF and fake.rpmh.get(0x50004) == 0x6FFFFFFF,
          'RPMh model holds drv-2\'s MC0 and SH0 votes')
    tcs = writes(fake, 'APPS_RSC')
    enables = [v for a, v in tcs if a == R.REGS['RSC_TCS_CMD_ENABLE_V3'].at(0)]
    check(enables and enables[0] == 0xF and enables[-1] == 0, 'TCS 0: four commands enabled, disarmed after')
    msgids = [v for a, v in tcs if R.REGS['RSC_TCS_CMD_MSGID_V3'].at(0) <= a < R.REGS['RSC_TCS_CMD_MSGID_V3'].at(0) + 24 * 4
              and (a - R.REGS['RSC_TCS_CMD_MSGID_V3'].at(0)) % 24 == 0]
    check(msgids and all(v == 0x10108 for v in msgids), 'MSGID = write with response (0x10108)')
    check(not writes(fake, 'GMU') and not writes(fake, 'GPU'), 'no GPU or GMU access')
    no_violation(fake, 'vote')
    status, out = run(fake, 'ddr-restore')
    check(status == 0, f'ddr-restore completes ({out["stopped"]})')
    sent = {p['bcm']: (p['x'], p['y'], p['valid']) for p in out['ddr']['sent']}
    check(sent['SH0'] == (1805, 1805, True) and sent['MC0'] == (1831, 1831, True) and sent['ACV'] == (0, 8, True)
          and sent['SH1'] == (0, 1, True), f'restore = the GMU OPP-1 row, x = y, masks on ({sent})')
    check(fake.rpmh[0x50000] & 0x3FFF == 1831, 'RPMh model now holds the restore vote')
    status, out = run(fake, 'ddr-restore', remove=True)
    check(status == 0 and all(not p['valid'] for p in out['ddr']['sent']), 'restore --remove: valid = 0')
    status, out = run(fake, 'ddr-vote', kbps=8_000_000)
    v = {p['bcm']: (p['x'], p['y']) for p in out['ddr']['sent']}
    check(status == 0 and v['MC0'][1] == D.bcm_div(D.bcm_div(8_000_000 * 48, 4) * 1000, 14000000),
          f'--kbps: Linux peak math ({v})')
    status, out = run(fake, 'ddr-vote')
    check(status == 2 and 'exactly one' in out['stopped']['reason'], 'ddr-vote needs --level or --kbps')
    status, out = run(fake, 'ddr-vote', level='max', kbps=1)
    check(status == 2, 'ddr-vote refuses both')


def test_failures():
    # D3: a visible (wake TCS) higher vote is kept.
    fake = fresh()
    fake._tcs_set(5, 'CMD_ENABLE', 1)
    fake._tcs_set_cmd(5, 0, 'ADDR', 0x50000)
    fake._tcs_set_cmd(5, 0, 'DATA', 0x20000000 | 0x3000 << 14 | 0x3000)
    status, out = run(fake, 'ddr-vote', kbps=2_000_000)
    mc0 = next(p for p in out['ddr']['sent'] if p['bcm'] == 'MC0')
    check(status == 0 and mc0['x'] == 0x3000 and mc0['y'] == 0x3000 and out['ddr']['kept_higher'],
          'a higher visible vote is never lowered (D3)')
    # D4: busy active TCS: refused, nothing written to the TCS.
    fake = fresh(rsc_busy=True)
    status, out = run(fake, 'ddr-vote', level='max')
    check(status == 2 and 'busy' in out['stopped']['reason'] and not [a for a, v in writes(fake, 'APPS_RSC')
                                                                       if a == R.REGS['RSC_TCS_CMD_ENABLE_V3'].at(0)],
          'busy active TCS: refused before any command is enabled (D4)')
    # D1: a cmd-db without SH1 (or any BCM) stops before the RSC.
    fake = fresh()
    T3.cmd_db_blob = lambda: cmd_db_blob({k: v for k, v in BCM.items() if k != 'SH1'})
    status, out = run(fake, 'ddr-vote', level='max')
    check(status == 2 and out['stopped']['step'] == 'DD0' and not writes(fake, 'APPS_RSC'),
          'missing BCM in cmd-db: stop at DD0, nothing sent (D1)')
    # D5: no completion: the TCS is disarmed, the record stops.
    fake = fresh()

    def never(addr, value, orig=fake._rsc_write):
        key = fake._rsc_decode(addr)
        if key[0] == 'tcs' and key[2] == 'CONTROL' and value & R.TCS_AMC_MODE_TRIGGER:
            fake.rsc[key] = value          # triggered, never completes
            return
        return orig(addr, value)
    fake._rsc_write = never
    status, out = run(fake, 'ddr-vote', level='max')
    tcs = writes(fake, 'APPS_RSC')
    check(status == 2 and out['stopped']['step'] == 'DD2'
          and tcs[-1] == (R.REGS['RSC_IRQ_CLEAR'].addr, 1)
          and (R.REGS['RSC_TCS_CMD_ENABLE_V3'].at(0), 0) in tcs, 'no completion: stop, TCS disarmed (D5)')


def test_cli():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    S3.add_parsers(sub)
    args = parser.parse_args(['ddr-vote', '--write', '--level', 'max'])
    check(args.command == 'ddr-vote' and args.level == 'max' and args.write, 'CLI: ddr-vote --write --level max')
    args = parser.parse_args(['ddr-restore', '--write', '--remove'])
    check(args.remove and args.kbps is None, 'CLI: ddr-restore --remove')
    args = parser.parse_args(['ddr-survey', '--llcc'])
    check(args.llcc and not args.ddr_stats, 'CLI: ddr-survey --llcc')
    check(S3.COMMANDS['ddr-vote'][1] and S3.COMMANDS['ddr-restore'][1] and not S3.COMMANDS['ddr-survey'][1],
          'vote/restore write; survey does not')


def main():
    for test in (test_cmd_db_and_math, test_survey, test_vote_and_restore, test_failures, test_cli):
        test()
    print(f'PASS: {checks} a16gpu DDR model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
