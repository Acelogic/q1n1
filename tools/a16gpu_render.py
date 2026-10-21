# SPDX-License-Identifier: MIT
"""Adreno X2-90, Stage 6's A16 half: `render` -- the 28 wscts WindowServer pipelines, compiled by air2ir3 (Stage 6
cache), drawn one submission and one fence each on the CP `cp-fence` started, read back and compared with the M3 Max
Metal goldens of the same pipelines, inputs and constants (docs/A16-ADRENO-STAGE6-RENDER.md, runbook
docs/A16-ADRENO-STAGE6-RENDER-RUNBOOK.md).

  render --write                       R0 checks, R1 window + uploads, R2 one draw per function (fence, readback,
                                       compare), R3 verdict
  render --write --functions A,B       only those functions (in scene order; probe_ubo_hi, the one out-of-envelope
                                       probe, always last)
  render --write --probes              the single-feature A16 probes (never the gate): UBO index sweep, output
                                       constants (alias.rt), framebuffer fetch, an A8 texture; probe_ubo_hi last

What it adds to P7 (tools/a16gpu_p6p7.py, which it builds on and does not change): any pipeline of the Stage 6
library, programmed from the exported variants through the binding ABI (tools/a16gpu_bindings.py, BINDING-ABI v6:
UBO table below 16 incl. the driver table, T#/S# with constexpr samplers, framebuffer fetch through bindless set 4,
vertex_id and frag_coord system values, varyings linked by name), up to two colour attachments in BGRA8/R8/RG8,
premultiplied source-over blending, 1D / 1D-array / RGBA16F textures. The register sequences are P7's (gallium's
a8xx sysmem batch), generalised the way the same Mesa functions generalise (emit_mrt's loop, the blend state's
per-RT loop, build_texture_state's holes, emit_vfd_dest's sideband, fd6_build_bindless_state's set 4).

Safety rules (P6/P7's, unchanged): the CP must be the idle one cp-fence left for this GMU boot; only arena memory is
mapped (the render regions below, each with its own permission; only render targets are GPU-writable); every
address an IB carries comes from the window and is walked through CB0's tables read back from DRAM before each
submission; every CPU write is an arena write; each submission's fence is a bounded poll, then idle, CB0 FSR and the
sticky error-interrupt bits; a fence that does not come records read-only diagnostics and stops (exit 2). An image
that differs from its golden is a result, not a fault: the remaining functions still draw, and R3 reports every
function (exit 2 with `stopped.step` R3 if any is over tolerance).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct

import adreno_x2 as R
import a8xx_regs as X
import a16gpu_stage3 as S3
import a16gpu_p6p7 as M
import a16gpu_bindings as B
import a16gpu_render_scenes as SC

Stop, Refused = S3.Stop, S3.Refused
hx = S3.hx
reg, desc, pkt_word, _raw = M.reg, M.desc, M.pkt_word, M._raw
HERE = Path(__file__).resolve().parent
ASSETS = HERE / 'a16gpu-render'
SHADERS = ASSETS / 'render-shaders.json'
SHADERS_SHA256 = '8bb658dfe04cf8f108dd49dec60f06be32b0c3b64e46fd2d0ff3470af404fd9e'   # make-render-shaders.sh (fix4 + air2ir3-abi6.patch, ABI 6, air2ir3-src-a51400fcd94cea44)
GOLDENS = ASSETS / 'goldens'
GOLDENS_SHA256 = '3f44829cf90b8a39340ed04a8a77d88be89d7f9ac5722392a257e4cb0b45232b'   # render_metal_golden.swift, M3 Max, macOS 27 26A428 (the 35 of run 1 byte-identical + 6 probes)


# ============================================================================================================
# The render window: arena regions (adreno_x2.ARENA_LAYOUT r_*), CB0 VA from R.RENDER_VA
# ============================================================================================================
RO, RW = S3.UNPRIV_RO, S3.UNPRIV_RW
RENDER_REGIONS = (('r_ib', RO), ('r_misc', RO), ('r_code', RO), ('r_data', RO), ('r_tex', RO), ('r_rt', RW))
IB_SLOT = 0x4000                  # 16 KiB per submission's IB
CODE_SLOT = 0x1000                # 4 KiB per shader stage
DATA_SLOT = 0x4000                # 16 KiB of UBOs / VBO / descriptors per function
RT_GUARD = 0x1000                 # canary before every render target (and after the last)


def region_va(name, offset=0):
    off, size = R.ARENA_LAYOUT[name]
    if not 0 <= offset <= size:
        raise Refused(f'{name}+{offset:#x} outside the region')
    return R.RENDER_VA + (off - R.RENDER_ARENA_WINDOW[0]) + offset


def render_mappings(base):
    return [(name, region_va(name), base + R.ARENA_LAYOUT[name][0], R.ARENA_LAYOUT[name][1], attrs)
            for name, attrs in RENDER_REGIONS]


class Window(M.Window):
    """P6/P7's Window (addr() refuses anything outside a mapped region or a write to a read-only one) over the
    render regions: the CP's memptrs and the r_* regions; never the framebuffer."""

    def __init__(self, base):   # noqa: super().__init__ would register the P6/P7 regions
        self.base = base
        self.ranges = [('memptrs', R.CP_MEMPTRS_VA, base + R.ARENA_LAYOUT['cp_memptrs'][0],
                        R.ARENA_LAYOUT['cp_memptrs'][1], True)]
        for name, va, pa, size, attrs in render_mappings(base):
            self.ranges.append((name, va, pa, size, not attrs & R.PTE_AP_RDONLY))
        self.fb_span = None
        self.refs = []


def build_tables(base):
    """CB0: cp-fence's own tables first (their pages never move), then the render regions."""
    tables = S3.build_tables(base, 'gpu')
    for _name, va, pa, size, attrs in render_mappings(base):
        tables.map(va, pa, size, attrs)
    return tables


def install_tables(s, out, label):
    """M.install_tables for the render window: only table pages that differ are written (highest first), TLBIASID
    on CB0, the image read back. Only called with the CP idle and its pointers agreeing (p67_ready)."""
    base = s.arena[0]
    offset, blob = build_tables(base).image()
    half = R.ARENA_LAYOUT['tables'][1] // 2
    if len(blob) > half:
        raise Refused('CB0 tables outgrow their half of the table pool')
    current = s.mem_read(offset, half, f'CB0 tables ({label}, compare)')
    want = blob + bytes(half - len(blob))
    changed = current != want
    if changed:
        for page in range(half - 4096, -1, -4096):
            if current[page:page + 4096] != want[page:page + 4096]:
                s.mem_write(offset + page, want[page:page + 4096], f'CB0 table page +{page:#x} ({label})')
        S3.smmu_tlbi(s, R.GPU_CB)
    image = s.mem_read(offset, len(blob), f'CB0 tables ({label}, verify)')
    if image != blob:
        raise Stop(s.step, f'CB0 tables did not read back ({label})')
    out.setdefault('cb0_tables', []).append({'label': label, 'changed': changed, 'bytes': len(blob)})
    return image, offset


def tables_as_installed(s, label):
    """Before each submission after R1: CB0's table image read back from DRAM must still be what R1 installed and
    verified (only the image, 28 KiB, not the whole 512 KiB half-pool R1 compared: nothing else writes it)."""
    offset, blob = build_tables(s.arena[0]).image()
    image = s.mem_read(offset, len(blob), f'CB0 tables ({label}, verify)')
    if image != blob:
        raise Stop(s.step, f'CB0 tables changed since R1 installed them ({label})')
    return image, offset


def verify_refs(s, win, tables_blob, tables_offset):
    """Every address the IB carries, both ends, walked through the tables as read back from DRAM: inside the
    arena, where the window says, writable where the GPU writes."""
    base = s.arena[0]
    table_base = base + tables_offset
    read_u64 = lambda pa: struct.unpack_from('<Q', tables_blob, pa - table_base)[0] \
        if table_base <= pa < table_base + len(tables_blob) else 0
    ttbr = S3.ttbr_for(build_tables(base), R.GPU_CB)
    arena = (base, base + s.arena[1])
    bad = []
    for ref in win.refs:
        for off in {0, ref['size'] - 1}:
            hit = S3.walk(read_u64, ttbr, ref['va'] + off)
            pa = hit[0] if hit else None
            ok = pa == ref['pa'] + off and arena[0] <= pa < arena[1]
            if ok and ref['write'] and hit[1] & R.PTE_AP_RDONLY:
                ok = False
            if not ok:
                bad.append((ref['what'], hx(ref['va'] + off), hx(pa) if pa is not None else 'fault'))
    if bad:
        raise Stop(s.step, f'CB0 does not translate the command stream\'s addresses as built: {bad[:6]}')
    return len(win.refs)


