#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Adreno X2-90, Stage 1: identify the GPU and power its CX side through q1n1.

  python3 tools/a16gpu.py plan                   # the ordered steps; touches nothing
  python3 tools/a16gpu.py guard                  # the running stage's SError guard; no GPU access
  python3 tools/a16gpu.py survey                 # READ-ONLY
  python3 tools/a16gpu.py cx-on --write          # survey again, CX power-on, identify

Stage 3 (tools/a16gpu_stage3.py, docs/A16-ADRENO-STAGE3.md, RUNBOOK): arena, status, rsc-survey,
gmu-smmu, gmu-boot, hfi, oob, cp-fence and quiesce -- each needs --write when it writes.

Subcommands run in a fixed order and each one repeats the read-only steps
before it, so cx-on cannot skip the survey:

  S0 cmd-db   EFI memory map: is 0x81c60000 still reserved; parse the RPMh cmd-db
  S1 GIC      distributor state of the GPU/GMU/SMMU interrupts
  S2 GCC      GPU BCR, CFG_AHB, GEMNOC, GPLL0 branches, vote 0x162038 -> proves GPUCC reachable
  S3 GPUCC    CX GDSC and branch status -> proves (or not) the CX gates
  S4 FREQ     CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS 0x03d9f014, the first GPU-block read,
              only once the critical GPUCC CBCRs read enabled and the CX GDSC reads on
              and not under HW control: survey skips it unless firmware left all of
              that on; cx-on reads it right after P4
  P1-P7       cx-on only: GCC votes, critical CBCRs (P3), the shared GMU/HUB/FF RCGs
              parked on XO (P5, before the GDSC as clk_rcg2_shared_init does), CX GDSC
              (P4, poll 0x03d99084 bit 16), the GMU clock set incl. GEMNOC in DT order
              (P6), the SMMU vote clock (P7). There is no P2: GEMNOC moved to P6.
  S5 ID       slice mask 0x03d9e21c, TCM_RET_CNTL 0x03d9e0e4, SMMU sCR0/IDR0/1/2/7,
              every SMR/S2CR -- in survey only when firmware left the gates on and
              the CX GDSC is not under HW control

Every access goes through Session: the register must be named in
tools/adreno_x2.py, its gate must already be proven by an earlier read, and a
GX-domain register is refused before anything is sent. Every poll is bounded
in reads and in time. The running q1n1 must have the SError guard
(P_Q1N1_SERROR): the tool arms strict mode, so an access that raises or finds
an SError fails at once and the tool stops with the record. Output is JSON
(default build/a16-gpu/stage1-<command>-<time>.json).

Exit status: 0 done, 2 an abort criterion stopped the run (state recorded),
3 SError or synchronous abort, 4 the stage has no SError guard, 5 the target
stopped answering mid-run (treat as a possible bus hang), 6 the tool refused an
access its own rules forbid (a bug in this tool).
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402

_spec = importlib.util.spec_from_file_location('q1n1proxy', TOOLS / 'q1n1proxy.py')
q1n1proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q1n1proxy)

MARKER32 = q1n1proxy.GUARD_MARKER & 0xFFFFFFFF
# Linux budgets (gdsc.c 2000 us, clk-branch.c 200 x 1 us, clk-rcg2.c 500 us) are
# shorter than one proxy round trip, so a read count bounds each poll, and a
# wall-clock budget bounds a slow link.
POLL_LIMITS = {'gdsc': 20, 'branch': 10, 'rcg': 10}
POLL_BUDGET_S = {'gdsc': 1.0, 'branch': 0.5, 'rcg': 0.5}
EFI_TYPES = ('Reserved', 'LoaderCode', 'LoaderData', 'BootCode', 'BootData', 'RuntimeCode',
             'RuntimeData', 'Conventional', 'Unusable', 'ACPIReclaim', 'ACPINVS', 'MMIO',
             'MMIOPort', 'PalCode', 'Persistent')
FREE_AFTER_EBS = {'LoaderCode', 'LoaderData', 'BootCode', 'BootData', 'Conventional'}

GCC_SURVEY = ('GCC_GPU_BCR', 'GCC_GPU_CFG_AHB_CBCR', 'GCC_QMIP_GPU_AHB_CBCR', 'GCC_GPU_GEMNOC_GFX_CBCR',
              'GCC_GPU_GPLL0_CLK_SRC_CBCR', 'GCC_GPU_GPLL0_DIV_CLK_SRC_CBCR', 'GCC_APCS_CLOCK_BRANCH_ENA_VOTE')
GPUCC_SURVEY = ('GPU_CC_CX_GDSCR', 'GPU_CC_CX_CFG_GDSCR', 'GPU_CC_CX_GDS_HW_CTRL',
                'GPU_CC_CB_CBCR', 'GPU_CC_CXO_AON_CBCR', 'GPU_CC_RSCC_XO_AON_CBCR',
                'GPU_CC_AHB_CBCR', 'GPU_CC_CX_GMU_CBCR', 'GPU_CC_CXO_CBCR', 'GPU_CC_HUB_CX_INT_CBCR',
                'GPU_CC_RSCC_HUB_AON_CBCR', 'GPU_CC_GPU_SMMU_VOTE_CBCR', 'GPU_CC_HUB_AON_CBCR',
                'GPU_CC_MEMNOC_GFX_CBCR', 'GPU_CC_SLEEP_CBCR',
                'GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CFG_RCGR', 'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR',
                'GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CFG_RCGR', 'GPU_CC_PLL0_MODE')
# gpucc-glymur.c clk_rcg2_shared_ops RCGs, parked on XO (CFG = 0) at clock registration by
# clk_rcg2_shared_init -- before anything can enable the CX GDSC ("A GDSC can wedge if it is
# turned on and the RCG is stuck on", clk-rcg2.c).
SHARED_RCGS = (('GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CFG_RCGR'), ('GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR'),
               ('GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CFG_RCGR'))
CRITICAL_CBCRS = ('GPU_CC_CB_CBCR', 'GPU_CC_CXO_AON_CBCR', 'GPU_CC_RSCC_XO_AON_CBCR')
# glymur.dtsi gmu clocks, in DT order (clk_bulk_prepare_enable enables in this order).
GMU_CLOCKS = ('GPU_CC_AHB_CBCR', 'GPU_CC_CX_GMU_CBCR', 'GPU_CC_CXO_CBCR', 'GCC_GPU_GEMNOC_GFX_CBCR',
              'GPU_CC_HUB_CX_INT_CBCR', 'GPU_CC_RSCC_HUB_AON_CBCR')
