#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline test of the full speed-bin GPU OPP table (agents/dvfs FAILURE-MODES.md section 9, O1-O5): which OPPs the
fuse allows, which ones every rail can carry (no under-vote), the perf/bw TABLE words for 11 OPPs, the GX_BW_PERF
vote of the top one, the hfi step's refusal of an OPP a rail cannot carry (nothing sent), and the host record's
hfi_opps that Snapintosh's GPU governor reads. The cmd-db is the A16's (tools/test-a16gpu-stage3.py A16_CMD_DB,
stage1-cx-on-4.json; the b12v13 stage-3 record reads the same lists). The hfi step runs through a16gpu_stage3.run
against FakeStage3, as tools/test-a16gpu-stage3.py does.

  python3 tools/test-a16gpu-opps.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402

_spec = importlib.util.spec_from_file_location('stage3_test', TOOLS / 'test-a16gpu-stage3.py')
T3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T3)

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


def a16_cmd_db():
    return {k: {'addr': a, 'aux': aux} for k, (a, aux) in T3.A16_CMD_DB.items()}


def levels(hex_aux):
    blob = bytes.fromhex(hex_aux)
    return [int.from_bytes(blob[i:i + 2], 'little') for i in range(0, len(blob) - 1, 2)]


def aux_of(level_list, width=32):
    blob = b''.join(v.to_bytes(2, 'little') for v in level_list)
    return (blob + bytes(width - len(blob))).hex()


def bw_index_table():
    """O5: for every table size N, the bw row of each OPP 1..N (q1n1's rule); Snapintosh's test-dvfs.c checks
    its own computation against these numbers."""
    rows = {}
    for n in range(1, 12):
        opps = S3.opp_levels(1, n)
        bws = [0] + sorted({bw for _hz, _l, bw, _hw in opps})
        rows[n] = [bws.index(bw) for _hz, _l, bw, _hw in opps]
    return rows


def test_tables():
    db = a16_cmd_db()
    check(levels(T3.A16_CMD_DB['mx.lvl'][1])[:11] == [0, 16, 64, 128, 192, 256, 320, 384, 416, 448, 452],
          'A16 mx.lvl tops out at 452 (TURBO_L4)')
    # O1: the fuse.
    check(R.SPEEDBINS[388] == 1 and S3.R.speed_bin(0x184) == (388, 1), 'fuse 388 (0x184) is speed bin 1')
    check(len(S3.opp_levels(1, 99)) == 12 and S3.opp_levels(1, 99)[-1][0] == 1850000000,
          'bin 1 allows all 12 OPPs, 1850 MHz included (opp-supported-hw 0x3)')
    check(len(S3.opp_levels(2, 99)) == 11 and len(S3.opp_levels(3, 99)) == 9, 'bin 2: 11 OPPs, bin 3: 9 (up to 1350)')
    # O2: the rails.
    safe, refused = S3.rail_safe_opps(db, 1)
    check(len(safe) == 11 and safe[-1][0] == 1700000000 and safe[-1][1] == 452,
          f'bin 1: 11 rail-safe OPPs, the top 1700 MHz at 452 ({len(safe)})')
    check(refused and refused[0][0] == 1850000000 and 'MX dependency' in refused[1] and '452' in refused[1],
          f'1850 MHz (456) refused: {refused and refused[1]}')
    check(S3.rail_safe_opps(db, 3) == (S3.opp_levels(3, 99), None), 'bin 3: every OPP rail-safe, none refused')
    # A cmd-db whose gmxc list stops at 416: Linux would vote the secondary at 416 for 448 (an under-vote); refused.
    short = dict(db, **{'gmxc.lvl': {'addr': '0x300d0', 'aux': aux_of([0, 16, 56, 64, 128, 192, 224, 256, 320, 384,
                                                                       416])}})
    safe2, refused2 = S3.rail_safe_opps(short, 1)
    check(len(safe2) == 9 and refused2[0][1] == 448 and 'GX secondary' in refused2[1],
          f'gmxc without 448: prefix 9, 1550 refused ({refused2 and refused2[1]})')
    try:
        S3.perf_table(short, 1, 10, 1)
        check(False, 'strict secondary vote accepted an under-vote')
    except S3.Stop as stop:
        check('refused, not under-voted' in str(stop), f'strict secondary: {stop}')
    # The 11-OPP TABLE (a8xx_hfi_send_perf_table): 12 GX rows of 3 words, row 11 = 1700 MHz.
    perf = S3.perf_table(db, 1, 11, 1)
    check(perf[:4] == [0, R.HFI_TABLE_GPU_PERF, 12, 3], f'GX entry header: 12 rows, stride 3 ({perf[:4]})')
    row = perf[4 + 11 * 3:4 + 12 * 3]
    check(row == [452 << 16 | 12 << 8 | 11, 0x3FFF << 14 | 10 << 8 | 0xFF, 1700000],
          f'1700 MHz row: gfx[11]=452, gmxc[12]=452, MX dependency mx[10]=452 ({[hex(w) for w in row]})')
    row6 = perf[4 + 6 * 3:4 + 7 * 3]
    check(row6 == S3.perf_table(db, 1, 6, 1)[4 + 6 * 3:4 + 7 * 3], 'rows 1-6 unchanged by the longer table')
    try:
        S3.perf_table(db, 1, 12, 1)
        check(False, '1850 MHz row built')
    except S3.Stop as stop:
        check('MX level 456' in str(stop), f'perf_table(12) stops on the MX dependency: {stop}')
    bw, bws = S3.bw_table(db, 1, 11)
    check(bws == [0, 2136719, 6074219, 12449219, 16500000, 18597657] and bw[0] == 6, f'6 bw rows {bws}')
    vote, name = S3.perf_vote(1, 11, 11)
    check(vote == [1, 11, 5] and '1700 MHz' in name and 'level 452' in name, f'top vote {vote} {name}')
    table = bw_index_table()
    check(table[6] == [1, 2, 3, 3, 4, 4] and table[11] == [1, 2, 3, 3, 4, 4, 4, 4, 5, 5, 5] and table[1] == [1],
          f'bw index per OPP: N=6 {table[6]}, N=11 {table[11]}')
    return table