# ============================================================================================================
# Assets: the exported variants and the goldens (both sha-pinned), the scenes (Python, a16gpu_render_scenes)
# ============================================================================================================
def load_assets(check=True):
    if not SHADERS.is_file():
        raise Stop('R0', f'{SHADERS} is not distributed (it is compiled from shaders shipped with macOS): '
                         'generate it with tools/a16gpu-render/make-render-shaders.sh')
    blob = SHADERS.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if check and SHADERS_SHA256 and digest != SHADERS_SHA256:
        raise Stop('R0', f'{SHADERS} sha256 {digest} is not the pinned {SHADERS_SHA256}')
    shaders = json.loads(blob)
    man_blob = (GOLDENS / 'goldens.json').read_bytes()
    man_digest = hashlib.sha256(man_blob).hexdigest()
    if check and GOLDENS_SHA256 and man_digest != GOLDENS_SHA256:
        raise Stop('R0', f'{GOLDENS}/goldens.json sha256 {man_digest} is not the pinned {GOLDENS_SHA256}')
    manifest = json.loads(man_blob)
    goldens = {}
    for g in manifest['goldens']:
        images = {}
        for r in g['rts']:
            data = (GOLDENS / r['file']).read_bytes()
            if check and hashlib.sha256(data).hexdigest() != r['sha256']:
                raise Stop('R0', f'golden {r["file"]} does not match goldens.json')
            images[r['index']] = data
        goldens[g['name']] = {'digest': g['digest'], 'images': images}
    return shaders, goldens, {'shaders_sha256': digest, 'goldens_sha256': man_digest,
                              'golden_device': manifest.get('device'), 'golden_os': manifest.get('os')}


# ============================================================================================================
# Formats and descriptors
# ============================================================================================================
# colour attachments: (RB format, swap, bytes per pixel, texture-view format) -- fd6_format_table.c (color and tex
# columns: B10G10R10A2_UNORM renders as 10_10_10_2_UNORM_DEST and is sampled as 10_10_10_2_UNORM); the XR
# formats are their BINDING-ABI 4.5 views: 554 = B10G10R10A2_UNORM, 552 = R16G16B16A16_UNORM with the B,G,R,A
# swap (WXYZ on a 16-bit format: on the ABI's A16 test list)
RT_HW = {'bgra8': ('FMT6_8_8_8_8_UNORM', 'WXYZ', 4, 'FMT6_8_8_8_8_UNORM'),
         'r8': ('FMT6_8_UNORM', 'WZYX', 1, 'FMT6_8_UNORM'),
         'rg8': ('FMT6_8_8_UNORM', 'WZYX', 2, 'FMT6_8_8_UNORM'),
         'bgr10_xr': ('FMT6_10_10_10_2_UNORM_DEST', 'WXYZ', 4, 'FMT6_10_10_10_2_UNORM'),
         'bgra10_xr': ('FMT6_16_16_16_16_UNORM', 'WXYZ', 8, 'FMT6_16_16_16_16_UNORM')}
TEX_HW = {'rgba8': ('FMT6_8_8_8_8_UNORM', 'WZYX', 4), 'rgba16f': ('FMT6_16_16_16_16_FLOAT', 'WZYX', 8),
          'bgr10_xr': ('FMT6_10_10_10_2_UNORM', 'WXYZ', 4), 'bgra10_xr': ('FMT6_16_16_16_16_UNORM', 'WXYZ', 8),
          # fd6_format_table.c _TC(A8_UNORM, A8_UNORM, WZYX); fdl6_view_init keeps A8's identity swizzle (the
          # hardware format returns (0, 0, 0, a)), unlike the other alpha formats freedreno fakes with R8
          'a8': ('FMT6_A8_UNORM', 'WZYX', 1)}
MTL_BLEND_FACTOR = {0: 'zero', 1: 'one', 4: 'source_alpha', 5: 'one_minus_source_alpha', 8: 'destination_alpha',
                    9: 'one_minus_destination_alpha'}
MTL_BLEND_OP = {0: 'add', 1: 'subtract', 2: 'reverse_subtract', 3: 'min', 4: 'max'}
VTX_HW = {'float': 'FMT6_32_FLOAT', 'float2': 'FMT6_32_32_FLOAT', 'float3': 'FMT6_32_32_32_FLOAT',
          'float4': 'FMT6_32_32_32_32_FLOAT'}
VTX_BYTES = {'float': 4, 'float2': 8, 'float3': 12, 'float4': 16}
BLEND_FACTOR = {'zero': 'FACTOR_ZERO', 'one': 'FACTOR_ONE', 'source_alpha': 'FACTOR_SRC_ALPHA',
                'one_minus_source_alpha': 'FACTOR_ONE_MINUS_SRC_ALPHA', 'destination_alpha': 'FACTOR_DST_ALPHA',
                'one_minus_destination_alpha': 'FACTOR_ONE_MINUS_DST_ALPHA'}
BLEND_OP = {'add': 'BLEND_DST_PLUS_SRC', 'subtract': 'BLEND_SRC_MINUS_DST', 'reverse_subtract': 'BLEND_DST_MINUS_SRC',
            'min': 'BLEND_MIN_DST_SRC', 'max': 'BLEND_MAX_DST_SRC'}


