# SPDX-License-Identifier: MIT
"""Adreno X2-90, Stage 3: GMU SMMU, GMU boot, HFI/OOB and a CP fence through q1n1.

The subcommands of tools/a16gpu.py that come after cx-on (docs/A16-ADRENO-STAGE3.md):

  arena [--claim]          the GPU arena in bootinfo; --claim takes one from Conventional memory
  status                   READ-ONLY: arena, SMMU CB0/CB1, GMU, HFI, OOB, CP, RSC votes
  rsc-survey               READ-ONLY: what the apps RSC (drv-2) TCSes hold
  gmu-smmu --write         P2: LPAE tables for SID 5, context bank 1, SMR/S2CR (--undo reverses)
  gmu-boot --write         P3: RPMh MX/CX, PLL0 + GMU/HUB RCGs, TZ SMC, firmware, CM3 start
  hfi --write              P4: HFI TABLE (lowest OPP), BW table, CORE_FW_START, START, perf vote
  oob --write              P4: OOB GPU_SET, GX-on proof
  cp-fence --write         P5: SIDs 0/1 on CB0, a8xx hw_init (+ CP aperture SMC), pre-start snapshot,
                           SQE start, CP_ME_INIT, SECVID, CP_MEM_WRITE fence (--sqe-probe fault: start the
                           SQE with every CB0 VA unmapped and report where its first fetch faulted)
  blit --write             P6 (a16gpu_p6p7.py): 2D clear and 2D copy in an arena target, read back; with
                           --to-framebuffer also into one rectangle of the GOP framebuffer (only its rows mapped)
  triangle --write         P7 (a16gpu_p6p7.py): SkyLight SimpleVertex + SimpleTextureFragment, one triangle,
                           read back and compared with the M3 Max Metal render
  quiesce --write          undo: GBIF halt, GMU slumber/reset, SMMU fault-all, clocks back to XO
  reset-reason             READ-ONLY: IMEM pil-reloc word (AO); --inferred-imem adds the INFERRED msm-imem
                           words (download cookie, restart reason, boot stats): never in a GPU session

Every subcommand is a fresh process: it re-proves the Stage 1 CX gates from reads (cx-on must
have run), re-reads the hardware state it depends on, and treats the arena's host record only as
bookkeeping, never as evidence. Sub-steps carry ids (G*, B*, H*, O*, C*, Q*) and --stop-after
ends a run early. GX registers are refused before a byte is sent until the GMU itself reports GX
on (OOB GPU_SET acknowledged and GMU_PWR_CLK_STATUS bits 0/1 clear) in the same run.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
import os
import secrets
import struct
import sys
import time

import adreno_x2 as R
import a16gpu as S1

Q = S1.q1n1proxy
hx = S1.hx
Stop, SErrorStop, AbortStop, PollTimeout, Refused = S1.Stop, S1.SErrorStop, S1.AbortStop, S1.PollTimeout, S1.Refused

P_MEMSET64, P_DC_CIVAC = 0x204, 0x30A      # m1n1 src/proxy.h (q1n1-proxy.c implements both)

# platform/uefi/gpu-el2.h struct q1n1_gpu_state, all u64 in this order.
GPU_STATE_FIELDS = ('magic version size arena_base arena_size arena_source arena_attr claims claim_status '
                    'smc_calls smc_refused smc_ok smc_status smc_fid smc_x1 smc_x2 smc_x3 '
                    'smc_res0 smc_res1 smc_res2 smc_res3 smc_interrupted smc_busy smc_undefined').split()
GPU_MAGIC = 0x31555047314E3151               # "Q1N1GPU1"
GPU_VERSION = 2                              # 2: GPU_STORE32, arenas zeroed when made (gpu-el2.h)
TZ_PENDING = 2                               # host record tz_ok: the SMC was left open in TrustZone
TZ_LEFT_PENDING = (-8, -10)                  # Q1N1_GPU_EWAITQ, Q1N1_GPU_EINTR
GPU_QUERY, GPU_CLAIM, GPU_SMC, GPU_RELEASE, GPU_STORE32 = 0, 1, 2, 3, 4
GPU_STATUS = {0: 'ok', -1: 'invalid argument', -2: 'no EFI memory map', -3: 'no Conventional region fits',
              -4: 'an arena of another size exists', -5: 'the EL2 tables do not map it as Normal memory',
              -6: 'SMC not allow-listed', -7: 'SMC trapped (UNDEFINED at EL2)', -8: 'TrustZone asked to sleep (WAITQ)',
              -9: 'TrustZone busy after retries', -10: 'TrustZone interrupted too often', -11: 'release refused'}
ARENA_SOURCES = {0: 'none', 1: 'efi (reserved before ExitBootServices)', 2: 'claimed from Conventional memory'}
SMC_GPU_INIT = (0x42002801, 1, 0x9, 0)       # fid, arginfo, ALWAYS_EN|TSENSE_EN, -- (qcom_scm_gpu_init_regs)
SMC_CP_APERTURE = R.SMC_CP_APERTURE          # qcom_scm_set_gpu_smmu_aperture(0) (payload version 3)

# The host's own record in the arena (bookkeeping across invocations, never evidence).
HOST_MAGIC = 0x3353363141314151              # "QA1A16S3"
HOST_FIELDS = ('magic version arena_base gmu_ttbr gpu_ttbr boot_token hfi_token hfi_seq oob_token cp_token '
               'rb_wptr fence_seq fence_value rsc_cx rsc_mx tz_ok hwinit_token pdc_sleep sqe_probe p67_token '
               'fb_mapped hfi_opps').split()
# hfi_opps (appended, agents/dvfs FAILURE-MODES.md O4; an older record reads 0): the number of GPU OPPs (off entry
# excluded) in the perf TABLE the hfi step sent for the GMU boot hfi_token names. Snapintosh's GPU governor
# (compat/gpu-dvfs.h) reads it and never votes a perf index past it.
# sqe_probe (appended; an older record reads 0 here, the arena is zeroed): the boot token of a GMU boot
# whose SQE was started by `cp-fence --sqe-probe fault`. That CP never runs CP_ME_INIT: quiesce first.
# p67_token, fb_mapped (appended for P6/P7, a16gpu_p6p7.py): the boot token whose CB0 carries the render
# window, and the framebuffer span's physical base while `blit --to-framebuffer` has it mapped (0 otherwise;
# the tables in DRAM are re-read regardless, this only says an earlier run was interrupted).

POLL = {'branch': (10, 0.5), 'rcg': (10, 0.5), 'pll': (200, 0.5), 'tlb': (20, 0.5), 'gmu_init': (400, None),
        'hfi_start': (50, None), 'msgq': (2000, None), 'oob': (100, None), 'rsc': (200, 1.0), 'rscc': (100, 0.5),
        'hfi_drain': (50, 0.2),
        'cp': (400, 1.0), 'fence': (400, 1.0), 'gbif': (100, 0.5), 'idle': (100, 0.5)}
DEFAULT_FW_DIRS = (str(Path(__file__).resolve().parent.parent / 'build' / 'a16-gpu' / 'fw'),)


def u32(data, index):
    return struct.unpack_from('<I', data, 4 * index)[0]


class Session3(S1.Session):
    """Stage 1's Session with the Stage 3 rules: GX only behind gate gx, the arena as the only
    writable DRAM, cache maintenance around every DRAM access, and the bootinfo gpu_call."""

    def __init__(self, proxy, allow_writes=False, guarded=True, clock=time.monotonic):
        super().__init__(proxy, allow_writes=allow_writes, guarded=guarded, clock=clock)
        self.arena = None           # (base, size)
        self.gpu_call = 0
        self.gpu_record = 0

    def _check(self, reg, write):
        if reg.gate == 'never':
            raise Refused(f'{reg.name} @ {reg.addr:#010x} is never touched')
        if reg.domain == 'GX' and 'gx' not in self.proven:
            raise Refused(f'{reg.name} @ {reg.addr:#010x} is GX: refused until the GMU reports GX on '
                          '(OOB GPU_SET acked, GMU_PWR_CLK_STATUS bits 0/1 clear)')
        if write and ('w' not in reg.access or not self.allow_writes):
            raise Refused(f'write to {reg.name} not allowed (access {reg.access!r}, writes '
                          f'{"on" if self.allow_writes else "off"})')
        if not write and 'r' not in reg.access:
            raise Refused(f'{reg.name} is not readable')
        if reg.gate not in self.proven:
            raise Refused(f'{reg.name} needs gate {reg.gate}; proven: {sorted(self.proven)}')

    def poll3(self, name, mask, want, kind, index=0, budget=None):
        """Bounded in reads and in wall time; at least two reads whatever the clock says."""
        limit, seconds = POLL[kind]
        seconds = budget if budget is not None else (seconds if seconds is not None else 1.0)
        deadline = self.clock() + seconds
        value = attempt = 0
        for attempt in range(1, limit + 1):
            value = self.rd(name, index)
            if value & mask == want:
                self.polls.append({'reg': name, 'reads': attempt, 'ok': True})
                return value
            if attempt >= 2 and self.clock() > deadline:
                break
        self.polls.append({'reg': name, 'reads': attempt, 'ok': False})
        raise PollTimeout(self.step, f'{name} = {value:#010x}: (value & {mask:#x}) != {want:#x} after {attempt} '
                                     f'reads', reg=name, value=hx(value))

    # --- DRAM: the arena only, cleaned to PoC after writes and before reads -----------------
    def _arena_addr(self, offset, length):
        if not self.arena:
            raise Refused('no GPU arena proven in this run')
        base, size = self.arena
        if offset < 0 or length < 0 or offset + length > size:
            raise Refused(f'arena access {offset:#x}+{length:#x} outside the {size:#x}-byte arena')
        return base + offset

    def _dram(self, op, address, call):
        try:
            return call()
        except Q.ProxyRemoteError as problem:
            self._remote_failure(problem, op, None, address)

    def clean(self, address, length):
        self._dram('dc_civac', address, lambda: self.p.request(P_DC_CIVAC, address, length))

    def mem_write(self, offset, data, what):
        if not self.allow_writes:
            raise Refused(f'arena write ({what}) with writes off')
        address = self._arena_addr(offset, len(data))
        self._dram('writemem', address, lambda: self.p.writemem(address, bytes(data)))
        self.clean(address, len(data))
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'writemem', 'addr': hx(address),
                         'size': len(data), 'what': what, 'domain': 'DRAM'})

    def mem_store32(self, offset, value, what):
        """One word the GMU shares a cache line with (HFI queue indices): clean+invalidate, store,
        clean+invalidate in one request on the target (gpu_call GPU_STORE32), never a writemem
        and a separate clean, which would hold the whole line dirty for a round trip and write the
        GMU's older words back over its newer ones."""
        if not self.allow_writes:
            raise Refused(f'arena store ({what}) with writes off')
        address = self._arena_addr(offset, 4)
        if address % 4:
            raise Refused('arena word stores are 4-byte aligned')
        status = self.call_gpu(GPU_STORE32, address, value & 0xFFFFFFFF, what=what)
        if status:
            raise Stop(self.step, f'GPU_STORE32 of {what} at {address:#x} refused: {GPU_STATUS.get(status, status)}')
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'store32', 'addr': hx(address),
                         'size': 4, 'what': what, 'domain': 'DRAM'})

    def mem_fill(self, offset, length, what):
        if not self.allow_writes:
            raise Refused(f'arena fill ({what}) with writes off')
        address = self._arena_addr(offset, length)
        if address % 8 or length % 8:
            raise Refused('arena fills are 8-byte aligned')
        self._dram('memset', address, lambda: self.p.request(P_MEMSET64, address, 0, length))
        self.clean(address, length)
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'memset', 'addr': hx(address),
                         'size': length, 'what': what, 'domain': 'DRAM'})

    def mem_read(self, offset, length, what):
        address = self._arena_addr(offset, length)
        self.clean(address, length)      # drop any line the core fetched before the device wrote
        data = self._dram('readmem', address, lambda: self.p.readmem(address, length))
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'readmem', 'addr': hx(address),
                         'size': length, 'what': what, 'domain': 'DRAM'})
        return bytes(data)

    def payload_read(self, address, length, what):
        """The payload's own records (gpu_state): plain RAM the payload publishes."""
        data = self._dram('readmem', address, lambda: self.p.readmem(address, length))
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'readmem', 'addr': hx(address),
                         'size': length, 'what': what, 'domain': 'DRAM'})
        return bytes(data)

    def call_gpu(self, op, *args, what=''):
        if not self.gpu_call:
            raise Refused('the running stage publishes no gpu_call')
        if op in (GPU_CLAIM, GPU_SMC, GPU_RELEASE, GPU_STORE32) and not self.allow_writes:
            raise Refused(f'gpu_call op {op} with writes off')
        value = self._dram('call', self.gpu_call, lambda: self.p.call(self.gpu_call, op, *args))
        self.log.append({'seq': len(self.log), 'step': self.step, 'op': 'gpu_call', 'call': op,
                         'args': [hx(a) for a in args], 'what': what, 'result': hx(value & 0xFFFFFFFFFFFFFFFF)})
        return Q.signed64(value)

    def wr_update(self, name, mask, bits, index=0):
        old = self.rd(name, index)
        new = (old & ~mask) | (bits & mask)
        if new != old:
            self.wr(name, new, index)
        return new


# --- pure helpers ----------------------------------------------------------------------------------
def region(name):
    return R.ARENA_LAYOUT[name]


def gpu_state_decode(blob):
    values = dict(zip(GPU_STATE_FIELDS, struct.unpack_from(f'<{len(GPU_STATE_FIELDS)}Q', blob)))
    for key in ('claim_status', 'smc_status'):
        values[key] = Q.signed64(values[key])
    return values


def host_state_blank(base):
    return dict(dict.fromkeys(HOST_FIELDS, 0), magic=HOST_MAGIC, version=1, arena_base=base)


def host_state_pack(state):
    return struct.pack(f'<{len(HOST_FIELDS)}Q', *(state[k] & 0xFFFFFFFFFFFFFFFF for k in HOST_FIELDS))


