#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline register/power-model test for tools/a16gpu.py and tools/adreno_x2.py.

Runs every subcommand against FakeA16, a model of the A16 GPU subsystem's GCC,
GPUCC, CX GDSC, CX_MISC and SMMU that enforces what the hardware would punish:
a GPUCC access without GCC_GPU_CFG_AHB is a bus hang, FREQ_LIMIT read before
the critical GPUCC CBCRs are enabled (outside the Linux precedent) is a bus
hang, a CX_MISC/SMMU access while the CX GDSC is under HW control (it may
collapse at any time) is a bus hang, one without the CX GDSC and its clocks is
an SError, any GX access is a violation.
It checks that the survey writes nothing and never touches GX, that the steps
run in the plan's order, that every poll is bounded and stops the run, that the
review corrections hold, and that the SError guard is required and acted on.
No hardware, no proxy, no network.

  python3 tools/test-a16gpu.py
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import a16gpu  # noqa: E402
import adreno_x2 as R  # noqa: E402

Q = a16gpu.q1n1proxy
checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


class FakeHang(Q.ProxyTimeout):
    """What a real bus hang looks like from the host: the link stops answering."""


GPUCC = 0x03D90000
MEMORY_MAP = 0xB0100000
CMD_DB_BASE = R.CMD_DB[0]


def efi_map(cmd_db_type=0):
    descriptors = [(7, 0x80000000, 0x1C60), (cmd_db_type, CMD_DB_BASE, 0x20), (7, 0x81C80000, 0x1000),
                   (0, R.GPU_ZAP_CARVEOUT[0], 2)]
    if cmd_db_type is None:        # REVIEW-2: cmd-db not described by the map at all
        del descriptors[1]
    return b''.join(struct.pack('<IIQQQQ', kind, 0, base, 0, pages, 0xF) for kind, base, pages in descriptors)


def cmd_db_blob():
    entries = [(b'cx.lvl', 0x30000), (b'mx.lvl', 0x30010), (b'gfx.lvl', 0x30020), (b'gmxc.lvl', 0x30030),
               (b'SH0', 0x50000), (b'MC0', 0x50010), (b'ACV', 0x50020)]
    header = struct.pack('<I4s', 1, R.CMD_DB_MAGIC)
    header += struct.pack('<5H3H', 3, 0, len(entries) * 24, len(entries), 1, 0, 0, 0)
    header += bytes(16 * 7) + bytes(8)
    body = b''.join(name.ljust(8, b'\0') + bytes(8) + struct.pack('<IHH', addr, 0, 0) for name, addr in entries)
    return (header + body).ljust(R.CMD_DB[1], b'\0')


# Branch model: name -> (offset, halt kind, needs the CX GDSC, needs the hub RCG source)
BRANCHES = {
    'GPU_CC_RSCC_XO_AON_CBCR': (0x9004, 'critical', False, False),
    'GPU_CC_CXO_AON_CBCR': (0x9008, 'critical', False, False),
    'GPU_CC_CB_CBCR': (0x93A4, 'critical', False, False),
    'GPU_CC_AHB_CBCR': (0x90BC, 'delay', False, False),
    'GPU_CC_CX_GMU_CBCR': (0x90D4, 'voted', True, False),
    'GPU_CC_CXO_CBCR': (0x90E4, 'halt', True, False),
    'GPU_CC_HUB_CX_INT_CBCR': (0x90E8, 'voted', True, True),
    'GPU_CC_RSCC_HUB_AON_CBCR': (0x93E8, 'halt', False, True),
    'GPU_CC_GPU_SMMU_VOTE_CBCR': (0x7000, 'voted', True, False),
}

COLD = {
    'guard': True, 'gcc_cfg_ahb_on': True, 'gcc_bcr': 0, 'vote': 0x00400000, 'gpll0_hwctl': False,
    'gemnoc_enabled': False, 'gemnoc_hwctl': False,
    'gdsc_on': False, 'hw_control': False, 'gdsc_fields': (0x8 << 16) | (0x2 << 12),   # gdsc.c defaults
    'enabled': (), 'branch_delay': 2, 'gdsc_delay': 3, 'rcg_delay': 2,
    'gmu_rcg_cfg': 0x00000500,      # GPLL0 (5), bypass divider
    'hub_rcg_src': 5, 'pll0_running': False,
    'ff_rcg_cfg': 0x00000500,       # GPLL0 (5), bypass divider
    'freq_limit': 388, 'slice_mask': 0xF, 'tcm_ret': 0, 'idr0': 0x78000018, 'idr1': 0x00190019, 'idr7': 0x21,
    'stuck': None, 'gdsc_never': False, 'pwr_on_bit_lags': False, 'ahb_clk_off_stuck': True,
    'serror_during': None, 'serror_write_at': None, 'serror_pending': False, 'sync_abort_at': None, 'cmd_db_type': 0,
    'hw_on_collapsed': False,    # GDSC on only through HW_CONTROL, SW_COLLAPSE still set
    'collapse_delay': 3,         # CFG reads that still show "on" once a collapse has started
    'gemnoc_needs_gdsc': False,  # GCC GEMNOC_GFX reports CLK_OFF while the CX GDSC is collapsed
    'rcg_update_stuck': False,   # the GMU RCG never acknowledges UPDATE
    'ignore_writes': (),         # GPUCC branches whose CLK_ENABLE write does not land
    'record_refused': False,     # strict mode refuses the SError record read too (older payload)
    'unconsumed_at': None,       # REVIEW-2: an SError the guard can neither defer nor take appears here
    'late_serror_at': None,      # REVIEW-2: an SError that arrives only after this (last) access
    'aon_running': (),           # A16: runs (CLK_OFF clear) with CLK_ENABLE that never latches
    'ahb_forced_on': False,      # A16: GPU_CC_AHB enable never latches, yet the GPU AHB path is up
}
CRITICAL = ('GPU_CC_RSCC_XO_AON_CBCR', 'GPU_CC_CXO_AON_CBCR', 'GPU_CC_CB_CBCR')
# Firmware left gpucc as its own probe would: critical CBCRs on, CX GDSC off.
PROBED = dict(enabled=CRITICAL)
# ... and the CX GDSC left on (not under HW control): the state FREQ_LIMIT is read in.
PROBED_ON = dict(enabled=CRITICAL, gdsc_on=True)
WARM = dict(COLD, gemnoc_enabled=True, gdsc_on=True, vote=0x00400003, enabled=tuple(BRANCHES),
            gmu_rcg_cfg=0x00000000, gdsc_fields=R.CX_GDSC_WAITS | R.GDSC_RETAIN_FF)


