# SPDX-License-Identifier: MIT
"""Adreno X2-90, Stage 3 P6 and P7: the first 2D blits and the first triangle, through the CP that
`cp-fence` started (docs/A16-ADRENO-STAGE3.md "P6 and P7", docs/A16-ADRENO-STAGE3-P6P7-RUNBOOK.md).

  blit --write                  P6: clear an offscreen BGRA8 target in the arena with the 2D engine and read it
                                back, then 2D-copy an arena image into it and read it back
  blit --write --to-framebuffer P6 opt-in: map only the framebuffer rows of one rectangle into CB0, clear the
                                rectangle and blit the arena image into it (visible on the panel), unmap
  triangle --write              P7: SkyLight SimpleVertex + SimpleTextureFragment (air2ir3, BINDING-ABI v2), one
                                textured triangle into an arena target, read back and compared with the M3 Max
                                Metal render of the same pipeline and inputs

The command streams follow Mesa's a8xx gallium driver (fd6_blitter.cc, fd6_gmem.cc sysmem path,
fd6_emit.cc, fd6_program.cc, fd6_texture.cc, fd6_const.cc, fd6_draw.cc; Turnip where noted) register by
register, with every register offset and field taken from Mesa's XML (tools/a8xx_regs.py, generated). The
ring side of each submission follows Linux's a7xx_submit (a8xx uses it). tools/testdata/a16gpu-p6p7 holds
Mesa's own command streams for the same operations, captured from the real driver running on drm-shim for
this chip, and tools/test-a16gpu-p6p7.py compares what this module builds with them register by register.

Rules on top of Stage 3's: the CP must be the one `cp-fence` started for this GMU boot (tokens, CP_SQE_CNTL,
ring pointers); every GPU address a command stream carries comes from a mapped window and is re-walked
through CB0's tables in DRAM before the submission (arena regions; framebuffer pages only inside the
opt-in step, removed again before the command returns); every DRAM write is an arena write; each submission
ends with a CACHE_CLEAN fence (Linux's), waited for with a bounded poll, then an idle check.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys

import adreno_x2 as R
import a8xx_regs as X
import a16gpu_stage3 as S3

S1 = S3.S1
Stop, Refused, PollTimeout = S3.Stop, S3.Refused, S3.PollTimeout
hx = S3.hx
HERE = Path(__file__).resolve().parent
ASSETS = HERE / 'a16gpu-p7'


# ============================================================================================================
# Register and packet packing (every offset and field from tools/a8xx_regs.py: Mesa's XML, A8XX variant)
# ============================================================================================================
def _field_bits(field, value):
    name, lo, hi, shr, ftype, radix = field
    width = hi - lo + 1
    if ftype == 'float':
        bits = struct.unpack('<I', struct.pack('<f', float(value)))[0] if width == 32 else \
            struct.unpack('<H', struct.pack('<e', float(value)))[0]
    elif ftype in ('fixed', 'ufixed'):
        bits = int(float(value) * (1 << radix)) & ((1 << width) - 1)      # gen_header: truncating cast
    elif ftype == 'boolean':
        bits = 1 if value else 0
    elif isinstance(value, str):
        bits = X.ENUMS[ftype][value]
    else:
        bits = int(value) >> shr
        if shr and bits << shr != int(value):
            raise Refused(f'field {name}: {int(value):#x} is not a multiple of {1 << shr}')
    if bits >> width:
        raise Refused(f'field {name}: {value!r} does not fit in {width} bits')
    return bits << lo


def pack(fields, what, **values):
    """Pack named field values (case-insensitive) into a register value; `value=` for an unnamed field,
    `dword=` for the whole register (Mesa's `.dword =`)."""
    if 'dword' in values:
        if len(values) != 1 or not 0 <= values['dword'] <= 0xFFFFFFFF:
            raise Refused(f'{what}: dword= is a whole 32-bit value on its own')
        return values['dword']
    by_name = {(f[0] or 'value').lower(): f for f in fields}
    if not fields:
        # A register the XML describes without fields (MAGIC/UNKNOWN ones): one raw 32-bit value.
        by_name = {'value': (None, 0, 31, 0, 'hex', 0)}
    word = 0
    for key, value in values.items():
        field = by_name.get(key.lower())
        if field is None:
            raise Refused(f'{what} has no field {key} on A8XX (a8xx_regs.py)')
        word |= _field_bits(field, value)
    return word


def reg(name, index=0, **values):
    """[(dword offset, value)] for one register (two for a 64-bit one), index into an array register."""
    offset, bits, fields, stride, length = X.REGS[name]
    if not 0 <= index < length:
        raise Refused(f'{name}[{index}] outside its {length} entries')
    offset += index * stride
    word = pack(fields, name, **values)
    if bits == 64:
        return [(offset, word & 0xFFFFFFFF), (offset + 1, word >> 32)]
    return [(offset, word)]


def desc(domain, n, **values):
    return pack(X.DESC[f'{domain}_{n}'][2], f'{domain}_{n}', **values)


def pkt_word(name, **values):
    return pack(X.PKT[name][2], name, **values)


def enum(kind, name):
    return X.ENUMS[kind][name]


def fui(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


def pkt4_header(offset, count):
    """pm4_pkt4_hdr (adreno_pm4.xml / freedreno_pm4.h): type 4, count, odd parities."""
    return 0x40000000 | count | R.pm4_parity(count) << 7 | (offset & 0x3FFFF) << 8 | R.pm4_parity(offset) << 27


# ============================================================================================================
# GPU windows: the only places a command stream may point at
# ============================================================================================================
RO, RW = S3.UNPRIV_RO, S3.UNPRIV_RW
RENDER_REGIONS = (('p67_ib', RO), ('p67_misc', RO), ('p6_src', RO), ('p6_dst', RW), ('p7_code', RO),
                  ('p7_data', RO), ('p7_tex', RO), ('p7_rt', RW))


def region_va(name, offset=0):
    off, size = R.ARENA_LAYOUT[name]
    if not 0 <= offset <= size:
        raise Refused(f'{name}+{offset:#x} outside the region')
    return R.P67_VA + (off - R.P67_ARENA_WINDOW[0]) + offset


def render_mappings(base):
    """(name, va, pa, size, attrs) for every render-window region (arena only)."""
    out = []
    for name, attrs in RENDER_REGIONS:
        off, size = R.ARENA_LAYOUT[name]
        out.append((name, region_va(name), base + off, size, attrs))
    return out


class Window:
    """Every GPU VA a command stream may reference: the CP's own buffers (memptrs), the render regions,
    and -- only when a framebuffer span is given -- those framebuffer pages. `addr` refuses anything else,
    so a command stream cannot even be built around an address outside them."""

    def __init__(self, base, fb_span=None):
        self.base = base
        self.ranges = [('memptrs', R.CP_MEMPTRS_VA, base + R.ARENA_LAYOUT['cp_memptrs'][0],
                        R.ARENA_LAYOUT['cp_memptrs'][1], True)]
        for name, va, pa, size, attrs in render_mappings(base):
            self.ranges.append((name, va, pa, size, not attrs & R.PTE_AP_RDONLY))
        self.fb_span = fb_span
        if fb_span:
            self.ranges.append(('framebuffer', fb_span['va'], fb_span['pa'], fb_span['size'], True))
        self.refs = []

    def addr(self, name, offset, length, write, what):
        for rname, va, pa, size, writable in self.ranges:
            if rname == name:
                if offset < 0 or length <= 0 or offset + length > size:
                    raise Refused(f'{what}: {name}+{offset:#x}/{length:#x} outside the {size:#x}-byte window')
                if write and not writable:
                    raise Refused(f'{what}: {name} is mapped read-only for the GPU')
                self.refs.append({'what': what, 'va': va + offset, 'pa': pa + offset, 'size': length,
                                  'write': write, 'window': name})
                return va + offset
        raise Refused(f'{what}: {name} is not mapped in this step')


def fb_window_span(fb, rect):
    """The framebuffer pages the rectangle's rows occupy: whole pages, never outside the framebuffer."""
    x, y, w, h = rect
    pitch = fb['pitch']
    first = fb['base'] + y * pitch
    last = fb['base'] + (y + h) * pitch
    start, end = first & ~0xFFF, (last + 0xFFF) & ~0xFFF
    limit = fb['base'] + (fb['size'] & ~0xFFF)
    if start < fb['base'] or end > limit:
        raise Refused(f'the rectangle\'s pages {start:#x}..{end:#x} are not whole pages inside the framebuffer '
                      f'{fb["base"]:#x}+{fb["size"]:#x}')
    if end - start > R.FB_VA_LIMIT:
        raise Refused('the rectangle spans more framebuffer than the FB_VA window')
    return {'pa': start, 'size': end - start, 'va': R.FB_VA, 'rect_offset': first - start}


def build_cb0_tables(base, fb_span=None):
    """CB0's tables: cp-fence's own (build_tables, allocated first, so their pages never move) plus the
    render window, plus the framebuffer pages when a span is given."""
    tables = S3.build_tables(base, 'gpu')
    for _name, va, pa, size, attrs in render_mappings(base):
        tables.map(va, pa, size, attrs)
    if fb_span:
        tables.map(fb_span['va'], fb_span['pa'], fb_span['size'], RW)
    return tables


# ============================================================================================================
# The command-stream builder
# ============================================================================================================
class CS:
    """One command buffer. Registers go through reg() (a8xx_regs); addresses through a Window."""

    def __init__(self, window, what):
        self.w = []
        self.win = window
        self.what = what

    def raw(self, *words):
        self.w.extend(w & 0xFFFFFFFF for w in words)

    def pkt7(self, opcode, *payload):
        self.raw(R.pkt7(opcode, len(payload)), *payload)

    def pkt4(self, *regs):
        """One PKT4 over consecutive registers (fd_pkt4)."""
        pairs = [p for r in regs for p in r]
        for (a, _v), (b, _w) in zip(pairs, pairs[1:]):
            if b != a + 1:
                raise Refused(f'PKT4 registers not consecutive: {a:#x}, {b:#x}')
        self.raw(pkt4_header(pairs[0][0], len(pairs)), *(v for _o, v in pairs))

    def crb(self, *regs):
        """CP_CONTEXT_REG_BUNCH: (offset, value) pairs (fd_crb)."""
        pairs = [p for r in regs for p in r]
        self.pkt7(R.CP_CONTEXT_REG_BUNCH, *(x for pair in pairs for x in pair))

    def ncrb(self, *regs):
        """CP_NON_CONTEXT_REG_BUNCH: 1, 0, then (offset, value) pairs (fd_ncrb, A7XX+)."""
        pairs = [p for r in regs for p in r]
        self.pkt7(R.CP_NON_CONTEXT_REG_BUNCH, 1, 0, *(x for pair in pairs for x in pair))

    def event(self, name, address=None, payload=None):
        """CP_EVENT_WRITE7 (fd6 __event_write<A8XX>): the event alone, or written to RAM with a 32-bit payload."""
        if address is None:
            self.pkt7(R.CP_EVENT_WRITE7, pkt_word('CP_EVENT_WRITE7_0', event=name, write_src='EV_WRITE_USER_32B',
                                                  write_dst='EV_DST_RAM'))
        else:
            self.pkt7(R.CP_EVENT_WRITE7, pkt_word('CP_EVENT_WRITE7_0', event=name, write_src='EV_WRITE_USER_32B',
                                                  write_dst='EV_DST_RAM', write_enabled=True),
                      address & 0xFFFFFFFF, address >> 32, payload)

    def wfi(self):
        self.pkt7(R.CP_WAIT_FOR_IDLE)

    def marker(self, mode):
        """fd6_set_render_mode<A8XX>: CP_SET_MARKER with the A8XX layout."""
        self.pkt7(R.CP_SET_MARKER, pkt_word('A8XX_CP_SET_MARKER_0', mode=mode))

    def dwords(self):
        return len(self.w)

    def blob(self):
        return struct.pack(f'<{len(self.w)}I', *self.w)


# --- pieces every submission shares -------------------------------------------------------------------------
def gmem_config_sysmem():
    """fd6_calc_gmem_cache_offsets(X2-90, gmem 21 MiB) sysmem half (freedreno_devices.py a8xx_gen2, num_ccu 8;
    a6xx_catalog.c .gmem = 21 * SZ_1M; fd6_gmem_cache.h)."""
    num_ccu, gmem = 8, 21 * 1024 * 1024
    # a8xx_gen2: sysmem color FULL (0), 128 KiB per CCU; depth THREE_QUARTER (3 on gen2: freedreno_dev_info.py
    # CCUColorCacheFraction), 192 KiB per CCU; VPC attr 128 KiB, pos 64 KiB, BV pos 32 KiB per CCU.
    cfg = {'color_cache_fraction': 0, 'color_cache_size': 128 * 1024, 'depth_cache_fraction': 3,
           'depth_cache_size': 192 * 1024, 'vpc_attr_buf_size': 131072, 'vpc_pos_buf_size': 65536,
           'vpc_bv_pos_buf_size': 32768}
    offset = gmem
    cfg['vpc_bv_pos_buf_offset'] = offset = offset - num_ccu * cfg['vpc_bv_pos_buf_size']
    cfg['vpc_attr_buf_offset'] = offset = offset - num_ccu * cfg['vpc_attr_buf_size']
    cfg['vpc_pos_buf_offset'] = offset = offset - num_ccu * cfg['vpc_pos_buf_size']
    cfg['color_ccu_offset'] = offset = offset - num_ccu * cfg['color_cache_size']
    cfg['depth_ccu_offset'] = offset - num_ccu * cfg['depth_cache_size']
    return cfg


def emit_gmem_cache_cntl_sysmem(cs):
    """fd6_emit_gmem_cache_cntl<A8XX>(gmem=false): one CRB of RB_CCU_CACHE_CNTL and the VPC/PC buffers."""
    c = gmem_config_sysmem()
    cs.crb(reg('RB_CCU_CACHE_CNTL', depth_cache_size=c['depth_cache_fraction'], depth_offset=c['depth_ccu_offset'],
               color_cache_size=c['color_cache_fraction'], color_offset=c['color_ccu_offset']),
           reg('VPC_ATTR_BUF_GMEM_SIZE', value=c['vpc_attr_buf_size']),
           reg('VPC_ATTR_BUF_GMEM_BASE', value=c['vpc_attr_buf_offset']),
           reg('PC_ATTR_BUF_GMEM_SIZE', value=c['vpc_attr_buf_size']),
           reg('VPC_POS_BUF_GMEM_SIZE', value=c['vpc_pos_buf_size']),
           reg('VPC_POS_BUF_GMEM_BASE', value=c['vpc_pos_buf_offset']),
           reg('PC_POS_BUF_GMEM_SIZE', value=c['vpc_pos_buf_size']),
           reg('VPC_BV_POS_BUF_GMEM_BASE', value=c['vpc_bv_pos_buf_offset']),
           reg('VPC_BV_POS_BUF_GMEM_SIZE', value=c['vpc_bv_pos_buf_size']),
           reg('PC_BV_POS_BUF_GMEM_SIZE', value=c['vpc_bv_pos_buf_size']))


def emit_lrz_inv(cs):
    """fd6_lrz_inv<A8XX> (fd6_emit.h)."""
    cs.crb(reg('GRAS_LRZ_CNTL', enable=True), reg('GRAS_LRZ_CNTL2', disable_on_wrong_dir=True, fc_enable=True),
           reg('RB_LRZ_CNTL2'))
    cs.event('LRZ_CACHE_FLUSH')
    cs.crb(reg('GRAS_LRZ_CNTL'), reg('GRAS_LRZ_CNTL2'), reg('RB_LRZ_CNTL2'))


# a8xx_base_raw_magic_regs (freedreno_devices.py): gen8 moves the chicken bits into the kernel; these remain
# for userspace and fd6_emit_static_non_context_regs writes them first.
MAGIC_RAW = (('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 0, 0), ('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 1, 0),
             ('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 2, 0), ('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 3, 0),
             ('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 4, 0), ('GRAS_BIN_FOVEAT_XY_FDM_OFFSET', 5, 0),
             ('RB_UNKNOWN_8818', 0, 0), ('RB_UNKNOWN_8819', 0, 0), ('RB_UNKNOWN_881A', 0, 0), ('RB_UNKNOWN_881B', 0, 0),
             ('RB_UNKNOWN_881C', 0, 0), ('RB_UNKNOWN_881D', 0, 0), ('RB_UNKNOWN_881E', 0, 0), ('RB_LRZ_CNTL2', 0, 0),
             ('RB_RESOLVE_CNTL_5', 0, 1), ('SP_UNKNOWN_AB01', 0, 0), ('SP_HLSQ_MODE_CNTL', 0, 0),
             ('SP_UNKNOWN_AB23', 0, 0), ('TPL1_PS_ROTATION_CNTL', 0, 4), ('TPL1_PS_SWIZZLE_CNTL', 0, 0),
             ('PC_UNKNOWN_980B', 0, 0x00800280), ('PC_MODE_CNTL', 0, 0x00003F00))


def _raw(name, index, value):
    offset, _bits, _fields, stride, length = X.REGS[name]
    if not 0 <= index < length:
        raise Refused(f'{name}[{index}]')
    return [(offset + index * stride, value)]


def emit_restore(cs, border_va):
    """fd6_emit_restore<A8XX> + fd6_emit_static_regs<A8XX> (inlined: gallium keeps the static half in its own
    IB, fd6_context->restore) + fd6_emit_gmem_cache_cntl(sysmem). Order and values as the reference capture."""
    cs.pkt7(R.CP_SET_MODE, 0)
    cs.pkt7(R.CP_THREAD_CONTROL, pkt_word('CP_THREAD_CONTROL_0', thread='CP_SET_THREAD_BR',
                                          concurrent_bin_disable=True))
    emit_lrz_inv(cs)
    cs.event('CCU_INVALIDATE_COLOR')
    cs.event('CCU_INVALIDATE_DEPTH')
    cs.pkt7(R.CP_CCHE_INVALIDATE)
    cs.event('CACHE_INVALIDATE7')
    cs.wfi()
    cs.pkt4(reg('SP_UPDATE_CNTL', vs_state=True, hs_state=True, ds_state=True, gs_state=True, fs_state=True,
                cs_state=True))
    cs.wfi()
    # fd6_emit_static_non_context_regs<A8XX>
    cs.ncrb(*[_raw(n, i, v) for n, i, v in MAGIC_RAW],
            reg('SP_PERFCTR_SHADER_MASK', vs=True, hs=True, ds=True, gs=True, fs=True, cs=True),
            reg('GRAS_SC_SCREEN_SCISSOR_CNTL'), reg('GRAS_BIN_FOVEAT'), reg('RB_BIN_FOVEAT'),
            reg('RB_A2D_DEST_BUFFER_ARRAY_PITCH', value=0))
    # fd6_emit_static_context_regs<A8XX>
    regs = [reg('SP_GFX_USIZE'), _raw('TPL1_PS_ROTATION_CNTL', 0, 0), reg('SP_UNKNOWN_A9A8'),
            reg('VFD_MODE_CNTL', vertex=True, instance=True), _raw('RB_MODE_CNTL', 0, 0x10),
            reg('GRAS_LRZ_PS_INPUT_CNTL'), reg('GRAS_LRZ_PS_SAMPLEFREQ_CNTL'), _raw('GRAS_MODE_CNTL', 0, 0x2),
            reg('RB_UNKNOWN_8818'), reg('VPC_REPLACE_MODE_CNTL'), reg('VPC_ROTATION_CNTL'),
            reg('VPC_SO_OVERRIDE', disable=True), reg('VPC_RAST_STREAM_CNTL'), reg('PC_STEREO_RENDERING_CNTL'),
            _raw('TPL1_PS_SWIZZLE_CNTL', 0, 0), reg('GRAS_SU_CONSERVATIVE_RAS_CNTL'), reg('GRAS_SU_VS_SIV_CNTL'),
            reg('GRAS_SC_CNTL'),
            reg('SP_REG_PROG_ID_3', linelengthregid=0xFC, foveationqualityregid=0xFC),
            reg('VFD_RENDER_MODE', render_mode='RENDERING_PASS'), reg('VFD_STEREO_RENDERING_CNTL'),
            reg('VPC_SO_CNTL'), reg('GRAS_LRZ_CNTL'), reg('GRAS_LRZ_CNTL2'), reg('RB_LRZ_CNTL'),
            reg('RB_DEPTH_PLANE_CNTL'), reg('GRAS_SU_DEPTH_PLANE_CNTL')]
    regs += [reg('VFD_VERTEX_BUFFER_SIZE', i, value=0) for i in range(32)]
    regs += [reg('TPL1_GFX_BORDER_COLOR_BASE', value=border_va), reg('TPL1_CS_BORDER_COLOR_BASE', value=border_va),
             reg('PC_DGEN_SU_CONSERVATIVE_RAS_CNTL'), _raw('VPC_UNKNOWN_CNTL', 0, 0),
             reg('PC_HS_BUFFER_SIZE', value=0x40000), reg('PC_TF_BUFFER_SIZE', value=0x4040),
             _raw('GRAS_LRZ_QUALITY_LOOKUP_TABLE_REG', 0, 0x09510840),
             _raw('GRAS_LRZ_QUALITY_LOOKUP_TABLE_REG', 1, 0x00000A62),
             reg('SP_PS_CNTL_0')]
    cs.crb(*regs)
    cs.pkt7(R.CP_SET_DRAW_STATE, pkt_word('CP_SET_DRAW_STATE__0', disable_all_groups=True), 0, 0)
    emit_gmem_cache_cntl_sysmem(cs)


def emit_static_binning_regs(cs):
    """fd7_emit_static_binning_regs<A8XX>(gmem=false): NCRB as the reference capture."""
    cs.ncrb(reg('RB_BUFFER_CNTL', **{f'{b}_sysmem': True for b in ('z', 's', 'rt0', 'rt1', 'rt2', 'rt3', 'rt4', 'rt5',
                                                                    'rt6', 'rt7')}),
            reg('GRAS_LRZ_CB_CNTL'), reg('GRAS_MODE_CNTL', value=0x2),
            reg('RB_CLEAR_TARGET', clear_mode='CLEAR_MODE_SYSMEM'))


def emit_lrz_off(cs):
    """emit_lrz with no depth buffer (fd6_gmem.cc): every LRZ buffer register zero."""
    cs.crb(reg('GRAS_LRZ_BUFFER_BASE', value=0), reg('GRAS_LRZ_BUFFER_PITCH'), reg('GRAS_LRZ_VIEW_INFO'),
           reg('GRAS_LRZ_FAST_CLEAR_BUFFER_BASE', value=0), reg('GRAS_LRZ_DEPTH_BUFFER_INFO'),
           reg('GRAS_LRZ_CNTL2'), reg('GRAS_LRZ_BUFFER_SLICE_PITCH'))


def emit_flushes(cs, ccu_color=False, inv_ccu_color=False, ccu_depth=False, inv_ccu_depth=False,
                 cache=False, inv_cche=False, wfi=False):
    """fd6_emit_flushes<A8XX> (fd6_barrier.cc): clean before invalidate, then the gen8 DUMMY/SUBPASS_FENCE/DUMMY
    triple, then CCHE invalidate behind a WFI."""
    if ccu_color or inv_ccu_color:
        cs.event('CCU_CLEAN_COLOR')
    if ccu_depth or inv_ccu_depth:
        cs.event('CCU_CLEAN_DEPTH')
    if inv_ccu_color:
        cs.event('CCU_INVALIDATE_COLOR')
    if inv_ccu_depth:
        cs.event('CCU_INVALIDATE_DEPTH')
    if cache:
        cs.event('CACHE_CLEAN')
    cs.event('DUMMY_EVENT')
    cs.event('SUBPASS_FENCE')
    cs.event('DUMMY_EVENT')
    if inv_cche:
        cs.wfi()
        cs.pkt7(R.CP_CCHE_INVALIDATE)
    if wfi:
        cs.wfi()


def emit_ib_fence(cs, memptrs_va, value):
    """The userspace fence at the end of every gallium batch (fd6_fence_write/emit_common_fini's
    CACHE_FLUSH_TS with WRITE_ENABLED): here into memptrs + CP_IB_FENCE_OFF."""
    cs.event('CACHE_FLUSH_TS', memptrs_va + R.CP_IB_FENCE_OFF, value)


# ============================================================================================================
# 2D engine (P6, and P7's clear): gallium fd6_blitter.cc / fd6_gmem.cc emit_sysmem_clears
# ============================================================================================================
FMT_8888 = 'FMT6_8_8_8_8_UNORM'


def swap_for(layout):
    """fd6_color_swap for the two 8888 layouts: BGRA in memory (B8G8R8A8_UNORM, GOP format 1) is WXYZ, RGBA
    (R8G8B8A8_UNORM, GOP format 0) is WZYX (fd6_format_table.c)."""
    return {'bgra': 'WXYZ', 'rgba': 'WZYX'}[layout]


def unorm8(x):
    """float_to_ubyte (u_math.h): clamp, then round to nearest."""
    x = min(max(float(x), 0.0), 1.0)
    return int(x * 255.0 + 0.5)


def blit_setup_regs(solid):
    """emit_blit_setup<A8XX>(FMT6_8_8_8_8_UNORM, R2D_UNORM8): RB/GRAS/TPL1 BLT_CNTL, SP_A2D_OUTPUT_INFO, PIXEL_CNTL."""
    common = dict(rotate='ROTATE_0', solid_color=solid, color_format=FMT_8888, mask=0xF, ifmt='R2D_UNORM8')
    return [reg('RB_A2D_BLT_CNTL', **common), reg('GRAS_A2D_BLT_CNTL', **common),
            reg('TPL1_A2D_BLT_CNTL', type='A6XX_TEX_2D'),
            reg('SP_A2D_OUTPUT_INFO', ifmt_type='OUTPUT_IFMT_2D_FLOAT', color_format=FMT_8888, mask=0xF),
            reg('RB_A2D_PIXEL_CNTL', pixel_op='PIXEL_OP_DISABLED')]


def dst_regs(va, pitch, layout):
    """emit_blit_dst<A8XX> for a linear 8888 surface (no UBWC)."""
    return [reg('RB_A2D_DEST_BUFFER_INFO', color_format=FMT_8888, tile_mode='TILE6_LINEAR',
                color_swap=swap_for(layout), samples='MSAA_ONE'),
            reg('RB_A2D_DEST_BUFFER_BASE', value=va), reg('RB_A2D_DEST_BUFFER_PITCH', value=pitch)]


def emit_2d_clear(cs, dst_va, pitch, layout, rect, rgba):
    """fd6_clear_surface<A8XX> (as emit_sysmem_clears calls it): marker BLIT2DSCALE, one NCRB with the
    rectangle, the clear colour (RGBA order: the swap applies on output) and the setup, one NCRB with the
    destination, DEBUG_LABEL, CP_BLIT(BLIT_OP_SCALE)."""
    x, y, w, h = rect
    cs.marker('RM6_BLIT2DSCALE')
    cs.ncrb(reg('GRAS_A2D_DEST_TL', x=x, y=y), reg('GRAS_A2D_DEST_BR', x=x + w - 1, y=y + h - 1),
            *[reg(f'RB_A2D_CLEAR_COLOR_DW{i}', value=unorm8(c)) for i, c in enumerate(rgba)],
            *blit_setup_regs(True))
    cs.ncrb(*dst_regs(dst_va, pitch, layout))
    cs.event('DEBUG_LABEL')
    cs.pkt7(R.CP_BLIT, pkt_word('CP_BLIT_0', op='BLIT_OP_SCALE'))


def emit_2d_copy(cs, src_va, src_pitch, src_size, src_rect, dst_va, dst_pitch, dst_xy, layout):
    """fd6_blit -> emit_blit_texture<A8XX>: emit_setup (flushes, BLIT2DSCALE, sysmem CCU), coordinates and setup
    in one NCRB, source and destination in another (emit_blit_src/emit_blit_dst), DEBUG_LABEL, CP_BLIT."""
    sx, sy, w, h = src_rect
    dx, dy = dst_xy
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True, ccu_depth=True, inv_ccu_depth=True)
    cs.marker('RM6_BLIT2DSCALE')
    emit_gmem_cache_cntl_sysmem(cs)
    cs.ncrb(reg('GRAS_A2D_SRC_XMIN', value=sx), reg('GRAS_A2D_SRC_XMAX', value=sx + w - 1),
            reg('GRAS_A2D_SRC_YMIN', value=sy), reg('GRAS_A2D_SRC_YMAX', value=sy + h - 1),
            reg('GRAS_A2D_DEST_TL', x=dx, y=dy), reg('GRAS_A2D_DEST_BR', x=dx + w - 1, y=dy + h - 1),
            *blit_setup_regs(False))
    sw, shh = src_size
    cs.ncrb(reg('TPL1_A2D_SRC_TEXTURE_INFO', color_format=FMT_8888, tile_mode='TILE6_LINEAR',
                color_swap=swap_for(layout), samples='MSAA_ONE', unk20=True, unk22=True),
            reg('TPL1_A2D_SRC_TEXTURE_SIZE', width=sw, height=shh),
            reg('TPL1_A2D_SRC_TEXTURE_BASE', value=src_va), reg('TPL1_A2D_SRC_TEXTURE_PITCH', pitch=src_pitch),
            reg('TPL1_A2D_SRC_TEXTURE_FLAG_BASE', value=0), reg('TPL1_A2D_SRC_TEXTURE_FLAG_PITCH', value=0),
            *dst_regs(dst_va, dst_pitch, layout))
    cs.event('DEBUG_LABEL')
    cs.pkt7(R.CP_BLIT, pkt_word('CP_BLIT_0', op='BLIT_OP_SCALE'))
    emit_flushes(cs, ccu_color=True, ccu_depth=True, cache=True, wfi=True)


def emit_nondraw_batch(cs, win, body):
    """A gallium nondraw batch in sysmem (fd6_emit_sysmem_prep with nondraw, fd6_emit_sysmem, fd6_emit_sysmem_fini):
    restore, LRZ flush, CCU config, static binning regs, render control, LRZ off, the body, the fini flushes
    and the batch fence. Skipped on purpose (preemption and queries only): CP_SET_AMBLE, ZPASS_DONE."""
    emit_restore(cs, win.addr('p67_misc', 0, 0x1000, False, 'border colour table (zeroed)'))
    cs.event('LRZ_CACHE_FLUSH')
    emit_gmem_cache_cntl_sysmem(cs)
    emit_static_binning_regs(cs)
    cs.crb(reg('RB_RENDER_CNTL', raster_mode='TYPE_TILED', raster_direction='LR_TB'), reg('GRAS_SU_RENDER_CNTL'))
    emit_lrz_off(cs)
    body(cs)
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True, cache=True, inv_cche=True, wfi=True)
    cs.pkt7(R.CP_SKIP_IB2_ENABLE_GLOBAL, 0)


# ============================================================================================================
# P7: the pipeline, its inputs, and the a8xx program state (fd6_program.cc, ported for VS + FS only)
# ============================================================================================================
P7_SHADERS = ASSETS / 'p7-shaders.json'
P7_SHADERS_SHA256 = '88aab4e2d9be944904ad700f32850f152e331e435a398494bf86390bc4004b31'   # tools/a16gpu-p7/make-p7-shaders.sh (air2ir3 c1d8fcf04b9eef02, Mesa bbeb2dca9fee)
P7_GOLDEN = ASSETS / 'p7-golden-m3max.bgra'
P7_GOLDEN_SHA256 = 'cb9c7424cb3ced93d2fdafe32ee1939c809b67f238fb9db2d27df6c3729e6ea0'    # p7_metal_golden.swift on the M3 Max, macOS 27 26A428
SKYLIGHT_METALLIB = '/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/Resources/SkyLightShaders.air64.metallib'
SKYLIGHT_SHA256 = '9c67e83e8bf9578e8161ed769cfdcabd30e4deeadd84c03a6d223e0200494183'   # Stage 6 corpus/index.json

# The scene: one triangle of SkyLight's SimpleVertex (float2 position, float2 texcoord, UBO 1 = float4x4 mvp)
# and SimpleTextureFragment (texture(0) sampled with sampler(0)), into a 96x64 BGRA8 target cleared first.
# The pixel-space vertices and the orthographic matrix are the Stage 6 scene sl_simple_texture's (its first
# triangle); the texture is generated here (p7_texture). One source of truth for the tool, the model and the
# Metal golden (--write-scene).
P7 = {
    'width': 96, 'height': 64, 'pitch': 384, 'clear': (0.1, 0.2, 0.3, 1.0),
    'vertices': (8.0, 4.0, 0.0, 0.0, 88.0, 4.0, 1.0, 0.0, 8.0, 60.0, 0.0, 1.0), 'stride': 16, 'vertex_count': 3,
    'attributes': ({'format': 'float2', 'offset': 0}, {'format': 'float2', 'offset': 8}),
    'mvp': (2.0 / 96, 0.0, 0.0, 0.0, 0.0, -2.0 / 64, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, -1.0, 1.0, 0.0, 1.0),
    'mvp_buffer_index': 1,
    'texture': {'width': 64, 'height': 64, 'format': 'rgba8', 'pitch': 256},
    'sampler': {'filter': 'linear', 'address': 'clamp_to_edge', 'mip': 'none'},
    'tolerance': 3,            # per channel, in 1/255 (the Stage 6 goldens' tolerance, results/exec/compare.json)
    'edge_budget': 16,         # pixels over tolerance allowed, and only within one pixel of a triangle edge
}
P7_DATA = {'vbo': 0x000, 'ubo': 0x100, 'tex_desc': 0x200, 'samp_desc': 0x300}
P7_CODE = {'vs': 0x0000, 'fs': 0x1000}
P7_RT_GUARD = 0x1000


def p7_texture():
    """64x64 RGBA8: smooth ramps plus an 8x8 checker of +-24, so filtering is visible but no step is so
    sharp that a filter-weight rounding difference between Apple and Adreno exceeds the tolerance."""
    out = bytearray()
    for y in range(64):
        for x in range(64):
            c = 24 if (x // 8 + y // 8) % 2 else -24
            out += bytes((min(255, max(0, 40 + 3 * x + c)), min(255, max(0, 40 + 3 * y - c)),
                          min(255, max(0, 200 - x - y + c)), 255))
    return bytes(out)


def p7_vbo():
    return struct.pack('<12f', *P7['vertices'])


def p7_ubo():
    return struct.pack('<16f', *P7['mvp'])


def p7_assets(check=True):
    """The exported ir3 variants (p7-shaders.json) and the M3 Max golden, both sha-pinned."""
    if not P7_SHADERS.is_file():
        raise Stop('T0', f'{P7_SHADERS} is not distributed (it is compiled from shaders shipped with macOS): '
                         'generate it with tools/a16gpu-p7/make-p7-shaders.sh')
    blob = P7_SHADERS.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if check and P7_SHADERS_SHA256 and digest != P7_SHADERS_SHA256:
        raise Stop('T0', f'{P7_SHADERS} sha256 {digest} is not the pinned {P7_SHADERS_SHA256}')
    sh = json.loads(blob)
    golden = P7_GOLDEN.read_bytes() if P7_GOLDEN.is_file() else None
    if check:
        if golden is None:
            raise Stop('T0', f'{P7_GOLDEN} missing: produce it with tools/a16gpu-p7/p7_metal_golden.swift')
        gd = hashlib.sha256(golden).hexdigest()
        if P7_GOLDEN_SHA256 and gd != P7_GOLDEN_SHA256:
            raise Stop('T0', f'{P7_GOLDEN} sha256 {gd} is not the pinned {P7_GOLDEN_SHA256}')
        if len(golden) != P7['width'] * P7['height'] * 4:
            raise Stop('T0', f'{P7_GOLDEN} is {len(golden)} bytes, not {P7["width"]}x{P7["height"]}x4')
    return sh, golden, digest


INVALID_REG = 0xFC                                   # regid(63, 0): ir3's "no register"
SLOT_POS, SLOT_PSIZ, SLOT_VAR0 = 0, 1, 32            # gl_varying_slot VARYING_SLOT_POS / PSIZ / VAR0
FRAG_RESULT_DEPTH, FRAG_RESULT_STENCIL, FRAG_RESULT_SAMPLE_MASK, FRAG_RESULT_DATA0 = 0, 1, 2, 4   # shader_enums.h


def variant_checks(sh):
    """What this port covers (VS + FS, sysmem, no tess/GS/streamout/clip/psize/private memory/driver UBOs/
    bindless/sampler prefetch), the Stage 6 verifier's points (constlen budget, private memory, driver UBOs)
    and the facts the draw depends on. A variant outside it is refused, never approximated."""
    problems, findings = [], []
    vs, fs = sh['vs'], sh['fs']
    if sh.get('chip_id') != '0x44070041':
        problems.append(f'variants compiled for {sh.get("chip_id")}, not the X2-90 0x44070041')
    for name, v in (('vs', vs), ('fs', fs)):
        if v['pvtmem_size']:
            problems.append(f'{name} needs {v["pvtmem_size"]} bytes of private memory (none is allocated)')
        if v['shared_size']:
            problems.append(f'{name} uses shared memory')
        cs = v['const_state']
        for kind in ('consts_ubo', 'driver_params_ubo', 'primitive_map_ubo', 'primitive_param_ubo'):
            if cs[kind] != -1:
                problems.append(f'{name} has an ir3 driver UBO ({kind} {cs[kind]}): BINDING-ABI 3.9 binding not built')
        if any(v['bindless']) or v['fb_read'] or v['num_uavs'] or v['has_ssbo']:
            problems.append(f'{name} uses bindless state, framebuffer fetch or UAVs')
        # constlen is in vec4; a8xx allows 512 (geometry) / 512 (FS) vec4 per stage (ir3_compiler max_const_*);
        # every UBO range the preamble pushes must fit inside the programmed length.
        if v['constlen'] > 512 or v['constlen'] % 4:
            problems.append(f'{name} constlen {v["constlen"]} vec4 is not a multiple of 4 below 512')
        for rng in cs['ubo_ranges']:
            if rng['bindless'] or rng['offset'] + (rng['end'] - rng['start']) > 16 * v['constlen']:
                problems.append(f'{name} pushes UBO {rng["block"]} bytes {rng["start"]}..{rng["end"]} to const '
                                f'{rng["offset"]} beyond constlen {v["constlen"]} vec4')
        if v['early_preamble'] and name == 'fs':
            problems.append('fs early preamble')
        # Each stage's code has a 4 KiB slot in p7_code (VS +0, FS +0x1000): the bytes uploaded and the
        # window the SP fetches (SP_*_INSTR_SIZE, 128-byte units) must both stay inside it.
        code_len = len(v.get('code_hex', '')) // 2
        if not 0 < code_len <= 0x1000 or not 0 < v['instrlen'] * 128 <= 0x1000 or code_len > v['instrlen'] * 128:
            problems.append(f'{name} code {code_len} bytes / instrlen {v["instrlen"]} does not fit its 4 KiB slot')
    if vs['clip_mask'] or vs['cull_mask'] or vs['stream_outputs'] or vs['writes_psize']:
        problems.append('vs writes clip/cull distances, stream output or point size')
    if fs['num_sampler_prefetch'] or fs['sample_shading'] or fs['frag_face'] or fs['fragcoord_compmask'] \
            or fs['reads_primid'] or fs['dual_src_blend'] or fs['yuv_color'] or fs['color0_mrt']:
        problems.append('fs uses prefetch, sample shading, face, fragcoord, primitive id, dual source or yuv')
    if any(i['sysval'] for i in vs['inputs']):
        problems.append('vs reads system values (VFD_CNTL_1 regids are INVALID in this port)')
    ubos = [b for b in vs['sidecar']['reflection']['bindings'] if b['kind'] == 'ubo' and b['present']]
    if [(b['slot'], b['type']) for b in ubos] != [(P7['mvp_buffer_index'], 'float4x4')]:
        problems.append(f'vs UBO bindings {ubos} are not the one float4x4 at buffer({P7["mvp_buffer_index"]})')
    tex = [b for b in fs['sidecar']['reflection']['bindings'] if b['kind'] in ('texture', 'sampler') and b['present']]
    if sorted((b['kind'], b['slot']) for b in tex) != [('sampler', 0), ('texture', 0)]:
        problems.append(f'fs texture/sampler bindings {tex} are not T#0 and S#0')
    if fs['sidecar']['reflection']['constexpr_sampler_mask'] != '0x0000':
        problems.append('fs has constexpr samplers (their S# slots are not written here)')
    # Stage 6 facts that differ from what Mesa's own GLSL path computes for the same shader (the reference
    # capture gl-tri-bgra.rd, GLSL compiled by Mesa): nir_shader_gather_info never ran on the AIR-derived NIR,
    # so textures_used and the quad-helper flags are empty. The descriptor counts are taken from the binding
    # reflection instead (BINDING-ABI 3.3/3.4); pixel LOD from the texture access.
    ntex = 1 + max(b['slot'] for b in tex if b['kind'] == 'texture')
    nsamp = 1 + max(b['slot'] for b in tex if b['kind'] == 'sampler')
    if fs['num_samp'] < max(ntex, nsamp):
        findings.append(f'fs num_samp {fs["num_samp"]} (air2ir3 never fills nir info.textures_used) is below the '
                        f'bound T#/S# ({ntex}/{nsamp}): SP_PS_CONFIG NTEX/NSAMP programmed from the reflection')
    sampled = any(b['kind'] == 'texture' and b.get('access') == 'sample' for b in tex)
    if sampled and not fs['need_pixlod']:
        findings.append('fs samples with implicit LOD but need_pixlod is 0 (nir gather_info not run): '
                        'PIXLODENABLE programmed as Mesa does for the same GLSL shader')
    return problems, findings, {'ntex': max(ntex, fs['num_samp']), 'nsamp': max(nsamp, fs['num_samp']),
                                'pixlod': bool(fs['need_pixlod'] or sampled)}


def output_regid(v, slot):
    for o in v['outputs']:
        if o['slot'] == slot:
            return o['regid'] | (0x100 if o['half'] else 0)
    return INVALID_REG


def sysval_regid(v, name):
    return v['sysval_regids'].get(name, INVALID_REG)


def link_vpc(sh):
    """emit_vpc's linkage: ir3_link_shaders (exported), then position (and nothing else in this port) appended
    at max_loc with ir3_link_add. VPC_VARYING_LM_TRANSFER_CNTL_DISABLE is written from the varmask *before*
    position is added (emit_vpc order), kept as fs_varmask."""
    link = dict(sh['link_vs_fs'])
    link['var'] = [dict(v) for v in link['var']]
    link['varmask'] = list(link['varmask'])
    link['fs_varmask'] = list(link['varmask'])
    pos = output_regid(sh['vs'], SLOT_POS)
    position_loc = 0xFF
    if pos != INVALID_REG:
        position_loc = link['max_loc']
        for j in range(4):                                   # util_last_bit(0xf) = 4
            loc = position_loc + j
            link['varmask'][loc // 32] |= 1 << (loc % 32)
        link['max_loc'] = max(link['max_loc'], position_loc + 4)
        link['var'].append({'slot': SLOT_POS, 'regid': pos, 'compmask': 0xF, 'loc': position_loc})
        link['cnt'] += 1
    link['position_loc'] = position_loc
    return link


def program_config(sh, sampling):
    """setup_config_stateobj<A8XX> (draw-state group PROG_CONFIG)."""
    vs, fs = sh['vs'], sh['fs']
    return [reg('SP_UPDATE_CNTL', vs_state=True, hs_state=True, ds_state=True, gs_state=True, fs_state=True,
                cs_state=True),
            reg('SP_VS_CONST_CONFIG', constlen=vs['constlen'], enabled=True),
            reg('SP_HS_CONST_CONFIG'), reg('SP_DS_CONST_CONFIG'), reg('SP_GS_CONST_CONFIG'),
            reg('SP_PS_CONST_CONFIG', constlen=fs['constlen'], enabled=True),
            reg('SP_VS_CONFIG', enabled=True, ntex=vs['num_samp'], nsamp=vs['num_samp'], nuav=vs['num_uavs']),
            reg('SP_HS_CONFIG'), reg('SP_DS_CONFIG'), reg('SP_GS_CONFIG'),
            reg('SP_PS_CONFIG', enabled=True, ntex=sampling['ntex'], nsamp=sampling['nsamp'], nuav=fs['num_uavs']),
            reg('SP_GFX_USIZE', value=fs['num_uavs'])]


def shader_regs(v, stage, base_va, sampling):
    """emit_shader_regs<A8XX> (fd6_program.cc) for the VS or the FS; no private memory (variant_checks)."""
    out = [reg('SP_MODE_CNTL', constant_demotion_enable=True, isammode='ISAMMODE_GL'),
           reg('TPL1_MODE_CNTL', isammode='ISAMMODE_GL', texcoordroundmode='COORD_TRUNCATE',
               arraycoordroundmode='COORD_TRUNCATE', nearestmipsnap='CLAMP_ROUND_TRUNCATE',
               destdatatypeoverride=True, clamp_disable=True)]
    info = v['info']
    common = dict(halfregfootprint=info['max_half_reg'] + 1, fullregfootprint=info['max_reg'] + 1,
                  branchstack=v['branchstack_hw'])
    if stage == 'vs':
        out += [reg('SP_VS_CNTL_0', mergedregs=bool(v['mergedregs']), earlypreamble=bool(v['early_preamble']),
                    **common),
                reg('SP_VS_INSTR_SIZE', value=v['instrlen']), reg('SP_VS_PROGRAM_COUNTER_OFFSET'),
                reg('SP_VS_BASE', value=base_va), reg('SP_VS_PVT_MEM_PARAM', memsizeperitem=0),
                reg('SP_VS_PVT_MEM_BASE', value=0), reg('SP_VS_PVT_MEM_SIZE', totalpvtmemsize=0, perwavememlayout=False),
                reg('SP_VS_PVT_MEM_STACK_OFFSET', offset=0), reg('SP_VS_VGS_CNTL')]
    else:
        out += [reg('SP_PS_CNTL_0', threadsize='THREAD128' if info['double_threadsize'] else 'THREAD64',
                    varying=v['total_in'] != 0, lodpixmask=bool(v['need_full_quad']), inoutregoverlap=True,
                    pixlodenable=sampling['pixlod'], earlypreamble=bool(v['early_preamble']),
                    mergedregs=bool(v['mergedregs']), **common),
                reg('RB_PS_CNTL', pixlodenable=sampling['pixlod'], lodpixmask=bool(v['need_full_quad'])),
                reg('SP_PS_INSTR_SIZE', value=v['instrlen']), reg('SP_PS_PROGRAM_COUNTER_OFFSET'),
                reg('SP_PS_BASE', value=base_va), reg('SP_PS_PVT_MEM_PARAM', memsizeperitem=0),
                reg('SP_PS_PVT_MEM_BASE', value=0), reg('SP_PS_PVT_MEM_SIZE', totalpvtmemsize=0, perwavememlayout=False),
                reg('SP_PS_PVT_MEM_STACK_OFFSET', offset=0), reg('SP_PS_VGS_CNTL')]
    return out


def program_linkage(sh):
    """setup_stateobj<A8XX>'s CRB: PC_STEREO_RENDERING_CNTL, emit_vfd_dest, emit_vpc (with emit_vs_system_values),
    emit_fs_inputs, emit_fs_outputs."""
    vs, fs = sh['vs'], sh['fs']
    out = [reg('PC_STEREO_RENDERING_CNTL')]
    attrs = [i for i in vs['inputs'] if not i['sysval']]
    out.append(reg('VFD_CNTL_0', fetch_cnt=len(attrs), decode_cnt=len(attrs)))
    out.append(reg('PC_VS_INPUT_CNTL', instr_cnt=len(attrs), sideband_cnt=0))
    for n, a in enumerate(attrs):
        out.append(reg('VFD_DEST_CNTL_INSTR', n, writemask=a['compmask'], regid=a['regid']))
    link = link_vpc(sh)
    # emit_vs_system_values: no sysvals in this port (variant_checks), every regid INVALID
    out += [reg('VFD_CNTL_1', regid4vtx=INVALID_REG, regid4inst=INVALID_REG, regid4primid=INVALID_REG,
                regid4viewid=INVALID_REG),
            reg('VFD_CNTL_2', regid_hsrelpatchid=INVALID_REG, regid_invocationid=INVALID_REG),
            reg('VFD_CNTL_3', regid_dsprimid=INVALID_REG, regid_dsrelpatchid=INVALID_REG, regid_tessx=INVALID_REG,
                regid_tessy=INVALID_REG),
            reg('VFD_CNTL_4', regid_dsviewid=INVALID_REG),
            reg('VFD_CNTL_5', regid_gsheader=INVALID_REG, regid_gsviewid=INVALID_REG),
            reg('VFD_CNTL_6', primid4psen=bool(fs['reads_primid']))]
    for i in range(4):
        out.append(_raw('VPC_VARYING_LM_TRANSFER_CNTL_DISABLE', i, ~link['fs_varmask'][i] & 0xFFFFFFFF))
    sp_out = [0] * 32
    sp_vpc = [0] * 32
    for i, var in enumerate(link['var']):
        sp_out[i] = (var['regid'] & 0xFF) | (var['compmask'] & 0xF) << 8
        sp_vpc[i] = var['loc'] & 0xFF
    for i in range((link['cnt'] + 1) // 2):
        out.append(_raw('SP_VS_OUTPUT_REG', i, sp_out[2 * i] | sp_out[2 * i + 1] << 16))
    for i in range((link['cnt'] + 3) // 4):
        out.append(_raw('SP_VS_VPC_DEST_REG', i, sp_vpc[4 * i] | sp_vpc[4 * i + 1] << 8 | sp_vpc[4 * i + 2] << 16
                        | sp_vpc[4 * i + 3] << 24))
    out += [reg('VPC_VS_CNTL', stride_in_vpc=link['max_loc'], positionloc=link['position_loc'], psizeloc=0xFF),
            reg('VPC_VS_CLIP_CULL_CNTL', clip_mask=0, clip_dist_03_loc=link['clip0_loc'],
                clip_dist_47_loc=link['clip1_loc']),
            reg('GRAS_CL_VS_CLIP_CULL_DISTANCE', clip_mask=0, cull_mask=0),
            reg('PC_VS_CNTL', stride_in_vpc=link['max_loc'], psize=False, layer=False, view=False,
                primitive_id=False, clip_mask=0),
            reg('SP_VS_OUTPUT_CNTL', out=link['cnt']),
            reg('VPC_VS_SIV_CNTL', layerloc=0xFF, viewloc=0xFF, shadingrateloc=0xFF),
            reg('GRAS_SU_VS_SIV_CNTL', writes_layer=False, writes_view=False),
            reg('PC_PS_CNTL', primitiveiden=bool(fs['reads_primid'])),
            reg('GRAS_MODE_CNTL', value=0x2), reg('SP_RENDER_CNTL', fs_disable=False),
            reg('VPC_PS_CNTL', numnonposvar=fs['total_in'], primidloc=link['primid_loc'], varying=bool(fs['total_in']),
                viewidloc=link['viewid_loc'])]
    # emit_fs_inputs<A8XX>
    ij_names = ('SYSTEM_VALUE_BARYCENTRIC_PERSP_PIXEL', 'SYSTEM_VALUE_BARYCENTRIC_PERSP_SAMPLE',
                'SYSTEM_VALUE_BARYCENTRIC_PERSP_CENTROID', 'SYSTEM_VALUE_BARYCENTRIC_PERSP_CENTER_RHW',
                'SYSTEM_VALUE_BARYCENTRIC_LINEAR_PIXEL', 'SYSTEM_VALUE_BARYCENTRIC_LINEAR_CENTROID',
                'SYSTEM_VALUE_BARYCENTRIC_LINEAR_SAMPLE')
    ij = {n.split('BARYCENTRIC_')[1].lower(): sysval_regid(fs, n) for n in ij_names}
    face, sampid, smask, coord = (sysval_regid(fs, 'SYSTEM_VALUE_' + n) for n in
                                  ('FRONT_FACE', 'SAMPLE_ID', 'SAMPLE_MASK_IN', 'FRAG_COORD'))
    valid = lambda r: r != INVALID_REG
    zwcoord = coord + 2 if valid(coord) else INVALID_REG
    out.append(reg('SP_PS_INITIAL_TEX_LOAD_CNTL', count=0, ij_write_disable=not valid(ij['persp_pixel']),
                   endofquad=False, constslotid=0x1FF, constslotid4coord=0x1FF))
    out += [reg('SP_LB_PARAM_LIMIT', primallocthreshold=7), reg('RB_LB_PARAM_LIMIT', primallocthreshold=7),
            reg('SP_REG_PROG_ID_0', faceregid=face, sampleid=sampid, samplemask=smask, centerrhw=ij['persp_center_rhw']),
            reg('SP_REG_PROG_ID_1', ij_persp_pixel=ij['persp_pixel'], ij_linear_pixel=ij['linear_pixel'],
                ij_persp_centroid=ij['persp_centroid'], ij_linear_centroid=ij['linear_centroid']),
            reg('SP_REG_PROG_ID_2', ij_persp_sample=ij['persp_sample'], ij_linear_sample=ij['linear_sample'],
                xycoordregid=coord, zwcoordregid=zwcoord),
            reg('SP_REG_PROG_ID_3', linelengthregid=INVALID_REG, foveationqualityregid=INVALID_REG)]
    sysval_regs = sum((1 if k == 'persp_center_rhw' else 2) for k, r in ij.items() if valid(r))
    sysval_regs += sum(1 for r in (face, sampid, smask) if valid(r)) + sum(2 for r in (coord, zwcoord) if valid(r))
    out.append(reg('SP_PS_CNTL_1', sysval_regs_count=sysval_regs, defer_wave_alloc_dis=True, evict_buf_mode=1))
    out.append(reg('SP_PS_WAVE_CNTL', varyings=fs['total_in'] > 0))
    need_size = valid(ij['persp_center_rhw'])            # has_implicit_fragface_fragcoord_ij_linear (a7xx+)
    interp = dict(ij_persp_pixel=valid(ij['persp_pixel']), ij_persp_centroid=valid(ij['persp_centroid']),
                  ij_persp_sample=valid(ij['persp_sample']), ij_linear_pixel=valid(ij['linear_pixel']) or need_size,
                  ij_linear_centroid=valid(ij['linear_centroid']), ij_linear_sample=valid(ij['linear_sample']),
                  coord_mask=fs['fragcoord_compmask'])
    out += [reg('GRAS_CL_INTERP_CNTL', faceness=bool(fs['frag_face']), centerrhw=valid(ij['persp_center_rhw']),
                **interp),
            reg('RB_INTERP_CNTL', interp_en=fs['total_in'] > 0, **interp),
            reg('RB_PS_INPUT_CNTL', samplemask=valid(smask), postdepthcoverage=bool(fs['post_depth_coverage']),
                faceness=bool(fs['frag_face']), sampleid=valid(sampid), fragcoordsamplemode='FRAGCOORD_CENTER',
                centerrhw=valid(ij['persp_center_rhw'])),
            reg('RB_PS_SAMPLEFREQ_CNTL', per_samp_mode=False),
            reg('GRAS_LRZ_PS_INPUT_CNTL', sampleid=valid(sampid), fragcoordsamplemode='FRAGCOORD_CENTER'),
            reg('GRAS_LRZ_PS_SAMPLEFREQ_CNTL', per_samp_mode=False)]
    # emit_fs_outputs<A8XX> (no MSAA: the sample mask output is ignored)
    out.append(reg('SP_PS_OUTPUT_CNTL', dual_color_in_enable=False,
                   depth_regid=output_regid(fs, FRAG_RESULT_DEPTH) & 0xFF, sampmask_regid=INVALID_REG,
                   stencilref_regid=output_regid(fs, FRAG_RESULT_STENCIL) & 0xFF))
    regs, count = [], 0
    for i in range(8):
        r = output_regid(fs, FRAG_RESULT_DATA0 + i)
        regs.append(r)
        if r != INVALID_REG:
            count = i + 1
    for i in range(count):
        out.append(reg('SP_PS_OUTPUT_REG', i, regid=regs[i] & 0xFF, half_precision=bool(regs[i] & 0x100)))
    out += [reg('SP_PS_OUTPUT_CONST_CNTL', enabled=False), reg('SP_PS_OUTPUT_CONST_MASK')]
    mrt = sum(0xF << (4 * i) for i in range(count) if regs[i] != INVALID_REG)
    return out, link, mrt


def interp_regs(sh):
    """emit_interp_state<A8XX> with no flat/rasterflat/point-sprite inputs: every mode 0 (smooth)."""
    fs = sh['fs']
    vinterp = [0] * 8
    for inp in fs['inputs']:
        if inp['compmask'] and inp['bary'] and inp['flat']:
            loc = inp['inloc']
            for i in range(4):
                if inp['compmask'] & (1 << i):
                    vinterp[loc // 16] |= 1 << ((loc % 16) * 2)       # INTERP_FLAT = 1
                    loc += 1
    return [_raw('VPC_VARYING_INTERP_MODE_MODE', i, vinterp[i]) for i in range(8)] + \
           [_raw('VPC_VARYING_REPLACE_MODE_MODE', i, 0) for i in range(8)]


def guardband(offset, scale):
    """fd_calc_guardband (freedreno_guardband.h), a4xx+ limits."""
    gb_min, gb_max = -32768.0, 32767.0
    lo = (gb_min - offset) / abs(scale)
    hi = (gb_max - offset) / abs(scale)
    adj = min(-lo, hi)
    if adj < 1.0 or math.isinf(adj):
        return 0x1FF
    mant, exp = math.frexp(adj)
    if exp <= 0 or exp > 8:
        return 0x1FF
    return ((exp - 1) << 6) | (int(math.trunc(mant * (1 << 7))) - (1 << 6))


def texture_descriptor(va, width, height, pitch, layout):
    """fdl6_view_init<A8XX> for a linear 2D single-level 8888 texture (fd6_view.cc): 16 dwords."""
    d = [0] * 16
    d[0] = desc('A8XX_TEX_MEMOBJ', 0, base_lo=va & 0xFFFFFFFF)
    d[1] = desc('A8XX_TEX_MEMOBJ', 1, base_hi=va >> 32, type='A6XX_TEX_2D', depth=1)
    d[2] = desc('A8XX_TEX_MEMOBJ', 2, width=width, height=height, samples='MSAA_ONE')
    d[3] = desc('A8XX_TEX_MEMOBJ', 3, fmt=FMT_8888, swap=swap_for(layout), swiz_x='A8XX_SWIZ_X',
                swiz_y='A8XX_SWIZ_Y', swiz_z='A8XX_SWIZ_Z', swiz_w='A8XX_SWIZ_W')
    d[4] = desc('A8XX_TEX_MEMOBJ', 4, tile_mode='TILE6_LINEAR')
    d[6] = desc('A8XX_TEX_MEMOBJ', 6, tex_line_offset=pitch * 8, miplvls=0, min_line_offset=2)   # pitchalign 8 - 6
    d[7] = desc('A8XX_TEX_MEMOBJ', 7, array_slice_offset=pitch * height)
    d[9] = desc('A8XX_TEX_MEMOBJ', 9, min_lod_clamp=0.0)
    return d


def sampler_descriptor():
    """fd6_sampler_state_create<A8XX> for Metal's (linear, linear, not mipmapped, clamp_to_edge x3, lod 0..FLT_MAX):
    as Stage 6's air_sampler_a8xx (a8xx_sampler.c) builds it: MIPMAPING_DIS, lod clamped to 0..0.25."""
    return [desc('A8XX_TEX_SAMP', 0, mipmaping_dis=True, xy_mag='A6XX_TEX_LINEAR', xy_min='A6XX_TEX_LINEAR',
                 wrap_s='A6XX_TEX_CLAMP_TO_EDGE', wrap_t='A6XX_TEX_CLAMP_TO_EDGE', wrap_r='A6XX_TEX_CLAMP_TO_EDGE',
                 lod_bias=0.0, aniso='A6XX_TEX_ANISO_1'),
            desc('A8XX_TEX_SAMP', 1, max_lod=0.25, min_lod=0.0, reduction_mode='A6XX_REDUCTION_MODE_AVERAGE',
                 compare_func='FUNC_NEVER', chroma_linear=True),
            0, 0]


def load_state6(stage_geom, state_type, src, block, num_unit, ext=None, payload=()):
    """CP_LOAD_STATE6_{GEOM,FRAG} words (adreno_pm4.xml)."""
    w0 = pkt_word('CP_LOAD_STATE6_0', dst_off=0, state_type=state_type, state_src=src, state_block=block,
                  num_unit=num_unit)
    ext = ext or 0
    return (R.CP_LOAD_STATE6_GEOM if stage_geom else R.CP_LOAD_STATE6_FRAG), \
        [w0, ext & 0xFFFFFFFF, ext >> 32] + list(payload)


def ubo_desc(va, size_bytes):
    """A6XX_UBO_DESC (fd6_pack.h): base, size in vec4."""
    return [desc('A6XX_UBO', 0, base_lo=va & 0xFFFFFFFF), desc('A6XX_UBO', 1, base_hi=va >> 32,
                                                             size=(size_bytes + 15) // 16)]


def p7_rt_va(win, write=True):
    return win.addr('p7_rt', P7_RT_GUARD, P7['pitch'] * P7['height'], write, 'P7 render target')


def build_p7_ib(win, sh, sampling, fence):
    """The P7 command buffer, in the order of Mesa's gallium a8xx sysmem batch for one draw (reference capture
    gl-tri-skylight.rd): fd6_emit_sysmem_prep, fd6_emit_sysmem (sysmem clear, render control, LRZ off), the
    draw (fd6_draw_vbo: vertex params, fd6_emit_3d_state non-group registers, the draw-state groups inline in
    group order, CP_DRAW_INDX_OFFSET), fd6_emit_sysmem_fini and the batch fence."""
    cs = CS(win, 'p7 triangle')
    W, H = P7['width'], P7['height']
    rt = p7_rt_va(win)
    emit_restore(cs, win.addr('p67_misc', 0, 0x1000, False, 'border colour table (zeroed)'))
    # fd6_emit_sysmem_prep<A8XX>
    cs.event('LRZ_CACHE_FLUSH')
    cs.marker('RM6_DIRECT_RENDER')
    cs.pkt4(reg('GRAS_SC_WINDOW_SCISSOR_TL', x=0, y=0), reg('GRAS_SC_WINDOW_SCISSOR_BR', x=W - 1, y=H - 1))   # set_scissor
    cs.pkt4(reg('GRAS_A2D_SCISSOR_TL', x=0, y=0), reg('GRAS_A2D_SCISSOR_BR', x=W - 1, y=H - 1))
    cs.pkt4(reg('RB_RESOLVE_CNTL_1', x=0, y=0), reg('RB_RESOLVE_CNTL_2', x=W - 1, y=H - 1))               # set_blit_scissor
    cs.pkt4(reg('GRAS_MODE_CNTL', value=0x2))
    bin_common = dict(render_mode='RENDERING_PASS', lrz_feedback_zmode_mask='LRZ_FEEDBACK_EARLY_Z_LATE_Z')
    cs.crb(reg('RB_WINDOW_OFFSET', x=0, y=0), reg('RB_RESOLVE_WINDOW_OFFSET', x=0, y=0),         # set_window_offset
           reg('SP_WINDOW_OFFSET', x=0, y=0), reg('TPL1_WINDOW_OFFSET', x=0, y=0),
           reg('TPL1_A2D_WINDOW_OFFSET', x=0, y=0),
           reg('GRAS_SC_BIN_CNTL', binw=0, binh=0, force_bi_dir_lrz_disable=True, **bin_common),        # set_bin_size
           reg('RB_CNTL', binw=0, binh=0, **bin_common), reg('VFD_RENDER_MODE', render_mode='RENDERING_PASS'),
           reg('TPL1_BIN_SIZE', binw=0, binh=0), reg('TPL1_A2D_BIN_SIZE', binw=0, binh=0),
           reg('SP_BIN_SIZE', binw=0, binh=0),
           *[reg('RB_MRT_GMEM_DIMENSION_REG', i, width=0, height=0) for i in range(8)],
           reg('RB_DEPTH_GMEM_DIMENSION', width=0, height=0), reg('RB_STENCIL_GMEM_DIMENSION', width=0, height=0),
           reg('RB_RESOLVE_CNTL_3', binw=0, binh=0),
           reg('RB_BUFFER_CNTL', **{f'{b}_sysmem': True for b in ('z', 's', 'rt0', 'rt1', 'rt2', 'rt3', 'rt4', 'rt5',
                                                                  'rt6', 'rt7')}),
           reg('GRAS_LRZ_CB_CNTL'), reg('VPC_SO_OVERRIDE', disable=False))
    cs.marker('RM6_DIRECT_RENDER')
    cs.pkt7(R.CP_SKIP_IB2_ENABLE_GLOBAL, 0)
    cs.pkt7(R.CP_SKIP_IB2_ENABLE_LOCAL, 1)
    cs.pkt7(R.CP_SET_VISIBILITY_OVERRIDE, 1)
    rt_size = P7['pitch'] * H
    cs.crb(reg('RB_DEPTH_BUFFER_INFO', depth_format='DEPTH6_NONE', tilemode='TILE6_LINEAR'),       # emit_zs (none)
           reg('RB_DEPTH_BUFFER_PITCH', value=0), reg('RB_DEPTH_BUFFER_ARRAY_PITCH', value=0),
           reg('RB_DEPTH_BUFFER_BASE', value=0), reg('RB_DEPTH_GMEM_BASE', value=0),
           reg('GRAS_SU_DEPTH_BUFFER_INFO', depth_format='DEPTH6_NONE'),
           reg('RB_STENCIL_BUFFER_INFO', tilemode='TILE6_LINEAR'),
           reg('RB_MRT_BUF_INFO', 0, color_format=FMT_8888, color_tile_mode='TILE6_LINEAR',             # emit_mrt
               color_swap=swap_for('bgra')),
           reg('RB_MRT_PITCH', 0, value=P7['pitch']), reg('RB_MRT_ARRAY_PITCH', 0, value=rt_size),
           reg('RB_MRT_BASE', 0, value=rt), reg('RB_MRT_BASE_GMEM', 0, value=0),
           reg('SP_PS_MRT_REG', 0, color_format=FMT_8888),
           reg('RB_COLOR_FLAG_BUFFER_ADDR', 0, value=rt), reg('RB_COLOR_FLAG_BUFFER_PITCH', 0, pitch=0, array_pitch=0),
           reg('GRAS_LRZ_MRT_BUFFER_INFO_0', color_format=FMT_8888), reg('RB_SRGB_CNTL', dword=0),
           reg('SP_SRGB_CNTL', dword=0), reg('GRAS_CL_ARRAY_SIZE', value=0),
           reg('TPL1_RAS_MSAA_CNTL', samples='MSAA_ONE'),                                                 # emit_msaa
           reg('TPL1_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('GRAS_SC_RAS_MSAA_CNTL', samples='MSAA_ONE'),
           reg('GRAS_SC_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('RB_RAS_MSAA_CNTL', samples='MSAA_ONE'), reg('RB_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('RB_RESOLVE_GMEM_BUFFER_INFO', samples='MSAA_ONE'))
    # fd6_emit_sysmem<A8XX>
    emit_gmem_cache_cntl_sysmem(cs)
    emit_static_binning_regs(cs)
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True, inv_cche=True, wfi=True)
    emit_2d_clear(cs, rt, P7['pitch'], 'bgra', (0, 0, W, H), P7['clear'])                         # emit_sysmem_clears
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True)
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True, inv_cche=True, wfi=True)
    cs.marker('RM6_DIRECT_RENDER')
    cs.crb(reg('RB_RENDER_CNTL', raster_mode='TYPE_TILED', raster_direction='LR_TB'), reg('GRAS_SU_RENDER_CNTL'))
    emit_lrz_off(cs)
    # --- the draw (fd6_draw_vbo) ---
    cs.crb(reg('VFD_INDEX_OFFSET', value=0), reg('VFD_INSTANCE_START_OFFSET', value=0),
           reg('PC_RESTART_INDEX', value=0xFFFFFFFF))
    # Metal's default viewport (0, 0, W, H, znear 0, zfar 1), clip-space y up and z in [0, 1] (BINDING-ABI 4.3):
    # y_window = H/2 - y_ndc * H/2, z_window = z_ndc. Scissor = the whole target.
    xs, xo, ys, yo, zs, zo = W / 2, W / 2, -H / 2, H / 2, 1.0, 0.0
    cs.crb(reg('RB_STENCIL_REF_CNTL', ref=0, bfref=0),                                              # fd6_emit_non_group
           reg('GRAS_CL_VIEWPORT_XOFFSET', 0, value=xo), reg('GRAS_CL_VIEWPORT_XSCALE', 0, value=xs),
           reg('GRAS_CL_VIEWPORT_YOFFSET', 0, value=yo), reg('GRAS_CL_VIEWPORT_YSCALE', 0, value=ys),
           reg('GRAS_CL_VIEWPORT_ZOFFSET', 0, value=zo), reg('GRAS_CL_VIEWPORT_ZSCALE', 0, value=zs),
           reg('GRAS_SC_VIEWPORT_SCISSOR_TL', 0, x=0, y=0), reg('GRAS_SC_VIEWPORT_SCISSOR_BR', 0, x=W - 1, y=H - 1),
           reg('GRAS_CL_GUARDBAND_CLIP_ADJ', horz=guardband(xo, xs), vert=guardband(yo, ys)))
    vs_va = win.addr('p7_code', P7_CODE['vs'], sh['vs']['info']['size'], False, 'VS code')
    fs_va = win.addr('p7_code', P7_CODE['fs'], sh['fs']['info']['size'], False, 'FS code')
    link_regs, link, mrt = program_linkage(sh)
    cs.crb(*program_config(sh, sampling))                                                           # group PROG_CONFIG
    cs.crb(*shader_regs(sh['vs'], 'vs', vs_va, sampling))                                           # group PROG
    cs.crb(*shader_regs(sh['fs'], 'fs', fs_va, sampling))
    cs.crb(*link_regs)
    cs.crb(*interp_regs(sh))                                                                        # group PROG_INTERP
    cs.crb(reg('RB_PS_OUTPUT_CNTL'), reg('RB_PS_MRT_CNTL', mrt=1), reg('SP_PS_MRT_CNTL', mrt=1),     # group PROG_FB_RAST
           _raw('SP_PS_OUTPUT_MASK', 0, mrt & 0xF), _raw('RB_PS_OUTPUT_MASK', 0, mrt & 0xF))
    cs.crb(reg('GRAS_LRZ_CNTL'), reg('RB_LRZ_CNTL'), reg('RB_DEPTH_PLANE_CNTL', z_mode='A6XX_LATE_Z'),  # group LRZ
           reg('GRAS_SU_DEPTH_PLANE_CNTL', z_mode='A6XX_LATE_Z'))
    vbo = win.addr('p7_data', P7_DATA['vbo'], len(p7_vbo()), False, 'vertex buffer')
    fetch = [reg('VFD_FETCH_INSTR_INSTR', i, idx=0, offset=a['offset'], format='FMT6_32_32_FLOAT', swap='WZYX',  # group VTXSTATE
                 unk30=True, float=True) + reg('VFD_FETCH_INSTR_STEP_RATE', i, value=1)
             for i, a in enumerate(P7['attributes'])]
    cs.crb(*fetch, reg('VFD_VERTEX_BUFFER_STRIDE', 0, value=P7['stride']))
    cs.crb(reg('VFD_VERTEX_BUFFER_BASE', 0, value=vbo), reg('VFD_VERTEX_BUFFER_SIZE', 0, value=len(p7_vbo())))  # group VBO
    ubo = win.addr('p7_data', P7_DATA['ubo'], len(p7_ubo()), False, 'UBO 1 (mvp)')                   # group CONST
    vs_ubos = []
    for i in range(sh['vs']['const_state']['num_app_ubos']):
        vs_ubos += ubo_desc(ubo, len(p7_ubo())) if i == P7['mvp_buffer_index'] else [0, 0]
    op, words = load_state6(True, 'ST6_UBO', 'SS6_DIRECT', 'SB6_VS_SHADER', len(vs_ubos) // 2, payload=vs_ubos)
    cs.pkt7(op, *words)
    op, words = load_state6(False, 'ST6_UBO', 'SS6_DIRECT', 'SB6_FS_SHADER',
                            sh['fs']['const_state']['num_app_ubos'], payload=[0, 0] * sh['fs']['const_state']['num_app_ubos'])
    cs.pkt7(op, *words)
    # CP_LOAD_STATE6 reads NUM_UNIT descriptors from each base: 16 bytes per S#, 64 per T# -- the referenced
    # ranges are exactly what the CP fetches, and must not run into each other.
    if P7_DATA['tex_desc'] + 64 * sampling['ntex'] > P7_DATA['samp_desc']:
        raise Refused(f'{sampling["ntex"]} T# descriptors overrun the S# slot in p7_data')
    samp = win.addr('p7_data', P7_DATA['samp_desc'], 16 * sampling['nsamp'], False, 'S#')      # group FS_TEX
    texd = win.addr('p7_data', P7_DATA['tex_desc'], 64 * sampling['ntex'], False, 'T#')
    cs.crb(reg('SP_PS_SAMPLER_BASE', value=samp), reg('SP_PS_TEXMEMOBJ_BASE', value=texd),
           reg('SP_PS_TSIZE', value=sampling['ntex']))
    op, words = load_state6(False, 'ST6_SHADER', 'SS6_INDIRECT', 'SB6_FS_TEX', sampling['nsamp'], ext=samp)
    cs.pkt7(op, *words)
    op, words = load_state6(False, 'ST6_CONSTANTS', 'SS6_INDIRECT', 'SB6_FS_TEX', sampling['ntex'], ext=texd)
    cs.pkt7(op, *words)
    zclamp = []
    for i in range(16):                                                                             # group RASTERIZER
        zclamp += [reg('GRAS_CL_VIEWPORT_ZCLAMP_MIN', i, value=0.0), reg('GRAS_CL_VIEWPORT_ZCLAMP_MAX', i, value=1.0),
                   reg('RB_VIEWPORT_ZCLAMP_MIN_REG', i, value=0.0), reg('RB_VIEWPORT_ZCLAMP_MAX_REG', i, value=1.0)]
    cs.crb(reg('GRAS_CL_CNTL', z_clamp_enable=True, zero_gb_scale_z=True, vp_clip_code_ignore=True),
           reg('GRAS_SU_CNTL', front_cw=True, linehalfwidth=0.5, line_mode='BRESENHAM'),
           reg('GRAS_SU_POINT_MINMAX', min=1.0, max=1.0), reg('GRAS_SU_POINT_SIZE', value=1.0),
           reg('GRAS_SU_POLY_OFFSET_SCALE', value=0.0), reg('GRAS_SU_POLY_OFFSET_OFFSET', value=0.0),
           reg('GRAS_SU_POLY_OFFSET_OFFSET_CLAMP', value=0.0),
           reg('PC_CNTL', primitive_restart=False, provoking_vtx_last=False),
           reg('VPC_PC_CNTL', primitive_restart=False, provoking_vtx_last=False),
           reg('VPC_RAST_CNTL', mode='POLYMODE6_TRIANGLES'), reg('PC_DGEN_RAST_CNTL', mode='POLYMODE6_TRIANGLES'),
           reg('GRAS_RAST_CNTL', mode='POLYMODE6_TRIANGLES'), reg('VPC_PS_RAST_CNTL', mode='POLYMODE6_TRIANGLES'),
           *zclamp)
    cs.crb(reg('RB_ALPHA_TEST_CNTL', alpha_test_func='FUNC_NEVER'), reg('SP_ALPHA_TEST_CNTL'),       # group ZSA
           reg('RB_STENCIL_CNTL'), reg('GRAS_SU_STENCIL_CNTL'), reg('RB_STENCIL_MASK'), reg('RB_STENCIL_WRITE_MASK'),
           reg('RB_DEPTH_CNTL', zfunc='FUNC_NEVER', z_clamp_enable=True, o_depth_01_clamp_en=True),
           reg('GRAS_SU_DEPTH_CNTL'), reg('RB_DEPTH_BOUND_MIN', value=0.0), reg('RB_DEPTH_BOUND_MAX', value=1.0))
    cs.crb(reg('RB_MRT_BLEND_CONTROL', 0, rgb_src_factor='FACTOR_ZERO', rgb_blend_opcode='BLEND_DST_PLUS_SRC',  # group BLEND
               rgb_dest_factor='FACTOR_ZERO', alpha_src_factor='FACTOR_ZERO', alpha_blend_opcode='BLEND_DST_PLUS_SRC',
               alpha_dest_factor='FACTOR_ZERO'),
           reg('RB_MRT_CONTROL', 0, rop_code='ROP_COPY', component_enable=0xF),
           reg('SP_MRT_BLEND_CNTL_REG', 0, component_write_mask=0xF), reg('RB_DITHER_CNTL'), reg('SP_BLEND_CNTL'),
           reg('RB_BLEND_CNTL', sample_mask=0xFFFF))
    cs.crb(reg('GRAS_SC_SCREEN_SCISSOR_TL', 0, x=0, y=0), reg('GRAS_SC_SCREEN_SCISSOR_BR', 0, x=W - 1, y=H - 1))  # group SCISSOR
    cs.crb(reg('RB_BLEND_CONSTANT_RED_FP32', value=0.0), reg('RB_BLEND_CONSTANT_GREEN_FP32', value=0.0),  # group BLEND_COLOR
           reg('RB_BLEND_CONSTANT_BLUE_FP32', value=0.0), reg('RB_BLEND_CONSTANT_ALPHA_FP32', value=0.0))
    cs.crb(reg('GRAS_SC_MSAA_SAMPLE_POS_CNTL'), reg('RB_MSAA_SAMPLE_POS_CNTL'))                     # group SAMPLE_LOCATIONS
    # groups BINDLESS 0..4 (fd6_build_bindless_state): gallium always binds its five descriptor sets. These
    # shaders use none (variant_checks); the sets point at the zeroed page so the registers hold Mesa's shape.
    empty_set = win.addr('p67_misc', 0, 0x1000, False, 'bindless descriptor sets (empty)')
    for i in range(5):
        cs.crb(reg('SP_UPDATE_CNTL'), reg('SP_GFX_BINDLESS_BASE_DESCRIPTOR', i, desc_size='BINDLESS_DESCRIPTOR_64B',
                                          addr=empty_set))
    cs.crb(reg('GRAS_SC_CNTL', single_prim_mode='NO_FLUSH'))                                        # group PRIM_MODE_SYSMEM
    cs.pkt7(R.CP_DRAW_INDX_OFFSET,
            pkt_word('CP_DRAW_INDX_OFFSET_0', prim_type='DI_PT_TRILIST', source_select='DI_SRC_SEL_AUTO_INDEX',
                     vis_cull='USE_VISIBILITY', index_size='INDEX4_SIZE_8_BIT', patch_type='TESS_QUADS'),
            pkt_word('CP_DRAW_INDX_OFFSET_1', num_instances=1), pkt_word('CP_DRAW_INDX_OFFSET_2',
                                                                           num_indices=P7['vertex_count']))
    # fd6_emit_sysmem_fini<A8XX> + emit_common_fini (no query: the ZPASS_DONE sample count is not taken)
    emit_flushes(cs, ccu_color=True, inv_ccu_color=True, ccu_depth=True, inv_ccu_depth=True, cache=True,
                 inv_cche=True, wfi=True)
    cs.event('CACHE_CLEAN')
    cs.pkt7(R.CP_SKIP_IB2_ENABLE_GLOBAL, 0)
    cs.event('LRZ_CACHE_FLUSH')
    emit_ib_fence(cs, win.addr('memptrs', 0, 0x100, True, 'batch fence (memptrs)'), fence)
    cs.fence = fence
    return cs, link


# ============================================================================================================
# P6 command buffers
# ============================================================================================================
P6 = {'size': 256, 'pitch': 1024, 'guard': 0x4000,
      'clear_rect': (16, 8, 208, 224), 'clear': (1.0, 0.25, 0.0, 1.0),          # RGBA; packed BGRA in memory
      'fb_rect_size': (256, 256), 'fb_clear': (0.0, 0.5, 1.0, 1.0)}


def p6_source():
    """256x256 BGRA8 source image: every byte a function of (x, y), no two rows alike."""
    out = bytearray()
    for y in range(256):
        for x in range(256):
            out += bytes(((x + 2 * y) & 0xFF, (3 * x + y) & 0xFF, (x ^ y) & 0xFF, 0xFF))
    return bytes(out)


def p6_dst_va(win, x=0, y=0):
    return win.addr('p6_dst', P6['guard'] + y * P6['pitch'] + 4 * x, P6['pitch'] * P6['size'] - (y * P6['pitch'] + 4 * x),
                    True, 'P6 target')


def build_p6_clear_ib(win, fence, target='arena', fb=None, rect=None):
    cs = CS(win, 'p6 clear')

    def body(cs):
        emit_flushes(cs, ccu_color=True, inv_ccu_color=True, ccu_depth=True, inv_ccu_depth=True)   # emit_setup
        if target == 'arena':
            va = win.addr('p6_dst', P6['guard'], P6['pitch'] * P6['size'], True, 'P6 target')
            emit_2d_clear(cs, va, P6['pitch'], 'bgra', P6['clear_rect'], P6['clear'])
        else:
            span, (x, y, w, h) = win.fb_span, rect
            va = win.addr('framebuffer', span['rect_offset'], fb['pitch'] * h, True, 'framebuffer rows')
            emit_2d_clear(cs, va, fb['pitch'], fb['layout'], (x, 0, w, h), P6['fb_clear'])
        emit_flushes(cs, ccu_color=True, ccu_depth=True, cache=True, wfi=True)
    emit_nondraw_batch(cs, win, body)
    emit_ib_fence(cs, win.addr('memptrs', 0, 0x100, True, 'batch fence (memptrs)'), fence)
    cs.fence = fence
    return cs


def build_p6_copy_ib(win, fence, target='arena', fb=None, rect=None):
    cs = CS(win, 'p6 copy')
    src = win.addr('p6_src', 0, P6['pitch'] * P6['size'], False, 'P6 source image')

    def body(cs):
        if target == 'arena':
            dst = win.addr('p6_dst', P6['guard'], P6['pitch'] * P6['size'], True, 'P6 target')
            emit_2d_copy(cs, src, P6['pitch'], (P6['size'], P6['size']), (0, 0, P6['size'], P6['size']), dst,
                         P6['pitch'], (0, 0), 'bgra')
        else:
            span, (x, y, w, h) = win.fb_span, rect
            dst = win.addr('framebuffer', span['rect_offset'], fb['pitch'] * h, True, 'framebuffer rows')
            emit_2d_copy(cs, src, P6['pitch'], (P6['size'], P6['size']), (0, 0, w, h), dst, fb['pitch'], (x, 0),
                         fb['layout'])
    emit_nondraw_batch(cs, win, body)
    emit_ib_fence(cs, win.addr('memptrs', 0, 0x100, True, 'batch fence (memptrs)'), fence)
    cs.fence = fence
    return cs


# ============================================================================================================
# Submission: a8xx's ring side (Linux a7xx_submit), the fence, bounded waits, fault reports
# ============================================================================================================
IB_SLOTS = {'p6-clear': 0, 'p6-copy': 1, 'fb-clear': 2, 'fb-copy': 3, 'p7-triangle': 4}
IB_SLOT_SIZE = 0x2000
CANARY = bytes((0xA5, 0x5A, 0xC3, 0x3C))
# RBBM_INT_0_STATUS bits that mean an error (a6xx.xml A6XX_RBBM_INT_0_MASK; the error half of Linux's
# A8XX_INT_MASK): CP_AHB_ERROR 1, RBBM_ATB_ASYNCFIFO_OVERFLOW 6, RBBM_GPC_ERROR 7, CP_HW_ERROR 9,
# RBBM_ATB_BUS_OVERFLOW 22, RBBM_HANG_DETECT 23, UCHE_OOB_ACCESS 24, UCHE_TRAP_INTR 25, TSBWRITEERROR 28,
# SWFUSEVIOLATION 29. Not errors: CP_CACHE_FLUSH_TS 20 and the other timestamp/IB bits every batch sets.
# PROVEN on the A16 (stage3-run2-probe.json): a CP fetch fault reads RBBM_INT_0_STATUS 0x200 (CP_HW_ERROR)
# and CP_INTERRUPT_STATUS_GLOBAL 0x10000 (SWFAULTBR, bit 16: outside the old 0x7F7F mask).
RBBM_INT_ERRORS = ((1 << 1) | (1 << 6) | (1 << 7) | (1 << 9) | (1 << 22) | (1 << 23) | (1 << 24) | (1 << 25) |
                   (1 << 28) | (1 << 29))
CP_GLOBAL_FAULTS = R.A8XX_CP_GLOBAL_INT_MASK          # HWFAULT{BR..DDEBV} bits 0-6 | SWFAULT{BR..DDEBV} bits 16-22


def interrupt_faults(s):
    """(RBBM_INT_0_STATUS, CP_INTERRUPT_STATUS_GLOBAL, the error bits of each). Nothing clears these (no IRQ
    handler runs), so an error bit stays set until quiesce resets the GPU."""
    int0, irq = s.rd('RBBM_INT_0_STATUS'), s.rd('CP_INTERRUPT_STATUS_GLOBAL')
    return int0, irq, int0 & RBBM_INT_ERRORS, irq & CP_GLOBAL_FAULTS


def ring_words(ib_va, ib_dwords, fence_va, bv_fence_va, value):
    """a7xx_submit (a6xx_gpu.c; the a8xx path), without what one context and no preemption need: no pagetable
    switch (one CB0 table), no stats counters, no IFPC markers (IFPC is not negotiated in HFI), no scratch
    register, no IRQ bit (nothing takes the interrupt; the tool polls). The BV thread commits its fence first,
    BR waits for it (CP_WAIT_TIMESTAMP), then BR commits the ring fence: both with CACHE_CLEAN, so every
    render-target byte is in DRAM before the fence value is."""
    tc = lambda **kw: pkt_word('CP_THREAD_CONTROL_0', **kw)
    clean = pkt_word('CP_EVENT_WRITE7_0', event='CACHE_CLEAN', write_enabled=True)
    return [R.pkt7(R.CP_THREAD_CONTROL, 1), tc(sync_threads=True, thread='CP_SET_THREAD_BR'),
            R.pkt7(R.CP_THREAD_CONTROL, 1), tc(thread='CP_SET_THREAD_BOTH'),
            R.pkt7(R.CP_INDIRECT_BUFFER, 3), ib_va & 0xFFFFFFFF, ib_va >> 32,
            pkt_word('A5XX_CP_INDIRECT_BUFFER_2', ib_size=ib_dwords),
            R.pkt7(R.CP_THREAD_CONTROL, 1), tc(thread='CP_SET_THREAD_BR'),
            R.pkt7(R.CP_EVENT_WRITE7, 1), pkt_word('CP_EVENT_WRITE7_0', event='CCU_INVALIDATE_DEPTH'),
            R.pkt7(R.CP_EVENT_WRITE7, 1), pkt_word('CP_EVENT_WRITE7_0', event='CCU_INVALIDATE_COLOR'),
            R.pkt7(R.CP_THREAD_CONTROL, 1), tc(thread='CP_SET_THREAD_BV'),
            R.pkt7(R.CP_EVENT_WRITE7, 4), clean, bv_fence_va & 0xFFFFFFFF, bv_fence_va >> 32, value,
            R.pkt7(R.CP_THREAD_CONTROL, 1), tc(thread='CP_SET_THREAD_BR'),
            R.pkt7(R.CP_WAIT_TIMESTAMP, 4), 0, bv_fence_va & 0xFFFFFFFF, bv_fence_va >> 32, value,
            R.pkt7(R.CP_EVENT_WRITE7, 4), clean, fence_va & 0xFFFFFFFF, fence_va >> 32, value,
            R.pkt7(R.CP_THREAD_CONTROL, 1), tc(thread='CP_SET_THREAD_BOTH')]


def verify_refs(s, win, tables_blob, tables_offset):
    """Every address the command stream carries, walked through CB0's tables as the SMMU will read them
    (the image just read back from DRAM): both ends of each range must land where the window says,
    inside the arena or inside the framebuffer span, with write permission where the GPU writes."""
    base = s.arena[0]
    table_base = base + tables_offset
    read_u64 = lambda pa: struct.unpack_from('<Q', tables_blob, pa - table_base)[0] \
        if table_base <= pa < table_base + len(tables_blob) else 0
    ttbr = S3.ttbr_for(build_cb0_tables(base, win.fb_span), R.GPU_CB)
    arena = (base, base + s.arena[1])
    fb = (win.fb_span['pa'], win.fb_span['pa'] + win.fb_span['size']) if win.fb_span else None
    bad = []
    for ref in win.refs:
        for off in {0, ref['size'] - 1}:
            hit = S3.walk(read_u64, ttbr, ref['va'] + off)
            pa = hit[0] if hit else None
            ok = pa == ref['pa'] + off and (arena[0] <= pa < arena[1] or (fb and fb[0] <= pa < fb[1]))
            if ok and ref['write'] and hit[1] & R.PTE_AP_RDONLY:
                ok = False
            if not ok:
                bad.append((ref['what'], hx(ref['va'] + off), hx(pa) if pa is not None else 'fault'))
    if bad:
        raise Stop(s.step, f'CB0 does not translate the command stream\'s addresses as built: {bad[:6]}')
    return len(win.refs)


def install_tables(s, out, win, label):
    """Write CB0's tables for this window (cp-fence's pages keep their contents, only new entries appear; with
    no framebuffer span the framebuffer entries disappear), invalidate CB0's TLB, read the image back and
    walk every reference. Only called with the CP idle and its pointers agreeing (p67_ready)."""
    base = s.arena[0]
    tables = build_cb0_tables(base, win.fb_span)
    offset, blob = tables.image()
    half = R.ARENA_LAYOUT['tables'][1] // 2
    if len(blob) > half:
        raise Refused('CB0 tables outgrow their half of the table pool')
    current = s.mem_read(offset, half, f'CB0 tables ({label}, compare)')
    want = blob + bytes(half - len(blob))                # pages past the image are zero: nothing stale stays
    changed = current != want
    if changed:
        # Only the pages that differ are written: cp-fence's SQE/ring/memptrs entries never change value.
        # Highest page first: the pool allocates a table before the tables it points to, so this writes a new
        # L3 page before the L2 entry that makes it reachable (map) and clears the L3 entries before the L2
        # entry goes (unmap). Either way no walk can reach a half-written table -- which matters on the D4
        # cleanup path, where the GPU may still be busy after a missing fence.
        for page in range(half - 4096, -1, -4096):
            if current[page:page + 4096] != want[page:page + 4096]:
                s.mem_write(offset + page, want[page:page + 4096], f'CB0 table page +{page:#x} ({label})')
        S3.smmu_tlbi(s, R.GPU_CB)
    image = s.mem_read(offset, len(blob), f'CB0 tables ({label}, verify)')
    if image != blob:
        raise Stop(s.step, f'CB0 tables did not read back ({label})')
    out.setdefault('cb0_tables', []).append({'label': label, 'changed': changed, 'bytes': len(blob)})
    return image, offset


def fb_mapped_now(s):
    """Whether any CB0 entry points into the framebuffer window, from the tables in DRAM (independent of the
    host record): the root and L2 entries covering FB_VA."""
    base = s.arena[0]
    offset, blob = build_cb0_tables(base).image()
    half = R.ARENA_LAYOUT['tables'][1] // 2
    image = s.mem_read(offset, half, 'CB0 tables (framebuffer window check)')
    table_base = base + offset
    read_u64 = lambda pa: struct.unpack_from('<Q', image, pa - table_base)[0] \
        if table_base <= pa < table_base + len(image) else 0
    ttbr = S3.ttbr_for(build_cb0_tables(base), R.GPU_CB)
    for va in range(R.FB_VA, R.FB_VA + R.FB_VA_LIMIT, 1 << 21):      # one probe per 2 MiB L3 table ...
        l1 = read_u64((ttbr & R.PTE_ADDR_MASK) + 8 * (va >> 30 & 3))
        if l1 & 3 != R.PTE_TYPE_TABLE:
            return False
        l2 = read_u64((l1 & R.PTE_ADDR_MASK) + 8 * (va >> 21 & 0x1FF))
        if l2 & 3 == R.PTE_TYPE_TABLE:
            return True                                               # ... any table under FB_VA counts
    return False


def p67_ready(s, out, host, wide=False):
    """The CP is the one cp-fence started for this GMU boot and it is idle: tokens, CP_SQE_CNTL, the ring
    pointers against the record, RBBM idle, CB0 as cp-fence built it, the stream table, no fault recorded."""
    if not host['boot_token'] or host['cp_token'] != host['boot_token'] or host['hwinit_token'] != host['boot_token']:
        raise Stop(s.step, 'the CP was not initialised by cp-fence for this GMU boot (CP_ME_INIT, SECVID): run '
                           '`cp-fence --write` first')
    if host.get('sqe_probe') and host['sqe_probe'] == host['boot_token']:
        raise Stop(s.step, 'this GMU boot\'s SQE was started by `--sqe-probe fault`: quiesce and start over')
    if not s.rd('CP_SQE_CNTL') & 1:
        raise Stop(s.step, 'CP_SQE_CNTL reads 0: the SQE this record describes is not running (quiesce first)')
    wptr, hw_wptr, hw_rptr = host['rb_wptr'], s.rd('CP_RB_WPTR'), s.rd('CP_RB_RPTR')
    if not wptr or hw_wptr != wptr or hw_rptr != wptr:
        raise Stop(s.step, f'ring pointers disagree: record wptr {wptr}, CP_RB_WPTR {hw_wptr}, CP_RB_RPTR {hw_rptr}: '
                           'this is not the idle CP cp-fence left (`quiesce --write` and start over)')
    status = s.rd('RBBM_STATUS')
    if status & ~R.RBBM_STATUS_CX_MASTER:
        raise Stop(s.step, f'RBBM_STATUS {status:#x}: the GPU is busy before anything was submitted')
    base = s.arena[0]
    if S3.cb_diff(S3.smmu_read_cb(s, R.GPU_CB), S3.smmu_cb_expected(S3.build_tables(base, 'gpu'), R.GPU_CB, wide)):
        raise Stop(s.step, 'context bank 0 is not the one cp-fence programmed for this arena' +
                   (' with --va-wide' if wide else ''))
    _scr0, groups = S3.smmu_globals(s, out)
    S3.smmu_verify(s, out, groups, {R.GPU_SID: R.GPU_CB, R.GPU_SID_LPAC: R.GPU_CB, R.GMU_SID: R.GMU_CB})
    fault, report = S3.cb_faults(s, R.GPU_CB)
    gfsr = s.rd('SMMU_sGFSR')
    if fault or gfsr:
        raise Stop(s.step, f'an SMMU fault is already recorded (CB0 {report}, sGFSR {gfsr:#x}): quiesce first',
                   cb0=report)
    int0, irq, int0_bad, irq_bad = interrupt_faults(s)
    if int0_bad or irq_bad:
        # A sticky error from an earlier submission (nothing clears these): a later check could not tell a new
        # fault from it, and the CP that reported it is not given more work.
        raise Stop(s.step, f'an error interrupt is already recorded (RBBM_INT_0_STATUS {int0:#x}, '
                           f'CP_INTERRUPT_STATUS_GLOBAL {irq:#x}): quiesce first')
    out['cp'] = {'rb_wptr': wptr, 'rbbm_status': hx(status), 'sqe_cntl': 1, 'rbbm_int_0_status': hx(int0),
                 'cp_interrupt_status_global': hx(irq)}
    return wptr


def p67_submit(s, out, host, win, cs, slot, what):
    """IB into its arena slot (read back), every reference re-walked through CB0, the a7xx_submit ring words,
    the fenced CP_RB_WPTR write, the fence waited for (bounded), the idle check, the fault registers."""
    words = cs.blob()
    if len(words) > IB_SLOT_SIZE:
        raise Refused(f'{what}: {len(words)} bytes of commands do not fit an IB slot')
    ib_off = IB_SLOTS[slot] * IB_SLOT_SIZE
    ib_va = win.addr('p67_ib', ib_off, len(words), False, f'IB {slot}')
    fence_va = win.addr('memptrs', R.CP_FENCE_OFF, 4, True, 'ring fence')
    bv_va = win.addr('memptrs', R.CP_BV_FENCE_OFF, 4, True, 'BV fence')
    image, table_off = install_tables(s, out, win, what)
    refs = verify_refs(s, win, image, table_off)
    s.mem_write(R.ARENA_LAYOUT['p67_ib'][0] + ib_off, words, f'IB {slot}')
    if s.mem_read(R.ARENA_LAYOUT['p67_ib'][0] + ib_off, len(words), f'IB {slot} (verify)') != words:
        raise Stop(s.step, f'IB {slot} did not read back')
    value = cs.fence
    ring = ring_words(ib_va, cs.dwords(), fence_va, bv_va, value)
    wptr = S3.ring_submit(s, host, ring, f'{what} (IB {cs.dwords()} dwords, fence {value:#x})')
    S3.write_host(s, host)
    record = {'what': what, 'ib': {'va': hx(ib_va), 'dwords': cs.dwords(),
                                   'sha256': hashlib.sha256(words).hexdigest()},
              'refs_walked': refs, 'fence': hx(value), 'wptr': wptr}
    out.setdefault('submissions', []).append(record)
    memptrs = R.ARENA_LAYOUT['cp_memptrs'][0]
    limit, seconds = S3.POLL['fence']
    deadline, seen, attempt = s.clock() + seconds, None, 0
    for attempt in range(1, limit + 1):
        seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_FENCE_OFF, 4, 'fence'))[0]
        if seen == value:
            break
        if attempt >= 2 and s.clock() > deadline:
            break
    s.polls.append({'reg': f'fence {what} (DRAM)', 'reads': attempt, 'ok': seen == value})
    record['fence_read'] = hx(seen)
    record['cb0'] = S3.cb_faults(s, R.GPU_CB)[1]
    if seen != value:
        record['diagnostics'] = submission_diagnostics(s)
        raise Stop(s.step, f'{what}: the GPU did not write the fence (read {seen:#x}, want {value:#x}): '
                           '`quiesce --write`', submission=record)
    bv_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_BV_FENCE_OFF, 4, 'BV fence'))[0]
    ib_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_IB_FENCE_OFF, 4, 'IB fence'))[0]
    record['bv_fence'], record['ib_fence'] = hx(bv_seen), hx(ib_seen)
    record['idle'] = S3.cp_idle(s, wptr)
    host['fence_value'] = value
    S3.write_host(s, host)
    fault, report = S3.cb_faults(s, R.GPU_CB)
    int0, irq, int0_bad, irq_bad = interrupt_faults(s)
    record['cp_interrupt_status_global'] = hx(irq)
    record['rbbm_int_0_status'] = hx(int0)
    if fault or irq_bad or int0_bad:
        record['diagnostics'] = submission_diagnostics(s)
        raise Stop(s.step, f'{what}: fence written but CB0 FSR {report["fsr"]} / CP_INTERRUPT_STATUS_GLOBAL {irq:#x} '
                           f'/ RBBM_INT_0_STATUS {int0:#x} report a fault', submission=record)
    if bv_seen != value or ib_seen != value:
        # The ring fence alone does not prove the IB ran to its end: its own CACHE_FLUSH_TS (the last packet
        # of every IB) and the BV thread's fence must carry the same value.
        record['diagnostics'] = submission_diagnostics(s)
        raise Stop(s.step, f'{what}: ring fence {value:#x} written but the IB fence reads {ib_seen:#x} and the BV '
                           f'fence {bv_seen:#x}: the IB did not run to its end', submission=record)
    return record


def submission_diagnostics(s):
    """Read-only: what the CP, RBBM and CB0 say after a fence that did not come."""
    out = {}
    for name in ('CP_RB_RPTR', 'CP_RB_WPTR', 'RBBM_STATUS', 'RBBM_INT_0_STATUS', 'CP_INTERRUPT_STATUS_GLOBAL',
                 'CP_SQE_CNTL', 'SMMU_sGFSR'):
        try:
            out[name] = hx(s.rd(name))
        except Stop as problem:
            out[name] = f'failed: {problem.reason}'
            break
    try:
        out['cb0'] = S3.cb_report(s, R.GPU_CB)
    except Stop as problem:
        out['cb0'] = f'failed: {problem.reason}'
    return out


def next_fence(host):
    host['fence_seq'] += 1
    return 0xF3A00000 | (host['fence_seq'] & 0xFFFFF)


# ============================================================================================================
# Framebuffer (read-only discovery; the opt-in step maps only the rectangle's rows)
# ============================================================================================================
GOP_LAYOUTS = {0: 'rgba', 1: 'bgra'}            # EFI_GRAPHICS_PIXEL_FORMAT: RedGreenBlueReserved / BlueGreenRedReserved
FB_EFI_TYPES = ('Reserved',)                    # the only EFI type D4 accepts without --fb-any-type
FB_NEVER_TYPES = ('MMIO', 'MMIOPort')           # device space: refused even with --fb-any-type (run 1's bus hang)


def fb_discover(s, out, args=None):
    """bootinfo's GOP framebuffer (q1n1 main.c copies GOP mode info into it), checked against the EFI map, the
    arena and q1n1's own regions. Nothing is written."""
    info = getattr(s, '_bootinfo_cache', None) or s.p.bootinfo()
    fb = {k: info.get(f'fb_{k}', 0) for k in ('base', 'size', 'width', 'height', 'stride', 'format')}
    report = {k: hx(v) if k in ('base', 'size') else v for k, v in fb.items()}
    out['framebuffer'] = report
    problems = []
    if not fb['base'] or not fb['width'] or not fb['height']:
        problems.append('bootinfo publishes no framebuffer')
    layout = GOP_LAYOUTS.get(fb['format'])
    if layout is None:
        problems.append(f'GOP pixel format {fb["format"]} is not 8-bit RGBX/BGRX')
    pitch = fb['stride'] * 4
    fb.update(pitch=pitch, layout=layout)
    if fb['stride'] < fb['width'] or fb['size'] < pitch * fb['height']:
        problems.append(f'stride {fb["stride"]} / size {fb["size"]:#x} do not hold {fb["width"]}x{fb["height"]}')
    if fb['base'] % 4096 or pitch % 64:
        problems.append('the framebuffer base is not page aligned or its pitch not a multiple of 64 bytes (the 2D '
                        'engine takes 64-byte aligned row starts)')
    base, size = s.arena
    if fb['base'] < base + size and base < fb['base'] + fb['size']:
        problems.append('the framebuffer overlaps the GPU arena')
    owned = [(k, info.get(f'{k}_base', 0), info.get(f'{k}_size', 0)) for k in ('image', 'heap', 'dma', 'stage')]
    owned += [('memory_map', info.get('memory_map', 0), info.get('memory_map_size', 0)),
              ('preload', info.get('preload_table', 0), info.get('preload_size', 0))]
    clash = [k for k, b, n in owned if b and n and b < fb['base'] + fb['size'] and fb['base'] < b + n]
    if clash:
        problems.append(f'the framebuffer overlaps q1n1\'s own {clash}')
    # The EFI map must say what the scan-out buffer is, all of it: an allow-list, not a deny-list. A span the
    # map does not describe (or describes only in part) is no evidence the GPU may write it -- the same rule
    # Stage 1 applies to cmd-db (REVIEW-2) -- and Runtime*/ACPI*/PalCode/Persistent/MMIO are memory other
    # agents own or device space. Anything but Reserved needs --fb-any-type, after reading D0's report; MMIO
    # (device space) is refused even then.
    regions = [r for r in (out.get('_efi_regions') or [])
               if r['base'] < fb['base'] + fb['size'] and fb['base'] < r['base'] + r['size']]
    kinds = sorted({str(r['type']) for r in regions})
    at = fb['base']
    for r in sorted(regions, key=lambda r: r['base']):
        if r['base'] > at:
            break
        at = max(at, r['base'] + r['size'])
    covered = at >= fb['base'] + fb['size']
    report['efi_types'] = kinds or ['not in the EFI map']
    report['efi_covered'] = covered
    any_type = getattr(args, 'fb_any_type', False)
    free = [k for k in kinds if k in S1.FREE_AFTER_EBS]
    other = [k for k in kinds if k not in FB_EFI_TYPES and k not in free and k not in FB_NEVER_TYPES]
    device = [k for k in kinds if k in FB_NEVER_TYPES]
    if device:
        # Not overridable: a GPU write into device space is the run-1 failure mode (a bus hang), and the arena
        # rules never let the GPU at MMIO either. A DRAM scan-out buffer the firmware mislabels needs a code change.
        problems.append(f'the EFI map lists framebuffer memory as {device}: device space, never written by the GPU '
                        '(--fb-any-type does not override this)')
    if free and not any_type:
        problems.append(f'the EFI map lists framebuffer memory as {free}: memory a guest could be given, not a '
                        'reserved scan-out buffer (--fb-any-type to go on)')
    if other and not any_type:
        problems.append(f'the EFI map lists framebuffer memory as {other}, not {list(FB_EFI_TYPES)} '
                        '(--fb-any-type to go on)')
    if not covered and not any_type:
        problems.append(f'the EFI map does not describe the whole framebuffer ({kinds or "none of it"}): no evidence '
                        'it is a scan-out buffer the GPU may write (--fb-any-type to go on)')
    report['layout'], report['pitch'] = layout, pitch
    return fb, problems


def fb_rect(fb, args):
    if getattr(args, 'fb_rect', None):
        x, y, w, h = (int(v, 0) for v in args.fb_rect.split(','))
    else:
        w, h = P6['fb_rect_size']
        x, y = max(0, (fb['width'] - w - 64) & ~15), 64
    if w <= 0 or h <= 0 or w > P6['size'] or h > P6['size'] or x < 0 or y < 0 or x + w > fb['width'] \
            or y + h > fb['height']:
        raise Stop('D0', f'framebuffer rectangle {x},{y},{w},{h} is not inside {fb["width"]}x{fb["height"]} or '
                         f'larger than the {P6["size"]}x{P6["size"]} source')
    return x, y, w, h


def fb_read(s, fb, span, offset, length, what):
    """Framebuffer bytes, only inside the mapped span, cleaned out of the CPU caches first (the display and
    the GPU read and write DRAM; q1n1's console, if it ever drew, did so through the CPU)."""
    if offset < 0 or offset + length > span['size']:
        raise Refused(f'framebuffer read {offset:#x}+{length:#x} outside the mapped span')
    address = span['pa'] + offset
    s.clean(address, length)
    data = s._dram('readmem', address, lambda: s.p.readmem(address, length))
    s.log.append({'seq': len(s.log), 'step': s.step, 'op': 'readmem', 'addr': hx(address), 'size': length,
                  'what': what, 'domain': 'FB'})
    return bytes(data)


# ============================================================================================================
# Readback checks
# ============================================================================================================
def check_region(got, want_fn, width, height, pitch, what, limit=8):
    """Compare a pitched image with want_fn(x, y) -> 4 bytes; return (bad count, first few)."""
    bad, first = 0, []
    for y in range(height):
        row = got[y * pitch:y * pitch + 4 * width]
        for x in range(width):
            want = want_fn(x, y)
            if row[4 * x:4 * x + 4] != want:
                bad += 1
                if len(first) < limit:
                    first.append({'x': x, 'y': y, 'got': row[4 * x:4 * x + 4].hex(), 'want': want.hex()})
    return bad, first


def bgra(rgba):
    return bytes((unorm8(rgba[2]), unorm8(rgba[1]), unorm8(rgba[0]), unorm8(rgba[3])))


def edge_pixels(width, height, verts, stride=4):
    """Pixels whose centre lies within one pixel of a triangle edge (window coordinates = the pixel-space
    vertices, as the P7 matrix and viewport map them)."""
    pts = [(verts[i], verts[i + 1]) for i in range(0, len(verts), stride)]
    edges = [(pts[i], pts[(i + 1) % 3]) for i in range(3)]
    out = set()
    for y in range(height):
        for x in range(width):
            cx, cy = x + 0.5, y + 0.5
            for (ax, ay), (bx, by) in edges:
                dx, dy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)))
                if math.hypot(ax + t * dx - cx, ay + t * dy - cy) < 1.0:
                    out.add((x, y))
                    break
    return out


