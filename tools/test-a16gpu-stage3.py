#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for the Adreno Stage 3 tool (tools/a16gpu_stage3.py).

FakeStage3 extends Stage 1's FakeA16 (tools/test-a16gpu.py) past cx-on: a CPU
data cache in front of DRAM (so a missing clean or invalidate shows up as a
stale read by one side), the Adreno SMMU with stream matching and a real LPAE
walk, GPUCC PLL0 and its RCGs, the apps RSC with RPMh votes, the TrustZone
SMC behind bootinfo gpu_call, the GMU (TCM load, boot checks, HFI queues,
OOB), and a CP that fetches the SQE and the ring through the SMMU and executes
PKT7s. It enforces what the hardware would punish or what Linux's order
implies: a GX access before the GMU powers GX is a bus hang; TCM writes to a
running CM3, a GMU boot with an unmapped or stale icache, the wrong clocks,
votes or registers, HFI out of order, SECVID before CP init, are failures.
Each rule is labelled PROVEN (Linux source) or INFERRED in the model.

  python3 tools/test-a16gpu-stage3.py
  A16_GPU_FW_DIR=<dir with the sha-pinned files> python3 tools/test-a16gpu-stage3.py   # also the real firmware
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402

_spec = importlib.util.spec_from_file_location('stage1_test', TOOLS / 'test-a16gpu.py')
T1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T1)
Q = a16gpu.q1n1proxy
FakeHang = T1.FakeHang

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


# --- synthetic firmware with the real image's shape (the real files are not in the tree) -------------
def synth_gmu():
    def blk(addr, data, kind=1, value=0):
        return struct.pack('<4I', addr, len(data), kind, value) + data
    itcm = bytes((i * 7 + 3) & 0xFF for i in range(0x210))
    icache = bytes((i * 13 + 1) & 0xFF for i in range(0x1C80C))
    dtcm_a = bytes((i * 5) & 0xFF for i in range(0x28))
    dtcm_b = struct.pack('<6I', 0xFFFFFFFF, R.GMU_FW_VERSION, 0x11, 0x22, 0x33, 0x44)
    headers = b''.join(struct.pack('<4I', a, 0, 1, n) for a, n in ((0, 0x210), (0x4000, 0x1C80C), (0x10004000, 0x2EDC)))
    headers += b''.join(struct.pack('<4I', a, 0, 7, n) for a, n in ((0x60000000, 0x1000), (0x60001000, 0x2000),
                                                                     (0x60003000, 0x1000)))
    return headers + blk(0, itcm, 0xDEADBEEF, 0xDEADBEEF) + blk(0x4000, icache, 0xDEADBEEF, 0xDEADBEEF) \
        + blk(0x10007F98, dtcm_a, 0xDEADBEEF, 0xDEADBEEF) + blk(0x10007FDC, dtcm_b, 0xDEADBEEF, 0xDEADBEEF)


SQE = struct.pack('<I', 0x0001AA00) + bytes((i * 3 + 9) & 0xFF for i in range(0x1D410))
GMU = synth_gmu()


@contextlib.contextmanager
def synthetic_firmware():
    saved = dict(R.FIRMWARE)
    with tempfile.TemporaryDirectory() as tmp:
        for key, data in (('gmu', GMU), ('sqe', SQE)):
            name = saved[key][0]
            (Path(tmp) / name).write_bytes(data)
            R.FIRMWARE[key] = (name, len(data), hashlib.sha256(data).hexdigest())
        try:
            yield tmp
        finally:
            R.FIRMWARE.clear()
            R.FIRMWARE.update(saved)


# --- the model ---------------------------------------------------------------------------------------
ARENA = 0x96000000
GPU_CALL, GPU_RECORD = 0xB0F00000, 0xB0F01000
LINE = 64
A16_CMD_DB = {   # stage1-cx-on-4.json (A16, PROVEN)
    'gfx.lvl': ('0x30050', '0000380040008000c000e000000140018001a001c001c401c801d00100000000'),
    'gmxc.lvl': ('0x300d0', '00001000380040008000c000e000000140018001a001c001c401c801d0010000'),
    'mx.lvl': ('0x30010', '0000100040008000c000000140018001a001c001c40100000000000000000000'),
    'cx.lvl': ('0x30000', '0000100040008000c000000140018001a001c001c40100000000000000000000'),
    'SH0': ('0x50004', 'c0acd800c0000100'), 'MC0': ('0x50000', '809fd50030000000'),
    'ACV': ('0x50068', '0100000000000d00'),
    'CN0': ('0x50038', '0100000000000400'),   # not read on the A16 yet (Stage 1 did not list CN0): placeholder
}

POST_CX_ON = dict(   # the A16 after cx-on (stage1-cx-on-4.json): AHB enable never latches, CXO_AON runs
    enabled=tuple(n for n in T1.BRANCHES if n not in ('GPU_CC_CXO_AON_CBCR', 'GPU_CC_AHB_CBCR')),
    aon_running=('GPU_CC_CXO_AON_CBCR',), ahb_forced_on=True, ignore_writes=('GPU_CC_AHB_CBCR',),
    gdsc_on=True, gdsc_fields=R.CX_GDSC_WAITS | R.GDSC_RETAIN_FF, gemnoc_enabled=True, vote=0x00400003,
    gmu_rcg_cfg=0x00100000, ff_rcg_cfg=0x00100000)

STAGE3_DEFAULTS = dict(
    arena=ARENA, gpu_call=True, efi_arena=False, smc_x0=0, smc_trap=False, pll_locks=True, gmu_boots=True,
    rails_preset=False, wake_cx=3, wake_mx=2, hidden_votes=None, rsc_busy=False, hfi_error_on=None, oob_acks=True, oob_gx_on=True,
    secvid_locked=False, cp_executes=True, smr1_foreign=False, gmu_init_delay=3, serror_gmu_write=None,
    hang_after_boot=None, drop_clean=False, gpu_version=3, gmu_index_delay=0, smc_fail=None,
    sleep_cx=None, sleep_mx=None, rscc_wakes=True, cb_fixed_bits=True, cb_stuck=False, legacy_ack=False, gbif_sticky_ack=True, gbif_ack_at_boot=3,
    aperture_x0=0, ahb_stuck=False, sqe_reset=False, imem=None, prestart_fault=False, aperture_fail=None,
    idr2=0x00005522)   # idr2: every A16 record (UBS 5); --va-wide refuses UBS 0

# sqe-reset (DIAGNOSIS.md): what run 1 did at CP_SQE_CNTL = 1. The model cannot know why; it models the
# consequence the tool must survive: the link dies at that write (no reply), nothing after it reaches the A16.
IMEM_DEFAULT = {0x14680000: 0, 0x14680004: 0, 0x1468001C: 0, 0x1468065C: 0x77665501, 0x1468094C: 0x5}