class FakeA16:
    def __init__(self, **scenario):
        self.sc = dict(COLD, **scenario)
        sc = self.sc
        self.log = []                 # (op, addr, value) of every MMIO/DRAM access that reached the model
        self.violations = []
        self.strict = False
        self.serror = dict.fromkeys(Q.SERROR_FIELDS, 0)
        self.serror.update(magic=Q.SERROR_MAGIC, mechanism=2)
        self.pending_serror = sc['serror_pending']
        self.exc = 0
        self.endpoint = 'fake'
        self.vote = sc['vote']
        self.gemnoc = int(sc['gemnoc_enabled']) | (R.CBCR_HW_CTL if sc['gemnoc_hwctl'] else 0)
        self.gemnoc_reads = 99 if sc['gemnoc_enabled'] else 0
        collapse = 0 if sc['gdsc_on'] and not sc['hw_on_collapsed'] else R.GDSC_SW_COLLAPSE
        self.gdscr = collapse | (R.GDSC_HW_CONTROL if sc['hw_control'] else 0) | sc['gdsc_fields']
        self.gdsc_on = sc['gdsc_on']
        self.gdsc_countdown = None
        self.collapse_countdown = None
        self.serror_raised = False
        self.enabled = set(sc['enabled'])
        self.branch_reads = {name: 99 for name in self.enabled}
        # REVIEW-2: the three clk_rcg2_shared_ops RCGs, CMD offset -> state.
        self.rcgs = {0x9318: {'cmd': 0, 'cfg': sc['gmu_rcg_cfg'], 'pending': None, 'upd': None},
                     0x93F0: {'cmd': 0, 'cfg': sc['hub_rcg_src'] << 8 | 5, 'pending': None, 'upd': None},
                     0x9474: {'cmd': 0, 'cfg': sc['ff_rcg_cfg'], 'pending': None, 'upd': None}}

    @property
    def rcg_cmd(self):
        return self.rcgs[0x9318]['cmd']

    def rcg_src(self, cmd):
        return self.rcgs[cmd]['cfg'] >> 8 & 7

    # --- power model -------------------------------------------------------------------
    def source_running(self, src):
        return {0: True, 1: self.sc['pll0_running'], 2: self.sc['pll0_running'], 3: self.sc['pll0_running'],
                5: bool(self.vote & 1), 6: bool(self.vote & 2)}.get(src, False)

    def branch_running(self, name):
        if name in self.sc['aon_running']:
            return True
        if name not in self.enabled or name == self.sc['stuck']:
            return False
        _off, kind, needs_gdsc, needs_hub = BRANCHES[name]
        if needs_gdsc and not self.gdsc_on:
            return False
        if needs_hub and not self.source_running(self.rcg_src(0x93F0)):
            return False
        if name == 'GPU_CC_CX_GMU_CBCR' and not self.source_running(self.rcg_src(0x9318)):
            return False
        return self.branch_reads.get(name, 0) >= self.sc['branch_delay'] or kind == 'critical'

    def gdsc_steady(self, addr):
        # Under HW control the GMU/RSC may collapse the GDSC at any moment, and
        # a collapse in progress still reads "on" for a few reads: an access
        # behind it is an access to a block that may be losing power.
        if self.gdsc_on and (self.gdscr & R.GDSC_HW_CONTROL or self.collapse_countdown is not None):
            self.violate('hang', addr, 'CX block access while the CX GDSC may be collapsing (HW control)')

    def cx_gmu_on(self):
        return (self.gdsc_on and self.ahb_on() and self.gemnoc & 1
                and all(self.branch_running(n) for n in ('GPU_CC_CX_GMU_CBCR', 'GPU_CC_CXO_CBCR',
                                                         'GPU_CC_HUB_CX_INT_CBCR', 'GPU_CC_RSCC_HUB_AON_CBCR')))

    def cx_smmu_on(self):
        return self.gdsc_on and self.ahb_on() and self.branch_running('GPU_CC_GPU_SMMU_VOTE_CBCR')

    def ahb_on(self):
        return 'GPU_CC_AHB_CBCR' in self.enabled or self.sc['ahb_forced_on']

    def violate(self, kind, addr, why):
        self.violations.append((kind, hex(addr), why))
        if kind == 'hang':
            raise FakeHang(f'bus hang at {addr:#x}: {why}')
        # An SError: pending until the proxy's poll consumes it.
        self.raise_serror(addr)

    def raise_serror(self, addr):
        self.serror_raised = True
        self.serror['during'] += 1
        self.serror.update(last_when=2, last_address=addr, last_syndrome=0xBF000002, last_opcode=0x105)
        if self.strict:
            raise Q.ProxyRemoteError('opcode 0x105 rejected (status -83)', Q.S_SERROR)

    def gpucc_reachable(self, addr):
        if not self.sc['gcc_cfg_ahb_on'] or self.sc['gcc_bcr'] & 1:
            self.violate('hang', addr, 'GPUCC access without GCC_GPU_CFG_AHB')

    # --- proxy surface ---------------------------------------------------------------------
    def _before(self, op, addr):
        self.serror['checks'] += 1
        if getattr(self, 'still_pending', False):
            # REVIEW-2 payload: strict mode refuses on a still-pending SError too.
            self.serror['unconsumed'] += 1
            if self.strict:
                self.serror['refused'] += 1
                raise Q.ProxyRemoteError('opcode rejected (status -83)', Q.S_SERROR)
        if self.pending_serror:
            self.pending_serror = False
            self.serror['before'] += 1
            self.serror.update(last_when=1, last_address=addr, last_syndrome=0xBF000001)
            if self.strict:
                self.serror['refused'] += 1
                raise Q.ProxyRemoteError('opcode rejected (status -83)', Q.S_SERROR)

    def read32(self, addr):
        self._before('r', addr)
        self.log.append(('r', addr, None))
        value = self._read(addr)
        if addr == self.sc['serror_during']:
            self.raise_serror(addr)
        if addr == self.sc['late_serror_at']:
            self.pending_serror = True
        if addr == self.sc['sync_abort_at']:
            self.exc += 1
            return Q.GUARD_MARKER
        return value

    def read64(self, addr):
        # Payload RAM (the SError record's magic): bracketed, no MMIO.
        self._before('r64', addr)
        return Q.SERROR_MAGIC

    def write32(self, addr, value):
        self._before('w', addr)
        self.log.append(('w', addr, value))
        self._write(addr, value & 0xFFFFFFFF)
        if addr in (self.sc['serror_during'], self.sc['serror_write_at']):
            self.raise_serror(addr)
        if addr == self.sc['unconsumed_at']:
            self.still_pending = True

    def get_exc_count(self):
        count, self.exc = self.exc, 0
        return count

    def udelay(self, microseconds):
        self.log.append(('udelay', microseconds, None))

    def bootinfo(self):
        return {'image_base': 0xB0000000, 'stage_generation': 1, 'current_el': 2, 'gic_distributor': 0x17000000,
                'memory_map': MEMORY_MAP, 'memory_map_size': len(efi_map()), 'memory_map_stride': 40}

    def readmem(self, addr, size, live=False):
        self._before('m', addr)
        if addr == MEMORY_MAP:
            return efi_map(self.sc['cmd_db_type'])[:size]
        if addr == CMD_DB_BASE:
            self.log.append(('m', addr, size))
            return cmd_db_blob()[:size]
        self.violate('hang', addr, 'readmem of an address the model does not know')

    def _guard(self):
        if not self.sc['guard']:
            raise Q.ProxyRemoteError('opcode 0x5131 rejected (status -1)', -1)

    def serror_state(self):
        self._guard()
        if self.sc['record_refused'] and self.strict and self.serror_raised:
            raise Q.ProxyRemoteError('memread refused', Q.ST_XFRERR)
        return dict(self.serror, flags=int(self.strict), address=0xB0001000)

    def serror_reset(self):
        self._guard()
        for key in ('checks', 'before', 'during', 'refused', 'unconsumed', 'last_syndrome', 'last_when',
                    'last_opcode', 'last_address'):
            self.serror[key] = 0

    def serror_strict(self, enabled):
        self._guard()
        self.strict = bool(enabled)

    # --- registers -------------------------------------------------------------------------------
    def _read(self, addr):
        block = R.block_of(addr)
        if block is None or block.domain == 'GX':
            self.violate('gx' if block else 'unknown', addr, 'Stage 1 must not touch this')
            return 0
        if block.name == 'GICD':
            off = addr - 0x17000000
            return {0x0: 0x37, 0x4: 0x0000001F}.get(off, 0)
        if block.name == 'GCC':
            return self._gcc_read(addr)
        if block.name == 'GPUCC':
            self.gpucc_reachable(addr)
            return self._gpucc_read(addr - GPUCC)
        if addr == 0x03D9F014:
            self.gpucc_reachable(addr)
            if not all(name in self.enabled or name in self.sc['aon_running'] for name in CRITICAL):
                # Outside the Linux precedent: gpucc probe has set these first.
                # An always-on branch that runs without a latched enable (seen on
                # the A16) is on in the only sense the bus cares about.
                self.violate('hang', addr, 'FREQ_LIMIT before the critical GPUCC CBCRs are enabled')
            self.gdsc_steady(addr)
            if not self.gdsc_on:
                # REVIEW-2: CX_MISC with the CX GDSC collapsed; nothing shows the fuse
                # mirror is outside the GDSC.
                self.violate('hang', addr, 'FREQ_LIMIT with the CX GDSC collapsed')
            return self.sc['freq_limit'] | 0x7000
        if block.name == 'CX_MISC':
            self.gdsc_steady(addr)
            if not self.cx_gmu_on():
                self.violate('serror', addr, 'CX_MISC without the CX GDSC and GMU clocks')
            return {0x03D9E21C: self.sc['slice_mask'], 0x03D9E0E4: self.sc['tcm_ret']}.get(addr, 0)
        if block.name == 'SMMU':
            self.gdsc_steady(addr)
            if not self.cx_smmu_on():
                self.violate('serror', addr, 'SMMU without the CX GDSC and the vote clock')
            off = addr - 0x03DA0000
            fixed = {0x0: 0x00200001, 0x20: self.sc['idr0'], 0x24: self.sc['idr1'], 0x28: 0x0000FF55,
                     0x3C: self.sc['idr7']}
            if off in fixed:
                return fixed[off]
            if 0x800 <= off < 0xC00:
                return 0x80000000 | ((off - 0x800) // 4)
            if 0xC00 <= off < 0x1000:
                return 0x00020000
            return 0
        self.violate('unknown', addr, f'block {block.name} not modelled for Stage 1')
        return 0

    def _gcc_read(self, addr):
        off = addr - 0x00100000
        if off == 0x71000:
            return self.sc['gcc_bcr']
        if off == 0x71004:
            return 0x00000001 if self.sc['gcc_cfg_ahb_on'] else 0x80000000
        if off == 0x71008:
            return 0x00000001
        if off == 0x71010:
            if self.gemnoc & 1:
                self.gemnoc_reads += 1
            running = (self.gemnoc & 1 and self.gemnoc_reads > self.sc['branch_delay']
                       and (self.gdsc_on or not self.sc['gemnoc_needs_gdsc']))
            return self.gemnoc | (0 if running else R.CBCR_CLK_OFF)
        if off in (0x71024, 0x7102C):
            bit = 1 if off == 0x71024 else 2
            hw = R.CBCR_HW_CTL if self.sc['gpll0_hwctl'] else 0
            return hw | (0 if self.vote & bit else R.CBCR_CLK_OFF)
        if off == 0x62038:
            return self.vote
        self.violate('unknown', addr, 'GCC register not modelled')
        return 0

    def _gpucc_read(self, off):
        for name, (boff, kind, _g, _h) in BRANCHES.items():
            if off == boff:
                if name in self.enabled:
                    self.branch_reads[name] = self.branch_reads.get(name, 0) + 1
                value = int(name in self.enabled)
                if kind == 'delay' and self.sc['ahb_clk_off_stuck']:
                    return value | R.CBCR_CLK_OFF          # REVIEW: HALT_DELAY status means nothing
                return value | (0 if self.branch_running(name) else R.CBCR_CLK_OFF)
        if off == 0x9080:
            on_bit = self.gdsc_on and not self.sc['pwr_on_bit_lags']
            return self.gdscr | (R.GDSC_PWR_ON if on_bit else 0)
        if off == 0x9084:
            if self.collapse_countdown is not None:
                self.collapse_countdown -= 1
                if self.collapse_countdown <= 0:
                    self.gdsc_on, self.collapse_countdown = False, None
            if self.gdsc_countdown is not None:
                self.gdsc_countdown -= 1
                if self.gdsc_countdown <= 0 and not self.sc['gdsc_never']:
                    self.gdsc_on, self.gdsc_countdown = True, None
            return R.CFG_POWER_UP_COMPLETE if self.gdsc_on else R.CFG_POWER_DOWN_COMPLETE
        if off == 0x9094:
            return 0x00000002 if self.gdsc_on else 0
        if off in self.rcgs:
            rcg = self.rcgs[off]
            if rcg['upd'] is not None:
                rcg['upd'] -= 1
                if rcg['upd'] <= 0:
                    rcg['cfg'], rcg['pending'], rcg['upd'] = rcg['pending'], None, None
                    rcg['cmd'] &= ~R.RCG_CMD_UPDATE
            root_on = rcg['cmd'] & R.RCG_CMD_ROOT_EN and self.source_running(self.rcg_src(off))
            return rcg['cmd'] | (0 if root_on else R.RCG_CMD_ROOT_OFF)
        if off - 4 in self.rcgs:
            rcg = self.rcgs[off - 4]
            return rcg['pending'] if rcg['pending'] is not None else rcg['cfg']
        if off in (0x90F0, 0x93EC, 0x90CC):
            return R.CBCR_CLK_OFF
        if off == 0x0:
            return 0
        self.violate('unknown', GPUCC + off, 'GPUCC register not modelled')
        return 0

    def _write(self, addr, value):
        block = R.block_of(addr)
        if block is None or block.domain == 'GX' or block.name not in ('GCC', 'GPUCC'):
            self.violate('gx' if block and block.domain == 'GX' else 'write', addr, 'Stage 1 write outside GCC/GPUCC')
            return
        if block.name == 'GCC':
            off = addr - 0x00100000
            if off == 0x62038:
                self.vote = value
            elif off == 0x71010:
                self.gemnoc = value & 3
            else:
                self.violate('write', addr, 'GCC register Stage 1 must not write')
            return
        self.gpucc_reachable(addr)
        off = addr - GPUCC
        for name, (boff, _kind, _g, _h) in BRANCHES.items():
            if off == boff:
                if name in self.sc['ignore_writes'] or name in self.sc['aon_running']:
                    return
                if value & 1 and name not in self.enabled:
                    self.enabled.add(name)
                    self.branch_reads[name] = 0
                return
        if off == 0x9080:
            was_collapsed = self.gdscr & R.GDSC_SW_COLLAPSE
            self.gdscr = value & ~R.GDSC_PWR_ON
            software = not value & R.GDSC_HW_CONTROL
            if software and value & R.GDSC_SW_COLLAPSE and self.gdsc_on and self.collapse_countdown is None:
                # HW control released with SW_COLLAPSE set: it starts to collapse,
                # and CFG keeps reading "on" for a few reads.
                self.collapse_countdown = self.sc['collapse_delay']
            elif software and not value & R.GDSC_SW_COLLAPSE:
                if self.collapse_countdown is not None:
                    self.collapse_countdown = None          # cancelled before it finished
                elif was_collapsed and not self.gdsc_on:
                    unparked = [hex(GPUCC + c) for c in self.rcgs if self.rcg_src(c) != 0
                                and not self.source_running(self.rcg_src(c))]
                    if unparked:
                        # clk-rcg2.c: "A GDSC can wedge if it is turned on and the RCG is stuck on".
                        self.violations.append(('wedge', hex(addr), f'CX GDSC enabled with RCGs {unparked} '
                                                                    'on a dead parent'))
                        return
                    if any(self.rcg_src(c) != 0 for c in self.rcgs):
                        self.violations.append(('order', hex(addr), 'CX GDSC enabled before the shared RCGs '
                                                                    'were parked (clk_rcg2_shared_init order)'))
                    self.gdsc_countdown = self.sc['gdsc_delay']
            return
        if off in self.rcgs:
            rcg = self.rcgs[off]
            if value & R.RCG_CMD_UPDATE and not rcg['cmd'] & R.RCG_CMD_UPDATE:
                stuck = self.sc['rcg_update_stuck'] and off == 0x9318
                both = not stuck and (self.source_running(self.rcg_src(off))
                                      and self.source_running((rcg['pending'] or 0) >> 8 & 7))
                rcg['upd'] = self.sc['rcg_delay'] if both else None
            rcg['cmd'] = value & (R.RCG_CMD_UPDATE | R.RCG_CMD_ROOT_EN)
            return
        if off - 4 in self.rcgs:
            self.rcgs[off - 4]['pending'] = value
            return
        self.violate('write', addr, 'GPUCC register Stage 1 must not write')

    # --- helpers for assertions ----------------------------------------------------------------
    def mmio(self, ops=('r', 'w')):
        return [(op, addr, value) for op, addr, value in self.log if op in ops]

    def writes(self):
        return [(name_of(addr), value) for op, addr, value in self.log if op == 'w']

    def reads_of(self, addr, after=0):
        return sum(1 for i, (op, a, _v) in enumerate(self.log) if op == 'r' and a == addr and i >= after)


def name_of(addr):
    for reg in R.REGS.values():
        for index in range(reg.count):
            if reg.at(index) == addr:
                return reg.name if reg.count == 1 else f'{reg.name}[{index}]'
    return hex(addr)


def category(addr):
    block = R.block_of(addr)
    if block is None:
        return 'other'
    if addr == 0x03D9F014:
        return 'freq_limit'
    return {'CMD_DB': 'cmd_db', 'GICD': 'gic', 'GCC': 'gcc', 'GPUCC': 'gpucc', 'CX_MISC': 'cx_misc',
            'SMMU': 'smmu'}.get(block.name, block.name)


def first_seen(fake):
    order = []
    for op, addr, _v in fake.log:
        if op in ('r', 'w', 'm'):
            cat = category(addr)
            if cat not in order:
                order.append(cat)
    return order


def run_opts(command, allow_unguarded=False, take_hw_control=False, **scenario):
    fake = FakeA16(**scenario)
    status, out = a16gpu.run(fake, command, allow_writes=command == 'cx-on', allow_unguarded=allow_unguarded,
                             take_hw_control=take_hw_control)
    return fake, status, out


def no_gx(fake, label):
    touched = [hex(a) for op, a, _v in fake.mmio() if (R.block_of(a) or R.BLOCKS[0]).domain == 'GX'
               or R.block_of(a) is None]
    check(not touched, f'{label}: no GX or unclassified access ({touched})')
    check(not fake.violations, f'{label}: power model saw no violation ({fake.violations})')


# --- registry ------------------------------------------------------------------------------------
def test_registry():
    for reg in R.REGS.values():
        check(reg.domain in R.DOMAINS and reg.gate in R.GATES and reg.evidence and reg.access in ('', 'r', 'rw'),
              f'{reg.name}: domain/gate/access/evidence present')
        block = R.block_of(reg.addr)
        check(block is not None and block.name == reg.block, f'{reg.name} @ {reg.addr:#x} sits in block {reg.block}')
        check(R.block_of(reg.at(reg.count - 1)) == block, f'{reg.name}: whole array inside {reg.block}')
        if reg.domain != block.domain:
            check(reg.block == 'GPUCC' and reg.domain == 'GX' and reg.access == '',
                  f'{reg.name}: only GX branches/resets inside GPUCC may differ from their block domain')
        if reg.domain == 'GX':
            # Stage 1 refuses every GX register (test_session_refusals); Stage 3 reaches one only
            # behind gate gx, which only the GMU's own report of GX on can prove.
            check(reg.gate in ('gx', 'never') and (reg.access == '' or reg.gate == 'gx'),
                  f'{reg.name}: GX is accessible only behind gate gx')
    for name in R.DROPPED:
        check(name not in R.REGS, f'dropped {name} is absent')
    check(not any('SPTPRAC' in name for name in R.REGS), 'no SPTPRAC names remain')
    # Stage 3 correction: a8xx still loads its DTCM blocks through the host window at XML 0x1c400
    # (a6xx_gmu_fw_load: REG_A6XX_GMU_CM3_DTCM_START + offset); only the legacy name stays dropped.
    check('GMU_CM3_DTCM_START' not in R.REGS and R.REGS['GMU_CM3_DTCM'].addr == 0x03D71000
          and R.REGS['GMU_CM3_DTCM'].at(0xFF8) == R.REGS['GMU_CORE_FW_VERSION'].addr,
          'the a8xx DTCM window is 0x03d71000 and its entry 0xff8 is CORE_FW_VERSION 0x03d74fe0')
    fixed = {'GPU_CC_CX_CFG_GDSCR': 0x03D99084, 'CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS': 0x03D9F014,
             'CX_MISC_SLICE_ENABLE_FINAL': 0x03D9E21C, 'CX_MISC_TCM_RET_CNTL': 0x03D9E0E4,
             'GBIF_CX_CONFIG': 0x03D0F000, 'GCC_GPU_CFG_AHB_CBCR': 0x00171004,
             'GCC_APCS_CLOCK_BRANCH_ENA_VOTE': 0x00162038, 'GPU_CC_CX_GDSCR': 0x03D99080,
             'GPU_CC_GMU_CMD_RCGR': 0x03D99318, 'SMMU_IDR1': 0x03DA0024, 'GX_GDSCR': 0x03D68024}
    for name, addr in fixed.items():
        check(R.REGS[name].addr == addr, f'{name} = {addr:#010x}')
    check(R.REGS['GBIF_CX_CONFIG'].domain == 'CX', 'REVIEW: GBIF is CX')
    check(R.REGS['GPU_CC_AHB_CBCR'].halt == 'HALT_DELAY', 'REVIEW: GPU_CC_AHB is BRANCH_HALT_DELAY')
    check(R.REGS['CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS'].gate == 'cx_fuse',
          'REVIEW-2 M1: FREQ_LIMIT only once the critical GPUCC CBCRs are on AND the CX GDSC reads on')
    check(set(a16gpu.CRITICAL_CBCRS) == set(CRITICAL)
          and [R.REGS[n].addr for n in a16gpu.CRITICAL_CBCRS] == [0x03D993A4, 0x03D99008, 0x03D99004],
          'critical CBCRs: CB 0x93a4, CXO_AON 0x9008, RSCC_XO_AON 0x9004')
    check(R.runtime_chip_id(0xF) == 0x44070041 and R.runtime_chip_id(0x7) == 0x44070031
          and R.runtime_chip_id(0xF0) == 0x44070001, 'runtime chip id = 0x44070001 | popcount(mask & 0xf) << 4')
    check([R.speed_bin(v)[1] for v in (0, 388, 357, 284, 100)] == [0, 1, 2, 3, None], 'speed bins')
    check(a16gpu.parse_cmd_db(cmd_db_blob())['gpu']['cx.lvl']['addr'] == '0x30000', 'cmd-db parser')


def test_session_refusals():
    fake = FakeA16(**WARM)
    session = a16gpu.Session(fake, allow_writes=False)
    for reg in R.REGS.values():
        if reg.domain == 'GX' or reg.gate in ('gx', 'never'):
            for call in (lambda: session.rd(reg.name), lambda: session.wr(reg.name, 0)):
                try:
                    call()
                    check(False, f'{reg.name} refused')
                except a16gpu.Refused:
                    pass
    check(not fake.log, 'refused accesses sent nothing')
    try:
        session.rd('GPU_CC_CX_GDSCR')
        check(False, 'GPUCC read before its gate is proven is refused')
    except a16gpu.Refused:
        check(not fake.log, 'GPUCC read before its gate is proven is refused, nothing sent')
    try:
        session.wr('GCC_GPU_GEMNOC_GFX_CBCR', 1)
        check(False, 'a write with writes off is refused')
    except a16gpu.Refused:
        check(not fake.log, 'a write with writes off is refused, nothing sent')
    session = a16gpu.Session(fake, allow_writes=True)
    try:
        session.wr('GCC_GPU_BCR', 0)
        check(False, 'BCR write refused')
    except a16gpu.Refused:
        check(not fake.log, 'GCC_GPU_BCR is never written')


# --- survey ------------------------------------------------------------------------------------
def test_survey_cold():
    fake, status, out = run_opts('survey')
    check(status == 0, f'cold survey completes ({out["stopped"]})')
    check(not fake.writes(), 'survey writes nothing')
    no_gx(fake, 'cold survey')
    check(first_seen(fake) == ['cmd_db', 'gic', 'gcc', 'gpucc'], f'cold survey order {first_seen(fake)}')
    check(out['order'] == ['cmd_db', 'gic', 'gcc', 'gpucc'] and 'skipped' in out['freq_limit']
          and 'gpucc_critical' not in out['proven'] and out['summary']['speed_bin'] is None,
          'REVIEW M1: critical CBCRs off -> the read-only survey does not read FREQ_LIMIT')
    check('skipped' in out['identify']['cx_misc'] and 'skipped' in out['identify']['smmu'],
          'CX_MISC/SMMU skipped while firmware left the CX GDSC off')
    for partial in (CRITICAL[:1], CRITICAL[1:]):
        fake2, status2, out2 = run_opts('survey', enabled=partial)
        check(status2 == 0 and 'skipped' in out2['freq_limit'] and not fake2.reads_of(0x03D9F014),
              f'only {len(partial)} of 3 critical CBCRs on: FREQ_LIMIT not read')
    fake, status, out = run_opts('survey', **PROBED)
    check(status == 0 and out['order'] == ['cmd_db', 'gic', 'gcc', 'gpucc'] and not fake.reads_of(0x03D9F014)
          and 'skipped' in out['freq_limit'],
          f'REVIEW-2 M1: critical CBCRs on but the CX GDSC off: FREQ_LIMIT not read ({out["order"]})')
    no_gx(fake, 'probed survey, GDSC off')
    fake, status, out = run_opts('survey', gdsc_on=True, hw_control=True, **PROBED)
    check(status == 0 and not fake.reads_of(0x03D9F014) and 'skipped' in out['freq_limit'],
          'REVIEW-2 M1: CX GDSC under HW control: FREQ_LIMIT not read')
    no_gx(fake, 'probed survey, GDSC under HW control')
    fake, status, out = run_opts('survey', gdsc_on=True, gdsc_fields=R.CX_GDSC_WAITS, **PROBED)
    check(status == 0 and out['order'] == ['cmd_db', 'gic', 'gcc', 'gpucc', 'freq_limit'],
          f'critical CBCRs on and the CX GDSC on: FREQ_LIMIT read ({out["order"]})')
    check('cx_misc' not in out['proven'], 'survey: GMU clocks off, so FREQ_LIMIT alone does not open CX_MISC')
    no_gx(fake, 'probed survey, GDSC on')
    check('CLK_ENABLE set or running' in out['proven']['gpucc_critical'], 'the gpucc_critical gate names its evidence')
    check('HW_CONTROL clear' in out['proven']['cx_fuse'], 'the cx_fuse gate names its evidence')
    gpu_reads = [a for _op, a, _v in fake.mmio() if 0x03D00000 <= a < 0x03E00000 and category(a) != 'gpucc']
    check(gpu_reads and gpu_reads[0] == 0x03D9F014, 'the first GPU-block read is FREQ_LIMIT 0x03d9f014')
    check(out['summary']['speed_bin'] == 1 and out['summary']['freq_limit_fuse'] == 388, 'speed bin 1 from 388')
    check(out['cmd_db']['reserved'] and out['cmd_db']['gpu']['gfx.lvl']['addr'] == '0x30020', 'cmd-db reserved, parsed')
    check(out['gic']['intids']['0x14c']['enabled'] == 0, 'GIC state recorded')
    check(fake.strict is False, 'strict SError mode restored afterwards')
    check(out['serror_guard']['after']['strict'] is True and out['serror_guard']['after']['during'] == 0,
          'survey ran in strict SError mode, no SError')
    check(not out['summary']['stage1_gate_met'], 'a survey alone never claims the Stage 1 gate')


def test_survey_warm():
    fake, status, out = run_opts('survey', **WARM)
    check(status == 0, f'warm survey completes ({out["stopped"]})')
    check(not fake.writes(), 'warm survey writes nothing')
    no_gx(fake, 'warm survey')
    check(first_seen(fake) == ['cmd_db', 'gic', 'gcc', 'gpucc', 'freq_limit', 'cx_misc', 'smmu'],
          f'warm survey order {first_seen(fake)}')
    ident = out['identify']
    check(ident['cx_misc']['slices'] == 4 and ident['cx_misc']['chip_id'] == '0x44070041', 'slices and chip id')
    check(ident['cx_misc']['tcm_ret_ok'], 'TCM_RET_CNTL 0')
    check(ident['smmu']['decoded']['context_banks'] == 25 and len(ident['smmu']['streams']) == 24,
          'SMMU: 25 context banks, every SMR/S2CR of 24 groups')
    smr_reads = fake.reads_of(0x03DA0800 + 4 * 23)
    check(smr_reads == 1 and fake.reads_of(0x03DA0800 + 4 * 24) == 0, 'SMR reads stop at NUMSMRG')

    for label, scenario in (('under HW control', dict(WARM, hw_control=True)),
                            ('on only through HW control', dict(WARM, hw_control=True, hw_on_collapsed=True))):
        fake, status, out = run_opts('survey', **scenario)
        check(status == 0 and out['gpucc']['cx_gdsc']['on'] and out['gpucc']['cx_gdsc']['hw_control'],
              f'REVIEW M2: CX GDSC {label} reads on')
        check('skipped' in out['identify']['cx_misc'] and 'skipped' in out['identify']['smmu']
              and 'HW control' in out['identify']['cx_misc']['skipped']
              and not {'cx_misc', 'smmu'} & set(first_seen(fake)) and not {'cx_gmu', 'cx_smmu'} & set(out['proven']),
              f'REVIEW M2: CX GDSC {label}: survey does not read CX_MISC/SMMU')
        no_gx(fake, f'warm survey, GDSC {label}')


def test_survey_stepwise():
    for stop_after, seen in (('S2', ['cmd_db', 'gic', 'gcc']), ('S3', ['cmd_db', 'gic', 'gcc', 'gpucc']),
                             ('S4', ['cmd_db', 'gic', 'gcc', 'gpucc', 'freq_limit'])):
        fake = FakeA16(**WARM)
        status, out = a16gpu.run(fake, 'survey', stop_after=stop_after)
        check(status == 0 and first_seen(fake) == seen and out['stopped_after'] == stop_after,
              f'survey --stop-after {stop_after} reads only {seen}')
        no_gx(fake, f'stop-after {stop_after}')


def test_survey_stops():
    fake, status, out = run_opts('survey', gcc_cfg_ahb_on=False)
    check(status == 2 and out['stopped']['step'] == 'S2', 'CFG_AHB off: stop at GCC')
    check(first_seen(fake) == ['cmd_db', 'gic', 'gcc'], 'CFG_AHB off: nothing past GCC is read')
    no_gx(fake, 'CFG_AHB off')
    fake, status, out = run_opts('survey', gcc_bcr=1)
    check(status == 2 and 'reset' in out['stopped']['reason'] and first_seen(fake)[-1] == 'gcc',
          'GPU BCR asserted: stop at GCC')
    fake, status, out = run_opts('survey', cmd_db_type=7)
    check(status == 0 and not out['cmd_db']['reserved'] and 'skipped' in out['cmd_db']
          and 'cmd_db' not in first_seen(fake), 'cmd-db in free memory: reported, not read')


# --- SError guard ----------------------------------------------------------------------------------
def test_guard():
    fake, status, out = run_opts('survey', guard=False)
    check(status == 4 and not fake.log, 'no guard in the stage: refused before any access')
    fake, status, out = run_opts('survey', guard=False, allow_unguarded=True)
    check(status == 0 and out['serror_guard'] == {'present': False}, '--allow-unguarded runs and says so')

    fake, status, out = run_opts('survey', serror_during=0x03D9F014, **PROBED_ON)
    check(status == 3 and out['stopped']['step'] == 'S4' and 'during' in out['stopped']['reason'],
          f'SError across FREQ_LIMIT stops the survey ({out["stopped"]})')
    last = fake.mmio()[-1]
    check(last[1] == 0x03D9F014, 'no access after the SError')
    check(out['stopped']['serror']['last_address'] == hex(0x03D9F014), 'the record names the address')

    fake, status, out = run_opts('survey', serror_pending=True)
    check(status == 3 and 'before' in out['stopped']['reason'] and not fake.mmio(),
          'an SError pending from earlier work: the first access is refused and the run stops')

    fake, status, out = run_opts('survey', sync_abort_at=0x03D9F014, **PROBED_ON)
    check(status == 3 and 'synchronous abort' in out['stopped']['reason'], 'a guard marker stops the run')

    # REVIEW S3: an older payload (or a record read that gets bracketed) is
    # refused in strict mode while an SError stays pending. The target
    # answered, so that is an SError stop (exit 3), not a link failure (exit 5).
    fake, status, out = run_opts('survey', serror_during=0x03D9F014, record_refused=True, **PROBED_ON)
    check(status == 3 and out['stopped']['step'] == 'S4' and 'record read was refused' in out['stopped']['reason']
          and 'after_error' in out['serror_guard'], f'SError record refused: exit 3, not 5 ({out["stopped"]})')

    fake, status, out = run_opts('guard')
    check(status == 0 and not fake.log and out['serror_guard']['present'] and fake.strict is False,
          'guard: reports, touches nothing, changes no mode')


# --- cx-on -------------------------------------------------------------------------------------------
EXPECTED_WRITES = [
    'GCC_APCS_CLOCK_BRANCH_ENA_VOTE',
    'GPU_CC_CB_CBCR', 'GPU_CC_CXO_AON_CBCR', 'GPU_CC_RSCC_XO_AON_CBCR',
    # REVIEW-2: the shared RCGs are parked before the CX GDSC (clk_rcg2_shared_init order)
    'GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CFG_RCGR', 'GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CMD_RCGR',
    'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR', 'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CMD_RCGR',
    'GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CFG_RCGR', 'GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CMD_RCGR',
    'GPU_CC_CX_GDSCR', 'GPU_CC_CX_GDSCR', 'GPU_CC_CX_GDSCR',
    'GPU_CC_AHB_CBCR', 'GPU_CC_CX_GMU_CBCR', 'GPU_CC_CXO_CBCR', 'GCC_GPU_GEMNOC_GFX_CBCR', 'GPU_CC_HUB_CX_INT_CBCR',
    'GPU_CC_RSCC_HUB_AON_CBCR',
    'GPU_CC_GPU_SMMU_VOTE_CBCR',
]


def test_cx_on_cold():
    fake, status, out = run_opts('cx-on')
    check(status == 0, f'cold cx-on completes ({out["stopped"]})')
    no_gx(fake, 'cold cx-on')
    names = [name for name, _v in fake.writes()]
    check(names == EXPECTED_WRITES, f'write order matches the plan: {names}')
    writes = fake.writes()
    check(writes[0][1] == 0x00400003, 'GCC vote ORs in bits 0-1 and keeps the other voters')
    gdsc_writes = [v for n, v in writes if n == 'GPU_CC_CX_GDSCR']
    check(gdsc_writes[0] & 0xFFF000 == 0x22F000 and not gdsc_writes[0] & (R.GDSC_HW_CONTROL | R.GDSC_SW_OVERRIDE),
          'gdsc_init: waits 2/2/0xf, HW_CONTROL and SW_OVERRIDE off')
    check(gdsc_writes[1] & R.GDSC_SW_COLLAPSE == 0 and gdsc_writes[2] & R.GDSC_RETAIN_FF,
          'then SW_COLLAPSE cleared, then RETAIN_FF')
    check(dict(writes)['GPU_CC_GMU_CFG_RCGR'] == 0 and dict(writes)['GPU_CC_HUB_CFG_RCGR'] == 0
          and dict(writes)['GPU_CC_FF_CFG_RCGR'] == 0, 'GMU, HUB and FF RCGs parked on XO: CFG = 0')
    last_park = max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a in (0x03D99318, 0x03D993F0,
                                                                                      0x03D99474))
    first_gdsc_w = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D99080)
    check(last_park < first_gdsc_w, 'REVIEW-2 M2: every shared RCG parked before the first CX GDSC write')
    first_misc = next(i for i, (op, a, _v) in enumerate(fake.log) if category(a) == 'cx_misc')
    last_write = max(i for i, (op, _a, _v) in enumerate(fake.log) if op == 'w')
    check(last_write < first_misc, 'identification reads only after the last power write')
    check(out['order'] == ['cmd_db', 'gic', 'gcc', 'gpucc', 'gcc_votes', 'critical_cbcrs', 'gmu_rcg',
                           'cx_gdsc', 'freq_limit', 'gmu_clocks', 'smmu_clock', 'cx_misc', 'smmu'],
          f'cx-on order {out["order"]}')
    freq = next(i for i, (op, a, _v) in enumerate(fake.log) if a == 0x03D9F014)
    last_critical = max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a in (0x03D993A4, 0x03D99008,
                                                                                          0x03D99004))
    last_gdsc = max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D99080)
    check(last_critical < last_gdsc < freq and fake.reads_of(0x03D9F014) == 1,
          'REVIEW-2 M1: FREQ_LIMIT read once, after P3 and after P4 brought the CX GDSC up')
    gemnoc = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x00171010)
    check(gemnoc > max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D99080),
          'REVIEW S2: GEMNOC_GFX enabled after the CX GDSC, with the GMU clocks (P6)')
    plan_ids = [step for step, _d, _t in a16gpu.PLAN]
    steps = [entry['step'] for entry in out['log']]
    check(all(plan_ids.index(a) <= plan_ids.index(b) for a, b in zip(steps, steps[1:])),
          'every logged access is in plan order S0..S3, P1, P3, P5, P4, S4, P6, P7, S5')
    check('P2' not in plan_ids and 'P2' not in steps, 'REVIEW S2: there is no P2 step any more')
    poll_reads = fake.reads_of(0x03D99084)
    check(any(p['reg'] == 'GPU_CC_CX_CFG_GDSCR' and p['ok'] for p in out['polls']) and poll_reads >= 2,
          'GDSC completion polled at 0x03d99084')
    ahb_write = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D990BC)
    check(fake.reads_of(0x03D990BC, after=ahb_write) == 1, 'AHB (HALT_DELAY): no CLK_OFF wait after enabling '
                                                            '(the one later read is the status re-read)')
    check(not any(p['reg'] == 'GPU_CC_AHB_CBCR' for p in out['polls']), 'AHB never polled')
    summary = out['summary']
    check(summary['stage1_gate_met'] and summary['slices'] == 4 and summary['chip_id'] == '0x44070041'
          and summary['speed_bin'] == 1 and summary['smmu_context_banks'] == 25 and summary['cx_gdsc'] == 'on'
          and summary['all_cx_clocks_running'] and summary['tcm_ret_ok'], f'Stage 1 gate JSON: {summary}')
    check(all(p['reads'] <= a16gpu.POLL_LIMITS['gdsc'] for p in out['polls']), 'every poll within its bound')


