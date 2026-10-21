#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for Stage 3 P6 (blit) and P7 (triangle): tools/a16gpu_p6p7.py.

FakeP67 extends test-a16gpu-stage3.py's FakeStage3 (the SMMU, the write-back CPU cache, the GMU, the CP that
fetches through CB0) with a CP that executes every packet P6/P7 submit:

  * the ring side of Linux's a7xx_submit: CP_THREAD_CONTROL, CP_INDIRECT_BUFFER (fetched through CB0),
    CP_EVENT_WRITE7 (events, and fence writes with WRITE_ENABLED), CP_WAIT_TIMESTAMP (it stalls until the
    value is in memory);
  * inside the IB: PKT4, CP_CONTEXT_REG_BUNCH, CP_NON_CONTEXT_REG_BUNCH into a register file (a register
    Mesa's XML does not know, or one in the kernel's CP_PROTECT list, is a violation), markers, draw-state
    disable, CP_LOAD_STATE6 (UBO descriptors direct, T#/S# indirect: fetched through CB0), CP_BLIT (the 2D
    engine: solid fill or copy, 8888 with the swaps) and CP_DRAW_INDX_OFFSET (checks the program state the
    hardware needs against the exported variants, fetches vertices, the UBO, the texture and sampler through
    CB0, and rasterises SimpleVertex + SimpleTextureFragment's semantics into the render target);
  * a colour cache (CCU): 2D and draw writes reach DRAM only on CCU_CLEAN_COLOR / CACHE_CLEAN /
    CACHE_FLUSH_TS, so a readback without the fence's clean sees stale bytes;
  * every GPU access translates through CB0; a GPU write outside the arena and the framebuffer is a
    violation, and a framebuffer write outside the rectangle's rows too;
  * a GOP framebuffer in DRAM, published in bootinfo and the EFI map, and q1n1's P_FB_SHUTDOWN.

The reference tests compare what the tool builds with Mesa's own command streams for the same operations
(tools/testdata/a16gpu-p6p7/*.rd: the gallium a8xx driver on drm-shim, X2-90, see its README.md), register
by register at each CP_BLIT and CP_DRAW, with every expected difference listed and explained.

  python3 tools/test-a16gpu-p6p7.py
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a8xx_regs as X  # noqa: E402
import a16gpu  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_p6p7 as M  # noqa: E402

_spec = importlib.util.spec_from_file_location('stage3_test', TOOLS / 'test-a16gpu-stage3.py')
T3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T3)
Q = a16gpu.q1n1proxy
REFS = TOOLS / 'testdata' / 'a16gpu-p6p7'

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


# --- register database views ------------------------------------------------------------------------------
def reg_offset(name, index=0):
    offset, _bits, _fields, stride, _length = X.REGS[name]
    return offset + index * stride


def field(name, value, fname):
    for f in X.REGS[name][2]:
        if (f[0] or 'value').lower() == fname.lower():
            return (value >> f[1]) & ((1 << (f[2] - f[1] + 1)) - 1)
    raise KeyError(fname)


def dfield(domain, n, value, fname):
    for f in X.DESC[f'{domain}_{n}'][2]:
        if f[0] and f[0].lower() == fname.lower():
            return (value >> f[1]) & ((1 << (f[2] - f[1] + 1)) - 1)
    raise KeyError(fname)


KNOWN_OFFSETS = set()
for _name, (_off, _bits, _f, _stride, _len) in X.REGS.items():
    for _i in range(_len):
        for _s in range(2 if _bits == 64 else 1):
            KNOWN_OFFSETS.add(_off + _i * _stride + _s)

PROTECTED = []
for _p in R.X285_PROTECT:
    _reg, _count = _p & 0x3FFFF, (_p >> 18) & 0x1FFF         # bit 31 is NORDWR (A6XX_PROTECT_NORDWR)
    PROTECTED.append((_reg, _reg + _count))


def protected(offset):
    return any(lo <= offset <= hi for lo, hi in PROTECTED)


def f32(bits):
    return struct.unpack('<f', struct.pack('<I', bits & 0xFFFFFFFF))[0]


# ==========================================================================================================
# The model
# ==========================================================================================================
FB_BASE, FB_W, FB_H, FB_STRIDE = 0xA0000000, 640, 360, 640          # a small GOP framebuffer, BGRX (format 1)


class FakeP67(T3.FakeStage3):
    def __init__(self, fb_format=1, fb_base=FB_BASE, fb_efi_type=0, fb_efi_pages=None, **scenario):
        super().__init__(**scenario)
        self.fb_efi_pages = fb_efi_pages
        # Error reporting the review asked for: RBBM_INT_0_STATUS (sticky, nothing clears it), and faults a
        # test injects after the Nth IB (n counts IBs from 1): {'int0': bits, 'cp_global': bits, 'drop_ib_fence': 1}
        self.int0 = 0
        self.ibs_run = 0
        self.inject = {}
        self.fb = {'base': fb_base, 'width': FB_W, 'height': FB_H, 'stride': FB_STRIDE, 'format': fb_format,
                   'size': FB_STRIDE * FB_H * 4}
        self.fb_efi_type = fb_efi_type
        self.fb_console_on = True
        self.fb_console_calls = []
        # the panel shows something before the test: a deterministic pattern in DRAM (the display reads DRAM)
        pattern = bytes((i * 7 + (i >> 11)) & 0xFF for i in range(self.fb['size']))
        self.dev_write(fb_base, pattern)
        self.fb_initial = pattern
        self.regs = {}
        self.marker_mode = None
        self.ccu = {}
        self.ubo = {'vs': [], 'fs': []}
        self.tex_state, self.samp_state = None, None
        self.p67_events = []
        self.blits, self.draws = [], []
        self.gpu_writes_fb = []
        self.stall = False
        self.fb_rect_rows = None           # set by tests that know the rectangle (framebuffer writes are checked)

    # --- proxy surface ------------------------------------------------------------------------------
    def bootinfo(self):
        info = super().bootinfo()
        info.update(fb_base=self.fb['base'], fb_size=self.fb['size'], fb_width=self.fb['width'],
                    fb_height=self.fb['height'], fb_stride=self.fb['stride'], fb_format=self.fb['format'])
        return info

    def efi(self):
        if self.fb_efi_type is None:                    # a framebuffer the EFI map does not describe
            return super().efi()
        pages = (self.fb['size'] + 4095) // 4096 if self.fb_efi_pages is None else self.fb_efi_pages
        return super().efi() + struct.pack('<IIQQQQ', self.fb_efi_type, 0, self.fb['base'], 0, pages, 0xF)

    def _gx_read(self, addr):
        if addr == R.REGS['RBBM_INT_0_STATUS'].addr:
            return self.int0
        return super()._gx_read(addr)

    def in_fb(self, addr, size):
        return self.fb['base'] <= addr and addr + size <= self.fb['base'] + self.fb['size']

    def readmem(self, addr, size, live=False):
        if self.in_fb(addr, size):
            self._before('m', addr)
            self.log.append(('m', addr, size))
            return self.cpu_read(addr, size)
        return super().readmem(addr, size, live)

    def request(self, opcode, *args):
        if opcode in (Q.Proxy.P_FB_INIT, Q.Proxy.P_FB_SHUTDOWN):
            self._before('op', 0)
            previous = int(self.fb_console_on)
            self.fb_console_on = opcode == Q.Proxy.P_FB_INIT
            self.fb_console_calls.append(self.fb_console_on)
            return previous
        return super().request(opcode, *args)

    def fb_console(self, enabled):
        return self.request(Q.Proxy.P_FB_INIT if enabled else Q.Proxy.P_FB_SHUTDOWN, 0)

    # --- GPU memory access --------------------------------------------------------------------------
    def gpu_read(self, va, size, what):
        data = self.dma_read(R.GPU_SID, va, size)
        if data is None:
            self.violations.append(('gpu', hex(va), f'{what}: CB0 does not translate the read'))
        return data

    def gpu_write(self, va, data, what):
        """A render write: through CB0 (page by page), into the CCU until a clean event."""
        at = 0
        while at < len(data):
            page_end = ((va + at) & ~0xFFF) + 0x1000
            n = min(len(data) - at, page_end - (va + at))
            pa = self.translate(R.GPU_SID, va + at, write=True, priv=False)
            if pa is None:
                self.violations.append(('gpu', hex(va + at), f'{what}: CB0 does not translate the write'))
                return False
            if self.in_arena(pa, n):
                pass
            elif self.in_fb(pa, n):
                row = (pa - self.fb['base']) // (self.fb['stride'] * 4)
                self.gpu_writes_fb.append((pa, n))
                if self.fb_rect_rows and not self.fb_rect_rows[0] <= row < self.fb_rect_rows[1]:
                    self.violations.append(('gpu', hex(pa), f'{what}: framebuffer write outside the rectangle rows'))
            else:
                self.violations.append(('gpu', hex(pa), f'{what}: GPU write outside the arena and the framebuffer'))
            self.ccu[pa] = bytes(data[at:at + n])
            at += n
        return True

    def ccu_clean(self):
        for pa, blob in self.ccu.items():
            self.dev_write(pa, blob)
        self.ccu = {}

    # --- the CP ---------------------------------------------------------------------------------------
    def _cp_run(self, wptr):
        if not self.cp_running or not self.s3['cp_executes']:
            return
        regs = R.REGS
        base = self.gx.get(regs['CP_RB_BASE'].addr, 0) | self.gx.get(regs['CP_RB_BASE_HI'].addr, 0) << 32
        while self.rptr < wptr and not self.stall:
            header = struct.unpack('<I', self.dma_read(R.GPU_SID, base + 4 * self.rptr, 4) or b'\0' * 4)[0]
            count, opcode = header & 0x3FFF, header >> 16 & 0x7F
            if header >> 28 != 7 or R.pkt7(opcode, count) != header:
                self.violations.append(('cp', hex(header), 'bad PKT7 header on the ring'))
                self.cp_running = False
                return
            body = list(struct.unpack(f'<{count}I', self.dma_read(R.GPU_SID, base + 4 * (self.rptr + 1), 4 * count)
                                      or b'\0' * 4 * count))
            if not self.ring_packet(opcode, body):
                break                                  # stalled (CP_WAIT_TIMESTAMP): rptr stays
            self.rptr += 1 + count
        shadow = self.gx.get(regs['CP_RB_RPTR_ADDR'].addr, 0) | self.gx.get(regs['CP_RB_RPTR_ADDR_HI'].addr, 0) << 32
        self.dma_write(R.GPU_SID, shadow, struct.pack('<I', self.rptr))

    def ring_packet(self, opcode, body):
        if opcode == R.CP_ME_INIT:
            self.me_init = True
        elif opcode == R.CP_MEM_WRITE:
            if not self.me_init:
                self.violations.append(('cp', hex(opcode), 'CP_MEM_WRITE before CP_ME_INIT'))
                return True
            self.dma_write(R.GPU_SID, body[0] | body[1] << 32, struct.pack(f'<{len(body) - 2}I', *body[2:]))
        elif opcode == R.CP_THREAD_CONTROL:
            pass
        elif opcode == R.CP_INDIRECT_BUFFER:
            if not self.me_init:
                self.violations.append(('cp', hex(opcode), 'IB before CP_ME_INIT'))
                return True
            self.run_ib(body[0] | body[1] << 32, body[2] & 0xFFFFF)
        elif opcode == R.CP_EVENT_WRITE7:
            self.event(body)
        elif opcode == R.CP_WAIT_TIMESTAMP:
            data = self.gpu_read(body[1] | body[2] << 32, 4, 'CP_WAIT_TIMESTAMP')
            if data is None or struct.unpack('<I', data)[0] < body[3]:
                self.stall = True
                return False
        else:
            self.violations.append(('cp', hex(opcode), 'unexpected opcode on the ring'))
        return True

    def event(self, body):
        w0 = body[0]
        ev = w0 & 0xFF
        name = next((k for k, v in X.ENUMS['vgt_event_type'].items() if v == ev), hex(ev))
        self.p67_events.append(name)
        if name in ('CCU_CLEAN_COLOR', 'CACHE_CLEAN', 'CACHE_FLUSH_TS'):
            self.ccu_clean()
        if w0 >> 27 & 1:                                  # WRITE_ENABLED: 32-bit payload to RAM
            if name == 'CACHE_FLUSH_TS' and self.inject.get(self.ibs_run, {}).get('drop_ib_fence'):
                return                                    # the IB's own fence never lands (injected)
            if len(body) < 4:
                self.violations.append(('cp', hex(w0), 'event write without address and payload'))
                return
            self.dma_write(R.GPU_SID, body[1] | body[2] << 32, struct.pack('<I', body[3]))

    def run_ib(self, va, dwords):
        self.ibs_run += 1
        fault = self.inject.get(self.ibs_run, {})
        if fault.get('int0'):
            self.int0 |= fault['int0']
        if fault.get('cp_global'):
            addr = R.REGS['CP_INTERRUPT_STATUS_GLOBAL'].addr
            self.gx[addr] = self.gx.get(addr, 0) | fault['cp_global']
        blob = self.gpu_read(va, 4 * dwords, 'IB fetch')
        if blob is None:
            self.stall = True
            return
        words = struct.unpack(f'<{dwords}I', blob)
        i = 0
        while i < len(words):
            h = words[i]
            if h >> 28 == 4:
                cnt, off = h & 0x7F, (h >> 8) & 0x3FFFF
                if M.pkt4_header(off, cnt) != h:
                    self.violations.append(('cp', hex(h), 'bad PKT4 header'))
                    return
                for k in range(cnt):
                    self.write_reg(off + k, words[i + 1 + k], 'pkt4')
                i += 1 + cnt
                continue
            if h >> 28 != 7:
                self.violations.append(('cp', hex(h), 'bad packet header in the IB'))
                return
            cnt, op = h & 0x3FFF, (h >> 16) & 0x7F
            if R.pkt7(op, cnt) != h:
                self.violations.append(('cp', hex(h), 'bad PKT7 parity in the IB'))
                return
            body = list(words[i + 1:i + 1 + cnt])
            self.ib_packet(op, body)
            i += 1 + cnt

    def write_reg(self, offset, value, how):
        if offset not in KNOWN_OFFSETS:
            self.violations.append(('cp', hex(offset), f'{how} write to a register Mesa\'s XML does not list for A8XX'))
        if protected(offset):
            self.violations.append(('cp', hex(offset), f'{how} write to a CP_PROTECT range (x285_protect)'))
        self.regs[offset] = value

    def r(self, name, index=0):
        return self.regs.get(reg_offset(name, index), 0)

    def r64(self, name, index=0):
        return self.r(name, index) | self.regs.get(reg_offset(name, index) + 1, 0) << 32

    def ib_packet(self, op, body):
        if op == R.CP_CONTEXT_REG_BUNCH:
            for k in range(0, len(body), 2):
                self.write_reg(body[k], body[k + 1], 'crb')
        elif op == R.CP_NON_CONTEXT_REG_BUNCH:
            if body[:2] != [1, 0]:
                self.violations.append(('cp', hex(op), 'NCRB without its 1, 0 header'))
            for k in range(2, len(body), 2):
                self.write_reg(body[k], body[k + 1], 'ncrb')
        elif op == R.CP_EVENT_WRITE7:
            self.event(body)
        elif op == R.CP_SET_MARKER:
            self.marker_mode = body[0] & 0x7F
        elif op in (R.CP_SET_MODE, R.CP_THREAD_CONTROL, R.CP_WAIT_FOR_IDLE, R.CP_CCHE_INVALIDATE,
                    R.CP_SKIP_IB2_ENABLE_GLOBAL, R.CP_SKIP_IB2_ENABLE_LOCAL, R.CP_SET_VISIBILITY_OVERRIDE):
            pass
        elif op == R.CP_SET_DRAW_STATE:
            if body[0] != M.pkt_word('CP_SET_DRAW_STATE__0', disable_all_groups=True):
                self.violations.append(('cp', hex(body[0]), 'a draw state group the model does not run'))
        elif op in (R.CP_LOAD_STATE6_GEOM, R.CP_LOAD_STATE6_FRAG):
            self.load_state(op, body)
        elif op == R.CP_BLIT:
            self.blit(body)
        elif op == R.CP_DRAW_INDX_OFFSET:
            self.draw(body)
        else:
            self.violations.append(('cp', hex(op), 'unexpected opcode in the IB'))

    def load_state(self, op, body):
        w0 = body[0]
        stype, src, block, n = (w0 >> 14) & 3, (w0 >> 16) & 3, (w0 >> 18) & 0xF, w0 >> 22
        ST, SS, SB = X.ENUMS['a6xx_state_type'], X.ENUMS['a6xx_state_src'], X.ENUMS['a6xx_state_block']
        if stype == ST['ST6_UBO'] and src == SS['SS6_DIRECT']:
            stage = 'vs' if block == SB['SB6_VS_SHADER'] and op == R.CP_LOAD_STATE6_GEOM else \
                'fs' if block == SB['SB6_FS_SHADER'] and op == R.CP_LOAD_STATE6_FRAG else None
            if stage is None or len(body) != 3 + 2 * n:
                self.violations.append(('cp', hex(w0), 'UBO state for an unexpected block'))
                return
            self.ubo[stage] = [(body[3 + 2 * i] | (body[4 + 2 * i] & 0x1FFFF) << 32, body[4 + 2 * i] >> 17)
                               for i in range(n)]
        elif block == SB['SB6_FS_TEX'] and src == SS['SS6_INDIRECT'] and op == R.CP_LOAD_STATE6_FRAG:
            addr = body[1] | body[2] << 32
            size = 16 if stype == ST['ST6_SHADER'] else 64
            data = self.gpu_read(addr, size * n, 'T#/S# state')
            if data is None:
                return
            if stype == ST['ST6_SHADER']:
                self.samp_state = struct.unpack('<4I', data[:16])
            else:
                self.tex_state = struct.unpack('<16I', data[:64])
        else:
            self.violations.append(('cp', hex(w0), 'unexpected CP_LOAD_STATE6'))

    # --- the 2D engine ----------------------------------------------------------------------------------
    SWAPS = {0: (0, 1, 2, 3), 1: (2, 1, 0, 3)}         # WZYX: R,G,B,A in memory; WXYZ: B,G,R,A

    def blit(self, body):
        op = body[0] & 0xF
        rb, gras = self.r('RB_A2D_BLT_CNTL'), self.r('GRAS_A2D_BLT_CNTL')
        fmt8888 = X.ENUMS['a6xx_format']['FMT6_8_8_8_8_UNORM']
        problems = []
        if op != X.ENUMS['cp_blit_cmd']['BLIT_OP_SCALE']:
            problems.append('CP_BLIT op')
        if self.marker_mode != X.ENUMS['a6xx_marker']['RM6_BLIT2DSCALE']:
            problems.append('CP_BLIT outside RM6_BLIT2DSCALE')
        if rb != gras or field('RB_A2D_BLT_CNTL', rb, 'COLOR_FORMAT') != fmt8888 or \
                field('RB_A2D_BLT_CNTL', rb, 'MASK') != 0xF or field('RB_A2D_BLT_CNTL', rb, 'IFMT') != \
                X.ENUMS['a6xx_2d_ifmt']['R2D_UNORM8']:
            problems.append(f'RB/GRAS_A2D_BLT_CNTL {rb:#x}/{gras:#x}')
        info = self.r('RB_A2D_DEST_BUFFER_INFO')
        if field('RB_A2D_DEST_BUFFER_INFO', info, 'COLOR_FORMAT') != fmt8888 or \
                field('RB_A2D_DEST_BUFFER_INFO', info, 'TILE_MODE') != 0:
            problems.append(f'RB_A2D_DEST_BUFFER_INFO {info:#x}')
        dswap = self.SWAPS.get(field('RB_A2D_DEST_BUFFER_INFO', info, 'COLOR_SWAP'))
        dst, dpitch = self.r64('RB_A2D_DEST_BUFFER_BASE'), (self.r('RB_A2D_DEST_BUFFER_PITCH') & 0xFFFF) << 6
        if dst & 0x3F or dswap is None:
            problems.append('destination base not 64-byte aligned or swap not 8888')
        tl, br = self.r('GRAS_A2D_DEST_TL'), self.r('GRAS_A2D_DEST_BR')
        x0, y0, x1, y1 = tl & 0x3FFF, tl >> 16 & 0x3FFF, br & 0x3FFF, br >> 16 & 0x3FFF
        if problems:
            self.violations.append(('blit', hex(dst), '; '.join(problems)))
            return
        solid = field('RB_A2D_BLT_CNTL', rb, 'SOLID_COLOR')
        record = {'dst': dst, 'rect': (x0, y0, x1, y1), 'solid': solid}
        if solid:
            rgba = [self.r(f'RB_A2D_CLEAR_COLOR_DW{i}') & 0xFF for i in range(4)]
            pixel = bytes(rgba[dswap[c]] for c in range(4))
            row = pixel * (x1 - x0 + 1)
            for y in range(y0, y1 + 1):
                self.gpu_write(dst + y * dpitch + 4 * x0, row, 'CP_BLIT fill')
        else:
            sinfo = self.r('TPL1_A2D_SRC_TEXTURE_INFO')
            sswap = self.SWAPS.get(field('TPL1_A2D_SRC_TEXTURE_INFO', sinfo, 'COLOR_SWAP'))
            src = self.r64('TPL1_A2D_SRC_TEXTURE_BASE')
            spitch = field('TPL1_A2D_SRC_TEXTURE_PITCH', self.r('TPL1_A2D_SRC_TEXTURE_PITCH'), 'PITCH')
            sx0, sx1 = self.r('GRAS_A2D_SRC_XMIN') >> 8, self.r('GRAS_A2D_SRC_XMAX') >> 8
            sy0, sy1 = self.r('GRAS_A2D_SRC_YMIN') >> 8, self.r('GRAS_A2D_SRC_YMAX') >> 8
            if field('TPL1_A2D_SRC_TEXTURE_INFO', sinfo, 'COLOR_FORMAT') != fmt8888 or sswap is None or \
                    (sx1 - sx0, sy1 - sy0) != (x1 - x0, y1 - y0):
                self.violations.append(('blit', hex(src), 'copy source format/swap/size'))
                return
            for dy in range(y1 - y0 + 1):
                data = self.gpu_read(src + (sy0 + dy) * spitch + 4 * sx0, 4 * (sx1 - sx0 + 1), 'CP_BLIT source')
                if data is None:
                    return
                out = bytearray()
                for k in range(0, len(data), 4):
                    rgba = [0] * 4
                    for c in range(4):
                        rgba[sswap[c]] = data[k + c]
                    out += bytes(rgba[dswap[c]] for c in range(4))
                self.gpu_write(dst + (y0 + dy) * dpitch + 4 * x0, bytes(out), 'CP_BLIT copy')
            record['src'] = src
        self.blits.append(record)

    # --- the draw -----------------------------------------------------------------------------------------
    def draw(self, body):
        """Checks what the hardware needs from the program state against the exported variants, then renders
        SimpleVertex + SimpleTextureFragment from the state as the GPU would see it."""
        sh, _golden, _d = M.p7_assets(check=False)
        vs, fs = sh['vs'], sh['fs']
        p = []
        if self.marker_mode != X.ENUMS['a6xx_marker']['RM6_DIRECT_RENDER']:
            p.append('draw outside RM6_DIRECT_RENDER')
        w0 = body[0]
        if w0 & 0x3F != X.ENUMS['pc_di_primtype']['DI_PT_TRILIST'] or (w0 >> 6 & 3) != \
                X.ENUMS['pc_di_src_sel']['DI_SRC_SEL_AUTO_INDEX'] or body[1] != 1:
            p.append(f'CP_DRAW_INDX_OFFSET {w0:#x}')
        nverts = body[2]
        # program: code at the bases is the exported code, sizes and footprints hold the variants
        for stage, v, prefix in (('vs', vs, 'SP_VS'), ('fs', fs, 'SP_PS')):
            code = bytes.fromhex(v['code_hex'])
            size = self.r(f'{prefix}_INSTR_SIZE')
            got = self.gpu_read(self.r64(f'{prefix}_BASE'), 128 * size, f'{stage} code') if size else None
            if size != v['instrlen'] or got is None or got[:len(code)] != code:
                p.append(f'{stage} code or instruction size')
            cntl = self.r(f'{prefix}_CNTL_0')
            if field(f'{prefix}_CNTL_0', cntl, 'FULLREGFOOTPRINT') < v['info']['max_reg'] + 1 or \
                    field(f'{prefix}_CNTL_0', cntl, 'BRANCHSTACK') < v['branchstack_hw'] or \
                    field(f'{prefix}_CNTL_0', cntl, 'MERGEDREGS') != v['mergedregs']:
                p.append(f'{prefix}_CNTL_0 {cntl:#x}')
            const = self.r(f'{prefix}_CONST_CONFIG')
            if field(f'{prefix}_CONST_CONFIG', const, 'CONSTLEN') * 4 < v['constlen'] or \
                    not field(f'{prefix}_CONST_CONFIG', const, 'ENABLED'):
                p.append(f'{prefix}_CONST_CONFIG {const:#x}')
        psc = self.r('SP_PS_CONFIG')
        if field('SP_PS_CONFIG', psc, 'NTEX') < 1 or field('SP_PS_CONFIG', psc, 'NSAMP') < 1:
            p.append(f'SP_PS_CONFIG {psc:#x} enables no T#/S# although the FS samples')
        # linkage: VFD destinations, VS outputs, VPC locations, FS barycentrics and output
        attrs = [i for i in vs['inputs'] if not i['sysval']]
        for n, a in enumerate(attrs):
            d = self.r('VFD_DEST_CNTL_INSTR', n)
            if field('VFD_DEST_CNTL_INSTR', d, 'REGID') != a['regid'] or \
                    field('VFD_DEST_CNTL_INSTR', d, 'WRITEMASK') != a['compmask']:
                p.append(f'VFD_DEST_CNTL[{n}] {d:#x}')
        outs = {}
        for i in range(8):
            w = self.r('SP_VS_OUTPUT_REG', i)
            locs = self.r('SP_VS_VPC_DEST_REG', i // 2)
            for half in range(2):
                k = 2 * i + half
                if k < field('SP_VS_OUTPUT_CNTL', self.r('SP_VS_OUTPUT_CNTL'), 'OUT'):
                    outs[(w >> (16 * half)) & 0xFF] = (locs >> (8 * (k % 4))) & 0xFF
        vs_pos = next(o['regid'] for o in vs['outputs'] if o['slot'] == M.SLOT_POS)
        vs_var = next(o['regid'] for o in vs['outputs'] if o['slot'] == M.SLOT_VAR0)
        fs_var = next(i for i in fs['inputs'] if i['slot'] == M.SLOT_VAR0)
        vpc = self.r('VPC_VS_CNTL')
        if outs.get(vs_pos) != field('VPC_VS_CNTL', vpc, 'POSITIONLOC') or outs.get(vs_var) != fs_var['inloc']:
            p.append(f'VS outputs {outs} vs positionloc {field("VPC_VS_CNTL", vpc, "POSITIONLOC")} and FS inloc')
        ij = next(i['regid'] for i in fs['inputs'] if i['sysval'])
        if field('SP_REG_PROG_ID_1', self.r('SP_REG_PROG_ID_1'), 'IJ_PERSP_PIXEL') != ij:
            p.append('SP_REG_PROG_ID_1 IJ_PERSP_PIXEL')
        fs_out = next(o['regid'] for o in fs['outputs'] if o['slot'] == M.FRAG_RESULT_DATA0)
        if field('SP_PS_OUTPUT_REG', self.r('SP_PS_OUTPUT_REG', 0), 'REGID') != fs_out:
            p.append('SP_PS_OUTPUT_REG[0]')
        if p:
            self.violations.append(('draw', 'program', '; '.join(p)))
            return
        # vertex fetch through CB0
        verts = []
        for n in range(nverts):
            vtx = []
            for i in range(len(attrs)):
                instr = self.r('VFD_FETCH_INSTR_INSTR', i)
                idx, off = field('VFD_FETCH_INSTR_INSTR', instr, 'IDX'), field('VFD_FETCH_INSTR_INSTR', instr, 'OFFSET')
                if field('VFD_FETCH_INSTR_INSTR', instr, 'FORMAT') != X.ENUMS['a6xx_format']['FMT6_32_32_FLOAT']:
                    self.violations.append(('draw', hex(instr), 'vertex format'))
                    return
                vb, size = self.r64('VFD_VERTEX_BUFFER_BASE', idx), self.r('VFD_VERTEX_BUFFER_SIZE', idx)
                stride = self.r('VFD_VERTEX_BUFFER_STRIDE', idx)
                if n * stride + off + 8 > size:
                    self.violations.append(('draw', hex(vb), 'vertex fetch beyond VFD_VERTEX_BUFFER_SIZE'))
                    return
                data = self.gpu_read(vb + n * stride + off, 8, 'vertex fetch')
                if data is None:
                    return
                vtx += list(struct.unpack('<2f', data))
            verts.append(vtx)
        # UBO 1 (mvp) through the VS UBO descriptor
        ubos = self.ubo['vs']
        if len(ubos) <= M.P7['mvp_buffer_index'] or ubos[M.P7['mvp_buffer_index']][1] * 16 < 64:
            self.violations.append(('draw', 'ubo', f'VS UBO descriptors {ubos}'))
            return
        mvp = struct.unpack('<16f', self.gpu_read(ubos[M.P7['mvp_buffer_index']][0], 64, 'UBO 1') or bytes(64))
        # texture and sampler from the loaded T#0/S#0
        if self.tex_state is None or self.samp_state is None:
            self.violations.append(('draw', 'tex', 'no T#/S# state loaded'))
            return
        t = self.tex_state
        tbase = t[0] & ~0x3F | (dfield('A8XX_TEX_MEMOBJ', 1, t[1], 'BASE_HI') << 32)
        tw, th = dfield('A8XX_TEX_MEMOBJ', 2, t[2], 'WIDTH'), dfield('A8XX_TEX_MEMOBJ', 2, t[2], 'HEIGHT')
        tpitch = dfield('A8XX_TEX_MEMOBJ', 6, t[6], 'TEX_LINE_OFFSET') // 8
        tfmt, tswap = dfield('A8XX_TEX_MEMOBJ', 3, t[3], 'FMT'), dfield('A8XX_TEX_MEMOBJ', 3, t[3], 'SWAP')
        if tfmt != X.ENUMS['a6xx_format']['FMT6_8_8_8_8_UNORM'] or tswap not in self.SWAPS or \
                dfield('A8XX_TEX_MEMOBJ', 4, t[4], 'TILE_MODE') != 0:
            self.violations.append(('draw', hex(t[3]), 'texture format'))
            return
        texels = self.gpu_read(tbase, tpitch * th, 'texture')
        s0 = self.samp_state[0]
        clamp = X.ENUMS['a6xx_tex_clamp']['A6XX_TEX_CLAMP_TO_EDGE']
        linear = X.ENUMS['a6xx_tex_filter']['A6XX_TEX_LINEAR']
        if texels is None or dfield('A8XX_TEX_SAMP', 0, s0, 'WRAP_S') != clamp or \
                dfield('A8XX_TEX_SAMP', 0, s0, 'WRAP_T') != clamp or dfield('A8XX_TEX_SAMP', 0, s0, 'XY_MAG') != linear \
                or dfield('A8XX_TEX_SAMP', 0, s0, 'XY_MIN') != linear:
            self.violations.append(('draw', hex(s0), 'sampler state or texture fetch'))
            return
        # render target
        info = self.r('RB_MRT_BUF_INFO', 0)
        rt, pitch = self.r64('RB_MRT_BASE', 0), self.r('RB_MRT_PITCH', 0) << 6
        swap = self.SWAPS.get(field('RB_MRT_BUF_INFO', info, 'COLOR_SWAP'))
        if field('RB_MRT_BUF_INFO', info, 'COLOR_FORMAT') != X.ENUMS['a6xx_format']['FMT6_8_8_8_8_UNORM'] or swap is None:
            self.violations.append(('draw', hex(info), 'render target format'))
            return
        xs, xo = f32(self.r('GRAS_CL_VIEWPORT_XSCALE')), f32(self.r('GRAS_CL_VIEWPORT_XOFFSET'))
        ys, yo = f32(self.r('GRAS_CL_VIEWPORT_YSCALE')), f32(self.r('GRAS_CL_VIEWPORT_YOFFSET'))
        tl, br = self.r('GRAS_SC_VIEWPORT_SCISSOR_TL'), self.r('GRAS_SC_VIEWPORT_SCISSOR_BR')
        sx0, sy0, sx1, sy1 = tl & 0xFFFF, tl >> 16, br & 0xFFFF, br >> 16
        # SimpleVertex: pos = mvp * float4(pos, 0, 1); tex passed through. Window = NDC * scale + offset.
        win = []
        for vx, vy, u, v in verts:
            cx = [mvp[c] * vx + mvp[4 + c] * vy + mvp[12 + c] for c in range(4)]
            win.append((cx[0] / cx[3] * xs + xo, cx[1] / cx[3] * ys + yo, u, v))
        self.draws.append({'verts': win})

        def texel(x, y):
            x, y = min(max(x, 0), tw - 1), min(max(y, 0), th - 1)
            px = texels[y * tpitch + 4 * x:y * tpitch + 4 * x + 4]
            rgba = [0] * 4
            for c in range(4):
                rgba[self.SWAPS[tswap][c]] = px[c]
            return rgba

        def sample(u, v):
            x, y = u * tw - 0.5, v * th - 0.5
            x0, y0 = math.floor(x), math.floor(y)
            fx, fy = x - x0, y - y0
            a, b, c, d = texel(x0, y0), texel(x0 + 1, y0), texel(x0, y0 + 1), texel(x0 + 1, y0 + 1)
            return [((a[k] * (1 - fx) + b[k] * fx) * (1 - fy) + (c[k] * (1 - fx) + d[k] * fx) * fy) / 255.0
                    for k in range(4)]

        def edge(a, b, px, py):
            return (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
        for tri in range(0, len(win) - 2, 3):
            a, b, c = win[tri:tri + 3]
            area = edge(a, b, c[0], c[1])
            if area == 0:
                continue
            for y in range(sy0, sy1 + 1):
                row = bytearray()
                xs_hit = []
                for x in range(sx0, sx1 + 1):
                    px, py = x + 0.5, y + 0.5
                    w0, w1, w2 = edge(b, c, px, py) / area, edge(c, a, px, py) / area, edge(a, b, px, py) / area
                    if w0 >= 0 and w1 >= 0 and w2 >= 0:
                        u = w0 * a[2] + w1 * b[2] + w2 * c[2]
                        v = w0 * a[3] + w1 * b[3] + w2 * c[3]
                        rgba = [M.unorm8(k) for k in sample(u, v)]
                        xs_hit.append((x, bytes(rgba[swap[k]] for k in range(4))))
                for x, pixel in xs_hit:
                    self.gpu_write(rt + y * pitch + 4 * x, pixel, 'draw')


def run(fake, command, **kw):
    return S3.run(fake, command, S3.Args(**kw))


def ready(fake, fw):
    T3.happy_path(fake, fw)


def no_violation(fake, label):
    check(not fake.violations, f'{label}: model saw no violation ({fake.violations[:4]})')


def arena_bytes(fake, name, offset=0, length=None):
    off, size = R.ARENA_LAYOUT[name]
    return fake.dev_read(T3.ARENA + off + offset, size - offset if length is None else length)


# ==========================================================================================================
# Tests
# ==========================================================================================================
def test_encoding():
    # PKT4 header: the value Mesa emitted for SP_UPDATE_CNTL (reference capture: 48ab1f01)
    check(M.pkt4_header(0xAB1F, 1) == 0x48AB1F01, 'pkt4 header with odd parities (pm4_pkt4_hdr)')
    check(M.reg('RB_A2D_BLT_CNTL', rotate='ROTATE_0', solid_color=True, color_format='FMT6_8_8_8_8_UNORM', mask=0xF,
                ifmt='R2D_UNORM8') == [(0x8C00, 0x00F03080)], 'RB_A2D_BLT_CNTL solid 8888 = 0x00f03080 (capture)')
    check(M.reg('TPL1_A2D_SRC_TEXTURE_PITCH', pitch=1024) == [(0xB2C4, 0x2000)], 'TPL1 2D source pitch field')
    check(M.reg('RB_MRT_BASE', 3, value=0x123456789C0) == [(0x8825 + 24, 0x456789C0), (0x8826 + 24, 0x123)],
          '64-bit array register, lo/hi')
    check(M.reg('GRAS_SU_POINT_MINMAX', min=1.0, max=1.0) == [(0x820A, 0x00100010)], 'ufixed radix 4 point size')
    check(M.guardband(48.0, 48.0) == 511 and M.guardband(32.0, -32.0) == 511, 'fd_calc_guardband for 96x64')
    for bad in (lambda: M.reg('RB_A2D_BLT_CNTL', nosuch=1), lambda: M.reg('SP_VS_CONST_CONFIG', constlen=3),
                lambda: M.reg('GRAS_A2D_DEST_TL', x=1 << 14), lambda: M.reg('VFD_DEST_CNTL_INSTR', 32)):
        try:
            bad()
            check(False, 'bad packing refused')
        except S3.Refused:
            check(True, 'bad packing refused (unknown field, a value that is not a multiple of 1<<shr, an '
                        'overflowing field, an array index out of range)')
    events = M.CS(M.Window(T3.ARENA), 't')
    events.event('CACHE_FLUSH_TS', 0x10200060, 7)
    check(events.w == [0x70460004, 0x08000004, 0x10200060, 0, 7], 'CACHE_FLUSH_TS fence: 0x08000004 as Mesa')
    # the gmem cache config Mesa computed for the X2-90 (capture: RB_CCU_CACHE_CNTL 0xd0c01240 and the buffers)
    cs = M.CS(M.Window(T3.ARENA), 't')
    M.emit_gmem_cache_cntl_sysmem(cs)
    check(cs.w[1:] == [0x88E5, 0xD0C01240, 0x9314, 0x20000, 0x9315, 0x13C0000, 0x9B16, 0x20000, 0x9316, 0x10000,
                       0x9317, 0x1340000, 0x9B17, 0x10000, 0x9319, 0x14C0000, 0x9318, 0x8000, 0x9B18, 0x8000],
          'fd6_calc_gmem_cache_offsets for 8 CCUs and 21 MiB GMEM gives the capture\'s values')


def test_windows():
    win = M.Window(T3.ARENA)
    for bad in (lambda: win.addr('p6_dst', 0, R.ARENA_LAYOUT['p6_dst'][1] + 1, True, 'x'),
                lambda: win.addr('p6_src', 0, 16, True, 'x'),                 # read-only for the GPU
                lambda: win.addr('framebuffer', 0, 16, True, 'x'),            # not mapped in this step
                lambda: win.addr('gmu_hfi', 0, 16, False, 'x')):              # not a render region at all
            try:
                bad()
                check(False, 'window refused')
            except S3.Refused:
                check(True, 'Window.addr refuses: past a region, a write to a read-only region, the framebuffer '
                            'outside its step, anything that is not a render region')
    fb = {'base': FB_BASE, 'size': FB_STRIDE * FB_H * 4, 'pitch': FB_STRIDE * 4, 'width': FB_W, 'height': FB_H}
    span = M.fb_window_span(fb, (100, 10, 64, 20))
    check(span['pa'] == FB_BASE + 10 * 2560 - (10 * 2560) % 4096 and span['size'] % 4096 == 0
          and span['pa'] + span['size'] >= FB_BASE + 30 * 2560, 'the span is whole pages covering the rows')
    try:
        M.fb_window_span(dict(fb, size=fb['size'] - 4096), (0, FB_H - 4, 16, 4))
        check(False, 'span past the framebuffer refused')
    except S3.Refused:
        check(True, 'a rectangle whose pages would pass the framebuffer end is refused')
    tables = M.build_cb0_tables(T3.ARENA)
    base = S3.build_tables(T3.ARENA, 'gpu')
    check(tables.image()[1][:len(base.image()[1])][:4096] == base.image()[1][:4096] and tables.root == base.root,
          'the render window adds tables after cp-fence\'s: CB0\'s root and first pages keep their place')
    blob = {pa: bytes(t) for pa, t in tables.tables.items()}
    read = lambda a: struct.unpack_from('<Q', blob[a & ~0xFFF], a & 0xFFF)[0]
    ttbr = S3.ttbr_for(tables, R.GPU_CB)
    for name, va, pa, size, attrs in M.render_mappings(T3.ARENA):
        for off in (0, size - 4):
            hit = S3.walk(read, ttbr, va + off)
            check(hit and hit[0] == pa + off and bool(hit[1] & R.PTE_AP_RDONLY) == bool(attrs & R.PTE_AP_RDONLY),
                  f'CB0 maps {name} {va + off:#x} -> arena {pa + off:#x} with its permission')
    check(S3.walk(read, ttbr, R.FB_VA) is None, 'no framebuffer page without a span')


def test_p7_assets():
    sh, golden, digest = M.p7_assets()
    check(digest == M.P7_SHADERS_SHA256 and hashlib.sha256(golden).hexdigest() == M.P7_GOLDEN_SHA256,
          'P7 shaders and the M3 Max golden match their pins')
    problems, findings, sampling = M.variant_checks(sh)
    check(not problems and sampling == {'ntex': 1, 'nsamp': 1, 'pixlod': True} and len(findings) == 2,
          f'SimpleVertex/SimpleTextureFragment are inside the port; Stage 6 findings recorded ({findings})')
    check(sh['vs']['sidecar']['key_inputs']['entry'] == 'SimpleVertex' and
          sh['fs']['sidecar']['key_inputs']['entry'] == 'SimpleTextureFragment' and
          sh['vs']['sidecar']['compiler_id'].startswith('air2ir3-src-c1d8fcf04b9eef02'),
          'the variants are the Stage 6 compiler\'s SkyLight SimpleVertex and SimpleTextureFragment')
    for mutate, why in ((lambda v: v['vs'].update(pvtmem_size=64), 'private memory'),
                        (lambda v: v['fs']['const_state'].update(driver_params_ubo=1), 'driver UBO'),
                        (lambda v: v['vs'].update(constlen=0), 'constlen below the pushed UBO range'),
                        (lambda v: v['fs'].update(bindless=[1, 0, 0, 0]), 'bindless'),
                        (lambda v: v.update(chip_id='0x43050a01'), 'another chip'),
                        (lambda v: v['vs'].update(code_hex='00' * 0x1008, instrlen=33), 'VS code past its 4 KiB slot'),
                        (lambda v: v['fs'].update(instrlen=0), 'an FS instruction size that fetches nothing')):
        variant = json.loads(json.dumps(sh))
        mutate(variant)
        check(M.variant_checks(variant)[0], f'a variant with {why} is refused')
    # the golden is what an exact rasteriser of the same scene gives, within one code
    tex, W, H = M.p7_texture(), M.P7['width'], M.P7['height']
    check(len(golden) == W * H * 4 and golden[:4] == M.bgra(M.P7['clear']), 'golden: 96x64, clear colour at 0,0')


def test_p6():
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        mark = len(fake.log)
        status, out = run(fake, 'blit')
        check(status == 0, f'blit completes ({out.get("stopped")})')
        no_violation(fake, 'blit')
        check(out['order'][-3:] == ['p6_window', 'p6_clear', 'p6_copy'] and out['p6_clear']['bad_pixels'] == 0
              and out['p6_copy']['identical'] and out['p6_clear']['guards_intact'], 'P6 in the arena: clear, copy')
        check([b['solid'] for b in fake.blits] == [1, 0], 'the model ran one fill and one copy')
        target = arena_bytes(fake, 'p6_dst', M.P6['guard'], M.P6['pitch'] * M.P6['size'])
        check(target == M.p6_source(), 'the arena target holds the source image (model DRAM)')
        check(fake.dev_read(FB_BASE, fake.fb['size']) == fake.fb_initial and not fake.fb_console_calls,
              'without --to-framebuffer the framebuffer and q1n1\'s console are untouched')
        subs = out['submissions']
        check(len(subs) == 2 and all(int(sub['fence'], 16) == int(sub['fence_read'], 16) == int(sub['bv_fence'], 16)
                                     == int(sub['ib_fence'], 16) for sub in subs),
              'each submission: ring fence, BV fence and the IB\'s own fence all carry the value')
        check(out['p6_clear']['color_bgra'] == '0040ffff', 'clear colour RGBA (1, 0.25, 0, 1) is BGRA 00 40 ff ff')
        writes = [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')]
        check(all(fake.in_arena(a, n) for _op, a, n in writes), 'every CPU write of blit is an arena write')
        # the IB's register state at the CP_BLIT equals Mesa's fd6_clear_texture / fd6_blit (reference test too)
        check('CACHE_FLUSH_TS' in fake.p67_events and fake.p67_events.count('CACHE_CLEAN') >= 4,
              'the batch fence and the ring fences clean the caches')
        # a rerun is idempotent in effect (fresh fence values, same pictures)
        status, out = run(fake, 'blit')
        check(status == 0 and out['p6_copy']['identical'] and not out['cb0_tables'][0]['changed'],
              'second blit: CB0 tables already in place, same result')
        no_violation(fake, 'second blit')


def test_p6_framebuffer():
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        rect = (320, 40, 256, 128)
        fake.fb_rect_rows = (rect[1], rect[1] + rect[3])
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='320,40,256,128')
        check(status == 0, f'blit --to-framebuffer completes ({out.get("stopped")})')
        no_violation(fake, 'blit --to-framebuffer')
        report = out['framebuffer']
        check(report['clear']['bad_pixels'] == 0 and report['blit']['bad_pixels'] == 0 and
              report['clear']['bytes_changed_outside_rect'] == 0 and report['blit']['bytes_changed_outside_rect'] == 0,
              'framebuffer clear and blit: exact inside, nothing changed outside the rectangle')
        check(report['unmapped'], 'framebuffer pages are out of CB0 again (walked from the tables in DRAM)')
        fbp = fake.fb['stride'] * 4
        src, sp = M.p6_source(), M.P6['pitch']
        x, y, w, h = rect
        panel = fake.dev_read(FB_BASE, fake.fb['size'])
        inside = all(panel[(y + yy) * fbp + 4 * x:(y + yy) * fbp + 4 * (x + w)] == src[yy * sp:yy * sp + 4 * w]
                     for yy in range(h))
        outside = bytearray(panel)
        init = fake.fb_initial
        for yy in range(h):
            at = (y + yy) * fbp + 4 * x
            outside[at:at + 4 * w] = init[at:at + 4 * w]
        check(inside and bytes(outside) == init, 'the panel shows the source in the rectangle, nothing else changed')
        check(fake.fb_console_calls == [False] and report['console_was_enabled'], 'q1n1\'s console yielded once')
        rows = {((pa - FB_BASE) // fbp) for pa, _n in fake.gpu_writes_fb}
        check(rows and min(rows) >= y and max(rows) < y + h, 'every GPU framebuffer write is in the rectangle rows')
        check(len(out['submissions']) == 4 and out['submissions'][2]['refs_walked'] > 0,
              'four submissions; the framebuffer ones walked their references through CB0')
        host = T3.host_record(fake)
        check(host['fb_mapped'] == 0, 'host record: no framebuffer mapped')
        # the tables in DRAM: no entry under FB_VA
        tables = M.build_cb0_tables(T3.ARENA)
        image = fake.dev_read(T3.ARENA + tables.image()[0], R.ARENA_LAYOUT['tables'][1] // 2)
        base = T3.ARENA + tables.image()[0]
        read = lambda a: struct.unpack_from('<Q', image, a - base)[0] if base <= a < base + len(image) else 0
        check(all(S3.walk(read, S3.ttbr_for(tables, R.GPU_CB), va) is None
                  for va in range(R.FB_VA, R.FB_VA + 0x200000, 0x1000)), 'CB0 in DRAM: the framebuffer window faults')
        check(fake.translate(R.GPU_SID, R.FB_VA, quiet=True) is None, 'the model\'s SMMU faults FB_VA')


def test_p7():
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        with tempfile.TemporaryDirectory() as tmp:
            status, out = run(fake, 'triangle', image_prefix=str(Path(tmp) / 'tri'))
            check(status == 0, f'triangle completes ({out.get("stopped")})')
            no_violation(fake, 'triangle')
            cmp = out['p7']['compare']
            check(cmp['ok'] and cmp['guards_intact'] and cmp['max_abs_diff'] <= 1,
                  f'the model\'s triangle matches the M3 Max golden ({ {k: cmp[k] for k in ("max_abs_diff", "mean_abs_diff")} })')
            saved = Path(out['images']['p7_target_96x64_bgra8']).read_bytes()
            check(hashlib.sha256(saved).hexdigest() == cmp['sha256'], 'the readback is saved next to the JSON')
        check(len(fake.draws) == 1 and out['order'][-1] == 'p7_matches_golden', 'one draw, matched')
        verts = fake.draws[0]['verts']
        check([(round(v[0], 3), round(v[1], 3)) for v in verts] == [(8.0, 4.0), (88.0, 4.0), (8.0, 60.0)],
              'SimpleVertex with the UBO matrix and the Metal viewport puts the vertices at the scene\'s pixels')
        check(out['p7']['findings'] and out['p7']['sampling']['ntex'] == 1, 'Stage 6 findings in the log')
        # uploads are the exported code
        sh = M.p7_assets()[0]
        code = arena_bytes(fake, 'p7_code', 0, 0x2000)
        check(code[:128] == bytes.fromhex(sh['vs']['code_hex']) and code[0x1000:0x1080] == bytes.fromhex(sh['fs']['code_hex']),
              'VS and FS ir3 code in the arena are the exported variants')
        # a wrong texture (not what the golden was made from) is caught by the golden compare
        saved = M.p7_texture
        M.p7_texture = lambda: bytes(b ^ 0x40 for b in saved())
        try:
            status, out = run(fake, 'triangle')
            check(status == 2 and out['stopped']['step'] == 'T3' and not out['stopped']['compare']['ok'],
                  'a triangle that differs from the golden stops at T3 (exit 2)')
        finally:
            M.p7_texture = saved
        # quiesce afterwards: safe, and a second cycle draws again
        status, out = run(fake, 'quiesce')
        check(status == 0 and out['quiesce'] == 'graceful' and not fake.cb[0][0] and not fake.smr[0],
              'quiesce after P7: graceful, CB0 off, SID 0 unmatched')
        check(T3.host_record(fake)['p67_token'] == 0, 'quiesce clears the P6/P7 record fields')
        ready(fake, fw)
        status, out = run(fake, 'triangle')
        check(status == 0 and out['p7']['compare']['ok'], 'second cycle after quiesce: the triangle again')
        no_violation(fake, 'second cycle')


def test_refusals():
    with T3.synthetic_firmware() as fw:
        # before cp-fence: no CP
        fake = FakeP67()
        T3.happy_path(fake, fw, upto='oob')
        for command, step in (('blit', 'D0'), ('triangle', 'T0')):
            status, out = run(fake, command)
            check(status == 2 and out['stopped']['step'] == step and 'cp-fence' in out['stopped']['reason']
                  and not fake.draws and not fake.blits, f'{command} before cp-fence: refused at {step}, nothing submitted')
        # a CP this tool did not start (record says otherwise)
        fake = FakeP67()
        ready(fake, fw)
        T3.host_record_set(fake, cp_token=0)
        status, out = run(fake, 'triangle')
        check(status == 2 and out['stopped']['step'] == 'T0' and 'cp-fence' in out['stopped']['reason'],
              'a CP whose record is not this boot\'s is refused')
        # ring pointers moved behind the record's back
        fake = FakeP67()
        ready(fake, fw)
        fake.rptr += 4
        status, out = run(fake, 'blit')
        check(status == 2 and 'disagree' in out['stopped']['reason'], 'ring pointers that disagree are refused')
        # framebuffer: overlapping the arena, in Conventional memory, an unknown pixel format
        # a framebuffer inside the arena never gets this far: arena_prove refuses the arena first (q1n1's 'fb')
        fake = FakeP67(fb_base=T3.ARENA + 0x1000000, fb_efi_type=None)
        status, out = run(fake, 'gmu-smmu')
        check(status == 2 and out['stopped']['step'] == 'A0' and "['fb']" in out['stopped']['reason'],
              'a framebuffer inside the arena: every Stage 3 command stops at A0')
        s = S3.Session3(fake)
        s.arena = (T3.ARENA, R.ARENA_SIZE)
        _fb, problems = M.fb_discover(s, {'_efi_regions': []})
        check(any('overlaps the GPU arena' in p for p in problems), 'fb_discover refuses it too (defence in depth)')
        for kw, text in ((dict(fb_efi_type=7), 'Conventional'), (dict(fb_format=2), 'pixel format')):
            fake = FakeP67(**kw)
            ready(fake, fw)
            status, out = run(fake, 'blit', to_framebuffer=True)
            check(status == 2 and out['stopped']['step'] == 'D0' and text in out['stopped']['reason']
                  and not fake.blits, f'blit --to-framebuffer refused before any submission: {text}')
            status, out = run(fake, 'blit')
            check(status == 0 and out['framebuffer']['problems'], 'the offscreen P6 still runs; the problem is logged')
        fake = FakeP67(fb_efi_type=7)
        ready(fake, fw)
        status, out = run(fake, 'blit', to_framebuffer=True, fb_any_type=True)
        check(status == 0, '--fb-any-type overrides the EFI-type refusal')
        fake = FakeP67()
        ready(fake, fw)
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='600,0,256,64')
        check(status == 2 and 'rectangle' in out['stopped']['reason'], 'a rectangle outside the framebuffer is refused')


def test_fence_timeout_and_recovery():
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        fake.s3['cp_executes'] = False
        status, out = run(fake, 'blit')
        sub = out['stopped'].get('submission', {})
        check(status == 2 and out['stopped']['step'] == 'D2' and 'did not write the fence' in out['stopped']['reason']
              and 'CP_RB_RPTR' in sub.get('diagnostics', {}), 'fence not written: stop at D2 with the CP state')
        polls = [p for p in out['polls'] if p['reg'].startswith('fence')]
        check(polls and not polls[-1]['ok'] and 2 <= polls[-1]['reads'] <= S3.POLL['fence'][0],
              'the fence wait is bounded')
        status, out = run(fake, 'blit')
        check(status == 2 and 'disagree' in out['stopped']['reason'], 'a stuck CP is not given more work')
        fake.s3['cp_executes'] = True
        status, out = run(fake, 'quiesce')
        check(status == 0 and out['order'][-1] == 'quiesced' and fake.s2cr[0] >> 16 == R.S2CR_TYPE_FAULT,
              'quiesce after a stuck submission returns to the safe state')
        # an interrupted --to-framebuffer run: the pages are still in CB0 (the link died after mapping)
        fake = FakeP67()
        ready(fake, fw)
        s = S3.Session3(fake, allow_writes=True)
        s.arena = (T3.ARENA, R.ARENA_SIZE)
        s.proven.update({'cx_smmu': 'test', 'gx': 'test'})
        fb = {'base': FB_BASE, 'size': fake.fb['size'], 'pitch': FB_STRIDE * 4}
        span = M.fb_window_span(fb, (0, 0, 64, 16))
        M.install_tables(s, {}, M.Window(T3.ARENA, span), 'test: leftover mapping')
        T3.host_record_set(fake, fb_mapped=span['pa'])
        check(fake.translate(R.GPU_SID, R.FB_VA, quiet=True) is not None, 'setup: framebuffer mapped in CB0')
        status, out = run(fake, 'blit')
        check(status == 0 and out.get('leftover_fb_mapping_removed') and
              fake.translate(R.GPU_SID, R.FB_VA, quiet=True) is None,
              'the next run removes a framebuffer mapping left by an interrupted run before submitting')
        no_violation(fake, 'leftover mapping')


def test_interrupt_faults():
    """Errors the CP and RBBM report while the fence still comes (review: the old 0x7F7F mask missed every
    SWFAULT bit, and RBBM_INT_0_STATUS was not read at all)."""
    check(0x10000 & 0x7F7F == 0 and M.CP_GLOBAL_FAULTS & 0x10000 and M.RBBM_INT_ERRORS & 0x200,
          'the run-2 probe fault (CP_INTERRUPT_STATUS_GLOBAL 0x10000, RBBM_INT_0_STATUS 0x200) is inside the masks')
    with T3.synthetic_firmware() as fw:
        for inject, text in (({'cp_global': 0x10000}, 'CP_INTERRUPT_STATUS_GLOBAL 0x10000'),
                             ({'cp_global': 0x1}, 'CP_INTERRUPT_STATUS_GLOBAL 0x1'),
                             ({'int0': 0x200}, 'RBBM_INT_0_STATUS 0x200'),
                             ({'int0': 1 << 24}, 'RBBM_INT_0_STATUS 0x1000000')):
            fake = FakeP67()
            ready(fake, fw)
            fake.inject = {1: inject}
            status, out = run(fake, 'blit')
            check(status == 2 and out['stopped']['step'] == 'D2' and 'report a fault' in out['stopped']['reason']
                  and text in out['stopped']['reason'] and len(fake.blits) == 1,
                  f'a fault reported with the fence ({text}) stops at D2 before the next submission')
            status, out = run(fake, 'triangle')
            check(status == 2 and out['stopped']['step'] == 'T0' and 'error interrupt is already recorded'
                  in out['stopped']['reason'] and not fake.draws,
                  f'the sticky error ({text}) refuses the next command before anything is written')
        # timestamp interrupts every batch raises are not errors
        fake = FakeP67()
        ready(fake, fw)
        fake.int0 = (1 << 20) | (1 << 17) | (1 << 15)
        status, out = run(fake, 'blit')
        check(status == 0, 'CP_CACHE_FLUSH_TS / RB_DONE_TS / PM4CPINTERRUPT bits are not errors')
        # the IB's own fence must land too: a ring fence alone does not prove the IB ran to its end
        fake = FakeP67()
        ready(fake, fw)
        fake.inject = {2: {'drop_ib_fence': 1}}
        status, out = run(fake, 'blit')
        check(status == 2 and out['stopped']['step'] == 'D3' and 'did not run to its end' in out['stopped']['reason'],
              'a missing IB fence stops the command (D3) although the ring fence came')
        # a fault in the framebuffer step: the first failure stays the reason, the pages still leave CB0
        fake = FakeP67()
        ready(fake, fw)
        fake.fb_rect_rows = (40, 168)
        fake.inject = {3: {'cp_global': 0x10000}}
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='320,40,256,128')
        check(status == 2 and out['stopped']['step'] == 'D4' and 'report a fault' in out['stopped']['reason']
              and out['framebuffer']['unmapped'] and T3.host_record(fake)['fb_mapped'] == 0
              and fake.translate(R.GPU_SID, R.FB_VA, quiet=True) is None,
              'D4 fault: the fault stays the reason, the framebuffer pages are out of CB0, the record says so')


def test_fb_cleanup_failures():
    """D4's cleanup after a failure: the first failure stays the reason, a cleanup failure is recorded next to it,
    the record keeps fb_mapped until DRAM shows the pages gone, and a lost link during the cleanup is exit 5."""
    off, size = R.ARENA_LAYOUT['tables']
    lo, hi = T3.ARENA + off + size // 2, T3.ARENA + off + size          # CB0's half of the table pool

    def breaking(fake, how, after_ib):
        writemem = fake.writemem

        def patched(addr, data):
            if fake.ibs_run >= after_ib and lo <= addr < hi:
                if how == 'link':
                    raise Q.ProxyError('expected 1 bytes, got 0')
                return                                    # the table write never reaches DRAM
            return writemem(addr, data)
        fake.writemem = patched

    with T3.synthetic_firmware() as fw:
        # a fault in the framebuffer clear, then the unmap's table write does not land
        fake = FakeP67()
        ready(fake, fw)
        fake.fb_rect_rows = (40, 168)
        fake.inject = {3: {'cp_global': 0x10000}}
        breaking(fake, 'drop', 3)
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='320,40,256,128')
        fb = out.get('framebuffer', {})
        check(status == 2 and out['stopped']['step'] == 'D4' and 'report a fault' in out['stopped']['reason']
              and 'did not read back' in fb.get('unmap_error', '') and fb.get('unmapped') is False
              and T3.host_record(fake)['fb_mapped'] != 0,
              'D4 fault + failed unmap: the fault stays the reason, unmap_error says why, the record keeps fb_mapped')
        # the same, but the link dies during the cleanup: exit 5, the fault kept as first_failure
        fake = FakeP67()
        ready(fake, fw)
        fake.fb_rect_rows = (40, 168)
        fake.inject = {3: {'cp_global': 0x10000}}
        breaking(fake, 'link', 3)
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='320,40,256,128')
        fb = out.get('framebuffer', {})
        check(status == 5 and 'proxy link failed' in out['stopped']['reason']
              and 'report a fault' in fb.get('first_failure', '') and 'ProxyError' in fb.get('unmap_error', '')
              and fb.get('unmapped') is False and T3.host_record(fake)['fb_mapped'] != 0,
              'a link lost during the D4 cleanup is exit 5; the first failure is kept in framebuffer.first_failure')
        # no earlier failure: a cleanup that does not land is itself the stop (D4), fb_mapped kept
        fake = FakeP67()
        ready(fake, fw)
        fake.fb_rect_rows = (40, 168)
        breaking(fake, 'drop', 4)
        status, out = run(fake, 'blit', to_framebuffer=True, fb_rect='320,40,256,128')
        fb = out.get('framebuffer', {})
        check(status == 2 and out['stopped']['step'] == 'D4' and 'did not read back' in out['stopped']['reason']
              and fb.get('unmapped') is False and T3.host_record(fake)['fb_mapped'] != 0,
              'a D4 run whose unmap does not land stops at D4 and keeps fb_mapped for the next run to clean up')


def test_fb_efi_allowlist():
    """D4 needs the whole framebuffer described as Reserved (else --fb-any-type)."""
    with T3.synthetic_firmware() as fw:
        pages = (FB_STRIDE * FB_H * 4 + 4095) // 4096
        for kw, text in ((dict(fb_efi_type=None), 'does not describe the whole framebuffer'),
                         (dict(fb_efi_pages=pages // 2), 'does not describe the whole framebuffer'),
                         (dict(fb_efi_type=6), "['RuntimeData']"),
                         (dict(fb_efi_type=10), "['ACPINVS']")):
            fake = FakeP67(**kw)
            ready(fake, fw)
            status, out = run(fake, 'blit', to_framebuffer=True)
            check(status == 2 and out['stopped']['step'] == 'D0' and text in out['stopped']['reason']
                  and not fake.blits and not fake.fb_console_calls,
                  f'D4 refused at D0 before anything is written: {text} ({kw})')
            status, out = run(fake, 'blit', to_framebuffer=True, fb_any_type=True)
            check(status == 0 and out['framebuffer']['unmapped'], f'--fb-any-type is the explicit override ({kw})')
        # device space is never overridable: a GPU write there is the run-1 bus hang
        for efi_type, name in ((11, "['MMIO']"), (12, "['MMIOPort']")):
            fake = FakeP67(fb_efi_type=efi_type)
            ready(fake, fw)
            for any_type in (False, True):
                status, out = run(fake, 'blit', to_framebuffer=True, fb_any_type=any_type)
                check(status == 2 and out['stopped']['step'] == 'D0' and name in out['stopped']['reason']
                      and 'device space' in out['stopped']['reason'] and not fake.blits and not fake.fb_console_calls
                      and fake.translate(R.GPU_SID, R.FB_VA, quiet=True) is None,
                      f'a framebuffer the EFI map calls {name} is refused at D0 (--fb-any-type {any_type})')
            status, out = run(fake, 'blit')
            check(status == 0 and any('device space' in p for p in out['framebuffer']['problems']),
                  f'{name}: the offscreen P6 still runs; the problem is logged')
        # the Reserved framebuffer the runbook expects: accepted, and reported as covered
        fake = FakeP67()
        ready(fake, fw)
        status, out = run(fake, 'blit', to_framebuffer=True, stop_after='D0')
        check(status == 0 and out['framebuffer']['efi_types'] == ['Reserved'] and out['framebuffer']['efi_covered']
              and not out['framebuffer']['problems'], 'a framebuffer wholly described as Reserved passes D0')


def test_table_write_order():
    """install_tables writes a new L3 page before the L2 entry that points at it (highest page first)."""
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        mark = len(fake.log)
        status, out = run(fake, 'blit', stop_after='D1')
        off, size = R.ARENA_LAYOUT['tables']
        lo, hi = T3.ARENA + off + size // 2, T3.ARENA + off + size
        pages = [a for op, a, *_ in fake.log[mark:] if op == 'mw' and lo <= a < hi and (a - lo) % 4096 == 0]
        check(status == 0 and len(pages) >= 2 and pages == sorted(pages, reverse=True),
              f'CB0 table pages written highest first ({[hex(p) for p in pages]})')


def test_ring_room():
    with T3.synthetic_firmware() as fw:
        fake = FakeP67()
        ready(fake, fw)
        ring = R.ARENA_LAYOUT['cp_rb'][1] // 4
        host = T3.host_record(fake)
        # pretend the ring is nearly full (both the record and the CP agree)
        fake.rptr = ring - 20
        fake.gx[R.REGS['CP_RB_WPTR'].addr] = ring - 20
        T3.host_record_set(fake, rb_wptr=ring - 20)
        status, out = run(fake, 'triangle')
        check(status == 2 and out['stopped']['step'] == 'T0' and 'never wraps' in out['stopped']['reason']
              and not fake.draws, 'no room on the ring: refused before anything is written')
        _ = host


# --- the Mesa reference ------------------------------------------------------------------------------------
RD_GPUADDR, RD_CMDSTREAM_ADDR, RD_BUFFER_CONTENTS = 3, 6, 12


def parse_rd(path):
    data = path.read_bytes()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    bufs, streams, off, cur = [], [], 0, None
    while off + 8 <= len(data):
        kind, size = struct.unpack_from('<II', data, off)
        body = data[off + 8:off + 8 + size]
        off += 8 + size
        words = struct.unpack_from(f'<{len(body) // 4}I', body)
        if kind == RD_GPUADDR:
            cur = words[0] | (words[2] << 32 if len(words) > 2 else 0)
        elif kind == RD_BUFFER_CONTENTS:
            bufs.append((cur, bytes(body)))
        elif kind == RD_CMDSTREAM_ADDR:
            streams.append((words[0] | (words[2] << 32 if len(words) > 2 else 0), words[1]))
    return bufs, streams


class Walker:
    """Walks a command stream as the CP would, for comparison: register writes (PKT4/CRB/NCRB, draw-state groups
    applied at the draw), the kind of packet each register came in, events, and the state at each CP_BLIT and
    CP_DRAW_INDX_OFFSET."""

    def __init__(self, bufs):
        self.bufs = sorted(bufs)
        self.regs, self.kind, self.groups, self.snaps, self.seq, self.loads = {}, {}, {}, [], [], []
        self.bodies = []                   # (opcode, payload) of every packet that is not a register write

    def read(self, va, n):
        for base, blob in self.bufs:
            if base <= va and va + n <= base + len(blob):
                return blob[va - base:va - base + n]
        raise KeyError(hex(va))

    def walk(self, va, ndw):
        words = struct.unpack(f'<{ndw}I', self.read(va, 4 * ndw))
        i = 0
        while i < len(words):
            h = words[i]
            if h >> 28 == 4:
                cnt, reg = h & 0x7F, (h >> 8) & 0x7FFFF
                for k in range(cnt):
                    self.set(reg + k, words[i + 1 + k], 'pkt4')
                i += 1 + cnt
                continue
            cnt, op = h & 0x3FFF, (h >> 16) & 0x7F
            body = list(words[i + 1:i + 1 + cnt])
            if op == R.CP_CONTEXT_REG_BUNCH:
                for k in range(0, cnt, 2):
                    self.set(body[k], body[k + 1], 'crb')
            elif op == R.CP_NON_CONTEXT_REG_BUNCH:
                for k in range(2, cnt, 2):
                    self.set(body[k], body[k + 1], 'ncrb')
            elif op == R.CP_INDIRECT_BUFFER:
                self.walk(body[0] | body[1] << 32, body[2] & 0xFFFFF)
            elif op == R.CP_SET_DRAW_STATE:
                for g in range(0, cnt, 3):
                    w0 = body[g]
                    if w0 & (1 << 18):
                        self.groups.clear()
                    elif w0 & (1 << 17):
                        self.groups.pop(w0 >> 24, None)
                    else:
                        self.groups[w0 >> 24] = (w0, body[g + 1] | body[g + 2] << 32, w0 & 0xFFFF)
            elif op == R.CP_DRAW_INDX_OFFSET:
                for gid in sorted(self.groups):
                    w0, addr, count = self.groups[gid]
                    if w0 & (1 << 22):
                        self.walk(addr, count)
                self.snaps.append(('draw', dict(self.regs), list(self.loads)))
            elif op == R.CP_BLIT:
                self.snaps.append(('blit', dict(self.regs), list(self.loads)))
            elif op in (R.CP_LOAD_STATE6_GEOM, R.CP_LOAD_STATE6_FRAG):
                self.loads.append((op, body))
            if op == R.CP_EVENT_WRITE7:
                self.seq.append(('event', body[0] & 0xFF))
            elif op not in (R.CP_CONTEXT_REG_BUNCH, R.CP_NON_CONTEXT_REG_BUNCH):
                self.seq.append(('pkt', op))
            if op not in (R.CP_CONTEXT_REG_BUNCH, R.CP_NON_CONTEXT_REG_BUNCH):
                self.bodies.append((op, tuple(body)))
            i += 1 + cnt

    def set(self, reg, value, kind):
        self.regs[reg] = value
        self.kind[reg] = kind


def ours_walker(cs, va=0x11000000):
    w = Walker([(va, cs.blob())])
    w.walk(va, cs.dwords())
    return w


def ref_walker(name):
    bufs, streams = parse_rd(REFS / name)
    w = Walker(bufs)
    for va, n in streams:
        w.walk(va, n)
    return w


def names_of(offsets):
    out = []
    for off in offsets:
        for name, (o, bits, _f, stride, length) in X.REGS.items():
            if any(o + i * stride <= off < o + i * stride + (2 if bits == 64 else 1) for i in range(length)):
                out.append(name)
                break
        else:
            out.append(hex(off))
    return sorted(set(out))


def payload_key(op, body):
    """A packet's payload with the words that are addresses or fence values masked: what must equal Mesa's.
    CP_EVENT_WRITE7 with WRITE_ENABLED: the event word (address and value differ); CP_LOAD_STATE6: the state
    word, and for direct UBO descriptors each descriptor's size (bases are addresses); everything else whole."""
    if op == R.CP_EVENT_WRITE7:
        return (op, body[:1])
    if op in (R.CP_LOAD_STATE6_GEOM, R.CP_LOAD_STATE6_FRAG):
        if (body[0] >> 16 & 3) == X.ENUMS['a6xx_state_src']['SS6_DIRECT']:
            return (op, body[:1], tuple(w >> 17 for w in body[4::2]))
        return (op, body[:1])
    return (op, tuple(body))


def payloads(walker, skip):
    return [payload_key(op, b) for op, b in walker.bodies if op not in skip]


def diff_state(ref, ours):
    return sorted(k for k in set(ref) | set(ours) if ref.get(k) != ours.get(k))


# Registers that must differ from Mesa's capture, and why: addresses (a different allocator), the scene
# (sizes, rectangles), Metal vs GL conventions, and what this tool leaves out on purpose.
ADDRESS_REGS = {'RB_A2D_DEST_BUFFER_BASE', 'TPL1_A2D_SRC_TEXTURE_BASE', 'TPL1_GFX_BORDER_COLOR_BASE',
                'TPL1_CS_BORDER_COLOR_BASE', 'RB_MRT_BASE', 'RB_COLOR_FLAG_BUFFER_ADDR', 'SP_VS_BASE', 'SP_PS_BASE',
                'SP_PS_SAMPLER_BASE', 'SP_PS_TEXMEMOBJ_BASE', 'VFD_VERTEX_BUFFER_BASE', 'SP_GFX_BINDLESS_BASE_DESCRIPTOR'}
P7_EXPECTED = ADDRESS_REGS | {
    # a 96x64 target (the capture's is 256x256): scissors, pitches, the clear rectangle
    'GRAS_SC_WINDOW_SCISSOR_BR', 'GRAS_SC_SCREEN_SCISSOR_BR', 'GRAS_A2D_DEST_BR', 'GRAS_A2D_SCISSOR_BR',
    'RB_RESOLVE_CNTL_2', 'RB_MRT_PITCH', 'RB_MRT_ARRAY_PITCH', 'RB_A2D_DEST_BUFFER_PITCH',
    # Metal conventions (BINDING-ABI 4.3): clip z in [0, 1] (clip_halfz), y up with row 0 at the top, flat
    # varyings from the first vertex
    'GRAS_CL_CNTL', 'GRAS_CL_VIEWPORT_YSCALE', 'GRAS_CL_VIEWPORT_ZOFFSET', 'GRAS_CL_VIEWPORT_ZSCALE', 'PC_CNTL',
    'VPC_PC_CNTL',
    # Metal's vertex descriptor: one buffer, two attributes at offsets 0 and 8 (GL used two buffers)
    'VFD_FETCH_INSTR_INSTR', 'VFD_VERTEX_BUFFER_SIZE', 'VFD_VERTEX_BUFFER_STRIDE',
    # Stage 6 findings (variant_checks): NTEX/NSAMP from the bindings, pixel LOD as Mesa's GLSL path sets it
    'SP_PS_CONFIG', 'SP_PS_CNTL_0', 'RB_PS_CNTL',
    # left out: the occlusion-query sample counter (autotune) and the bindless-set draw-state group's
    # SP_UPDATE_CNTL order
    'RB_SAMPLE_COUNTER_CNTL'}


def test_reference():
    sh = M.p7_assets()[0]
    problems, findings, sampling = M.variant_checks(sh)
    # --- P7: our IB vs gallium's for the same two variants (drm-shim, P67_VARIANT_VS/FS) -------------------
    ref = ref_walker('gl-tri-skylight.rd')
    cs, _link = M.build_p7_ib(M.Window(T3.ARENA), sh, sampling, 7)
    ours = ours_walker(cs)
    rdraw = [s for s in ref.snaps if s[0] == 'draw']
    odraw = [s for s in ours.snaps if s[0] == 'draw']
    check(len(rdraw) == len(odraw) == 1, 'one draw in each')
    differ = set(names_of(diff_state(rdraw[0][1], odraw[0][1])))
    unexplained = differ - P7_EXPECTED
    check(not unexplained, f'P7 register state at the draw equals Mesa\'s except the explained registers '
                           f'(unexplained: {sorted(unexplained)})')
    check({'GRAS_CL_VIEWPORT_YSCALE', 'SP_PS_CONFIG', 'RB_MRT_BASE'} <= differ,
          'the comparison sees the expected differences (it is not vacuous)')
    same = set(names_of(rdraw[0][1])) - differ
    check(len(same) > 200, f'{len(same)} registers identical to Mesa\'s at the draw')
    # every register both write comes in the same kind of packet (PKT4 / CONTEXT_REG_BUNCH / NON_CONTEXT)
    kinds = [(names_of([r])[0], ref.kind[r], ours.kind[r]) for r in set(ref.kind) & set(ours.kind)
             if ref.kind[r] != ours.kind[r]]
    check(not kinds, f'same packet kind per register as Mesa ({kinds[:4]})')
    # the event and packet sequence: Mesa's minus what is left out on purpose (IBs are inlined here, CP_SET_AMBLE
    # preambles are preemption-only, the sample counter query, the draw-state groups run inline)
    skip_pkts = {R.CP_INDIRECT_BUFFER, 0x55, R.CP_SET_DRAW_STATE}
    zpass = X.ENUMS['vgt_event_type']['ZPASS_DONE']
    rseq = [e for e in ref.seq if not (e[0] == 'pkt' and e[1] in skip_pkts) and e != ('event', zpass)]
    oseq = [e for e in ours.seq if not (e[0] == 'pkt' and e[1] in skip_pkts)]
    check(rseq == oseq, f'P7 packet and event order equals Mesa\'s ({len(oseq)} entries)')
    # ... and every non-register packet's payload (markers, thread control, the draw, the blit, the event
    # flags, the CP_LOAD_STATE6 words), not only its opcode: a wrong CP_SET_MARKER mode or draw word fails here
    rpay = [p for p in payloads(ref, skip_pkts) if not (p[0] == R.CP_EVENT_WRITE7 and p[1][0] & 0xFF == zpass)]
    opay = payloads(ours, skip_pkts)
    check(rpay == opay, f'P7 packet payloads equal Mesa\'s but for addresses and fence values ({len(opay)} packets; '
                        f'first difference {next(((a, b) for a, b in zip(rpay, opay) if a != b), None)})')
    # the T#/S# descriptors Mesa loaded (its 64x64 RGBA8 texture) vs ours
    rt = [b for op, b in rdraw[0][2] if op == R.CP_LOAD_STATE6_FRAG and (b[0] >> 14 & 3) == 1]
    rs = [b for op, b in rdraw[0][2] if op == R.CP_LOAD_STATE6_FRAG and (b[0] >> 14 & 3) == 0 and (b[0] >> 16 & 3) == 2]
    mesa_tex = struct.unpack('<16I', ref.read(rt[0][1] | rt[0][2] << 32, 64))
    mesa_samp = struct.unpack('<4I', ref.read(rs[0][1] | rs[0][2] << 32, 16))
    ours_tex = M.texture_descriptor(mesa_tex[0] & ~0x3F, 64, 64, 256, 'rgba')
    check(list(mesa_tex) == ours_tex, 'T#0: fdl6_view_init<A8XX> for 64x64 RGBA8 linear, dword for dword')
    ours_samp = M.sampler_descriptor()
    wrap_r = [f for f in X.DESC['A8XX_TEX_SAMP_0'][2] if f[0] == 'WRAP_R'][0]
    mask = ((1 << (wrap_r[2] - wrap_r[1] + 1)) - 1) << wrap_r[1]
    check(mesa_samp[1:] == tuple(ours_samp[1:]) and (mesa_samp[0] & ~mask) == (ours_samp[0] & ~mask),
          'S#0: as Mesa\'s except WRAP_R (GL defaults to REPEAT, Metal to clamp_to_edge)')
    rubo = [b for op, b in rdraw[0][2] if op == R.CP_LOAD_STATE6_GEOM]
    oubo = [b for op, b in odraw[0][2] if op == R.CP_LOAD_STATE6_GEOM]
    check(rubo[0][:5] == oubo[0][:5] and (rubo[0][6] & ~0x1FFFF) == (oubo[0][6] & ~0x1FFFF) == 4 << 17,
          'VS UBO state: two descriptors, UBO 0 empty, UBO 1 = 4 vec4 (the mvp), as Mesa')
    # --- P6: the nondraw 2D clear (glClearTexSubImage -> fd6_clear_texture) and the copy (glBlitFramebuffer) --
    for name, build in (('gl-cleartex-bgra.rd', M.build_p6_clear_ib), ('gl-blit-bgra.rd', M.build_p6_copy_ib)):
        ref = ref_walker(name)
        ours = ours_walker(build(M.Window(T3.ARENA), 7))
        rb = [s for s in ref.snaps if s[0] == 'blit']
        ob = [s for s in ours.snaps if s[0] == 'blit']
        differ = set(names_of(diff_state(rb[0][1], ob[0][1])))
        check(len(rb) == len(ob) == 1 and differ <= ADDRESS_REGS,
              f'{name}: register state at the CP_BLIT equals Mesa\'s but for addresses ({sorted(differ)})')
        rseq = [e for e in ref.seq if not (e[0] == 'pkt' and e[1] in skip_pkts)]
        oseq = [e for e in ours.seq if not (e[0] == 'pkt' and e[1] in skip_pkts)]
        check(rseq[:len(oseq)] == oseq, f'{name}: packet and event order equals Mesa\'s first batch')
        rpay, opay = payloads(ref, skip_pkts), payloads(ours, skip_pkts)
        check(rpay[:len(opay)] == opay, f'{name}: packet payloads equal Mesa\'s first batch but for addresses and '
                                        f'fence values ({len(opay)} packets)')
        kinds = [r for r in set(ref.kind) & set(ours.kind) if ref.kind[r] != ours.kind[r]]
        check(not kinds, f'{name}: same packet kind per register')


def test_cli():
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = FakeP67()
        ready(fake, fw)
        real = Q.connect
        Q.connect = lambda device=None, wait=0.0, **_: fake
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    a16gpu.main(['triangle'])
                    check(False, 'triangle without --write refused')
                except SystemExit as refused:
                    check(refused.code == 2, 'triangle without --write: refused by the parser')
                path = Path(tmp) / 'tri.json'
                status = a16gpu.main(['--out', str(path), 'triangle', '--write'])
                saved = json.loads(path.read_text())
                check(status == 0 and saved['order'][-1] == 'p7_matches_golden' and
                      Path(tmp, 'tri.p7.bgra').is_file(), 'CLI triangle: JSON and the readback image written')
                path = Path(tmp) / 'blit.json'
                status = a16gpu.main(['--out', str(path), 'blit', '--write', '--stop-after', 'D2'])
                saved = json.loads(path.read_text())
                check(status == 0 and saved['stopped_after'] == 'D2' and len(saved['submissions']) == 1,
                      'CLI blit --stop-after D2: one submission')
        finally:
            Q.connect = real


def main():
    if not M.P7_SHADERS.is_file():
        print(f'SKIP: a16gpu P6/P7 model checks: {M.P7_SHADERS.name} is not distributed (compiled from macOS '
              'shaders); generate it with tools/a16gpu-p7/make-p7-shaders.sh')
        return 0
    for test in (test_encoding, test_windows, test_p7_assets, test_reference, test_p6, test_p6_framebuffer, test_p7,
                 test_refusals, test_fence_timeout_and_recovery, test_interrupt_faults, test_fb_cleanup_failures, test_fb_efi_allowlist,
                 test_table_write_order, test_ring_room, test_cli):
        test()
    print(f'PASS: {checks} a16gpu P6/P7 model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