SMMU_CLOCKS = ('GPU_CC_GPU_SMMU_VOTE_CBCR',)

PLAN = (
    ('S0', 'AO', 'bootinfo + EFI memory map (q1n1 RAM); cmd-db 0x81c60000 reserved? parse gfx/gmxc/mx/cx.lvl, SH0/MC0/ACV'),
    ('S1', 'AO', 'GICD_CTLR/TYPER; group/enable/pending/config/route of INTIDs 0x14c 0x14d 0x150 0x151 0x2c2 0x2c6'),
    ('S2', 'AO', 'GCC 0x171000 BCR, 0x171004 CFG_AHB, 0x171008 QMIP, 0x171010 GEMNOC, 0x171024/0x17102c GPLL0 '
                 'branches, 0x162038 vote. Stop unless BCR released and CFG_AHB running.'),
    ('S3', 'CX', 'GPUCC status: CX GDSCR/CFG_GDSCR/HW_CTRL, 12 CBCRs, GMU+HUB RCGs, PLL0 mode. Decides the '
                 'cx_gmu and cx_smmu gates from what firmware left on.'),
    ('P1', 'AO', 'cx-on: 0x162038 |= GPLL0|GPLL0_DIV votes; wait for 0x171024/0x17102c'),
    ('P3', 'CX', 'cx-on: critical CBCRs CB 0x93a4, CXO_AON 0x9008, RSCC_XO_AON 0x9004 enable (as gpucc probe), '
                 're-read them'),
    ('P5', 'CX', 'cx-on: shared RCGs GMU 0x9318, HUB 0x93f0, FF 0x9474 parked on XO (force ROOT_EN, CFG=0, UPDATE, '
                 'release) unless already XO, BEFORE the CX GDSC (clk_rcg2_shared_init); ROOT_EN released on a '
                 'poll failure too'),
    ('P4', 'CX', 'cx-on: CX GDSC: gdsc_init waits, clear SW_COLLAPSE (always, as gdsc_enable), poll CFG_GDSCR '
                 f'0x03d99084 bit 16 (<= {POLL_LIMITS["gdsc"]} reads), then RETAIN_FF; re-read -> cx_fuse gate'),
    ('S4', 'CX', 'CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS 0x03d9f014 -> speed bin (first GPU-block read), only once the '
                 'critical CBCRs read enabled and the CX GDSC reads on, not under HW control (survey: if firmware '
                 'left that; cx-on: right after P4)'),
    ('P6', 'CX', 'cx-on: GMU clocks in DT order: AHB (HALT_DELAY, no CLK_OFF wait), CX_GMU, CXO, GCC GEMNOC_GFX '
                 '(skipped wait while hw-gated), HUB_CX_INT, RSCC_HUB_AON'),
    ('P7', 'CX', 'cx-on: GPU_SMMU_VOTE 0x7000 enable, wait; re-read GPUCC status -> gates from reads'),
    ('S5', 'CX', 'slice mask 0x03d9e21c (chip id), TCM_RET_CNTL 0x03d9e0e4 (must be 0), SMMU sCR0, IDR0/1/2/7, '
                 'SMR/S2CR[0..NUMSMRG). survey: only if already gated on and the CX GDSC is not under HW control. '
                 'No GX register is ever touched.'),
)


class Stop(Exception):
    """An abort criterion: stop touching the hardware and write what we have."""
    exit_code = 2

    def __init__(self, step, reason, **detail):
        super().__init__(f'{step}: {reason}')
        self.step, self.reason, self.detail = step, reason, detail


class SErrorStop(Stop):
    exit_code = 3


class AbortStop(Stop):
    exit_code = 3


class PollTimeout(Stop):
    pass


class GuardMissing(Stop):
    exit_code = 4


class Refused(Exception):
    """A programming error: the tool asked for an access its own rules forbid."""


def hx(value):
    return f'{value:#010x}'