def test_cx_on_warm():
    fake, status, out = run_opts('cx-on', **WARM)
    check(status == 0 and out['gmu_rcg_park'] == 'already on XO', 'warm cx-on: RCG already on XO')
    park = ['GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR', 'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CMD_RCGR',
            'GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CFG_RCGR', 'GPU_CC_FF_CMD_RCGR', 'GPU_CC_FF_CMD_RCGR']
    check([n for n, _v in fake.writes()] == park,
          f'warm cx-on: only the HUB/FF RCGs left on GPLL0 are parked, nothing else written ({fake.writes()})')
    no_gx(fake, 'warm cx-on')
    fake, status, out = run_opts('cx-on', **dict(WARM, gdsc_fields=(0x8 << 16) | (0x2 << 12)))
    writes = fake.writes()
    gdsc_only = [(n, v) for n, v in writes if 'RCGR' not in n]
    check(status == 0 and [n for n, _v in gdsc_only] == ['GPU_CC_CX_GDSCR', 'GPU_CC_CX_GDSCR']
          and all(not v & R.GDSC_SW_COLLAPSE for _n, v in gdsc_only),
          f'GDSC already on with other waits: gdsc_init waits and RETAIN_FF only, never a collapse ({writes})')


def test_cx_on_aon_not_latched():
    # A16, 2026-09-25: GPU_CC_CXO_AON_CBCR read 0x08000000 before and after
    # CLK_ENABLE was written (build/a16-gpu/stage1-cx-on.json). Linux never
    # reads it back; running is the evidence that matters.
    aon = ('GPU_CC_CXO_AON_CBCR',)
    fake, status, out = run_opts('cx-on', **dict(PROBED, enabled=tuple(n for n in CRITICAL if n not in aon),
                                               aon_running=aon))
    check(status == 0, f'cx-on continues when a critical branch runs without a latched enable ({out.get("stopped")})')
    check('running (enable not latched)' in out['proven'].get('gpucc_critical', ''),
          'the gate records the non-latching branch')
    no_gx(fake, 'cx-on with a non-latching AON branch')
    a16 = dict(PROBED, enabled=tuple(n for n in CRITICAL if n not in aon), aon_running=aon,
               ignore_writes=('GPU_CC_AHB_CBCR',))
    fake, status, out = run_opts('cx-on', **dict(a16, ahb_forced_on=True))
    check('cx_misc' in out['order'] and out['identify']['cx_misc'].get('slices') == 4,
          f'A16 shape: AHB enable never latches, yet CX_MISC identifies 4 slices ({out["order"]})')
    check('FREQ_LIMIT' in out['proven'].get('cx_misc', '')
          and 'clean CX_MISC reads' in out['proven'].get('cx_smmu', ''),
          'cx_misc from the clean FREQ_LIMIT read plus running GMU clocks; cx_smmu from those CX_MISC reads')
    check(status == 0 and 'smmu' in out['order'] and out['summary']['smmu_context_banks'] is not None,
          f'A16 shape: the SMMU is identified ({out.get("stopped")})')
    check(out['summary']['stage1_gate_met'] and not out['summary']['ahb_enable_latched'],
          'A16 shape: Stage-1 gate met, with the unlatched AHB enable recorded')
    no_gx(fake, 'A16-shaped cx-on')
    # Accepted residual risk: if the AHB path were really off, the first CX_MISC
    # identity read raises an SError; the strict guard stops the run there.
    fake, status, out = run_opts('cx-on', **a16)
    check(status == 3 and out['stopped']['step'] == 'S5' and 'CX_MISC_SLICE_ENABLE_FINAL' in out['stopped']['reason'],
          f'AHB really off: the guard stops at the slice read ({out.get("stopped")})')
    after = [a for op, a, _v in fake.log[fake.log.index(('r', 0x03D9E21C, None)) + 1:] if op in ('r', 'w')] \
        if ('r', 0x03D9E21C, None) in fake.log else ['<slice read not logged>']
    check(not after, f'nothing is accessed after the refused identity read ({after})')
    fake, status, out = run_opts('cx-on', **dict(PROBED, enabled=(), ignore_writes=aon))
    check(status == 2 and out['stopped']['step'] == 'P3',
          'a critical branch that is neither enabled nor running still stops P3')