class PageTables:
    """AArch64 stage-1 LPAE, 4 KiB granule, T0SZ 32 (start level 1), in a pool inside the arena."""

    def __init__(self, arena_base, pool_offset, pool_size):
        self.base, self.pool_offset, self.pool_size = arena_base, pool_offset, pool_size
        self.used = 0
        self.tables = {}
        self.root = self._alloc()

    def _alloc(self):
        if self.used + 4096 > self.pool_size:
            raise Refused('page-table pool exhausted')
        phys = self.base + self.pool_offset + self.used
        self.used += 4096
        self.tables[phys] = bytearray(4096)
        return phys

    def _entry(self, table, index):
        return struct.unpack_from('<Q', self.tables[table], 8 * index)[0]

    def _set(self, table, index, value):
        struct.pack_into('<Q', self.tables[table], 8 * index, value)

    def _next(self, table, index):
        entry = self._entry(table, index)
        if entry & 3 == R.PTE_TYPE_TABLE:
            return entry & R.PTE_ADDR_MASK
        if entry:
            raise Refused('page-table overlap')
        child = self._alloc()
        self._set(table, index, child | R.PTE_TYPE_TABLE)
        return child

    def map(self, va, pa, size, attrs):
        if va % 4096 or pa % 4096 or size % 4096 or va + size > 1 << R.CB_T0SZ:
            raise Refused(f'bad mapping {va:#x} -> {pa:#x} + {size:#x}')
        for page in range(0, size, 4096):
            v = va + page
            l2 = self._next(self.root, v >> 30 & 3)
            l3 = self._next(l2, v >> 21 & 0x1FF)
            if self._entry(l3, v >> 12 & 0x1FF):
                raise Refused(f'VA {v:#x} mapped twice')
            self._set(l3, v >> 12 & 0x1FF, (pa + page) | attrs | R.PTE_TYPE_PAGE)

    def image(self):
        """(arena offset, bytes) covering every table, contiguous from the pool start."""
        blob = b''.join(bytes(self.tables[self.base + self.pool_offset + 4096 * n]) for n in range(self.used // 4096))
        return self.pool_offset, blob


def walk(read_u64, ttbr, va, t0sz=R.CB_T0SZ):
    """Translate va through tables read with read_u64(phys); (pa, leaf) or None. Used by status and the model.
    t0sz: the bank's TCR T0SZ (32, or CB_T0SZ_WIDE for a --va-wide CB0): start level 1 either way."""
    if va >> (64 - t0sz):
        return None
    table = ttbr & R.PTE_ADDR_MASK
    for shift in (30, 21):
        entry = read_u64(table + 8 * (va >> shift & ((1 << (34 - t0sz)) - 1 if shift == 30 else 0x1FF)))
        if entry & 3 != R.PTE_TYPE_TABLE:
            return None
        table = entry & R.PTE_ADDR_MASK
    leaf = read_u64(table + 8 * (va >> 12 & 0x1FF))
    if leaf & 3 != R.PTE_TYPE_PAGE:
        return None
    return (leaf & R.PTE_ADDR_MASK) | (va & 0xFFF), leaf


PRIV_RW = R.PTE_NG | R.PTE_AF | R.PTE_SH_OS | R.PTE_ATTR_NC              # IOMMU_READ|WRITE|PRIV, not cached
UNPRIV_RW = PRIV_RW | R.PTE_AP_UNPRIV
UNPRIV_RO = UNPRIV_RW | R.PTE_AP_RDONLY


def gmu_mappings(base):
    """a6xx_gmu_init's GMU VM (icache, dummy, debug with MSM_BO_MAP_PRIV; log and HFI dynamic)."""
    return (('icache', R.GMU_ICACHE_VA, base + region('gmu_icache')[0], R.GMU_ICACHE_SIZE, PRIV_RW),
            ('dummy', R.GMU_DUMMY_VA, base + region('gmu_dummy')[0], R.GMU_DUMMY_SIZE, PRIV_RW),
            ('debug', R.GMU_DEBUG_VA, base + region('gmu_debug')[0], R.GMU_DEBUG_SIZE, PRIV_RW),
            ('log', R.GMU_LOG_VA, base + region('gmu_log')[0], R.GMU_LOG_SIZE, UNPRIV_RW),
            ('hfi', R.GMU_HFI_VA, base + region('gmu_hfi')[0], R.GMU_HFI_SIZE, UNPRIV_RW))


def gpu_mappings(base):
    """The CP's buffers on CB0. Linux maps the SQE read-only and the ring read-only+priv; both are
    mapped unprivileged-readable here so either privilege level of the fetch works (INFERRED)."""
    return (('sqe', R.CP_SQE_VA, base + region('cp_sqe')[0], region('cp_sqe')[1], UNPRIV_RO),
            ('rb', R.CP_RB_VA, base + region('cp_rb')[0], region('cp_rb')[1], UNPRIV_RO),
            ('memptrs', R.CP_MEMPTRS_VA, base + region('cp_memptrs')[0], region('cp_memptrs')[1], UNPRIV_RW))


def build_tables(base, which, probe=False):
    """probe (cp-fence --sqe-probe fault): CB0 gets a root table with nothing in it, so every GPU fetch --
    the SQE's first -- ends as a CB0 translation fault that names the VA and the stream."""
    pool_offset, pool_size = region('tables')
    half = pool_size // 2
    tables = PageTables(base, pool_offset + (half if which == 'gpu' else 0), half)
    mappings = gmu_mappings(base) if which == 'gmu' else gpu_mappings(base)
    for _name, va, pa, size, attrs in (() if probe else mappings):
        tables.map(va, pa, size, attrs)
    return tables


def ttbr_for(tables, cb):
    return tables.root | (cb << 48)          # ASID = context bank index (arm-smmu.c cfg->asid = cbndx)


def smr_matches(smr, sid):
    """Arm SMMUv2 stream matching: valid and (sid ^ id) & ~mask == 0 (15-bit IDs, EXIDS off on the A16)."""
    return bool(smr & R.SMR_VALID) and not ((sid ^ smr) & ~(smr >> 16) & 0x7FFF)


def load_firmware(fw_dir):
    out = {}
    for key, (name, size, sha) in R.FIRMWARE.items():
        path = Path(fw_dir) / name
        if not path.is_file():
            raise Stop('fw', f'{path} missing (pass --fw-dir: the directory with the sha-verified firmware files)')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != size or digest != sha:
            raise Stop('fw', f'{path}: size {len(data)} sha256 {digest} is not the pinned {size} {sha}')
        out[key] = data
    return out


def parse_gmu_fw(data):
    """a6xx_gmu_fw_load: {u32 addr, size, type, value; data[size]} until the end; size 0 skipped."""
    blocks, offset = [], 0
    while offset + 16 <= len(data):
        addr, size, kind, value = struct.unpack_from('<4I', data, offset)
        if offset + 16 + size > len(data):
            raise Stop('fw', f'GMU firmware block at {offset:#x} runs past the end')
        blocks.append({'offset': offset, 'addr': addr, 'size': size, 'type': kind, 'value': value,
                       'data': data[offset + 16:offset + 16 + size]})
        offset += 16 + size
    if offset != len(data):
        raise Stop('fw', 'GMU firmware has trailing bytes')
    return blocks


def plan_gmu_fw(blocks):
    """Route every non-empty block as a6xx_gmu_fw_load does: ITCM, DTCM, else a GMU buffer."""
    plan = {'itcm': [], 'dtcm': [], 'memory': [], 'prealloc': [], 'version': None}
    buffers = [(name, va, size) for name, va, _pa, size, _a in gmu_mappings(0) if name in ('icache', 'dummy', 'debug')]
    for blk in blocks:
        if not blk['size']:
            if blk['type'] == 7:                # prealloc request: must fall inside a mapped buffer
                plan['prealloc'].append((blk['addr'], blk['value']))
            continue
        if blk['size'] % 4:
            raise Stop('fw', f'GMU block at {blk["addr"]:#x} is not a whole number of words')
        addr = blk['addr']
        if R.GMU_ITCM_BASE <= addr < R.GMU_ITCM_BASE + R.GMU_TCM_SIZE:
            if addr + blk['size'] > R.GMU_ITCM_BASE + R.GMU_TCM_SIZE:
                raise Stop('fw', 'ITCM block overflows 16 KiB')
            plan['itcm'].append(((addr - R.GMU_ITCM_BASE) // 4, blk['data']))
        elif R.GMU_DTCM_BASE <= addr < R.GMU_DTCM_BASE + R.GMU_TCM_SIZE:
            if addr + blk['size'] > R.GMU_DTCM_BASE + R.GMU_TCM_SIZE:
                raise Stop('fw', 'DTCM block overflows 16 KiB')
            plan['dtcm'].append(((addr - R.GMU_DTCM_BASE) // 4, blk['data']))
            word = (0xFF8 * 4 - (addr - R.GMU_DTCM_BASE)) // 4
            if 0 <= word < blk['size'] // 4:
                plan['version'] = u32(blk['data'], word)
        else:
            for name, va, size in buffers:
                if va <= addr and addr + blk['size'] <= va + size:
                    plan['memory'].append((name, addr - va, blk['data']))
                    break
            else:
                raise Stop('fw', f'GMU block {addr:#x}+{blk["size"]:#x} matches no buffer (Linux only logs this)')
    for addr, length in plan['prealloc']:
        if not any(va <= addr and addr + length <= va + size for _n, va, size in buffers):
            raise Stop('fw', f'GMU prealloc request {addr:#x}+{length:#x} is outside the mapped buffers')
    return plan


def aux_levels(entry):
    blob = bytes.fromhex(entry['aux'])
    return [struct.unpack_from('<H', blob, i)[0] for i in range(0, len(blob) - 1, 2)]


def aux_bcm(entry):
    blob = bytes.fromhex(entry['aux'])
    unit, width, vcd = struct.unpack_from('<IHB', blob)
    return {'unit': unit, 'width': width, 'vcd': vcd}


def arc_vote(pri, sec, level, strict=False):
    """a6xx_gmu_rpmh_arc_votes_init for one frequency. Linux falls back to the highest non-zero secondary level when
    none reaches `level` (an under-vote of that rail); strict (every GPU OPP since agents/dvfs O2) refuses instead."""
    pindex = next((j for j, v in enumerate(pri) if v >= level), None)
    if pindex is None:
        raise Stop('H1', f'RPMh level {level} is not in the primary list {pri}')
    sindex = 0
    for j, v in enumerate(sec):
        if v >= level:
            sindex = j
            break
        if v:
            sindex = j
    else:
        if strict and level:
            raise Stop('H1', f'RPMh level {level} is above every secondary level {sec}: refused, not under-voted')
    return (pri[pindex] & 0xFFFF) << 16 | sindex << 8 | pindex


def dep_vote(mx, level):
    index = next((j for j, v in enumerate(mx) if v >= level), None)
    if index is None:
        raise Stop('H1', f'MX level {level} is not in {mx}')
    return (0x3FFF << 14) | (index << 8) | 0xFF


def opp_levels(speed_bin, max_opps):
    """GPU OPPs this speed bin supports (opp-supported-hw BIT(bin)), lowest first, capped."""
    usable = [opp for opp in R.GPU_OPPS if opp[3] >> speed_bin & 1]
    return usable[:max_opps]


def rail_refusal(cmd_db, level):
    """agents/dvfs O2: why RPMh level `level` cannot be voted on every rail the a8xx perf table names without an
    under-vote (a6xx_gmu.c:1658-1820), or None. GX: gfx.lvl (primary) and gmxc.lvl, or mx.lvl without gmxc
    (secondary, where Linux would fall back to the highest non-zero level); the MX dependency vote: mx.lvl (where
    Linux fails the whole table). The A16's mx.lvl tops out at 452 (TURBO_L4), so 1850 MHz (456) is refused."""
    sec_key = 'gmxc.lvl' if cmd_db.get('gmxc.lvl') else 'mx.lvl'
    for key, role in (('gfx.lvl', 'GX'), (sec_key, 'GX secondary'), ('mx.lvl', 'MX dependency')):
        levels = aux_levels(cmd_db[key])
        if not any(v >= level for v in levels):
            return f'{role} rail {key} tops out at level {max(levels)}, below {level}'
    return None


def rail_safe_opps(cmd_db, speed_bin):
    """agents/dvfs O1/O2: this bin's GPU OPPs, lowest first, up to (not including) the first one some rail cannot
    carry; with the refused OPP and its reason (None when every OPP of the bin passes)."""
    safe = []
    for opp in opp_levels(speed_bin, len(R.GPU_OPPS)):
        why = rail_refusal(cmd_db, opp[1])
        if why:
            return safe, (opp, why)
        safe.append(opp)
    return safe, None


def perf_table(cmd_db, speed_bin, max_opps=1, max_gmu_opps=1):
    """a8xx_hfi_send_perf_table payload (after the header dword): version, type, GX entry, CX entry."""
    gfx, gmxc, mx, cx = (aux_levels(cmd_db[k]) for k in ('gfx.lvl', 'gmxc.lvl', 'mx.lvl', 'cx.lvl'))
    gpu = [(0, 0)] + [(hz, level) for hz, level, _bw, _hw in opp_levels(speed_bin, max_opps)]
    gmu = [(0, 0)] + list(R.GMU_OPPS[:max_gmu_opps])
    words = [0, R.HFI_TABLE_GPU_PERF, len(gpu), 3]
    for n, (hz, level) in enumerate(gpu):
        words += [arc_vote(gfx, gmxc, level, strict=True) if hz else arc_vote(gfx, gmxc, 0),
                  0xFFFFFFFF if n == 0 else dep_vote(mx, level), hz // 1000]
    words += [len(gmu), 2]
    for hz, level in gmu:
        words += [arc_vote(cx, mx, level) if hz else arc_vote(cx, mx, 0), hz // 1000]
    return words


def perf_vote(speed_bin, max_opps, perf_opp=1):
    """GX_BW_PERF_VOTE payload (ack type, perf-table index, bw-table index) for GPU OPP perf_opp (1 = lowest), and
    its log name. Index 0 of both tables is the off entry; the bw index is that OPP's bandwidth row (bw_table)."""
    opps = opp_levels(speed_bin, max_opps)
    if not 1 <= perf_opp <= len(opps):
        raise Stop('H0', f'--perf-opp {perf_opp} is outside the {len(opps)} OPPs in the table (--max-opps {max_opps})')
    bws = [0] + sorted({bw for _hz, _l, bw, _hw in opps})
    hz, level, bw, _hw = opps[perf_opp - 1]
    vote = [1, perf_opp, bws.index(bw)]
    if perf_opp == 1 and vote == [1, 1, 1]:
        return vote, 'GX_BW_PERF_VOTE (ack, perf 1, bw 1: lowest OPP)'
    return vote, f'GX_BW_PERF_VOTE (ack, perf {perf_opp}, bw {vote[2]}: {hz // 1000000} MHz, level {level}, {bw} kBps)'


def bcm_cmd(commit, valid, x, y):
    return (R.BCM_COMMIT if commit else 0) | (R.BCM_VALID if valid else 0) | (x & 0x3FFF) << 14 | (y & 0x3FFF)


def bw_votes(cmd_db, bandwidth):
    """a6xx_gmu_rpmh_bw_votes_init for one bandwidth (kBps) over SH0, MC0, ACV."""
    data, db = [], [aux_bcm(cmd_db[name]) for name, *_ in R.GPU_BCMS]
    for index, (name, buswidth, fixed, perfmode) in enumerate(R.GPU_BCMS):
        commit = index == len(R.GPU_BCMS) - 1 or db[index]['vcd'] != db[index + 1]['vcd']
        if not bandwidth:
            data.append(bcm_cmd(commit, False, 0, 0))
        elif fixed:
            data.append(bcm_cmd(commit, True, 0, perfmode))     # a8xx is not a7xx: perfmode always
        else:
            peak = bandwidth * db[index]['width'] // buswidth * 1000 // db[index]['unit']
            vote = min(max(peak, 1), 0x3FFF)
            data.append(bcm_cmd(commit, True, vote, vote))
    return data


def bw_table(cmd_db, speed_bin, max_opps=1):
    """struct a6xx_hfi_msg_bw_table after the header dword (a6xx_generate_bw_table)."""
    bws = [0] + sorted({bw for _hz, _l, bw, _hw in opp_levels(speed_bin, max_opps)})
    addrs = [int(cmd_db[name]['addr'], 16) for name, *_ in R.GPU_BCMS]
    rows = [bw_votes(cmd_db, bw) for bw in bws]
    ddr_wait = sum(1 << j for j, cmd in enumerate(rows[0]) if cmd & R.BCM_COMMIT)
    cnoc = [bcm_cmd(True, False, 0, 0), bcm_cmd(True, True, 0, 1)]
    words = [len(bws), 1, len(addrs), cnoc[0] & R.BCM_COMMIT and 1, ddr_wait]
    words += [int(cmd_db['CN0']['addr'], 16)] + [0] * 5
    words += [cnoc[0]] + [0] * 5 + [cnoc[1]] + [0] * 5
    words += addrs + [0] * (8 - len(addrs))
    for n in range(16):
        row = rows[n] if n < len(rows) else []
        words += row + [0] * (8 - len(row))
    return words, bws


def hfi_table_image():
    """a6xx_hfi_init: table header, then 3 queue headers; queue data at +4 KiB each."""
    table = struct.pack('<6I', 0, 24 + 3 * 48, 6, 12, 3, 3)
    headers = b''
    for index, ident in enumerate(R.HFI_QUEUE_IDS):
        iova = R.GMU_HFI_VA + 4096 * (index + 1)
        headers += struct.pack('<12I', 1, iova, 10 << 8 | ident, R.HFI_QUEUE_DWORDS, 0, 0, 1, 1, 1, 0, 0, 0)
    return table + headers


HFI_HDR = {'status': 0, 'iova': 1, 'type': 2, 'size': 3, 'read_index': 10, 'write_index': 11}


def hfi_header_offset(queue, field):
    return region('gmu_hfi')[0] + 24 + 48 * queue + 4 * HFI_HDR[field]


def hfi_data_offset(queue, index):
    return region('gmu_hfi')[0] + 4096 * (queue + 1) + 4 * index


def hfi_header(seq, msg_id, dwords):
    return (seq & 0xFFF) << 20 | 0 << 16 | (dwords & 0xFF) << 8 | msg_id


# --- common prologue ----------------------------------------------------------------------------------
def prologue(s, out, need=('cx_gmu', 'cx_smmu')):
    """S0, S2, S3, S4 as Stage 1 reads them, then the Stage 3 gates. Stage 1's cx-on must have run."""
    S1.step_cmd_db(s, out)
    S1.step_gcc(s, out)
    values = S1.step_gpucc(s, out)
    S1.step_freq_limit(s, out)
    gdsc = out['gpucc']['cx_gdsc']
    gemnoc = s.rd('GCC_GPU_GEMNOC_GFX_CBCR')
    S1.evaluate_gates(s, gdsc, values, gemnoc)
    prove_cx_gmu(s, gdsc, values, gemnoc)
    missing = [gate for gate in need if gate not in s.proven]
    if missing:
        raise Stop('S3', f'Stage 1 gates {missing} are not proven from reads: run `a16gpu.py cx-on --write` first',
                   proven=sorted(s.proven))
    out['order'].append('stage1_gates')
    return values


def prove_cx_gmu(s, gdsc, values, gemnoc):
    """ACCEPTED DEPARTURE (as Stage 1's cx_smmu): GPU_CC_AHB_CBCR's enable does not latch on the A16,
    so cx_gmu is proven from the clean CX_MISC read under the strict guard with every other GMU clock
    running, the CX GDSC on and not under HW control."""
    if 'cx_gmu' in s.proven:
        return
    ok = lambda name: S1.running(R.REGS[name], gemnoc if name.startswith('GCC_') else values[name])
    others = [n for n in S1.GMU_CLOCKS if n != 'GPU_CC_AHB_CBCR']
    if 'FREQ_LIMIT' in str(s.proven.get('cx_misc', '')) and gdsc['on'] and not gdsc['hw_control'] \
            and all(ok(n) for n in others):
        s.prove('cx_gmu', 'GPU AHB path shown by a clean CX_MISC read (AHB enable does not latch on the A16); '
                          f'CX GDSC on, HW_CONTROL clear; running: {", ".join(others)}')


def bootinfo_gpu(s, out):
    info = s.p.bootinfo()
    s.gpu_call, s.gpu_record = info.get('gpu_call', 0), info.get('gpu_state', 0)
    out['bootinfo_gpu'] = {k: hx(info.get(k, 0)) for k in ('gpu_call', 'gpu_state', 'gpu_arena_base',
                                                           'gpu_arena_size', 'gpu_arena_flags')}
    if not s.gpu_call or not s.gpu_record:
        raise S1.GuardMissing('A0', 'the running stage has no gpu_call/gpu_state (bootinfo): chainload a stage '
                                    'built with platform/uefi/gpu-el2.c')
    return info


def read_gpu_state(s):
    blob = s.payload_read(s.gpu_record, 8 * len(GPU_STATE_FIELDS), 'gpu_state')
    state = gpu_state_decode(blob)
    if state['magic'] != GPU_MAGIC or state['size'] != 8 * len(GPU_STATE_FIELDS):
        raise Stop('A0', f'gpu_state record magic {state["magic"]:#x} size {state["size"]} is not ours')
    if state['version'] < GPU_VERSION:
        raise Stop('A0', f'gpu_state version {state["version"]}: the running stage has no GPU_STORE32 (the HFI '
                         f'queue indices need it); chainload a stage built with gpu-el2.c version {GPU_VERSION}')
    return state


def arena_prove(s, out, claim=False, limit=1 << 32):
    """A0: the arena is published, big enough, and (from the EFI map read in S0) not a region the
    firmware kept. Returns the host record."""
    s.step = 'A0'
    info = bootinfo_gpu(s, out)
    state = read_gpu_state(s)
    if not state['arena_base'] and claim:
        status = s.call_gpu(GPU_CLAIM, R.ARENA_SIZE, limit, 0, what='claim arena')
        state = read_gpu_state(s)
        if status:
            raise Stop('A0', f'arena claim failed: {GPU_STATUS.get(status, status)}', gpu_state=_json(state))
    out['gpu_state'] = _json(state)
    base, size = state['arena_base'], state['arena_size']
    if not base:
        raise Stop('A0', 'no GPU arena: run `a16gpu.py arena --claim --write` (or boot an ESP payload that reserves one)')
    if size < R.ARENA_SIZE or base % 4096:
        raise Stop('A0', f'arena {base:#x}+{size:#x} is smaller than {R.ARENA_SIZE:#x} or unaligned')
    regions = out.get('_efi_regions') or []
    hits = [r for r in regions if r['base'] < base + size and base < r['base'] + r['size']]
    kinds = {r['type'] for r in hits}
    covered = sum(min(base + size, r['base'] + r['size']) - max(base, r['base']) for r in hits)
    out['arena'] = {'base': hx(base), 'size': hx(size), 'source': ARENA_SOURCES.get(state['arena_source']),
                    'efi_types': sorted(map(str, kinds))}
    if kinds - {'Conventional', 'LoaderData'} or covered < size:
        raise Stop('A0', f'the arena is not wholly RAM the EFI map gave up (types {sorted(map(str, kinds))}, '
                         f'{covered:#x} of {size:#x} bytes described): refusing to DMA into it')
    # REVIEW: LoaderData is also q1n1's own image, heap and DMA pool. A claimed arena must be
    # Conventional only; any arena must stay clear of every region bootinfo says q1n1 owns.
    if state['arena_source'] == 2 and kinds != {'Conventional'}:
        raise Stop('A0', f'a claimed arena must be Conventional memory only, found {sorted(map(str, kinds))}')
    owned = [(k, info.get(f'{k}_base', 0), info.get(f'{k}_size', 0)) for k in ('image', 'heap', 'dma', 'stage', 'fb')]
    owned += [('memory_map', info.get('memory_map', 0), info.get('memory_map_size', 0)),
              ('preload', info.get('preload_table', 0), info.get('preload_size', 0))]
    clash = [k for k, b, n in owned if b and n and b < base + size and base < b + n]
    if clash:
        raise Stop('A0', f'the arena {base:#x}+{size:#x} overlaps q1n1\'s own {clash}: refusing to DMA into it')
    s.arena = (base, size)
    host = read_host(s)
    if host['magic'] != HOST_MAGIC or host['arena_base'] != base:
        host = host_state_blank(base)
    out['order'].append('arena')
    return host


def read_host(s):
    blob = s.mem_read(region('state')[0], 8 * len(HOST_FIELDS), 'host record')
    return dict(zip(HOST_FIELDS, struct.unpack(f'<{len(HOST_FIELDS)}Q', blob)))


def write_host(s, host):
    s.mem_write(region('state')[0], host_state_pack(host), 'host record')


def _json(values):
    return {k: (hx(v) if isinstance(v, int) and not isinstance(v, bool) and k not in ('claim_status', 'smc_status')
                else v) for k, v in values.items()}


# --- SMMU ----------------------------------------------------------------------------------------------------
def smmu_read_cb(s, cb):
    names = ('SMMU_CB_SCTLR', 'SMMU_CB_TCR', 'SMMU_CB_TCR2', 'SMMU_CB_TTBR0', 'SMMU_CB_TTBR0_HI', 'SMMU_CB_MAIR0',
             'SMMU_CB_ACTLR', 'SMMU_CB_FSR')
    values = {n: s.rd(n, cb) for n in names}
    values['SMMU_CBAR'] = s.rd('SMMU_CBAR', cb)
    values['SMMU_CBA2R'] = s.rd('SMMU_CBA2R', cb)
    return values


def smmu_cb_expected(tables, cb, wide=False):
    """What smmu_program writes; `wide` (cp-fence/adopt --va-wide) gives CB0, and only CB0, T0SZ 30."""
    ttbr = ttbr_for(tables, cb)
    tcr = R.CB_TCR_WIDE if wide and cb == R.GPU_CB else R.CB_TCR
    return {'SMMU_CB_SCTLR': R.CB_SCTLR, 'SMMU_CB_TCR': tcr, 'SMMU_CB_TCR2': R.CB_TCR2,
            'SMMU_CB_TTBR0': ttbr & 0xFFFFFFFF, 'SMMU_CB_TTBR0_HI': ttbr >> 32, 'SMMU_CB_MAIR0': R.CB_MAIR0,
            'SMMU_CB_ACTLR': R.CB_ACTLR, 'SMMU_CBAR': R.CBAR_S1_BYPASS_S2, 'SMMU_CBA2R': R.CBA2R_VA64}


def cb_diff(current, expect):
    """Registers whose read-back differs from what we write, outside the bits the hardware fixes."""
    return {k: (hx(current[k]), hx(v)) for k, v in expect.items()
            if (current[k] ^ v) & ~R.CB_READBACK_FIXED.get(k, 0)}


def smmu_streams(s, groups):
    return [(n, s.rd('SMMU_SMR', n), s.rd('SMMU_S2CR', n)) for n in range(groups)]


def smmu_globals(s, out):
    scr0, idr0, idr1 = s.rd('SMMU_sCR0'), s.rd('SMMU_IDR0'), s.rd('SMMU_IDR1')
    decoded = S1.decode_smmu(idr0, idr1, s.rd('SMMU_IDR7'))
    out['smmu'] = {'sCR0': hx(scr0), 'IDR1': hx(idr1), 'decoded': decoded}
    if scr0 & R.SCR0_CLIENTPD:
        raise Stop(s.step, 'sCR0.CLIENTPD is set: the SMMU bypasses every stream; refusing to build on that')
    if decoded['context_banks'] != R.SMMU_NUM_CB or decoded['page_size'] != 0x1000 or (idr1 >> 28 & 7) != 4:
        raise Stop(s.step, f'SMMU geometry {decoded} / IDR1 {idr1:#x} differs from the A16 (25 CBs, 4 KiB, '
                           'NUMPAGENDXB 4): the context-bank addresses would be wrong')
    return scr0, decoded['stream_match_groups']


def smmu_tlbi(s, cb):
    s.wr('SMMU_CB_TLBIASID', cb, cb)
    s.wr('SMMU_CB_TLBSYNC', 0, cb)
    s.poll3('SMMU_CB_TLBSTATUS', 1, 0, 'tlb', index=cb)


def smmu_program(s, out, tables, cb, smr_index, sid, label, wide=False):
    """arm_smmu_init_domain_context + arm_smmu_write_context_bank + arm_smmu_write_sme, for one stream."""
    expect = smmu_cb_expected(tables, cb, wide)
    current = smmu_read_cb(s, cb)
    smr, s2cr = s.rd('SMMU_SMR', smr_index), s.rd('SMMU_S2CR', smr_index)
    want_smr, want_s2cr = R.SMR_VALID | sid, R.S2CR_TYPE_TRANS << 16 | cb
    ours = not cb_diff(current, expect)
    if ours and smr == want_smr and s2cr == want_s2cr:
        out[label] = {'programmed': 'already (read back identical)'}
        return False
    if current['SMMU_CB_SCTLR'] & R.SCTLR_M and not ours:
        raise Stop(s.step, f'context bank {cb} is enabled with a configuration that is not ours: {_json(current)}')
    if smr & R.SMR_VALID and smr != want_smr:
        raise Stop(s.step, f'SMR{smr_index} {smr:#010x} is in use by someone else')
    if smr & R.SMR_VALID:
        # REVIEW: the stream is live on this bank. SCTLR.M = 0 below would make an SMMUv2 S1 bank
        # bypass translation (input address used as the physical address), so the stream is
        # taken off first, as arm_smmu_write_sme's inverse: S2CR fault, SMR invalid, TLB sync.
        s.wr('SMMU_S2CR', R.S2CR_TYPE_FAULT << 16, smr_index)
        s.wr('SMMU_SMR', 0, smr_index)
        smmu_tlbi(s, cb)
    # init_context (qcom client_match ACTLR) before the bank is written, then write_context_bank order.
    s.wr('SMMU_CB_SCTLR', 0, cb)
    s.wr('SMMU_CB_ACTLR', R.CB_ACTLR, cb)
    s.wr('SMMU_CBA2R', R.CBA2R_VA64, cb)
    s.wr('SMMU_CBAR', R.CBAR_S1_BYPASS_S2, cb)
    s.wr('SMMU_CB_TCR2', R.CB_TCR2, cb)
    s.wr('SMMU_CB_TCR', expect['SMMU_CB_TCR'], cb)
    s.wr('SMMU_CB_TTBR0', expect['SMMU_CB_TTBR0'], cb)
    s.wr('SMMU_CB_TTBR0_HI', expect['SMMU_CB_TTBR0_HI'], cb)
    s.wr('SMMU_CB_TTBR1', 0, cb)
    s.wr('SMMU_CB_TTBR1_HI', cb << 16, cb)
    s.wr('SMMU_CB_MAIR0', R.CB_MAIR0, cb)
    s.wr('SMMU_CB_MAIR1', 0, cb)
    s.wr('SMMU_CB_FSR', 0xFFFFFFFF, cb)          # clear stale faults (write-1-to-clear)
    s.wr('SMMU_CB_SCTLR', R.CB_SCTLR, cb)
    smmu_tlbi(s, cb)
    # Read the bank back before routing the stream to it (the stream still faults here).
    readback = smmu_read_cb(s, cb)
    diff = cb_diff(readback, expect)
    if diff:
        raise Stop(s.step, f'context bank {cb} did not read back as written (read/want {diff}); '
                           'the stream is not routed to it')
    fixed = {k: hx(readback[k] ^ v) for k, v in expect.items() if readback[k] != v}
    # arm_smmu_write_sme: S2CR before SMR, so a matching SMR never points at a stale S2CR.
    s.wr('SMMU_S2CR', want_s2cr, smr_index)
    s.wr('SMMU_SMR', want_smr, smr_index)
    out[label] = {'programmed': True, 'ttbr0': hx(ttbr_for(tables, cb)), 'smr': smr_index, 'sid': sid,
                  'readback_fixed_bits': fixed}
    return True


def sids_reaching_memory(s, scr0, groups, sids):
    """Streams among sids the SMMU would let through: a matching entry that is not FAULT, or no
    matching entry at all while sCR0.USFCFG is clear (unmatched streams then bypass)."""
    streams = smmu_streams(s, groups)
    live = []
    for sid in sids:
        hits = [(n, smr, s2cr) for n, smr, s2cr in streams if smr_matches(smr, sid)]
        live += [(sid, n, hx(smr), hx(s2cr)) for n, smr, s2cr in hits if s2cr >> 16 & 3 != R.S2CR_TYPE_FAULT]
        if not hits and not scr0 & R.SCR0_USFCFG:
            live.append((sid, None, 'unmatched', 'USFCFG clear: bypass'))
    return live


def smmu_disable(s, cb, smr_index, sid):
    """Undo: the stream faults (S2CR type FAULT), the SMR is invalid, the bank is off."""
    smr = s.rd('SMMU_SMR', smr_index)
    if smr & R.SMR_VALID and smr & 0x7FFF != sid:
        raise Stop(s.step, f'SMR{smr_index} {smr:#010x} is not ours (SID {sid}); leaving it')
    s.wr('SMMU_S2CR', R.S2CR_TYPE_FAULT << 16, smr_index)
    s.wr('SMMU_SMR', 0, smr_index)
    s.wr('SMMU_CB_SCTLR', 0, cb)
    smmu_tlbi(s, cb)


def smmu_route(s, smr_index, sid, cb):
    """A second stream on an already programmed bank (arm_smmu_write_sme: S2CR, then SMR)."""
    want_smr, want_s2cr = R.SMR_VALID | sid, R.S2CR_TYPE_TRANS << 16 | cb
    smr, s2cr = s.rd('SMMU_SMR', smr_index), s.rd('SMMU_S2CR', smr_index)
    if smr == want_smr and s2cr == want_s2cr:
        return False
    if smr & R.SMR_VALID and smr != want_smr:
        raise Stop(s.step, f'SMR{smr_index} {smr:#010x} is in use by someone else')
    s.wr('SMMU_S2CR', want_s2cr, smr_index)
    s.wr('SMMU_SMR', want_smr, smr_index)
    return True


def smmu_unroute(s, smr_index, sid):
    """Take one stream off its bank (S2CR fault, SMR invalid) without touching the bank."""
    smr = s.rd('SMMU_SMR', smr_index)
    if not smr & R.SMR_VALID:
        return False
    if smr & 0x7FFF != sid:
        raise Stop(s.step, f'SMR{smr_index} {smr:#010x} is not ours (SID {sid}); leaving it')
    s.wr('SMMU_S2CR', R.S2CR_TYPE_FAULT << 16, smr_index)
    s.wr('SMMU_SMR', 0, smr_index)
    return True


def smmu_verify(s, out, groups, expect_sids):
    """Stream table as the hardware will apply it: expect_sids {sid: cb}; MUST_FAULT_SIDS unmatched."""
    streams = smmu_streams(s, groups)
    table = {}
    for sid in sorted(set(range(8)) | set(expect_sids)):
        hits = [(n, smr, s2cr) for n, smr, s2cr in streams if smr_matches(smr, sid)]
        table[sid] = [{'smr': n, 'type': s2cr >> 16 & 3, 'cb': s2cr & 0xFF} for n, _smr, s2cr in hits]
    out['stream_table'] = {str(k): v for k, v in table.items()}
    for sid, cb in expect_sids.items():
        hit = table[sid]
        if len(hit) != 1 or hit[0]['type'] != R.S2CR_TYPE_TRANS or hit[0]['cb'] != cb:
            raise Stop(s.step, f'SID {sid} does not translate through exactly CB{cb}: {hit}')
    for sid in R.MUST_FAULT_SIDS:
        if any(h['type'] != R.S2CR_TYPE_FAULT for h in table[sid]):
            raise Stop(s.step, f'SID {sid} must fault but matches {table[sid]}')
    scr0 = s.rd('SMMU_sCR0')
    if not scr0 & R.SCR0_USFCFG:
        raise Stop(s.step, f'sCR0 {scr0:#x}: USFCFG clear, unmatched streams would bypass')


def ensure_usfcfg(s):
    scr0 = s.rd('SMMU_sCR0')
    if not scr0 & R.SCR0_USFCFG:
        s.wr('SMMU_sCR0', scr0 | R.SCR0_USFCFG)
    return scr0


def write_tables(s, tables, what):
    offset, blob = tables.image()
    current = s.mem_read(offset, len(blob), f'{what} tables (compare)')
    if current == blob:
        return False
    s.mem_write(offset, blob, f'{what} tables')
    if s.mem_read(offset, len(blob), f'{what} tables (verify)') != blob:
        raise Stop(s.step, f'{what} page tables did not read back')
    return True


def cb_faults(s, cb):
    fsr = s.rd('SMMU_CB_FSR', cb)
    report = {'fsr': hx(fsr)}
    if fsr & R.CB_FSR_FAULT:
        report.update(far=hx(s.rd('SMMU_CB_FAR_HI', cb) << 32 | s.rd('SMMU_CB_FAR', cb)),
                      fsynr0=hx(s.rd('SMMU_CB_FSYNR0', cb)), cbfrsynra=hx(s.rd('SMMU_CBFRSYNRA', cb)))
    return fsr & R.CB_FSR_FAULT, report


# --- P2: gmu-smmu ---------------------------------------------------------------------------------------------
def cmd_gmu_smmu(s, out, args):
    prologue(s, out)
    host = arena_prove(s, out)
    base = s.arena[0]
    s.step = 'G1'
    _scr0, groups = smmu_globals(s, out)
    gmu_running = gmu_state(s)['live']      # REVIEW: out of reset counts, booted or not
    if args.undo:
        if gmu_running:
            raise Stop('G1', 'the GMU CM3 is out of reset: `quiesce --write` first, then undo the SMMU')
        smmu_disable(s, R.GMU_CB, R.GMU_SMR, R.GMU_SID)
        host['gmu_ttbr'] = 0
        write_host(s, host)
        out['order'].append('gmu_smmu_undone')
        return
    s.step = 'G2'
    tables = build_tables(base, 'gmu')
    if gmu_running:
        # Never rewrite tables under a running GMU; only check that they are the ones it uses.
        offset, blob = tables.image()
        if s.mem_read(offset, len(blob), 'GMU tables (running)') != blob:
            raise Stop('G2', 'the GMU is running on tables that differ from this arena layout')
        out['tables'] = 'unchanged (GMU running)'
    else:
        out['tables'] = 'written' if write_tables(s, tables, 'GMU') else 'already identical'
    out['order'].append('gmu_tables')
    s.step = 'G3'
    if gmu_running:
        if cb_diff(smmu_read_cb(s, R.GMU_CB), smmu_cb_expected(tables, R.GMU_CB)):
            raise Stop('G3', 'the GMU runs on a context bank that is not ours')
    else:
        smmu_program(s, out, tables, R.GMU_CB, R.GMU_SMR, R.GMU_SID, 'gmu_cb')
    ensure_usfcfg(s)
    out['order'].append('gmu_cb')
    s.step = 'G4'
    expect = {R.GMU_SID: R.GMU_CB}
    if host.get('gpu_ttbr'):
        expect.update({sid: R.GPU_CB for sid in R.GPU_SIDS})
    smmu_verify(s, out, groups, expect)
    fault, report = cb_faults(s, R.GMU_CB)
    out['gmu_cb_faults'] = report
    host['gmu_ttbr'] = ttbr_for(tables, R.GMU_CB)
    write_host(s, host)
    out['order'].append('gmu_smmu_verified')
    s.prove('gmu_smmu', f'CB{R.GMU_CB} TTBR0 {hx(host["gmu_ttbr"])}; SID {R.GMU_SID} -> CB{R.GMU_CB}; '
                        f'SIDs {R.MUST_FAULT_SIDS} unmatched; USFCFG set')


def gmu_smmu_proven(s, out):
    """P3+ re-prove P2 from the hardware: CB1 as built for this arena, SID 5 routed to it."""
    tables = build_tables(s.arena[0], 'gmu')
    diff = cb_diff(smmu_read_cb(s, R.GMU_CB), smmu_cb_expected(tables, R.GMU_CB))
    if diff:
        raise Stop(s.step, f'GMU context bank is not set up (diff read/want {diff}): run gmu-smmu first')
    _scr0, groups = smmu_globals(s, out)
    smmu_verify(s, out, groups, {R.GMU_SID: R.GMU_CB})
    s.prove('gmu_smmu', f'CB{R.GMU_CB} read back as built; SID 5 routed; others fault')
    return tables


# --- GMU state ----------------------------------------------------------------------------------------------
def gmu_state(s):
    reset = s.rd('GMU_CM3_SYSRESET')
    init = s.rd('GMU_CM3_FW_INIT_RESULT')
    hfi = s.rd('GMU_HFI_CTRL_STATUS')
    booted = init & R.GMU_INIT_RESULT_MASK == R.GMU_INIT_RESULT_OK
    # live: the CM3 is out of reset, whether or not it booted (a failed or crashed GMU still
    # fetches and DMAs through SID 5). Nothing that rewrites its tables or bank may run then.
    return {'sysreset': reset, 'init_result': init, 'hfi_ctrl_status': hfi, 'booted': booted,
            'running': not reset & 1 and booted, 'live': not reset & 1, 'hfi_ready': bool(hfi & 1)}


# --- RPMh (apps RSC drv-2) -------------------------------------------------------------------------------------
def rsc_layout(s):
    drv_id = s.rd('RSC_DRV_ID')
    config = s.rd('RSC_DRV_PRNT_CHLD_CONFIG')
    major = drv_id >> 16 & 0xFF
    layout = {'suffix': '_V3' if major >= 3 else '_V2', 'major': major, 'minor': drv_id >> 8 & 0xFF,
              'max_tcs': config >> (6 * 2) & 0x3F, 'ncpt': config >> 27 & 0x1F}
    if layout['max_tcs'] < R.RSC_NUM_TCS or not 1 <= layout['ncpt'] <= 16:
        raise Stop(s.step, f'apps RSC drv-2 reports {layout}: not the 8-TCS layout glymur.dtsi describes')
    return layout


def rsc_survey(s, layout):
    sfx, tcs_list = layout['suffix'], []
    for tcs in range(R.RSC_NUM_TCS):
        enable = s.rd('RSC_TCS_CMD_ENABLE' + sfx, tcs)
        entry = {'tcs': tcs, 'status': hx(s.rd('RSC_TCS_STATUS' + sfx, tcs)), 'enable': hx(enable),
                 'control': hx(s.rd('RSC_TCS_CONTROL' + sfx, tcs)), 'cmds': []}
        for cmd in range(layout['ncpt']):
            if enable >> cmd & 1:
                at = tcs * 16 + cmd
                entry['cmds'].append({'cmd': cmd, 'addr': hx(s.rd('RSC_TCS_CMD_ADDR' + sfx, at)),
                                      'data': hx(s.rd('RSC_TCS_CMD_DATA' + sfx, at)),
                                      'msgid': hx(s.rd('RSC_TCS_CMD_MSGID' + sfx, at))})
        tcs_list.append(entry)
    return tcs_list


def rsc_existing(survey, addr, skip=()):
    """The highest vote any TCS not in skip already carries for addr (RPMh keeps one per DRV)."""
    values = [int(c['data'], 16) for t in survey if t['tcs'] not in skip for c in t['cmds']
              if int(c['addr'], 16) == addr]
    return max(values) if values else None


def rsc_send(s, layout, tcs, cmds):
    """rpmh_rsc_send_data on an idle ACTIVE TCS, then tcs_tx_done, polled instead of the IRQ."""
    sfx = layout['suffix']
    if not int(s.rd('RSC_TCS_STATUS' + sfx, tcs)) or s.rd('RSC_TCS_CMD_ENABLE' + sfx, tcs):
        raise Stop(s.step, f'active TCS {tcs} is busy (STATUS 0 or commands enabled): someone else owns it')
    enable = 0
    for j, (addr, data) in enumerate(cmds):
        at = tcs * 16 + j
        s.wr('RSC_TCS_CMD_MSGID' + sfx, R.CMD_MSGID_LEN | R.CMD_MSGID_WRITE | R.CMD_MSGID_RESP_REQ, at)
        s.wr('RSC_TCS_CMD_ADDR' + sfx, addr, at)
        s.wr('RSC_TCS_CMD_DATA' + sfx, data, at)
        enable |= 1 << j
    s.wr('RSC_TCS_CMD_ENABLE' + sfx, enable, tcs)
    s.wr_update('RSC_IRQ_ENABLE', 1 << tcs, 1 << tcs)
    control = s.rd('RSC_TCS_CONTROL' + sfx, tcs) & ~(R.TCS_AMC_MODE_TRIGGER | R.TCS_AMC_MODE_ENABLE)
    s.wr('RSC_TCS_CONTROL' + sfx, control, tcs)
    s.wr('RSC_TCS_CONTROL' + sfx, control | R.TCS_AMC_MODE_ENABLE, tcs)
    s.wr('RSC_TCS_CONTROL' + sfx, control | R.TCS_AMC_MODE_ENABLE | R.TCS_AMC_MODE_TRIGGER, tcs)
    try:
        # Every command asked for a response (RESP_REQ), so each one's COMPL is the completion;
        # IRQ_STATUS (what Linux waits on) is recorded, not relied on.
        for j in range(len(cmds)):
            s.poll3('RSC_TCS_CMD_STATUS' + sfx, R.CMD_STATUS_COMPL, R.CMD_STATUS_COMPL, 'rsc', index=tcs * 16 + j)
        s.rsc_irq = s.rd('RSC_IRQ_STATUS')
    finally:
        # tcs_tx_done: trigger off, commands off, IRQ acknowledged -- also after a timeout, so the TCS
        # is not left armed (only on a poll timeout; an SError stops without another access).
        s.wr('RSC_TCS_CONTROL' + sfx, control, tcs)
        s.wr('RSC_TCS_CMD_ENABLE' + sfx, 0, tcs)
        s.wr('RSC_IRQ_CLEAR', 1 << tcs)


def step_rpmh(s, out, host, args):
    """B1: host votes MX and CX to at least LOW_SVS (gpucc required-opps), never lower than a vote the
    RSC already holds for them. The optional DDR (BCM) vote is off unless --host-bw (INFERRED)."""
    s.step = 'B1'
    cmd_db = out['cmd_db'].get('gpu') or {}
    if not all(cmd_db.get(k) for k in ('cx.lvl', 'mx.lvl')):
        raise Stop('B1', 'cmd-db cx.lvl/mx.lvl were not read in S0: cannot build RPMh votes')
    layout = rsc_layout(s)
    survey = rsc_survey(s, layout)
    result = {'layout': layout, 'survey': survey, 'sent': []}
    cmds, unseen = [], []
    for key, field in (('mx.lvl', 'rsc_mx'), ('cx.lvl', 'rsc_cx')):
        addr = int(cmd_db[key]['addr'], 16)
        floor = R.RPMH_LEVELS[getattr(args, 'rail_level', 'low_svs')]
        want = next((j for j, v in enumerate(aux_levels(cmd_db[key])) if v >= floor), None)
        # REVIEW-2: only a wake TCS (rpmh.c caches every active vote as the wake value) or an
        # active TCS still enabled shows drv-2's live level. A sleep-TCS vote is the level drv-2
        # asks for while the apps subsystem sleeps -- usually lower -- and is no evidence of it.
        existing = rsc_existing(survey, addr, skip=R.RSC_SLEEP_TCS)
        anywhere = rsc_existing(survey, addr)
        vote = max(want, existing or 0, anywhere or 0)
        if host.get(field):
            vote = max(vote, host[field] - 1)   # never lower a vote this tool already sent on this arena
        result[key] = {'addr': hx(addr), 'floor_level': floor, 'floor_index': want, 'existing': existing, 'vote': vote,
                       'sleep_only': existing is None and anywhere is not None}
        if host.get(field) == vote + 1:
            continue                            # this tool already sent exactly this vote on this arena
        if existing is None:
            # REVIEW: a vote from this DRV replaces drv-2's current active vote, and that vote is
            # invisible here: rpmh-rsc's tcs_tx_done clears an active TCS's CMD_ENABLE once it
            # completes (so does rsc_send). Only a wake TCS shows what firmware asked for.
            # Without one, LOW_SVS could lower a rail that USB (the proxy link) or the display
            # needs. The operator decides, from rsc-survey, with --rail-vote-unseen.
            unseen.append(key)
        cmds.append((addr, vote))
        host[field] = vote + 1
    if unseen and not getattr(args, 'rail_vote_unseen', False):
        raise Stop('B1', f'no wake TCS shows drv-2\'s current vote for {unseen}: a LOW_SVS vote could lower '
                         'it. Nothing was sent. Pass --rail-vote-unseen (with --rail-level nom to vote a level '
                         'firmware\'s USB/display votes do not exceed) to vote anyway (see rsc-survey)',
                   rpmh=result)
    if args.host_bw:
        bw = opp_levels(out.get('speed_bin', 1), 1)[0][2]
        for (name, *_), data in zip(R.GPU_BCMS, bw_votes(cmd_db, bw)):
            addr = int(cmd_db[name]['addr'], 16)
            existing = rsc_existing(survey, addr)
            if existing is not None and (existing & 0x0FFFFFFF) >= (data & 0x0FFFFFFF):
                continue
            cmds.append((addr, data))
    if cmds:
        rsc_send(s, layout, R.RSC_ACTIVE_TCS[0], cmds)
        result['sent'] = [{'addr': hx(a), 'data': hx(d)} for a, d in cmds]
        result['irq_status'] = hx(getattr(s, 'rsc_irq', 0))
    out['rpmh'] = result
    out['order'].append('rpmh')


# --- B2: PLL0 and the GMU/HUB RCGs --------------------------------------------------------------------------
def pll0_enable(s, out):
    """clk_lucid_evo_pll_configure (unless enabled), prepare (calibrate when PCAL_NOT_DONE: enable +
    disable), then alpha_pll_lucid_evo_enable."""
    mode, opmode, user = s.rd('GPU_CC_PLL0_MODE'), s.rd('GPU_CC_PLL0_OPMODE'), s.rd('GPU_CC_PLL0_USER_CTL')
    report = {'mode_before': hx(mode), 'opmode_before': hx(opmode), 'user_ctl_before': hx(user)}
    if user & R.PLL_USER_VOTE_RUN:
        raise Stop('B2', 'PLL0 is in FSM (vote) mode: someone else manages it')
    if opmode & R.PLL_OPMODE_RUN and mode & R.PLL_MODE_OUTCTRL:
        l_val, alpha = s.rd('GPU_CC_PLL0_L_VAL'), s.rd('GPU_CC_PLL0_ALPHA_VAL')
        if l_val & 0xFFFF != R.PLL0_L_VAL & 0xFFFF or alpha != R.PLL0_ALPHA or not mode & R.PLL_MODE_LOCK_DET:
            raise Stop('B2', f'PLL0 already runs at L {l_val:#x} alpha {alpha:#x} (mode {mode:#x}), not 1150 MHz')
        report['state'] = 'already running at 1150 MHz'
        out['pll0'] = report
        return
    for name, value in (('GPU_CC_PLL0_L_VAL', R.PLL0_L_VAL), ('GPU_CC_PLL0_ALPHA_VAL', R.PLL0_ALPHA),
                        ('GPU_CC_PLL0_CONFIG_CTL', R.PLL0_CONFIG_CTL), ('GPU_CC_PLL0_CONFIG_CTL_U', R.PLL0_CONFIG_CTL_U),
                        ('GPU_CC_PLL0_CONFIG_CTL_U1', R.PLL0_CONFIG_CTL_U1), ('GPU_CC_PLL0_USER_CTL', R.PLL0_USER_CTL),
                        ('GPU_CC_PLL0_USER_CTL_U', R.PLL0_USER_CTL_U)):
        s.wr(name, value)
    s.wr_update('GPU_CC_PLL0_MODE', R.PLL_MODE_OUTCTRL, 0)
    s.wr('GPU_CC_PLL0_OPMODE', R.PLL_OPMODE_STANDBY)
    s.wr_update('GPU_CC_PLL0_MODE', R.PLL_MODE_RESET_N, R.PLL_MODE_RESET_N)
    if s.rd('GPU_CC_PLL0_MODE') & R.PLL_MODE_PCAL_NOT_DONE:
        _pll_on(s)
        _pll_off(s)
        report['calibrated'] = True
    _pll_on(s)
    mode = s.rd('GPU_CC_PLL0_MODE')
    if not mode & R.PLL_MODE_LOCK_DET or not mode & R.PLL_MODE_OUTCTRL:
        raise Stop('B2', f'PLL0 MODE {mode:#x} after enable: not locked with outputs on')
    report['mode_after'] = hx(mode)
    report['state'] = 'enabled at 1150 MHz (PLL0_OUT_EVEN 575 MHz)'
    out['pll0'] = report


def _pll_on(s):
    s.wr_update('GPU_CC_PLL0_MODE', R.PLL_MODE_RESET_N, R.PLL_MODE_RESET_N)
    s.wr('GPU_CC_PLL0_OPMODE', R.PLL_OPMODE_RUN)
    try:
        s.poll3('GPU_CC_PLL0_MODE', R.PLL_MODE_LOCK_DET, R.PLL_MODE_LOCK_DET, 'pll')
    except PollTimeout:
        s.wr('GPU_CC_PLL0_OPMODE', R.PLL_OPMODE_STANDBY)     # never leave a PLL running unlocked
        raise
    s.wr_update('GPU_CC_PLL0_USER_CTL', R.PLL_USER_OUT_MASK, R.PLL_USER_OUT_MASK)
    s.wr_update('GPU_CC_PLL0_MODE', R.PLL_MODE_OUTCTRL, R.PLL_MODE_OUTCTRL)


def _pll_off(s):
    s.wr_update('GPU_CC_PLL0_MODE', R.PLL_MODE_OUTCTRL, 0)
    s.wr_update('GPU_CC_PLL0_USER_CTL', R.PLL_USER_OUT_MASK, 0)
    s.wr('GPU_CC_PLL0_OPMODE', R.PLL_OPMODE_STANDBY)


def rcg_switch(s, cmd, cfg_name, value):
    """clk_rcg2_shared_force_enable_clear: force the root on, CFG, UPDATE, release (also on failure)."""
    cfg = s.rd(cfg_name)
    if cfg & R.RCG_CFG_SRC_MASK == value:
        return f'already {hx(value)}'
    s.wr_update(cmd, R.RCG_CMD_ROOT_EN, R.RCG_CMD_ROOT_EN)
    try:
        s.poll3(cmd, R.RCG_CMD_ROOT_OFF, 0, 'rcg')
        s.wr(cfg_name, (cfg & ~(R.RCG_CFG_SRC_MASK | 1 << 20)) | value)
        s.wr_update(cmd, R.RCG_CMD_UPDATE, R.RCG_CMD_UPDATE)
        s.poll3(cmd, R.RCG_CMD_UPDATE, 0, 'rcg')
    except PollTimeout as timeout:
        try:
            s.wr_update(cmd, R.RCG_CMD_ROOT_EN, 0)
            timeout.detail['root_en_released'] = True
        except Stop as release:
            timeout.detail['root_en_released'] = f'failed: {release.reason}'
        raise
    s.wr_update(cmd, R.RCG_CMD_ROOT_EN, 0)
    return f'switched from {hx(cfg)} to {hx(value)}'


def step_clocks(s, out):
    s.step = 'B2'
    for name in ('GCC_GPU_GPLL0_CLK_SRC_CBCR', 'GCC_GPU_GPLL0_DIV_CLK_SRC_CBCR'):
        value = s.rd(name)
        if not value & R.CBCR_HW_CTL and value & R.CBCR_CLK_OFF:
            raise Stop('B2', f'{name} = {value:#x}: GPLL0 is not reaching GPUCC (cx-on P1 votes it)')
    pll0_enable(s, out)
    out['rcg'] = {'gmu': rcg_switch(s, 'GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CFG_RCGR', R.GMU_RCG_CFG_575),
                  'hub': rcg_switch(s, 'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR', R.HUB_RCG_CFG_200)}
    for name in ('GPU_CC_CX_GMU_CBCR', 'GPU_CC_HUB_CX_INT_CBCR', 'GPU_CC_RSCC_HUB_AON_CBCR'):
        value = s.rd(name)
        if value & R.CBCR_CLK_OFF:
            raise Stop('B2', f'{name} = {value:#x} stopped after the RCG switch')
    out['order'].append('clocks')


# --- B3: TrustZone ----------------------------------------------------------------------------------------------
def step_tz(s, out, host, force=False):
    s.step = 'B3'
    state = read_gpu_state(s)
    pending = host.get('tz_ok') == TZ_PENDING or Q.signed64(state['smc_status']) in TZ_LEFT_PENDING
    if pending and not force:
        # Linux resumes an INTERRUPTED call until it finishes; a call given up (EINTR) or asked to
        # sleep (WAITQ) is still open in TrustZone. A fresh call on top of it is not something Linux
        # ever does: stop until a reboot (the ESP zeroes the arena, so the flag clears with it).
        raise Stop('B3', 'the last SMC 0x42002801 was left pending in TrustZone (interrupted or WAITQ): '
                         'reboot the A16 before another gmu-boot (--force-smc overrides)',
                   gpu_state=_json(state))
    if (state['smc_ok'] or host.get('tz_ok') == 1) and not force:
        # Once per boot, as GMU_STATUS_SECURE_INIT in Linux; the arena record carries it across a
        # chainload (the new stage's gpu_state starts at zero).
        out['tz'] = {'skipped': f'already done this boot (gpu_state smc_ok={state["smc_ok"]}, '
                                f'arena record tz_ok={host.get("tz_ok")})'}
    else:
        status = s.call_gpu(GPU_SMC, *SMC_GPU_INIT, what='qcom_scm_gpu_init_regs(ALWAYS_EN|TSENSE_EN)')
        state = read_gpu_state(s)
        out['tz'] = {'status': GPU_STATUS.get(status, status), 'x0': hx(state['smc_res0']),
                     'interrupted': state['smc_interrupted'], 'busy': state['smc_busy']}
        if status in TZ_LEFT_PENDING:
            host['tz_ok'] = TZ_PENDING
            write_host(s, host)
        if status:
            raise Stop('B3', f'SMC 0x42002801 not completed: {GPU_STATUS.get(status, status)}', tz=out['tz'])
        if state['smc_res0']:
            raise Stop('B3', f'TrustZone refused SMC 0x42002801: x0 = {Q.signed64(state["smc_res0"])}', tz=out['tz'])
        host['tz_ok'] = 1
        write_host(s, host)
    fuse = s.rd('CX_MISC_SW_FUSE_VALUE')
    out['tz']['sw_fuse_value'] = hx(fuse)
    out['order'].append('tz')


# --- B4..B10: firmware and CM3 start ------------------------------------------------------------------------
def step_fw_load(s, out, fw, host):
    s.step = 'B7'
    plan = plan_gmu_fw(parse_gmu_fw(fw['gmu']))
    if plan['version'] != R.GMU_FW_VERSION:
        raise Stop('B7', f'GMU firmware version word {plan["version"]} is not the pinned {R.GMU_FW_VERSION:#x}')
    # Hold the CM3 in reset while its TCMs are written (Linux's force_off writes the same).
    s.wr('GMU_CM3_SYSRESET', 1)
    if not s.rd('GMU_CM3_SYSRESET') & 1:
        raise Stop('B7', 'CM3_SYSRESET does not read back 1: the TCMs are not written')
    s.prove('gmu_reset', 'GMU_CM3_SYSRESET read back 1')
    for name, reg in (('itcm', 'GMU_CM3_ITCM'), ('dtcm', 'GMU_CM3_DTCM')):
        for word, data in plan[name]:
            for n in range(len(data) // 4):
                s.wr(reg, u32(data, n), word + n)
    # The GMU buffers are zeroed pages in Linux (GEM); the firmware blocks go on top.
    for name in ('gmu_icache', 'gmu_dummy', 'gmu_debug', 'gmu_log'):
        s.mem_fill(region(name)[0], region(name)[1], name)
    for buffer, offset, data in plan['memory']:
        s.mem_write(region('gmu_' + buffer)[0] + offset, data, f'GMU {buffer} +{offset:#x}')
    s.mem_fill(region('gmu_hfi')[0], region('gmu_hfi')[1], 'gmu_hfi')
    s.mem_write(region('gmu_hfi')[0], hfi_table_image(), 'HFI queue table')
    version = s.rd('GMU_CORE_FW_VERSION')
    out['fw'] = {'itcm_words': sum(len(d) // 4 for _w, d in plan['itcm']),
                 'dtcm_words': sum(len(d) // 4 for _w, d in plan['dtcm']),
                 'memory_blocks': [(b, hx(o), len(d)) for b, o, d in plan['memory']],
                 'prealloc': [(hx(a), hx(n)) for a, n in plan['prealloc']], 'version_word': hx(version)}
    if version != R.GMU_FW_VERSION:
        raise Stop('B7', f'CORE_FW_VERSION reads {version:#x} after the DTCM load, not {R.GMU_FW_VERSION:#x}')
    out['order'].append('fw_load')


def step_gmu_regs(s, out, host):
    s.step = 'B8'
    log_iova = R.GMU_LOG_VA
    writes = (
        ('GMU_CM3_FW_INIT_RESULT', 0), ('GMU_CM3_BOOT_CONFIG', R.GMU_BOOT_CONFIG_VALUE),
        ('GMU_HFI_QTBL_ADDR', R.GMU_HFI_VA), ('GMU_HFI_QTBL_INFO', 1),
        ('GMU_AHB_FENCE_RANGE_0', R.GMU_FENCE_RANGE_0), ('GMU_CM3_CFG', R.GMU_CM3_CFG_VALUE),
        ('GMU_GENERAL_10', R.GMU_CHIPID_A8),
        ('GMU_GENERAL_8', (log_iova & 0xFFFFF000) | ((R.GMU_LOG_SIZE // 4096 - 1) & 0xFF)),
        ('GBIF_QSB_SIDE0', R.GBIF_QSB_SIDE_VALUE), ('GBIF_QSB_SIDE1', R.GBIF_QSB_SIDE_VALUE),
        ('GBIF_QSB_SIDE2', R.GBIF_QSB_SIDE_VALUE), ('GBIF_QSB_SIDE3', R.GBIF_QSB_SIDE_VALUE),
        ('GBIF_CX_CONFIG', R.GBIF_CX_CONFIG_VALUE),        # a840_gbif entry
        ('GBIF_CX_CONFIG', R.GBIF_CX_CONFIG_VALUE),        # a8xx branch of fw_start writes it again
        ('GMU_MRC_GBIF_QOS_CTRL', R.MRC_GBIF_QOS_VALUE),
        ('GMU_SYS_BUS_CONFIG', 1), ('GMU_ICACHE_CONFIG', 1), ('GMU_DCACHE_CONFIG', 1),
    )
    for name, value in writes:
        s.wr(name, value)
    # KGSL gen8_gmu_fw_start: clear a GBIF halt left by an earlier power-down (GBIF_HALT = BIT(3)),
    # after the GBIF list and before the CM3 starts. Linux a8xx does not; its GBIF_HALT = 0 in
    # hw_init left GBIF_HALT_ACK at 0x3 on the A16 (run 2).
    before = s.rd('GBIF_HALT_ACK')
    s.wr('GBIF_HALT', R.GBIF_HALT_CLEAR_GEN8)
    out['gbif_clear'] = {'ack_before': hx(before), 'halt_after': hx(s.rd('GBIF_HALT')),
                         'ack_after': hx(s.rd('GBIF_HALT_ACK'))}
    out['order'].append('gmu_regs')


def step_gmu_start(s, out, host, timeout):
    s.step = 'B9'
    version = s.rd('GMU_CORE_FW_VERSION')     # a6xx_gmu_start reads DTCM + 0xff8 first
    mask, want = ((R.GMU_INIT_RESULT_MASK, R.GMU_INIT_RESULT_OK) if version > 0x20010004
                  else R.GMU_INIT_RESULT_LEGACY)
    s.wr('GMU_CM3_SYSRESET', 1)
    s.wr('GMU_GENERAL_9', 0)
    s.withdraw('gmu_reset')
    s.wr('GMU_CM3_SYSRESET', 0)
    try:
        value = s.poll3('GMU_CM3_FW_INIT_RESULT', mask, want, 'gmu_init', budget=timeout)
    except PollTimeout as timeout_error:
        _gmu_diagnostics(s, timeout_error.detail)
        raise
    out['gmu_start'] = {'version': hx(version), 'init_result': hx(value)}
    s.wr('GMU_HFI_CTRL_INIT', 1)
    # Linux allows 10 ms; over the proxy one read is about a millisecond, so 100 ms and >= 2 reads.
    s.poll3('GMU_HFI_CTRL_STATUS', 1, 1, 'hfi_start', budget=max(R.HFI_START_TIMEOUT_S, 0.1))
    s.wr('GMU_GMU2HOST_INTR_CLR', 0xFFFFFFFF)
    s.wr('GMU_GMU2HOST_INTR_MASK', ~R.GMU2HOST_CM3_FAULT & 0xFFFFFFFF)
    host.update(boot_token=secrets.randbits(63) | 1, hfi_token=0, hfi_seq=0, oob_token=0, cp_token=0, hwinit_token=0)
    write_host(s, host)
    out['order'].append('gmu_start')


def _gmu_diagnostics(s, detail):
    try:
        detail['ao_host_interrupt_status'] = hx(s.rd('GMU_AO_HOST_INTERRUPT_STATUS'))
        detail['gmu2host_info'] = hx(s.rd('GMU_GMU2HOST_INTR_INFO'))
        detail['cb1'] = cb_faults(s, R.GMU_CB)[1]
    except Stop as nested:
        detail['diagnostics'] = f'failed: {nested.reason}'


def cmd_gmu_boot(s, out, args):
    prologue(s, out)
    host = arena_prove(s, out)
    s.step = 'B0'
    out['speed_bin'] = out.get('freq_limit', {}).get('speed_bin', 1)
    gmu_smmu_proven(s, out)
    state = gmu_state(s)
    out['gmu_before'] = _json(state)
    if state['running'] and state['hfi_ready']:
        if host['boot_token']:
            out['gmu'] = 'already running (INIT_RESULT ok, HFI ready): nothing written'
            out['order'].append('gmu_running')
            return
        raise Stop('B0', 'a GMU is running that this arena did not boot: `quiesce --write` first')
    tcm = s.rd('CX_MISC_TCM_RET_CNTL')
    if tcm:
        raise Stop('B0', f'CX_MISC_TCM_RET_CNTL = {tcm:#x}: the GMU would take the warm-boot path')
    fw = load_firmware(args.fw_dir)
    out['order'].append('gmu_checks')
    for step_id, run in (('B1', lambda: step_rpmh(s, out, host, args)),
                         ('B2', lambda: (step_clocks(s, out), ahb_timeouts(s, out, args))),
                         ('B3', lambda: step_tz(s, out, host, force=args.force_smc)),
                         ('B6', lambda: step_irq_setup(s, out, host)), ('B7', lambda: step_fw_load(s, out, fw, host)),
                         ('B8', lambda: step_gmu_regs(s, out, host)),
                         ('B9', lambda: step_gmu_start(s, out, host, args.init_timeout))):
        run()
        if args.stop_after == step_id:
            out['stopped_after'] = step_id
            write_host(s, host)
            return
    fault, report = cb_faults(s, R.GMU_CB)
    out['gmu_cb_faults'] = report
    if fault:
        raise Stop('B10', f'context bank 1 recorded a fault while the GMU booted: {report}')
    s.prove('gmu_booted', f'INIT_RESULT {out["gmu_start"]["init_result"]}, HFI_CTRL_STATUS bit 0')


def step_irq_setup(s, out, host):
    """a6xx_gmu_resume: AO host interrupts cleared and masked to WDOG|AHB_ERR|FENCE_ERR; slice info;
    then a6xx_gmu_fw_start's FAL veto and RSCC wake (step_rpmh_start)."""
    s.step = 'B6'
    s.wr('GMU_AO_HOST_INTERRUPT_CLR', 0xFFFFFFFF)
    s.wr('GMU_AO_HOST_INTERRUPT_MASK',
         ~(R.AO_HOST_IRQ_WDOG | R.AO_HOST_IRQ_AHB_ERR | R.AO_HOST_IRQ_FENCE_ERR) & 0xFFFFFFFF)
    s.wr('GMU_CX_FALNEXT_INTF', 1)            # "Vote veto for FAL10": a8xx writes FALNEXT then FAL
    s.wr('GMU_CX_FAL_INTF', 1)
    out['slice_mask'] = hx(s.rd('CX_MISC_SLICE_ENABLE_FINAL'))
    out['order'].append('irq_fal')
    step_rpmh_start(s, out, host)


def step_rpmh_start(s, out, host):
    """REVIEW-2: a6xx_rpmh_start, where a6xx_gmu_fw_start calls it (after the FAL veto, before the
    firmware load). Linux wakes the GPU RSC only after its own a6xx_rpmh_stop (GMU_STATUS_PDC_SLEEP);
    quiesce records that stop in the host record. The RSC's own sleep status (RSC_STATUS0_DRV0 bit
    16, what rpmh_stop waits for; INFERRED to stay set while it sleeps) also counts, so a lost record
    cannot start a GMU on a sleeping RSC. On a timeout the request stays set, as in Linux."""
    status0 = s.rd('RSCC_RSC_STATUS0_DRV0')
    report = {'pdc_sleep_recorded': host.get('pdc_sleep', 0), 'status0': hx(status0)}
    out['rscc_start'] = report
    if not host.get('pdc_sleep') and not status0 & 1 << 16:
        report['skipped'] = 'no a6xx_rpmh_stop recorded and RSC_STATUS0 bit 16 clear (as Linux\'s first resume)'
        return
    s.wr('GMU_RSCC_CONTROL_REQ', 1 << 1)
    s.poll3('GMU_RSCC_CONTROL_ACK', 1 << 1, 1 << 1, 'rscc')
    s.poll3('RSCC_SEQ_BUSY_DRV0', 0xFFFFFFFF, 0, 'rscc')
    s.wr('GMU_RSCC_CONTROL_REQ', 0)
    host['pdc_sleep'] = 0
    write_host(s, host)
    report['woken'] = True
    out['order'].append('rscc_start')


# --- P4: HFI --------------------------------------------------------------------------------------------------
def gmu_running_proven(s, out, host):
    gmu_smmu_proven(s, out)
    state = gmu_state(s)
    out['gmu'] = _json(state)
    if not (state['running'] and state['hfi_ready']):
        raise Stop(s.step, f'the GMU is not running with HFI ready ({_json(state)}): run gmu-boot')
    if not host['boot_token']:
        raise Stop(s.step, 'the running GMU was not booted from this arena record: quiesce and gmu-boot again')
    fault, report = cb_faults(s, R.GMU_CB)
    if fault:
        raise Stop(s.step, f'context bank 1 holds a fault: {report}')
    s.prove('gmu_booted', f'INIT_RESULT {hx(state["init_result"])}, HFI ready, boot token {hx(host["boot_token"])}')


def cmd_queue_drained(s):
    """The command queue header once the GMU has consumed everything the host queued (read index
    == write index), polled for a bounded time. This tool keeps one command in flight, so an
    undrained queue means the GMU did not take a command, or a GMU index update was lost."""
    limit, seconds = POLL['hfi_drain']
    deadline = s.clock() + seconds
    header = ()
    for attempt in range(1, limit + 1):
        header = struct.unpack('<12I', s.mem_read(hfi_header_offset(0, 'status'), 48, 'cmd queue header'))
        size, read, write = header[3], header[10], header[11]
        if size != R.HFI_QUEUE_DWORDS or read >= size or write >= size or read % 4 or write % 4:
            raise Stop(s.step, f'HFI command queue header is corrupt: {[hx(w) for w in header]}')
        if read == write:
            s.polls.append({'reg': 'hfi cmd queue drained (DRAM)', 'reads': attempt, 'ok': True})
            return header
        if attempt >= 2 and s.clock() > deadline:
            break
    s.polls.append({'reg': 'hfi cmd queue drained (DRAM)', 'reads': attempt, 'ok': False})
    raise Stop(s.step, f'HFI command queue not drained (read {header[10]}, write {header[11]}): the GMU did not '
                       'consume the last command; quiesce', header=[hx(w) for w in header])


def hfi_send(s, host, msg_id, payload, name):
    """a6xx_hfi_send_msg: queue write (padded with 0xfafafafa), doorbell, MSGQ, ack on the response queue."""
    words_needed = 1 + len(payload)
    header = cmd_queue_drained(s)
    size, read, write = header[3], header[10], header[11]
    space = (read - write - 1) % size
    if space < words_needed:
        raise Stop(s.step, f'HFI command queue has no room ({space} dwords free, header {header})')
    host['hfi_seq'] = (host['hfi_seq'] + 1) % 0xFFF
    seq = host['hfi_seq']
    words = [hfi_header(seq, msg_id, words_needed)] + list(payload)
    index, data = write, []
    for word in words:
        data.append((index, word))
        index = (index + 1) % size
    while index % 4:
        data.append((index, 0xFAFAFAFA))
        index = (index + 1) % size
    runs = []                        # contiguous runs (two when the message wraps)
    for at, word in data:
        if runs and at == runs[-1][0] + len(runs[-1][1]):
            runs[-1][1].append(word)
        else:
            runs.append((at, [word]))
    for at, run in runs:
        s.mem_write(hfi_data_offset(0, at), struct.pack(f'<{len(run)}I', *run), f'HFI {name}')
    s.mem_store32(hfi_header_offset(0, 'write_index'), index, 'cmd queue write_index')
    s.wr('GMU_HOST2GMU_INTR_SET', 1)
    response = hfi_wait_ack(s, seq, msg_id, name)
    write_host(s, host)
    return response


def hfi_wait_ack(s, seq, msg_id, name):
    for _round in range(4):
        s.poll3('GMU_GMU2HOST_INTR_INFO', R.GMU2HOST_MSGQ, R.GMU2HOST_MSGQ, 'msgq', budget=R.HFI_MSG_TIMEOUT_S)
        s.wr('GMU_GMU2HOST_INTR_CLR', R.GMU2HOST_MSGQ)
        # REVIEW: bounded. A write index the 4-dword alignment steps over (or a GMU that keeps
        # posting) would otherwise keep this loop reading the ring forever.
        for _message in range(R.HFI_QUEUE_DWORDS // 4 + 1):
            header = struct.unpack('<12I', s.mem_read(hfi_header_offset(1, 'status'), 48, 'rsp queue header'))
            size, read, write = header[3], header[10], header[11]
            if read == write:
                break
            if size != R.HFI_QUEUE_DWORDS or read >= size or write >= size:
                raise Stop(s.step, f'HFI response queue header is corrupt: {header}')
            page = s.mem_read(hfi_data_offset(1, 0), 4 * size, 'rsp queue data')
            first = u32(page, read)
            dwords = first >> 8 & 0xFF
            if not 1 <= dwords <= 19:
                raise Stop(s.step, f'HFI response header {first:#x} has size {dwords}: corrupt queue')
            words, at = [], read
            for _ in range(dwords):
                words.append(u32(page, at))
                at = (at + 1) % size
            at = ((at + 3) & ~3) % size
            # The GMU also writes this line (cmd read_index): let it finish with the command queue
            # first, then store the one word on the target (GPU_STORE32), never a writemem + clean.
            cmd_queue_drained(s)
            s.mem_store32(hfi_header_offset(1, 'read_index'), at, 'rsp queue read_index')
            ident, kind = words[0] & 0xFF, words[0] >> 16 & 0xF
            if ident == R.HFI_F2H_MSG_ERROR:
                s.log.append({'seq': len(s.log), 'step': s.step, 'op': 'hfi_error', 'words': [hx(w) for w in words]})
                continue
            # a6xx_hfi_wait_for_ack: an ack is matched by the header it returns (seqnum, and here the id
            # too). Legacy firmware sends id 126; 0x50020026 sends the command's id with type ACK.
            is_ack = ident == R.HFI_F2H_MSG_ACK or (kind == R.HFI_MSG_TYPE_ACK and ident == msg_id)
            if not is_ack or len(words) < 3 or words[1] >> 20 & 0xFFF != seq or words[1] & 0xFF != msg_id:
                s.log.append({'seq': len(s.log), 'step': s.step, 'op': 'hfi_skipped', 'words': [hx(w) for w in words]})
                continue
            if words[2]:
                raise Stop(s.step, f'HFI {name} (id {msg_id}) returned error {words[2]}', words=[hx(w) for w in words])
            return {'seq': seq, 'ack': [hx(w) for w in words[:3]], 'payload': [hx(w) for w in words[3:]]}
        else:
            raise Stop(s.step, f'HFI {name}: the response queue did not drain in {R.HFI_QUEUE_DWORDS // 4 + 1} '
                               'messages (read index never met the write index)')
    raise Stop(s.step, f'HFI {name}: no acknowledgement for seq {seq}')


def cmd_hfi(s, out, args):
    prologue(s, out)
    host = arena_prove(s, out)
    s.step = 'H0'
    gmu_running_proven(s, out, host)
    if host['hfi_token'] == host['boot_token'] and not args.resend:
        out['hfi'] = 'already started for this GMU boot (START acknowledged): nothing sent'
        out['order'].append('hfi_done')
        return
    cmd_db = out['cmd_db'].get('gpu') or {}
    if not all(cmd_db.get(k) for k in R.CMD_DB_GPU_KEYS):
        raise Stop('H0', f'cmd-db entries missing for the HFI tables: {[k for k in R.CMD_DB_GPU_KEYS if not cmd_db.get(k)]}')
    speed_bin = out.get('freq_limit', {}).get('speed_bin')
    if speed_bin is None:
        raise Stop('H0', 'no speed bin read in this run')
    # agents/dvfs O1/O2: every OPP in the table is allowed by the fuse and votable on every rail; else nothing is sent.
    safe, refused = rail_safe_opps(cmd_db, speed_bin)
    allowed = opp_levels(speed_bin, len(R.GPU_OPPS))
    if args.max_opps > len(safe):
        if args.max_opps > len(allowed):
            raise Stop('H0', f'--max-opps {args.max_opps}: speed bin {speed_bin} allows {len(allowed)} GPU OPPs')
        opp, why = refused
        raise Stop('H0', f'--max-opps {args.max_opps} reaches {opp[0] // 1000000} MHz (level {opp[1]}): {why}. '
                         f'Refused rather than under-voted; at most --max-opps {len(safe)} '
                         f'({safe[-1][0] // 1000000} MHz). Nothing was sent')
    perf = perf_table(cmd_db, speed_bin, args.max_opps, 1)
    bw, bws = bw_table(cmd_db, speed_bin, args.max_opps)
    vote, vote_name = perf_vote(speed_bin, args.max_opps, args.perf_opp)
    out['hfi_tables'] = {'perf': [hx(w) for w in perf], 'bw': [hx(w) for w in bw[:40]], 'bandwidths_kBps': bws,
                         'gpu_opps_mhz': [hz // 1000000 for hz, *_ in opp_levels(speed_bin, args.max_opps)],
                         'rail_safe_opps': len(safe),
                         'refused': None if refused is None else {'mhz': refused[0][0] // 1000000,
                                                                  'level': refused[0][1], 'why': refused[1]}}
    if vote != [1, 1, 1]:
        out['hfi_tables']['perf_vote'] = vote
    sequence = (('H1', R.HFI_MSG_TABLE, perf, 'TABLE (GPU_PERF)'), ('H2', R.HFI_MSG_BW_TABLE, bw, 'BW_TABLE'),
                ('H3', R.HFI_MSG_CORE_FW_START, [0], 'CORE_FW_START'), ('H4', R.HFI_MSG_START, [], 'START'),
                ('H5', R.HFI_MSG_GX_BW_PERF_VOTE, vote, vote_name))
    out['hfi'] = {}
    for step_id, msg_id, payload, name in sequence:
        s.step = step_id
        out['hfi'][name] = hfi_send(s, host, msg_id, payload, name)
        out['order'].append(f'hfi_{msg_id}')
        if step_id == 'H4':
            host['hfi_token'] = host['boot_token']
            host['hfi_opps'] = len(opp_levels(speed_bin, args.max_opps))   # O4: the table the governor may vote
            write_host(s, host)
        if args.stop_after == step_id:
            out['stopped_after'] = step_id
            return


# --- P4: OOB GPU_SET ----------------------------------------------------------------------------------------
def gx_on_proven(s, what):
    status = s.rd('GMU_PWR_CLK_STATUS')
    if status & R.PWR_CLK_STATUS_GX_OFF:
        s.withdraw('gx')
        return status, False
    s.prove('gx', f'{what}; GMU_PWR_CLK_STATUS {status:#010x} bits 0/1 clear (GX GDSC on, GX clock on)')
    return status, True


def cmd_oob(s, out, args):
    prologue(s, out)
    host = arena_prove(s, out)
    s.step = 'O0'
    gmu_running_proven(s, out, host)
    if host['hfi_token'] != host['boot_token']:
        raise Stop('O0', 'HFI START was not acknowledged for this GMU boot: run hfi first')
    if host['oob_token'] == host['boot_token']:
        status, on = gx_on_proven(s, 'OOB GPU_SET already acknowledged for this boot')
        if on:
            out['oob'] = {'state': 'already set', 'pwr_clk_status': hx(status)}
            out['order'].append('gx_on')
            return
    s.step = 'O1'
    s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_REQUEST)
    try:
        s.poll3('GMU_GMU2HOST_INTR_INFO', 1 << R.OOB_GPU_SET_ACK, 1 << R.OOB_GPU_SET_ACK, 'oob',
                budget=max(R.OOB_TIMEOUT_S * 10, 0.1))
    except PollTimeout:
        s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_CLEAR)     # take the request back
        raise
    s.wr('GMU_GMU2HOST_INTR_CLR', 1 << R.OOB_GPU_SET_ACK)
    s.step = 'O2'
    status, on = gx_on_proven(s, 'OOB GPU_SET acknowledged (GMU2HOST_INTR_INFO bit 31)')
    out['oob'] = {'pwr_clk_status': hx(status), 'gx_on': on}
    if not on:
        s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_CLEAR)
        raise Stop('O2', f'OOB acknowledged but GMU_PWR_CLK_STATUS {status:#x} still reports GX off')
    host['oob_token'] = host['boot_token']
    write_host(s, host)
    out['order'].append('gx_on')


# --- P5: CP ------------------------------------------------------------------------------------------------------
def pipe_write(s, pipe, name, value, index=0):
    s.wr('CP_APERTURE_CNTL_HOST', R.aperture(pipe))
    s.wr(name, value, index)


def aperture_clear(s):
    s.wr('CP_APERTURE_CNTL_HOST', R.aperture(R.PIPE_NONE))


def wr64(s, name, value):
    """gpu_write64: low word at the register, high word at the next one."""
    reg = R.REGS[name]
    if reg.count == 2:
        s.wr(name, value & 0xFFFFFFFF, 0)
        s.wr(name, value >> 32, 1)
    else:
        s.wr(name, value & 0xFFFFFFFF)
        s.wr(name + '_HI', value >> 32)


def ahb_timeouts(s, out, args):
    """KGSL gen8_enable_ahb_timeout_detection: "catch any register access taking longer time before NOC
    timeout gets detected". Each CX AHB port (AON, GMU, CP, VBIF/SMMU, HOST) answers a transfer that has
    not completed in 2^(v+1) HUB cycles with an error, below the GPU config-NoC timeout (3.41 ms on
    gen8). With it a stuck GPU register access reaches the CPU as a bus error, which the q1n1 SError
    guard reports (exit 3), instead of a NoC timeout that TrustZone may treat as fatal. Upstream Linux
    does not program it; KGSL does on every gen8 GMU boot. INFERRED for the X2-90 (same gen8 register
    file); the read-back check stops if the registers do not take the value."""
    if getattr(args, 'no_ahb_timeout', False):
        out['cx_ahb'] = {'skipped': '--no-ahb-timeout',
                         'found': [hx(s.rd('CX_MISC_CX_AHB_CNTL', n)) for n in range(len(R.CX_AHB_PORTS))]}
        return None
    hub_cfg = s.rd('GPU_CC_HUB_CFG_RCGR') & R.RCG_CFG_SRC_MASK
    hub_hz = R.HUB_HZ.get(hub_cfg)
    want = R.ahb_timeout_cntl(hub_hz) if hub_hz else None
    if want is None:
        raise Stop(s.step, f'HUB RCG CFG {hub_cfg:#x} is not a rate the AHB timeout can be sized for '
                           '(gmu-boot B2 sets 0x505 = 200 MHz); pass --no-ahb-timeout to go on without it')
    before = [s.rd('CX_MISC_CX_AHB_CNTL', n) for n in range(len(R.CX_AHB_PORTS))]
    foreign = [hx(v) for v in before if v & ~R.AHB_CNTL_FIELDS]
    if foreign:
        # The layout is KGSL gen8's, INFERRED for the X2-90: a bit KGSL does not document means this is
        # some other register. Read-back after the write would be too late; do not write at all.
        out['cx_ahb'] = {'before': dict(zip(R.CX_AHB_PORTS, map(hx, before))), 'written': False}
        raise Stop(s.step, f'CX AHB timeout registers read {foreign} with bits outside KGSL\'s fields '
                           f'({R.AHB_CNTL_FIELDS:#x}): not the registers KGSL describes, nothing written '
                           '(pass --no-ahb-timeout to go on without them)', cx_ahb=out['cx_ahb'])
    for n, value in enumerate(before):
        if value != want:
            s.wr('CX_MISC_CX_AHB_CNTL', want, n)
    after = [s.rd('CX_MISC_CX_AHB_CNTL', n) for n in range(len(R.CX_AHB_PORTS))]
    out['cx_ahb'] = {'hub_hz': hub_hz, 'value': hx(want), 'timeout_us': round((2 << (want & 0x1F)) * 1e6 / hub_hz),
                     'before': dict(zip(R.CX_AHB_PORTS, map(hx, before))),
                     'after': dict(zip(R.CX_AHB_PORTS, map(hx, after)))}
    if after != [want] * len(after):
        raise Stop(s.step, f'CX AHB timeout registers read back {list(map(hx, after))}, not {hx(want)}: the '
                           'timeout is not armed (pass --no-ahb-timeout to go on without it)', cx_ahb=out['cx_ahb'])
    return want


def cp_aperture(s, out, args, host):
    """adreno_hw_init: qcom_scm_set_gpu_smmu_aperture(0) after the interrupt masks, before the SQE base.
    Linux logs a TrustZone refusal and carries on; so does this. A call the payload could not complete
    stops, and one left open in TrustZone blocks every later SMC until a reboot (as B3)."""
    if getattr(args, 'no_aperture', False):
        out['aperture'] = {'skipped': '--no-aperture'}
        return
    state = read_gpu_state(s)
    if state['version'] < R.GPU_VERSION_APERTURE:
        raise Stop('C3', f'the running stage (gpu_state version {state["version"]}) cannot forward '
                         'qcom_scm_set_gpu_smmu_aperture: chainload a stage built with gpu-el2.c version '
                         f'{R.GPU_VERSION_APERTURE}, or pass --no-aperture')
    if host.get('tz_ok') == TZ_PENDING or Q.signed64(state['smc_status']) in TZ_LEFT_PENDING:
        raise Stop('C3', 'an SMC was left pending in TrustZone this boot: reboot the A16 before another SMC')
    status = s.call_gpu(GPU_SMC, *SMC_CP_APERTURE, what='qcom_scm_set_gpu_smmu_aperture(0)')
    state = read_gpu_state(s)
    out['aperture'] = {'status': GPU_STATUS.get(status, status), 'x0': hx(state['smc_res0']),
                       'interrupted': state['smc_interrupted'], 'busy': state['smc_busy']}
    if status in TZ_LEFT_PENDING:
        host['tz_ok'] = TZ_PENDING
        write_host(s, host)
    if status:
        raise Stop('C3', f'CP aperture SMC not completed: {GPU_STATUS.get(status, status)}', aperture=out['aperture'])
    if state['smc_res0']:
        out['aperture']['note'] = (f'TrustZone answered {Q.signed64(state["smc_res0"])}: Linux logs "unable to set '
                                   'SMMU aperture" and starts the SQE anyway (it only matters for per-process '
                                   'page tables)')


def hw_init(s, out, args=None, host=None):
    """a8xx_gpu.c hw_init up to (not including) the SQE start, in its order, with adreno_hw_init's
    aperture SMC where Linux makes it. Skipped and why: the UBWC block (needs the master ubwc.h
    helpers; rendering only), preemption (one ring: a8xx_preempt_start returns at once), the pwrup
    reglist (CP_ME_INIT BIT(8) clear) and HW clock gating (Linux enables it after CP_ME_INIT)."""
    args = args or Args()
    s.step = 'C3'
    ahb_timeouts(s, out, args)          # KGSL arms these with the GMU clocks; make sure before any GX work
    aperture_clear(s)
    # Linux a8xx writes GBIF_HALT = 0 here; KGSL gen8 never touches it after gen8_gmu_fw_start's BIT(3),
    # so the GBIF runs with BIT(3) set. Follow the vendor driver for this generation.
    s.wr('GBIF_HALT', R.GBIF_HALT_CLEAR_GEN8)
    s.rd('GBIF_HALT')
    s.wr('RBBM_GBIF_HALT', 0)
    s.rd('RBBM_GBIF_HALT')
    s.wr('RBBM_SECVID_TSB_CNTL', 0)
    wr64(s, 'RBBM_SECVID_TSB_TRUSTED_BASE', 0)
    s.wr('RBBM_SECVID_TSB_TRUSTED_SIZE', 0)
    s.wr('RBBM_PERFCTR_GPU_BUSY_MASKED', 0xFFFFFFFF)
    wr64(s, 'UCHE_CCHE_GC_GMEM_RANGE_MIN', R.GMEM_RANGE_MIN)
    wr64(s, 'SP_HLSQ_GC_GMEM_RANGE_MIN', R.GMEM_RANGE_MIN)
    for name in ('UCHE_TRAP_BASE', 'UCHE_WRITE_THRU_BASE', 'UCHE_CCHE_TRAP_BASE', 'UCHE_CCHE_WRITE_THRU_BASE'):
        wr64(s, name, R.UCHE_TRAP_BASE)
    s.wr('RBBM_PERFCTR_CNTL', 1)
    s.wr('RBBM_SLICE_PERFCTR_CNTL', 1)
    s.wr('GMU_POWER_COUNTER_SELECT_XOCLK_1', 0x4)
    s.wr('CP_PERFCTR_CP_SEL', 1, 0)
    if getattr(args, 'x290', False):
        import a16gpu_x290            # agents/gpu-x290: a8xx_set_ubwc_config (glymur), where Linux calls it
        a16gpu_x290.ubwc_config(s, out)
    for n, weight in enumerate(R.BICUBIC_WEIGHTS):
        s.wr('TPL1_BICUBIC_WEIGHTS_TABLE', weight, n)
    s.wr('UCHE_CLIENT_PF', (1 << 7) | 1)
    gmem_protect = 0
    for pipe in range(R.PIPE_NONE, R.PIPE_DDE_BV + 1):           # a8xx_nonctxt_config
        if pipe == R.PIPE_LPAC:
            continue
        s.wr('CP_APERTURE_CNTL_HOST', R.aperture(pipe))
        for name, value, pipes in R.X285_NONCTXT:
            if pipes >> pipe & 1:
                if name == 'RB_GC_GMEM_PROTECT':
                    gmem_protect = value
                s.wr(name, value)
    aperture_clear(s)
    s.wr('RBBM_INTERFACE_HANG_INT_CNTL', (1 << 30) | 0xCFFFFF)
    s.wr('RBBM_SLICE_INTERFACE_HANG_INT_CNTL', 1 << 30)
    s.wr('GMU_AO_GPU_CX_BUSY_MASK', 0xFF000000)
    s.wr_update('GMU_POWER_COUNTER_SELECT_XOCLK_0', 0xFF, 1 << 5)
    s.wr('GMU_POWER_COUNTER_ENABLE', 1)
    pipe_write(s, R.PIPE_BR, 'CP_PROTECT_CNTL_PIPE', R.CP_PROTECT_CNTL)     # a8xx_set_cp_protect
    pipe_write(s, R.PIPE_BV, 'CP_PROTECT_CNTL_PIPE', R.CP_PROTECT_CNTL)
    aperture_clear(s)
    for n, value in enumerate(R.X285_PROTECT):
        s.wr('CP_PROTECT_GLOBAL', value, n)
    pipe_write(s, R.PIPE_BR, 'CP_PROTECT_PIPE', R.X285_PROTECT[-1], 15)
    pipe_write(s, R.PIPE_BV, 'CP_PROTECT_PIPE', R.X285_PROTECT[-1], 15)
    aperture_clear(s)
    pipe_write(s, R.PIPE_BR, 'RB_CONTEXT_SWITCH_GMEM_SAVE_RESTORE_ENABLE', 1)
    for pipe in range(R.PIPE_BR, R.PIPE_DDE_BV + 1):
        if pipe == R.PIPE_LPAC:
            continue
        s.wr('CP_APERTURE_CNTL_HOST', R.aperture(pipe))
        s.wr('CP_APRIV_CNTL_PIPE', R.A8XX_BR_APRIV_MASK if pipe == R.PIPE_BR else R.A8XX_APRIV_MASK)
        s.wr('CP_INTERRUPT_STATUS_MASK_PIPE', R.A8XX_CP_INT_MASK_PIPE)
        s.wr('CP_HW_FAULT_STATUS_MASK_PIPE', R.A8XX_CP_HW_FAULT_MASK_PIPE)
    aperture_clear(s)
    s.wr('CP_INTERRUPT_STATUS_MASK_GLOBAL', R.A8XX_CP_GLOBAL_INT_MASK)
    s.wr('RBBM_INT_0_MASK', R.A8XX_INT_MASK)
    cp_aperture(s, out, args, host if host is not None else {})     # adreno_hw_init
    s.step = 'C3'
    base = s.arena[0]
    wr64(s, 'CP_SQE_INSTR_BASE', R.CP_SQE_VA)
    wr64(s, 'CP_RB_BASE', R.CP_RB_VA)
    s.wr('CP_RB_CNTL', R.RB_CNTL_DEFAULT)
    wr64(s, 'CP_RB_RPTR_ADDR', R.CP_MEMPTRS_VA + R.CP_RPTR_SHADOW_OFF)
    wr64(s, 'CP_RB_RPTR_ADDR_BV', R.CP_MEMPTRS_VA + R.CP_BV_RPTR_OFF)
    out['hw_init'] = {'gmem_protect': hx(gmem_protect), 'arena': hx(base)}
    return gmem_protect


# The registers the pre-start snapshot and the post-start samples read. CX first: the SMMU and the GMU
# answer even when a GX access would not.
PRESTART_CX = ('SMMU_sCR0', 'SMMU_sGFSR', 'SMMU_sGFSYNR0', 'SMMU_sGFSYNR1', 'SMMU_sGFSYNR2', 'SMMU_sGFAR',
               'SMMU_sGFAR_HI', 'GMU_CM3_SYSRESET', 'GMU_CM3_FW_INIT_RESULT', 'GMU_HFI_CTRL_STATUS',
               'GMU_PWR_CLK_STATUS', 'GMU_RPMH_POWER_STATE', 'GMU_AO_GPU_CX_BUSY_STATUS',
               'GMU_AO_HOST_INTERRUPT_STATUS', 'GMU_GMU2HOST_INTR_INFO', 'GMU_AHB_FENCE_STATUS', 'GBIF_HALT',
               'GBIF_HALT_ACK')
PRESTART_GX = ('RBBM_SECVID_TRUST_CNTL', 'RBBM_SECVID_TSB_CNTL', 'RBBM_SECVID_TSB_TRUSTED_SIZE', 'RBBM_STATUS',
               'RBBM_STATUS1', 'RBBM_INT_0_STATUS', 'RBBM_SW_FUSE_INT_STATUS', 'RBBM_GBIF_HALT_ACK', 'CP_SQE_CNTL',
               'CP_SQE_INSTR_BASE', 'CP_SQE_INSTR_BASE_HI', 'CP_RB_BASE', 'CP_RB_BASE_HI', 'CP_RB_CNTL', 'CP_RB_RPTR',
               'CP_RB_WPTR', 'CP_INTERRUPT_STATUS_GLOBAL', 'CP_CP2GMU_STATUS')
SAMPLE_CX = ('SMMU_sGFSR', 'GMU_AO_HOST_INTERRUPT_STATUS', 'GMU_GMU2HOST_INTR_INFO', 'GMU_PWR_CLK_STATUS',
             'GMU_AHB_FENCE_STATUS', 'GMU_AO_GPU_CX_BUSY_STATUS')
SAMPLE_GX = ('RBBM_STATUS', 'RBBM_INT_0_STATUS', 'CP_INTERRUPT_STATUS_GLOBAL', 'CP_SQE_CNTL', 'CP_RB_RPTR')


def cb_report(s, cb):
    """Every CB fault register, whatever FSR says (the snapshot wants the stale ones too)."""
    names = ('SMMU_CB_SCTLR', 'SMMU_CB_TTBR0', 'SMMU_CB_TTBR0_HI', 'SMMU_CB_FSR', 'SMMU_CB_FAR', 'SMMU_CB_FAR_HI',
             'SMMU_CB_FSYNR0', 'SMMU_CB_FSYNR1')
    report = {n[len('SMMU_CB_'):].lower(): hx(s.rd(n, cb)) for n in names}
    report['cbfrsynra'] = hx(s.rd('SMMU_CBFRSYNRA', cb))
    return report


def sqe_prestart(s, out, args, fw, probe):
    """C3P, read-only: everything that says whether starting the SQE is safe, logged (and written to
    the checkpoint file) before CP_SQE_CNTL is touched."""
    s.step = 'C3P'
    snap = {'cx': {n: hx(s.rd(n)) for n in PRESTART_CX}}
    snap['cb0'], snap['cb1'] = cb_report(s, R.GPU_CB), cb_report(s, R.GMU_CB)
    snap['cx_ahb'] = dict(zip(R.CX_AHB_PORTS, (hx(s.rd('CX_MISC_CX_AHB_CNTL', n)) for n in range(len(R.CX_AHB_PORTS)))))
    snap['gx'] = {n: hx(s.rd(n)) for n in PRESTART_GX}
    snap['gx']['CP_AQE_INSTR_BASE_0'] = hx(s.rd('CP_AQE_INSTR_BASE_0', 1) << 32 | s.rd('CP_AQE_INSTR_BASE_0', 0))
    _scr0, groups = smmu_globals(s, out)
    smmu_verify(s, out, groups, {R.GPU_SID: R.GPU_CB, R.GPU_SID_LPAC: R.GPU_CB, R.GMU_SID: R.GMU_CB})
    snap['stream_table'] = {k: out['stream_table'][k] for k in ('0', '1', '5')}
    # What CB0 will do with the SQE's first fetches, walked from the tables in DRAM (what the SMMU reads).
    offset, blob = build_tables(s.arena[0], 'gpu', probe=probe).image()
    image = s.mem_read(offset, len(blob), 'GPU tables (pre-start walk)')
    table_base = s.arena[0] + offset
    read_u64 = lambda pa: struct.unpack_from('<Q', image, pa - table_base)[0] \
        if table_base <= pa < table_base + len(image) else 0
    ttbr = int(snap['cb0']['ttbr0_hi'], 16) << 32 | int(snap['cb0']['ttbr0'], 16)
    walks, bad = {}, []
    for name, va, pa, size, _attrs in gpu_mappings(s.arena[0]):
        for probe_va, want in ((va, pa), (va + size - 4, pa + size - 4)):
            hit = walk(read_u64, ttbr, probe_va)
            got = hit[0] if hit else None
            walks[f'{name}@{probe_va:#x}'] = hx(got) if got is not None else 'fault'
            if got != (None if probe else want):
                bad.append((name, hx(probe_va), walks[f'{name}@{probe_va:#x}']))
    snap['cb0_walk'] = walks
    sqe = fw['sqe'][4:]
    digest = hashlib.sha256(s.mem_read(region('cp_sqe')[0], len(sqe), 'SQE (pre-start)')).hexdigest()
    snap['sqe_image'] = {'bytes': len(sqe), 'sha256_dram': digest,
                         'matches_file_minus_dword0': digest == hashlib.sha256(sqe).hexdigest()}
    snap['probe'] = 'fault (every CB0 VA unmapped)' if probe else 'none'
    out['prestart'] = snap
    # Checks: a reason to stop is a reason not to start the SQE at all.
    problems = []
    fault_bits = lambda v: int(v, 16) & R.CB_FSR_FAULT
    if (fault_bits(snap['cb0']['fsr']) or fault_bits(snap['cb1']['fsr']) or int(snap['cx']['SMMU_sGFSR'], 16)) \
            and not getattr(args, 'prestart_faults_ok', False):
        problems.append('an SMMU fault is already recorded (CB0/CB1 FSR or sGFSR): something used a stream before '
                        'the SQE started (--prestart-faults-ok to start anyway)')
    if int(snap['gx']['CP_SQE_CNTL'], 16) & 1:
        problems.append('CP_SQE_CNTL already reads 1')
    if int(snap['cx']['GMU_PWR_CLK_STATUS'], 16) & R.PWR_CLK_STATUS_GX_OFF:
        problems.append('GMU_PWR_CLK_STATUS says GX is off')
    # GBIF_HALT_ACK is not the halt state on the A16 (PROVEN, run 2): it read 0x3 with no halt requested,
    # before and after KGSL's BIT(3) clear, while the GMU booted from DDR through this GBIF. A halt
    # request (GBIF_HALT bits 0/1) or a GX-side halt acknowledgement still stops the start.
    if int(snap['cx']['GBIF_HALT'], 16) & 3 or int(snap['gx']['RBBM_GBIF_HALT_ACK'], 16) & 1:
        problems.append('a GBIF halt is requested (GBIF_HALT bits 0/1) or acknowledged on the GX side')
    elif int(snap['cx']['GBIF_HALT_ACK'], 16) & 3:
        snap.setdefault('notes', []).append(
            f"GBIF_HALT_ACK {snap['cx']['GBIF_HALT_ACK']} with no halt requested (GBIF_HALT "
            f"{snap['cx']['GBIF_HALT']}): the A16's idle value, not a halt (GMU DDR traffic flows)")
    if int(snap['gx']['RBBM_SECVID_TSB_TRUSTED_SIZE'], 16):
        problems.append('RBBM_SECVID_TSB_TRUSTED_SIZE is not 0: part of the VA space would be secure')
    for lo, hi, want in (('CP_SQE_INSTR_BASE', 'CP_SQE_INSTR_BASE_HI', R.CP_SQE_VA),
                         ('CP_RB_BASE', 'CP_RB_BASE_HI', R.CP_RB_VA)):
        got = int(snap['gx'][hi], 16) << 32 | int(snap['gx'][lo], 16)
        if got not in (want, 0):
            problems.append(f'{lo} reads {got:#x}, not {want:#x}')
        elif not got:
            snap.setdefault('notes', []).append(f'{lo} reads 0 (write-only on this CP?)')
    if bad:
        problems.append(f'CB0 does not translate the CP buffers as built: {bad}')
    if not snap['sqe_image']['matches_file_minus_dword0']:
        problems.append('the SQE image in DRAM is not gen80100_sqe.fw minus its first dword')
    want_ahb = None if getattr(args, 'no_ahb_timeout', False) else out.get('cx_ahb', {}).get('value')
    if want_ahb and set(snap['cx_ahb'].values()) != {want_ahb}:
        problems.append(f'the CX AHB timeouts are not armed ({snap["cx_ahb"]})')
    snap['secure_mode_at_start'] = bool(int(snap['gx']['RBBM_SECVID_TRUST_CNTL'], 16) & 1)
    checkpoint = getattr(args, 'checkpoint', None)
    if checkpoint:
        # Written before the SQE starts: if the A16 goes down with it, this file still says what it saw.
        Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint).write_text(json.dumps({'command': out.get('command'), 'order': out.get('order'),
                                                'prestart': snap, 'problems': problems,
                                                'hw_init': out.get('hw_init'), 'aperture': out.get('aperture'),
                                                'cx_ahb': out.get('cx_ahb')}, indent=2, default=str) + '\n')
        print(f'pre-start snapshot written to {checkpoint}', file=sys.stderr)
    if problems:
        raise Stop('C3P', 'not starting the SQE: ' + '; '.join(problems), prestart=snap)
    out['order'].append('sqe_prestart')
    return snap


def sqe_start(s, out, args, probe, baseline=None):
    """C3S: CP_SQE_CNTL = 1 (a8xx hw_init's last line), then a few bounded read-only samples, CX side
    first, each timed from the write, so the log shows how far the A16 got if it goes down."""
    s.step = 'C3S'
    count = max(1, int(getattr(args, 'sqe_samples', 4)))
    gap_us = int(getattr(args, 'sqe_sample_us', 2000))
    record = out['sqe_start'] = {'probe': bool(probe), 'samples': []}
    t0 = s.clock()
    s.wr('CP_SQE_CNTL', 1)
    record['written'] = True
    for n in range(count):
        sample = {'n': n, 't_ms': round((s.clock() - t0) * 1000, 3)}
        record['samples'].append(sample)          # kept even if a read below never comes back
        sample['cb0_fsr'] = hx(s.rd('SMMU_CB_FSR', R.GPU_CB))
        for name in SAMPLE_CX + SAMPLE_GX:
            sample[name] = hx(s.rd(name))
        if n + 1 < count:
            s.udelay(gap_us)
    record['cb0'] = cb_report(s, R.GPU_CB)
    gfsr = s.rd('SMMU_sGFSR')
    record['global'] = {'sGFSR': hx(gfsr)}
    if gfsr:
        record['global'].update({n: hx(s.rd(n)) for n in ('SMMU_sGFSYNR0', 'SMMU_sGFSYNR1', 'SMMU_sGFSYNR2',
                                                          'SMMU_sGFAR', 'SMMU_sGFAR_HI')})
    fsr = int(record['cb0']['fsr'], 16)
    far = int(record['cb0']['far_hi'], 16) << 32 | int(record['cb0']['far'], 16)
    sid = int(record['cb0']['cbfrsynra'], 16) & 0xFFFF
    sqe_lo, sqe_hi = R.CP_SQE_VA, R.CP_SQE_VA + region('cp_sqe')[1]
    if probe:
        if fsr & 0x2 and sqe_lo <= far < sqe_hi:
            record['verdict'] = (f'the SQE fetched through the SMMU and faulted as intended: CB0 translation fault '
                                 f'at {far:#x} (SQE image), stream {sid}; the A16 stayed up')
            out['order'].append('sqe_probe')
            return record
        verdict = ('no CB0 fault after the samples: the SQE did not fetch its image (or not on SID 0/1)'
                   if not fsr & R.CB_FSR_FAULT else f'CB0 FSR {fsr:#x} at {far:#x}, stream {sid}: not the SQE fetch')
        record['verdict'] = verdict + (f'; sGFSR {gfsr:#x}' if gfsr else '')
        raise Stop('C3S', f'SQE probe: {record["verdict"]}', sqe_start=record)
    # What the snapshot already showed (--prestart-faults-ok) is not this start's doing.
    base_fsr = int((baseline or {}).get('cb0', {}).get('fsr', '0x0'), 16)
    base_gfsr = int((baseline or {}).get('cx', {}).get('SMMU_sGFSR', '0x0'), 16)
    if fsr & R.CB_FSR_FAULT and fsr != base_fsr or gfsr and gfsr != base_gfsr:
        raise Stop('C3S', f'the SQE started but the SMMU recorded a fault: CB0 FSR {fsr:#x} FAR {far:#x} stream {sid}, '
                          f'sGFSR {gfsr:#x}: `quiesce --write`', sqe_start=record)
    out['order'].append('sqe_started')
    return record


def x285_gmem_protect():
    """The RB_GC_GMEM_PROTECT value hw_init's nonctxt pass records (a8xx_nonctxt_config)."""
    return next(value for name, value, _pipes in R.X285_NONCTXT if name == 'RB_GC_GMEM_PROTECT')


def ring_submit(s, host, words, what):
    """OUT_RING at the host's wptr, then a8xx_flush: the fenced CP_RB_WPTR write."""
    offset = region('cp_rb')[0]
    size = region('cp_rb')[1] // 4
    wptr = host['rb_wptr']
    # The record is bookkeeping: the CP's own pointers decide. A WPTR behind the CP's (a lost
    # record write) would make the CP run on through the whole ring from its RPTR, executing
    # stale or zeroed words; a CP that has not reached WPTR is still busy with the last packet.
    hw_wptr, hw_rptr = s.rd('CP_RB_WPTR'), s.rd('CP_RB_RPTR')
    if hw_wptr != wptr or hw_rptr != wptr:
        raise Stop(s.step, f'ring pointers disagree before {what}: record wptr {wptr}, CP_RB_WPTR {hw_wptr}, '
                           f'CP_RB_RPTR {hw_rptr}: `quiesce --write` and start over')
    if wptr + len(words) >= size:
        raise Stop(s.step, 'ring would wrap: quiesce and start over (this tool never wraps the ring)')
    s.mem_write(offset + 4 * wptr, struct.pack(f'<{len(words)}I', *words), f'ring: {what}')
    wptr += len(words)
    fenced_write(s, 'CP_RB_WPTR', wptr)
    host['rb_wptr'] = wptr
    return wptr


def fenced_write(s, name, value):
    """a6xx_fenced_write: write, then AHB_FENCE_STATUS WRITEDROPPED (bit 0) must stay clear; retry once."""
    for attempt in range(2):
        s.wr(name, value)
        status = s.rd('GMU_AHB_FENCE_STATUS')
        if not status & 1:
            return
        s.udelay(10)
    raise Stop(s.step, f'fenced write of {name} dropped twice (AHB_FENCE_STATUS {status:#x})')


def cp_idle(s, wptr):
    """adreno_idle + _a8xx_check_idle: rptr caught up, RBBM idle but the CX master, no hang."""
    s.poll3('CP_RB_RPTR', 0xFFFFFFFF, wptr, 'cp')
    status = s.rd('RBBM_STATUS')
    irq = s.rd('RBBM_INT_0_STATUS')
    busy = s.rd('GMU_AO_GPU_CX_BUSY_STATUS')
    if status & ~R.RBBM_STATUS_CX_MASTER or irq & R.RBBM_INT_HANG_DETECT:
        raise Stop(s.step, f'GPU not idle: RBBM_STATUS {status:#x}, RBBM_INT_0_STATUS {irq:#x}, CX_BUSY {busy:#x}')
    return {'rbbm_status': hx(status), 'int0': hx(irq), 'cx_busy': hx(busy)}


def cmd_cp_fence(s, out, args):
    prologue(s, out)
    host = arena_prove(s, out)
    s.step = 'C0'
    gmu_running_proven(s, out, host)
    if host['oob_token'] != host['boot_token']:
        raise Stop('C0', 'OOB GPU_SET was not acknowledged for this GMU boot: run oob first')
    status, on = gx_on_proven(s, 'OOB GPU_SET held for this boot')
    if not on:
        raise Stop('C0', f'GMU_PWR_CLK_STATUS {status:#x}: GX is off; GX registers stay refused')
    out['order'].append('gx_on')
    fw = load_firmware(args.fw_dir)
    base = s.arena[0]
    sqe_on = s.rd('CP_SQE_CNTL') & 1
    cp_ready = host['cp_token'] == host['boot_token'] and sqe_on
    wide = getattr(args, 'va_wide', False)
    if wide:
        # --va-wide (Snapintosh ADRENO_VA_WIDE): CB0 T0SZ 30 needs an upstream bus wider than 32 bits, and a CP this
        # GMU boot already started must have been given the same CB0 (C1 runs once per GMU boot).
        idr2 = s.rd('SMMU_IDR2')
        if not idr2 >> R.IDR2_UBS_SHIFT & R.IDR2_UBS_MASK:
            raise Stop('C0', f'SMMU_IDR2 {idr2:#x}: UBS is 32 bits, too narrow for CB0 T0SZ {R.CB_T0SZ_WIDE} (--va-wide)')
        if cp_ready or (host['hwinit_token'] == host['boot_token'] and sqe_on):
            tcr = s.rd('SMMU_CB_TCR', R.GPU_CB)
            if tcr != R.CB_TCR_WIDE:
                raise Stop('C0', f'CB0 TCR {tcr:#x} is not the --va-wide {R.CB_TCR_WIDE:#x} this GMU boot: '
                                 '`quiesce --write`, then gmu-boot, hfi, oob and cp-fence --va-wide again')
        out['va_wide'] = {'idr2': hx(idr2), 'cb0_tcr': hx(R.CB_TCR_WIDE)}
    if getattr(args, 'x290', False):
        # --x290 (agents/gpu-x290): the 4-slice X2-90 proven from the fuse before anything is written; a GMU boot whose
        # hw_init already ran must have had the same flag (a16gpu_x290.cp_fence_check).
        import a16gpu_x290
        a16gpu_x290.cp_fence_check(s, out, host, cp_ready or (host['hwinit_token'] == host['boot_token'] and sqe_on))
    if not cp_ready:
        # REVIEW: never re-run C1-C3 under a started SQE. It fetches through its instruction
        # cache from the SQE buffer C2 zero-fills, and Linux programs SQE/RB bases only before
        # SQE_CNTL = 1. A run that stopped after C3 (the runbook's first cp-fence) or timed out
        # in C4 resumes at C4; a running SQE this tool did not start this GMU boot needs quiesce.
        hw_done = host['hwinit_token'] == host['boot_token'] and sqe_on
        if sqe_on and host.get('sqe_probe') and host['sqe_probe'] == host['boot_token']:
            raise Stop('C0', 'this GMU boot\'s SQE was started by `--sqe-probe fault` (every CB0 VA unmapped): it '
                             'never runs CP_ME_INIT. `quiesce --write`, then gmu-boot, hfi, oob and cp-fence again')
        if sqe_on and not hw_done:
            raise Stop('C0', 'CP_SQE_CNTL reads 1 but this GMU boot\'s hw_init was not done by this tool: '
                             '`quiesce --write` first (a running CP is never re-initialised)')
        if hw_done:
            gmem_protect = x285_gmem_protect()
            out['hw_init'] = 'already done for this GMU boot (SQE running): resuming at C4'
            out['order'].append('hw_init_done')
        else:
            s.step = 'C1'
            probe = getattr(args, 'sqe_probe', 'none') == 'fault'
            tables = build_tables(base, 'gpu', probe=probe)
            _scr0, groups = smmu_globals(s, out)
            if cb_diff(smmu_read_cb(s, R.GPU_CB), smmu_cb_expected(tables, R.GPU_CB, wide)):
                # CB0 is about to be rewritten (SCTLR.M = 0 on the way): SID 1 must not be routed to it
                # then; smmu_program takes SID 0 off itself.
                smmu_unroute(s, R.GPU_SMR_LPAC, R.GPU_SID_LPAC)
            changed = write_tables(s, tables, 'GPU')
            if not smmu_program(s, out, tables, R.GPU_CB, R.GPU_SMR, R.GPU_SID, 'gpu_cb', wide) and changed:
                smmu_tlbi(s, R.GPU_CB)     # the bank read back identical but its tables changed (probe <-> real)
            # glymur.dtsi: the GPU's second stream (SID 1) is in the same domain (Linux: one SME per ID).
            smmu_route(s, R.GPU_SMR_LPAC, R.GPU_SID_LPAC, R.GPU_CB)
            ensure_usfcfg(s)
            smmu_verify(s, out, groups, {R.GPU_SID: R.GPU_CB, R.GPU_SID_LPAC: R.GPU_CB, R.GMU_SID: R.GMU_CB})
            out['gpu_cb']['tables'] = 'probe: empty (every VA faults)' if probe else 'SQE, ring, memptrs'
            host['gpu_ttbr'] = ttbr_for(tables, R.GPU_CB)
            out['order'].append('gpu_cb')
            s.step = 'C2'
            sqe = fw['sqe'][4:]                         # adreno_fw_create_bo: skip the first dword
            s.mem_fill(region('cp_sqe')[0], region('cp_sqe')[1], 'cp_sqe')
            s.mem_write(region('cp_sqe')[0], sqe, 'SQE image')
            if s.mem_read(region('cp_sqe')[0], len(sqe), 'SQE (verify)') != sqe:
                raise Stop('C2', 'SQE image did not read back')
            s.mem_fill(region('cp_rb')[0], region('cp_rb')[1], 'ringbuffer')
            s.mem_fill(region('cp_memptrs')[0], region('cp_memptrs')[1], 'memptrs')
            host.update(rb_wptr=0, cp_token=0, hwinit_token=0)
            write_host(s, host)
            out['order'].append('cp_buffers')
            gmem_protect = hw_init(s, out, args, host)
            out['order'].append('hw_init')
            if args.stop_after == 'C3':
                out['stopped_after'] = 'C3'
                return
            snapshot = sqe_prestart(s, out, args, fw, probe)
            if args.stop_after == 'C3P':
                out['stopped_after'] = 'C3P'
                return
            # From the write on, this GMU boot has a started SQE: record it before anything can go wrong.
            host['hwinit_token'] = host['boot_token']
            host['sqe_probe'] = host['boot_token'] if probe else 0
            write_host(s, host)
            sqe_start(s, out, args, probe, snapshot)
            if probe:
                out['stopped_after'] = 'C3S'
                return
        if args.stop_after in ('C3', 'C3P', 'C3S'):
            out['stopped_after'] = args.stop_after
            return
        s.step = 'C4'
        if host['rb_wptr']:
            # CP_ME_INIT already went out this boot (a C4 timeout, or a lost C5): wait again,
            # never queue a second one behind it -- if the CP's WPTR says the same.
            wptr = host['rb_wptr']
            hw_wptr = s.rd('CP_RB_WPTR')
            if hw_wptr != wptr:
                raise Stop('C4', f'record wptr {wptr} but CP_RB_WPTR {hw_wptr}: `quiesce --write` and start over')
            out['cp_init_resubmitted'] = False
        else:
            init = [R.pkt7(R.CP_THREAD_CONTROL, 1), 1 << 27,
                    R.pkt7(R.CP_ME_INIT, 7), R.CP_ME_INIT_MASK, 0x00000003, 0x20000000, 0x00000002, 0, 0, 1 << 31]
            wptr = ring_submit(s, host, init, 'CP_THREAD_CONTROL + CP_ME_INIT (BIT(8) clear)')
            write_host(s, host)
        try:
            out['cp_init'] = cp_idle(s, wptr)
        except PollTimeout as stuck:
            stuck.detail['cb0'] = cb_faults(s, R.GPU_CB)[1]
            stuck.detail['sqe_cntl'] = hx(s.rd('CP_SQE_CNTL'))
            stuck.detail['rbbm_status'] = hx(s.rd('RBBM_STATUS'))
            raise
        out['order'].append('cp_init')
        s.step = 'C5'
        s.wr('RBBM_SECVID_TRUST_CNTL', R.SECVID_TRUST_CNTL_NONSECURE)
        secvid = s.rd('RBBM_SECVID_TRUST_CNTL')
        out['secvid'] = hx(secvid)
        if secvid != R.SECVID_TRUST_CNTL_NONSECURE:
            raise Stop('C5', f'RBBM_SECVID_TRUST_CNTL reads {secvid:#x} after writing 0: still secure')
        pipe_write(s, R.PIPE_BR, 'RB_GC_GMEM_PROTECT', gmem_protect)
        aperture_clear(s)
        host['cp_token'] = host['boot_token']
        write_host(s, host)
        out['order'].append('secvid')
        if args.stop_after == 'C5':
            out['stopped_after'] = 'C5'
            return
    s.step = 'C6'
    host['fence_seq'] += 1
    value = 0xF3A00000 | (host['fence_seq'] & 0xFFFFF)
    fence_va = R.CP_MEMPTRS_VA + R.CP_FENCE_OFF
    wptr = ring_submit(s, host, [R.pkt7(R.CP_MEM_WRITE, 3), fence_va & 0xFFFFFFFF, fence_va >> 32, value],
                       f'CP_MEM_WRITE {value:#x}')
    write_host(s, host)
    fence_off = region('cp_memptrs')[0] + R.CP_FENCE_OFF
    limit, seconds = POLL['fence']
    seen = None
    deadline = s.clock() + seconds
    for attempt in range(1, limit + 1):
        seen = struct.unpack('<I', s.mem_read(fence_off, 4, 'fence'))[0]
        if seen == value:
            break
        if attempt >= 2 and s.clock() > deadline:
            break
    s.polls.append({'reg': 'fence (DRAM)', 'reads': attempt, 'ok': seen == value})
    fault, report = cb_faults(s, R.GPU_CB)
    out['fence'] = {'value': hx(value), 'read': hx(seen), 'cb0': report, 'wptr': wptr}
    if seen != value:
        raise Stop('C6', f'the GPU did not write the fence: read {seen:#x}, want {value:#x}', cb0=report)
    out['fence']['idle'] = cp_idle(s, wptr)
    host['fence_value'] = value
    write_host(s, host)
    out['order'].append('fence')


# --- quiesce --------------------------------------------------------------------------------------------------
def gbif_halt(s, out):
    """a8xx_bus_clear_pending_transactions(gx_off=false): client halt, arbiter halt, then (as Linux) clear."""
    s.wr('GBIF_HALT', 1)
    s.poll3('GBIF_HALT_ACK', 1, 1, 'gbif')
    s.wr('GBIF_HALT', 2)
    s.poll3('GBIF_HALT_ACK', 2, 2, 'gbif')
    if not out.get('_leave_halted'):
        s.wr('GBIF_HALT', 0)


def cmd_quiesce(s, out, args):
    out['_leave_halted'] = args.leave_halted
    prologue(s, out)
    host = arena_prove(s, out)
    s.step = 'Q0'
    state = gmu_state(s)
    out['gmu_before'] = _json(state)
    graceful = []
    if not state['sysreset'] & 1:          # CM3 out of reset: running, or crashed with GX possibly on
        _status, gx = gx_on_proven(s, 'quiesce: GX state read')
        out['gx_on'] = gx
        s.step = 'Q1'
        if host['oob_token'] == host['boot_token'] and host['boot_token']:
            s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_CLEAR)   # a6xx_gmu_clear_oob(GPU_SET)
            graceful.append('oob_cleared')
        try:
            if not state['running']:
                raise Stop('Q1', 'the GMU is out of reset but not booted: no graceful path')
            if host['hfi_token'] == host['boot_token'] and host['boot_token'] \
                    and host['oob_token'] != host['boot_token']:
                # REVIEW-2: a6xx_gmu_shutdown -- "GMU firmware's internal power state gets messed
                # up if we send prepare_slumber hfi when oob_gpu handshake wasn't done after the
                # last wake up": a dummy GPU_SET/clear first; no ack means the force path.
                oob_handshake(s)
                graceful.append('oob_handshake')
            s.poll3('GMU_RPMH_POWER_STATE', 0xFFFFFFFF, R.GMU_IDLE_STATE_ACTIVE, 'idle')   # wait_for_idle
            gbif_halt(s, out)                                                           # bus_halt
            graceful.append('gbif_halted')
            if host['hfi_token'] == host['boot_token'] and host['boot_token']:
                # a6xx_gmu_notify_slumber: "Disable the power counter so the GMU isn't busy"
                # (hw_init turned it on).
                s.wr('GMU_POWER_COUNTER_ENABLE', 0)
                hfi_send(s, host, R.HFI_MSG_PREPARE_SLUMBER, [0, 0], 'PREPARE_SLUMBER')
                graceful.append('prepare_slumber')
            s.wr('GMU_AO_AHB_FENCE_CTRL', 0)
            s.wr('GMU_CX_FALNEXT_INTF', 1)
            s.poll3('GMU_AO_GPU_CX_BUSY_STATUS', R.CX_BUSY_GPUBUSYIGNAHB, 0, 'idle')
            out['quiesce'] = 'graceful'
        except (PollTimeout, Stop) as problem:
            if isinstance(problem, (SErrorStop, AbortStop)):
                raise
            out['quiesce'] = f'graceful path failed ({problem.reason}): forcing off'
            s.wr('GMU_GMU_PWR_COL_KEEPALIVE', 0)
            s.withdraw('gx')
            s.wr('GMU_CX_FALNEXT_INTF', 1)
            for n in range(10):                     # a6xx_gmu_rpmh_off: best effort, bounded
                try:
                    s.poll3('RSCC_TCS_DRV0_STATUS', 1, 1, 'rscc', index=n)
                except PollTimeout:
                    pass
            s.wr('GMU_AHB_FENCE_STATUS_CLR', 0x7)
            s.wr('GMU_AO_AHB_FENCE_CTRL', 0)
            try:
                gbif_halt(s, out)
            except PollTimeout:
                out['gbif'] = 'halt not acknowledged'
        s.step = 'Q2'
        s.wr('GMU_AO_HOST_INTERRUPT_MASK', 0xFFFFFFFF)
        s.wr('GMU_GMU2HOST_INTR_MASK', 0xFFFFFFFF)
        s.wr('GMU_CM3_SYSRESET', 1)
        # a6xx_hfi_stop, but with the CM3 held in reset: nothing else writes the header line now.
        # A reset that does not read back leaves the headers alone and quiesce carries on: the
        # SMMU step below is what stops the GMU's DMA, and arena --release re-checks the reset.
        out['hfi_stop'] = _hfi_stop(s)
        s.withdraw('gx')
        s.wr('GMU_RSCC_CONTROL_REQ', 1)            # a6xx_rpmh_stop
        host['pdc_sleep'] = 1                      # REVIEW-2: the next gmu-boot wakes it (a6xx_rpmh_start)
        write_host(s, host)
        try:
            s.poll3('RSCC_RSC_STATUS0_DRV0', 1 << 16, 1 << 16, 'rscc')
        except PollTimeout:
            out['rscc'] = 'power-off not acknowledged'
        s.wr('GMU_RSCC_CONTROL_REQ', 0)
        graceful.append('gmu_reset')
        # Linux then drops its GX power-domain vote (gxpd); this tool never touches the GX GDSC,
        # so it only records whether the GMU left GX off.
        out['gx_after'] = hx(s.rd('GMU_PWR_CLK_STATUS'))
    else:
        s.withdraw('gx')
        graceful.append('already in reset')
    out['gmu_steps'] = graceful
    s.step = 'Q3'
    _scr0, groups = smmu_globals(s, out)
    smmu_unroute(s, R.GPU_SMR_LPAC, R.GPU_SID_LPAC)      # SID 1 off CB0 before CB0 goes off
    for cb, smr, sid in ((R.GPU_CB, R.GPU_SMR, R.GPU_SID), (R.GMU_CB, R.GMU_SMR, R.GMU_SID)):
        if s.rd('SMMU_SMR', smr) & R.SMR_VALID or s.rd('SMMU_CB_SCTLR', cb) & R.SCTLR_M:
            smmu_disable(s, cb, smr, sid)
    ensure_usfcfg(s)             # after quiesce SIDs 0/1/5 are unmatched: they must fault, never bypass
    live = sids_reaching_memory(s, s.rd('SMMU_sCR0'), groups, R.GPU_SIDS + (R.GMU_SID,) + R.MUST_FAULT_SIDS)
    if live:
        raise Stop('Q3', f'streams still reach memory after quiesce: {live}')
    out['order'].append('smmu_off')
    s.step = 'Q4'
    out['rcg'] = {'gmu': S1.park_rcg(s, 'GPU_CC_GMU_CMD_RCGR', 'GPU_CC_GMU_CFG_RCGR'),
                  'hub': S1.park_rcg(s, 'GPU_CC_HUB_CMD_RCGR', 'GPU_CC_HUB_CFG_RCGR')}
    if s.rd('GPU_CC_PLL0_OPMODE') & R.PLL_OPMODE_RUN:
        _pll_off(s)
        out['pll0'] = 'off (STANDBY, outputs off)'
    out['order'].append('clocks_parked')
    host.update(boot_token=0, hfi_token=0, oob_token=0, cp_token=0, gmu_ttbr=0, gpu_ttbr=0, rb_wptr=0, hwinit_token=0,
                sqe_probe=0, p67_token=0, fb_mapped=0)
    write_host(s, host)
    if str(out.get('hfi_stop', '')).startswith('skipped'):
        # Everything else is done (streams fault, clocks parked), but the CM3 may still run.
        raise Stop('Q2', 'GMU_CM3_SYSRESET did not read back 1: the SMMU now faults the GMU\'s DMA, but the CM3 '
                         'may still be running; do not release the arena, reboot the A16', hfi_stop=out['hfi_stop'])
    out['order'].append('quiesced')


def oob_handshake(s):
    """a6xx_gmu_set_oob + a6xx_gmu_clear_oob (GPU_SET), as a6xx_gmu_shutdown's dummy handshake.
    The request is taken back on a timeout too, as cmd_oob does."""
    s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_REQUEST)
    try:
        s.poll3('GMU_GMU2HOST_INTR_INFO', 1 << R.OOB_GPU_SET_ACK, 1 << R.OOB_GPU_SET_ACK, 'oob',
                budget=max(R.OOB_TIMEOUT_S * 10, 0.1))
    except PollTimeout:                    # only a timeout: an SError stops without another access
        s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_CLEAR)
        raise
    s.wr('GMU_GMU2HOST_INTR_CLR', 1 << R.OOB_GPU_SET_ACK)
    s.wr('GMU_HOST2GMU_INTR_SET', 1 << R.OOB_GPU_SET_CLEAR)


def _hfi_stop(s):
    """a6xx_hfi_stop: read and write indices back to 0 in every queue header, only with the CM3
    held in reset (a plain write + clean would race a live GMU on the index line)."""
    if not s.rd('GMU_CM3_SYSRESET') & 1:
        return 'skipped: CM3_SYSRESET does not read back 1, the HFI headers are left alone'
    for queue in range(3):
        s.mem_write(hfi_header_offset(queue, 'read_index'), struct.pack('<2I', 0, 0), f'queue {queue} indices')
    return 'indices zeroed with the CM3 in reset'


# --- read-only commands --------------------------------------------------------------------------------------
def cmd_status(s, out, args):
    prologue(s, out, need=())
    try:
        host = arena_prove(s, out)
        out['host_record'] = _json(host)
    except Stop as missing:
        out['arena'] = {'missing': missing.reason}
    if 'cx_smmu' in s.proven:
        s.step = 'T1'
        _scr0, groups = smmu_globals(s, out)
        out['cb'] = {str(cb): {k: hx(v) for k, v in smmu_read_cb(s, cb).items()} for cb in (R.GPU_CB, R.GMU_CB)}
        out['streams'] = [{'n': n, 'smr': hx(a), 's2cr': hx(b)} for n, a, b in smmu_streams(s, groups)
                          if a & R.SMR_VALID]
        # sqe-reset: every fault register, stale or not (after a --sqe-probe this is the result)
        out['faults'] = {'global': {n: hx(s.rd(n)) for n in ('SMMU_sGFSR', 'SMMU_sGFSYNR0', 'SMMU_sGFSYNR1',
                                                               'SMMU_sGFSYNR2', 'SMMU_sGFAR', 'SMMU_sGFAR_HI')},
                         'cb0': cb_report(s, R.GPU_CB), 'cb1': cb_report(s, R.GMU_CB)}
    if 'cx_misc' in s.proven:
        out['cx_ahb'] = dict(zip(R.CX_AHB_PORTS, (hx(s.rd('CX_MISC_CX_AHB_CNTL', n))
                                                  for n in range(len(R.CX_AHB_PORTS)))))
    if 'cx_gmu' in s.proven:
        s.step = 'T2'
        out['gmu'] = _json(gmu_state(s))
        out['gmu_regs'] = {n: hx(s.rd(n)) for n in ('GMU_PWR_CLK_STATUS', 'GMU_GMU2HOST_INTR_INFO',
                                                    'GMU_AO_HOST_INTERRUPT_STATUS', 'GMU_RPMH_POWER_STATE',
                                                    'GMU_AO_GPU_CX_BUSY_STATUS', 'GBIF_HALT', 'GBIF_HALT_ACK',
                                                    'GPU_CC_PLL0_MODE', 'GPU_CC_PLL0_OPMODE',
                                                    'GPU_CC_GMU_CFG_RCGR', 'GPU_CC_HUB_CFG_RCGR')}
    out['order'].append('status')


def cmd_rsc_survey(s, out, args):
    S1.step_cmd_db(s, out)
    s.step = 'R1'
    layout = rsc_layout(s)
    out['rsc'] = {'layout': layout, 'survey': rsc_survey(s, layout)}
    out['order'].append('rsc_survey')


def release_proven(s, out):
    """REVIEW: the host record is bookkeeping, never evidence. Before the arena is forgotten the
    hardware itself must show that nothing can reach it: the CM3 held in reset, CB0/CB1 off,
    and every stream-match entry that SID 0 or SID 5 hits set to fault."""
    s.step = 'A1'
    reset = s.rd('GMU_CM3_SYSRESET')
    scr0, groups = smmu_globals(s, out)
    banks = {cb: s.rd('SMMU_CB_SCTLR', cb) for cb in (R.GPU_CB, R.GMU_CB)}
    live = sids_reaching_memory(s, scr0, groups, R.GPU_SIDS + (R.GMU_SID,))
    out['release_check'] = {'cm3_sysreset': hx(reset), 'cb_sctlr': {str(k): hx(v) for k, v in banks.items()},
                            'sCR0': hx(scr0), 'live_streams': live}
    if not reset & 1 or any(v & R.SCTLR_M for v in banks.values()) or live:
        raise Stop('A1', 'the GPU or GMU can still reach the arena (CM3 out of reset, a bank on, or SID 0/1/5 '
                         'routed or unmatched with USFCFG clear): `quiesce --write` first',
                   release_check=out['release_check'])


def cmd_arena(s, out, args):
    if args.release:
        prologue(s, out)             # the CX gates the SMMU/GMU reads in release_proven need
    else:
        S1.step_cmd_db(s, out)
    host = arena_prove(s, out, claim=args.claim, limit=args.limit)
    out['host_record'] = _json(host)
    if args.release:
        s.step = 'A1'
        if host['boot_token'] or host['gmu_ttbr'] or host['gpu_ttbr']:
            raise Stop('A1', 'the host record says the GPU may still use the arena: quiesce first')
        release_proven(s, out)
        status = s.call_gpu(GPU_RELEASE, 0x455341454C455247, what='release arena')
        if status:
            raise Stop('A1', f'release refused: {GPU_STATUS.get(status, status)}')
        out['released'] = True


def cmd_reset_reason(s, out, args):
    """READ-ONLY, AO only (no GPU gate, no prologue): the IMEM words firmware and HLOS use to say why the
    last reset happened and whether a crash would enter the download (ramdump) mode. Meaningful for the
    reset that led to *this* boot: after an A16 reset the next boot is Windows (BootNext is one-shot),
    so read it in the first q1n1 boot after a reset only when resets land in q1n1 (see the runbook);
    otherwise it is this boot's baseline. Offsets and values INFERRED (msm-imem layout)."""
    s.step = 'R0'
    # Review: only pil-reloc (glymur.dtsi pil-sram@94c) is a word Linux is known to touch from the
    # non-secure side on this SoC. The other offsets come from downstream msm-imem layouts where they are
    # relative to a different IMEM base (0x146aa000 on SM8550-class trees); here they are unknown words in
    # an IMEM whose secure partitions are not public, and an XPU-protected read can be fatal rather than an
    # SError. They are read only on request (--inferred-imem), never as part of a GPU session.
    out['imem'] = {'pil_reloc_word0': hx(s.rd('IMEM_PIL_RELOC'))}
    if not getattr(args, 'inferred_imem', False):
        out['imem']['inferred_words'] = 'not read (pass --inferred-imem; see the runbook, 7f)'
        out['order'].append('reset_reason')
        return
    dload = [s.rd('IMEM_DLOAD_MAGIC', n) for n in range(2)]
    reason = s.rd('IMEM_RESTART_REASON')
    out['imem'].update({
        'dload_magic': list(map(hx, dload)),
        # INFERRED: modern Qualcomm SoCs arm download mode through TCSR/SCM, not this cookie; a match here
        # does not prove a crash will enter 05c6:9008, and no match does not prove it will not.
        'dload_cookie_matches': tuple(dload) == R.IMEM_DLOAD_MAGIC,
        'dload_type': hx(s.rd('IMEM_DLOAD_TYPE')),
        'restart_reason': hx(reason),
        'restart_reason_decoded': R.IMEM_RESTART_REASONS.get(reason, 'unknown (not an HLOS restart cookie)'),
        'boot_stats': [hx(s.rd('IMEM_BOOT_STATS', n)) for n in range(8)],
    })
    out['order'].append('reset_reason')


COMMANDS = {
    'arena': (cmd_arena, False), 'status': (cmd_status, False), 'rsc-survey': (cmd_rsc_survey, False),
    'reset-reason': (cmd_reset_reason, False),
    'gmu-smmu': (cmd_gmu_smmu, True), 'gmu-boot': (cmd_gmu_boot, True), 'hfi': (cmd_hfi, True),
    'oob': (cmd_oob, True), 'cp-fence': (cmd_cp_fence, True), 'quiesce': (cmd_quiesce, True),
}
STEP_IDS = {'gmu-boot': ('B1', 'B2', 'B3', 'B6', 'B7', 'B8', 'B9'), 'hfi': ('H1', 'H2', 'H3', 'H4', 'H5'),
            'cp-fence': ('C3', 'C3P', 'C3S', 'C5')}
# P6 (blit) and P7 (triangle) live in a16gpu_p6p7.py and run on the CP cp-fence left; imported at the end of
# this module (they use everything above).


class Args:
    """Defaults for run() callers that are not the command line (the offline model)."""
    undo = claim = release = resend = force_smc = host_bw = leave_halted = rail_vote_unseen = False
    no_aperture = no_ahb_timeout = prestart_faults_ok = inferred_imem = False
    sqe_probe = 'none'
    va_wide = False
    x290 = False
    to_framebuffer = fb_any_type = False
    fb_rect = None
    functions = None
    image_prefix = None
    sqe_samples = 4
    sqe_sample_us = 2000
    checkpoint = None
    stop_after = None
    rail_level = 'low_svs'
    max_opps = 1
    perf_opp = 1
    init_timeout = R.GMU_INIT_TIMEOUT_S
    limit = 1 << 32
    fw_dir = None

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


def run(proxy, command, args=None, clock=time.monotonic):
    """One Stage 3 subcommand against a connected proxy: (exit status, JSON dict)."""
    args = args or Args()
    fn, writes = COMMANDS[command]
    if getattr(args, 'claim', False) or getattr(args, 'release', False):
        writes = True
    out = {'tool': 'a16gpu', 'format': 1, 'plan_stage': 3, 'command': command,
           'started': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
           'order': [], 'stopped': None}
    session, status, previous = None, 0, None
    try:
        previous = S1.arm_guard(proxy, out, allow_unguarded=False)
        session = Session3(proxy, allow_writes=writes, guarded=True, clock=clock)
        _hook_efi_regions(session, out)
        fn(session, out, args)
    except Stop as stop:
        out['stopped'] = {'step': stop.step, 'reason': stop.reason, **stop.detail}
        status = stop.exit_code
    except Refused as refused:
        out['stopped'] = {'step': session.step if session else 'setup', 'reason': f'refused: {refused}'}
        status = 6
    except (Q.ProxyError, OSError) as problem:
        out['stopped'] = {'step': session.step if session else 'setup',
                          'reason': f'proxy link failed: {problem} (possible bus hang: see RUNBOOK recovery)'}
        status = 5
    finally:
        if previous is not None:
            try:
                if status == 0:
                    try:
                        proxy.read64(previous['address'])
                    except Q.ProxyRemoteError:
                        pass
                out['serror_guard']['after'] = S1.serror_json(proxy.serror_state())
                proxy.serror_strict(bool(previous['flags'] & Q.SERROR_STRICT))
            except (Q.ProxyError, OSError) as problem:
                out['serror_guard']['after_error'] = str(problem)
        if session is not None:
            out['proven'] = session.proven
            out['polls'] = session.polls
            out['log'] = session.log
            after = out.get('serror_guard', {}).get('after', {})
            if status == 0 and (after.get('before') or after.get('during') or after.get('unconsumed')):
                out['stopped'] = {'step': 'end', 'reason': 'SError counters are not zero after the run'}
                status = SErrorStop.exit_code
        out.pop('_efi_regions', None)
        out.pop('_leave_halted', None)
    return status, out


def _hook_efi_regions(session, out):
    """Keep S0's parsed EFI map on out for arena_prove (Stage 1 only records the cmd-db hits)."""
    original = session.dram

    def dram(base, size):
        data = original(base, size)
        if not out.get('_efi_regions') and size > 40:
            info = getattr(session, '_bootinfo_cache', None) or session.p.bootinfo()
            session._bootinfo_cache = info
            if base == info['memory_map']:
                out['_efi_regions'] = S1.parse_efi_map(data, info['memory_map_stride'])
        return data
    session.dram = dram


def add_parsers(sub):
    """Register the Stage 3 subcommands on a16gpu.py's argparse subparsers."""
    fw_default = os.environ.get('A16_GPU_FW_DIR', DEFAULT_FW_DIRS[0])
    ar = sub.add_parser('arena', help='Stage 3: report (or --claim) the GPU arena')
    ar.add_argument('--claim', action='store_true', help='claim one from Conventional memory (needs --write)')
    ar.add_argument('--release', action='store_true', help='forget a claimed arena (needs --write; after quiesce)')
    ar.add_argument('--limit', type=lambda v: int(v, 0), default=1 << 32, help='claim below this address')
    ar.add_argument('--write', action='store_true')
    sub.add_parser('status', help='Stage 3: read-only state of arena, SMMU, GMU')
    sub.add_parser('rsc-survey', help='Stage 3: read-only apps RSC drv-2 TCS contents')
    rr = sub.add_parser('reset-reason', help='Stage 3: read-only IMEM words (AO, no GPU access)')
    rr.add_argument('--inferred-imem', action='store_true',
                    help='also read the INFERRED msm-imem words (download cookie, restart reason, boot stats); '
                         'not in a GPU session: an XPU-protected read can reset the A16')
    for name, text in (('gmu-smmu', 'P2: GMU context bank 1 for SID 5'), ('gmu-boot', 'P3: boot the GMU'),
                       ('hfi', 'P4: HFI tables and START'), ('oob', 'P4: OOB GPU_SET (GX on)'),
                       ('cp-fence', 'P5: CP init, SECVID, a fence'), ('quiesce', 'undo P5..P2')):
        p = sub.add_parser(name, help=f'Stage 3 {text}')
        p.add_argument('--write', action='store_true', help='required: this writes GPU registers and the arena')
        if name in STEP_IDS:
            p.add_argument('--stop-after', choices=STEP_IDS[name])
        if name == 'gmu-smmu':
            p.add_argument('--undo', action='store_true', help='disable CB1 and SMR1 (GMU must be quiesced)')
        if name in ('gmu-boot', 'cp-fence'):
            p.add_argument('--fw-dir', default=fw_default, help='sha-pinned firmware (A16_GPU_FW_DIR)')
        if name in ('gmu-boot', 'cp-fence'):
            p.add_argument('--no-ahb-timeout', action='store_true',
                           help='leave the CX AHB timeout registers as found (KGSL arms them; see the runbook)')
        if name == 'cp-fence':
            p.add_argument('--sqe-probe', choices=('none', 'fault'), default='none',
                           help='fault: start the SQE with every CB0 VA unmapped and report its first fetch')
            p.add_argument('--no-aperture', action='store_true',
                           help='skip qcom_scm_set_gpu_smmu_aperture(0) (Linux makes it before the SQE start)')
            p.add_argument('--prestart-faults-ok', action='store_true',
                           help='start the SQE although an SMMU fault is already recorded')
            p.add_argument('--sqe-samples', type=int, default=4, help='read-only samples after CP_SQE_CNTL = 1')
            p.add_argument('--va-wide', action='store_true',
                           help='CB0 T0SZ 30 (GPU VA up to 16 GiB) for a Snapintosh ADRENO_VA_WIDE monitor; give it to '
                                'every cp-fence of this GMU boot and to adopt')
            p.add_argument('--x290', action='store_true',
                           help='opt-in X2-90 hw_init (a16gpu_x290.py): refuses unless the slice fuse reads 0xf after '
                                'this boot\'s TZ GPU init; adds a8xx_set_ubwc_config for glymur; give it to every '
                                'cp-fence of this GMU boot')
        if name == 'gmu-boot':
            p.add_argument('--init-timeout', type=float, default=R.GMU_INIT_TIMEOUT_S)
            p.add_argument('--force-smc', action='store_true', help='repeat the TZ SMC even if done this boot')
            p.add_argument('--host-bw', action='store_true', help='also vote DDR BCMs from the host (INFERRED)')
            p.add_argument('--rail-vote-unseen', action='store_true',
                           help='B1: vote MX/CX even when no wake TCS shows drv-2\'s current vote')
            p.add_argument('--rail-level', choices=sorted(R.RPMH_LEVELS), default='low_svs',
                           help='B1: lowest MX/CX level to vote (nom: at or above firmware\'s USB/display votes)')
        if name == 'hfi':
            p.add_argument('--max-opps', type=int, default=1, help='GPU OPPs in the TABLE (default: lowest only)')
            p.add_argument('--perf-opp', type=int, default=1,
                           help='GPU OPP the GX_BW_PERF_VOTE asks for, 1-based within the TABLE (default 1: lowest)')
            p.add_argument('--resend', action='store_true')
        if name == 'quiesce':
            p.add_argument('--leave-halted', action='store_true', help='leave GBIF halted (exit/chainload)')
    import a16gpu_p6p7
    a16gpu_p6p7.add_parsers(sub)
    import a16gpu_adopt
    a16gpu_adopt.add_parsers(sub)
    import a16gpu_render
    a16gpu_render.add_parsers(sub)
    import a16gpu_ddr
    a16gpu_ddr.add_parsers(sub)
    import a16gpu_llcc
    a16gpu_llcc.add_parsers(sub)
    import a16gpu_x290
    a16gpu_x290.add_parsers(sub)


import a16gpu_p6p7  # noqa: E402 -- P6/P7 build on everything above; registered here so run() and the CLI see them

# When a16gpu_p6p7 is the module imported first, it is still half-built here; it registers itself at its end.
COMMANDS.update(getattr(a16gpu_p6p7, 'COMMANDS', {}))
STEP_IDS.update(getattr(a16gpu_p6p7, 'STEP_IDS', {}))
import a16gpu_adopt  # noqa: E402 -- Stage 4's read-only hand-off; registers `adopt` in COMMANDS
COMMANDS.update(getattr(a16gpu_adopt, 'COMMANDS', {}))
if hasattr(a16gpu_p6p7, 'Window'):
    # Stage 6's A16 half (`render`) subclasses P6/P7 at import time, so it can only be imported once a16gpu_p6p7
    # is complete; when a16gpu_p6p7 is the module imported first (half-built here), add_parsers imports it later
    # and it registers itself in COMMANDS then.
    import a16gpu_render  # noqa: E402
    COMMANDS.update(getattr(a16gpu_render, 'COMMANDS', {}))
    STEP_IDS.update(getattr(a16gpu_render, 'STEP_IDS', {}))
import a16gpu_ddr  # noqa: E402 -- DDR/interconnect votes (ddr-survey, ddr-vote, ddr-restore); registers itself
COMMANDS.update(getattr(a16gpu_ddr, 'COMMANDS', {}))
import a16gpu_llcc  # noqa: E402 -- LLCC slices (llcc-survey, llcc-activate, llcc-deactivate, llcc-gpu); registers itself
COMMANDS.update(getattr(a16gpu_llcc, 'COMMANDS', {}))
import a16gpu_x290  # noqa: E402 -- X2-90 identity (x290-survey read-only, x290-slices); registers itself
COMMANDS.update(getattr(a16gpu_x290, 'COMMANDS', {}))