def compare_golden(got, golden, width, height, tol, edge_budget, verts):
    """Per-channel |got - golden| <= tol everywhere, except at most edge_budget pixels that lie within one
    pixel of a triangle edge (rasterisers may round a coverage decision differently there)."""
    edges = edge_pixels(width, height, verts)
    over, over_edge, worst = [], 0, 0
    total = 0
    for i in range(width * height):
        d = max(abs(got[4 * i + c] - golden[4 * i + c]) for c in range(4))
        worst = max(worst, d)
        total += d
        if d > tol:
            x, y = i % width, i // width
            if (x, y) in edges:
                over_edge += 1
            else:
                over.append({'x': x, 'y': y, 'got': got[4 * i:4 * i + 4].hex(), 'golden': golden[4 * i:4 * i + 4].hex()})
    ok = not over and over_edge <= edge_budget
    return {'ok': ok, 'max_abs_diff': worst, 'mean_abs_diff': round(total / (width * height), 4),
            'pixels_over_tolerance_inside': len(over), 'pixels_over_tolerance_on_edges': over_edge,
            'edge_budget': edge_budget, 'tolerance': tol, 'first_inside': over[:8],
            'identical': got == golden}


# ============================================================================================================
# The subcommands
# ============================================================================================================
SUBMISSION_RING_DWORDS = len(ring_words(0, 0, 0, 0, 0))