def test_cx_on_bounds():
    limit = a16gpu.POLL_LIMITS
    for stuck, step in (('GPU_CC_CX_GMU_CBCR', 'P6'), ('GPU_CC_GPU_SMMU_VOTE_CBCR', 'P7'),
                        ('GPU_CC_CXO_CBCR', 'P6')):
        fake, status, out = run_opts('cx-on', stuck=stuck)
        addr = R.REGS[stuck].addr
        enable = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == addr)
        check(status == 2 and out['stopped']['step'] == step and stuck in out['stopped']['reason'],
              f'{stuck} never runs: cx-on stops in {step}')
        check(fake.reads_of(addr, after=enable) <= limit['branch'], f'{stuck}: at most {limit["branch"]} polls')
        check(not any(op == 'w' for op, _a, _v in fake.log[enable + 1:]), f'{stuck}: no write after the failure')
        check(category(fake.mmio()[-1][1]) == 'gpucc', f'{stuck}: nothing past GPUCC read after the failure')
        no_gx(fake, f'{stuck} stuck')

    fake, status, out = run_opts('cx-on', gdsc_never=True)
    collapse = [i for i, (op, a, v) in enumerate(fake.log) if op == 'w' and a == 0x03D99080][1]
    check(status == 2 and out['stopped']['step'] == 'P4', 'CX GDSC never completes: stop in P4')
    check(fake.reads_of(0x03D99084, after=collapse) <= limit['gdsc'], f'GDSC poll bounded ({limit["gdsc"]})')
    check(not any(op == 'w' for op, _a, _v in fake.log[collapse + 1:]), 'no RETAIN_FF, RCG or branch write after it')
    no_gx(fake, 'GDSC stuck')

    fake, status, out = run_opts('cx-on', gmu_rcg_cfg=0x00000200)    # parked on a dead PLL0
    root_en = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D99318)
    check(status == 2 and out['stopped']['step'] == 'P5', 'GMU RCG on a dead PLL0: stop in P5')
    check(fake.reads_of(0x03D99318, after=root_en) <= limit['rcg'] + 1,
          'RCG poll bounded (plus the one read of the ROOT_EN release)')
    check(not any(op == 'w' and a == 0x03D9931C for op, a, _v in fake.log), 'CFG never written off a dead source')
    check(not fake.rcg_cmd & R.RCG_CMD_ROOT_EN and out['stopped']['root_en_released'] is True
          and fake.writes()[-1][0] == 'GPU_CC_GMU_CMD_RCGR' and not fake.writes()[-1][1] & R.RCG_CMD_ROOT_EN,
          'REVIEW S4: ROOT_EN released after the P5 failure, and that is the last write')
    no_gx(fake, 'RCG stuck')

    fake, status, out = run_opts('cx-on', rcg_update_stuck=True)
    check(status == 2 and out['stopped']['step'] == 'P5' and out['stopped']['root_en_released'] is True
          and not fake.rcg_cmd & R.RCG_CMD_ROOT_EN and fake.writes()[-1][0] == 'GPU_CC_GMU_CMD_RCGR'
          and not fake.writes()[-1][1] & R.RCG_CMD_ROOT_EN,
          f'REVIEW S4: UPDATE never acknowledged: stop in P5, ROOT_EN released ({out["stopped"]})')
    check(out['polls'][-1] == {'reg': 'GPU_CC_GMU_CMD_RCGR', 'reads': limit['rcg'], 'ok': False},
          'RCG UPDATE poll bounded')
    no_gx(fake, 'RCG update stuck')

    fake, status, out = run_opts('cx-on', ignore_writes=('GPU_CC_CB_CBCR',))
    check(status == 2 and out['stopped']['step'] == 'P3' and 'GPU_CC_CB_CBCR' in out['stopped']['reason']
          and not fake.reads_of(0x03D9F014) and not any(a == 0x03D99080 for op, a, _v in fake.mmio('w')),
          'REVIEW M1: a critical CBCR that does not read enabled after P3: stop, FREQ_LIMIT never read')
    no_gx(fake, 'critical CBCR stuck')