class Session:
    """The only path to the hardware. Checks, performs and logs every access."""

    def __init__(self, proxy, allow_writes=False, guarded=True, clock=time.monotonic):
        self.p = proxy
        self.allow_writes = allow_writes
        self.guarded = guarded
        self.clock = clock
        self.proven = {'always': 'AO'}
        self.log = []
        self.polls = []
        self.step = 'setup'

    def prove(self, gate, evidence):
        self.proven[gate] = evidence

    def withdraw(self, gate):
        self.proven.pop(gate, None)

    def _check(self, reg, write):
        if reg.domain == 'GX' or reg.gate in ('gx', 'never'):
            raise Refused(f'{reg.name} @ {reg.addr:#010x} is {reg.domain}/{reg.gate}: Stage 1 never touches it')
        if write and ('w' not in reg.access or not self.allow_writes):
            raise Refused(f'write to {reg.name} not allowed (access {reg.access!r}, writes '
                          f'{"on" if self.allow_writes else "off"})')
        if not write and 'r' not in reg.access:
            raise Refused(f'{reg.name} is not readable in Stage 1')
        if reg.gate not in self.proven:
            raise Refused(f'{reg.name} needs gate {reg.gate}; proven: {sorted(self.proven)}')

    def _record(self, op, reg, addr, value=None, **extra):
        entry = {'seq': len(self.log), 'step': self.step, 'op': op, 'reg': reg.name if reg else None,
                 'addr': hx(addr), 'block': reg.block if reg else None, 'domain': reg.domain if reg else 'DRAM'}
        if value is not None:
            entry['value'] = hx(value)
        entry.update(extra)
        self.log.append(entry)

    def _remote_failure(self, problem, op, reg, addr):
        self._record(op, reg, addr, failed=str(problem))
        where = f'{op} {reg.name if reg else "memory"} @ {addr:#010x}'
        if self.guarded and problem.status in (q1n1proxy.S_SERROR, q1n1proxy.ST_XFRERR):
            try:
                state = self.p.serror_state()
            except q1n1proxy.ProxyRemoteError as unreadable:
                # The target answered, so the link is alive: this is not a bus
                # hang (exit 5). A payload that brackets its own record read
                # refuses it in strict mode while an SError stays pending.
                raise SErrorStop(self.step, f'SError at {where}? the record read was refused ({unreadable}); '
                                            'an SError is probably still pending')
            if state['unconsumed'] and not (state['before'] or state['during']):
                raise SErrorStop(self.step, f'SError still pending at {where} (unconsumed: the guard could '
                                            'neither defer nor take it); nothing more is sent',
                                 serror=serror_json(state))
            if state['before'] or state['during']:
                when = q1n1proxy.SERROR_WHEN.get(state['last_when'])
                raise SErrorStop(self.step, f'SError {when} {where}' + (
                    ' (it was already pending: raised by earlier work, this access was not performed)'
                    if when == 'before' else ' (raised across this access)'),
                    serror=serror_json(state))
        if problem.status == q1n1proxy.ST_XFRERR:
            raise AbortStop(self.step, f'{where}: transfer refused (synchronous abort)')
        raise Stop(self.step, f'{where}: proxy error {problem}')

    def rd(self, name, index=0):
        reg = R.REGS[name]
        self._check(reg, write=False)
        addr = reg.at(index)
        try:
            raw = self.p.read32(addr)
        except q1n1proxy.ProxyRemoteError as problem:
            self._remote_failure(problem, 'read', reg, addr)
        value = int(raw) & 0xFFFFFFFF
        if value == MARKER32:
            faults = self.p.get_exc_count()
            if faults:
                self._record('read', reg, addr, value, abort=faults)
                raise AbortStop(self.step, f'read {name} @ {addr:#010x}: synchronous abort (guard marker)')
        self._record('read', reg, addr, value, index=index if reg.count > 1 else None)
        return value

    def wr(self, name, value, index=0):
        reg = R.REGS[name]
        self._check(reg, write=True)
        addr = reg.at(index)
        value &= 0xFFFFFFFF
        try:
            self.p.write32(addr, value)
        except q1n1proxy.ProxyRemoteError as problem:
            self._remote_failure(problem, 'write', reg, addr)
        faults = self.p.get_exc_count()
        self._record('write', reg, addr, value, abort=faults or None)
        if faults:
            raise AbortStop(self.step, f'write {name} @ {addr:#010x}: synchronous abort')

    def update(self, name, mask, bits):
        """regmap_update_bits: read, change only mask, write if it differs."""
        old = self.rd(name)
        new = (old & ~mask) | (bits & mask)
        if new != old:
            self.wr(name, new)
        return new

    def udelay(self, microseconds):
        self.p.udelay(microseconds)
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'udelay', 'us': microseconds})

    def poll(self, name, mask, want, kind):
        limit, deadline = POLL_LIMITS[kind], self.clock() + POLL_BUDGET_S[kind]
        value = attempt = 0
        for attempt in range(1, limit + 1):
            value = self.rd(name)
            if value & mask == want:
                self.polls.append({'reg': name, 'reads': attempt, 'ok': True})
                return value
            if self.clock() > deadline:
                break
        self.polls.append({'reg': name, 'reads': attempt, 'ok': False})
        raise PollTimeout(self.step, f'{name} = {value:#010x}: (value & {mask:#x}) != {want:#x} '
                                     f'after {attempt} reads', reg=name, value=hx(value))

    def dram(self, base, size):
        """Plain memory (bootinfo-described or reserved DRAM), never MMIO."""
        if R.block_of(base) not in (None,) and R.block_of(base).name != 'CMD_DB':
            raise Refused(f'readmem of {base:#x} is MMIO')
        try:
            data = self.p.readmem(base, size)
        except q1n1proxy.ProxyRemoteError as problem:
            self._remote_failure(problem, 'readmem', None, base)
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'readmem', 'addr': hx(base),
                         'size': size, 'domain': 'DRAM'})
        return data


# --- pure helpers ----------------------------------------------------------------------
def parse_efi_map(blob, stride):
    regions = []
    for offset in range(0, len(blob) - 39, stride):
        kind, _pad, phys, _virt, pages, attr = struct.unpack_from('<IIQQQQ', blob, offset)
        regions.append({'type': EFI_TYPES[kind] if kind < len(EFI_TYPES) else kind,
                        'base': phys, 'size': pages << 12, 'attr': attr})
    return regions


def covering(regions, base, size):
    return [{'type': r['type'], 'base': hex(r['base']), 'size': hex(r['size'])}
            for r in regions if r['base'] < base + size and base < r['base'] + r['size']]


def parse_cmd_db(blob):
    """drivers/soc/qcom/cmd-db.c: header {u32 version, u8 magic[4], rsc_hdr[8],
    u32 checksum, u32 reserved}, rsc_hdr {slv_id, header_offset, data_offset, cnt,
    version, reserved[3]} (u16), entry {id[8], priority[2], u32 addr, u16 len,
    u16 offset}. Iteration stops at the first empty slave, as cmd-db.c does."""
    if blob[4:8] != R.CMD_DB_MAGIC:
        return {'valid': False, 'magic': blob[4:8].hex()}
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
    return {'valid': True, 'version': struct.unpack_from('<I', blob, 0)[0], 'count': len(entries),
            'gpu': {key: entries.get(key) for key in R.CMD_DB_GPU_KEYS}}


def decode_smmu(idr0, idr1, idr7):
    return {'s1': bool(idr0 >> 30 & 1), 's2': bool(idr0 >> 29 & 1), 'nested': bool(idr0 >> 28 & 1),
            'stream_match_groups': idr0 & 0xFF, 'context_banks': idr1 & 0xFF,
            's2_only_context_banks': idr1 >> 16 & 0xFF, 'page_size': 0x10000 if idr1 >> 31 else 0x1000,
            'version': f'{idr7 >> 4 & 0xF}.{idr7 & 0xF}'}


def serror_json(state):
    return {'mechanism': q1n1proxy.SERROR_MECHANISMS.get(state['mechanism'], state['mechanism']),
            'strict': bool(state['flags'] & q1n1proxy.SERROR_STRICT), 'checks': state['checks'],
            'before': state['before'], 'during': state['during'], 'refused': state['refused'],
            'unconsumed': state['unconsumed'], 'last_syndrome': hx(state['last_syndrome']),
            'last_when': q1n1proxy.SERROR_WHEN.get(state['last_when']), 'last_opcode': hex(state['last_opcode']),
            'last_address': hex(state['last_address'])}


def branch_state(reg, value):
    enabled, off = bool(value & R.CBCR_CLK_ENABLE), bool(value & R.CBCR_CLK_OFF)
    if reg.block == 'GCC' and value & R.CBCR_HW_CTL:
        return 'hw-gated'
    if reg.halt == 'HALT_DELAY':
        # REVIEW: CLK_OFF of a HALT_DELAY branch is not a status; only the enable is.
        return 'enabled' if enabled else 'disabled'
    return 'running' if not off else ('enabled-not-running' if enabled else 'off')


def running(reg, value):
    return branch_state(reg, value) in ('running', 'enabled', 'hw-gated')