class FakeStage3(T1.FakeA16):
    def __init__(self, **scenario):
        stage1 = {k: v for k, v in scenario.items() if k not in STAGE3_DEFAULTS}
        super().__init__(**dict(POST_CX_ON, **stage1))
        self.s3 = dict(STAGE3_DEFAULTS, **{k: v for k, v in scenario.items() if k in STAGE3_DEFAULTS})
        for cmd in self.rcgs:                       # cx-on parked all three on XO
            self.rcgs[cmd]['cfg'] = 0x00100000
        self.dram = {}                              # line address -> bytearray(64): what devices see
        self.cache = {}                             # line address -> [bytearray(64), dirty, bytes the CPU wrote]
        self.gmu_pending = []                       # [ops to go, pa, data]: GMU writes still in flight
        self.gpu_rec = dict.fromkeys(S3.GPU_STATE_FIELDS, 0)
        self.gpu_rec.update(magic=S3.GPU_MAGIC, version=self.s3['gpu_version'], size=8 * len(S3.GPU_STATE_FIELDS))
        if self.s3['arena']:
            self.gpu_rec.update(arena_base=self.s3['arena'], arena_size=R.ARENA_SIZE,
                                arena_source=1 if self.s3['efi_arena'] else 2)
        self.claimable = True
        self.tz_done = False
        self.fuse = 0
        self.rpmh = {}                              # resource address -> hlvl
        if self.s3['rails_preset']:
            self.rpmh.update({0x30000: 2, 0x30010: 2})
        if self.s3['hidden_votes']:                 # drv-2's live active vote: in RPMh, in no TCS
            self.rpmh.update(self.s3['hidden_votes'])
        # PLL0
        self.pll = {0x00: 0x00000100, 0x04: 0, 0x10: 0, 0x14: 0, 0x18: 0, 0x1C: 0, 0x20: 0, 0x24: 0, 0x28: 0}
        self.pll_lock_reads = None
        self.pll_calibrated = False
        # SMMU (A16 stage1-cx-on-4.json values)
        self.scr0 = 0x00270406
        self.smr = [0] * 25
        self.s2cr = [0x00020000] * 25
        if self.s3['smr1_foreign']:
            self.smr[1], self.s2cr[1] = R.SMR_VALID | 0x0C00, 0x3
        self.cbar = [0] * 25
        self.cba2r = [0] * 25
        self.cb = [dict.fromkeys(range(0, 0x800, 4), 0) for _ in range(25)]
        self.smmu_faults = []
        # apps RSC drv-2 (v3 layout; INFERRED version)
        self.rsc = {}
        self.rsc_irq = 0
        wake = 0
        for cmd, (key, addr) in enumerate((('wake_cx', 0x30000), ('wake_mx', 0x30010))):
            if self.s3[key] is not None:          # firmware left a wake vote for CX / MX
                wake |= 1 << cmd
                self._tcs_set_cmd(5, cmd, 'ADDR', addr)
                self._tcs_set_cmd(5, cmd, 'DATA', self.s3[key])
        if wake:
            self._tcs_set(5, 'CMD_ENABLE', wake)
        sleep = 0
        for cmd, (key, addr) in enumerate((('sleep_cx', 0x30000), ('sleep_mx', 0x30010))):
            if self.s3[key] is not None:          # REVIEW-2: a sleep-TCS vote (applied only when the
                sleep |= 1 << cmd                 # apps subsystem sleeps): no evidence of the live level
                self._tcs_set_cmd(2, cmd, 'ADDR', addr)
                self._tcs_set_cmd(2, cmd, 'DATA', self.s3[key])
        if sleep:
            self._tcs_set(2, 'CMD_ENABLE', sleep)
        # GMU
        self.gmu = {}
        self.gmu[R.REGS['GMU_CM3_SYSRESET'].addr] = 1
        # REVIEW-2: the GPU RSC. a6xx_rpmh_stop (RSCC_CONTROL_REQ 1) puts it to sleep; only
        # a6xx_rpmh_start (REQ BIT(1), CONTROL_ACK BIT(1), SEQ_BUSY 0) wakes it. PROVEN pairing in
        # Linux (GMU_STATUS_PDC_SLEEP); INFERRED: STATUS0 bit 16 stays set while it sleeps, and a GMU
        # started with its RSC asleep never initialises.
        self.pdc_asleep = False
        self.rscc_ack = 0
        self.itcm = [0] * 0x1000
        self.dtcm = [0] * 0x1000
        self.gmu_running = False
        self.init_countdown = None
        self.hfi_countdown = None
        self.gmu_fail = []
        self.g2h = 0
        self.hfi_seen = []
        self.gx_on = False
        self.oob_held = False
        self.oob_since_boot = False                 # REVIEW-2: an OOB GPU_SET handshake since the GMU started
        self.slumbered = False
        self.hfi_state = set()
        # GX / CP
        self.gx = {}
        self.aperture = 0
        self.pipe_writes = []
        self.cp_running = False
        self.me_init = False
        self.rptr = 0
        self.secvid = 1
        self.gbif_halt = 0
        self.gbif_ack = self.s3['gbif_ack_at_boot']
        self.rbbm_gbif_halt = 0
        # sqe-reset additions: SMMU global fault state, CBFRSYNRA, CX AHB timeouts, the aperture SMC
        self.gfsr, self.gfsynr1 = 0, 0
        self.cbfrsynra = [0] * 25
        self.cx_ahb = [0] * 5                       # INFERRED reset value: timeouts off
        self.aperture_calls = 0
        self.sqe_fetch_fault = False
        self.imem = {**IMEM_DEFAULT, **(self.s3['imem'] or {})}
        self.dead = False                           # the A16 left USB (sqe_reset)

    def serror_state(self):
        if self.dead:
            raise Q.ProxyError('expected 1 bytes, got 0')
        return super().serror_state()

    def serror_strict(self, enabled):
        if self.dead:
            raise Q.ProxyError('expected 1 bytes, got 0')
        return super().serror_strict(enabled)

    # --- DRAM behind a write-back cache --------------------------------------------------------------
    def _line(self, addr):
        base = addr & ~(LINE - 1)
        if base not in self.dram:
            self.dram[base] = bytearray(LINE)
        return base

    def dev_read(self, addr, size):
        out = bytearray()
        for a in range(addr, addr + size):
            out.append(self.dram[self._line(a)][a % LINE])
        return bytes(out)

    def dev_write(self, addr, data):
        for n, byte in enumerate(data):
            a = addr + n
            self.dram[self._line(a)][a % LINE] = byte

    def cpu_read(self, addr, size):
        out = bytearray()
        for a in range(addr, addr + size):
            base = self._line(a)
            if base not in self.cache:
                self.cache[base] = [bytearray(self.dram[base]), False, set()]
            out.append(self.cache[base][0][a % LINE])
        return bytes(out)

    def cpu_write(self, addr, data):
        for n, byte in enumerate(data):
            a = addr + n
            base = self._line(a)
            if base not in self.cache:
                self.cache[base] = [bytearray(self.dram[base]), False, set()]
            self.cache[base][0][a % LINE] = byte
            self.cache[base][1] = True
            self.cache[base][2].add(a % LINE)

    def civac(self, addr, size):
        if self.s3['drop_clean']:
            return
        for base in range(addr & ~(LINE - 1), addr + size, LINE):
            entry = self.cache.pop(base, None)
            if entry and entry[1]:
                # PROVEN (Arm ARM, write-back caches): the whole line goes back. A byte the CPU did
                # not write but that differs from DRAM was written there by a device after the line
                # was filled: the write-back destroys it.
                line = self.dram[self._line(base)]
                lost = [off for off in range(LINE) if off not in entry[2] and entry[0][off] != line[off]]
                if lost:
                    self.violations.append(('clobber', hex(base + lost[0]),
                                            f'a write-back of line {base:#x} overwrote {len(lost)} device-written bytes'))
                line[:] = entry[0]

    def _before(self, op, addr):
        if getattr(self, 'dead', False):
            raise Q.ProxyError('expected 1 bytes, got 0')
        super()._before(op, addr)
        if op in ('r', 'r64', 'w'):
            return                                # counted in memory/cache requests, not register polls
        for write in list(self.gmu_pending):      # a GMU write in flight lands "during" a later memory request
            write[0] -= 1
            if write[0] <= 0:
                self.gmu_pending.remove(write)
                self.dev_write(write[1], write[2])

    def in_arena(self, addr, size):
        base = self.gpu_rec['arena_base']
        return base and base <= addr and addr + size <= base + self.gpu_rec['arena_size']

    # --- proxy surface additions ---------------------------------------------------------------------
    def bootinfo(self):
        info = super().bootinfo()
        info['memory_map_size'] = len(self.efi())
        info.update(gpu_call=GPU_CALL if self.s3['gpu_call'] else 0, gpu_state=GPU_RECORD if self.s3['gpu_call'] else 0,
                    gpu_arena_base=self.gpu_rec['arena_base'], gpu_arena_size=self.gpu_rec['arena_size'],
                    gpu_arena_flags=self.gpu_rec['arena_source'])
        return info

    def efi(self):
        """Stage 1's map plus the arena: LoaderData when the ESP reserved it, else inside Conventional."""
        if self.s3['efi_arena']:
            return T1.efi_map(0) + struct.pack('<IIQQQQ', 2, 0, ARENA, 0, R.ARENA_SIZE // 4096, 0xF)
        return T1.efi_map(0) + struct.pack('<IIQQQQ', 7, 0, 0x94000000, 0, 0x8000, 0xF)

    def readmem(self, addr, size, live=False):
        self._before('m', addr)
        if addr == T1.MEMORY_MAP:
            return self.efi()[:size]
        if addr == T1.CMD_DB_BASE:
            self.log.append(('m', addr, size))
            return cmd_db_blob()[:size]
        if addr == GPU_RECORD:
            return struct.pack(f'<{len(S3.GPU_STATE_FIELDS)}Q', *(self.gpu_rec[k] & (2**64 - 1)
                                                                    for k in S3.GPU_STATE_FIELDS))[:size]
        if self.in_arena(addr, size):
            self.log.append(('m', addr, size))
            return self.cpu_read(addr, size)
        self.violate('hang', addr, 'readmem outside the arena and known payload records')

    def cp_buffer_hit(self, addr, size):
        base = self.gpu_rec['arena_base']
        for name in ('cp_sqe', 'cp_rb'):
            off, length = R.ARENA_LAYOUT[name]
            if self.cp_running and addr < base + off + length and base + off < addr + size:
                self.violations.append(('cp', hex(addr), f'{name} rewritten under a running SQE'))

    def writemem(self, addr, data):
        self._before('mw', addr)
        if not self.in_arena(addr, len(data)):
            self.violate('hang', addr, 'writemem outside the arena')
        if not (self.gpu_rec['arena_base'] + R.ARENA_LAYOUT['cp_rb'][0] <= addr
                < self.gpu_rec['arena_base'] + R.ARENA_LAYOUT['cp_rb'][0] + R.ARENA_LAYOUT['cp_rb'][1]):
            self.cp_buffer_hit(addr, len(data))     # appending commands to the ring is the normal case
        self.log.append(('mw', addr, len(data)))
        self.cpu_write(addr, data)

    def request(self, opcode, *args):
        self._before('op', args[0] if args else 0)
        if opcode == S3.P_DC_CIVAC:
            self.log.append(('civac', args[0], args[1]))
            self.civac(args[0], args[1])
            return 0
        if opcode == S3.P_MEMSET64:
            addr, value, size = args
            if not self.in_arena(addr, size):
                self.violate('hang', addr, 'memset outside the arena')
            self.cp_buffer_hit(addr, size)
            self.log.append(('memset', addr, size))
            for base in range(addr, addr + size, LINE):     # fast path: whole lines, dirty in cache
                self.cache[self._line(base)] = [bytearray(struct.pack('<Q', value) * (LINE // 8)), True, set(range(LINE))]
            return 0
        raise Q.ProxyRemoteError(f'opcode {opcode:#x} not modelled', -1)

    def call(self, address, *args, timeout=None):
        self._before('call', address)
        if address != GPU_CALL or not self.s3['gpu_call']:
            self.violate('hang', address, 'P_CALL to an unknown address')
        op = args[0]
        self.log.append(('call', op, args[1:]))
        if op == S3.GPU_CLAIM:
            if not self.claimable:
                return (-3) & (2**64 - 1)
            if not self.gpu_rec['arena_base']:
                self.gpu_rec.update(arena_base=ARENA, arena_size=R.ARENA_SIZE, arena_source=2)
                self.s3['arena'] = ARENA
            self.gpu_rec['claims'] += 1
            return 0
        if op == S3.GPU_SMC:
            fid, x1, x2, x3 = args[1:5]
            if (fid, x1, x2, x3) == S3.SMC_CP_APERTURE and self.gpu_rec['version'] >= 3:
                # qcom_scm_set_gpu_smmu_aperture(0): PROVEN call shape (qcom_scm.c); the model only counts it.
                self.gpu_rec['smc_calls'] += 1
                if self.cp_running:
                    self.violations.append(('order', hex(fid), 'aperture SMC after the SQE started'))
                if self.s3['aperture_fail']:
                    self.gpu_rec['smc_status'] = self.s3['aperture_fail'] & (2**64 - 1)
                    return self.s3['aperture_fail'] & (2**64 - 1)
                self.aperture_calls += 1
                self.gpu_rec['smc_res0'] = self.s3['aperture_x0'] & (2**64 - 1)
                self.gpu_rec['smc_status'] = 0
                if not self.s3['aperture_x0']:
                    self.gpu_rec['smc_ok'] += 1
                return 0
            if (fid, x1, x2, x3) != S3.SMC_GPU_INIT:
                self.gpu_rec['smc_refused'] += 1
                return (-6) & (2**64 - 1)
            self.gpu_rec['smc_calls'] += 1
            if self.s3['smc_fail']:              # EINTR after the resumes, or WAITQ: the call stays open in TZ
                self.gpu_rec['smc_status'] = self.s3['smc_fail'] & (2**64 - 1)
                self.gpu_rec['smc_interrupted'] += 1025 if self.s3['smc_fail'] == -10 else 0
                self.tz_open = True
                return self.s3['smc_fail'] & (2**64 - 1)
            if self.s3['smc_trap']:
                self.gpu_rec['smc_undefined'] += 1
                return (-7) & (2**64 - 1)
            self.gpu_rec['smc_res0'] = self.s3['smc_x0'] & (2**64 - 1)
            if not self.s3['smc_x0']:
                self.gpu_rec['smc_ok'] += 1
                self.tz_done = True      # TrustZone state: survives a chainload, unlike the stage's record
                self.fuse = 0x7          # INFERRED: TZ programs SW_FUSE_VALUE (RAYTRACING|FASTBLEND|LPAC)
            return 0
        if op == S3.GPU_RELEASE:
            self.gpu_rec.update(arena_base=0, arena_size=0, arena_source=0)
            return 0
        if op == S3.GPU_STORE32 and self.gpu_rec['version'] >= 2:
            addr, value = args[1], args[2]
            if not self.in_arena(addr, 4) or addr % 4 or value >> 32:
                return (-1) & (2**64 - 1)
            self.log.append(('store32', addr, value))
            self.civac(addr, 4)                      # gpu_store32: DC CIVAC, STR, DC CIVAC on the target
            self.cpu_write(addr, struct.pack('<I', value))
            self.civac(addr, 4)
            return 0
        return (-1) & (2**64 - 1)

    # --- registers ------------------------------------------------------------------------------------
    def _read(self, addr):
        block = R.block_of(addr)
        name = block.name if block else None
        if name == 'GPUCC' and addr - 0x03D90000 < 0x30:
            self.gpucc_reachable(addr)
            return self._pll_read(addr - 0x03D90000)
        if name == 'CX_MISC' and addr == 0x03D9F000:
            self.gdsc_steady(addr)
            return self.fuse
        if name == 'CX_MISC' and 0x03D9E040 <= addr < 0x03D9E054:
            self._cx_ok(addr)
            return self.cx_ahb[(addr - 0x03D9E040) // 4]
        if name == 'IMEM':
            return self.imem.get(addr, 0)
        if name == 'SMMU':
            return self._smmu_read(addr - R.SMMU_BASE)
        if name == 'APPS_RSC':
            return self._rsc_read(addr)
        if name in ('GMU', 'RSCC', 'GBIF'):
            self._cx_ok(addr)
            return self._gmu_read(addr)
        if name == 'GPU':
            self._gx_ok(addr)
            return self._gx_read(addr)
        return super()._read(addr)

    def _write(self, addr, value):
        block = R.block_of(addr)
        name = block.name if block else None
        if addr == self.s3['serror_gmu_write']:
            self.raise_serror(addr)
        if name == 'GPUCC' and addr - 0x03D90000 < 0x30:
            self.gpucc_reachable(addr)
            return self._pll_write(addr - 0x03D90000, value)
        if name == 'CX_MISC' and 0x03D9E040 <= addr < 0x03D9E054:
            self._cx_ok(addr)
            if not self.s3['ahb_stuck']:
                self.cx_ahb[(addr - 0x03D9E040) // 4] = value
            return
        if name == 'IMEM':
            self.violate('write', addr, 'IMEM is read-only for the tool')
            return
        if name == 'SMMU':
            return self._smmu_write(addr - R.SMMU_BASE, value)
        if name == 'APPS_RSC':
            return self._rsc_write(addr, value)
        if name in ('GMU', 'RSCC', 'GBIF'):
            self._cx_ok(addr)
            return self._gmu_write(addr, value)
        if name == 'GPU':
            self._gx_ok(addr)
            return self._gx_write(addr, value)
        return super()._write(addr, value)

    def write64(self, addr, value):
        self.write32(addr, value & 0xFFFFFFFF)
        self.write32(addr + 4, value >> 32)

    def _cx_ok(self, addr):
        if not (self.gdsc_on and self.ahb_on() and self.branch_running('GPU_CC_CX_GMU_CBCR')):
            self.violate('serror', addr, 'GMU/GBIF/RSCC access without the CX GDSC and GMU clocks')

    def _gx_ok(self, addr):
        if not self.gx_on:
            self.violate('hang', addr, 'GX access while GX is off (the GMU never reported it on)')

    # --- PLL0 (INFERRED lock conditions; PROVEN register sequence in Linux) --------------------------
    def rails_ok(self):
        return self.rpmh.get(0x30000, 0) >= 2 and self.rpmh.get(0x30010, 0) >= 2

    def _pll_read(self, off):
        if off == 0x00:
            mode = self.pll[0]
            if self.pll_lock_reads is not None:
                self.pll_lock_reads -= 1
                if self.pll_lock_reads <= 0:
                    self.pll_lock_reads = None
                    self.pll[0] |= R.PLL_MODE_LOCK_DET
                    self.pll[0] &= ~R.PLL_MODE_PCAL_NOT_DONE
                    self.pll_calibrated = True
                mode = self.pll[0]
            return mode
        return self.pll.get(off, 0)

    def _pll_write(self, off, value):
        if off == 0x04:
            self.pll[4] = value
            if value & 1:
                good = (self.pll[0x10] == R.PLL0_L_VAL and self.pll[0x14] == R.PLL0_ALPHA
                        and self.pll[0x20] == R.PLL0_CONFIG_CTL and self.pll[0x24] == R.PLL0_CONFIG_CTL_U
                        and self.pll[0x28] == R.PLL0_CONFIG_CTL_U1 and self.pll[0] & R.PLL_MODE_RESET_N)
                if not self.rails_ok():
                    self.gmu_fail.append('PLL0 RUN with MX/CX below LOW_SVS (INFERRED)')
                if good and self.rails_ok() and self.s3['pll_locks']:
                    self.pll_lock_reads = 2
            else:
                self.pll[0] &= ~R.PLL_MODE_LOCK_DET
                self.pll_lock_reads = None
            return
        if off == 0x00 and value & R.PLL_MODE_OUTCTRL and not self.pll[0] & R.PLL_MODE_LOCK_DET:
            self.violations.append(('order', hex(0x03D90000), 'PLL0 output enabled before lock'))
        self.pll[off] = value if off else (value & ~(R.PLL_MODE_LOCK_DET | R.PLL_MODE_PCAL_NOT_DONE)) | \
            (self.pll[0] & (R.PLL_MODE_LOCK_DET | R.PLL_MODE_PCAL_NOT_DONE))

    def pll_even_running(self):
        return bool(self.pll[0] & R.PLL_MODE_LOCK_DET and self.pll[0] & R.PLL_MODE_OUTCTRL
                    and self.pll[0x18] & 2 and self.pll[4] & 1)

    def source_running(self, src):
        if src in (1, 2, 3):
            return self.pll_even_running()
        return super().source_running(src)

    # --- SMMU ------------------------------------------------------------------------------------------
    def _smmu_read(self, off):
        if off < 0x1000:
            fixed = {0x0: self.scr0, 0x20: 0x7C017E19, 0x24: 0x40000019, 0x28: self.s3['idr2'], 0x3C: 0x24,
                     0x48: self.gfsr, 0x50: 0, 0x54: self.gfsynr1}
            if off in fixed:
                return fixed[off]
            if 0x800 <= off < 0x800 + 4 * 25:
                return self.smr[(off - 0x800) // 4]
            if 0xC00 <= off < 0xC00 + 4 * 25:
                return self.s2cr[(off - 0xC00) // 4]
            return 0
        if off < 0x2000:
            n = (off - 0x1000) % 0x400 // 4
            if off < 0x1400:
                return self.cbar[n]
            if 0x1400 <= off < 0x1400 + 4 * 25:
                return self.cbfrsynra[n]
            if 0x1800 <= off < 0x1800 + 4 * 25:
                return self.cba2r[(off - 0x1800) // 4]
            return 0
        cb, reg = divmod(off - (R.SMMU_CB_BASE - R.SMMU_BASE), 0x1000)
        if reg == 0x7F4:
            return 0
        return self.cb[cb].get(reg, 0)

    def _smmu_write(self, off, value):
        if off < 0x1000:
            if off == 0x48:
                self.gfsr &= ~value                  # sGFSR is write-1-to-clear
            elif off == 0x0:
                self.scr0 = value
            elif 0x800 <= off < 0x800 + 4 * 25:
                self.smr[(off - 0x800) // 4] = value
            elif 0xC00 <= off < 0xC00 + 4 * 25:
                self.s2cr[(off - 0xC00) // 4] = value
            return
        if off < 0x2000:
            if off < 0x1400:
                self.cbar[(off - 0x1000) // 4] = value
            elif 0x1800 <= off < 0x1800 + 4 * 25:
                self.cba2r[(off - 0x1800) // 4] = value
            return
        cb, reg = divmod(off - (R.SMMU_CB_BASE - R.SMMU_BASE), 0x1000)
        if reg == 0x58:
            self.cb[cb][0x58] &= ~value
            return
        if reg == 0 and not value & R.SCTLR_M and self.routed_to(cb):
            # PROVEN (Arm SMMUv2): a stage-1 bank with SCTLR.M clear passes the input address
            # through untranslated; a valid SMR whose S2CR translates through it is a DMA hole.
            self.violations.append(('bypass', hex(off), f'CB{cb} disabled while SMR(s) {self.routed_to(cb)} route to it'))
        if reg in (0x20, 0x24, 0x30, 0x10) and self.cb[cb].get(0, 0) & R.SCTLR_M:
            self.violations.append(('order', hex(off), f'CB{cb} translation registers changed while enabled'))
        if self.s3['cb_fixed_bits']:
            # PROVEN on the A16 (stage3-run1-b1.json): SCTLR.ASIDPNE reads 0, TCR2 bits 5/6 read 1.
            if reg == 0:
                value &= ~R.SCTLR_ASIDPNE
            elif reg == 0x10:
                value |= 0x60
        if self.s3['cb_stuck'] and reg == 0x30:
            return                                  # a MAIR0 that does not take the write
        self.cb[cb][reg] = value

    def routed_to(self, cb):
        return [n for n in range(25) if self.smr[n] & R.SMR_VALID and self.s2cr[n] >> 16 & 3 == R.S2CR_TYPE_TRANS
                and self.s2cr[n] & 0xFF == cb]

    def translate(self, sid, va, write=False, priv=True, quiet=False):
        """quiet: the test harness asking, not a device transaction (no fault state is recorded)."""
        if not quiet:
            return self._translate(sid, va, write, priv)
        saved = (self.gfsr, self.gfsynr1, list(self.cbfrsynra), [dict(cb) for cb in self.cb], len(self.smmu_faults))
        result = self._translate(sid, va, write, priv)
        self.gfsr, self.gfsynr1, self.cbfrsynra, self.cb = saved[0], saved[1], saved[2], saved[3]
        del self.smmu_faults[saved[4]:]
        return result

    def _translate(self, sid, va, write, priv):
        hits = [n for n in range(25) if S3.smr_matches(self.smr[n], sid)]
        if not hits:
            if self.scr0 & R.SCR0_USFCFG:
                self.smmu_faults.append((sid, hex(va), 'unmatched (USFCFG)'))
                self.gfsr |= 1 << 1                  # sGFSR.USF (Arm SMMUv2)
                self.gfsynr1 = sid
                return None
            self.violations.append(('bypass', hex(va), f'SID {sid} bypassed the SMMU'))
            return va
        if len(hits) > 1:
            self.smmu_faults.append((sid, hex(va), 'stream match conflict'))
            return None
        s2cr = self.s2cr[hits[0]]
        kind, cb = s2cr >> 16 & 3, s2cr & 0xFF
        if kind != R.S2CR_TYPE_TRANS:
            self.smmu_faults.append((sid, hex(va), 'S2CR fault' if kind == 2 else 'S2CR bypass'))
            return None if kind == 2 else va
        regs = self.cb[cb]
        if not regs.get(0, 0) & R.SCTLR_M:
            self.violations.append(('bypass', hex(va), f'SID {sid} through disabled CB{cb}: untranslated'))
            return va
        t0sz = regs.get(0x30, 0) & 0x3F     # CB0 may run --va-wide's T0SZ 30; every other bank T0SZ 32
        if not self.cba2r[cb] & 1 or t0sz not in ((R.CB_T0SZ, R.CB_T0SZ_WIDE) if cb == R.GPU_CB else (R.CB_T0SZ,)) \
                or self.cbar[cb] >> 16 & 3 != 1:
            self._cb_fault(cb, va, 1 << 1, f'CB{cb} not configured for an AArch64 S1 walk')
            return None
        ttbr = regs.get(0x24, 0) << 32 | regs.get(0x20, 0)
        result = S3.walk(lambda a: struct.unpack('<Q', self.dev_read(a, 8))[0], ttbr, va, t0sz)
        self.cbfrsynra[cb] = sid if result is None else self.cbfrsynra[cb]
        if result is None:
            self._cb_fault(cb, va, 1 << 1, 'translation fault')
            return None
        pa, leaf = result
        if write and leaf & R.PTE_AP_RDONLY or not priv and not leaf & R.PTE_AP_UNPRIV:
            self._cb_fault(cb, va, 1 << 3, 'permission fault')
            return None
        return pa

    def _cb_fault(self, cb, va, bits, why):
        self.cb[cb][0x58] = self.cb[cb].get(0x58, 0) | bits
        self.cb[cb][0x60] = va & 0xFFFFFFFF
        if va >> 32:
            self.cb[cb][0x64] = va >> 32            # FAR's high word (a --va-wide CB0 VA above 4 GiB)
        self.smmu_faults.append((cb, hex(va), why))

    def dma_read(self, sid, va, size, priv=True):
        out = b''
        for page in range(va & ~0xFFF, va + size, 0x1000):
            start, end = max(va, page), min(va + size, page + 0x1000)
            pa = self.translate(sid, start, priv=priv)
            if pa is None:
                return None
            out += self.dev_read(pa, end - start)
        return out

    def dma_write(self, sid, va, data, priv=True):
        pa = self.translate(sid, va, write=True, priv=priv)
        if pa is None:
            return False
        self.dev_write(pa, data)
        return True

    # --- apps RSC --------------------------------------------------------------------------------------
    TCS_REGS = {0x20: 'WAIT', 0x24: 'CONTROL', 0x28: 'STATUS', 0x2C: 'CMD_ENABLE'}

    def _tcs_set(self, tcs, reg, value):
        self.rsc[('tcs', tcs, reg)] = value

    def _tcs_set_cmd(self, tcs, cmd, reg, value):
        self.rsc[('cmd', tcs, cmd, reg)] = value

    def _rsc_decode(self, addr):
        if addr < R.RSC_TCS:
            return ('drv', addr - R.RSC_DRV2)
        off = addr - R.RSC_TCS
        if off < 0x10:
            return ('irq', off)
        tcs, reg = divmod(off, 672)
        if reg in self.TCS_REGS:
            return ('tcs', tcs, self.TCS_REGS[reg])
        if 0x34 <= reg < 0x34 + 16 * 24:
            cmd, sub = divmod(reg - 0x34, 24)
            return ('cmd', tcs, cmd, {0: 'MSGID', 4: 'ADDR', 8: 'DATA', 0xC: 'STATUS', 0x10: 'RESP'}[sub])
        return ('other', off)

    def _rsc_read(self, addr):
        key = self._rsc_decode(addr)
        if key == ('drv', 0):
            return 0x00030000
        if key == ('drv', 0xC):
            return (8 << 12) | (16 << 27)
        if key == ('irq', 4):
            return self.rsc_irq
        if key[0] == 'tcs' and key[2] == 'STATUS':
            return 0 if self.s3['rsc_busy'] and key[1] == 0 else 1
        return self.rsc.get(key, 0)

    def _rsc_write(self, addr, value):
        key = self._rsc_decode(addr)
        if key == ('irq', 8):
            self.rsc_irq &= ~value
            return
        self.rsc[key] = value
        if key[0] == 'tcs' and key[2] == 'CONTROL' and value & R.TCS_AMC_MODE_TRIGGER:
            tcs = key[1]
            if not value & R.TCS_AMC_MODE_ENABLE:
                self.violations.append(('order', hex(addr), 'TCS triggered without AMC_MODE_ENABLE'))
                return
            enable = self.rsc.get(('tcs', tcs, 'CMD_ENABLE'), 0)
            for cmd in range(16):
                if enable >> cmd & 1:
                    target = self.rsc.get(('cmd', tcs, cmd, 'ADDR'), 0)
                    data = self.rsc.get(('cmd', tcs, cmd, 'DATA'), 0)
                    if target in (0x30000, 0x30010) and data < self.rpmh.get(target, 0):
                        self.violations.append(('vote', hex(target), f'vote lowered to {data}'))
                    self.rpmh[target] = data
                    self.rsc[('cmd', tcs, cmd, 'STATUS')] = R.CMD_STATUS_COMPL | R.CMD_STATUS_ISSUED
            self.rsc_irq |= 1 << tcs

    # --- GMU -----------------------------------------------------------------------------------------------
    def _gmu_read(self, addr):
        regs = R.REGS
        if regs['GMU_CM3_ITCM'].addr <= addr < regs['GMU_CM3_ITCM'].at(0xFFF) + 4:
            return self.itcm[(addr - regs['GMU_CM3_ITCM'].addr) // 4]
        if regs['GMU_CM3_DTCM'].addr <= addr < regs['GMU_CM3_DTCM'].at(0xFFF) + 4:
            return self.dtcm[(addr - regs['GMU_CM3_DTCM'].addr) // 4]
        if addr == regs['GMU_CM3_FW_INIT_RESULT'].addr and self.init_countdown is not None:
            self.init_countdown -= 1
            if self.init_countdown <= 0:
                self.init_countdown = None
                self.gmu[addr] = R.GMU_INIT_RESULT_OK
                self.gmu_running = True
        if addr == regs['GMU_HFI_CTRL_STATUS'].addr and self.hfi_countdown is not None:
            self.hfi_countdown -= 1
            if self.hfi_countdown <= 0:
                self.hfi_countdown = None
                self.gmu[addr] = 1
        if addr == regs['GMU_GMU2HOST_INTR_INFO'].addr:
            return self.g2h
        if addr == regs['GMU_PWR_CLK_STATUS'].addr:
            return 0 if self.gx_on else R.PWR_CLK_STATUS_GX_OFF
        if addr == regs['GBIF_HALT_ACK'].addr:
            return self.gbif_ack
        if addr == regs['RSCC_RSC_STATUS0_DRV0'].addr:
            return (1 << 16) if self.pdc_asleep else 0
        if addr == regs['GMU_RSCC_CONTROL_ACK'].addr:
            return self.rscc_ack
        if addr == regs['GMU_RPMH_POWER_STATE'].addr:
            return 0 if self.gmu_running else 0xF
        if addr in (regs['GMU_AHB_FENCE_STATUS'].addr, regs['GMU_AO_GPU_CX_BUSY_STATUS'].addr,
                    regs['GMU_AO_HOST_INTERRUPT_STATUS'].addr):
            return 0
        if addr == regs['GBIF_HALT'].addr:
            return getattr(self, 'gbif_halt_reg', self.gbif_halt) | getattr(self, 'gbif_halt_stuck', 0)
        return self.gmu.get(addr, 0)

    def _gmu_write(self, addr, value):
        regs = R.REGS
        for tcm, reg in ((self.itcm, 'GMU_CM3_ITCM'), (self.dtcm, 'GMU_CM3_DTCM')):
            if regs[reg].addr <= addr < regs[reg].at(0xFFF) + 4:
                if not self.gmu.get(regs['GMU_CM3_SYSRESET'].addr, 1) & 1:
                    self.violations.append(('order', hex(addr), 'TCM written while the CM3 runs'))
                tcm[(addr - regs[reg].addr) // 4] = value
                return
        if addr == regs['GMU_CM3_SYSRESET'].addr:
            was = self.gmu.get(addr, 1)
            self.gmu[addr] = value
            if value & 1:
                self.gmu_running = False
                self.init_countdown = self.hfi_countdown = None
                self.gx_collapse()
                self.hfi_state = set()
            elif was & 1:
                self._gmu_boot()
            return
        if addr == regs['GMU_HFI_CTRL_INIT'].addr and value & 1:
            if self.gmu_running:
                self.hfi_countdown = 2
            return
        if addr == regs['GMU_GMU2HOST_INTR_CLR'].addr:
            self.g2h &= ~value
            return
        if addr == regs['GMU_HOST2GMU_INTR_SET'].addr:
            return self._doorbell(value)
        if addr == regs['GBIF_HALT'].addr:
            self.gbif_halt = value & 3
            self.gbif_halt_reg = value
            if value & 3 or not self.s3['gbif_sticky_ack']:
                # PROVEN on the A16 (run 2): with no halt requested the ack reads 0x3 and neither a 0 write
                # nor KGSL's BIT(3) changes it; a halt request sets its bits as before.
                self.gbif_ack = (self.gbif_ack | value) & 3 if self.s3['gbif_sticky_ack'] else value & 3
            return
        if addr == regs['GMU_RSCC_CONTROL_REQ'].addr:
            self.gmu[addr] = value
            if value & 1:
                self.pdc_asleep, self.rscc_ack = True, 0
            if value & 2 and self.s3['rscc_wakes']:
                self.pdc_asleep, self.rscc_ack = False, 2
            if not value:
                self.rscc_ack = 0
            return
        self.gmu[addr] = value

    def expected_fw(self):
        plan = S3.plan_gmu_fw(S3.parse_gmu_fw(GMU))
        return plan

    def _gmu_boot(self):
        """INFERRED boot checks: what a6xx_gmu_fw_start/a6xx_gmu_start set up before SYSRESET 0."""
        regs, g, why = R.REGS, self.gmu, []
        plan = self.expected_fw()
        for name, tcm in (('itcm', self.itcm), ('dtcm', self.dtcm)):
            for word, data in plan[name]:
                if tcm[word:word + len(data) // 4] != [S3.u32(data, n) for n in range(len(data) // 4)]:
                    why.append(f'{name} content')
        want = {'GMU_CM3_BOOT_CONFIG': 2, 'GMU_HFI_QTBL_ADDR': R.GMU_HFI_VA, 'GMU_HFI_QTBL_INFO': 1,
                'GMU_AHB_FENCE_RANGE_0': R.GMU_FENCE_RANGE_0, 'GMU_CM3_CFG': 0x4052, 'GMU_GENERAL_10': 0x8010100,
                'GMU_GENERAL_8': 0x60004003, 'GBIF_CX_CONFIG': 0x20023000, 'GMU_MRC_GBIF_QOS_CTRL': 0x33,
                'GMU_SYS_BUS_CONFIG': 1, 'GMU_ICACHE_CONFIG': 1, 'GMU_DCACHE_CONFIG': 1, 'GMU_CX_FAL_INTF': 1,
                'GMU_CX_FALNEXT_INTF': 1, 'GBIF_QSB_SIDE0': 0x71E20, 'GBIF_QSB_SIDE3': 0x71E20}
        for name, value in want.items():
            if g.get(regs[name].addr) != value:
                why.append(f'{name} = {g.get(regs[name].addr)}')
        if not (self.rcgs[0x9318]['cfg'] & 0x71F == R.GMU_RCG_CFG_575 and self.pll_even_running()):
            why.append('GMU clock is not 575 MHz from PLL0_OUT_EVEN')
        if not self.tz_done:
            why.append('TrustZone GPU init not done (INFERRED requirement)')
        icache = plan['memory'][0][2]
        fetched = self.dma_read(R.GMU_SID, R.GMU_ICACHE_VA, len(icache))
        if fetched != icache:
            why.append('icache fetch through SID 5 does not return the firmware')
        for va, length in plan['prealloc']:
            if self.dma_read(R.GMU_SID, va, 4) is None:
                why.append(f'prealloc {va:#x} unmapped')
        table = self.dma_read(R.GMU_SID, R.GMU_HFI_VA, 24)
        if table is None or struct.unpack('<6I', table) != (0, 168, 6, 12, 3, 3):
            why.append('HFI queue table not visible to the GMU')
        if g.get(regs['GMU_CM3_FW_INIT_RESULT'].addr) != 0:
            why.append('INIT_RESULT not cleared before start')
        if self.pdc_asleep:
            why.append('GPU RSC asleep: a6xx_rpmh_stop without a6xx_rpmh_start before the start')
        self.gmu_fail = why
        if not why and self.s3['gmu_boots']:
            self.init_countdown = self.s3['gmu_init_delay']
            self.oob_since_boot = False
            self.hfi_state = set()
            self.hfi_seq_seen = []

    def _queue(self, index):
        header = self.dma_read(R.GMU_SID, R.GMU_HFI_VA + 24 + 48 * index, 48, priv=False)
        return list(struct.unpack('<12I', header))

    def _doorbell(self, value):
        if not self.gmu_running:
            return
        if value & 1:
            self._hfi_process()
        if value & (1 << R.OOB_GPU_SET_REQUEST):
            if self.s3['oob_acks'] and {'START', 'VOTE'} <= self.hfi_state:
                self.gx_on = bool(self.s3['oob_gx_on'])
                self.oob_held = True
                self.oob_since_boot = True
                self.g2h |= 1 << R.OOB_GPU_SET_ACK
        if value & (1 << R.OOB_GPU_SET_CLEAR):
            self.oob_held = False

    def _hfi_process(self):
        cmd = self._queue(0)
        size, read, write = cmd[3], cmd[10], cmd[11]
        base = R.GMU_HFI_VA + 4096
        while read != write:
            words = self.dma_read(R.GMU_SID, base + 4 * read, 4, priv=False)
            header = struct.unpack('<I', words)[0]
            dwords = header >> 8 & 0xFF
            msg = [struct.unpack('<I', self.dma_read(R.GMU_SID, base + 4 * ((read + n) % size), 4, priv=False))[0]
                   for n in range(dwords)]
            read = (read + dwords + 3) // 4 * 4 % size
            error = self._hfi_message(msg)
            self.hfi_seen.append((header & 0xFF, error))
            self._respond(header, error)
        if self.s3['gmu_index_delay']:
            # INFERRED: the GMU may post the ack before it moves its command read index; the
            # index write then lands while the host is already handling the ack.
            pa = self.translate(R.GMU_SID, R.GMU_HFI_VA + 24 + 40, write=True, priv=False)
            self.gmu_pending.append([self.s3['gmu_index_delay'], pa, struct.pack('<I', read)])
            return
        self.dma_write(R.GMU_SID, R.GMU_HFI_VA + 24 + 40, struct.pack('<I', read), priv=False)

    def _hfi_message(self, msg):
        ident, state = msg[0] & 0xFF, self.hfi_state
        if self.s3['hfi_error_on'] == ident:
            return 22
        if ident == R.HFI_MSG_TABLE:
            if msg[1] != 0 or msg[2] != R.HFI_TABLE_GPU_PERF or msg[4] != 3 or len(msg) != 1 + 4 + 3 * msg[3] + 2 + 2 * msg[5 + 3 * msg[3]]:
                return 1
            state.add('TABLE')
        elif ident == R.HFI_MSG_BW_TABLE:
            if len(msg) != 160 or 'TABLE' not in state:
                return 2
            state.add('BW')
        elif ident == R.HFI_MSG_CORE_FW_START:
            if not {'TABLE', 'BW'} <= state:
                return 3
            state.add('CORE')
        elif ident == R.HFI_MSG_START:
            if 'CORE' not in state:
                return 4
            state.add('START')
        elif ident == R.HFI_MSG_GX_BW_PERF_VOTE:
            if 'START' not in state or msg[2] != 1 or msg[3] != 1:
                return 5
            state.add('VOTE')
        elif ident == R.HFI_MSG_PREPARE_SLUMBER:
            if self.oob_held:
                return 6             # PROVEN: Linux clears GPU_SET before slumbering
            if not self.oob_since_boot:
                # PROVEN (a6xx_gmu_shutdown): "GMU firmware's internal power state gets messed up
                # if we send prepare_slumber hfi when oob_gpu handshake wasn't done after the last
                # wake up" -- Linux does a dummy GPU_SET/clear first.
                self.violations.append(('order', 'PREPARE_SLUMBER', 'no OOB GPU_SET handshake since the GMU started'))
            if self.gmu.get(R.REGS['GMU_POWER_COUNTER_ENABLE'].addr):
                # PROVEN order (a6xx_gmu_notify_slumber): the power counter goes off first
                self.violations.append(('order', 'PREPARE_SLUMBER', 'power counter still enabled'))
            self.gx_collapse()
            self.slumbered = True
        else:
            return 7
        return 0

    def _respond(self, header, error):
        rsp = self._queue(1)
        size, write = rsp[3], rsp[11]
        if self.s3['legacy_ack']:
            words = [R.HFI_F2H_MSG_ACK | 3 << 8, header, error, 0xFAFAFAFA]
        else:   # PROVEN on the A16 (fw 0x50020026): the command's id, type ACK, seq 0
            words = [header & 0xFF | R.HFI_MSG_TYPE_ACK << 16 | 3 << 8, header, error, 0xFAFAFAFA]
        base = R.GMU_HFI_VA + 2 * 4096
        for n, word in enumerate(words):
            self.dma_write(R.GMU_SID, base + 4 * ((write + n) % size), struct.pack('<I', word), priv=False)
        self.dma_write(R.GMU_SID, R.GMU_HFI_VA + 24 + 48 + 44, struct.pack('<I', (write + 4) % size), priv=False)
        self.g2h |= R.GMU2HOST_MSGQ

    # --- GX / CP --------------------------------------------------------------------------------------------
    def gx_collapse(self):
        """GX power collapse loses the CP and RBBM state (INFERRED: what a GX GDSC off implies)."""
        self.gx_on = False
        self.gx, self.cp_running, self.me_init, self.rptr, self.secvid, self.rbbm_gbif_halt = {}, False, False, 0, 1, 0

    def _gx_read(self, addr):
        regs = R.REGS
        if addr == regs['CP_RB_RPTR'].addr:
            return self.rptr
        if addr == regs['RBBM_SECVID_TRUST_CNTL'].addr:
            return self.secvid
        if addr == regs['RBBM_GBIF_HALT_ACK'].addr:
            return self.rbbm_gbif_halt
        if addr in (regs['RBBM_STATUS'].addr, regs['RBBM_INT_0_STATUS'].addr):
            return 0
        return self.gx.get(addr, 0)

    def _gx_write(self, addr, value):
        regs = R.REGS
        if self.cp_running and addr in (regs['CP_SQE_INSTR_BASE'].addr, regs['CP_RB_BASE'].addr,
                                        regs['CP_RB_CNTL'].addr, regs['CP_SQE_CNTL'].addr):
            # PROVEN (a8xx hw_init): the SQE and RB bases are programmed before SQE_CNTL = 1, once.
            self.violations.append(('cp', hex(addr), 'CP base/control rewritten under a running SQE'))
        self.gx[addr] = value
        if addr == regs['CP_RB_RPTR_ADDR_BV_HI'].addr and self.s3['prestart_fault']:
            self.gfsr |= 1 << 1                     # a USF recorded before the SQE starts (SID 1)
            self.gfsynr1 = 1
        if addr == regs['CP_APERTURE_CNTL_HOST'].addr:
            self.aperture = value
        elif addr in (regs['RB_GC_GMEM_PROTECT'].addr, regs['CP_PROTECT_CNTL_PIPE'].addr,
                      regs['CP_APRIV_CNTL_PIPE'].addr):
            self.pipe_writes.append((addr, value, self.aperture >> 12 & 0xF))
        elif addr == regs['RBBM_GBIF_HALT'].addr:
            self.rbbm_gbif_halt = value
        elif addr == regs['RBBM_SECVID_TRUST_CNTL'].addr:
            if self.me_init and not self.s3['secvid_locked']:
                self.secvid = value
            # PROVEN in spirit (a8xx hw_init comment): written before the CP is up it is blocked.
        elif addr == regs['CP_SQE_CNTL'].addr and value & 1:
            if self.s3['sqe_reset']:
                # Run 1 (stage3-run1-c3.json): no reply to this write, the A16 left USB. The model's link dies.
                self.dead = True
                raise Q.ProxyError('expected 1 bytes, got 0')
            base = self.gx.get(regs['CP_SQE_INSTR_BASE'].addr, 0) | self.gx.get(regs['CP_SQE_INSTR_BASE_HI'].addr, 0) << 32
            fetched = self.dma_read(R.GPU_SID, base, 256)
            self.cp_running = fetched is not None and fetched == SQE[4:4 + 256]
            if fetched is None:
                # INFERRED: an aborted SQE fetch leaves the CP idle; the fault is in CB0 (FSR/FAR/CBFRSYNRA).
                self.sqe_fetch_fault = True
            elif not self.cp_running:
                self.violations.append(('cp', hex(base), 'SQE fetched the wrong image'))
        elif addr == regs['CP_RB_WPTR'].addr:
            self._cp_run(value)

    def _cp_run(self, wptr):
        if not self.cp_running or not self.s3['cp_executes']:
            return
        regs = R.REGS
        base = self.gx.get(regs['CP_RB_BASE'].addr, 0) | self.gx.get(regs['CP_RB_BASE_HI'].addr, 0) << 32
        while self.rptr < wptr:
            header = struct.unpack('<I', self.dma_read(R.GPU_SID, base + 4 * self.rptr, 4) or b'\0' * 4)[0]
            count, opcode = header & 0x3FFF, header >> 16 & 0x7F
            if header >> 28 != 7 or R.pkt7(opcode, count) != header:
                self.violations.append(('cp', hex(header), 'bad PKT7 header'))
                self.cp_running = False
                return
            body = [struct.unpack('<I', self.dma_read(R.GPU_SID, base + 4 * (self.rptr + 1 + n), 4))[0]
                    for n in range(count)]
            if opcode == R.CP_ME_INIT:
                if body[0] & (1 << 8):
                    self.violations.append(('cp', hex(body[0]), 'CP_ME_INIT asks for a pwrup list at 0'))
                    self.cp_running = False
                    return
                self.me_init = True
            elif opcode == R.CP_MEM_WRITE:
                if not self.me_init:
                    self.violations.append(('cp', hex(header), 'CP_MEM_WRITE before CP_ME_INIT'))
                    return
                va = body[0] | body[1] << 32
                self.dma_write(R.GPU_SID, va, struct.pack(f'<{count - 2}I', *body[2:]))
            elif opcode != R.CP_THREAD_CONTROL:
                self.violations.append(('cp', hex(opcode), 'unexpected opcode'))
            self.rptr += 1 + count
        shadow = self.gx.get(regs['CP_RB_RPTR_ADDR'].addr, 0) | self.gx.get(regs['CP_RB_RPTR_ADDR_HI'].addr, 0) << 32
        self.dma_write(R.GPU_SID, shadow, struct.pack('<I', self.rptr))


def cmd_db_blob():
    entries = [(k.encode(), int(a, 16), bytes.fromhex(aux)) for k, (a, aux) in A16_CMD_DB.items()]
    header = struct.pack('<I4s', 1, R.CMD_DB_MAGIC)
    data_len = sum(len(aux) for *_x, aux in entries)
    header += struct.pack('<5H3H', 3, 0, len(entries) * 24, len(entries), 1, 0, 0, 0)
    header += bytes(16 * 7) + bytes(8)
    body, aux_blob = b'', b''
    for name, addr, aux in entries:
        body += name.ljust(8, b'\0') + bytes(8) + struct.pack('<IHH', addr, len(aux), len(aux_blob))
        aux_blob += aux
    assert data_len == len(aux_blob)
    return (header + body + aux_blob).ljust(R.CMD_DB[1], b'\0')


# --- running ---------------------------------------------------------------------------------------------
def run3(fake, command, **kw):
    args = S3.Args(**kw)
    status, out = S3.run(fake, command, args)
    return status, out


def happy_path(fake, fw_dir, upto='cp-fence'):
    order = ['gmu-smmu', 'gmu-boot', 'hfi', 'oob', 'cp-fence']
    results = {}
    for command in order[:order.index(upto) + 1]:
        status, out = run3(fake, command, fw_dir=fw_dir)
        results[command] = (status, out)
        check(status == 0, f'{command} completes ({out["stopped"]})')
    return results


def smc_count(fake, call):
    return sum(1 for entry in fake.log if entry[0] == 'call' and entry[1] == S3.GPU_SMC and tuple(entry[2][:4]) == call)


def no_violation(fake, label):
    check(not fake.violations, f'{label}: model saw no violation ({fake.violations[:4]})')


def writes_to(fake, block_names, after=0):
    return [(hex(a), v) for op, a, v in fake.log[after:] if op == 'w' and (R.block_of(a) or R.BLOCKS[0]).name in block_names]


# --- tests -------------------------------------------------------------------------------------------------
def test_registry():
    fixed = {'GMU_CM3_SYSRESET': 0x03D7E000, 'GMU_CM3_FW_INIT_RESULT': 0x03D7E070, 'GMU_CM3_BOOT_CONFIG': 0x03D7E004,
             'GMU_CM3_CFG': 0x03D7E0B4, 'GMU_HFI_CTRL_INIT': 0x03D7E618, 'GMU_HFI_CTRL_STATUS': 0x03D7E600,
             'GMU_HFI_QTBL_ADDR': 0x03D7E614, 'GMU_GENERAL_8': 0x03D7E720, 'GMU_GENERAL_9': 0x03D7E724,
             'GMU_GENERAL_10': 0x03D7E728, 'GMU_AHB_FENCE_RANGE_0': 0x03D8EC44, 'GMU_MRC_GBIF_QOS_CTRL': 0x03D7D42C,
             'GMU_SYS_BUS_CONFIG': 0x03D7D03C, 'GMU_ICACHE_CONFIG': 0x03D7D000, 'GMU_DCACHE_CONFIG': 0x03D7D004,
             'GMU_HOST2GMU_INTR_SET': 0x03D7E650, 'GMU_GMU2HOST_INTR_INFO': 0x03D7E648,
             'GMU_PWR_CLK_STATUS': 0x03D7DFA0, 'GMU_CX_FAL_INTF': 0x03D7DFB0, 'GMU_CX_FALNEXT_INTF': 0x03D7DFB4,
             'GMU_CORE_FW_VERSION': 0x03D74FE0, 'GMU_CM3_ITCM': 0x03D6D000, 'GBIF_HALT': 0x03D0F114,
             'GBIF_CX_CONFIG': 0x03D0F000, 'GBIF_QSB_SIDE0': 0x03D0F00C, 'RSCC_RSC_STATUS0_DRV0': 0x03D50010,
             'RSCC_SEQ_BUSY_DRV0': 0x03D50404, 'CP_SQE_INSTR_BASE': 0x03D02058, 'CP_SQE_CNTL': 0x03D02054,
             'CP_RB_WPTR': 0x03D0201C, 'RBBM_SECVID_TRUST_CNTL': 0x03D3D000, 'RBBM_GBIF_HALT': 0x03D00028,
             'GPU_CC_PLL0_L_VAL': 0x03D90010, 'SMMU_CBAR': 0x03DA1000, 'SMMU_CB_SCTLR': 0x03DC0000,
             'RSC_DRV_ID': 0x18920000, 'RSC_IRQ_STATUS': 0x18920D04}
    for name, addr in fixed.items():
        check(R.REGS[name].addr == addr, f'{name} = {addr:#010x}')
    check(R.REGS['SMMU_CB_SCTLR'].at(1) == 0x03DC1000 and R.REGS['SMMU_CBAR'].at(1) == 0x03DA1004,
          'CB1 at SMMU base + 32 pages + 1 page (A16 IDR1 NUMPAGENDXB 4), CBAR(1) in GR1')
    check(R.REGS['RSC_TCS_CMD_ADDR_V3'].at(16 * 5 + 2) == R.RSC_TCS + 672 * 5 + 24 * 2 + 0x38,
          'RSC TCS n command k: tcs_base + 672 n + 24 k + CMD_ADDR (v3)')
    check(R.REGS['RSCC_TCS_DRV0_STATUS'].at(9) == 0x03D50000 + 4 * (0x92E + 4), 'RSCC TCS9 status with seqmem_off 4')
    gx = [r for r in R.REGS.values() if r.domain == 'GX' and r.access]
    check(gx and all(r.gate == 'gx' for r in gx), 'every accessible GX register is behind gate gx')
    check(R.REGS['GMU_CM3_ITCM'].gate == 'gmu_reset' and R.REGS['GMU_CM3_DTCM'].gate == 'gmu_reset',
          'TCM windows only behind gmu_reset')


def test_encoders():
    check(R.pkt7(R.CP_MEM_WRITE, 3) == 0x703D8003, 'PKT7(CP_MEM_WRITE, 3) = 0x703d8003 (freedreno traces)')
    check(R.pkt7(R.CP_ME_INIT, 8) == 0x70C80008, 'PKT7(CP_ME_INIT, 8) = 0x70c80008 (a6xx traces)')
    check(R.pkt7(R.CP_ME_INIT, 7) >> 16 & 0x7F == 72 and R.pm4_parity(7) == 0, 'ME_INIT with 7 payload dwords')
    check(R.CP_ME_INIT_MASK == 0x4B and not R.CP_ME_INIT_MASK & 0x100, 'CP_ME_INIT mask 0x4b: BIT(8) clear')
    check(R.PLL0_L_VAL == 0x0044003B and R.GMU_RCG_CFG_575 == 0x201 and R.HUB_RCG_CFG_200 == 0x505,
          'PLL0 L 0x3b | CAL 0x44 << 16; GMU RCG src 2 div 1; HUB src 5 div 3')
    check(R.GMU_FENCE_RANGE_0 == 0x80C808C0 and R.CB_SCTLR == 0x1167 and R.CB_TCR == 0x802020
          and R.CB_MAIR0 == 0xF404FF44 and R.CBAR_S1_BYPASS_S2 == 0x1F300, 'fence range, SCTLR, TCR, MAIR, CBAR')
    check(len(R.X285_PROTECT) == 52 and R.X285_PROTECT[0] == 0x0E6C0008 and R.X285_PROTECT[-1] == 0x80027C06,
          'x285 protect list encoded as A6XX_PROTECT_RDONLY/NORDWR')
    cmd_db = {k: {'addr': a, 'aux': aux} for k, (a, aux) in A16_CMD_DB.items()}
    perf = S3.perf_table(cmd_db, 1, 1, 1)
    check(perf[:4] == [0, 1, 2, 3] and perf[4:7] == [0, 0xFFFFFFFF, 0]
          and perf[7:10] == [0x00380201, 0x0FFFC2FF, 310000], f'GX perf rows from the A16 cmd-db: {[hex(w) for w in perf]}')
    check(perf[10:12] == [2, 2] and perf[12:14] == [0, 0] and perf[14:16] == [0x00400202, 575000],
          'CX perf rows: 575 MHz at LOW_SVS -> 0x00400202')
    bw, bws = S3.bw_table(cmd_db, 1, 1)
    check(bws == [0, 2136719] and bw[:5] == [2, 1, 3, 1, 0x7], f'BW header {bw[:5]}')
    row1 = bw[5 + 6 + 12 + 8 + 8:5 + 6 + 12 + 8 + 16]
    check(row1[:3] == [S3.bcm_cmd(True, True, 1805, 1805), S3.bcm_cmd(True, True, 1831, 1831),
                       S3.bcm_cmd(True, True, 0, 4)], f'BW row for 2136719 kBps: {[hex(w) for w in row1[:3]]}')
    check(bw[5 + 6 + 12:5 + 6 + 12 + 3] == [0x50004, 0x50000, 0x50068] and len(bw) == 159,
          'DDR BCM addresses from cmd-db; 159 payload dwords (+ header = sizeof(a6xx_hfi_msg_bw_table)/4)')
    # GX_BW_PERF_VOTE (docs: Snapintosh USERSPACE-GRAPHICS "Speed batch"): the default is today's lowest-OPP vote,
    # byte for byte; --max-opps 6 --perf-opp 6 asks for 915 MHz (RPMh 256, NOM) with its 16.5 GB/s row (bw index 4).
    check(S3.perf_vote(1, 1) == ([1, 1, 1], 'GX_BW_PERF_VOTE (ack, perf 1, bw 1: lowest OPP)'),
          'default perf vote unchanged: [1, 1, 1] and the same log name')
    vote, name = S3.perf_vote(1, 6, 6)
    check(vote == [1, 6, 4] and '915 MHz' in name and 'level 256' in name, f'915 MHz vote {vote} {name}')
    perf6 = S3.perf_table(cmd_db, 1, 6, 1)
    check(perf6[2] == 7 and perf6[4 + 6 * 3 + 2] == 915000 and perf6[4 + 6 * 3] >> 16 == 0x100,
          f'6-OPP perf table: 7 GX rows, row 6 = 915 MHz at level 256: {[hex(w) for w in perf6[22:25]]}')
    bw6, bws6 = S3.bw_table(cmd_db, 1, 6)
    check(bws6 == [0, 2136719, 6074219, 12449219, 16500000] and bws6[vote[2]] == 16500000,
          f'6-OPP bw rows {bws6}; the vote names the 16.5 GB/s row')
    for bad in ((1, 7), (6, 0), (6, 7)):
        try:
            S3.perf_vote(1, *bad)
            check(False, f'perf vote {bad} accepted')
        except S3.Stop as stop:
            check(stop.step == 'H0' if hasattr(stop, 'step') else True, f'perf vote {bad} refused before anything is sent')
    tables = S3.build_tables(ARENA, 'gmu')
    blob = {phys: bytes(t) for phys, t in tables.tables.items()}
    read = lambda a: struct.unpack_from('<Q', blob[a & ~0xFFF], a & 0xFFF)[0]
    ttbr = S3.ttbr_for(tables, 1)
    for va, region, priv in ((0x4000, 'gmu_icache', True), (0xFFF000, 'gmu_icache', True),
                             (0x60000000, 'gmu_dummy', True), (0x60003000, 'gmu_dummy', True),
                             (0x60400000, 'gmu_debug', True), (0x60004000, 'gmu_log', False),
                             (0x60008000, 'gmu_hfi', False)):
        pa, leaf = S3.walk(read, ttbr, va)
        start = ARENA + R.ARENA_LAYOUT[region][0]
        check(start <= pa < start + R.ARENA_LAYOUT[region][1] and bool(leaf & R.PTE_AP_UNPRIV) != priv
              and leaf & R.PTE_AF and not leaf & 0x7 << 2, f'GMU VA {va:#x} -> {region} (priv {priv}, NC)')
    for va in (0x0, 0x3000, 0x1000000, 0x6000C000, 0x60407000, 0x80000000):
        check(S3.walk(read, ttbr, va) is None, f'GMU VA {va:#x} unmapped')
    check(len(tables.tables) == 13 and ttbr >> 48 == 1, '13 tables (L1, 2 L2, 10 L3); ASID 1')
    plan = S3.plan_gmu_fw(S3.parse_gmu_fw(GMU))
    check(sum(len(d) for _w, d in plan['itcm']) == 0x210 and sum(len(d) for _w, d in plan['dtcm']) == 0x40
          and plan['memory'][0][:2] == ('icache', 0) and plan['version'] == R.GMU_FW_VERSION,
          'synthetic GMU image routes like gen80100_gmu.bin')
    real = os.environ.get('A16_GPU_FW_DIR')
    if real and (Path(real) / 'gen80100_gmu.bin').is_file():
        fw = S3.load_firmware(real)
        plan = S3.plan_gmu_fw(S3.parse_gmu_fw(fw['gmu']))
        check(sum(len(d) // 4 for _w, d in plan['itcm']) == 132 and sum(len(d) // 4 for _w, d in plan['dtcm']) == 16
              and [(b, o, len(d)) for b, o, d in plan['memory']] == [('icache', 0, 0x1C80C)]
              and plan['version'] == 0x50020026 and plan['prealloc'] == [(0x60000000, 0x1000), (0x60001000, 0x2000),
                                                                         (0x60003000, 0x1000)],
              'real gen80100_gmu.bin: 132 ITCM words, 16 DTCM words, icache 0x1c80c bytes at VA 0x4000, v 0x50020026')
        check(len(fw['sqe']) == 119828, 'real gen80100_sqe.fw pinned')
        print('  (real firmware checked)')


def test_happy_path():
    with synthetic_firmware() as fw:
        fake = FakeStage3()
        status, out = run3(fake, 'arena')
        check(status == 0 and out['arena']['base'] == hex(ARENA) and out['arena']['efi_types'] == ['Conventional'],
              f'arena: the claimed arena sits in Conventional memory ({out.get("stopped")})')
        results = happy_path(fake, fw)
        no_violation(fake, 'happy path')
        out = results['gmu-smmu'][1]
        check(out['stream_table']['5'] == [{'smr': 1, 'type': 0, 'cb': 1}] and all(
            out['stream_table'][str(sid)] == [] for sid in R.MUST_FAULT_SIDS), 'SID 5 -> CB1; 1, 2, 4, 7 unmatched')
        for sid in R.MUST_FAULT_SIDS:
            check(fake.translate(sid, 0x4000, quiet=True) is None, f'the model faults SID {sid} (USFCFG)')
        check(fake.scr0 & R.SCR0_USFCFG, 'USFCFG=1')
        check(fake.cb[1][0] == R.CB_SCTLR & ~R.SCTLR_ASIDPNE and fake.cb[1][4] == R.CB_ACTLR and fake.cba2r[1] == 1
              and fake.cbar[1] == R.CBAR_S1_BYPASS_S2, 'CB1 SCTLR/ACTLR/CBA2R/CBAR as Linux')
        boot = results['gmu-boot'][1]
        want = ['gmu_checks', 'rpmh', 'clocks', 'tz', 'irq_fal', 'fw_load', 'gmu_regs', 'gmu_start']
        check([step for step in boot['order'] if step in want] == want, f'gmu-boot order {boot["order"]}')
        check(boot['rpmh']['cx.lvl']['vote'] == 3 and boot['rpmh']['mx.lvl']['vote'] == 2 and fake.rpmh[0x30000] == 3,
              'RPMh: CX kept at the firmware\'s higher wake vote (3), MX raised to LOW_SVS (2)')
        check(fake.pll_calibrated and fake.pll_even_running() and fake.rcgs[0x9318]['cfg'] & 0x71F == 0x201
              and fake.rcgs[0x93F0]['cfg'] & 0x71F == 0x505, 'PLL0 calibrated and locked, GMU 575 MHz, HUB 200 MHz')
        check(smc_count(fake, S3.SMC_GPU_INIT) == 1 and boot['tz']['sw_fuse_value'] == '0x00000007',
              'TZ init once, fuse read')
        check(smc_count(fake, S3.SMC_CP_APERTURE) == 1 and fake.aperture_calls == 1,
              'the CP aperture SMC once, in hw_init (adreno_hw_init)')
        writes = [a for op, a, _v in fake.log if op == 'w']
        start = writes.index(R.REGS['GMU_CM3_SYSRESET'].addr)
        reset_seq = [a for a in writes if a in (R.REGS['GMU_CM3_SYSRESET'].addr, R.REGS['GMU_GENERAL_9'].addr)]
        check(reset_seq[-3:] == [R.REGS['GMU_CM3_SYSRESET'].addr, R.REGS['GMU_GENERAL_9'].addr,
                                 R.REGS['GMU_CM3_SYSRESET'].addr] and start < writes.index(R.REGS['GMU_CM3_ITCM'].addr),
              'SYSRESET held while the TCMs load; start is SYSRESET 1, GENERAL_9 0, SYSRESET 0')
        version_read = max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'r' and a == 0x03D74FE0)
        last_release = max(i for i, (op, a, v) in enumerate(fake.log) if op == 'w' and a == 0x03D7E000 and v == 0)
        check(version_read < last_release, 'the firmware version word 0x03d74fe0 is read before the CM3 is released')
        hfi = results['hfi'][1]
        check([o for o in hfi['order'] if o.startswith('hfi_')] == ['hfi_15', 'hfi_3', 'hfi_14', 'hfi_10', 'hfi_30'],
              'hfi: messages in a6xx_hfi_start order, then the perf vote')
        check([m for m, e in fake.hfi_seen] == [15, 3, 14, 10, 30] and all(not e for _m, e in fake.hfi_seen),
              f'HFI: TABLE, BW_TABLE, CORE_FW_START, START, perf vote, all acked ({fake.hfi_seen})')
        check(fake.gx_on and fake.oob_held and results['oob'][1]['oob']['gx_on'], 'OOB GPU_SET: GX on, held')
        cp = results['cp-fence'][1]
        check(cp['secvid'] == '0x00000000' and fake.secvid == 0 and fake.me_init, 'SECVID 0 after CP_ME_INIT, read back')
        fence = struct.unpack('<I', fake.dev_read(ARENA + R.ARENA_LAYOUT['cp_memptrs'][0] + R.CP_FENCE_OFF, 4))[0]
        check(cp['fence']['value'] == hex(fence) and fence == 0xF3A00001, f'the GPU wrote the fence {fence:#x}')
        check((R.REGS['RB_GC_GMEM_PROTECT'].addr, 0x15000000, R.PIPE_BR) in fake.pipe_writes[-1:],
              'RB_GC_GMEM_PROTECT written through the BR aperture after SECVID')
        secvid_w = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D3D000)
        me_init_wptr = next(i for i, (op, a, _v) in enumerate(fake.log) if op == 'w' and a == 0x03D0201C)
        check(me_init_wptr < secvid_w, 'SECVID written only after the CP_ME_INIT submission')
        first_gx = next(i for i, (op, a, _v) in enumerate(fake.log) if op in ('r', 'w') and
                        (R.block_of(a) or R.BLOCKS[0]).domain == 'GX')
        oob_ack = max(i for i, (op, a, _v) in enumerate(fake.log) if op == 'r' and a == 0x03D7DFA0 and i < first_gx)
        check(oob_ack < first_gx, 'no GX access before GMU_PWR_CLK_STATUS reported GX on')
        # second fence: only the ring and WPTR
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        gx_writes = writes_to(fake, ('GPU',), mark)
        check(status == 0 and out['order'][-1] == 'fence' and [a for a, _v in gx_writes] == [hex(0x03D0201C)],
              f'second cp-fence: one WPTR write, no hw_init ({gx_writes[:5]})')
        check(struct.unpack('<I', fake.dev_read(ARENA + R.ARENA_LAYOUT['cp_memptrs'][0] + R.CP_FENCE_OFF, 4))[0]
              == 0xF3A00002, 'second fence value')
        # quiesce
        status, out = run3(fake, 'quiesce')
        check(status == 0 and out['quiesce'] == 'graceful' and 'prepare_slumber' in out['gmu_steps'],
              f'quiesce: graceful ({out.get("stopped")}, {out.get("quiesce")})')
        check(not fake.gx_on and fake.gmu[0x03D7E000] == 1 and not fake.smr[0] and not fake.smr[1]
              and fake.s2cr[0] >> 16 == 2 and fake.s2cr[1] >> 16 == 2 and not fake.cb[0][0] and not fake.cb[1][0],
              'quiesce: GX off, CM3 in reset, SMR0/1 invalid, S2CR fault, CB0/1 off')
        check(fake.translate(0, R.CP_RB_VA, quiet=True) is None and fake.translate(5, 0x4000, quiet=True) is None, 'SIDs 0 and 5 fault after quiesce')
        check(fake.gbif_halt == 0 and not fake.pll_even_running() and fake.rcgs[0x9318]['cfg'] & 0x71F == 0,
              'quiesce: GBIF halt cleared (Linux), PLL0 off, GMU RCG back on XO')
        no_violation(fake, 'quiesce')
        status, out = run3(fake, 'status')
        check(status == 0 and out['gmu']['sysreset'] == '0x00000001', 'status afterwards is read-only and clean')
        # and a fresh cycle works after quiesce
        results = happy_path(fake, fw)
        no_violation(fake, 'second full cycle after quiesce')


def test_idempotent():
    with synthetic_firmware() as fw:
        fake = FakeStage3()
        happy_path(fake, fw)
        for command in ('gmu-smmu', 'gmu-boot', 'hfi', 'oob'):
            mark = len(fake.log)
            status, out = run3(fake, command, fw_dir=fw)
            hw = writes_to(fake, ('GMU', 'GBIF', 'RSCC', 'SMMU', 'GPUCC', 'GPU', 'APPS_RSC'), mark)
            check(status == 0 and not hw, f'{command} again: nothing written to hardware ({hw[:4]})')
        no_violation(fake, 'idempotent reruns')


def test_order_enforced():
    with synthetic_firmware() as fw:
        for command, need in (('gmu-boot', 'gmu-smmu'), ('hfi', 'gmu-boot'), ('oob', 'gmu-boot'),
                              ('cp-fence', 'gmu-boot')):
            fake = FakeStage3()
            status, out = run3(fake, command, fw_dir=fw)
            check(status == 2 and not writes_to(fake, ('GMU', 'GPU', 'GPUCC', 'APPS_RSC', 'SMMU')),
                  f'{command} before {need}: stopped, nothing written ({out["stopped"]})')
            no_violation(fake, f'{command} out of order')
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-boot')
        status, out = run3(fake, 'oob')
        check(status == 2 and 'HFI START' in out['stopped']['reason'], 'oob before hfi: refused')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and 'OOB' in out['stopped']['reason'] and not any(
            (R.block_of(a) or R.BLOCKS[0]).domain == 'GX' for op, a, _v in fake.log if op in ('r', 'w')),
              'cp-fence before oob: refused, no GX access at all')
        cold = FakeStage3(gdsc_on=False, enabled=(), aon_running=())
        status, out = run3(cold, 'gmu-smmu')
        check(status == 2 and out['stopped']['step'] == 'S3' and 'cx-on' in out['stopped']['reason'],
              'before cx-on: the Stage 1 gates are missing and nothing is written')
        session = S3.Session3(FakeStage3(), allow_writes=True)
        for call in (lambda: session.rd('CP_RB_RPTR'), lambda: session.wr('CP_SQE_CNTL', 1)):
            try:
                call()
                check(False, 'GX refused')
            except S3.Refused as refused:
                check('GMU reports GX on' in str(refused), 'GX access refused before gate gx')


def test_failures():
    with synthetic_firmware() as fw:
        def upto(fake, command):
            happy_path(fake, fw, upto=command)

        fake = FakeStage3(smc_x0=-1)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B3' and not fake.itcm[0], 'TZ refuses: stop at B3, no fw load')
        fake = FakeStage3(smc_trap=True)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and 'trapped' in out['stopped']['reason'], 'SMC UNDEFINED at EL2: stop at B3')

        fake = FakeStage3(pll_locks=False)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B2' and fake.pll[4] == 0,
              'PLL0 never locks: stop at B2, OPMODE back to STANDBY')
        check(max(p['reads'] for p in out['polls']) <= S3.POLL['pll'][0], 'PLL poll bounded')

        fake = FakeStage3(gmu_boots=False)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B9' and 'cb1' in out['stopped'],
              f'GMU init never completes: stop at B9 with diagnostics ({out["stopped"]["reason"]})')
        check(out['polls'][-1]['reg'] == 'GMU_CM3_FW_INIT_RESULT' and out['polls'][-1]['reads'] <= S3.POLL['gmu_init'][0],
              'INIT_RESULT poll bounded')
        status, out = run3(fake, 'quiesce')
        check(status == 0 and 'forcing off' in out['quiesce'] and fake.gmu[0x03D7E000] == 1 and not fake.smr[1],
              f'quiesce after a failed boot takes the force path ({out.get("quiesce")})')
        no_violation(fake, 'failed boot + quiesce')

        # Cache maintenance matters: with DC CIVAC doing nothing the tool's own read-back still
        # matches (it reads its cache), but the GMU walks and fetches stale DRAM and never boots.
        fake = FakeStage3(drop_clean=True)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 0, 'without a working clean the tool cannot tell from its own reads (model)')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B9'
              and 'icache fetch through SID 5 does not return the firmware' in fake.gmu_fail,
              f'...the GMU fetches stale memory and the boot times out ({fake.gmu_fail[:2]})')

        fake = FakeStage3(hfi_error_on=R.HFI_MSG_BW_TABLE)
        upto(fake, 'gmu-boot')
        status, out = run3(fake, 'hfi')
        check(status == 2 and out['stopped']['step'] == 'H2' and 'error 22' in out['stopped']['reason'],
              'HFI error on BW_TABLE: stop at H2')

        fake = FakeStage3(oob_acks=False)
        upto(fake, 'hfi')
        status, out = run3(fake, 'oob')
        writes = [v for op, a, v in fake.log if op == 'w' and a == 0x03D7E650]
        check(status == 2 and out['stopped']['step'] == 'O1' and writes[-2:] == [1 << 30, 1 << 31],
              'OOB never acked: stop at O1, request cleared')
        fake = FakeStage3(oob_gx_on=False)
        upto(fake, 'hfi')
        status, out = run3(fake, 'oob')
        check(status == 2 and out['stopped']['step'] == 'O2' and 'gx' not in out['proven'],
              'OOB acked but GX reported off: stop at O2, gx never proven')

        fake = FakeStage3(secvid_locked=True)
        upto(fake, 'oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C5', 'SECVID stays secure: stop at C5')
        fake = FakeStage3(cp_executes=False)
        upto(fake, 'oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C4' and out['stopped']['cb0']['fsr'] == '0x00000000',
              'CP never consumes the ring: stop at C4 (idle poll) with CB0 and SQE diagnostics')

        fake = FakeStage3(serror_gmu_write=R.REGS['GMU_CM3_BOOT_CONFIG'].addr)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        last = [e for e in fake.log if e[0] in ('r', 'w')][-1]
        check(status == 3 and out['stopped']['step'] == 'B8' and last[1] == R.REGS['GMU_CM3_BOOT_CONFIG'].addr,
              'SError across a GMU write: exit 3 at that access, nothing after')

        fake = FakeStage3(rsc_busy=True)
        upto(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B1' and 'busy' in out['stopped']['reason'],
              'active TCS busy: stop at B1 without writing a vote')

        fake = FakeStage3(smr1_foreign=True)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 2 and out['stopped']['step'] == 'G3' and fake.smr[1] == R.SMR_VALID | 0x0C00
              and not writes_to(fake, ('SMMU',)), 'SMR1 owned by someone else: stop, SMMU untouched')

        fake = FakeStage3(arena=0)
        fake.claimable = False
        status, out = run3(fake, 'gmu-smmu')
        check(status == 2 and 'arena --claim' in out['stopped']['reason'], 'no arena: stop with the claim hint')
        status, out = run3(fake, 'arena', claim=True)
        check(status == 2 and 'claim failed' in out['stopped']['reason'], 'claim refused by the payload: stop')
        fake = FakeStage3(gpu_call=False)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 4, 'a stage without gpu_call: exit 4 (chainload a Stage 3 stage)')
        fake = FakeStage3(arena=0x81C00000)          # the payload publishes an arena over cmd-db (Reserved)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 2 and 'not wholly RAM' in out['stopped']['reason'] and not writes_to(fake, ('SMMU',)),
              'an arena the EFI map does not give up as RAM is refused before any write')


def test_stop_after_and_undo():
    with synthetic_firmware() as fw:
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-smmu')
        for step in ('B1', 'B2', 'B3'):
            status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after=step)
            check(status == 0 and out['stopped_after'] == step and fake.gmu[0x03D7E000] == 1,
                  f'gmu-boot --stop-after {step}: CM3 still in reset')
        check(fake.gpu_rec['smc_calls'] == 1, 'the TZ SMC ran once over repeated partial boots')
        fake.gpu_rec.update(smc_ok=0, smc_calls=0)        # a chainload: the new stage's record starts at zero
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B3')
        check(status == 0 and fake.gpu_rec['smc_calls'] == 0 and 'skipped' in out['tz'],
              'after a chainload the arena record still says TZ init is done: no second SMC')
        status, out = run3(fake, 'gmu-smmu', undo=True)
        check(status == 0 and not fake.smr[1] and not fake.cb[1][0], 'gmu-smmu --undo with the CM3 in reset')
        happy_path(fake, fw, upto='gmu-boot')
        status, out = run3(fake, 'gmu-smmu', undo=True)
        check(status == 2 and fake.smr[1], 'gmu-smmu --undo refused while the GMU runs')
        status, out = run3(fake, 'quiesce', leave_halted=True)
        check(status == 0 and fake.gbif_halt == 2, 'quiesce --leave-halted leaves GBIF halted (exit/chainload)')
        no_violation(fake, 'stop-after and undo')


def host_record(fake):
    base = fake.gpu_rec['arena_base'] + R.ARENA_LAYOUT['state'][0]
    fake.civac(base, 4096)
    return dict(zip(S3.HOST_FIELDS, struct.unpack(f'<{len(S3.HOST_FIELDS)}Q',
                                                    fake.dev_read(base, 8 * len(S3.HOST_FIELDS)))))


def host_record_set(fake, **fields):
    base = fake.gpu_rec['arena_base'] + R.ARENA_LAYOUT['state'][0]
    record = dict(host_record(fake), **fields)
    fake.cpu_write(base, S3.host_state_pack(record))
    fake.civac(base, 4096)


def test_review_fixes():
    """REVIEW (review-1): the hazards found against stage3.patch, each now refused or ordered."""
    with synthetic_firmware() as fw:
        # 1. The runbook's cp-fence sequence (--stop-after C3S, --stop-after C5, full) never
        #    re-initialises a started SQE and never rewrites the SQE/RB buffers under it.
        fake = FakeStage3()
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3S')
        check(status == 0 and fake.cp_running and out['order'][-1] == 'sqe_started',
              'cp-fence --stop-after C3S: SQE started')
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw, stop_after='C5')
        again = [a for a, _v in writes_to(fake, ('GPU',), mark)]
        check(status == 0 and 'hw_init_done' in out['order'] and 'hw_init' not in out['order']
              and hex(R.REGS['CP_SQE_CNTL'].addr) not in again and hex(R.REGS['CP_RB_BASE'].addr) not in again,
              f'cp-fence --stop-after C5 after C3S: resumes at C4, no second hw_init ({out["stopped"]})')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 0 and out['order'][-1] == 'fence', 'then the fence')
        no_violation(fake, 'runbook cp-fence sequence')
        # 2. C4 timed out: the rerun waits again and does not queue a second CP_ME_INIT.
        fake = FakeStage3(cp_executes=False)
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        wptr = host_record(fake)['rb_wptr']
        check(status == 2 and out['stopped']['step'] == 'C4' and wptr == 10, f'C4 timeout recorded (wptr {wptr})')
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        wptr_writes = [v for op, a, v in fake.log[mark:] if op == 'w' and a == R.REGS['CP_RB_WPTR'].addr]
        check(status == 2 and out['stopped']['step'] == 'C4' and not wptr_writes and host_record(fake)['rb_wptr'] == 10,
              'C4 rerun: no second CP_ME_INIT, no ring rewrite')
        no_violation(fake, 'C4 rerun')
        # 3. A running SQE this tool did not start this boot: refused before any GX write.
        fake = FakeStage3()
        happy_path(fake, fw, upto='oob')
        run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3S')
        host_record_set(fake, hwinit_token=0)
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C0' and 'quiesce' in out['stopped']['reason']
              and not writes_to(fake, ('GPU', 'SMMU'), mark), 'SQE running without this boot\'s hw_init: refused')
        no_violation(fake, 'foreign running SQE')
        # 4. B1 sends nothing when drv-2's current vote is invisible, unless the operator says so.
        fake = FakeStage3(wake_mx=None)
        happy_path(fake, fw, upto='gmu-smmu')
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B1')
        check(status == 2 and out['stopped']['step'] == 'B1' and 'mx.lvl' in out['stopped']['reason']
              and not writes_to(fake, ('APPS_RSC',), mark), 'B1 with no visible MX vote: refused, nothing sent')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B1', rail_vote_unseen=True)
        check(status == 0 and fake.rpmh[0x30010] == 2, 'B1 --rail-vote-unseen: MX LOW_SVS sent')
        hidden = FakeStage3(wake_mx=None, hidden_votes={0x30010: 4})
        happy_path(hidden, fw, upto='gmu-smmu')
        run3(hidden, 'gmu-boot', fw_dir=fw, stop_after='B1', rail_vote_unseen=True)
        check(any(v[0] == 'vote' for v in hidden.violations),
              'what the refusal prevents: an unseen higher drv-2 vote is lowered once the flag overrides it')
        hidden = FakeStage3(wake_mx=None, hidden_votes={0x30010: 4})
        happy_path(hidden, fw, upto='gmu-smmu')
        status, out = run3(hidden, 'gmu-boot', fw_dir=fw, stop_after='B1', rail_vote_unseen=True, rail_level='nom')
        check(status == 0 and hidden.rpmh[0x30010] == 5 and hidden.rpmh[0x30000] == 5
              and not any(v[0] == 'vote' for v in hidden.violations),
              '--rail-level nom: MX/CX NOM sent, the unseen SVS_L1 vote is not lowered')
        mark = len(hidden.log)
        status, out = run3(hidden, 'gmu-boot', fw_dir=fw, stop_after='B1', rail_vote_unseen=True)
        check(status == 0 and hidden.rpmh[0x30010] == 5 and not out['rpmh']['sent']
              and not writes_to(hidden, ('APPS_RSC',), mark),
              'rerun at the default level keeps this tool\'s own NOM vote (nothing sent)')
        # 4a. Both ack forms are accepted; a reply for another command is skipped and logged.
        legacy = FakeStage3(legacy_ack=True)
        happy_path(legacy, fw, upto='hfi')
        no_violation(legacy, 'legacy id-126 acks')
        fake = FakeStage3()
        results = happy_path(fake, fw, upto='hfi')
        acks = results['hfi'][1]['hfi']
        check(all(v['ack'][0].endswith('0103' + format(i, '02x')) for i, v in
                  zip((R.HFI_MSG_TABLE, R.HFI_MSG_BW_TABLE, R.HFI_MSG_CORE_FW_START, R.HFI_MSG_START,
                       R.HFI_MSG_GX_BW_PERF_VOTE), acks.values())),
              f'fw 0x50020026 acks (own id, type 1) accepted for all five messages: {[v["ack"][0] for v in acks.values()]}')
        # 4c. A GBIF halt left acknowledged from before this boot (run 2: GBIF_HALT_ACK 0x3 with
        #     GBIF_HALT 0): gmu-boot clears it with KGSL's BIT(3) while the CM3 is in reset.
        fake = FakeStage3()
        results = happy_path(fake, fw, upto='gmu-boot')
        clear = results['gmu-boot'][1].get('gbif_clear')
        check(clear == {'ack_before': '0x00000003', 'halt_after': '0x00000008', 'ack_after': '0x00000003'},
              f'gmu-boot writes KGSL\'s BIT(3); the A16\'s idle ack stays 0x3: {clear}')
        for command in ('hfi', 'oob'):
            check(run3(fake, command)[0] == 0, command)
        status, out = run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3P')
        notes = out.get('prestart', {}).get('notes', [])
        check(status == 0 and any('GBIF_HALT_ACK 0x00000003' in n for n in notes),
              f'C3P starts with the idle ack recorded as a note, not a halt: {notes}')
        halted = FakeStage3()
        happy_path(halted, fw, upto='oob')
        run3(halted, 'cp-fence', fw_dir=fw, stop_after='C3')
        halted.gbif_halt_stuck = 1                  # a halt request that stays asserted
        status, out = run3(halted, 'cp-fence', fw_dir=fw, stop_after='C3P')
        check(status == 2 and 'halt is requested' in out['stopped']['reason'], 'C3P refuses a requested GBIF halt')
        no_violation(fake, 'GBIF idle ack')
        # 4b. Context-bank read-back: the A16's fixed SCTLR/TCR2 bits pass; any other difference stops
        #     gmu-smmu before SID 5 is routed to the bank.
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B1')
        check(status == 0 and out['order'][-1] == 'rpmh', 'fixed SCTLR/TCR2 bits: gmu-boot passes B0')
        exact = FakeStage3(cb_fixed_bits=False)
        happy_path(exact, fw, upto='gmu-smmu')
        check(run3(exact, 'gmu-boot', fw_dir=fw, stop_after='B1')[0] == 0, 'exact read-back also passes B0')
        stuck = FakeStage3(cb_stuck=True)
        status, out = run3(stuck, 'gmu-smmu')
        check(status == 2 and out['stopped']['step'] == 'G3' and 'did not read back' in out['stopped']['reason']
              and not stuck.smr[1] & R.SMR_VALID, 'a register that does not take the write: G3 stops, SID 5 not routed')
        no_violation(stuck, 'stuck MAIR0')
        # 5. arena --release reads the hardware, not the host record.
        fake = FakeStage3()
        happy_path(fake, fw)
        host_record_set(fake, boot_token=0, gmu_ttbr=0, gpu_ttbr=0)
        status, out = run3(fake, 'arena', release=True)
        check(status == 2 and out['stopped']['step'] == 'A1' and fake.gpu_rec['arena_base'],
              f'release with the GMU live and SID 0/5 routed: refused ({out["stopped"]["reason"][:60]})')
        check(run3(fake, 'quiesce')[0] == 0, 'quiesce')
        status, out = run3(fake, 'arena', release=True)
        check(status == 0 and out.get('released') and not fake.gpu_rec['arena_base'], 'release after quiesce')
        no_violation(fake, 'release')
        # 6. (defence in depth) SMR1 still routes SID 5 to a CB1 that is off -- a bypass hole the
        #    tool never creates but must not keep open while it reprograms: the stream is faulted
        #    first, then the bank is written and enabled, then SID 5 is routed again.
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-smmu')
        fake.cb[1][0] = 0
        status, out = run3(fake, 'gmu-smmu')
        check(status == 0 and out['gmu_cb']['programmed'] is True and fake.smr[1] == R.SMR_VALID | 5
              and fake.cb[1][0] == R.CB_SCTLR & ~R.SCTLR_ASIDPNE, 'gmu-smmu closes a routed-to-disabled-bank hole before reprogramming')
        no_violation(fake, 'routed stream to a disabled CB1')
        # 7. A CM3 out of reset that never booted is live: gmu-smmu only verifies, --undo refuses.
        fake = FakeStage3(gmu_boots=False)
        happy_path(fake, fw, upto='gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'B9' and not fake.gmu[0x03D7E000] & 1, 'B9 timeout, CM3 live')
        fake.cb[1][0x20] ^= 0x1000
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 2 and not writes_to(fake, ('SMMU',), mark), 'gmu-smmu under a live, unbooted CM3: no SMMU write')
        status, out = run3(fake, 'gmu-smmu', undo=True)
        check(status == 2 and fake.smr[1] & R.SMR_VALID, 'gmu-smmu --undo under a live CM3: refused')
        # 8. A response queue whose write index the reader's alignment steps over: bounded.
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-boot')
        real = fake._respond

        def garbage(header, error):
            real(header, error)
            rsp = R.GMU_HFI_VA + 2 * 4096
            for n in range(R.HFI_QUEUE_DWORDS):
                fake.dma_write(R.GMU_SID, rsp + 4 * n, struct.pack('<I', 0x00000101), priv=False)
            fake.dma_write(R.GMU_SID, R.GMU_HFI_VA + 24 + 48 + 44, struct.pack('<I', 3), priv=False)
        fake._respond = garbage
        status, out = run3(fake, 'hfi')
        check(status == 2 and 'did not drain' in out['stopped']['reason'], 'HFI response reader is bounded')
        # 9. PREPARE_SLUMBER goes out with the power counter off (a6xx_gmu_notify_slumber).
        fake = FakeStage3()
        happy_path(fake, fw)
        status, out = run3(fake, 'quiesce')
        check(status == 0 and out['quiesce'] == 'graceful', 'graceful quiesce')
        no_violation(fake, 'slumber with the power counter off')
        # 10. A claimed arena that is not wholly Conventional, or overlaps q1n1, is refused.
        fake = FakeStage3()
        real_bootinfo = fake.bootinfo
        fake.bootinfo = lambda: dict(real_bootinfo(), heap_base=ARENA + 0x100000, heap_size=0x100000)
        status, out = run3(fake, 'status')
        check('heap' in out['arena'].get('missing', ''), 'an arena overlapping the q1n1 heap is refused')


def arena_word(fake, offset):
    return fake.gpu_rec['arena_base'] + offset


def test_fix_round():
    """FIX (after review-1): the HFI index line, ring pointers, quiesce order, unmatched streams, a
    TrustZone call left open, and a stage without GPU_STORE32."""
    import a16gpu_stage3 as tool
    with synthetic_firmware() as fw:
        # 1. The HFI index words share one 64-byte line with the words the GMU writes. The tool
        #    stores them with GPU_STORE32 (clean+invalidate, store, clean+invalidate on the target),
        #    and only once the command queue has drained, never as a writemem and a later clean.
        cmd_write = arena_word(FakeStage3(), S3.hfi_header_offset(0, 'write_index'))
        rsp_read = arena_word(FakeStage3(), S3.hfi_header_offset(1, 'read_index'))
        check(cmd_write // LINE == rsp_read // LINE == arena_word(FakeStage3(), S3.hfi_header_offset(0, 'read_index')) // LINE
              == arena_word(FakeStage3(), S3.hfi_header_offset(1, 'write_index')) // LINE,
              'the four HFI index words of queues 0 and 1 share one cache line (the hazard is real)')
        for delay in (1, 4, 7, 8, 12):
            fake = FakeStage3(gmu_index_delay=delay)
            happy_path(fake, fw, upto='gmu-boot')     # the queue table is written with the CM3 in reset
            mark = len(fake.log)
            for command in ('hfi', 'oob', 'cp-fence'):
                check(run3(fake, command, fw_dir=fw)[0] == 0, f'{command} with GMU index writes {delay} late')
            stored = {a for op, a, *_v in fake.log[mark:] if op == 'store32'}
            plain = [a for op, a, n in (e for e in fake.log[mark:] if e[0] == 'mw')
                     if a <= rsp_read < a + n or a <= cmd_write < a + n]
            check(fake.gmu_running and cmd_write in stored and rsp_read in stored and not plain,
                  f'GMU index writes {delay} requests late: indices stored with GPU_STORE32 only')
            no_violation(fake, f'HFI index line, GMU {delay} requests late')
        # What the fix prevents: the Stage 3 build's writemem + separate clean with no drain.
        saved = (tool.Session3.mem_store32, tool.cmd_queue_drained)
        tool.Session3.mem_store32 = lambda self, offset, value, what: self.mem_write(offset, struct.pack('<I', value), what)
        tool.cmd_queue_drained = lambda s: struct.unpack('<12I', s.mem_read(tool.hfi_header_offset(0, 'status'), 48, 'hdr'))
        try:
            fake = FakeStage3(gmu_index_delay=7)
            happy_path(fake, fw, upto='hfi')
        finally:
            tool.Session3.mem_store32, tool.cmd_queue_drained = saved
        check(any(v[0] == 'clobber' for v in fake.violations),
              'the old writemem+clean path loses a GMU index write in the model (the fix is load-bearing)')
        # 2. A GMU that never moves its command read index: bounded, stops before the next command.
        fake = FakeStage3(gmu_index_delay=10**9)
        happy_path(fake, fw, upto='gmu-boot')
        status, out = run3(fake, 'hfi')
        drains = [p for p in out['polls'] if p['reg'].startswith('hfi cmd queue drained')] if 'polls' in out else []
        check(status == 2 and 'not drained' in out['stopped']['reason'], 'an undrained command queue stops hfi')
        check(not drains or all(p['reads'] <= S3.POLL['hfi_drain'][0] for p in drains), 'the drain poll is bounded')
        # 3. A stage whose gpu_call predates GPU_STORE32 is refused before anything is written.
        fake = FakeStage3(gpu_version=1)
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-smmu')
        check(status == 2 and out['stopped']['step'] == 'A0' and 'GPU_STORE32' in out['stopped']['reason']
              and not writes_to(fake, ('SMMU', 'GMU'), mark) and not [e for e in fake.log[mark:] if e[0] == 'mw'],
              'gpu_state version 1: refused at A0, nothing written')
        # 4. The ring: the record's WPTR must equal the CP's WPTR and RPTR before anything is queued.
        fake = FakeStage3()
        happy_path(fake, fw, upto='oob')
        check(run3(fake, 'cp-fence', fw_dir=fw, stop_after='C5')[0] == 0, 'cp-fence --stop-after C5')
        host_record_set(fake, rb_wptr=4)
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        wptr_writes = [v for op, a, v in fake.log[mark:] if op == 'w' and a == R.REGS['CP_RB_WPTR'].addr]
        check(status == 2 and 'disagree' in out['stopped']['reason'] and not wptr_writes,
              'a record WPTR behind the CP: the fence is not queued (the CP would run stale ring words)')
        no_violation(fake, 'ring pointer disagreement')
        fake = FakeStage3(cp_executes=False)
        happy_path(fake, fw, upto='oob')
        check(run3(fake, 'cp-fence', fw_dir=fw)[1]['stopped']['step'] == 'C4', 'C4 timeout (CP_ME_INIT queued)')
        fake.gx[R.REGS['CP_RB_WPTR'].addr] = 0
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C4' and 'CP_RB_WPTR' in out['stopped']['reason'],
              'C4 resume with a CP WPTR that is not the record\'s: stop, quiesce and start over')
        # 5. Quiesce zeroes the HFI indices only after CM3_SYSRESET reads back 1, and carries on
        #    to the SMMU step if it does not.
        fake = FakeStage3()
        happy_path(fake, fw)
        mark = len(fake.log)
        status, out = run3(fake, 'quiesce')
        reset_at = next(n for n, e in enumerate(fake.log[mark:]) if e[0] == 'w'
                        and e[1] == R.REGS['GMU_CM3_SYSRESET'].addr and e[2] == 1)
        header_at = [n for n, e in enumerate(fake.log[mark:]) if e[0] == 'mw' and e[1] <= cmd_write < e[1] + e[2]]
        check(status == 0 and header_at and min(header_at) > reset_at and 'zeroed' in out['hfi_stop'],
              'quiesce: HFI indices zeroed after the CM3 is held in reset')
        no_violation(fake, 'quiesce order')
        fake = FakeStage3()
        happy_path(fake, fw)
        real_write = fake._gmu_write

        def stuck_reset(addr, value):
            if addr == R.REGS['GMU_CM3_SYSRESET'].addr:
                return None
            return real_write(addr, value)
        fake._gmu_write = stuck_reset
        status, out = run3(fake, 'quiesce')
        check(status == 2 and out['stopped']['step'] == 'Q2' and out['hfi_stop'].startswith('skipped')
              and 'smmu_off' in out['order'] and not fake.smr[0] & R.SMR_VALID and not fake.smr[1] & R.SMR_VALID,
              'a CM3 reset that does not latch: headers left alone, SMMU still off, and quiesce reports failure')
        fake._gmu_write = real_write
        status, out = run3(fake, 'arena', release=True)
        check(status == 2 and out['stopped']['step'] == 'A1', 'and the arena is not released while the CM3 runs')
        # 6. After quiesce SID 0 and 5 are unmatched: USFCFG must make them fault, not bypass.
        fake = FakeStage3()
        happy_path(fake, fw)
        fake.scr0 &= ~R.SCR0_USFCFG
        status, out = run3(fake, 'quiesce')
        check(status == 0 and fake.scr0 & R.SCR0_USFCFG, 'quiesce sets sCR0.USFCFG again before it declares SID 0/5 off')
        fake.scr0 &= ~R.SCR0_USFCFG
        status, out = run3(fake, 'arena', release=True)
        check(status == 2 and 'unmatched' in str(out['stopped'].get('release_check', out['stopped']))
              and fake.gpu_rec['arena_base'], 'arena --release with USFCFG clear: SID 0/5 would bypass, refused')
        fake.scr0 |= R.SCR0_USFCFG
        check(run3(fake, 'arena', release=True)[0] == 0 and not fake.gpu_rec['arena_base'], 'release once they fault')
        # 7. A TrustZone call left open (EINTR after the resumes, or WAITQ) is never re-issued.
        for fail, name in ((-10, 'EINTR'), (-8, 'WAITQ')):
            fake = FakeStage3(smc_fail=fail)
            happy_path(fake, fw, upto='gmu-smmu')
            status, out = run3(fake, 'gmu-boot', fw_dir=fw)
            check(status == 2 and out['stopped']['step'] == 'B3' and host_record(fake)['tz_ok'] == S3.TZ_PENDING,
                  f'{name}: B3 stops and the arena record says the call is open')
            fake.s3['smc_fail'] = None
            calls = fake.gpu_rec['smc_calls']
            status, out = run3(fake, 'gmu-boot', fw_dir=fw)
            check(status == 2 and out['stopped']['step'] == 'B3' and 'reboot' in out['stopped']['reason']
                  and fake.gpu_rec['smc_calls'] == calls, f'{name}: a rerun issues no second SMC')
            fake.gpu_rec.update(smc_status=0)          # a chainload: the stage record forgets; the arena does not
            status, out = run3(fake, 'gmu-boot', fw_dir=fw)
            check(status == 2 and fake.gpu_rec['smc_calls'] == calls, f'{name}: nor after a chainload')
            status, out = run3(fake, 'gmu-boot', fw_dir=fw, force_smc=True)
            check(status == 0 and fake.gpu_rec['smc_calls'] == calls + 1 and host_record(fake)['tz_ok'] == 1,
                  f'{name}: --force-smc is the operator\'s override')
            no_violation(fake, f'SMC left open ({name})')


def test_review2():
    """REVIEW-2: the GPU RSC is woken after a quiesce put it to sleep (a6xx_rpmh_start), and a
    sleep-TCS vote is not taken as evidence of drv-2's live rail level."""
    with synthetic_firmware() as fw:
        # 1. quiesce's a6xx_rpmh_stop is paired with a6xx_rpmh_start in the next gmu-boot, in
        #    Linux's place: after the FAL veto, before the firmware load.
        fake = FakeStage3()
        results = happy_path(fake, fw)
        check('skipped' in results['gmu-boot'][1]['rscc_start'],
              'first gmu-boot: no rpmh_stop recorded, so no RSCC wake (as Linux\'s first resume)')
        status, out = run3(fake, 'quiesce', fw_dir=fw)
        check(status == 0 and fake.pdc_asleep and host_record(fake)['pdc_sleep'] == 1,
              f'quiesce: GPU RSC asleep and recorded in the host record ({out.get("stopped")})')
        run3(fake, 'gmu-smmu')
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 0 and not fake.pdc_asleep and out['rscc_start'].get('woken'),
              f'second gmu-boot wakes the GPU RSC and boots ({out.get("stopped")})')
        writes = [a for op, a, _v in fake.log[mark:] if op == 'w']
        req, fal = R.REGS['GMU_RSCC_CONTROL_REQ'].addr, R.REGS['GMU_CX_FAL_INTF'].addr
        itcm = R.REGS['GMU_CM3_ITCM'].addr
        first_itcm = next(n for n, a in enumerate(writes) if itcm <= a < itcm + 0x4000)
        check(writes.index(fal) < writes.index(req) < first_itcm,
              'a6xx_gmu_fw_start order: FAL veto, then the RSCC wake, then the TCM load')
        check(host_record(fake)['pdc_sleep'] == 0, 'the wake clears the host record flag')
        results = {c: run3(fake, c, fw_dir=fw) for c in ('hfi', 'oob', 'cp-fence')}
        check(all(r[0] == 0 for r in results.values()), 'the second cycle reaches the fence')
        no_violation(fake, 'quiesce, then a second full cycle with the RSC woken')

        # 2. The hardware's own sleep status wakes it too, when the record lost the flag.
        fake = FakeStage3()
        happy_path(fake, fw, upto='gmu-boot')
        run3(fake, 'quiesce', fw_dir=fw)
        host_record_set(fake, pdc_sleep=0)
        run3(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        check(status == 0 and out['rscc_start'].get('woken') and not fake.pdc_asleep,
              f'RSC_STATUS0 bit 16 alone also triggers the wake ({out.get("stopped")})')

        # 3. An RSC that never acknowledges the wake stops B6 before anything is loaded.
        fake = FakeStage3(rscc_wakes=False)
        happy_path(fake, fw, upto='gmu-boot')
        run3(fake, 'quiesce', fw_dir=fw)
        run3(fake, 'gmu-smmu')
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-boot', fw_dir=fw)
        itcm_writes = [a for op, a, _v in fake.log[mark:] if op == 'w' and itcm <= a < itcm + 0x4000]
        check(status != 0 and out['stopped']['step'] == 'B6' and not itcm_writes
              and fake.gmu[R.REGS['GMU_CM3_SYSRESET'].addr] & 1,
              f'no RSCC wake ack: stop at B6, no TCM write, CM3 still in reset ({out.get("stopped")})')
        no_violation(fake, 'RSCC wake timeout')

        # 4. B1: only a wake (or still-enabled active) TCS shows drv-2's live level; a sleep-TCS
        #    vote is what drv-2 wants while the apps subsystem sleeps. Taking it as the live
        #    level would send LOW_SVS over a higher live vote.
        fake = FakeStage3(wake_cx=None, wake_mx=None, sleep_cx=1, sleep_mx=1,
                          hidden_votes={0x30000: 4, 0x30010: 4})
        run3(fake, 'gmu-smmu')
        mark = len(fake.log)
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B1')
        sent = writes_to(fake, ('APPS_RSC',), mark)
        check(status != 0 and out['stopped']['step'] == 'B1' and not sent and fake.rpmh[0x30000] == 4
              and fake.rpmh[0x30010] == 4,
              f'sleep-TCS votes only: B1 sends nothing, live votes untouched ({out.get("stopped")}, {sent[:3]})')
        check(out['stopped']['rpmh']['mx.lvl']['sleep_only'] and out['stopped']['rpmh']['cx.lvl']['sleep_only'],
              'B1 reports the sleep-only votes')
        no_violation(fake, 'sleep-only votes refused')
        fake = FakeStage3(sleep_cx=1, sleep_mx=1)
        run3(fake, 'gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, stop_after='B1')
        check(status == 0 and out['rpmh']['cx.lvl']['existing'] == 3 and out['rpmh']['mx.lvl']['existing'] == 2,
              f'wake votes plus sleep votes: B1 proceeds from the wake level ({out.get("stopped")})')
        no_violation(fake, 'wake and sleep votes')

        # 5. quiesce after hfi but before oob: Linux's a6xx_gmu_shutdown does a dummy OOB GPU_SET
        #    handshake before PREPARE_SLUMBER when none happened since the GMU woke.
        fake = FakeStage3()
        happy_path(fake, fw, upto='hfi')
        mark = len(fake.log)
        status, out = run3(fake, 'quiesce', fw_dir=fw)
        door = [v for op, a, v in fake.log[mark:] if op == 'w' and a == R.REGS['GMU_HOST2GMU_INTR_SET'].addr]
        check(status == 0 and out['quiesce'] == 'graceful' and out['gmu_steps'][:2] == ['oob_handshake', 'gbif_halted']
              and 'prepare_slumber' in out['gmu_steps'] and not fake.oob_held,
              f'quiesce after hfi: dummy OOB handshake, then the graceful slumber ({out.get("stopped")}, {out.get("gmu_steps")})')
        check(door[:2] == [1 << R.OOB_GPU_SET_REQUEST, 1 << R.OOB_GPU_SET_CLEAR] and door[2] & 1,
              'order: GPU_SET, clear, then the PREPARE_SLUMBER doorbell')
        no_violation(fake, 'quiesce after hfi without oob')
        fake = FakeStage3(oob_acks=False)
        happy_path(fake, fw, upto='hfi')
        status, out = run3(fake, 'quiesce', fw_dir=fw)
        check(status == 0 and 'forcing off' in out['quiesce'] and not fake.slumbered and fake.gmu[0x03D7E000] == 1,
              f'no ack to the dummy handshake: no PREPARE_SLUMBER, force path (as Linux) ({out.get("quiesce")})')
        no_violation(fake, 'dummy handshake timeout')


def test_cli():
    fakes = []
    real = Q.connect

    def connect(device=None, wait=0.0, **_):
        fakes.append(FakeStage3())
        return fakes[-1]
    Q.connect = connect
    try:
        with synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                a16gpu.main(['gmu-boot'])
                check(False, 'gmu-boot without --write refused')
            except SystemExit as refused:
                check(refused.code == 2 and not fakes, 'gmu-boot without --write: refused before connecting')
            path = Path(tmp) / 'smmu.json'
            status = a16gpu.main(['--out', str(path), 'gmu-smmu', '--write'])
            saved = json.loads(path.read_text())
            check(status == 0 and saved['plan_stage'] == 3 and saved['order'][-1] == 'gmu_smmu_verified',
                  'CLI gmu-smmu --write: JSON written')
            status = a16gpu.main(['--out', str(path), 'status'])
            check(status == 0, 'CLI status (read-only, no --write)')
            status = a16gpu.main(['--out', str(path), 'reset-reason'])
            check(status == 0 and json.loads(path.read_text())['order'] == ['reset_reason'],
                  'CLI reset-reason (read-only, no --write)')
            status = a16gpu.main(['--out', str(path), 'cp-fence', '--write', '--fw-dir', fw, '--sqe-probe', 'fault',
                                  '--stop-after', 'C3P', '--no-aperture', '--no-ahb-timeout', '--prestart-faults-ok',
                                  '--sqe-samples', '2'])
            check(status == 2 and json.loads(path.read_text())['stopped']['step'] == 'C0',
                  'CLI cp-fence takes the sqe-reset options (a fresh model stops at C0: no GMU)')
            _ = fw
    finally:
        Q.connect = real



def log_index(fake, op, addr, value=None, last=False):
    hits = [i for i, (o, a, v) in enumerate(fake.log) if o == op and a == addr and (value is None or v == value)]
    return (hits[-1] if last else hits[0]) if hits else None


def test_sqe_reset():
    """sqe-reset (DIAGNOSIS.md): run 1 lost the A16 at CP_SQE_CNTL = 1. Every difference from Linux/KGSL
    before that write is closed or instrumented, and the start is preceded by a read-only snapshot."""
    # --- registry and encoders -------------------------------------------------------------------
    check(R.ahb_timeout_cntl(200000000) == 0x1B12 and R.ahb_timeout_cntl(150000000) == 0x1B11,
          'AHB timeout word as adreno_get_ahb_timeout_val: 200 MHz -> 18, 150 MHz -> 17 (| 0x1b00)')
    check(R.ahb_timeout_cntl(200000000, noc_timeout_us=10 ** 9) is None,
          'a timeout that does not fit the 5-bit field is refused, as KGSL')
    check(S3.SMC_CP_APERTURE == (0x42000C1B, 4, 0xFFFF0000, 0xFFFFFFFF), 'aperture SMC as qcom_scm_set_gpu_smmu_aperture(0)')
    check(1 not in R.MUST_FAULT_SIDS and R.GPU_SIDS == (0, 1), 'SID 1 is the GPU\'s (glymur.dtsi iommus 0 and 1)')
    for name in ('IMEM_DLOAD_MAGIC', 'IMEM_RESTART_REASON', 'IMEM_BOOT_STATS', 'IMEM_DLOAD_TYPE', 'IMEM_PIL_RELOC'):
        reg = R.REGS[name]
        check(reg.access == 'r' and reg.domain == 'AO' and R.block_of(reg.addr).name == 'IMEM', f'{name}: AO, read-only')
    for name in ('CP_AQE_INSTR_BASE_0', 'CP_INTERRUPT_STATUS_GLOBAL', 'CP_CP2GMU_STATUS', 'RBBM_SW_FUSE_INT_STATUS'):
        check(R.REGS[name].access == 'r' and R.REGS[name].gate == 'gx', f'{name}: read-only, behind gx')
    check(R.REGS['CX_MISC_CX_AHB_CNTL'].addr == 0x03D00000 + 4 * 0x27810 and R.REGS['CX_MISC_CX_AHB_CNTL'].count == 5,
          'CX AHB CNTL at gen8_reg.h 0x27810..0x27814')

    with synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        # --- 1. the full path: order, Linux parity, snapshot before the start ---------------------
        fake = FakeStage3()
        results = happy_path(fake, fw)
        no_violation(fake, 'sqe-reset happy path')
        cp = results['cp-fence'][1]
        check(cp['stream_table']['1'] == [{'smr': 2, 'type': 0, 'cb': 0}] and cp['stream_table']['0'] ==
              [{'smr': 0, 'type': 0, 'cb': 0}], 'C1: SID 0 and SID 1 both translate through CB0 (Linux: one domain)')
        order = cp['order']
        check(order.index('hw_init') < order.index('sqe_prestart') < order.index('sqe_started') < order.index('cp_init'),
              f'cp-fence order hw_init, sqe_prestart, sqe_started, cp_init ({order})')
        sqe_cntl = log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr, 1)
        int_mask = log_index(fake, 'w', R.REGS['RBBM_INT_0_MASK'].addr)
        sqe_base = log_index(fake, 'w', R.REGS['CP_SQE_INSTR_BASE'].addr)
        aperture = next(i for i, e in enumerate(fake.log) if e[0] == 'call' and e[1] == S3.GPU_SMC
                        and tuple(e[2][:4]) == S3.SMC_CP_APERTURE)
        check(int_mask < aperture < sqe_base < sqe_cntl,
              'the aperture SMC sits where adreno_hw_init makes it: after the interrupt masks, before the SQE base')
        snap = cp['prestart']
        last_snapshot_read = log_index(fake, 'r', R.REGS['CP_CP2GMU_STATUS'].addr)
        check(last_snapshot_read < sqe_cntl and snap['cx']['SMMU_sGFSR'] == '0x00000000'
              and snap['gx']['RBBM_SECVID_TRUST_CNTL'] == '0x00000001' and snap['secure_mode_at_start'],
              'the snapshot is read before CP_SQE_CNTL (and shows the GPU in secure mode, as at reset)')
        check(snap['cb0_walk']['sqe@0x10000000'] == hex(ARENA + R.ARENA_LAYOUT['cp_sqe'][0])
              and snap['sqe_image']['matches_file_minus_dword0'], 'pre-start walk and SQE image checked from DRAM')
        check(set(snap['cx_ahb'].values()) == {'0x00001b12'} and fake.cx_ahb == [0x1B12] * 5,
              'CX AHB timeouts armed (HUB 200 MHz -> 0x1b12) before the SQE start')
        ahb_first = log_index(fake, 'w', R.REGS['CX_MISC_CX_AHB_CNTL'].addr)
        itcm_first = log_index(fake, 'w', R.REGS['GMU_CM3_ITCM'].addr)
        check(ahb_first < itcm_first, 'gmu-boot arms the AHB timeouts right after the clocks, before the GMU loads (KGSL)')
        samples = cp['sqe_start']['samples']
        check(len(samples) == 4 and all(s_['cb0_fsr'] == '0x00000000' for s_ in samples)
              and log_index(fake, 'r', R.REGS['SMMU_CB_FSR'].addr, last=False) is not None,
              'C3S: four read-only samples after the start, CX first, no fault')
        first_after = next(e for e in fake.log[sqe_cntl + 1:] if e[0] in ('r', 'w'))
        check(first_after[0] == 'r' and R.block_of(first_after[1]).domain == 'CX',
              'the first access after CP_SQE_CNTL is a CX read (the SMMU answers even if GX does not)')
        # quiesce takes SID 1 off before CB0 goes off, and leaves every GPU stream faulting
        status, out = run3(fake, 'quiesce')
        check(status == 0 and not fake.smr[2] and fake.s2cr[2] >> 16 == 2 and not fake.cb[0][0], 'quiesce: SMR2 off too')
        check(fake.translate(1, R.CP_SQE_VA, quiet=True) is None, 'SID 1 faults after quiesce')
        no_violation(fake, 'quiesce with SID 1 routed')

        # --- 2. --stop-after C3 and C3P never touch CP_SQE_CNTL; a rerun after them is clean ----------
        fake = FakeStage3()
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3')
        check(status == 0 and out['stopped_after'] == 'C3' and 'prestart' not in out
              and log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr) is None, '--stop-after C3: hw_init only')
        checkpoint = Path(tmp) / 'c3p.prestart.json'
        with contextlib.redirect_stderr(io.StringIO()):
            status, out = run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3P', checkpoint=str(checkpoint))
        saved = json.loads(checkpoint.read_text())
        check(status == 0 and out['stopped_after'] == 'C3P' and saved['prestart']['cx'] == out['prestart']['cx']
              and log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr) is None,
              '--stop-after C3P: snapshot logged and written to the checkpoint file, SQE not started')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 0 and out['order'][-1] == 'fence', 'then the full cp-fence runs C1..C6')
        no_violation(fake, 'C3, C3P, then full')

        # --- 3. run 1 again: the link dies at CP_SQE_CNTL. The snapshot is already on disk. -----------
        fake = FakeStage3(sqe_reset=True)
        happy_path(fake, fw, upto='oob')
        checkpoint = Path(tmp) / 'run1.prestart.json'
        with contextlib.redirect_stderr(io.StringIO()):
            status, out = run3(fake, 'cp-fence', fw_dir=fw, checkpoint=str(checkpoint))
        check(status == 5 and out['stopped']['step'] == 'C3S' and 'prestart' in out
              and 'written' not in out['sqe_start'] and json.loads(checkpoint.read_text())['problems'] == [],
              f'run-1 replay: exit 5 at C3S, the snapshot in the log and the checkpoint ({out["stopped"]})')
        check('after_error' in out['serror_guard'], 'the guard read after a dead link is reported, not raised')

        # --- 4. the probe: every CB0 VA unmapped, the SQE's first fetch is a CB0 translation fault -----
        fake = FakeStage3()
        happy_path(fake, fw, upto='oob')
        run3(fake, 'cp-fence', fw_dir=fw, stop_after='C3P')          # real tables first
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw, sqe_probe='fault')
        tlbi = [i for i, (o, a, v) in enumerate(fake.log[mark:]) if o == 'w' and a == R.REGS['SMMU_CB_TLBIASID'].addr]
        check(tlbi, 'probe after real tables: CB0 read back identical, tables changed, so its TLB is invalidated')
        check(status == 0 and out['stopped_after'] == 'C3S' and fake.sqe_fetch_fault and not fake.cp_running
              and 'faulted as intended' in out['sqe_start']['verdict'] and '0x10000000' in out['sqe_start']['verdict'],
              f'probe: CB0 translation fault at the SQE VA, reported ({out.get("stopped")}, {out.get("sqe_start")})')
        check(out['prestart']['cb0_walk']['sqe@0x10000000'] == 'fault' and out['gpu_cb']['tables'].startswith('probe'),
              'probe: the pre-start walk shows every CP VA faulting')
        check(host_record(fake)['sqe_probe'] == host_record(fake)['boot_token'], 'probe recorded for this GMU boot')
        mark = len(fake.log)
        status, out = run3(fake, 'status')
        check(status == 0 and int(out['faults']['cb0']['fsr'], 16) & 2 and out['faults']['cb0']['far'] == '0x10000000'
              and set(out['cx_ahb'].values()) == {'0x00001b12'} and not [e for e in fake.log[mark:] if e[0] == 'w'],
              'status (read-only) shows the probe\'s CB0 fault and the AHB timeouts')
        mark = len(fake.log)
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C0' and 'sqe-probe' in out['stopped']['reason']
              and not writes_to(fake, ('GPU', 'SMMU'), mark), 'after a probe: cp-fence refuses until quiesce')
        status, out = run3(fake, 'quiesce')
        check(status == 0 and host_record(fake)['sqe_probe'] == 0, 'quiesce clears the probe mark')
        happy_path(fake, fw)
        no_violation(fake, 'probe, quiesce, full cycle')

        # --- 5. pre-start refusals ---------------------------------------------------------------
        fake = FakeStage3(prestart_fault=True)
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C3P' and 'SMMU fault' in out['stopped']['reason']
              and log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr) is None,
              'a global fault recorded before the start: C3P stops, SQE never started')
        status, out = run3(fake, 'cp-fence', fw_dir=fw, prestart_faults_ok=True)
        check(status == 0 and out['order'][-1] == 'fence', '--prestart-faults-ok starts it anyway')

        # --- 6. the aperture SMC ------------------------------------------------------------------
        fake = FakeStage3(gpu_version=2)
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C3' and 'version 3' in out['stopped']['reason']
              and log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr) is None, 'a version-2 stage: stop before the start')
        status, out = run3(fake, 'cp-fence', fw_dir=fw, no_aperture=True)
        check(status == 0 and out['aperture'] == {'skipped': '--no-aperture'} and smc_count(fake, S3.SMC_CP_APERTURE) == 0
              and out['order'][-1] == 'fence', '--no-aperture on a version-2 stage: hw_init without the SMC, then the fence')
        fake = FakeStage3(aperture_x0=-22)
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 0 and 'note' in out['aperture'], 'TrustZone refusing the aperture is recorded, as Linux logs it')
        fake = FakeStage3(aperture_fail=-10)
        happy_path(fake, fw, upto='oob')
        status, out = run3(fake, 'cp-fence', fw_dir=fw)
        check(status == 2 and out['stopped']['step'] == 'C3' and host_record(fake)['tz_ok'] == S3.TZ_PENDING
              and log_index(fake, 'w', R.REGS['CP_SQE_CNTL'].addr) is None,
              'an aperture SMC left open in TrustZone: stop, recorded as pending (reboot)')

        # --- 7. AHB timeouts that do not take ------------------------------------------------------
        fake = FakeStage3(ahb_stuck=True)
        happy_path(fake, fw, upto='gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, rail_vote_unseen=False)
        check(status == 2 and out['stopped']['step'] == 'B2' and 'not armed' in out['stopped']['reason']
              and log_index(fake, 'w', R.REGS['GMU_CM3_ITCM'].addr) is None, 'AHB timeouts not taken: stop at B2')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, no_ahb_timeout=True)
        check(status == 0 and out['cx_ahb']['skipped'], '--no-ahb-timeout goes on without them')
        for command in ('hfi', 'oob'):
            check(run3(fake, command, fw_dir=fw)[0] == 0, f'{command} after --no-ahb-timeout')
        status, out = run3(fake, 'cp-fence', fw_dir=fw, no_ahb_timeout=True)
        check(status == 0 and out['order'][-1] == 'fence', 'cp-fence --no-ahb-timeout: C3P does not insist on them')
        no_violation(fake, 'AHB timeouts skipped')

        # --- 8. reset-reason: AO reads only ---------------------------------------------------------
        fake = FakeStage3(imem={0x14680000: 0xE47B337D, 0x14680004: 0xCE14091A})
        status, out = run3(fake, 'reset-reason')
        imem_reads = [a for o, a, _v in fake.log if o == 'r' and R.block_of(a).name == 'IMEM']
        check(status == 0 and imem_reads == [R.REGS['IMEM_PIL_RELOC'].addr] and 'dload_magic' not in out['imem']
              and not [e for e in fake.log if e[0] == 'w'],
              'reset-reason by default reads only the Linux-documented pil-reloc word (review)')
        fake = FakeStage3(imem={0x14680000: 0xE47B337D, 0x14680004: 0xCE14091A})
        status, out = run3(fake, 'reset-reason', inferred_imem=True)
        check(status == 0 and out['imem']['dload_cookie_matches'] and out['imem']['restart_reason_decoded'] ==
              'normal reboot' and not [e for e in fake.log if e[0] == 'w'],
              'reset-reason --inferred-imem: read-only, decoded')
        check(all(R.block_of(a).name == 'IMEM' for o, a, _v in fake.log if o == 'r'), 'reset-reason reads IMEM only')

        # --- 9. review: AHB registers that are not KGSL's are never written -----------------------------
        fake = FakeStage3()
        fake.cx_ahb = [0x0, 0x0, 0x80000001, 0x0, 0x0]          # a bit KGSL does not document
        happy_path(fake, fw, upto='gmu-smmu')
        status, out = run3(fake, 'gmu-boot', fw_dir=fw, rail_vote_unseen=False)
        check(status == 2 and out['stopped']['step'] == 'B2' and 'outside' in out['stopped']['reason']
              and not [e for e in fake.log if e[0] == 'w' and 0x03D9E040 <= e[1] < 0x03D9E054]
              and log_index(fake, 'w', R.REGS['GMU_CM3_ITCM'].addr) is None,
              'AHB registers with bits outside KGSL\'s fields: stop at B2 before any AHB write')
        fake = FakeStage3()
        fake.cx_ahb = [0x1B12, 0x0, 0x1B11, 0x0, 0x0]           # KGSL-shaped values: rewritten to ours
        happy_path(fake, fw, upto='gmu-boot')
        check(fake.cx_ahb == [0x1B12] * 5, 'AHB registers with KGSL-shaped values are armed as usual')


def main():
    for test in (test_registry, test_encoders, test_happy_path, test_idempotent, test_order_enforced, test_failures,
                 test_stop_after_and_undo, test_review_fixes, test_fix_round, test_review2, test_sqe_reset, test_cli):
        test()
    print(f'PASS: {checks} a16gpu Stage 3 model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