def test_cx_on_review_cases():
    fake, status, out = run_opts('cx-on', pwr_on_bit_lags=True)
    check(status == 0 and out['summary']['cx_gdsc'] == 'on',
          'GDSC completion comes from CFG_GDSCR bit 16, not GDSCR bit 31')
    fake, status, out = run_opts('cx-on', hw_control=True)
    check(status == 2 and out['stopped']['step'] == 'P4' and not fake.writes(),
          'GDSC under HW control: stop before any write')
    fake, status, out = run_opts('cx-on', hw_control=True, take_hw_control=True)
    check(status == 0 and not fake.gdscr & R.GDSC_HW_CONTROL, '--take-hw-control clears it as gdsc_init does')
    fake, status, out = run_opts('cx-on', **dict(WARM, hw_control=True, hw_on_collapsed=True), take_hw_control=True)
    gdsc_writes = [v for n, v in fake.writes() if n == 'GPU_CC_CX_GDSCR']
    check(status == 0 and fake.gdsc_on and not fake.gdscr & R.GDSC_SW_COLLAPSE
          and len(gdsc_writes) == 2 and gdsc_writes[0] & R.GDSC_SW_COLLAPSE and not gdsc_writes[1] & R.GDSC_SW_COLLAPSE
          and any(p['reg'] == 'GPU_CC_CX_CFG_GDSCR' for p in out['polls']),
          f'REVIEW S1: on only through HW control: SW_COLLAPSE cleared and power-up polled anyway ({out["stopped"]})')
    no_gx(fake, 'on only through HW control')
    fake, status, out = run_opts('cx-on', gemnoc_needs_gdsc=True)
    check(status == 0, f'REVIEW S2: GEMNOC reads CLK_OFF while the CX GDSC is down: no false abort ({out["stopped"]})')
    fake, status, out = run_opts('cx-on', gemnoc_hwctl=True, gpll0_hwctl=True)
    check(status == 0 and not any(p['reg'].startswith('GCC_') for p in out['polls']),
          'hw-gated GCC branches are not polled')
    fake, status, out = run_opts('cx-on', tcm_ret=1)
    check(status == 0 and out['summary']['tcm_ret_ok'] is False, 'TCM_RET_CNTL != 0 reported')
    fake, status, out = run_opts('cx-on', serror_write_at=0x03D990D4)
    check(status == 3 and out['stopped']['step'] == 'P6' and fake.mmio()[-1][:2] == ('w', 0x03D990D4),
          'SError across a CBCR write stops cx-on at that access')
    fake, status, out = run_opts('cx-on', serror_write_at=0x03D990D4, record_refused=True)
    check(status == 3 and out['stopped']['step'] == 'P6' and fake.mmio()[-1][:2] == ('w', 0x03D990D4),
          'REVIEW S3: SError record refused in strict mode: exit 3 at that access, not 5')