# --- steps ----------------------------------------------------------------------------------
def step_cmd_db(s, out):
    s.step = 'S0'
    info = s.p.bootinfo()
    out['stage'] = {k: hex(info[k]) for k in ('image_base', 'stage_generation', 'current_el',
                                             'gic_distributor', 'memory_map')}
    regions = parse_efi_map(s.dram(info['memory_map'], info['memory_map_size']), info['memory_map_stride'])
    hits = covering(regions, *R.CMD_DB)
    result = {'efi_regions': hits, 'zap_carveout_regions': covering(regions, *R.GPU_ZAP_CARVEOUT)}
    free = [r for r in hits if r['type'] in FREE_AFTER_EBS]
    result['reserved'] = bool(hits) and not free
    if not hits:
        # REVIEW-2: not in the map at all -> no evidence it is DRAM this core may read.
        result['skipped'] = 'cmd-db is not described by the EFI memory map'
    elif free:
        # UEFI could have handed this memory out, so what is there now proves nothing.
        result['skipped'] = 'cmd-db overlaps memory UEFI frees at ExitBootServices'
    else:
        # DRAM that Linux maps (cmd-db.c memremap) and AOP only writes at boot. A
        # region missing from the map may be unmapped: that is a synchronous
        # translation fault, which the proxy guard turns into ST_XFRERR.
        try:
            result.update(parse_cmd_db(s.dram(*R.CMD_DB)))
        except AbortStop as fault:
            result['unreadable'] = fault.reason   # recorded; not an abort criterion for DRAM
    out['cmd_db'] = result
    out['order'].append('cmd_db')


