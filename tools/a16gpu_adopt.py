# SPDX-License-Identifier: MIT
"""Stage 4 hand-off: prove, read-only, that the GPU is "CP idle after cp-fence" and write the record
Snapintosh's monitor adopts (docs/A16-ADRENO-STAGE4.md; Snapintosh probes/adreno.py, compat/adreno.h).

Power stays with q1n1: the GMU, its RPMh votes, OOB GPU_SET and the CP this tool started keep running
while macOS boots under Snapintosh's monitor. The monitor never writes the GMU, GPUCC or RSCC; it
fences on the ring cp-fence left, forwards INTID 0x14C, mirrors a virtual DART into CB0 root
entries 1..3 (GPU VA 1..4 GiB, from the `snap_smmu` pool in this arena) and halts GX-side DMA before
the guest's memory is handed back. This command writes nothing to the GPU or the arena.

  a16gpu.py adopt --record FILE     # then SNAP_ADRENO_ADOPT=FILE for the Snapintosh launch

--va-wide (after `cp-fence --va-wide`, for a Snapintosh ADRENO_VA_WIDE monitor): CB0 must have T0SZ 30, and root
entries 1..15 must be empty; that monitor mirrors GPU VA 1..16 GiB from tables in its own memory, not the arena's
`snap_smmu` pool (the record still names the pool; the wide monitor never uses it).

Refuses (exit 2) unless every Stage 3 gate still holds from reads: the arena, a GMU booted from it, OOB
GPU_SET acknowledged for this boot and GX on, the CP cp-fence initialised idle at the record's WPTR with
room for the monitor's fences, CB0 and the stream table as cp-fence left them, no SMMU or error
interrupt recorded, no framebuffer rows or render window mapped, and CB0 root entries 1..3 empty.
"""
from __future__ import annotations

import json
import struct

import adreno_x2 as R
import a16gpu_stage3 as S3
from a16gpu_stage3 import Stop, hx

SCHEMA = 'q1n1-adreno-adopt-v1'
MONITOR_FENCES = 64                 # CP_MEM_WRITE (4 dwords) the monitor may queue before a quiesce