def test_cli():
    """The command line end to end, with connect() returning the model."""
    fakes = []
    real_connect = Q.connect

    def connect(device=None, wait=0.0, **_ignored):
        fakes.append(FakeA16(**COLD))
        return fakes[-1]

    Q.connect = connect
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as printed, \
                contextlib.redirect_stderr(io.StringIO()):
            path = Path(tmp) / 'cx.json'
            status = a16gpu.main(['--out', str(path), 'cx-on', '--write'])
            saved = json.loads(path.read_text())
            check(status == 0 and saved['summary']['stage1_gate_met'] and json.loads(printed.getvalue())['order'][-1]
                  == 'smmu', 'CLI cx-on --write: exit 0, JSON written with the gate met')
            try:
                a16gpu.main(['cx-on'])
                check(False, 'cx-on without --write is refused')
            except SystemExit as refused:
                check(refused.code == 2 and len(fakes) == 1, 'cx-on without --write: refused before connecting')
            status = a16gpu.main(['--out', str(path), 'survey', '--stop-after', 'S2'])
            check(status == 0 and json.loads(path.read_text())['stopped_after'] == 'S2', 'CLI survey --stop-after')
            check(a16gpu.main(['plan']) == 0 and len(fakes) == 2, 'plan needs no connection')
    finally:
        Q.connect = real_connect