def linear_pitch(width, cpp):
    """fdl6_layout_image, linear: pitchalign = cpp_shift + 6 (fdl_set_pitchalign), pitch aligned to 1 << it."""
    shift = (cpp - 1).bit_length() + 6
    return -(-width * cpp // (1 << shift)) * (1 << shift), shift


def tex_layout(t):
    """(pitch, layer stride, total bytes, pitchalign) of a linear texture as Mesa lays it out (layer_first, layers
    4 KiB aligned for arrays)."""
    cpp = TEX_HW[t['format']][2]
    pitch, align = linear_pitch(t['width'], cpp)
    slice_bytes = pitch * t['height']
    layer = -(-slice_bytes // 4096) * 4096            # layer_first: layer_size = align64(size, 4096)
    return pitch, layer, layer * t['layers'], align


def texture_descriptor(va, t):
    """fdl6_view_init<A8XX> for a linear single-level 2D / 1D / 1D-array texture (fd6_view.cc), 16 dwords."""
    fmt, swap, _cpp = TEX_HW[t['format']]
    pitch, layer, _size, align = tex_layout(t)
    kind = {'2d': 'A6XX_TEX_2D', '1d': 'A6XX_TEX_1D', '1d_array': 'A6XX_TEX_1D'}[t['type']]
    d = [0] * 16
    d[0] = desc('A8XX_TEX_MEMOBJ', 0, base_lo=va & 0xFFFFFFFF)
    d[1] = desc('A8XX_TEX_MEMOBJ', 1, base_hi=va >> 32, type=kind, depth=t['layers'])
    d[2] = desc('A8XX_TEX_MEMOBJ', 2, width=t['width'], height=t['height'], samples='MSAA_ONE')
    d[3] = desc('A8XX_TEX_MEMOBJ', 3, fmt=fmt, swap=swap, swiz_x='A8XX_SWIZ_X', swiz_y='A8XX_SWIZ_Y',
                swiz_z='A8XX_SWIZ_Z', swiz_w='A8XX_SWIZ_W')
    d[4] = desc('A8XX_TEX_MEMOBJ', 4, tile_mode='TILE6_LINEAR')
    d[6] = desc('A8XX_TEX_MEMOBJ', 6, tex_line_offset=pitch * 8, miplvls=0, min_line_offset=align - 6)
    d[7] = desc('A8XX_TEX_MEMOBJ', 7, array_slice_offset=layer)
    d[9] = desc('A8XX_TEX_MEMOBJ', 9, min_lod_clamp=0.0)
    return d


def rt_view_descriptor(va, fmt, width, height, pitch):
    """The framebuffer-fetch view of a colour attachment (patch_fb_read_sysmem: fdl6_view_init of the surface)."""
    _rb, swap, cpp, hw = RT_HW[fmt]
    _p, align = linear_pitch(width, cpp)
    d = [0] * 16
    d[0] = desc('A8XX_TEX_MEMOBJ', 0, base_lo=va & 0xFFFFFFFF)
    d[1] = desc('A8XX_TEX_MEMOBJ', 1, base_hi=va >> 32, type='A6XX_TEX_2D', depth=1)
    d[2] = desc('A8XX_TEX_MEMOBJ', 2, width=width, height=height, samples='MSAA_ONE')
    d[3] = desc('A8XX_TEX_MEMOBJ', 3, fmt=hw, swap=swap, swiz_x='A8XX_SWIZ_X', swiz_y='A8XX_SWIZ_Y',
                swiz_z='A8XX_SWIZ_Z', swiz_w='A8XX_SWIZ_W')
    d[4] = desc('A8XX_TEX_MEMOBJ', 4, tile_mode='TILE6_LINEAR')
    d[6] = desc('A8XX_TEX_MEMOBJ', 6, tex_line_offset=pitch * 8, miplvls=0, min_line_offset=align - 6)
    d[7] = desc('A8XX_TEX_MEMOBJ', 7, array_slice_offset=pitch * height)
    d[9] = desc('A8XX_TEX_MEMOBJ', 9, min_lod_clamp=0.0)
    return d


def api_sampler_descriptor(sp):
    """An MTLSamplerDescriptor (no mipmaps, normalized, lod 0..FLT_MAX) as Stage 6's air_sampler_a8xx builds a
    sampler (fd6_sampler_state_create<A8XX>): MIPMAPING_DIS, lod clamped to 0..0.25; clamp_to_zero is
    CLAMP_TO_BORDER with the fast transparent-black border."""
    wrap = {'clamp_to_edge': 'A6XX_TEX_CLAMP_TO_EDGE', 'repeat': 'A6XX_TEX_REPEAT',
            'mirrored_repeat': 'A6XX_TEX_MIRROR_REPEAT', 'clamp_to_zero': 'A6XX_TEX_CLAMP_TO_BORDER'}[sp['address']]
    filt = {'linear': 'A6XX_TEX_LINEAR', 'nearest': 'A6XX_TEX_NEAREST'}
    d0 = desc('A8XX_TEX_SAMP', 0, mipmaping_dis=True, xy_mag=filt[sp['mag']], xy_min=filt[sp['min']], wrap_s=wrap,
              wrap_t=wrap, wrap_r=wrap, lod_bias=0.0, aniso='A6XX_TEX_ANISO_1')
    d1 = desc('A8XX_TEX_SAMP', 1, max_lod=0.25, min_lod=0.0, reduction_mode='A6XX_REDUCTION_MODE_AVERAGE',
              compare_func='FUNC_NEVER', chroma_linear=sp['mag'] == 'linear' and sp['min'] == 'linear')
    d2 = desc('A8XX_TEX_SAMP', 2, fastbordercoloren=True, fastbordercolor='A6XX_BORDER_COLOR_0_0_0_0') \
        if sp['address'] == 'clamp_to_zero' else 0
    return [d0, d1, d2, 0]


# ============================================================================================================
# The plan: where every byte of every function goes (pure: the same for the tool and the model)
# ============================================================================================================
def select(names=None, xr=False, probes=False):
    """The gate's 28 scenes in order, plus the XR set with `xr`, or only the probes with `probes`; `names` picks any
    of them (in that order)."""
    everything = SC.all_scenes()
    if names:
        want = [n.strip() for n in names.split(',') if n.strip()]
        known = {s['name'] for s in everything}
        unknown = [n for n in want if n not in known]
        if unknown:
            raise Stop('R0', f'unknown functions {unknown} (tools/a16gpu_render_scenes.py --list)')
        return out_of_envelope_last([s for s in everything if s['name'] in want])
    if probes:
        return out_of_envelope_last([s for s in everything if s['group'] == 'probe'])
    return [s for s in everything if s['group'] == 'gate' or (xr and s['group'] == 'xr')]


def out_of_envelope(sc):
    """A probe that programs state outside what Mesa ever emits: probe_ubo_hi (--test-ubo-slots 32: 31 FS UBO
    descriptors and ldc from UBO 16..30, which a8xx does not honour)."""
    return '--test-ubo-slots' in sc.get('compile_args', ())


def out_of_envelope_last(scenes):
    """Scene order, except that out-of-envelope probes draw after everything else in the command, so no other result
    of the same command (in particular probe_fetch's descriptor-cache check) comes after that state."""
    return [s for s in scenes if not out_of_envelope(s)] + [s for s in scenes if out_of_envelope(s)]


def _texture_key(t):
    return (t['name'], t['type'], t['format'], t['width'], t['height'], t['layers'],
            hashlib.sha256(t['data']).hexdigest())


def plan(scenes, shaders):
    """Offsets of everything in the render regions, per function; refuses a scene or variant outside the port."""
    textures, tex_at = {}, 0
    out = {'functions': [], 'textures': textures}
    rt_at = 0
    for k, sc in enumerate(scenes):
        pipe = shaders['pipelines'].get(sc['name'])
        if pipe is None:
            raise Refused(f'{sc["name"]}: no exported variants')
        vs, fs = pipe['vs'], pipe['fs']
        # textures (deduplicated across functions)
        tex = {}
        for t in sc['textures']:
            key = _texture_key(t)
            if key not in textures:
                pitch, layer, size, _a = tex_layout(t)
                if tex_at + size > R.ARENA_LAYOUT['r_tex'][1]:
                    raise Refused('the textures do not fit r_tex')
                textures[key] = {'offset': tex_at, 'size': size, 'pitch': pitch, 'layer': layer, 'tex': t}
                tex_at += -(-size // 4096) * 4096
            tex[t['index']] = textures[key]
        f = {'name': sc['name'], 'scene': sc, 'pipe': pipe, 'tex': tex, 'ib': k * IB_SLOT,
             'code': {'vs': 2 * k * CODE_SLOT, 'fs': (2 * k + 1) * CODE_SLOT}, 'data_base': k * DATA_SLOT}
        if (k + 1) * IB_SLOT > R.ARENA_LAYOUT['r_ib'][1] or (2 * k + 2) * CODE_SLOT > R.ARENA_LAYOUT['r_code'][1] \
                or (k + 1) * DATA_SLOT > R.ARENA_LAYOUT['r_data'][1]:
            raise Refused(f'{sc["name"]}: function {k} outside the IB / code / data regions')
        for st, v in (('vs', vs), ('fs', fs)):
            code = bytes.fromhex(v['code_hex'])
            # instrlen (SP_xS_INSTR_SIZE) counts ir3's instructions in units of 16 and can be shorter than the
            # padded binary (ir3_collect_info); Mesa programs it as is and the SP fetches the rest on demand.
            if not 0 < len(code) <= CODE_SLOT or not 0 < 128 * v['instrlen'] <= CODE_SLOT:
                raise Refused(f'{sc["name"]} {st}: code {len(code)} bytes / instrlen {v["instrlen"]} does not fit 4 KiB')
        # data slot: allocate with 64-byte alignment
        at = [0]

        def take(n, align=64):
            at[0] = -(-at[0] // align) * align
            off = at[0]
            at[0] += n
            if at[0] > DATA_SLOT:
                raise Refused(f'{sc["name"]}: data does not fit its {DATA_SLOT:#x}-byte slot')
            return f['data_base'] + off
        vtx = sc['vertex']
        f['vbo'] = take(len(vtx['data'])) if vtx['data'] else None
        f['buffers'] = {}
        for st in ('vs', 'fs'):
            v = pipe[st]
            count, table = B.ubo_table(v)
            bufs = {b['index']: b for b in sc['buffers'][st]}
            placed = {}
            for idx, b in bufs.items():
                size = max(len(b['data']), -(-B.ubo_min_bytes(v, idx) // 16) * 16 if not b['device'] else 0)
                placed[idx] = {'offset': take(size, 256), 'size': size, 'data': b['data'] + bytes(size - len(b['data'])),
                               'device': b['device']}
            if sc.get('bind_undeclared_ubos'):
                # probe_ubo_hi only: the A16 also gets the scene's other buffers at their index, so a UBO read that
                # lands on another index shows whose value it got (Metal ignores buffers the function does not declare)
                table = dict(table)
                for idx in bufs:
                    if idx < count and idx not in table and not bufs[idx]['device']:
                        table[idx] = ('buffer', idx)
            for slot, what in table.items():
                if what[0] == 'buffer' and what[1] not in bufs:
                    raise Refused(f'{sc["name"]} {st}: constant buffer({what[1]}) has no scene buffer')
                if what[0] == 'buffer' and bufs[what[1]]['device']:
                    raise Refused(f'{sc["name"]} {st}: buffer({what[1]}) is constant space but the scene makes it device')
            for b in B.bindings(v, 'device_buffer'):
                if (b['present'] or b['used']) and (b['metal_index'] not in bufs or not bufs[b['metal_index']]['device']):
                    raise Refused(f'{sc["name"]} {st}: device buffer({b["metal_index"]}) has no device scene buffer')
            f['buffers'][st] = placed
            f.setdefault('ubo', {})[st] = (count, table)
            if any(w[0] == 'driver_table' for w in table.values()):
                f.setdefault('driver_table', {})[st] = take(B.DRIVER_TABLE_BYTES, 256)
        ntex, nsamp = B.texture_counts(fs)
        if B.texture_counts(vs) != (0, 0):
            raise Refused(f'{sc["name"]}: the VS samples textures (not built here)')
        f['ntex'], f['nsamp'] = ntex, nsamp
        # descriptors are loaded only when the FS binds a non-bindless T# or S# (gallium's build_texture_state has
        # nothing to load otherwise: a framebuffer-fetch-only FS has no T#/S# of its own); T# and S# counts are
        # separate since ABI v4 (NTEX, NSAMP)
        loads = bool(B.texture_slots(fs) or any(B.sampler_slots(fs)))
        f['tex_desc'] = take(64 * ntex) if ntex and loads else None
        f['samp_desc'] = take(16 * nsamp) if nsamp and loads else None
        f['fb_fetch'] = B.fb_fetch(fs)
        f['bindless'] = take(64 * B.BINDLESS_DESC_COUNT, 64) if f['fb_fetch'] else None
        # render targets
        f['rts'] = []
        classes = B.rt_classes(fs)
        for r in sc['rts']:
            cpp = RT_HW[r['format']][2]
            if classes[r['index']] != SC.XR_CLASS.get(r['format'], 0):
                raise Refused(f'{sc["name"]}: attachment {r["index"]} is {r["format"]}, the FS variant was compiled for '
                              f'format class {classes[r["index"]]}')
            pitch, _a = linear_pitch(sc['width'], cpp)
            size = -(-pitch * sc['height'] // 4096) * 4096
            f['rts'].append({'index': r['index'], 'format': r['format'], 'pitch': pitch, 'cpp': cpp,
                             'slot': rt_at, 'size': RT_GUARD + size, 'image': rt_at + RT_GUARD, 'init': r['init']})
            rt_at += RT_GUARD + size
        tclasses = B.texture_classes(fs)
        for slot, index in B.texture_slots(fs).items():
            t = next((t for t in sc['textures'] if t['index'] == index), None)
            if t is not None and tclasses[slot] != SC.XR_CLASS.get(t['format'], 0):
                raise Refused(f'{sc["name"]}: texture({index}) is {t["format"]}, the FS variant was compiled for '
                              f'texture class {tclasses[slot]}')
        # an XR texture's border colour must be given in the view's u-domain (a custom border-colour table entry,
        # BINDING-ABI 3.3); this port has only the fast transparent-black border, so no border addressing with XR
        if any(tclasses) and (any(sp['address'] == 'clamp_to_zero' for sp in sc['samplers']) or
                              any('border' in c['state']['s_address'] + c['state']['t_address']
                                  for c in B.sidecar(fs)['constant_samplers']['samplers'])):
            raise Refused(f'{sc["name"]}: border addressing on an XR texture needs a u-domain border colour')
        if rt_at + RT_GUARD > R.ARENA_LAYOUT['r_rt'][1]:
            raise Refused('the render targets do not fit r_rt')
        out['functions'].append(f)
    out['rt_end'] = rt_at + RT_GUARD
    return out


def rt_region_bytes(p):
    """The whole used part of r_rt as uploaded: canary everywhere, each image's rows at their pitch."""
    size = p['rt_end']
    blob = bytearray(M.canary(size))
    for f in p['functions']:
        W, H = f['scene']['width'], f['scene']['height']
        for rt in f['rts']:
            row = W * rt['cpp']
            for y in range(H):
                blob[rt['image'] + y * rt['pitch']:rt['image'] + y * rt['pitch'] + row] = rt['init'][y * row:(y + 1) * row]
    return bytes(blob)


def texture_bytes(entry):
    """The texture's bytes at Mesa's pitch and layer stride (tight rows in the scene)."""
    t = entry['tex']
    cpp = TEX_HW[t['format']][2]
    row = t['width'] * cpp
    out = bytearray(entry['size'])
    for layer in range(t['layers']):
        for y in range(t['height']):
            src = (layer * t['height'] + y) * row
            dst = layer * entry['layer'] + y * entry['pitch']
            out[dst:dst + row] = t['data'][src:src + row]
    return bytes(out)


# ============================================================================================================
# The command stream (P7's build_p7_ib, generalised)
# ============================================================================================================
def output_regid(v, slot):
    return M.output_regid(v, slot)


VARYING_SLOT_LAYER, VARYING_SLOT_VIEWPORT = 22, 23                     # gl_varying_slot (shader_enums.h)
INTERP_FLAT, INTERP_ZERO = 1, 2                                         # a6xx_interp_mode


def fs_outputs(fs):
    """emit_fs_outputs<A8XX> (fd6_program.cc): per colour output i the register and the components ir3 writes with
    alias.rt instead (constants; ir3_create_alias_rt). An output counts when it has a register or aliased components;
    SP_PS_OUTPUT_CONST_CNTL/MASK enable the aliased ones (4 bits per output). Returns (regids, count, alias mask,
    mrt component mask). A8XX run 1: with the mask left 0, the aliased components came out as register garbage
    (AlphaTextureFragment and ShadowCompositeFragment rgb, ShadowHorizontalBlurFragment alpha)."""
    regs, count, alias, mrt = [], 0, 0, 0
    for i in range(8):
        o = next((o for o in fs['outputs'] if o['slot'] == M.FRAG_RESULT_DATA0 + i), None)
        r = (o['regid'] | (0x100 if o['half'] else 0)) if o else M.INVALID_REG
        a = (o.get('aliased_components') or 0) if o else 0
        regs.append(r)
        if (r & 0xFF) != M.INVALID_REG or a:
            count = i + 1
            alias |= a << (4 * i)
            mrt |= 0xF << (4 * i)
    return regs, count, alias, mrt


def program_linkage(pipe, fs_mrt_count):
    """M.program_linkage for any exported pipeline: emit_vfd_dest (with the vertex-id sideband), emit_vpc (linkage
    by name, exported), emit_vs_system_values, emit_fs_inputs, emit_fs_outputs (with the alias.rt constants, which
    P7's pair does not have). For the P7 pipeline it emits the same words as M.program_linkage
    (test_p7_equivalence)."""
    words, link, _mrt = M.program_linkage(pipe)
    regs, count, alias, mrt = fs_outputs(pipe['fs'])
    out_reg = X.REGS['SP_PS_OUTPUT_REG']
    fixed = []
    for w in words:
        off = w[0][0]
        if out_reg[0] <= off < out_reg[0] + out_reg[4] * max(out_reg[3], 1):
            continue                                              # re-emitted below from fs_outputs
        if off == X.REGS['SP_PS_OUTPUT_CONST_CNTL'][0]:
            fixed += [reg('SP_PS_OUTPUT_REG', i, regid=regs[i] & 0xFF, half_precision=bool(regs[i] & 0x100))
                      for i in range(count)]
            w = reg('SP_PS_OUTPUT_CONST_CNTL', enabled=alias != 0)
        elif off == X.REGS['SP_PS_OUTPUT_CONST_MASK'][0]:
            w = _raw('SP_PS_OUTPUT_CONST_MASK', 0, alias)
        fixed.append(w)
    words = fixed
    vs = pipe['vs']
    vtx = M.sysval_regid(vs, 'SYSTEM_VALUE_VERTEX_ID')
    inst = M.sysval_regid(vs, 'SYSTEM_VALUE_INSTANCE_ID')
    sideband = (vtx != M.INVALID_REG) + (inst != M.INVALID_REG)
    attrs = [i for i in vs['inputs'] if not i['sysval']]
    patched = []
    for w in words:
        if w[0][0] == X.REGS['PC_VS_INPUT_CNTL'][0]:
            w = reg('PC_VS_INPUT_CNTL', instr_cnt=len(attrs), sideband_cnt=sideband)
        elif w[0][0] == X.REGS['VFD_CNTL_1'][0]:
            w = reg('VFD_CNTL_1', regid4vtx=vtx, regid4inst=inst, regid4primid=M.INVALID_REG,
                    regid4viewid=M.INVALID_REG)
        patched.append(w)
    return patched, link, mrt


def interp_regs(pipe):
    """emit_interp_state<A8XX> (fd6_program.cc) without point sprites or rasterflat: for each varying the FS reads
    (compmask and bary), a layer or viewport input the VS does not write reads ZERO (Mesa: "implicitly zero"),
    otherwise FLAT; other flat inputs FLAT per component; everything else smooth (0). A framebuffer-fetching FS reads
    gl_Layer for the fetch (nir_lower_fb_read: coord (x, y, layer)); A16 run 1 programmed it FLAT from a location no
    VS output fills, and the fetch came back 0 (Mesa's gl-fbfetch programs INTERP_ZERO there). For FSs without
    such an input it emits M.interp_regs' words."""
    fs, vs = pipe['fs'], pipe['vs']
    vs_slots = {o['slot'] for o in vs['outputs']}
    vinterp = [0] * 8
    for inp in fs['inputs']:
        if not (inp['compmask'] and inp['bary']):
            continue
        loc = inp['inloc']
        if inp['slot'] in (VARYING_SLOT_LAYER, VARYING_SLOT_VIEWPORT):
            mode = INTERP_ZERO if inp['slot'] not in vs_slots and inp['compmask'] & 1 else INTERP_FLAT
            vinterp[loc // 16] |= mode << ((loc % 16) * 2)
        elif inp['flat']:
            for i in range(4):
                if inp['compmask'] & (1 << i):
                    vinterp[loc // 16] |= INTERP_FLAT << ((loc % 16) * 2)
                    loc += 1
    return [_raw('VPC_VARYING_INTERP_MODE_MODE', i, vinterp[i]) for i in range(8)] + \
           [_raw('VPC_VARYING_REPLACE_MODE_MODE', i, 0) for i in range(8)]


def program_config(pipe, sampling):
    """M.program_config (setup_config_stateobj) with sp_xs_config's BINDLESS_* bits from each variant (Mesa sets
    them from v->bindless_tex/samp/ibo/ubo; the framebuffer-fetch FS reads its attachment through a bindless T#).
    For P7's variants (no bindless) it emits P7's words."""
    words = M.program_config(pipe, sampling)
    out = []
    for w in words:
        for st, name in (('vs', 'SP_VS_CONFIG'), ('fs', 'SP_PS_CONFIG')):
            if w[0][0] == X.REGS[name][0]:
                tex, samp, uav, ubo = (bool(b) for b in pipe[st]['bindless'])
                w = [(w[0][0], w[0][1] | M.pack(X.REGS[name][2], name, bindless_tex=tex, bindless_samp=samp,
                                                 bindless_uav=uav, bindless_ubo=ubo))]
        out.append(w)
    return out


def build_ib(win, p, f, fence):
    """One function's command buffer, in the order of gallium's a8xx sysmem batch for one draw (as P7): restore,
    sysmem prep (scissors, bin size, emit_zs, emit_mrt over every attachment, emit_msaa), sysmem (cache config,
    binning regs, render control, LRZ off -- no clear: every target starts from its uploaded contents), the draw
    (non-group registers, the program / linkage / interp / fb-rast / LRZ / vertex / const / texture / rasterizer /
    ZSA / blend / scissor / blend-colour / sample-location / bindless groups inline, CP_DRAW_INDX_OFFSET), the
    sysmem fini flushes and the batch fence."""
    sc, pipe = f['scene'], f['pipe']
    vs, fs = pipe['vs'], pipe['fs']
    W, H = sc['width'], sc['height']
    cs = M.CS(win, f'render {f["name"]}')
    misc = win.addr('r_misc', 0, 0x1000, False, 'border colour table (zeroed)')
    M.emit_restore(cs, misc)
    rts = []
    for rt in f['rts']:
        va = win.addr('r_rt', rt['image'], rt['pitch'] * H, True, f'render target {rt["index"]}')
        rts.append(dict(rt, va=va))
    # fd6_emit_sysmem_prep<A8XX>
    cs.event('LRZ_CACHE_FLUSH')
    cs.marker('RM6_DIRECT_RENDER')
    cs.pkt4(reg('GRAS_SC_WINDOW_SCISSOR_TL', x=0, y=0), reg('GRAS_SC_WINDOW_SCISSOR_BR', x=W - 1, y=H - 1))
    cs.pkt4(reg('GRAS_A2D_SCISSOR_TL', x=0, y=0), reg('GRAS_A2D_SCISSOR_BR', x=W - 1, y=H - 1))
    cs.pkt4(reg('RB_RESOLVE_CNTL_1', x=0, y=0), reg('RB_RESOLVE_CNTL_2', x=W - 1, y=H - 1))
    cs.pkt4(reg('GRAS_MODE_CNTL', value=0x2))
    bin_common = dict(render_mode='RENDERING_PASS', lrz_feedback_zmode_mask='LRZ_FEEDBACK_EARLY_Z_LATE_Z')
    cs.crb(reg('RB_WINDOW_OFFSET', x=0, y=0), reg('RB_RESOLVE_WINDOW_OFFSET', x=0, y=0),
           reg('SP_WINDOW_OFFSET', x=0, y=0), reg('TPL1_WINDOW_OFFSET', x=0, y=0),
           reg('TPL1_A2D_WINDOW_OFFSET', x=0, y=0),
           reg('GRAS_SC_BIN_CNTL', binw=0, binh=0, force_bi_dir_lrz_disable=True, **bin_common),
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
    mrt_regs = []
    for rt in rts:                                                                                 # emit_mrt
        hw, swap, _cpp, _tex = RT_HW[rt['format']]
        i = rt['index']
        mrt_regs += [reg('RB_MRT_BUF_INFO', i, color_format=hw, color_tile_mode='TILE6_LINEAR', color_swap=swap),
                     reg('RB_MRT_PITCH', i, value=rt['pitch']), reg('RB_MRT_ARRAY_PITCH', i, value=rt['pitch'] * H),
                     reg('RB_MRT_BASE', i, value=rt['va']), reg('RB_MRT_BASE_GMEM', i, value=0),
                     reg('SP_PS_MRT_REG', i, color_format=hw),
                     reg('RB_COLOR_FLAG_BUFFER_ADDR', i, value=rt['va']),
                     reg('RB_COLOR_FLAG_BUFFER_PITCH', i, pitch=0, array_pitch=0)]
    mrt0 = RT_HW[rts[0]['format']][0] if rts and rts[0]['index'] == 0 else 'FMT6_NONE'
    cs.crb(reg('RB_DEPTH_BUFFER_INFO', depth_format='DEPTH6_NONE', tilemode='TILE6_LINEAR'),       # emit_zs (none)
           reg('RB_DEPTH_BUFFER_PITCH', value=0), reg('RB_DEPTH_BUFFER_ARRAY_PITCH', value=0),
           reg('RB_DEPTH_BUFFER_BASE', value=0), reg('RB_DEPTH_GMEM_BASE', value=0),
           reg('GRAS_SU_DEPTH_BUFFER_INFO', depth_format='DEPTH6_NONE'),
           reg('RB_STENCIL_BUFFER_INFO', tilemode='TILE6_LINEAR'),
           *mrt_regs,
           reg('GRAS_LRZ_MRT_BUFFER_INFO_0', color_format=mrt0), reg('RB_SRGB_CNTL', dword=0),
           reg('SP_SRGB_CNTL', dword=0), reg('GRAS_CL_ARRAY_SIZE', value=0),
           reg('TPL1_RAS_MSAA_CNTL', samples='MSAA_ONE'),                                                 # emit_msaa
           reg('TPL1_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('GRAS_SC_RAS_MSAA_CNTL', samples='MSAA_ONE'),
           reg('GRAS_SC_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('RB_RAS_MSAA_CNTL', samples='MSAA_ONE'), reg('RB_DEST_MSAA_CNTL', samples='MSAA_ONE', msaa_disable=True),
           reg('RB_RESOLVE_GMEM_BUFFER_INFO', samples='MSAA_ONE'))
    # fd6_emit_sysmem<A8XX> (no fast clear)
    M.emit_gmem_cache_cntl_sysmem(cs)
    M.emit_static_binning_regs(cs)
    cs.crb(reg('RB_RENDER_CNTL', raster_mode='TYPE_TILED', raster_direction='LR_TB'), reg('GRAS_SU_RENDER_CNTL'))
    M.emit_lrz_off(cs)
    # --- the draw ---
    cs.crb(reg('VFD_INDEX_OFFSET', value=0), reg('VFD_INSTANCE_START_OFFSET', value=0),
           reg('PC_RESTART_INDEX', value=0xFFFFFFFF))
    xs, xo, ys, yo, zs, zo = W / 2, W / 2, -H / 2, H / 2, 1.0, 0.0                # Metal viewport (BINDING-ABI 4.3)
    cs.crb(reg('RB_STENCIL_REF_CNTL', ref=0, bfref=0),
           reg('GRAS_CL_VIEWPORT_XOFFSET', 0, value=xo), reg('GRAS_CL_VIEWPORT_XSCALE', 0, value=xs),
           reg('GRAS_CL_VIEWPORT_YOFFSET', 0, value=yo), reg('GRAS_CL_VIEWPORT_YSCALE', 0, value=ys),
           reg('GRAS_CL_VIEWPORT_ZOFFSET', 0, value=zo), reg('GRAS_CL_VIEWPORT_ZSCALE', 0, value=zs),
           reg('GRAS_SC_VIEWPORT_SCISSOR_TL', 0, x=0, y=0), reg('GRAS_SC_VIEWPORT_SCISSOR_BR', 0, x=W - 1, y=H - 1),
           reg('GRAS_CL_GUARDBAND_CLIP_ADJ', horz=M.guardband(xo, xs), vert=M.guardband(yo, ys)))
    vs_va = win.addr('r_code', f['code']['vs'], vs['info']['size'], False, 'VS code')
    fs_va = win.addr('r_code', f['code']['fs'], fs['info']['size'], False, 'FS code')
    sampling = {'ntex': f['ntex'], 'nsamp': f['nsamp'], 'pixlod': bool(fs['need_pixlod'])}
    link_regs, link, mrt = program_linkage(pipe, len(rts))
    cs.crb(*program_config(pipe, sampling))                                                         # PROG_CONFIG
    cs.crb(*M.shader_regs(vs, 'vs', vs_va, sampling))                                               # PROG
    cs.crb(*M.shader_regs(fs, 'fs', fs_va, sampling))
    cs.crb(*link_regs)
    cs.crb(*interp_regs(pipe))                                                                      # PROG_INTERP
    nr = len(rts)
    comps = sum(0xF << (4 * rt['index']) for rt in rts) & mrt                                      # PROG_FB_RAST
    cs.crb(reg('RB_PS_OUTPUT_CNTL'), reg('RB_PS_MRT_CNTL', mrt=nr), reg('SP_PS_MRT_CNTL', mrt=nr),
           _raw('SP_PS_OUTPUT_MASK', 0, comps), _raw('RB_PS_OUTPUT_MASK', 0, comps))
    cs.crb(reg('GRAS_LRZ_CNTL'), reg('RB_LRZ_CNTL'), reg('RB_DEPTH_PLANE_CNTL', z_mode='A6XX_LATE_Z'),   # LRZ
           reg('GRAS_SU_DEPTH_PLANE_CNTL', z_mode='A6XX_LATE_Z'))
    vtx = sc['vertex']                                                                              # VTXSTATE, VBO
    attr_by_index = {a['index']: a for a in vtx['attributes']}
    fetch = []
    for i, a in enumerate(B.vertex_attributes(vs)):
        if a not in attr_by_index:
            raise Refused(f'{f["name"]}: the VS reads attribute {a}, the vertex descriptor has none')
        at = attr_by_index[a]
        fetch.append(reg('VFD_FETCH_INSTR_INSTR', i, idx=0, offset=at['offset'], format=VTX_HW[at['format']],
                         swap='WZYX', unk30=True, float=True) + reg('VFD_FETCH_INSTR_STEP_RATE', i, value=1))
    if fetch:
        cs.crb(*fetch, reg('VFD_VERTEX_BUFFER_STRIDE', 0, value=vtx['stride']))
        vbo = win.addr('r_data', f['vbo'], len(vtx['data']), False, 'vertex buffer')
        cs.crb(reg('VFD_VERTEX_BUFFER_BASE', 0, value=vbo), reg('VFD_VERTEX_BUFFER_SIZE', 0, value=len(vtx['data'])))
    for st, geom, block in (('vs', True, 'SB6_VS_SHADER'), ('fs', False, 'SB6_FS_SHADER')):          # CONST (UBOs)
        count, table = f['ubo'][st]
        words = []
        for slot in range(count):
            what = table.get(slot)
            if what is None:
                words += [0, 0]
            elif what[0] == 'buffer':
                b = f['buffers'][st][what[1]]
                words += M.ubo_desc(win.addr('r_data', b['offset'], b['size'], False,
                                             f'{st} UBO {slot} = buffer({what[1]})'), b['size'])
            elif what[0] == 'driver_table':
                words += M.ubo_desc(win.addr('r_data', f['driver_table'][st], B.DRIVER_TABLE_BYTES, False,
                                             f'{st} UBO {slot} (driver table)'), B.DRIVER_TABLE_BYTES)
            else:                                                                                   # constant data
                code = f['code'][st]
                words += M.ubo_desc(win.addr('r_code', code + what[1], what[2], False, f'{st} constant data'), what[2])
        op, pw = M.load_state6(geom, 'ST6_UBO', 'SS6_DIRECT', block, count, payload=words)
        cs.pkt7(op, *pw)
    if f['tex_desc'] is not None or f['samp_desc'] is not None:                                     # FS_TEX
        samp = win.addr('r_data', f['samp_desc'], 16 * f['nsamp'], False, 'S# array') \
            if f['samp_desc'] is not None else 0
        texd = win.addr('r_data', f['tex_desc'], 64 * f['ntex'], False, 'T# array') \
            if f['tex_desc'] is not None else 0
        cs.crb(reg('SP_PS_SAMPLER_BASE', value=samp), reg('SP_PS_TEXMEMOBJ_BASE', value=texd),
               reg('SP_PS_TSIZE', value=f['ntex'] if f['tex_desc'] is not None else 0))              # (loaded = NTEX)
        if f['samp_desc'] is not None:
            op, pw = M.load_state6(False, 'ST6_SHADER', 'SS6_INDIRECT', 'SB6_FS_TEX', f['nsamp'], ext=samp)
            cs.pkt7(op, *pw)
        if f['tex_desc'] is not None:
            op, pw = M.load_state6(False, 'ST6_CONSTANTS', 'SS6_INDIRECT', 'SB6_FS_TEX', f['ntex'], ext=texd)
            cs.pkt7(op, *pw)
    else:
        cs.crb(reg('SP_PS_SAMPLER_BASE', value=0), reg('SP_PS_TEXMEMOBJ_BASE', value=0), reg('SP_PS_TSIZE', value=0))
    zclamp = []
    for i in range(16):                                                                             # RASTERIZER
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
    cs.crb(reg('RB_ALPHA_TEST_CNTL', alpha_test_func='FUNC_NEVER'), reg('SP_ALPHA_TEST_CNTL'),       # ZSA
           reg('RB_STENCIL_CNTL'), reg('GRAS_SU_STENCIL_CNTL'), reg('RB_STENCIL_MASK'), reg('RB_STENCIL_WRITE_MASK'),
           reg('RB_DEPTH_CNTL', zfunc='FUNC_NEVER', z_clamp_enable=True, o_depth_01_clamp_en=True),
           reg('GRAS_SU_DEPTH_CNTL'), reg('RB_DEPTH_BOUND_MIN', value=0.0), reg('RB_DEPTH_BOUND_MAX', value=1.0))
    blend_regs, enable = [], 0                                                                      # BLEND
    lowering = B.rt_lowering(fs)
    for i in range(max((rt['index'] for rt in rts), default=0) + 1):
        b, mask = effective_blend(sc, lowering, i)
        on = bool(b.get('enabled'))
        if on:
            enable |= 1 << i
            fac = dict(rgb_src_factor=BLEND_FACTOR[b['src_rgb']], rgb_blend_opcode=BLEND_OP[b['rgb_op']],
                       rgb_dest_factor=BLEND_FACTOR[b['dst_rgb']], alpha_src_factor=BLEND_FACTOR[b['src_alpha']],
                       alpha_blend_opcode=BLEND_OP[b['alpha_op']], alpha_dest_factor=BLEND_FACTOR[b['dst_alpha']])
        else:
            fac = dict(rgb_src_factor='FACTOR_ZERO', rgb_blend_opcode='BLEND_DST_PLUS_SRC', rgb_dest_factor='FACTOR_ZERO',
                       alpha_src_factor='FACTOR_ZERO', alpha_blend_opcode='BLEND_DST_PLUS_SRC',
                       alpha_dest_factor='FACTOR_ZERO')
        blend_regs += [reg('RB_MRT_BLEND_CONTROL', i, **fac),
                       reg('RB_MRT_CONTROL', i, color_blend_en=on, alpha_blend_en=on, rop_code='ROP_COPY',
                           component_enable=mask),
                       reg('SP_MRT_BLEND_CNTL_REG', i, color_blend_en=on, alpha_blend_en=on, component_write_mask=mask)]
    # Metal's blend state is per attachment: independent whenever there is more than one (as gallium sets it
    # for the same two-target framebuffer, tools/testdata/a16gpu-render/gl-mrt.rd)
    indep = len(rts) > 1
    cs.crb(*blend_regs, reg('RB_DITHER_CNTL'), reg('SP_BLEND_CNTL', enable_blend=enable, independent_blend_en=indep),
           reg('RB_BLEND_CNTL', blend_reads_dest=enable, independent_blend=indep, sample_mask=0xFFFF))
    cs.crb(reg('GRAS_SC_SCREEN_SCISSOR_TL', 0, x=0, y=0), reg('GRAS_SC_SCREEN_SCISSOR_BR', 0, x=W - 1, y=H - 1))
    cs.crb(reg('RB_BLEND_CONSTANT_RED_FP32', value=0.0), reg('RB_BLEND_CONSTANT_GREEN_FP32', value=0.0),
           reg('RB_BLEND_CONSTANT_BLUE_FP32', value=0.0), reg('RB_BLEND_CONSTANT_ALPHA_FP32', value=0.0))
    cs.crb(reg('GRAS_SC_MSAA_SAMPLE_POS_CNTL'), reg('RB_MSAA_SAMPLE_POS_CNTL'))
    # groups BINDLESS 0..4 (fd6_build_bindless_state): the FS set (4) holds the framebuffer-fetch descriptors when
    # the FS reads its attachments (BINDING-ABI 3.6), otherwise every set points at the zeroed page.
    empty_set = win.addr('r_misc', 0, 0x1000, False, 'bindless descriptor sets (empty)')
    for i in range(5):
        addr = empty_set
        if i == B.FS_BINDLESS_SET and f['bindless'] is not None:
            addr = win.addr('r_data', f['bindless'], 64 * B.BINDLESS_DESC_COUNT, False, 'FS bindless set 4 (fb fetch)')
        cs.crb(reg('SP_UPDATE_CNTL'), reg('SP_GFX_BINDLESS_BASE_DESCRIPTOR', i, desc_size='BINDLESS_DESCRIPTOR_64B',
                                          addr=addr))
    # build_prim_mode (fd6_emit.cc): a FS that fetches its attachment in sysmem flushes per overlapping primitive
    prim_mode = 'FLUSH_PER_OVERLAP_AND_OVERWRITE' if f['fb_fetch'] else 'NO_FLUSH'
    cs.crb(reg('GRAS_SC_CNTL', single_prim_mode=prim_mode))                                          # PRIM_MODE_SYSMEM
    cs.pkt7(R.CP_DRAW_INDX_OFFSET,
            pkt_word('CP_DRAW_INDX_OFFSET_0', prim_type='DI_PT_TRILIST', source_select='DI_SRC_SEL_AUTO_INDEX',
                     vis_cull='USE_VISIBILITY', index_size='INDEX4_SIZE_8_BIT', patch_type='TESS_QUADS'),
            pkt_word('CP_DRAW_INDX_OFFSET_1', num_instances=1),
            pkt_word('CP_DRAW_INDX_OFFSET_2', num_indices=vtx['count']))
    M.emit_flushes(cs, ccu_color=True, inv_ccu_color=True, ccu_depth=True, inv_ccu_depth=True, cache=True,
                   inv_cche=True, wfi=True)
    cs.event('CACHE_CLEAN')
    cs.pkt7(R.CP_SKIP_IB2_ENABLE_GLOBAL, 0)
    cs.event('LRZ_CACHE_FLUSH')
    M.emit_ib_fence(cs, win.addr('memptrs', 0, 0x100, True, 'batch fence (memptrs)'), fence)
    cs.fence = fence
    cs.link = link
    return cs


def effective_blend(sc, lowering, i):
    """The blend state and write mask to program for attachment i: the scene's (Metal's) for a native attachment;
    for a lowered one the variant's `hw_blend` (off when the shader blends) and `hw_write_mask_clear`
    (BINDING-ABI 4.5)."""
    lw = lowering.get(i)
    if lw is None:
        return (sc['blend'].get(i) or sc['blend'].get(str(i)) or {}), 0xF
    hb = lw['hw_blend']
    b = {'enabled': bool(hb['enabled'])}
    if b['enabled']:
        b.update(rgb_op=MTL_BLEND_OP[hb['rgb_op']], alpha_op=MTL_BLEND_OP[hb['alpha_op']],
                 src_rgb=MTL_BLEND_FACTOR[hb['src_rgb']], dst_rgb=MTL_BLEND_FACTOR[hb['dst_rgb']],
                 src_alpha=MTL_BLEND_FACTOR[hb['src_alpha']], dst_alpha=MTL_BLEND_FACTOR[hb['dst_alpha']])
    mask = 0xF & ~(0x8 if 'A' in lw.get('hw_write_mask_clear', '') else 0)
    return b, mask


def data_uploads(win, p, f):
    """[(region, offset, bytes, what)] for one function's code and data slot (addresses from the window)."""
    sc, pipe = f['scene'], f['pipe']
    ups = []
    for st in ('vs', 'fs'):
        ups.append(('r_code', f['code'][st], bytes.fromhex(pipe[st]['code_hex']), f'{f["name"]} {st.upper()} code'))
    if f['vbo'] is not None:
        ups.append(('r_data', f['vbo'], sc['vertex']['data'], f'{f["name"]} vertex buffer'))
    for st in ('vs', 'fs'):
        for idx, b in f['buffers'][st].items():
            ups.append(('r_data', b['offset'], b['data'], f'{f["name"]} {st} buffer({idx})'))
        if st in f.get('driver_table', {}):
            addr = lambda n, st=st: win.addr('r_data', f['buffers'][st][n]['offset'], f['buffers'][st][n]['size'],
                                             False, f'{st} device buffer({n})')
            ups.append(('r_data', f['driver_table'][st], B.driver_table(pipe[st], addr), f'{f["name"]} {st} driver table'))
    fs = pipe['fs']
    if f['tex_desc'] is not None:
        t_slots = B.texture_slots(fs)
        words = []
        for slot in range(f['ntex']):
            if slot in t_slots:
                if t_slots[slot] not in f['tex']:
                    raise Refused(f'{f["name"]}: texture({t_slots[slot]}) has no scene texture')
                e = f['tex'][t_slots[slot]]
                va = win.addr('r_tex', e['offset'], e['size'], False, f'texture({t_slots[slot]}) {e["tex"]["name"]}')
                words += texture_descriptor(va, e['tex'])
            else:
                words += [0] * 16
        ups.append(('r_data', f['tex_desc'], struct.pack(f'<{len(words)}I', *words), f'{f["name"]} T# array'))
    if f['samp_desc'] is not None:
        api, const = B.sampler_slots(fs)
        samplers = {s['index']: s for s in sc['samplers']}
        words = []
        for slot in range(f['nsamp']):
            if slot in const:
                words += const[slot]
            elif slot in api:
                if api[slot] not in samplers:
                    raise Refused(f'{f["name"]}: sampler({api[slot]}) has no scene sampler')
                words += api_sampler_descriptor(samplers[api[slot]])
            elif B.sidecar(fs).get('abi_version', 0) >= 5:
                words += api_sampler_descriptor(B.DEFAULT_SAMPLER)      # ABI v5 3.4: a valid descriptor in a hole
            else:
                words += [0] * 4
        ups.append(('r_data', f['samp_desc'], struct.pack(f'<{len(words)}I', *words), f'{f["name"]} S# array'))
    if f['bindless'] is not None:
        blob = bytearray(64 * B.BINDLESS_DESC_COUNT)
        by_index = {rt['index']: rt for rt in f['rts']}
        for n, slot in f['fb_fetch']:
            rt = by_index.get(n)
            if rt is None:
                raise Refused(f'{f["name"]}: framebuffer fetch of colour {n}, which the scene does not attach')
            va = win.addr('r_rt', rt['image'], rt['pitch'] * sc['height'], False, f'fb fetch view of colour {n}')
            d = rt_view_descriptor(va, rt['format'], sc['width'], sc['height'], rt['pitch'])
            blob[64 * slot:64 * slot + 64] = struct.pack('<16I', *d)
        ups.append(('r_data', f['bindless'], bytes(blob), f'{f["name"]} FS bindless set 4'))
    return ups


# ============================================================================================================
# Compare
# ============================================================================================================
def edge_pixels(width, height, points):
    """Pixels whose centre lies within one pixel of any triangle edge of the scene (pixel-space vertices)."""
    tris = [points[i:i + 3] for i in range(0, len(points) - 2, 3)]
    out = set()
    for tri in tris:
        for (ax, ay), (bx, by) in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            dx, dy = bx - ax, by - ay
            ln = dx * dx + dy * dy
            x0, x1 = int(max(0, min(ax, bx) - 2)), int(min(width - 1, max(ax, bx) + 2))
            y0, y1 = int(max(0, min(ay, by) - 2)), int(min(height - 1, max(ay, by) + 2))
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    cx, cy = x + 0.5, y + 0.5
                    t = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / ln)) if ln else 0.0
                    if math.hypot(ax + t * dx - cx, ay + t * dy - cy) < 1.0:
                        out.add((x, y))
    return out


def channels(data, fmt, i):
    """Pixel i's channel values in the unit the tolerance is in: bytes for 8-bit formats, XR codes (0..1023) for
    the XR formats (554: B, G, R in one word, alpha bits ignored; 552: B, G, R, A words >> 6)."""
    if fmt == 'bgr10_xr':
        w = struct.unpack_from('<I', data, 4 * i)[0]
        return (w & 0x3FF, w >> 10 & 0x3FF, w >> 20 & 0x3FF)
    if fmt == 'bgra10_xr':
        return tuple(x >> 6 for x in struct.unpack_from('<4H', data, 8 * i))
    cpp = RT_HW[fmt][2]
    return tuple(data[cpp * i:cpp * i + cpp])


def compare(got, golden, width, height, fmt, tol, edge_budget, edges):
    cpp = RT_HW[fmt][2]
    over, over_edge, worst, total = [], 0, 0, 0
    for i in range(width * height):
        d = max(abs(a - b) for a, b in zip(channels(got, fmt, i), channels(golden, fmt, i)))
        worst = max(worst, d)
        total += d
        if d > tol:
            x, y = i % width, i // width
            if (x, y) in edges:
                over_edge += 1
            else:
                over.append({'x': x, 'y': y, 'got': got[cpp * i:cpp * i + cpp].hex(),
                             'golden': golden[cpp * i:cpp * i + cpp].hex()})
    return {'ok': not over and over_edge <= edge_budget, 'max_abs_diff': worst,
            'mean_abs_diff': round(total / (width * height), 4), 'pixels_over_tolerance_inside': len(over),
            'pixels_over_tolerance_on_edges': over_edge, 'edge_budget': edge_budget, 'tolerance': tol,
            'first_inside': over[:8], 'identical': got == golden}


# ============================================================================================================
# The command
# ============================================================================================================
def render_submit(s, out, host, win, cs, slot_offset, what):
    """P7's p67_submit for the render window: IB into its slot (read back), the tables read back as R1 installed
    them, every reference walked (the IB's and those inside its descriptors), the a7xx_submit ring words, the
    bounded fence poll, idle, faults."""
    words = cs.blob()
    if len(words) > IB_SLOT:
        raise Refused(f'{what}: {len(words)} bytes of commands do not fit an IB slot')
    ib_va = win.addr('r_ib', slot_offset, len(words), False, f'IB {what}')
    fence_va = win.addr('memptrs', R.CP_FENCE_OFF, 4, True, 'ring fence')
    bv_va = win.addr('memptrs', R.CP_BV_FENCE_OFF, 4, True, 'BV fence')
    image, table_off = tables_as_installed(s, what)
    refs = verify_refs(s, win, image, table_off)
    at = R.ARENA_LAYOUT['r_ib'][0] + slot_offset
    s.mem_write(at, words, f'IB {what}')
    if s.mem_read(at, len(words), f'IB {what} (verify)') != words:
        raise Stop(s.step, f'IB {what} did not read back')
    value = cs.fence
    ring = M.ring_words(ib_va, cs.dwords(), fence_va, bv_va, value)
    wptr = S3.ring_submit(s, host, ring, f'{what} (IB {cs.dwords()} dwords, fence {value:#x})')
    S3.write_host(s, host)
    record = {'what': what, 'ib': {'va': hx(ib_va), 'dwords': cs.dwords(), 'sha256': hashlib.sha256(words).hexdigest()},
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
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'{what}: the GPU did not write the fence (read {seen:#x}, want {value:#x}): '
                           '`quiesce --write`', submission=record)
    bv_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_BV_FENCE_OFF, 4, 'BV fence'))[0]
    ib_seen = struct.unpack('<I', s.mem_read(memptrs + R.CP_IB_FENCE_OFF, 4, 'IB fence'))[0]
    record['bv_fence'], record['ib_fence'] = hx(bv_seen), hx(ib_seen)
    record['idle'] = S3.cp_idle(s, wptr)
    host['fence_value'] = value
    S3.write_host(s, host)
    fault, report = S3.cb_faults(s, R.GPU_CB)
    int0, irq, int0_bad, irq_bad = M.interrupt_faults(s)
    record['cp_interrupt_status_global'] = hx(irq)
    record['rbbm_int_0_status'] = hx(int0)
    if fault or irq_bad or int0_bad:
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'{what}: fence written but CB0 FSR {report["fsr"]} / CP_INTERRUPT_STATUS_GLOBAL {irq:#x} '
                           f'/ RBBM_INT_0_STATUS {int0:#x} report a fault', submission=record)
    if bv_seen != value or ib_seen != value:
        record['diagnostics'] = M.submission_diagnostics(s)
        raise Stop(s.step, f'{what}: ring fence {value:#x} written but the IB fence reads {ib_seen:#x} and the BV '
                           f'fence {bv_seen:#x}: the IB did not run to its end', submission=record)
    return record


def probe_bands(sc, image):
    """The UBO probes: what each band read -- its own UBO's value, another UBO's (the index wrapped), zero (the
    descriptor ignored) or something else -- from the pixel at the band's centre, row 32 (BGRA8)."""
    b = sc['bands']
    colours = {tuple(SC.probe_colour(k)[i] for i in (2, 1, 0, 3)): k for k in range(31)}
    out = []
    for n in range(b['count']):
        x = n * b['width'] + b['width'] // 2
        px = tuple(image[4 * (32 * sc['width'] + x):4 * (32 * sc['width'] + x) + 4])
        k = colours.get(px)
        want = b['first_ubo'] + n
        read = 'own value' if k == want else f'UBO {k} value' if k is not None else \
            'zero' if px[:3] == (0, 0, 0) else 'other'
        out.append({'ubo': want, 'bgra': bytes(px).hex(), 'reads': read})
    return out


def function_summary(f):
    pipe = f['pipe']
    c = pipe.get('compile', {})
    return {'vs': pipe['vs_function'], 'fs': f['name'], 'lib': pipe['lib'],
            'variant_ids': {st: c.get(st, {}).get('variant_id') for st in ('vs', 'fs')},
            'cache_keys': {st: c.get(st, {}).get('key') for st in ('vs', 'fs')},
            'cache_hit': {st: c.get(st, {}).get('cache', {}).get('hit') for st in ('vs', 'fs')},
            'constlen': {'vs': pipe['vs']['constlen'], 'fs': pipe['fs']['constlen']},
            'instrlen': {'vs': pipe['vs']['instrlen'], 'fs': pipe['fs']['instrlen']},
            'ntex_nsamp': [f['ntex'], f['nsamp']], 'fb_fetch': bool(f['fb_fetch']),
            'rts': [(rt['index'], rt['format']) for rt in f['rts']],
            'blend': bool(f['scene']['blend']), 'tolerance': f['scene']['tolerance'],
            'edge_budget': f['scene']['edge_budget'], 'realistic': f['scene']['realistic']}


def cmd_render(s, out, args):
    scenes = select(getattr(args, 'functions', None), getattr(args, 'xr', False), getattr(args, 'probes', False))
    host = M.p67_prologue(s, out, 'R0', len(scenes))
    shaders, goldens, pins = load_assets()
    out['render'] = dict(pins, functions={})
    problems = []
    for sc in scenes:
        pipe = shaders['pipelines'].get(sc['name'])
        if pipe is None:
            problems.append(f'{sc["name"]}: not in render-shaders.json')
            continue
        digest = SC.scene_digest(sc)
        if pipe.get('scene_digest') != digest:
            problems.append(f'{sc["name"]}: the scene changed since its variants were exported')
        g = goldens.get(sc['name'])
        if g is None or g['digest'] != digest:
            problems.append(f'{sc["name"]}: no golden for this scene (render_metal_golden.swift)')
        elif sorted(g['images']) != sorted(r['index'] for r in sc['rts']):
            problems.append(f'{sc["name"]}: the golden has attachments {sorted(g["images"])}')
        for st in ('vs', 'fs'):
            problems += [f'{sc["name"]}: {p}' for p in B.check_variant(pipe[st], st, probe=sc['group'] == 'probe')]
        problems += [f'{sc["name"]}: {p}' for p in B.check_pipeline(pipe)]
    if problems:
        raise Stop('R0', 'outside what this tool builds: ' + '; '.join(problems[:12]), problems=problems)
    try:
        p = plan(scenes, shaders)
    except (Refused, B.ABIError) as problem:
        raise Stop('R0', f'layout: {problem}')
    # review: build every command stream and descriptor set now (pure: a scratch window, nothing written), so
    # nothing the builder refuses can surface after R1 or between draws
    for f in p['functions']:
        dry = Window(s.arena[0])
        try:
            data_uploads(dry, p, f)
            cs = build_ib(dry, p, f, 0)
        except (Refused, B.ABIError, KeyError) as problem:
            raise Stop('R0', f'{f["name"]}: command stream: {problem!r}')
        if len(cs.blob()) > IB_SLOT:
            raise Stop('R0', f'{f["name"]}: {cs.dwords()} dwords of commands do not fit an IB slot')
    for f in p['functions']:
        out['render']['functions'][f['name']] = function_summary(f)
    out['render']['order'] = [f['name'] for f in p['functions']]
    if args.stop_after == 'R0':
        out['stopped_after'] = 'R0'
        return
    # R1: the window, then every upload (code, data, textures, targets with canaries), each read back
    s.step = 'R1'
    win = Window(s.arena[0])
    install_tables(s, out, 'render window')
    host['p67_token'] = host['boot_token']
    S3.write_host(s, host)
    s.mem_fill(*R.ARENA_LAYOUT['r_misc'], 'misc page (border colours, empty descriptor sets)')
    for name in ('r_code', 'r_data'):
        s.mem_fill(*R.ARENA_LAYOUT[name], f'{name} region')
    for key, e in p['textures'].items():
        M.upload(s, 'r_tex', e['offset'], texture_bytes(e), f'texture {e["tex"]["name"]} {e["tex"]["width"]}x'
                                                            f'{e["tex"]["height"]}x{e["tex"]["layers"]}')
    for f in p['functions']:
        # addresses the GPU finds inside descriptors (T# bases, the driver table's device-buffer VAs, the
        # framebuffer-fetch view) are walked through CB0 with that function's IB references before its submission
        mark = len(win.refs)
        for region, offset, data, what in data_uploads(win, p, f):
            M.upload(s, region, offset, data, what)
        f['indirect_refs'] = win.refs[mark:]
    targets = rt_region_bytes(p)
    M.upload(s, 'r_rt', 0, targets, 'render targets: initial contents and canaries')
    win.refs.clear()
    out['order'].append('render_uploaded')
    if args.stop_after == 'R1':
        out['stopped_after'] = 'R1'
        return
    # R2: one submission per function, each read back and compared before the next
    s.step = 'R2'
    results = out['render']['results'] = {}
    for f in p['functions']:
        sc = f['scene']
        win.refs[:] = f['indirect_refs']
        cs = build_ib(win, p, f, M.next_fence(host))
        record = render_submit(s, out, host, win, cs, f['ib'], f['name'])
        out['render']['functions'][f['name']]['draw_dwords'] = cs.dwords()
        W, H = sc['width'], sc['height']
        edges = edge_pixels(W, H, sc['vertex']['triangles_px'])
        res = {'fence': record['fence'], 'ib_dwords': cs.dwords(), 'rts': []}
        for rt in f['rts']:
            start = rt['slot']
            blob = s.mem_read(R.ARENA_LAYOUT['r_rt'][0] + start, rt['size'] + RT_GUARD,
                              f'{f["name"]} render target {rt["index"]} (readback)')
            want = targets[start:start + rt['size'] + RT_GUARD]
            img_off = rt['image'] - start
            row = W * rt['cpp']
            image = b''.join(blob[img_off + y * rt['pitch']:img_off + y * rt['pitch'] + row] for y in range(H))
            outside = bytearray(blob)
            for y in range(H):
                outside[img_off + y * rt['pitch']:img_off + y * rt['pitch'] + row] = \
                    want[img_off + y * rt['pitch']:img_off + y * rt['pitch'] + row]
            guards_ok = bytes(outside) == want
            cmp = compare(image, goldens[f['name']]['images'][rt['index']], W, H, rt['format'], sc['tolerance'],
                          sc['edge_budget'], edges)
            cmp.update(index=rt['index'], format=rt['format'], guards_intact=guards_ok,
                       sha256=hashlib.sha256(image).hexdigest(),
                       changed_by_draw=image != rt['init'])
            if sc.get('bands'):
                cmp['bands'] = probe_bands(sc, image)
            cmp['ok'] = cmp['ok'] and guards_ok
            res['rts'].append(cmp)
            M.save_image(args, f'{f["name"]}.rt{rt["index"]}.raw', image, out, f'{f["name"]}.rt{rt["index"]}')
            if not guards_ok:
                # The GPU wrote outside a target (inside r_rt: nothing else is writable): stop, it is not a result.
                results[f['name']] = res
                raise Stop('R2', f'{f["name"]}: the render target\'s guard bands or row padding were written',
                           result=res)
        res['ok'] = all(r['ok'] for r in res['rts'])
        results[f['name']] = res
        out['order'].append(f'drawn:{f["name"]}')
    # R3: the verdict over every function drawn
    s.step = 'R3'
    failed = [n for n, r in results.items() if not r['ok']]
    gate_drawn = sorted(n for n in results if SC_GROUP.get(n) == 'gate') == GATE
    out['render']['verdict'] = {'drawn': len(results), 'within_tolerance': len(results) - len(failed), 'failed': failed,
                                'gate': gate_drawn,
                                # review: the Stage 6 gate itself: all 28 drawn and every one within tolerance
                                # (the XR scenes and a --functions subset never decide it)
                                'gate_met': gate_drawn and not any(SC_GROUP.get(n) == 'gate' for n in failed)}
    if failed:
        raise Stop('R3', f'{len(failed)} of {len(results)} functions differ from their M3 Max goldens beyond '
                         f'tolerance: {failed}', verdict=out['render']['verdict'])
    out['order'].append('render_matches_goldens')


SC_GROUP = {s['name']: s['group'] for s in SC.all_scenes()}
GATE = sorted(n for n, g in SC_GROUP.items() if g == 'gate')

COMMANDS = {'render': (cmd_render, True)}
STEP_IDS = {'render': ('R0', 'R1')}
S3.COMMANDS.update(COMMANDS)
S3.STEP_IDS.update(STEP_IDS)


def add_parsers(sub):
    r = sub.add_parser('render', help='Stage 6: the 28 wscts pipelines drawn on the A16, compared with the M3 Max')
    r.add_argument('--write', action='store_true', help='required: this submits GPU work and writes the arena')
    r.add_argument('--stop-after', choices=STEP_IDS['render'])
    r.add_argument('--functions', help='comma-separated scene names (default: the gate\'s 28, in scene order)')
    r.add_argument('--xr', action='store_true', help='also draw the XR lowering scenes (after the 28)')
    r.add_argument('--probes', action='store_true', help='draw only the single-feature A16 probes (never the gate)')


if __name__ == '__main__':
    import sys
    sys.exit('the subcommand runs from tools/a16gpu.py (render --write); scenes: tools/a16gpu_render_scenes.py')