def step_gic(s, out):
    s.step = 'S1'
    # AO: q1n1 itself programs this distributor for its EL2 tick (gicv3.c).
    if int(out['stage']['gic_distributor'], 16) not in (0, R.REGS['GICD_CTLR'].addr):
        raise Stop('S1', f'bootinfo GICD {out["stage"]["gic_distributor"]} is not the registry\'s 0x17000000')
    typer = s.rd('GICD_TYPER')
    lines = 32 * ((typer & 0x1F) + 1)
    result = {'GICD_CTLR': hx(s.rd('GICD_CTLR')), 'GICD_TYPER': hx(typer), 'lines': lines, 'intids': {}}
    for intid in R.SURVEY_INTIDS:
        if intid >= lines:
            result['intids'][hex(intid)] = 'beyond GICD_TYPER lines'
            continue
        word, bit = divmod(intid, 32)
        result['intids'][hex(intid)] = {
            'group1': s.rd('GICD_IGROUPR', word) >> bit & 1,
            'enabled': s.rd('GICD_ISENABLER', word) >> bit & 1,
            'pending': s.rd('GICD_ISPENDR', word) >> bit & 1,
            'edge': s.rd('GICD_ICFGR', intid // 16) >> (2 * (intid % 16) + 1) & 1,
            'irouter_lo': hx(s.rd('GICD_IROUTER_LO', intid)),
        }
    out['gic'] = result
    out['order'].append('gic')


def step_gcc(s, out):
    s.step = 'S2'
    # AO: GCC is always on; these are status/vote registers only (no BCR write).
    values = {name: s.rd(name) for name in GCC_SURVEY}
    decoded = {name: branch_state(R.REGS[name], values[name]) for name in GCC_SURVEY
               if name.endswith('_CBCR')}
    out['gcc'] = {'registers': {n: hx(v) for n, v in values.items()}, 'branches': decoded,
                  'gpu_bcr_asserted': bool(values['GCC_GPU_BCR'] & R.BCR_BLK_ARES)}
    out['order'].append('gcc')
    if values['GCC_GPU_BCR'] & R.BCR_BLK_ARES:
        raise Stop('S2', 'GCC_GPU_BCR holds the GPU subsystem in reset; not released blind')
    if values['GCC_GPU_CFG_AHB_CBCR'] & R.CBCR_CLK_OFF:
        raise Stop('S2', 'GCC_GPU_CFG_AHB_CLK is off: GPUCC is unreachable, nothing past GCC is read')
    s.prove('gpu_cfg_ahb', f'GCC_GPU_CFG_AHB_CBCR={hx(values["GCC_GPU_CFG_AHB_CBCR"])} (CLK_OFF clear), '
                           f'GCC_GPU_BCR={hx(values["GCC_GPU_BCR"])} (released)')
    return values


def step_gpucc(s, out, label='gpucc'):
    s.step = 'S3' if label == 'gpucc' else s.step
    # CX: the GPUCC register file itself, on GCC_GPU_CFG_AHB (proven in S2). These
    # are status registers of clocks and of the GDSC; reading one turns nothing on.
    values = {name: s.rd(name) for name in GPUCC_SURVEY}
    gcc_gemnoc = s.rd('GCC_GPU_GEMNOC_GFX_CBCR')
    gdscr, cfg = values['GPU_CC_CX_GDSCR'], values['GPU_CC_CX_CFG_GDSCR']
    gdsc = gdsc_state(gdscr, cfg)
    branches = {name: branch_state(R.REGS[name], values[name]) for name in GPUCC_SURVEY if name.endswith('_CBCR')}
    branches['GCC_GPU_GEMNOC_GFX_CBCR'] = branch_state(R.REGS['GCC_GPU_GEMNOC_GFX_CBCR'], gcc_gemnoc)
    cfg_rcgr = values['GPU_CC_GMU_CFG_RCGR']
    rcg = {'src_sel': cfg_rcgr >> 8 & 7, 'src_div': cfg_rcgr & R.RCG_CFG_SRC_DIV,
           'root_off': bool(values['GPU_CC_GMU_CMD_RCGR'] & R.RCG_CMD_ROOT_OFF),
           'on_xo': is_xo(cfg_rcgr)}
    out[label] = {'registers': {n: hx(v) for n, v in values.items()}, 'cx_gdsc': gdsc,
                  'branches': branches, 'gmu_rcg': rcg}
    if label == 'gpucc':
        out['order'].append('gpucc')
    prove_critical(s, values)
    evaluate_gates(s, gdsc, values, gcc_gemnoc)
    return values


def prove_critical(s, values):
    """gpucc probe sets CLK_ENABLE on these (qcom_branch_set_clk_en, no halt check)
    before a6xx_gpu_init reads FREQ_LIMIT: that is the gpucc_critical gate.

    A16, 2026-09-25 (build/a16-gpu/stage1-cx-on.json): GPU_CC_CXO_AON_CBCR does
    not latch CLK_ENABLE (0x08000001 written, 0x08000000 read back) yet reads
    CLK_OFF clear before and after, as Linux never checks. So a critical branch
    counts when its enable bit is set or it is proven running (CLK_OFF clear);
    the evidence says which."""
    def state(name):
        value = values[name]
        if value & R.CBCR_CLK_ENABLE and not value & R.CBCR_CLK_OFF:
            return 'enabled+running'
        if value & R.CBCR_CLK_ENABLE:
            return 'enabled'
        if not value & R.CBCR_CLK_OFF:
            return 'running (enable not latched)'
        return 'off'
    evidence = ', '.join(f'{name}={hx(values[name])} {state(name)}' for name in CRITICAL_CBCRS)
    if all(state(name) != 'off' for name in CRITICAL_CBCRS):
        s.prove('gpucc_critical', f'CLK_ENABLE set or running: {evidence}')
    else:
        s.withdraw('gpucc_critical')
    return evidence


def evaluate_gates(s, gdsc, values, gcc_gemnoc):
    """Prove cx_gmu / cx_smmu only from what was just read."""
    regs = R.REGS
    ok = lambda name: running(regs[name], gcc_gemnoc if name.startswith('GCC_') else values[name])
    # REVIEW: under HW control (GDSCR bit 1) the GMU/RSC may collapse the GDSC
    # between this status read and the next access, so "on" proves nothing then.
    # cx-on clears HW_CONTROL in P4 (--take-hw-control); survey leaves S5 alone.
    prove_fuse(s, gdsc, 'S3 reads' if s.step == 'S3' else f'{s.step} reads')
    domain = gdsc['on'] and not gdsc['hw_control']
    ahb = ok('GPU_CC_AHB_CBCR')
    if domain and ahb and all(ok(name) for name in GMU_CLOCKS):
        s.prove('cx_gmu', f'CX GDSC CFG bit16 set, HW_CONTROL clear; running: {", ".join(GMU_CLOCKS)}')
    else:
        s.withdraw('cx_gmu')
    others = [name for name in GMU_CLOCKS if name != 'GPU_CC_AHB_CBCR']
    if 'cx_gmu' in s.proven:
        s.prove('cx_misc', f'cx_gmu: {s.proven["cx_gmu"]}')
    elif getattr(s, 'freq_limit_clean', None) and domain and all(ok(name) for name in others):
        s.prove('cx_misc', f'FREQ_LIMIT {s.freq_limit_clean} read cleanly under the strict SError guard; '
                           f'CX GDSC on, HW_CONTROL clear; running: {", ".join(others)} '
                           f'(GPU_CC_AHB_CBCR is HALT_DELAY and its enable does not latch on the A16)')
    else:
        s.withdraw('cx_misc')
    # The SMMU sits behind the same GPU AHB. With the AHB enable unprovable on
    # the A16, a clean CX_MISC read in this state is the evidence for the path.
    ahb_by_read = 'cx_misc' in s.proven and 'cx_gmu' not in s.proven
    if domain and (ahb or ahb_by_read) and all(ok(name) for name in SMMU_CLOCKS):
        s.prove('cx_smmu', f'CX GDSC CFG bit16 set, HW_CONTROL clear; running: {", ".join(SMMU_CLOCKS)}; '
                           + ('GPU_CC_AHB_CBCR enabled' if ahb else 'GPU AHB path shown by clean CX_MISC reads'))
    else:
        s.withdraw('cx_smmu')


def gdsc_state(gdscr, cfg):
    return {'on': bool(cfg & R.CFG_POWER_UP_COMPLETE), 'pwr_on_bit31': bool(gdscr & R.GDSC_PWR_ON),
            'sw_collapse': bool(gdscr & R.GDSC_SW_COLLAPSE), 'hw_control': bool(gdscr & R.GDSC_HW_CONTROL),
            'retain_ff': bool(gdscr & R.GDSC_RETAIN_FF),
            'power_down_complete': bool(cfg & R.CFG_POWER_DOWN_COMPLETE)}


def prove_fuse(s, gdsc, where):
    """REVIEW-2: FREQ_LIMIT sits in CX_MISC; nothing shows it is outside the CX GDSC,
    and Linux reads it while genpd still holds a boot-on GDSC on. So: critical CBCRs
    enabled AND the CX GDSC on and not under HW control."""
    if 'gpucc_critical' in s.proven and gdsc['on'] and not gdsc['hw_control']:
        s.prove('cx_fuse', f'gpucc_critical, and CX GDSC CFG bit16 set with HW_CONTROL clear ({where})')
    else:
        s.withdraw('cx_fuse')


def is_xo(cfg_rcgr):
    return (cfg_rcgr & R.RCG_CFG_SRC_SEL) == 0 and (cfg_rcgr & R.RCG_CFG_SRC_DIV) in (0, 1)


def step_freq_limit(s, out):
    s.step = 'S4'
    # REVIEW: the first GPU-block read. Linux reads this in a6xx_gpu_init with no
    # GPU runtime-PM vote (a6xx_gpu.c:2613), but only after gpucc probe enabled the
    # critical CBCRs; gate = that state (INFERRED safe). Never read outside it.
    if 'cx_fuse' not in s.proven:
        out['freq_limit'] = {'skipped': 'critical GPUCC CBCRs (CB, CXO_AON, RSCC_XO_AON) not all enabled, or the '
                                        'CX GDSC off or under HW control; cx-on reads it after P4'}
        return
    raw = s.rd('CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS')
    fuse, speed = R.speed_bin(raw)
    out['freq_limit'] = {'raw': hx(raw), 'fuse': fuse, 'speed_bin': speed, 'known_bin': speed is not None,
                         'gate': s.proven['cx_fuse']}
    out['order'].append('freq_limit')
    # A16, 2026-09-25: GPU_CC_AHB_CBCR (HALT_DELAY) does not latch CLK_ENABLE, so
    # cx_gmu can never be proven from it, yet this CX_MISC read completed under
    # the strict SError guard (an SError would have stopped the run before this
    # line). evaluate_gates uses it, with every other GMU clock running, as the
    # evidence for the rest of CX_MISC (gate cx_misc).
    s.freq_limit_clean = hx(raw)


def step_identify(s, out, required):
    s.step = 'S5'
    result = {}
    if 'cx_misc' in s.proven:
        # CX GDSC on with the GMU clock set running (proven from GPUCC reads), or
        # the same CX_MISC block already answered FREQ_LIMIT in this state.
        result['cx_misc_gate'] = s.proven['cx_misc']
        mask = s.rd('CX_MISC_SLICE_ENABLE_FINAL')
        tcm = s.rd('CX_MISC_TCM_RET_CNTL')
        result['cx_misc'] = {'slice_enable_final': hx(mask), 'slices': bin(mask & R.SLICE_MASK).count('1'),
                             'chip_id': hx(R.runtime_chip_id(mask)), 'tcm_ret_cntl': hx(tcm),
                             'tcm_ret_ok': tcm == 0}
        out['order'].append('cx_misc')
    else:
        result['cx_misc'] = {'skipped': 'gate cx_misc not proven (CX GDSC off or under HW control, a GMU clock '
                                        'off, or neither cx_gmu nor a clean FREQ_LIMIT read in this power state)'}
    if 'cx_smmu' in s.proven:
        # CX GDSC on with the SMMU vote clock running (proven from GPUCC reads).
        idr0, idr1, idr2, idr7 = (s.rd(n) for n in ('SMMU_IDR0', 'SMMU_IDR1', 'SMMU_IDR2', 'SMMU_IDR7'))
        scr0 = s.rd('SMMU_sCR0')
        decoded = decode_smmu(idr0, idr1, idr7)
        groups = min(decoded['stream_match_groups'], R.REGS['SMMU_SMR'].count)
        streams = [{'n': n, 'smr': hx(s.rd('SMMU_SMR', n)), 's2cr': hx(s.rd('SMMU_S2CR', n))} for n in range(groups)]
        result['smmu'] = {'sCR0': hx(scr0), 'clientpd': bool(scr0 & 1), 'IDR0': hx(idr0), 'IDR1': hx(idr1),
                          'IDR2': hx(idr2), 'IDR7': hx(idr7), 'decoded': decoded, 'streams': streams}
        out['order'].append('smmu')
    else:
        result['smmu'] = {'skipped': 'gate cx_smmu not proven (CX GDSC off or under HW control, '
                                     'or GPU_SMMU_VOTE is off)'}
    out['identify'] = result
    if required and ('skipped' in result['cx_misc'] or 'skipped' in result['smmu']):
        raise Stop('S5', 'CX power-on finished but a gate is still not proven by the status registers')


SURVEY_STEPS = ('S0', 'S1', 'S2', 'S3', 'S4', 'S5')


def survey(s, out, identify=True, stop_after='S5'):
    """S0..S5. stop_after lets a first contact go one block at a time."""
    last = SURVEY_STEPS.index(stop_after)
    steps = (step_cmd_db, step_gic, step_gcc, step_gpucc, step_freq_limit,
             (lambda s_, o_: step_identify(s_, o_, required=False)) if identify else None)
    for index, step in enumerate(steps[:last + 1]):
        if step:
            step(s, out)
    if last < len(steps) - 1:
        out['stopped_after'] = stop_after


def branch_on(s, name):
    reg = R.REGS[name]
    value = s.update(name, R.CBCR_CLK_ENABLE, R.CBCR_CLK_ENABLE)
    if reg.halt == 'HALT_DELAY':
        s.udelay(10)            # clk_branch_wait: BRANCH_HALT_DELAY -> udelay(10), no status
        return
    if reg.halt == 'CRITICAL':
        return                  # qcom_branch_set_clk_en: no halt check
    if reg.block == 'GCC' and value & R.CBCR_HW_CTL:
        return                  # clk_branch_in_hwcg_mode: CLK_OFF means nothing while hw-gated
    s.poll(name, R.CBCR_CLK_OFF, 0, 'branch')


def park_rcg(s, cmd, cfg_name):
    cfg = s.rd(cfg_name)
    if is_xo(cfg):
        return 'already on XO'
    # clk_rcg2_shared_disable: force the root on, CFG = safe source (XO) << 8,
    # UPDATE, release. The old parent must be running for the switch.
    s.update(cmd, R.RCG_CMD_ROOT_EN, R.RCG_CMD_ROOT_EN)
    try:
        s.poll(cmd, R.RCG_CMD_ROOT_OFF, 0, 'rcg')
        s.wr(cfg_name, 0)
        s.update(cmd, R.RCG_CMD_UPDATE, R.RCG_CMD_UPDATE)
        s.poll(cmd, R.RCG_CMD_UPDATE, 0, 'rcg')
    except PollTimeout as timeout:
        # clk_rcg2_shared_disable clears the force-enable whatever update_config
        # returned: do not leave ROOT_EN forcing a root that never came up. Only
        # after a poll timeout; an SError or abort stops without another access.
        try:
            s.update(cmd, R.RCG_CMD_ROOT_EN, 0)
            timeout.detail['root_en_released'] = True
        except Stop as release:
            timeout.detail['root_en_released'] = f'failed: {release.reason}'
        raise
    s.update(cmd, R.RCG_CMD_ROOT_EN, 0)
    return f'parked on XO from CFG {hx(cfg)}'


def park_shared_rcgs(s, out):
    """clk_rcg2_shared_init parks every shared RCG at registration, before the CX
    GDSC can be enabled: an RCG left stuck on a dead parent can wedge the GDSC."""
    out['rcg_park'] = {}
    for cmd, cfg_name in SHARED_RCGS:
        out['rcg_park'][cmd] = park_rcg(s, cmd, cfg_name)
    out['gmu_rcg_park'] = out['rcg_park']['GPU_CC_GMU_CMD_RCGR']


def cx_on(s, out, take_hw_control=False):
    # S0-S3 as the survey does them. S4 waits for P3: FREQ_LIMIT is read only in
    # the state Linux reads it in (critical CBCRs enabled).
    for step in (step_cmd_db, step_gic, step_gcc, step_gpucc):
        step(s, out)
    gdscr = int(out['gpucc']['registers']['GPU_CC_CX_GDSCR'], 16)
    if gdscr & R.GDSC_HW_CONTROL and not take_hw_control:
        raise Stop('P4', 'CX GDSC is under HW control (GDSCR bit 1): someone else may own it; '
                         'rerun with --take-hw-control to clear it as gdsc_init does')

    s.step = 'P1'
    # Gates proven from the S3 reads go stale once cx-on writes; each is re-proven
    # from fresh reads before it is used again.
    for gate in ('cx_fuse', 'cx_gmu', 'cx_smmu'):
        s.withdraw(gate)
    s.update('GCC_APCS_CLOCK_BRANCH_ENA_VOTE', R.GCC_VOTE_GPU_GPLL0 | R.GCC_VOTE_GPU_GPLL0_DIV,
             R.GCC_VOTE_GPU_GPLL0 | R.GCC_VOTE_GPU_GPLL0_DIV)
    for name in ('GCC_GPU_GPLL0_CLK_SRC_CBCR', 'GCC_GPU_GPLL0_DIV_CLK_SRC_CBCR'):
        if not s.rd(name) & R.CBCR_HW_CTL:
            s.poll(name, R.CBCR_CLK_OFF, 0, 'branch')
    out['order'].append('gcc_votes')

    # REVIEW: no P2. GEMNOC_GFX is enabled in P6 with the other GMU clocks, after
    # the CX GDSC, in DT order (gmu-master.c:1324-1331): a branch into a collapsed
    # domain may report CLK_OFF, which would be a false abort here.

    s.step = 'P3'
    for name in CRITICAL_CBCRS:
        branch_on(s, name)
    out['order'].append('critical_cbcrs')
    evidence = prove_critical(s, {name: s.rd(name) for name in CRITICAL_CBCRS})
    if 'gpucc_critical' not in s.proven:
        raise Stop('P3', f'critical GPUCC CBCRs are not running after P3: {evidence}')

    # REVIEW-2: Linux parks the shared RCGs (clk_rcg2_shared_init) when gpucc
    # registers its clocks, which is before any consumer can enable the CX GDSC.
    # The step keeps its old name; it now runs before P4.
    s.step = 'P5'
    s.withdraw('cx_fuse')
    park_shared_rcgs(s, out)
    out['order'].append('gmu_rcg')

    s.step = 'P4'
    # gdsc_init: HW trigger and SW override off, gpu_cc_cx_gdsc's wait values.
    mask = R.GDSC_HW_CONTROL | R.GDSC_SW_OVERRIDE | R.GDSC_EN_REST_WAIT | R.GDSC_EN_FEW_WAIT | R.GDSC_CLK_DIS_WAIT
    s.update('GPU_CC_CX_GDSCR', mask, R.CX_GDSC_WAITS)
    # gdsc_enable -> gdsc_toggle_logic: clear SW_COLLAPSE, 1 us (gds_hw_ctrl), poll.
    # Always, as Linux does, even when the GDSC reads on: one that was on only
    # through HW_CONTROL still has SW_COLLAPSE set and starts to collapse as soon
    # as the update above clears HW_CONTROL. update() writes only if the bit is set.
    s.update('GPU_CC_CX_GDSCR', R.GDSC_SW_COLLAPSE, 0)
    s.udelay(1)
    s.poll('GPU_CC_CX_CFG_GDSCR', R.CFG_POWER_UP_COMPLETE, R.CFG_POWER_UP_COMPLETE, 'gdsc')
    s.udelay(1)
    gdscr = s.update('GPU_CC_CX_GDSCR', R.GDSC_RETAIN_FF, R.GDSC_RETAIN_FF)
    out['order'].append('cx_gdsc')
    # REVIEW-2: FREQ_LIMIT only with the CX GDSC proven on by a fresh read.
    prove_fuse(s, gdsc_state(gdscr, s.rd('GPU_CC_CX_CFG_GDSCR')), 'P4 reads')
    if 'cx_fuse' not in s.proven:
        raise Stop('P4', 'CX GDSC does not read on (or is under HW control) after P4; FREQ_LIMIT not read')
    step_freq_limit(s, out)

    s.step = 'P6'
    # glymur.dtsi gmu clocks in DT order (clk_bulk_prepare_enable), GEMNOC included.
    for name in GMU_CLOCKS:
        branch_on(s, name)
    out['order'].append('gmu_clocks')

    s.step = 'P7'
    for name in SMMU_CLOCKS:
        branch_on(s, name)
    out['order'].append('smmu_clock')

    step_gpucc(s, out, label='gpucc_after')
    step_identify(s, out, required=True)


def summarize(out, command):
    gpucc = out.get('gpucc_after') or out.get('gpucc') or {}
    ident = out.get('identify', {})
    misc, smmu = ident.get('cx_misc', {}), ident.get('smmu', {})
    summary = {
        'slices': misc.get('slices'), 'slice_mask': misc.get('slice_enable_final'), 'chip_id': misc.get('chip_id'),
        'speed_bin': out.get('freq_limit', {}).get('speed_bin'), 'freq_limit_fuse': out.get('freq_limit', {}).get('fuse'),
        'smmu_context_banks': smmu.get('decoded', {}).get('context_banks'),
        'smmu_stream_match_groups': smmu.get('decoded', {}).get('stream_match_groups'),
        'tcm_ret_cntl': misc.get('tcm_ret_cntl'), 'tcm_ret_ok': misc.get('tcm_ret_ok'),
        'cx_gdsc': ('on' if gpucc['cx_gdsc']['on'] else 'off') if gpucc else None,
        'cbcrs': gpucc.get('branches'),
    }
    wanted = [n for n in GMU_CLOCKS + SMMU_CLOCKS]
    # A16: GPU_CC_AHB_CBCR's enable does not latch; when clean CX_MISC reads in
    # this state proved the AHB path, count it and say so.
    ahb_by_read = 'FREQ_LIMIT' in str(out.get('proven', {}).get('cx_misc', ''))
    summary['ahb_enable_latched'] = bool(gpucc) and gpucc['branches'].get('GPU_CC_AHB_CBCR') == 'enabled'
    summary['all_cx_clocks_running'] = bool(gpucc) and all(
        gpucc['branches'].get(n) in ('running', 'enabled', 'hw-gated')
        or (n == 'GPU_CC_AHB_CBCR' and ahb_by_read) for n in wanted)
    serror = out.get('serror_guard', {}).get('after', {})
    summary['serror'] = {k: serror.get(k) for k in ('mechanism', 'before', 'during', 'refused', 'unconsumed')}
    summary['stage1_gate_met'] = (
        command == 'cx-on' and not out.get('stopped') and summary['cx_gdsc'] == 'on'
        and summary['all_cx_clocks_running'] and summary['slices'] is not None
        and summary['smmu_context_banks'] is not None and summary['speed_bin'] is not None
        and not serror.get('before') and not serror.get('during') and not serror.get('unconsumed'))
    out['summary'] = summary


def arm_guard(proxy, out, allow_unguarded, arm=True):
    try:
        state = proxy.serror_state()
    except q1n1proxy.ProxyRemoteError:
        out['serror_guard'] = {'present': False}
        if allow_unguarded:
            return None
        raise GuardMissing('setup', 'the running q1n1 has no SError guard (P_Q1N1_SERROR rejected): '
                                    'chainload a stage built with it, or pass --allow-unguarded')
    if state['mechanism'] == 0 and not allow_unguarded:
        raise GuardMissing('setup', 'the SError guard reports no mechanism on this core')
    out['serror_guard'] = {'present': True, 'before_reset': serror_json(state)}
    if arm:
        proxy.serror_reset()
        proxy.serror_strict(True)
    return state


def run(proxy, command, allow_writes=False, allow_unguarded=False, take_hw_control=False, clock=time.monotonic,
        stop_after='S5'):
    """Run one subcommand against a connected proxy; returns (exit status, JSON dict)."""
    out = {'tool': 'a16gpu', 'format': 1, 'command': command,
           'started': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
           'order': [], 'stopped': None}
    session = None
    status = 0
    previous = None
    if command not in ('guard', 'survey', 'cx-on'):
        raise ValueError(command)
    try:
        previous = arm_guard(proxy, out, allow_unguarded, arm=command != 'guard')
        if command == 'guard':
            return status, out      # report only: no reset, no mode change, no GPU access
        session = Session(proxy, allow_writes=allow_writes, guarded=previous is not None, clock=clock)
        if command == 'survey':
            survey(session, out, stop_after=stop_after)
        else:
            cx_on(session, out, take_hw_control=take_hw_control)
    except Stop as stop:
        out['stopped'] = {'step': stop.step, 'reason': stop.reason, **stop.detail}
        status = stop.exit_code
    except Refused as refused:
        out['stopped'] = {'step': session.step if session else 'setup', 'reason': f'refused: {refused}'}
        status = 6
    except (q1n1proxy.ProxyError, OSError) as problem:
        out['stopped'] = {'step': session.step if session else 'setup',
                          'reason': f'proxy link failed: {problem} (possible bus hang: see RUNBOOK recovery)'}
        status = 5
    finally:
        if previous is not None and command != 'guard':
            try:
                # REVIEW-2: a posted write's error can arrive after the last
                # bracketed check. One more bracketed access (a read of the
                # record's own magic through P_READ64) polls for it first; in
                # strict mode it is refused if one is pending, which is fine.
                # Only after a clean run: after a stop it would overwrite the
                # record's last_* fields that name the culprit.
                if status == 0:
                    try:
                        proxy.read64(previous['address'])
                    except q1n1proxy.ProxyRemoteError:
                        pass
                out['serror_guard']['after'] = serror_json(proxy.serror_state())
                proxy.serror_strict(bool(previous['flags'] & q1n1proxy.SERROR_STRICT))
            except (q1n1proxy.ProxyError, OSError) as problem:
                out['serror_guard']['after_error'] = str(problem)
        if session is not None:
            out['proven'] = session.proven
            out['polls'] = session.polls
            out['log'] = session.log
            after = out.get('serror_guard', {}).get('after', {})
            if status == 0 and (after.get('before') or after.get('during') or after.get('unconsumed')):
                out['stopped'] = {'step': 'end', 'reason': 'SError counters are not zero after the run'}
                status = SErrorStop.exit_code
        if command != 'guard':
            summarize(out, command)
    return status, out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--device', help='tty, UNIX socket or udp://enN (default: discover)')
    parser.add_argument('--wait', type=float, default=0.0)
    parser.add_argument('--out', type=Path, help='JSON output (default build/a16-gpu/stage1-<command>-<time>.json)')
    parser.add_argument('--allow-unguarded', action='store_true',
                        help='run on a stage without the SError guard (not for GPU work)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan', help='print the ordered steps; no hardware')
    sub.add_parser('guard', help='report the SError guard; no GPU access')
    sv = sub.add_parser('survey', help='read-only survey')
    sv.add_argument('--stop-after', choices=SURVEY_STEPS, default='S5',
                    help='end after this step (first contact: S2, then S3, then the whole survey)')
    cx = sub.add_parser('cx-on', help='survey, then CX power-on, then identify')
    cx.add_argument('--write', action='store_true', help='required: this writes GCC/GPUCC registers')
    cx.add_argument('--take-hw-control', action='store_true',
                    help='clear GDSC HW_CONTROL as gdsc_init does, if firmware left it set')
    stage3 = _stage3()
    stage3.add_parsers(sub)
    args = parser.parse_args(argv)

    if args.command in stage3.COMMANDS:
        return _main_stage3(parser, args, stage3)
    if args.command == 'plan':
        for step, domain, text in PLAN:
            print(f'{step:3} [{domain}] {text}')
        return 0
    if args.command == 'cx-on' and not args.write:
        parser.error('cx-on writes GCC and GPUCC registers; pass --write')
    proxy = q1n1proxy.connect(args.device, wait=args.wait)
    status, out = run(proxy, args.command, allow_writes=args.command == 'cx-on',
                      allow_unguarded=args.allow_unguarded,
                      take_hw_control=getattr(args, 'take_hw_control', False),
                      stop_after=getattr(args, 'stop_after', 'S5'))
    out['endpoint'] = getattr(proxy, 'endpoint', None)
    path = args.out or (TOOLS.parent / 'build' / 'a16-gpu' /
                        f'stage1-{args.command}-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + '\n')
    brief = {k: out[k] for k in ('command', 'order', 'stopped') if k in out}
    brief['summary'] = out.get('summary')
    brief['serror_guard'] = out.get('serror_guard')
    print(json.dumps(brief, indent=2))
    print(f'wrote {path}', file=sys.stderr)
    return status


def _stage3():
    import a16gpu_stage3   # noqa: E402 -- imports this module; loaded lazily to keep Stage 1 standalone
    return a16gpu_stage3


def _main_stage3(parser, args, stage3):
    _fn, writes = stage3.COMMANDS[args.command]
    if (writes or getattr(args, 'claim', False) or getattr(args, 'release', False)) and not getattr(args, 'write', False):
        parser.error(f'{args.command} writes GPU registers or the arena; pass --write')
    path = args.out or (TOOLS.parent / 'build' / 'a16-gpu' /
                        f'stage3-{args.command}-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.json')
    if args.command == 'cp-fence':
        # C3P writes what it read here before CP_SQE_CNTL is touched (the A16 may not answer after it).
        args.checkpoint = path.with_name(path.stem + '.prestart.json')
    if args.command in ('blit', 'triangle', 'render'):
        # P6/P7 save what they read back next to the JSON (<stem>.p6.bgra / <stem>.p7.bgra, raw BGRA8); render
        # every target (<stem>.<function>.rt<i>.raw, tight rows).
        args.image_prefix = str(path.with_suffix(''))
    proxy = q1n1proxy.connect(args.device, wait=args.wait)
    status, out = stage3.run(proxy, args.command, args)
    out['endpoint'] = getattr(proxy, 'endpoint', None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str) + '\n')
    brief = {k: out[k] for k in ('command', 'order', 'stopped', 'stopped_after') if k in out}
    brief['serror_guard'] = out.get('serror_guard', {}).get('after')
    print(json.dumps(brief, indent=2, default=str))
    print(f'wrote {path}', file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.modules.setdefault('a16gpu', sys.modules['__main__'])   # one copy when a16gpu_stage3 imports it
    sys.exit(main())