def test_review2():
    # M2: a shared RCG stuck on a dead parent must stop the run before the CX GDSC.
    for label, scenario in (('HUB', dict(hub_rcg_src=2)), ('FF', dict(ff_rcg_cfg=0x00000200)),
                            ('GMU', dict(gmu_rcg_cfg=0x00000300))):
        fake, status, out = run_opts('cx-on', **scenario)
        check(status == 2 and out['stopped']['step'] == 'P5' and out['stopped']['root_en_released'] is True,
              f'{label} RCG on a dead PLL0: stop in P5 ({out["stopped"]})')
        check(not any(op == 'w' and a == 0x03D99080 for op, a, _v in fake.log),
              f'{label} RCG on a dead PLL0: the CX GDSC is never written')
        check(not fake.reads_of(0x03D9F014), f'{label} RCG on a dead PLL0: FREQ_LIMIT never read')
        no_gx(fake, f'{label} RCG on a dead PLL0')
    # M1 (cx-on): FREQ_LIMIT only after P4, also when the GDSC was under HW control.
    fake, status, out = run_opts('cx-on', take_hw_control=True, gdsc_on=True, hw_control=True,
                                 hw_on_collapsed=True, **PROBED)
    check(status == 0 and fake.reads_of(0x03D9F014) == 1, f'HW-control cx-on completes ({out["stopped"]})')
    no_gx(fake, 'HW-control cx-on: FREQ_LIMIT never read under HW control or while collapsing')
    # S: an SError that the guard can neither defer nor take stops the run.
    fake, status, out = run_opts('cx-on', unconsumed_at=0x00162038)
    vote = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x00162038)
    check(status == 3 and 'unconsumed' in out['stopped']['reason'] and not out['summary']['stage1_gate_met'],
          f'still-pending SError after the P1 write: exit 3 ({out["stopped"]})')
    check(not any(op in ('r', 'w') for op, _a, _v in fake.log[vote + 1:]), 'nothing sent after it')
    # S: cmd-db not in the EFI map at all: not read.
    fake, status, out = run_opts('survey', cmd_db_type=None)
    check(status == 0 and 'skipped' in out['cmd_db'] and not any(op == 'm' for op, _a, _v in fake.log),
          'cmd-db absent from the EFI map: not read')
    # S: an SError that shows up only after the last access is still caught.
    last = R.REGS['SMMU_S2CR'].at(0x18 - 1)
    fake, status, out = run_opts('cx-on', late_serror_at=last)
    check(status == 3 and out['serror_guard']['after']['before'] == 1 and not out['summary']['stage1_gate_met'],
          f'late SError after the last access: exit 3 ({out["stopped"]})')


def main():
    for test in (test_registry, test_session_refusals, test_survey_cold, test_survey_warm, test_survey_stepwise, test_survey_stops,
                 test_guard, test_cx_on_cold, test_cx_on_warm, test_cx_on_aon_not_latched, test_cx_on_bounds, test_cx_on_review_cases, test_review2,
                 test_cli):
        test()
    print(f'PASS: {checks} a16gpu register/power-model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