def p67_prologue(s, out, step, submissions):
    """Stage 1 gates, the arena, a booted GMU with GX on, and the idle CP cp-fence started; room on the ring
    for every submission this command makes; a framebuffer mapping left by an interrupted run removed."""
    S3.prologue(s, out)
    host = S3.arena_prove(s, out)
    s.step = step
    S3.gmu_running_proven(s, out, host)
    if host['oob_token'] != host['boot_token']:
        raise Stop(step, 'OOB GPU_SET was not acknowledged for this GMU boot: run oob and cp-fence first')
    status, on = S3.gx_on_proven(s, 'OOB GPU_SET held for this boot')
    if not on:
        raise Stop(step, f'GMU_PWR_CLK_STATUS {status:#x}: GX is off; GX registers stay refused')
    out['order'].append('gx_on')
    wptr = p67_ready(s, out, host)
    ring_dwords = R.ARENA_LAYOUT['cp_rb'][1] // 4
    if wptr + submissions * SUBMISSION_RING_DWORDS >= ring_dwords:
        raise Stop(step, f'the ring has {ring_dwords - wptr} dwords left, {submissions} submissions need '
                         f'{submissions * SUBMISSION_RING_DWORDS} (this tool never wraps the ring): quiesce and start over')
    out['order'].append('cp_ready')
    if host.get('fb_mapped') or fb_mapped_now(s):
        # An interrupted `blit --to-framebuffer` (link lost after the mapping): the CP is idle (just checked),
        # so the pages go before anything else is submitted.
        install_tables(s, out, Window(s.arena[0]), 'leftover framebuffer mapping removed')
        host['fb_mapped'] = 0
        S3.write_host(s, host)
        out['leftover_fb_mapping_removed'] = True
        if fb_mapped_now(s):
            raise Stop(step, 'a framebuffer mapping is still in CB0 after removing it')
    return host