def cmd_adopt(s, out, args):
    import a16gpu_p6p7 as M
    S3.prologue(s, out)
    host = S3.arena_prove(s, out)
    s.step = 'AD1'
    S3.gmu_running_proven(s, out, host)
    if host['oob_token'] != host['boot_token']:
        raise Stop('AD1', 'OOB GPU_SET was not acknowledged for this GMU boot: run oob and cp-fence first')
    status, on = S3.gx_on_proven(s, 'OOB GPU_SET held for this boot')
    if not on:
        raise Stop('AD1', f'GMU_PWR_CLK_STATUS {status:#x}: GX is off')
    s.step = 'AD2'
    wide = getattr(args, 'va_wide', False)
    wptr = M.p67_ready(s, out, host, wide)    # tokens, SQE, ring pointers, RBBM idle, CB0, streams, faults
    # review: a GX-side GBIF halt (a Snapintosh quiesce, or a hang recovery) holds until the next hw_init;
    # the monitor's fence and any guest ring would stall behind it.
    halt, ack = s.rd('RBBM_GBIF_HALT'), s.rd('RBBM_GBIF_HALT_ACK')
    if halt or ack & 1:
        raise Stop('AD2', f'RBBM_GBIF_HALT {halt:#x} / RBBM_GBIF_HALT_ACK {ack:#x}: GX-side GPU DMA is halted '
                          '(a previous Snapintosh quiesce): quiesce --write and redo cp-fence')
    # review: the CX AHB timeouts (cp-fence C3, KGSL) are what turn a stuck GPU register access into a bus
    # error instead of a NoC timeout; the guest reaches GPU registers directly from here on, so re-prove
    # them. CX_MISC is read-only to the guest, so it cannot disarm them.
    hub_hz = R.HUB_HZ.get(s.rd('GPU_CC_HUB_CFG_RCGR') & R.RCG_CFG_SRC_MASK)
    want = R.ahb_timeout_cntl(hub_hz) if hub_hz else None
    ahb = [s.rd('CX_MISC_CX_AHB_CNTL', n) for n in range(len(R.CX_AHB_PORTS))]
    if want is None or ahb != [want] * len(ahb):
        raise Stop('AD2', f'CX AHB timeouts read {[hx(v) for v in ahb]}, not {hx(want) if want else "a sized value"} '
                          'on every port: the guest would reach GPU registers without the bus-error guard '
                          '(cp-fence without --no-ahb-timeout)')
    ring_dwords = R.ARENA_LAYOUT['cp_rb'][1] // 4
    if wptr + 4 * MONITOR_FENCES >= ring_dwords:
        raise Stop('AD2', f'{ring_dwords - wptr} ring dwords left; the monitor needs {4 * MONITOR_FENCES} '
                          '(the ring never wraps): quiesce and start over')
    s.step = 'AD3'
    if host.get('fb_mapped') or M.fb_mapped_now(s):
        raise Stop('AD3', 'framebuffer rows are mapped in CB0: run blit once more to remove them, or quiesce')
    base, size = s.arena
    tables = S3.build_tables(base, 'gpu')
    root = tables.root
    slots = 1 << (64 - R.CB_T0SZ_WIDE - 30) if wide else 4
    entries = struct.unpack(f'<{slots}Q', s.mem_read(root - base, 8 * slots, 'CB0 root (mirror window)'))
    if any(entries[1:]):
        raise Stop('AD3', f'CB0 root entries 1..{slots - 1} are in use ({[hx(e) for e in entries[1:] if e]}): the '
                          f'monitor mirrors GPU VA 1..{slots} GiB there; quiesce first')
    cb = S3.smmu_read_cb(s, R.GPU_CB)
    ttbr = cb['SMMU_CB_TTBR0'] | cb['SMMU_CB_TTBR0_HI'] << 32
    if ttbr != S3.ttbr_for(tables, R.GPU_CB):
        raise Stop('AD3', f'CB0 TTBR0 {ttbr:#x} is not cp-fence\'s root {S3.ttbr_for(tables, R.GPU_CB):#x}')
    info = s.p.bootinfo()
    rb_off, memptrs_off = R.ARENA_LAYOUT['cp_rb'][0], R.ARENA_LAYOUT['cp_memptrs'][0]
    pool_off, pool_size = R.ARENA_LAYOUT['snap_smmu']
    record = {
        'schema': SCHEMA, 'ready': True,
        'stage_generation': info['stage_generation'], 'gicd': info['gic_distributor'],
        'arena': {'base': base, 'size': size},
        'cb0': {'ttbr0': ttbr, 'tcr': cb['SMMU_CB_TCR'], 'sctlr': cb['SMMU_CB_SCTLR']},
        'ring': {'pa': base + rb_off, 'va': R.CP_RB_VA, 'dwords': ring_dwords, 'wptr': wptr},
        'memptrs': {'pa': base + memptrs_off, 'va': R.CP_MEMPTRS_VA},
        'fence': {'pa': base + memptrs_off + R.MONITOR_FENCE_OFF, 'va': R.CP_MEMPTRS_VA + R.MONITOR_FENCE_OFF},
        'pool': {'pa': base + pool_off, 'size': pool_size},
        'tz_init_done': host['tz_ok'] == 1, 'cx_ahb_cntl': want,
        'tokens': {k: host[k] for k in ('boot_token', 'hfi_token', 'oob_token', 'cp_token', 'hwinit_token')},
        'gmu_pwr_clk_status': status,
    }
    out['adopt'] = {k: (hx(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in record.items()
                    if not isinstance(v, dict)}
    out['adopt'].update({k: {kk: hx(vv) for kk, vv in v.items()} for k, v in record.items() if isinstance(v, dict)})
    if args.record:
        with open(args.record, 'w') as f:
            json.dump(record, f, indent=1)
            f.write('\n')
        out['adopt']['record'] = args.record
    out['order'].append('adopt_ready')


COMMANDS = {'adopt': (cmd_adopt, False)}
S3.COMMANDS.update(COMMANDS)


def add_parsers(sub):
    a = sub.add_parser('adopt', help='Stage 4: prove the idle CP read-only and write the Snapintosh adoption record')
    a.add_argument('--record', help='write the adoption record (JSON) here: SNAP_ADRENO_ADOPT for the launch')
    a.add_argument('--va-wide', action='store_true',
                   help='CB0 from `cp-fence --va-wide` (T0SZ 30) for a Snapintosh ADRENO_VA_WIDE monitor')