def test_hfi_step():
    with T3.synthetic_firmware() as fw:
        fake = T3.FakeStage3()
        T3.happy_path(fake, fw, upto='gmu-boot')
        mark = len(fake.log)
        status, out = T3.run3(fake, 'hfi', max_opps=12)
        check(status != 0 and 'Refused rather than under-voted' in str(out.get('stopped')),
              f'--max-opps 12 refused before any message ({out.get("stopped")})')
        doorbell = R.reg('GMU_HOST2GMU_INTR_SET').addr
        rings = lambda since: [e for e in fake.log[since:] if e[0] == 'w' and e[1] == doorbell]
        check(not rings(mark), 'nothing sent: no HFI doorbell')
        host = T3.host_record(fake)
        check(host['hfi_opps'] == 0 and host['hfi_token'] != host['boot_token'], 'no START, hfi_opps still 0')
        mark = len(fake.log)
        status, out = T3.run3(fake, 'hfi', max_opps=11)
        check(len(rings(mark)) == 5, f'--max-opps 11: five HFI messages rung (TABLE..VOTE): {len(rings(mark))}')
        check(status == 0 and out['hfi_tables']['gpu_opps_mhz'][-1] == 1700 and out['hfi_tables']['rail_safe_opps']
              == 11 and out['hfi_tables']['refused']['mhz'] == 1850, f'--max-opps 11 sends the table ({out.get("stopped")})')
        host = T3.host_record(fake)
        check(host['hfi_opps'] == 11 and host['hfi_token'] == host['boot_token'],
              f'host record: hfi_opps 11 with the START of this GMU boot ({host["hfi_opps"]})')
        check(S3.HOST_FIELDS.index('hfi_opps') == 21, 'hfi_opps is host-record word 21 (byte 168, compat/gpu-dvfs.h)')


def main():
    table = test_tables()
    test_hfi_step()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(table) + '\n')
    print(f'PASS: {checks} GPU OPP table checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