def prepare_window(s, out, host, label):
    """The render window on CB0 (arena regions only), its zeroed misc page."""
    win = Window(s.arena[0])
    install_tables(s, out, win, label)
    host['p67_token'] = host['boot_token']
    S3.write_host(s, host)
    off, size = R.ARENA_LAYOUT['p67_misc']
    s.mem_fill(off, size, 'misc page (border colours, empty descriptor sets)')
    return win


def upload(s, name, offset, data, what):
    off, size = R.ARENA_LAYOUT[name]
    if offset < 0 or offset + len(data) > size:
        raise Refused(f'{what}: {len(data)} bytes at {name}+{offset:#x} overflow the region')
    s.mem_write(off + offset, data, what)
    if s.mem_read(off + offset, len(data), f'{what} (verify)') != data:
        raise Stop(s.step, f'{what} did not read back')


def canary(length):
    return (CANARY * (length // 4 + 1))[:length]


def save_image(args, suffix, data, out, key):
    prefix = getattr(args, 'image_prefix', None)
    if prefix:
        path = Path(f'{prefix}.{suffix}')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        out.setdefault('images', {})[key] = str(path)


def cmd_blit(s, out, args):
    to_fb = getattr(args, 'to_framebuffer', False)
    host = p67_prologue(s, out, 'D0', 4 if to_fb else 2)
    fb, problems = fb_discover(s, out, args)
    if to_fb and problems:
        raise Stop('D0', 'framebuffer step refused: ' + '; '.join(problems), framebuffer=out['framebuffer'])
    out['framebuffer']['problems'] = problems
    if args.stop_after == 'D0':
        out['stopped_after'] = 'D0'
        return
    s.step = 'D1'
    win = prepare_window(s, out, host, 'render window')
    source = p6_source()
    upload(s, 'p6_src', 0, source, 'P6 source image')
    dst_off, dst_size = R.ARENA_LAYOUT['p6_dst']
    upload(s, 'p6_dst', 0, canary(dst_size), 'P6 target canary')
    out['order'].append('p6_window')
    if args.stop_after == 'D1':
        out['stopped_after'] = 'D1'
        return
    g, pitch, n = P6['guard'], P6['pitch'], P6['size']

    def read_target(what):
        blob = s.mem_read(dst_off, dst_size, what)
        guards_ok = blob[:g] == canary(dst_size)[:g] and blob[g + pitch * n:] == canary(dst_size)[g + pitch * n:]
        return blob[g:g + pitch * n], guards_ok

    # D2: solid clear of a rectangle inside the target (fd6_clear_texture)
    s.step = 'D2'
    cs = build_p6_clear_ib(win, next_fence(host))
    p67_submit(s, out, host, win, cs, 'p6-clear', 'P6 2D clear')
    target, guards_ok = read_target('P6 target after the clear')
    x0, y0, w, h = P6['clear_rect']
    color = bgra(P6['clear'])
    can = canary(dst_size)[g:g + pitch * n]
    want = lambda x, y: color if x0 <= x < x0 + w and y0 <= y < y0 + h else can[y * pitch + 4 * x:y * pitch + 4 * x + 4]
    bad, first = check_region(target, want, n, n, pitch, 'clear')
    out['p6_clear'] = {'rect': P6['clear_rect'], 'color_bgra': color.hex(), 'bad_pixels': bad, 'first': first,
                       'guards_intact': guards_ok}
    if bad or not guards_ok:
        raise Stop('D2', f'the 2D clear wrote {bad} wrong pixels (inside: clear colour, outside: canary) '
                         f'{"" if guards_ok else "and touched the guard bands"}', p6_clear=out['p6_clear'])
    out['order'].append('p6_clear')
    if args.stop_after == 'D2':
        out['stopped_after'] = 'D2'
        return
    # D3: 2D copy of the whole source into the target (fd6_blit -> emit_blit_texture)
    s.step = 'D3'
    cs = build_p6_copy_ib(win, next_fence(host))
    p67_submit(s, out, host, win, cs, 'p6-copy', 'P6 2D copy')
    target, guards_ok = read_target('P6 target after the copy')
    identical = target == source
    out['p6_copy'] = {'identical': identical, 'guards_intact': guards_ok,
                      'sha256': hashlib.sha256(target).hexdigest()}
    save_image(args, 'p6.bgra', target, out, 'p6_target_256x256_bgra8')
    if not identical or not guards_ok:
        bad, first = check_region(target, lambda x, y: source[y * pitch + 4 * x:y * pitch + 4 * x + 4], n, n, pitch, 'copy')
        out['p6_copy'].update(bad_pixels=bad, first=first)
        raise Stop('D3', f'the 2D copy differs from the source in {bad} pixels'
                         f'{"" if guards_ok else " and touched the guard bands"}', p6_copy=out['p6_copy'])
    out['order'].append('p6_copy')
    if args.stop_after == 'D3' or not to_fb:
        if not to_fb:
            out['framebuffer']['step'] = 'not requested (--to-framebuffer)'
        return
    framebuffer_step(s, out, host, args, fb, source)


def framebuffer_step(s, out, host, args, fb, source):
    """D4 (opt-in): only the pages of the rectangle's rows go into CB0, for two submissions, then out again."""
    s.step = 'D4'
    rect = fb_rect(fb, args)
    span = fb_window_span(fb, rect)
    x, y, w, h = rect
    report = out['framebuffer']
    report.update(rect=rect, span={'pa': hx(span['pa']), 'size': hx(span['size']), 'va': hx(span['va'])})
    report['console_was_enabled'] = bool(s.p.fb_console(False))      # P_FB_SHUTDOWN: q1n1 stops drawing, pixels stay
    s.log.append({'seq': len(s.log), 'step': 'D4', 'op': 'fb_console', 'enabled': False})
    before = fb_read(s, fb, span, 0, span['size'], 'framebuffer span (before)')
    host['fb_mapped'] = span['pa']
    S3.write_host(s, host)
    winfb = Window(s.arena[0], span)
    pitch, ro = fb['pitch'], span['rect_offset']

    def check(after, pixel, label):
        bad, first = 0, []
        for yy in range(h):
            row = ro + yy * pitch
            for xx in range(w):
                at = row + 4 * (x + xx)
                if after[at:at + 4] != pixel(xx, yy):
                    bad += 1
                    if len(first) < 8:
                        first.append({'x': x + xx, 'y': y + yy, 'got': after[at:at + 4].hex(), 'want': pixel(xx, yy).hex()})
        outside = bytearray(after)
        for yy in range(h):
            at = ro + yy * pitch + 4 * x
            outside[at:at + 4 * w] = before[at:at + 4 * w]
        changed = sum(1 for a, b in zip(outside, before) if a != b)
        report[label] = {'bad_pixels': bad, 'first': first, 'bytes_changed_outside_rect': changed}
        if bad or changed:
            raise Stop('D4', f'framebuffer {label}: {bad} wrong pixels in the rectangle, {changed} bytes changed '
                             'outside it', framebuffer=report)

    try:
        cs = build_p6_clear_ib(winfb, next_fence(host), target='framebuffer', fb=fb, rect=rect)
        p67_submit(s, out, host, winfb, cs, 'fb-clear', 'P6 framebuffer clear')
        c = P6['fb_clear']
        color = bgra(c) if fb['layout'] == 'bgra' else bytes(unorm8(v) for v in c)
        check(fb_read(s, fb, span, 0, span['size'], 'framebuffer span (after clear)'), lambda xx, yy: color, 'clear')
        cs = build_p6_copy_ib(winfb, next_fence(host), target='framebuffer', fb=fb, rect=rect)
        p67_submit(s, out, host, winfb, cs, 'fb-copy', 'P6 framebuffer blit')
        sp = P6['pitch']
        after = fb_read(s, fb, span, 0, span['size'], 'framebuffer span (after blit)')
        check(after, lambda xx, yy: source[yy * sp + 4 * xx:yy * sp + 4 * xx + 4], 'blit')
        report['left_on_panel'] = f'{w}x{h} at {x},{y}: the P6 source image'
        out['order'].append('fb_blit')
    finally:
        # Out again, whatever happened in between -- unless the link or the access that failed means nothing
        # more should be sent (an SError, a synchronous abort): then quiesce/reboot, as the runbook says.
        original = sys.exc_info()[1]
        if not isinstance(original, (S3.SErrorStop, S3.AbortStop, S3.Q.ProxyError, OSError)):
            s.step = 'D4'
            try:
                install_tables(s, out, Window(s.arena[0]), 'framebuffer unmapped')
                report['unmapped'] = not fb_mapped_now(s)
                if report['unmapped']:
                    host['fb_mapped'] = 0
                    S3.write_host(s, host)
            except Exception as problem:                      # noqa: BLE001 -- recorded, then re-raised or kept
                # Keep the first failure as the reason (it has the submission's diagnostics); record this one.
                report['unmapped'] = False
                report['unmap_error'] = f'{type(problem).__name__}: {getattr(problem, "reason", problem)}'
                if original is None:
                    raise
                if isinstance(problem, (S3.SErrorStop, S3.AbortStop, S3.Q.ProxyError, OSError)):
                    # The bus or the link failed during the cleanup: that decides what the operator does next
                    # (exit 3/5: the pages may still be in CB0), so it becomes the exit; the first failure stays
                    # on record next to it.
                    report['first_failure'] = (f'{getattr(original, "step", "D4")}: '
                                               f'{getattr(original, "reason", original)}')
                    raise
            if not report['unmapped'] and original is None:
                raise Stop('D4', 'the framebuffer pages are still in CB0 after unmapping them', framebuffer=report)


def cmd_triangle(s, out, args):
    host = p67_prologue(s, out, 'T0', 1)
    sh, golden, digest = p7_assets()
    problems, findings, sampling = variant_checks(sh)
    out['p7'] = {'shaders_sha256': digest, 'findings': findings, 'sampling': sampling,
                 'vs': {k: sh['vs'][k] for k in ('instrlen', 'constlen', 'branchstack_hw', 'mergedregs')},
                 'fs': {k: sh['fs'][k] for k in ('instrlen', 'constlen', 'branchstack_hw', 'mergedregs', 'total_in')}}
    if problems:
        raise Stop('T0', 'the P7 variants are outside what this tool builds: ' + '; '.join(problems), p7=out['p7'])
    if args.stop_after == 'T0':
        out['stopped_after'] = 'T0'
        return
    s.step = 'T1'
    win = prepare_window(s, out, host, 'render window')
    code_off, code_size = R.ARENA_LAYOUT['p7_code']
    s.mem_fill(code_off, code_size, 'P7 code region')
    for stage in ('vs', 'fs'):
        upload(s, 'p7_code', P7_CODE[stage], bytes.fromhex(sh[stage]['code_hex']), f'P7 {stage.upper()} ir3 code')
    data_off, data_size = R.ARENA_LAYOUT['p7_data']
    s.mem_fill(data_off, data_size, 'P7 data region')
    upload(s, 'p7_data', P7_DATA['vbo'], p7_vbo(), 'P7 vertex buffer')
    upload(s, 'p7_data', P7_DATA['ubo'], p7_ubo(), 'P7 UBO 1 (mvp)')
    tex_va = win.addr('p7_tex', 0, P7['texture']['pitch'] * P7['texture']['height'], False, 'P7 texture')
    tdesc = texture_descriptor(tex_va, P7['texture']['width'], P7['texture']['height'], P7['texture']['pitch'], 'rgba')
    upload(s, 'p7_data', P7_DATA['tex_desc'], struct.pack('<16I', *tdesc), 'P7 T#0 descriptor')
    upload(s, 'p7_data', P7_DATA['samp_desc'], struct.pack('<4I', *sampler_descriptor()), 'P7 S#0 descriptor')
    upload(s, 'p7_tex', 0, p7_texture(), 'P7 texture 64x64 RGBA8')
    rt_off, rt_size = R.ARENA_LAYOUT['p7_rt']
    upload(s, 'p7_rt', 0, canary(rt_size), 'P7 render target canary')
    out['order'].append('p7_uploaded')
    if args.stop_after == 'T1':
        out['stopped_after'] = 'T1'
        return
    s.step = 'T2'
    cs, link = build_p7_ib(win, sh, sampling, next_fence(host))
    out['p7']['linkage'] = {'max_loc': link['max_loc'], 'cnt': link['cnt'], 'position_loc': link['position_loc']}
    p67_submit(s, out, host, win, cs, 'p7-triangle', 'P7 triangle')
    out['order'].append('p7_drawn')
    if args.stop_after == 'T2':
        out['stopped_after'] = 'T2'
        return
    s.step = 'T3'
    blob = s.mem_read(rt_off, rt_size, 'P7 render target (readback)')
    size = P7['pitch'] * P7['height']
    image = blob[P7_RT_GUARD:P7_RT_GUARD + size]
    guards_ok = blob[:P7_RT_GUARD] == canary(rt_size)[:P7_RT_GUARD] and \
        blob[P7_RT_GUARD + size:] == canary(rt_size)[P7_RT_GUARD + size:]
    save_image(args, 'p7.bgra', image, out, 'p7_target_96x64_bgra8')
    result = compare_golden(image, golden, P7['width'], P7['height'], P7['tolerance'], P7['edge_budget'],
                            P7['vertices'])
    result['guards_intact'] = guards_ok
    result['sha256'] = hashlib.sha256(image).hexdigest()
    out['p7']['compare'] = result
    if not guards_ok or not result['ok']:
        raise Stop('T3', f'the triangle does not match the M3 Max golden (max |diff| {result["max_abs_diff"]}, '
                         f'{result["pixels_over_tolerance_inside"]} pixels over {P7["tolerance"]} inside, '
                         f'{result["pixels_over_tolerance_on_edges"]} on edges)'
                         f'{"" if guards_ok else "; the guard bands were written"}', compare=result)
    out['order'].append('p7_matches_golden')


COMMANDS = {'blit': (cmd_blit, True), 'triangle': (cmd_triangle, True)}
STEP_IDS = {'blit': ('D0', 'D1', 'D2', 'D3'), 'triangle': ('T0', 'T1', 'T2')}
S3.COMMANDS.update(COMMANDS)          # also when this module is imported before a16gpu_stage3 (see its end)
S3.STEP_IDS.update(STEP_IDS)


def add_parsers(sub):
    b = sub.add_parser('blit', help='Stage 3 P6: 2D clear and blit in the arena (--to-framebuffer: on the panel)')
    b.add_argument('--write', action='store_true', help='required: this submits GPU work and writes the arena')
    b.add_argument('--stop-after', choices=STEP_IDS['blit'])
    b.add_argument('--to-framebuffer', action='store_true',
                   help='opt-in: map only the framebuffer rows of one rectangle, clear it and blit the image into it')
    b.add_argument('--fb-rect', help='X,Y,W,H of the framebuffer rectangle (default: 256x256 near the top right)')
    b.add_argument('--fb-any-type', action='store_true',
                   help='go on when the EFI map does not describe the framebuffer as Reserved (never for MMIO)')
    t = sub.add_parser('triangle', help='Stage 3 P7: SimpleVertex + SimpleTextureFragment, one triangle, vs M3 Max')
    t.add_argument('--write', action='store_true', help='required: this submits GPU work and writes the arena')
    t.add_argument('--stop-after', choices=STEP_IDS['triangle'])


def write_scene(directory):
    """The P7 scene for tools/a16gpu-p7/p7_metal_golden.swift: the same bytes the A16 tool uploads."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / 'texture.rgba').write_bytes(p7_texture())
    scene = {'width': P7['width'], 'height': P7['height'], 'clear': list(P7['clear']),
             'metallib': SKYLIGHT_METALLIB, 'metallib_sha256': SKYLIGHT_SHA256,
             'vertex_function': 'SimpleVertex', 'fragment_function': 'SimpleTextureFragment',
             'vertices': list(P7['vertices']), 'stride': P7['stride'], 'vertex_count': P7['vertex_count'],
             'attributes': list(P7['attributes']), 'mvp': list(P7['mvp']), 'mvp_buffer_index': P7['mvp_buffer_index'],
             'texture': dict(P7['texture'], file='texture.rgba'), 'sampler': dict(P7['sampler'])}
    (d / 'scene.json').write_text(json.dumps(scene, indent=1) + '\n')
    return d


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write-scene':
        print(write_scene(sys.argv[2]))
    else:
        sys.exit('usage: a16gpu_p6p7.py --write-scene DIR   (the subcommands run from tools/a16gpu.py)')
