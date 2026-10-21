#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for Stage 6's `render` (tools/a16gpu_render.py): the 28 wscts pipelines.

FakeRender extends test-a16gpu-p6p7.py's FakeP67 (the SMMU walking CB0 in model DRAM, the write-back CPU cache, the
GMU, the CP executing the ring and IB packets, a CCU that reaches DRAM only on the clean events) with a draw that
checks every piece of program and binding state the hardware needs against the exported variants and the scene
*as the GPU would see it*: it identifies the pipeline by the code at SP_VS_BASE / SP_PS_BASE, then reads every
UBO through its descriptor (every index below 16: a8xx honours no more), the driver table and the device buffers it
points at, every T# and the texels
behind it (pitch, layer stride, format, type), every S# (API: filter/wrap/coordinates; constexpr: the sidecar's
words), the bindless set 4 framebuffer-fetch descriptor against the attachment, the vertex fetch against the
vertex descriptor and the vertex bytes, the vertex-id sideband, the VS->FS linkage *by name* (the location each FS
input reads is the one the VS output of the same name is written to), the FS outputs against the attachments, the
MRT formats/swaps/pitches, blend state, viewport -- all of it through CB0, so a wrong address faults as on the
chip. The model cannot execute ir3: when the state is right it writes the M3 Max golden into the target through
CB0 (the pixel-level comparison is the A16's experiment; Stage 6's llvmpipe runs of the same NIR are the host-side
evidence). A wrong byte anywhere in the state is a violation, and the tests prove that by breaking things.
A16 run 1 added three checks the first model lacked: UBO indices below 16, the alias.rt output constants
(SP_PS_OUTPUT_CONST_CNTL/MASK) and INTERP_ZERO for a layer input the VS does not write.

The Mesa comparison (test_p7_equivalence): `render` programs SkyLight SimpleVertex + SimpleTextureFragment through
the generalised path; its register state at the draw equals P7's (build_p7_ib, proven on the A16) except the
explained differences, and equals Mesa's own stream for the same variants (tools/testdata/a16gpu-p6p7/
gl-tri-skylight.rd) except P7's explained differences plus the ones `render` adds on purpose.

  python3 tools/test-a16gpu-render.py
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
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
import a16gpu_render as RD  # noqa: E402
import a16gpu_bindings as B  # noqa: E402
import a16gpu_render_scenes as SC  # noqa: E402

_spec = importlib.util.spec_from_file_location('p6p7_test', TOOLS / 'test-a16gpu-p6p7.py')
T67 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T67)
T3 = T67.T3
Q = a16gpu.q1n1proxy
field, dfield, f32 = T67.field, T67.dfield, T67.f32

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


if not (RD.SHADERS.is_file() and M.P7_SHADERS.is_file()):
    print(f'SKIP: a16gpu render model checks: {RD.SHADERS.name} / {M.P7_SHADERS.name} are not distributed '
          '(compiled from macOS shaders); generate them with tools/a16gpu-render/make-render-shaders.sh and '
          'tools/a16gpu-p7/make-p7-shaders.sh')
    sys.exit(0)
SHADERS, GOLDENS, _PINS = RD.load_assets(check=False)
SCENES = {s['name']: s for s in SC.all_scenes()}
GATE = [s['name'] for s in SC.scenes()]
PROBES = [s['name'] for s in SC.probe_scenes()]
FMT = X.ENUMS['a6xx_format']
SWAP = X.ENUMS['a3xx_color_swap']


def efmt(name):
    return FMT[name]


# ==========================================================================================================
# The model
# ==========================================================================================================
class FakeRender(T67.FakeP67):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.loads = {}                 # (op, state_type, block) -> (addr or None, n, direct payload)
        self.render_draws = []
        self.break_golden = set()       # functions whose "render" writes a wrong image (tests)
        self.stray_write = set()        # functions whose "render" writes into the guard band (tests)
        self.corrupt_tables_after = None  # after this many IBs, something rewrites an unused CB0 table entry (tests)

    def run_ib(self, va, dwords):
        super().run_ib(va, dwords)
        if self.corrupt_tables_after == self.ibs_run:
            offset, blob = RD.build_tables(T3.ARENA).image()
            at = T3.ARENA + offset + len(blob) - 8          # the last entry of the last table page (unused)
            self.dev_write(at, b'\x03' + bytes(7))

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
            if n > 16:
                self.violations.append(('cp', hex(w0), f'{n} T#/S# loaded: a stage has 16'))
                return
            addr = body[1] | body[2] << 32
            size = 16 if stype == ST['ST6_SHADER'] else 64
            data = self.gpu_read(addr, size * n, 'T#/S# state') if n else b''
            if data is None:
                return
            key = 'samp' if stype == ST['ST6_SHADER'] else 'tex'
            self.loads[key] = [struct.unpack_from(f'<{size // 4}I', data, size * i) for i in range(n)]
            if key == 'samp':
                self.samp_state = self.loads['samp'][0] if n else None
            else:
                self.tex_state = self.loads['tex'][0] if n else None
        else:
            self.violations.append(('cp', hex(w0), 'unexpected CP_LOAD_STATE6'))

    # --- the draw -------------------------------------------------------------------------------------------
    def draw(self, body):
        p = []

        def bad(what):
            p.append(what)
        if self.marker_mode != X.ENUMS['a6xx_marker']['RM6_DIRECT_RENDER']:
            bad('draw outside RM6_DIRECT_RENDER')
        w0 = body[0]
        if w0 & 0x3F != X.ENUMS['pc_di_primtype']['DI_PT_TRILIST'] or (w0 >> 6 & 3) != \
                X.ENUMS['pc_di_src_sel']['DI_SRC_SEL_AUTO_INDEX'] or body[1] != 1:
            bad(f'CP_DRAW_INDX_OFFSET {w0:#x}')
        # which pipeline: the code the SP fetches
        base = {'vs': self.r64('SP_VS_BASE'), 'fs': self.r64('SP_PS_BASE')}
        name = None
        for n, pp in SHADERS['pipelines'].items():
            want = {st: bytes.fromhex(pp[st]['code_hex']) for st in ('vs', 'fs')}
            if all(self.translate(R.GPU_SID, base[st], quiet=True) is not None and
                   self.dma_read(R.GPU_SID, base[st], len(want[st])) == want[st] for st in ('vs', 'fs')):
                name = n
                break
        if name is None:
            if self.r64('SP_PS_BASE') >> 24 == R.P67_VA >> 24:
                return super().draw(body)          # P7's triangle (its own model)
            self.violations.append(('draw', 'program', 'the code at SP_VS_BASE/SP_PS_BASE is no exported pipeline'))
            return
        # several scenes can share both variants (none here); the scene is the one whose functions match
        pipe, sc = SHADERS['pipelines'][name], SCENES[name]
        vs, fs = pipe['vs'], pipe['fs']
        if body[2] != sc['vertex']['count']:
            bad(f'{body[2]} vertices, the scene draws {sc["vertex"]["count"]}')
        for st, v, prefix in (('vs', vs, 'SP_VS'), ('fs', fs, 'SP_PS')):
            if self.r(f'{prefix}_INSTR_SIZE') != v['instrlen']:
                bad(f'{prefix}_INSTR_SIZE')
            cntl = self.r(f'{prefix}_CNTL_0')
            if field(f'{prefix}_CNTL_0', cntl, 'FULLREGFOOTPRINT') < v['info']['max_reg'] + 1 or \
                    field(f'{prefix}_CNTL_0', cntl, 'BRANCHSTACK') < v['branchstack_hw'] or \
                    field(f'{prefix}_CNTL_0', cntl, 'MERGEDREGS') != v['mergedregs']:
                bad(f'{prefix}_CNTL_0 {cntl:#x}')
            const = self.r(f'{prefix}_CONST_CONFIG')
            if field(f'{prefix}_CONST_CONFIG', const, 'CONSTLEN') * 4 < v['constlen'] or \
                    not field(f'{prefix}_CONST_CONFIG', const, 'ENABLED'):
                bad(f'{prefix}_CONST_CONFIG {const:#x}')
            if self.r(f'{prefix}_PVT_MEM_SIZE') or self.r64(f'{prefix}_PVT_MEM_BASE'):
                bad(f'{prefix} private memory programmed')
        # --- UBOs, the driver table, device buffers -----------------------------------------------------------
        for st, v in (('vs', vs), ('fs', fs)):
            ubos = self.ubo[st]
            probe = sc['group'] == 'probe' and B.stats(v).get('ubo_slots') == 32
            if len(ubos) > (32 if probe else B.UBO_SLOTS):
                bad(f'{st}: {len(ubos)} UBO descriptors loaded; a8xx honours UBO 0..15 (A16 run 1: UBO 31 read zeros)')
                continue
            if len(ubos) < v['const_state']['num_app_ubos']:
                bad(f'{st}: {len(ubos)} UBO descriptors loaded, ir3 reads {v["const_state"]["num_app_ubos"]}')
                continue
            bufs = {b['index']: b for b in sc['buffers'][st]}
            for b in B.bindings(v, 'ubo'):
                if not (b['present'] or b['used']):
                    continue
                base, vec4 = ubos[b['slot']]
                want = bufs[b['metal_index']]['data']
                got = self.gpu_read(base, len(want), f'{st} UBO {b["slot"]}') if vec4 else None
                if got != want or 16 * vec4 < max(len(want), B.ubo_min_bytes(v, b['slot'])):
                    bad(f'{st}: UBO {b["slot"]} does not hold buffer({b["metal_index"]}) (size {16 * vec4})')
            devs = [b for b in B.bindings(v, 'device_buffer') if b['present'] or b['used']]
            if devs:
                dt = devs[0]['driver_table_ubo']
                base, vec4 = ubos[dt] if dt < len(ubos) else (0, 0)
                table = self.gpu_read(base, 16 * vec4, f'{st} driver table') if vec4 else None
                if table is None or 16 * vec4 < B.DRIVER_TABLE_BYTES:
                    bad(f'{st}: UBO {dt} (driver table) missing or short')
                else:
                    for b in devs:
                        va = struct.unpack_from('<Q', table, b['slot'])[0]
                        want = bufs[b['metal_index']]['data']
                        if self.gpu_read(va, len(want), f'{st} device buffer({b["metal_index"]})') != want:
                            bad(f'{st}: driver table slot {b["slot"]} does not point at device buffer({b["metal_index"]})')
            for rng in v['const_state']['ubo_ranges']:
                if rng['block'] < len(ubos) and 16 * ubos[rng['block']][1] < rng['end']:
                    bad(f'{st}: UBO {rng["block"]} smaller than the pushed range ..{rng["end"]}')
        # --- T# / S# ------------------------------------------------------------------------------------------
        # Mesa's single count NTEX = NSAMP = num_samp (capped at 16), which covers ABI v4's separate ntex / nsamp
        n = min(fs['num_samp'], 16)
        if n < max(B.stats(fs)['ntex'], B.stats(fs)['nsamp']):
            bad(f'num_samp {n} below the sidecar\'s NTEX / NSAMP')
        nt = ns = n
        loads = bool(B.texture_slots(fs) or any(B.sampler_slots(fs)))
        psc = self.r('SP_PS_CONFIG')
        if field('SP_PS_CONFIG', psc, 'NTEX') != nt or field('SP_PS_CONFIG', psc, 'NSAMP') != ns or \
                self.r('SP_PS_TSIZE') != (nt if loads else 0):
            bad(f'SP_PS_CONFIG {psc:#x} / TSIZE {self.r("SP_PS_TSIZE")}: want NTEX {nt} / NSAMP {ns}')
        for st, v, rname in (('vs', vs, 'SP_VS_CONFIG'), ('fs', fs, 'SP_PS_CONFIG')):
            cfg = self.r(rname)
            if [field(rname, cfg, k) for k in ('BINDLESS_TEX', 'BINDLESS_SAMP', 'BINDLESS_UAV', 'BINDLESS_UBO')] != \
                    [int(bool(b)) for b in v['bindless']]:
                bad(f'{rname} bindless bits {cfg:#x} are not the variant\'s {v["bindless"]}')
        if field('GRAS_SC_CNTL', self.r('GRAS_SC_CNTL'), 'SINGLE_PRIM_MODE') != \
                X.ENUMS['a6xx_single_prim_mode']['FLUSH_PER_OVERLAP_AND_OVERWRITE' if fs['fb_read'] else 'NO_FLUSH']:
            bad('GRAS_SC_CNTL single-prim mode (a framebuffer-fetch FS flushes per overlap in sysmem)')
        if field('RB_BLEND_CNTL', self.r('RB_BLEND_CNTL'), 'INDEPENDENT_BLEND') != (len(sc['rts']) > 1) or \
                field('SP_BLEND_CNTL', self.r('SP_BLEND_CNTL'), 'INDEPENDENT_BLEND_EN') != (len(sc['rts']) > 1):
            bad('independent blend (Metal: per attachment)')
        tex = self.loads.get('tex', []) if nt and loads else []
        samp = self.loads.get('samp', []) if ns and loads else []
        if loads and (len(tex) < nt or len(samp) < ns):
            bad(f'{len(tex)} T# / {len(samp)} S# loaded, NTEX {nt} / NSAMP {ns}')
        elif loads:
            scene_tex = {t['index']: t for t in sc['textures']}
            t_slots = B.texture_slots(fs)
            for slot in range(nt):
                t = tex[slot]
                if slot not in t_slots:
                    if any(t):
                        bad(f'T#{slot} is a hole but not zero')
                    continue
                want = scene_tex[t_slots[slot]]
                problem = self.check_texture(t, want)
                if problem:
                    bad(f'T#{slot} (texture({t_slots[slot]}) {want["name"]}): {problem}')
            api, const = B.sampler_slots(fs)
            scene_samp = {s['index']: s for s in sc['samplers']}
            for slot in range(ns):
                s = list(samp[slot])
                if slot in const:
                    if s != const[slot]:
                        bad(f'S#{slot}: not the constexpr sampler of the sidecar')
                elif slot in api:
                    problem = self.check_sampler(s, scene_samp[api[slot]])
                    if problem:
                        bad(f'S#{slot}: {problem}')
                elif s != RD.api_sampler_descriptor(B.DEFAULT_SAMPLER):
                    bad(f'S#{slot} is a hole without the default sampler (ABI v5 3.4)')
        # --- framebuffer fetch (bindless set 4) ---------------------------------------------------------------
        fbf = B.fb_fetch(fs)
        set4 = self.r64('SP_GFX_BINDLESS_BASE_DESCRIPTOR', B.FS_BINDLESS_SET) & ~0x3
        for att, slot in fbf:
            d = self.gpu_read(set4 + 64 * slot, 64, 'bindless set 4')
            d = struct.unpack('<16I', d) if d else [0] * 16
            base = d[0] & ~0x3F | dfield('A8XX_TEX_MEMOBJ', 1, d[1], 'BASE_HI') << 32
            info = self.r('RB_MRT_BUF_INFO', att)
            view = efmt(RD.RT_HW[next(r['format'] for r in sc['rts'] if r['index'] == att)][3])
            if base != self.r64('RB_MRT_BASE', att) or dfield('A8XX_TEX_MEMOBJ', 3, d[3], 'FMT') != view or \
                    dfield('A8XX_TEX_MEMOBJ', 3, d[3], 'SWAP') != field('RB_MRT_BUF_INFO', info, 'COLOR_SWAP') or \
                    dfield('A8XX_TEX_MEMOBJ', 6, d[6], 'TEX_LINE_OFFSET') // 8 != self.r('RB_MRT_PITCH', att) << 6:
                bad(f'bindless set 4 descriptor {slot} is not a view of colour attachment {att}')
        # --- vertex fetch, system values ----------------------------------------------------------------------
        attrs = [i for i in vs['inputs'] if not i['sysval']]
        if field('VFD_CNTL_0', self.r('VFD_CNTL_0'), 'FETCH_CNT') != len(attrs):
            bad('VFD_CNTL_0 fetch count')
        by_index = {a['index']: a for a in sc['vertex']['attributes']}
        for i, a in enumerate(attrs):
            d = self.r('VFD_DEST_CNTL_INSTR', i)
            if field('VFD_DEST_CNTL_INSTR', d, 'REGID') != a['regid'] or \
                    field('VFD_DEST_CNTL_INSTR', d, 'WRITEMASK') != a['compmask']:
                bad(f'VFD_DEST_CNTL[{i}]')
            want = by_index.get(a['slot'] - 15)
            instr = self.r('VFD_FETCH_INSTR_INSTR', i)
            if want is None or field('VFD_FETCH_INSTR_INSTR', instr, 'OFFSET') != want['offset'] or \
                    field('VFD_FETCH_INSTR_INSTR', instr, 'FORMAT') != efmt(RD.VTX_HW[want['format']]) or \
                    field('VFD_FETCH_INSTR_INSTR', instr, 'IDX') != 0:
                bad(f'VFD_FETCH_INSTR[{i}] is not attribute {a["slot"] - 15}')
        if attrs:
            vb, size = self.r64('VFD_VERTEX_BUFFER_BASE', 0), self.r('VFD_VERTEX_BUFFER_SIZE', 0)
            if self.r('VFD_VERTEX_BUFFER_STRIDE', 0) != sc['vertex']['stride'] or \
                    self.gpu_read(vb, size, 'vertex buffer') != sc['vertex']['data']:
                bad('vertex buffer 0 is not the scene\'s vertices')
        vtx = M.sysval_regid(vs, 'SYSTEM_VALUE_VERTEX_ID')
        if field('VFD_CNTL_1', self.r('VFD_CNTL_1'), 'REGID4VTX') != vtx or \
                field('PC_VS_INPUT_CNTL', self.r('PC_VS_INPUT_CNTL'), 'SIDEBAND_CNT') != int(vtx != M.INVALID_REG):
            bad('vertex id sideband (VFD_CNTL_1 / PC_VS_INPUT_CNTL)')
        # --- linkage by name: each FS input's location is where the VS output of the same name is written -------
        outs = {}
        cnt = field('SP_VS_OUTPUT_CNTL', self.r('SP_VS_OUTPUT_CNTL'), 'OUT')
        for k in range(cnt):
            w = self.r('SP_VS_OUTPUT_REG', k // 2) >> (16 * (k % 2))
            locs = self.r('SP_VS_VPC_DEST_REG', k // 4)
            outs.setdefault(w & 0xFF, set()).add((locs >> (8 * (k % 4))) & 0xFF)
        vs_by_name = {v['name']: 32 + v['slot'] for v in B.sidecar(vs)['reflection']['varyings']}
        for var in B.sidecar(fs)['reflection']['varyings']:
            fin = next((i for i in fs['inputs'] if i['slot'] == 32 + var['slot'] and not i['sysval']), None)
            if fin is None:
                continue                      # declared but not read by the FS (ColorFill's texCoord): no linkage
            vreg = M.output_regid(vs, vs_by_name[var['name']])
            if fin['inloc'] not in outs.get(vreg, set()):
                bad(f'varying {var["name"]}: FS reads location {fin["inloc"]}, the VS writes it to {outs.get(vreg)}')
        pos = M.output_regid(vs, M.SLOT_POS)
        if field('VPC_VS_CNTL', self.r('VPC_VS_CNTL'), 'POSITIONLOC') not in outs.get(pos, set()):
            bad('position location')
        ij = M.sysval_regid(fs, 'SYSTEM_VALUE_BARYCENTRIC_PERSP_PIXEL')
        if field('SP_REG_PROG_ID_1', self.r('SP_REG_PROG_ID_1'), 'IJ_PERSP_PIXEL') != ij:
            bad('SP_REG_PROG_ID_1 IJ_PERSP_PIXEL')
        # a layer/viewport input the VS does not write must read zero (emit_interp_state; nir_lower_fb_read's layer)
        vs_slots = {o['slot'] for o in vs['outputs']}
        for inp in fs['inputs']:
            if inp['compmask'] and inp['bary'] and inp['slot'] in (RD.VARYING_SLOT_LAYER, RD.VARYING_SLOT_VIEWPORT) \
                    and inp['slot'] not in vs_slots:
                loc = inp['inloc']
                if (self.r('VPC_VARYING_INTERP_MODE_MODE', loc // 16) >> ((loc % 16) * 2)) & 3 != RD.INTERP_ZERO:
                    bad(f'FS input slot {inp["slot"]} (layer/viewport) at location {loc} is not INTERP_ZERO: the VS '
                        'does not write it')
        coord = M.sysval_regid(fs, 'SYSTEM_VALUE_FRAG_COORD')
        if field('SP_REG_PROG_ID_2', self.r('SP_REG_PROG_ID_2'), 'XYCOORDREGID') != coord:
            bad('SP_REG_PROG_ID_2 frag coord register')
        if coord != M.INVALID_REG and not field('GRAS_CL_INTERP_CNTL', self.r('GRAS_CL_INTERP_CNTL'), 'COORD_MASK'):
            bad('GRAS_CL_INTERP_CNTL coord mask for a FS reading frag_coord')
        # --- outputs and attachments --------------------------------------------------------------------------
        W, H = sc['width'], sc['height']
        if field('RB_PS_MRT_CNTL', self.r('RB_PS_MRT_CNTL'), 'MRT') != len(sc['rts']):
            bad('RB_PS_MRT_CNTL')
        mask = self.r('RB_PS_OUTPUT_MASK')
        alias = 0
        for o in fs['outputs']:
            if o['slot'] >= M.FRAG_RESULT_DATA0:
                alias |= (o.get('aliased_components') or 0) << (4 * (o['slot'] - M.FRAG_RESULT_DATA0))
        if self.r('SP_PS_OUTPUT_CONST_MASK') != alias or \
                field('SP_PS_OUTPUT_CONST_CNTL', self.r('SP_PS_OUTPUT_CONST_CNTL'), 'ENABLED') != (alias != 0):
            bad(f'SP_PS_OUTPUT_CONST_CNTL/MASK {self.r("SP_PS_OUTPUT_CONST_MASK"):#x}: the variant writes components '
                f'{alias:#x} with alias.rt')
        for rt in sc['rts']:
            i = rt['index']
            regid = M.output_regid(fs, M.FRAG_RESULT_DATA0 + i)
            if field('SP_PS_OUTPUT_REG', self.r('SP_PS_OUTPUT_REG', i), 'REGID') != regid & 0xFF or \
                    (mask >> (4 * i)) & 0xF != 0xF:
                bad(f'SP_PS_OUTPUT_REG[{i}] / output mask')
            hw, swap, cpp, _view = RD.RT_HW[rt['format']]
            info = self.r('RB_MRT_BUF_INFO', i)
            if field('RB_MRT_BUF_INFO', info, 'COLOR_FORMAT') != efmt(hw) or \
                    field('RB_MRT_BUF_INFO', info, 'COLOR_SWAP') != SWAP[swap] or \
                    field('SP_PS_MRT_REG', self.r('SP_PS_MRT_REG', i), 'COLOR_FORMAT') != efmt(hw) or \
                    (self.r('RB_MRT_PITCH', i) << 6) < W * cpp:
                bad(f'RB_MRT_BUF_INFO[{i}] / SP_PS_MRT_REG / pitch for {rt["format"]}')
            # a native attachment blends as the scene (Metal) says; a lowered one as the variant's hw_blend, with
            # alpha writes masked for 554 (BINDING-ABI 4.5) -- the lowering record, not the tool's helper
            lw = B.rt_lowering(fs).get(i)
            if lw is None:
                b, wmask = sc['blend'].get(i, {}), 0xF
            else:
                hb = lw['hw_blend']
                b = {'enabled': hb['enabled']}
                if hb['enabled']:
                    b.update({k: RD.MTL_BLEND_FACTOR[hb[k]] for k in ('src_rgb', 'dst_rgb', 'src_alpha', 'dst_alpha')})
                wmask = 0x7 if 'A' in lw['hw_write_mask_clear'] else 0xF
            ctl = self.r('RB_MRT_CONTROL', i)
            bc = self.r('RB_MRT_BLEND_CONTROL', i)
            on = bool(b.get('enabled'))
            if field('RB_MRT_CONTROL', ctl, 'COLOR_BLEND_EN') != on or field('RB_MRT_CONTROL', ctl, 'COMPONENT_ENABLE') != wmask \
                    or (field('SP_BLEND_CNTL', self.r('SP_BLEND_CNTL'), 'ENABLE_BLEND') >> i & 1) != on:
                bad(f'blend enable of attachment {i}')
            if on and (field('RB_MRT_BLEND_CONTROL', bc, 'RGB_SRC_FACTOR') !=
                       X.ENUMS['adreno_rb_blend_factor'][RD.BLEND_FACTOR[b['src_rgb']]] or
                       field('RB_MRT_BLEND_CONTROL', bc, 'RGB_DEST_FACTOR') !=
                       X.ENUMS['adreno_rb_blend_factor'][RD.BLEND_FACTOR[b['dst_rgb']]] or
                       field('RB_MRT_BLEND_CONTROL', bc, 'ALPHA_DEST_FACTOR') !=
                       X.ENUMS['adreno_rb_blend_factor'][RD.BLEND_FACTOR[b['dst_alpha']]]):
                bad(f'blend factors of attachment {i}')
        xs, xo = f32(self.r('GRAS_CL_VIEWPORT_XSCALE')), f32(self.r('GRAS_CL_VIEWPORT_XOFFSET'))
        ys, yo = f32(self.r('GRAS_CL_VIEWPORT_YSCALE')), f32(self.r('GRAS_CL_VIEWPORT_YOFFSET'))
        if (xs, xo, ys, yo) != (W / 2, W / 2, -H / 2, H / 2):
            bad('viewport is not Metal\'s (0, 0, W, H)')
        if p:
            self.violations.append(('draw', name, '; '.join(p)))
            return
        # the state is what the scene says: "render" = the M3 Max golden, written through CB0 like the RB would
        self.render_draws.append(name)
        for rt in sc['rts']:
            i = rt['index']
            cpp = RD.RT_HW[rt['format']][2]
            img = GOLDENS[name]['images'][i]
            if name in self.break_golden:
                img = bytes(b ^ 0x20 for b in img)
            base, pitch = self.r64('RB_MRT_BASE', i), self.r('RB_MRT_PITCH', i) << 6
            for y in range(H):
                self.gpu_write(base + y * pitch, img[y * W * cpp:(y + 1) * W * cpp], f'{name} rt{i}')
            if name in self.stray_write:
                self.gpu_write(base - 64, bytes(16), f'{name} stray')

    def check_texture(self, t, want):
        base = t[0] & ~0x3F | dfield('A8XX_TEX_MEMOBJ', 1, t[1], 'BASE_HI') << 32
        w, h = dfield('A8XX_TEX_MEMOBJ', 2, t[2], 'WIDTH'), dfield('A8XX_TEX_MEMOBJ', 2, t[2], 'HEIGHT')
        depth = dfield('A8XX_TEX_MEMOBJ', 1, t[1], 'DEPTH')
        kind = dfield('A8XX_TEX_MEMOBJ', 1, t[1], 'TYPE')
        fmt, swap = dfield('A8XX_TEX_MEMOBJ', 3, t[3], 'FMT'), dfield('A8XX_TEX_MEMOBJ', 3, t[3], 'SWAP')
        pitch = dfield('A8XX_TEX_MEMOBJ', 6, t[6], 'TEX_LINE_OFFSET') // 8
        layer = dfield('A8XX_TEX_MEMOBJ', 7, t[7], 'ARRAY_SLICE_OFFSET') << 12
        hw, wswap, cpp = RD.TEX_HW[want['format']]
        want_kind = X.ENUMS['a6xx_tex_type']['A6XX_TEX_2D' if want['type'] == '2d' else 'A6XX_TEX_1D']
        if (w, h, depth, kind, fmt, swap) != (want['width'], want['height'], want['layers'], want_kind, efmt(hw),
                                               SWAP[wswap]):
            return f'descriptor {(w, h, depth, kind, fmt, swap)}'
        if dfield('A8XX_TEX_MEMOBJ', 4, t[4], 'TILE_MODE') != 0 or pitch < w * cpp or pitch % 64:
            return 'tile mode or pitch'
        row = w * cpp
        for z in range(depth):
            for y in range(h):
                got = self.gpu_read(base + z * layer + y * pitch, row, 'texels')
                if got != want['data'][(z * h + y) * row:(z * h + y + 1) * row]:
                    return f'texels of layer {z} row {y} differ'
        return None

    def check_sampler(self, s, want):
        wrap = {'clamp_to_edge': 'A6XX_TEX_CLAMP_TO_EDGE', 'repeat': 'A6XX_TEX_REPEAT',
                'clamp_to_zero': 'A6XX_TEX_CLAMP_TO_BORDER'}[want['address']]
        filt = {'linear': 'A6XX_TEX_LINEAR', 'nearest': 'A6XX_TEX_NEAREST'}
        clamp, tf = X.ENUMS['a6xx_tex_clamp'], X.ENUMS['a6xx_tex_filter']
        if any(dfield('A8XX_TEX_SAMP', 0, s[0], k) != clamp[wrap] for k in ('WRAP_S', 'WRAP_T', 'WRAP_R')) or \
                dfield('A8XX_TEX_SAMP', 0, s[0], 'XY_MAG') != tf[filt[want['mag']]] or \
                dfield('A8XX_TEX_SAMP', 0, s[0], 'XY_MIN') != tf[filt[want['min']]] or \
                dfield('A8XX_TEX_SAMP', 1, s[1], 'UNNORM_COORDS'):
            return f'{[hex(x) for x in s]} is not {want}'
        return None


def run(fake, command, **kw):
    return S3.run(fake, command, S3.Args(**kw))


def ready(fake, fw):
    T3.happy_path(fake, fw)


def no_violation(fake, label):
    check(not fake.violations, f'{label}: model saw no violation ({fake.violations[:3]})')


# ==========================================================================================================
# Tests
# ==========================================================================================================
def test_assets():
    shaders, goldens, pins = RD.load_assets()
    check(len(GATE) == 28 and len(PROBES) == 6 and len(SCENES) == 39 and sorted(shaders['pipelines']) == sorted(SCENES),
          '28 gate scenes + 5 XR scenes + 6 probes, 39 exported pipelines')
    ref = Path(__file__).resolve().parent / 'a16gpu-render' / 'wscts-functions.txt'
    want = [l.split()[1:3] for l in ref.read_text().splitlines() if l.startswith('PASS')]
    check(sorted(fn for _lib, fn in want) == sorted(GATE) and len(want) == 28,
          'the scenes are exactly the 28 functions of the wscts reference run (wscts-functions.txt)')
    for lib, fn in want:
        check(SCENES[fn]['lib'] == lib, f'{fn}: library {lib}')
    for name, sc in SCENES.items():
        pipe = shaders['pipelines'][name]
        check(pipe['scene_digest'] == SC.scene_digest(sc) and goldens[name]['digest'] == SC.scene_digest(sc),
              f'{name}: variants and golden made from this scene')
        probe = sc['group'] == 'probe'
        check(not B.check_variant(pipe['vs'], 'vs', probe) and not B.check_variant(pipe['fs'], 'fs', probe)
              and not B.check_pipeline(pipe), f'{name}: inside the port ({B.check_variant(pipe["fs"], "fs", probe)})')
        check(all(B.stats(pipe[st])['max_ubo_index'] < (32 if probe else B.UBO_SLOTS) for st in ('vs', 'fs')),
              f'{name}: every UBO index below 16 (A16 run 1)')
        if sc['group'] == 'gate':
            check(all(pipe['compile'][st]['cache']['hit'] and pipe['compile'][st]['cache']['identical_online']
                      for st in ('vs', 'fs')), f'{name}: both stages from the Stage 6 cache, byte-identical online')
        else:                             # XR keys and probes the Stage 6 sweep did not make: compiled online
            check(all(pipe['compile'][st]['cache'].get('hit') or pipe['compile'][st]['cache'].get('compiled')
                      for st in ('vs', 'fs')), f'{name}: from the cache or compiled online through the library')
        if probe:
            continue                      # probe goldens: checked against their analytic values (test_probes)
        # realistic: nothing flat, the draw changes the target (the wscts references were half flat colours)
        for i, img in goldens[name]['images'].items():
            rt = next(r for r in sc['rts'] if r['index'] == i)
            cpp = RD.RT_HW[rt['format']][2]
            distinct = {img[k:k + cpp] for k in range(0, len(img), cpp)}
            check(len(distinct) >= 64 and img != rt['init'],
                  f'{name} rt{i}: golden has {len(distinct)} distinct values and differs from the initial contents')
    check(pins['golden_device'] == 'Apple M3 Max', 'goldens rendered on the M3 Max')
    if RD.SHADERS_SHA256:
        check(pins['shaders_sha256'] == RD.SHADERS_SHA256 and pins['goldens_sha256'] == RD.GOLDENS_SHA256,
              'assets match their pins')
    # refusals of the ABI port (each on a copy)
    fs = shaders['pipelines']['GPUPass']['fs']
    vs = copy.deepcopy(shaders['pipelines']['TextureCopy']['vs'])
    for b in vs['sidecar']['reflection']['bindings']:
        b['driver_table_ubo'] = 31                   # ABI v3's slot
    vs['sidecar']['ir3']['stats']['driver_table_ubo'] = 31
    check(B.check_variant(vs, 'vs'), 'a driver table at UBO 31 is refused (A16 run 1: it reads zeros)')
    for mutate, why in ((lambda v: v.update(pvtmem_size=512), 'private memory'),
                        (lambda v: v['sidecar'].update(abi_version=2), 'another ABI version'),
                        (lambda v: v['sidecar']['reflection']['rt_lowering'].append({'index': 0, 'class': 2,
                                                                                     'dual_source': True}),
                         'dual-source XR lowering'),
                        (lambda v: v['sidecar']['key_inputs'].update(rt_classes=[3] + [0] * 7), 'a class-3 (550) target'),
                        (lambda v: v['const_state'].update(driver_params_ubo=3), 'driver params UBO'),
                        (lambda v: v.update(constlen=0), 'a pushed range beyond constlen'),
                        (lambda v: v.update(sample_shading=1), 'sample shading'),
                        (lambda v: v['sidecar']['ir3']['stats'].update(ubo_slots=32), 'a --test-ubo-slots compile'),
                        (lambda v: v['sidecar']['reflection']['bindings'].append(
                            {'kind': 'ubo', 'metal_index': 16, 'slot': 16, 'present': True, 'used': True}),
                         'a constant buffer at UBO 16'),
                        (lambda v: v['sidecar']['reflection']['requires_no_attachment'].append(0),
                         'requires_no_attachment 0')):
        v = copy.deepcopy(fs)
        mutate(v)
        check(B.check_variant(v, 'fs'), f'a variant with {why} is refused')
    pipe = copy.deepcopy(shaders['pipelines']['BlurComposite'])
    pipe['varying_map'] = [dict(m, vs_slot=32) for m in pipe['varying_map']]
    check(B.check_pipeline(pipe), 'a linkage that does not pair varyings by name is refused')


def test_plan_and_ib():
    p = RD.plan(SC.all_scenes(), SHADERS)
    win = RD.Window(T3.ARENA)
    report = {}
    for f in p['functions']:
        cs = RD.build_ib(win, p, f, 7)
        check(cs.dwords() * 4 <= RD.IB_SLOT, f'{f["name"]}: IB fits its slot')
        report[f['name']] = cs.dwords()
        RD.data_uploads(win, p, f)
    over = [(f['name'], f['ntex'], f['nsamp']) for f in p['functions'] if f['ntex'] > 16 or f['nsamp'] > 16]
    check(not over, f'no function programs or loads more than 16 T#/S# ({over})')
    xr = next(f for f in p['functions'] if f['name'] == 'xr552_tint_premul_over')
    check(xr['pipe']['fs']['num_samp'] == 2 and (xr['ntex'], xr['nsamp']) == (2, 2),
          'the fetching FS with one T# programs Mesa\'s num_samp 2 (1 + the bindless read) for NTEX and NSAMP')
    fns = {f['name']: f for f in p['functions']}
    bc = B.stats(fns['BlurComposite']['pipe']['fs'])
    check((bc['ntex'], bc['nsamp']) == (9, 3) and (fns['BlurComposite']['ntex'], fns['BlurComposite']['nsamp']) == (9, 9),
          'BlurComposite: v4 reports NTEX 9 / NSAMP 3 (constexpr sampler in the lowest free S#); programmed 9 / 9 (Mesa)')
    check(max(max(f['ntex'], f['nsamp']) for f in p['functions'] if f['scene']['group'] == 'gate') == 9,
          'ABI v4\'s lowest-free constexpr slots: no gate function loads 16 T#/S# any more (fix2: 16 in 17 of 28)')
    plain = copy.deepcopy(fns['SimpleTextureFragment']['pipe']['fs'])
    for k, n in (('ntex', 17), ('nsamp', 17), ('ntex', 0), ('num_samp', 3)):
        v = copy.deepcopy(plain)
        if k == 'num_samp':
            v['num_samp'] = n                        # more than max(ntex, nsamp) without a framebuffer read
        else:
            v['sidecar']['ir3']['stats'][k] = n
        try:
            B.texture_counts(v)
            refused = False
        except B.ABIError:
            refused = True
        check(refused, f'{k} {n} is refused (16 per stage; not below the bound T#/S#; num_samp explained)')
    for name in ('TextureCopy', 'ColorFill', 'GPUPass'):
        f = fns[name]
        count, table = f['ubo']['vs']
        check(count == 1 and table == {0: ('driver_table',)},
              f'{name} VS: one UBO descriptor, the driver table at UBO 0 (run 1: 32, the table at 31)')
    writable = {r['window'] for r in win.refs if r['write']}
    check(writable <= {'r_rt', 'memptrs'}, f'the IBs write only render targets and the fence ({writable})')
    check(sum(len(f['rts']) for f in p['functions']) == 41 and p['rt_end'] <= R.ARENA_LAYOUT['r_rt'][1],
          '41 render targets (35 of the gate and XR scenes, two MRT pairs; 6 probes), all inside r_rt')
    # every region inside the arena, disjoint from every other arena region
    spans = sorted(R.ARENA_LAYOUT.values())
    check(all(a + b <= c for (a, b), (c, _d) in zip(spans, spans[1:])) and spans[-1][0] + spans[-1][1] <= R.ARENA_SIZE,
          'arena layout: the render regions overlap nothing and stay inside the 32 MiB arena')
    lo, size = R.RENDER_ARENA_WINDOW
    check(all(lo <= R.ARENA_LAYOUT[n][0] and R.ARENA_LAYOUT[n][0] + R.ARENA_LAYOUT[n][1] <= lo + size
              for n, _a in RD.RENDER_REGIONS) and R.RENDER_VA + size <= R.FB_VA and R.RENDER_VA >= R.P67_VA + 0x100000,
          'the render window sits below FB_VA and the Stage 4 mirror (1 GiB), clear of P6/P7 and the CP')
    return report


def test_render():
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = FakeRender()
        ready(fake, fw)
        mark = len(fake.log)
        status, out = run(fake, 'render', image_prefix=str(Path(tmp) / 'r'))
        check(status == 0, f'render completes ({out.get("stopped")})')
        no_violation(fake, 'render')
        check(fake.render_draws == GATE, 'the model validated all 28 draws, in scene order')
        res = out['render']['results']
        check(len(res) == 28 and all(r['ok'] for r in res.values())
              and all(c['identical'] and c['guards_intact'] for r in res.values() for c in r['rts']),
              'every function read back equal to its golden, guards intact')
        subs = out['submissions']
        check(len(subs) == 28 and all(int(s['fence'], 16) == int(s['fence_read'], 16) == int(s['bv_fence'], 16) ==
                                      int(s['ib_fence'], 16) for s in subs), 'one submission and one fence per draw')
        check(out['order'][-1] == 'render_matches_goldens' and out['render']['verdict']['within_tolerance'] == 28
              and out['render']['verdict']['gate_met'], 'R3 verdict: 28 of 28, gate met')
        writes = [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')]
        check(all(fake.in_arena(a, n) for _op, a, n in writes), 'every CPU write of render is an arena write')
        check(len(list(Path(tmp).glob('r.*.raw'))) == 30, 'every readback saved next to the JSON')
        by = {sub['what']: sub for sub in out['submissions']}
        check(by['TextureCopy']['refs_walked'] > by['ColorFill']['refs_walked'] >= 11,
              'descriptor-carried addresses (T# bases, driver-table VAs) are walked with the IB references')
        summ = out['render']['functions']
        check(all(summ[n]['variant_ids']['fs'] and summ[n]['draw_dwords'] > 990 for n in summ),
              'per function: variant ids and draw dwords recorded')
        # the XR lowering scenes after the 28 (--xr): 33 draws, XR targets compared in XR codes
        status, out = run(fake, 'render', xr=True)
        res = out['render']['results']
        check(status == 0 and len(res) == 33 and list(res)[28:] == [s['name'] for s in SC.xr_scenes()]
              and out['render']['verdict']['gate'], f'render --xr: the 28 then the 5 XR scenes ({out.get("stopped")})')
        no_violation(fake, 'render --xr')
        xr = SHADERS['pipelines']['xr552_tint_premul_over']['fs']
        check(B.rt_lowering(xr)[0]['blend_in_shader'] and xr['fb_read'],
              'the class-2 premultiplied source-over blends in the shader through framebuffer fetch (ABI v3)')
        # a second render on the same CP: the window is in place, same results
        status, out = run(fake, 'render', functions='SimpleColorFragment,GPUPass')
        check(status == 0 and list(out['render']['results']) == ['SimpleColorFragment', 'GPUPass']
              and not out['cb0_tables'][0]['changed'] and not out['render']['verdict']['gate_met'],
              '--functions: a subset in scene order, tables already in place, never the gate')
        no_violation(fake, 'second render')
        # quiesce, second cycle
        status, out = run(fake, 'quiesce')
        check(status == 0 and out['quiesce'] == 'graceful', 'quiesce after render')
        ready(fake, fw)
        status, out = run(fake, 'render', functions='InPlaceSover')
        check(status == 0 and out['render']['results']['InPlaceSover']['ok'], 'second cycle after quiesce')
        no_violation(fake, 'second cycle')
        # P6/P7 still work after render (their window replaces the render window)
        status, out = run(fake, 'triangle')
        check(status == 0 and out['p7']['compare']['ok'], 'P7 still passes after render on the same CP')
        # the probes (render --probes): six draws, never the gate; the UBO bands read their own values
        status, out = run(fake, 'render', probes=True)
        order = [n for n in PROBES if n != 'probe_ubo_hi'] + ['probe_ubo_hi']
        check(status == 0 and fake.render_draws[-6:] == order and list(out['render']['results']) == order
              and not out['render']['verdict']['gate'] and not out['render']['verdict']['gate_met'],
              f'render --probes: the six probes, not the gate ({out.get("stopped")})')
        no_violation(fake, 'render --probes')
        bands = {n: out['render']['results'][n]['rts'][0]['bands'] for n in ('probe_ubo_lo', 'probe_ubo_hi')}
        check(len(bands['probe_ubo_lo']) == 16 and len(bands['probe_ubo_hi']) == 15 and
              all(b['reads'] == 'own value' for bs in bands.values() for b in bs),
              'probe bands: each UBO band reports what it read (the model writes the golden: all their own value)')
        check([sc['name'] for sc in RD.select('probe_ubo_hi,probe_fetch,probe_ubo_lo')] ==
              ['probe_ubo_lo', 'probe_fetch', 'probe_ubo_hi'] and
              [sc['name'] for sc in RD.select(probes=True)] == order,
              'the out-of-envelope probe (UBO 16..30) draws last in any command that selects it')
        by = {sub['what']: sub for sub in out['submissions']}
        check(by['probe_ubo_hi']['refs_walked'] > by['probe_ubo_lo']['refs_walked'],
              'probe_ubo_hi binds UBO 0..15 too (bind_undeclared_ubos): their addresses are walked')


def test_mismatch_and_faults():
    with T3.synthetic_firmware() as fw:
        # an image that differs: a result, the rest still draws, R3 reports it
        fake = FakeRender()
        ready(fake, fw)
        fake.break_golden = {'ColorFillYCbCr'}
        status, out = run(fake, 'render')
        v = out['render'].get('verdict', {})
        check(status == 2 and out['stopped']['step'] == 'R3' and v.get('failed') == ['ColorFillYCbCr']
              and len(out['render']['results']) == 28 and len(fake.render_draws) == 28 and not v.get('gate_met'),
              'a function over tolerance: all 28 drawn, R3 names it (exit 2), gate not met')
        # a write outside a target: stop at once
        fake = FakeRender()
        ready(fake, fw)
        fake.stray_write = {'SimpleTextureFragment'}
        status, out = run(fake, 'render')
        check(status == 2 and out['stopped']['step'] == 'R2' and 'guard' in out['stopped']['reason']
              and len(fake.render_draws) == 2, 'a write into a guard band stops R2 after that draw')
        # a fence that never comes: bounded, diagnostics, nothing more submitted
        fake = FakeRender()
        ready(fake, fw)
        fake.inject = {3: {'drop_ib_fence': 1}}
        status, out = run(fake, 'render')
        check(status == 2 and out['stopped']['step'] == 'R2' and 'did not run to its end' in out['stopped']['reason']
              and len(out['submissions']) == 3, 'a missing IB fence stops at the third draw')
        fake = FakeRender()
        ready(fake, fw)
        fake.inject = {2: {'cp_global': 0x10000}}
        status, out = run(fake, 'render')
        check(status == 2 and 'report a fault' in out['stopped']['reason'] and len(out['submissions']) == 2,
              'a fault reported with the fence stops before the next draw')
        status, out = run(fake, 'render')
        check(status == 2 and out['stopped']['step'] == 'R0' and 'error interrupt' in out['stopped']['reason'],
              'the sticky error refuses the next render at R0')
        fake = FakeRender()
        ready(fake, fw)
        fake.s3['cp_executes'] = False
        status, out = run(fake, 'render')
        check(status == 2 and 'did not write the fence' in out['stopped']['reason']
              and 'CP_RB_RPTR' in out['stopped']['submission']['diagnostics'], 'CP stuck: bounded wait, diagnostics')
        # CB0's tables changed behind the tool's back after R1: the next submission is refused
        fake = FakeRender()
        ready(fake, fw)
        fake.corrupt_tables_after = 1
        status, out = run(fake, 'render')
        check(status == 2 and out['stopped']['step'] == 'R2' and 'changed since R1' in out['stopped']['reason']
              and len(out['submissions']) == 1, 'CB0 tables that changed after R1 stop the next submission')
        # refusals before anything is written
        fake = FakeRender()
        T3.happy_path(fake, fw, upto='oob')
        status, out = run(fake, 'render')
        check(status == 2 and out['stopped']['step'] == 'R0' and 'cp-fence' in out['stopped']['reason'],
              'render before cp-fence: refused at R0')
        fake = FakeRender()
        ready(fake, fw)
        status, out = run(fake, 'render', functions='NoSuchFunction')
        check(status == 2 and 'unknown functions' in out['stopped']['reason'] and not fake.render_draws,
              'an unknown function name is refused')
        mark = len(fake.log)
        status, out = run(fake, 'render', stop_after='R0')
        check(status == 0 and out['stopped_after'] == 'R0'
              and not [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')], 'R0 writes nothing')
        ring = R.ARENA_LAYOUT['cp_rb'][1] // 4
        fake.rptr = ring - 500
        fake.gx[R.REGS['CP_RB_WPTR'].addr] = ring - 500
        T3.host_record_set(fake, rb_wptr=ring - 500)
        status, out = run(fake, 'render')
        check(status == 2 and 'never wraps' in out['stopped']['reason'], 'no ring room for 28 submissions: refused')
    # assets that are not the pinned ones
    for attr in ('SHADERS_SHA256', 'GOLDENS_SHA256'):
        saved = getattr(RD, attr)
        setattr(RD, attr, '0' * 64)
        try:
            with T3.synthetic_firmware() as fw:
                fake = FakeRender()
                ready(fake, fw)
                mark = len(fake.log)
                status, out = run(fake, 'render')
                check(status == 2 and out['stopped']['step'] == 'R0' and 'is not the pinned' in out['stopped']['reason']
                      and not [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')],
                      f'{attr}: assets that are not the pinned ones stop R0 before anything is written')
        finally:
            setattr(RD, attr, saved)
    # a scene that drifted from its exported variants / golden
    saved = SC.scenes

    def drifted():
        out = saved()
        out[0]['buffers']['vs'][0]['data'] = bytes(64)
        return out
    SC.scenes = drifted
    try:
        with T3.synthetic_firmware() as fw:
            fake = FakeRender()
            ready(fake, fw)
            status, out = run(fake, 'render')
            check(status == 2 and out['stopped']['step'] == 'R0' and 'scene changed' in out['stopped']['reason'],
                  'a scene that no longer matches its variants and golden is refused at R0')
    finally:
        SC.scenes = saved


def test_refusal_before_writes():
    """review: a command stream the builder refuses for a late function stops R0, before R1 writes anything."""
    saved = RD.build_ib

    def refusing(win, p, f, fence):
        if f['name'] == 'GPUPass':
            raise RD.Refused('GPUPass: refused by the test')
        return saved(win, p, f, fence)
    RD.build_ib = refusing
    try:
        with T3.synthetic_firmware() as fw:
            fake = FakeRender()
            ready(fake, fw)
            mark = len(fake.log)
            status, out = run(fake, 'render')
            check(status == 2 and out['stopped']['step'] == 'R0' and 'GPUPass' in out['stopped']['reason']
                  and not [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')] and not fake.render_draws,
                  'a refused command stream (function 27) stops R0: nothing written, nothing drawn')
    finally:
        RD.build_ib = saved


def test_probes():
    """The probe goldens (Metal, M3 Max) are their analytic values, and the band report reads a zero / wrapped UBO."""
    W, H = 96, 64
    px = lambda img, x, y: tuple(img[4 * (y * W + x):4 * (y * W + x) + 4])
    bgra = lambda k: tuple(SC.probe_colour(k)[i] for i in (2, 1, 0, 3))
    g = {n: GOLDENS[n]['images'][0] for n in PROBES}
    sc = {s['name']: s for s in SC.probe_scenes()}
    check(all(px(g['probe_ubo_lo'], 6 * k + 3, y) == bgra(k) for k in range(16) for y in (0, 32, 63)) and
          all(px(g['probe_ubo_hi'], 6 * k + 3, y) == bgra(16 + k) for k in range(15) for y in (0, 32, 63)),
          'UBO probe goldens: band k is constant buffer(k)\'s value (Metal honours all 31)')
    check(all(px(g['probe_alias_rgb'], x, 30)[:3] == (153, 102, 51) for x in range(96)) and
          all(px(g['probe_alias_zw'], x, 30)[0::3] == (102, 255) for x in range(96)),
          'alias probe goldens: the constant components (0.2, 0.4, 0.6) / (0.4, 1.0)')
    init = sc['probe_fetch']['rts'][0]['init']
    check(all(px(g['probe_fetch'], x, y) == (init[4 * (y * W + x) + 2], init[4 * (y * W + x) + 1], init[4 * (y * W + x)],
                                            init[4 * (y * W + x) + 3]) for y in range(5, 59) for x in range(9, 87)),
          'fetch probe golden: the initial image with R and B swapped inside the quad')
    tex = sc['probe_a8']['textures'][0]['data']
    check(all(px(g['probe_a8'], x, y) == (102, 51, tex[y * 64 + x - 16], 255) for y in range(64) for x in range(16, 80)),
          'A8 probe golden: red = the texel\'s alpha, green 0.2, blue 0.4 (A8 samples as (0, 0, 0, a))')
    # the band report on images the A16 might return
    lo, hi = sc['probe_ubo_lo'], sc['probe_ubo_hi']
    zero = bytearray(g['probe_ubo_hi'])
    for y in range(H):
        for x in range(90):
            zero[4 * (y * W + x):4 * (y * W + x) + 3] = bytes(3)
    wrapped = bytearray(g['probe_ubo_hi'])
    for y in range(H):
        for x in range(90):
            wrapped[4 * (y * W + x):4 * (y * W + x) + 4] = bytes(bgra(x // 6))
    check(all(b['reads'] == 'own value' for b in RD.probe_bands(lo, g['probe_ubo_lo'])) and
          all(b['reads'] == 'zero' for b in RD.probe_bands(hi, bytes(zero))) and
          [b['reads'] for b in RD.probe_bands(hi, bytes(wrapped))][:2] == ['UBO 0 value', 'UBO 1 value'],
          'probe band report: own value / zero (ignored) / UBO k - 16 (wrapped)')


def test_model_catches_bad_state():
    """The model is not vacuous: break one binding at a time in the IB builder and see the draw refused."""
    cases = {
        'texture_descriptor': ('SimpleTextureFragment', lambda orig: lambda va, t: orig(va + 64, t), 'T#0'),
        'api_sampler_descriptor': ('SimpleTextureEDRFragment',
                                   lambda orig: lambda sp: orig(dict(sp, address='clamp_to_edge')), 'S#0'),
        'rt_view_descriptor': ('InPlaceSover', lambda orig: lambda va, fmt, w, h, p: orig(va + 4096, fmt, w, h, p),
                               'bindless set 4'),
    }
    for fn_name, (target, wrap, text) in cases.items():
        orig = getattr(RD, fn_name)
        setattr(RD, fn_name, wrap(orig))
        try:
            with T3.synthetic_firmware() as fw:
                fake = FakeRender()
                ready(fake, fw)
                status, out = run(fake, 'render', functions=target)
                check(any(v[0] == 'draw' and text in v[2] for v in fake.violations) and target not in fake.render_draws,
                      f'a broken {fn_name} is caught by the model ({text})')
        finally:
            setattr(RD, fn_name, orig)
    # A16 run 1's three state bugs, put back one at a time: the model refuses each draw
    for target, patch, text in (
            ('AlphaTextureFragment', ('fs_outputs', lambda orig: lambda fs: orig(fs)[:2] + (0,) + orig(fs)[3:]),
             'SP_PS_OUTPUT_CONST'),
            ('ShadowHorizontalBlurFragment', ('fs_outputs', lambda orig: lambda fs: orig(fs)[:2] + (0,) + orig(fs)[3:]),
             'SP_PS_OUTPUT_CONST'),
            ('InPlaceSover', ('interp_regs', lambda orig: lambda pipe: M.interp_regs(pipe)), 'INTERP_ZERO')):
        name, wrap = patch
        orig = getattr(RD, name)
        setattr(RD, name, wrap(orig))
        try:
            with T3.synthetic_firmware() as fw:
                fake = FakeRender()
                ready(fake, fw)
                run(fake, 'render', functions=target)
                check(any(v[0] == 'draw' and text in v[2] for v in fake.violations) and target not in fake.render_draws,
                      f'{target}: run 1\'s {name} state is caught by the model ({text})')
        finally:
            setattr(RD, name, orig)
    # the driver table at UBO 31 again (ABI v3): refused by the port before anything is written
    shaders, goldens, pins = RD.load_assets(check=False)
    for b in shaders['pipelines']['TextureCopy']['vs']['sidecar']['reflection']['bindings']:
        b['driver_table_ubo'] = 31
    shaders['pipelines']['TextureCopy']['vs']['sidecar']['ir3']['stats']['driver_table_ubo'] = 31
    saved = RD.load_assets
    RD.load_assets = lambda check=True: (shaders, goldens, pins)
    try:
        with T3.synthetic_firmware() as fw:
            fake = FakeRender()
            ready(fake, fw)
            mark = len(fake.log)
            status, out = run(fake, 'render', functions='TextureCopy')
            check(status == 2 and out['stopped']['step'] == 'R0' and 'UBO 31' in out['stopped']['reason']
                  and not [e for e in fake.log[mark:] if e[0] in ('mw', 'memset')],
                  'a variant with the driver table at UBO 31 stops R0 (nothing written)')
    finally:
        RD.load_assets = saved
    # the driver table pointing at the wrong device buffer
    orig = B.driver_table
    B.driver_table = lambda v, address_of: orig(v, lambda n: address_of(n) + 16)
    try:
        with T3.synthetic_firmware() as fw:
            fake = FakeRender()
            ready(fake, fw)
            run(fake, 'render', functions='TextureCopy')
            check(any('driver table' in v[2] for v in fake.violations), 'a wrong driver-table address is caught')
    finally:
        B.driver_table = orig
    # linkage: swap the two varyings of BlurComposite in the exported linkage
    shaders, goldens, pins = RD.load_assets(check=False)
    var = shaders['pipelines']['BlurComposite']['link_vs_fs']['var']
    var[0]['loc'], var[1]['loc'] = var[1]['loc'], var[0]['loc']
    saved = RD.load_assets
    RD.load_assets = lambda check=True: (shaders, goldens, pins)
    try:
        with T3.synthetic_firmware() as fw:
            fake = FakeRender()
            ready(fake, fw)
            run(fake, 'render', functions='BlurComposite')
            check(any('varying' in v[2] for v in fake.violations), 'a linkage that crosses two varyings is caught')
    finally:
        RD.load_assets = saved


# --- the generalised builder against P7 (proven on the A16) and Mesa -----------------------------------------------
def p7_as_render():
    """P7's pipeline and scene expressed as a render function (the P7 variants, the P7 inputs; a 96x64 BGRA8
    target at Mesa's pitch 512, no clear: its contents uploaded)."""
    sh = M.p7_assets()[0]
    pipe = {'vs': sh['vs'], 'fs': dict(sh['fs']), 'link_vs_fs': sh['link_vs_fs'], 'vs_function': 'SimpleVertex',
            'lib': 'SkyLight', 'varying_map': []}
    # P7's variants are ABI v2 (num_samp 0: the Stage 6 finding): program the counts P7 used
    pipe['fs']['num_samp'] = 1
    pipe['fs']['need_pixlod'] = 1
    verts = M.P7['vertices']
    sc = {'name': 'P7', 'width': 96, 'height': 64,
          'vertex': {'stride': 16, 'count': 3, 'data': SC.f32(*verts), 'attributes': [dict(a, index=i) for i, a in enumerate(M.P7['attributes'])],
                     'triangles_px': [(verts[i], verts[i + 1]) for i in range(0, 12, 4)]},
          'buffers': {'vs': [{'index': 1, 'data': SC.f32(*M.P7['mvp']), 'device': False}], 'fs': []},
          'textures': [{'index': 0, 'name': 'p7', 'type': '2d', 'format': 'rgba8', 'width': 64, 'height': 64,
                        'layers': 1, 'data': M.p7_texture()}],
          'samplers': [{'index': 0, 'min': 'linear', 'mag': 'linear', 'address': 'clamp_to_edge'}],
          'rts': [{'index': 0, 'format': 'bgra8', 'init': bytes(96 * 64 * 4)}], 'blend': {}, 'tolerance': 3,
          'edge_budget': 16, 'realistic': ''}
    return pipe, sc


def test_p7_equivalence():
    pipe, sc = p7_as_render()
    shaders = {'pipelines': {'P7': pipe}}
    p = RD.plan([sc], shaders)
    ours = T67.ours_walker(RD.build_ib(RD.Window(T3.ARENA), p, p['functions'][0], 7), va=0x12000000)
    sh = M.p7_assets()[0]
    _pr, _fi, sampling = M.variant_checks(sh)
    p7 = T67.ours_walker(M.build_p7_ib(M.Window(T3.ARENA), sh, sampling, 7)[0])
    od = [s for s in ours.snaps if s[0] == 'draw']
    pd = [s for s in p7.snaps if s[0] == 'draw']
    check(len(od) == len(pd) == 1, 'one draw in each')
    differ = set(T67.names_of(T67.diff_state(pd[0][1], od[0][1])))
    # expected: addresses; the target's pitch (Mesa's 512 for 96 BGRA8 texels, P7 used 384); P7's 2D clear
    # (render uploads the target's contents instead) and its CP_BLIT registers; SP_PS_TSIZE is written by the
    # texture group in both but P7's T#/S# bases differ (addresses)
    expected = T67.ADDRESS_REGS | {'RB_MRT_PITCH', 'RB_MRT_ARRAY_PITCH', 'GRAS_A2D_DEST_TL', 'GRAS_A2D_DEST_BR',
                                   'RB_A2D_BLT_CNTL', 'GRAS_A2D_BLT_CNTL', 'TPL1_A2D_BLT_CNTL', 'SP_A2D_OUTPUT_INFO',
                                   'RB_A2D_PIXEL_CNTL', 'RB_A2D_DEST_BUFFER_INFO', 'RB_A2D_DEST_BUFFER_BASE',
                                   'RB_A2D_DEST_BUFFER_PITCH', 'RB_A2D_CLEAR_COLOR_DW0', 'RB_A2D_CLEAR_COLOR_DW1',
                                   'RB_A2D_CLEAR_COLOR_DW2', 'RB_A2D_CLEAR_COLOR_DW3'}
    unexplained = differ - expected
    check(not unexplained, f'render\'s state for the P7 pipeline equals P7\'s (proven on the A16) but for the '
                           f'explained registers (unexplained: {sorted(unexplained)})')
    check({'RB_MRT_PITCH', 'RB_MRT_BASE'} <= differ and len(set(T67.names_of(od[0][1])) - differ) > 200,
          f'not vacuous: {len(set(T67.names_of(od[0][1])) - differ)} registers identical')
    # the packets around the draw: P7's sequence minus the 2D clear (marker, the two register bunches, the
    # DEBUG_LABEL event, CP_BLIT) and its surrounding flushes
    blit = R.CP_BLIT
    check(blit in [op for op, _b in p7.bodies] and blit not in [op for op, _b in ours.bodies],
          'P7 clears with CP_BLIT, render does not')
    # vs Mesa: gl-tri-skylight.rd (the same two variants on the real gallium driver)
    ref = T67.ref_walker('gl-tri-skylight.rd')
    rd = [s for s in ref.snaps if s[0] == 'draw']
    differ = set(T67.names_of(T67.diff_state(rd[0][1], od[0][1])))
    unexplained = differ - T67.P7_EXPECTED - expected
    check(not unexplained, f'render\'s state for the same variants equals Mesa\'s but for P7\'s explained registers '
                           f'and the upload-instead-of-clear ones (unexplained: {sorted(unexplained)})')
    kinds = [(T67.names_of([r])[0], ref.kind[r], ours.kind[r]) for r in set(ref.kind) & set(ours.kind)
             if ref.kind[r] != ours.kind[r]]
    check(not kinds, f'same packet kind per register as Mesa ({kinds[:4]})')
    # T#/S#: the generalised descriptors equal P7's (and so Mesa's, test-a16gpu-p6p7 test_reference)
    t = sc['textures'][0]
    check(RD.texture_descriptor(0x110B4000, t) == M.texture_descriptor(0x110B4000, 64, 64, 256, 'rgba'),
          'T#: fdl6_view_init for 64x64 RGBA8 as P7 (and Mesa)')
    check(RD.api_sampler_descriptor(sc['samplers'][0]) == M.sampler_descriptor(), 'S#: linear clamp as P7')
    # the generalised linkage emits P7's words for P7's pipeline (no vertex id there)
    sh2 = dict(sh)
    check(RD.program_linkage(sh2, 1)[0] == M.program_linkage(sh2)[0], 'program_linkage == P7\'s for P7')


# --- the new state classes against Mesa's own streams (tools/testdata/a16gpu-render, rcap.c on drm-shim) ----------
RENDER_REFS = TOOLS / 'testdata' / 'a16gpu-render'
METAL_CONVENTIONS = {'GRAS_CL_CNTL', 'GRAS_CL_VIEWPORT_YSCALE', 'GRAS_CL_VIEWPORT_ZOFFSET', 'GRAS_CL_VIEWPORT_ZSCALE',
                     'PC_CNTL', 'VPC_PC_CNTL'}                    # P7_EXPECTED's Metal half (BINDING-ABI 4.3)
# state that follows the compiled program (GLSL compiled by Mesa there, the air2ir3 variant here)
PROGRAM_REGS = {'SP_VS_CNTL_0', 'SP_PS_CNTL_0', 'SP_VS_INSTR_SIZE', 'SP_PS_INSTR_SIZE', 'SP_VS_CONST_CONFIG',
                'SP_PS_CONST_CONFIG', 'SP_VS_OUTPUT_REG', 'SP_VS_VPC_DEST_REG', 'SP_PS_OUTPUT_REG', 'SP_PS_CONFIG',
                'SP_PS_OUTPUT_CONST_CNTL', 'SP_PS_OUTPUT_CONST_MASK', 'RB_PS_CNTL', 'VPC_VS_CNTL', 'VPC_PS_CNTL',
                'PC_VS_CNTL', 'PC_VS_INPUT_CNTL', 'VPC_VARYING_LM_TRANSFER_CNTL_DISABLE', 'VPC_VARYING_INTERP_MODE_MODE',
                'VFD_CNTL_0', 'VFD_DEST_CNTL_INSTR', 'VFD_FETCH_INSTR_INSTR', 'VFD_FETCH_INSTR_STEP_RATE',
                'VFD_VERTEX_BUFFER_SIZE', 'VFD_VERTEX_BUFFER_STRIDE', 'SP_PS_TSIZE'}
# GLSL's framebuffer-fetch lowering makes the FS per-sample (fs->sample_shading); the air2ir3 variant is not
SAMPLE_SHADING = {'RB_PS_INPUT_CNTL', 'RB_PS_SAMPLEFREQ_CNTL', 'GRAS_LRZ_PS_INPUT_CNTL', 'GRAS_LRZ_PS_SAMPLEFREQ_CNTL'}
MRT_REGS = ['RB_MRT_BUF_INFO', 'RB_MRT_PITCH', 'RB_MRT_ARRAY_PITCH', 'SP_PS_MRT_REG', 'RB_COLOR_FLAG_BUFFER_PITCH',
            'GRAS_LRZ_MRT_BUFFER_INFO_0', 'RB_SRGB_CNTL', 'SP_SRGB_CNTL', 'RB_PS_MRT_CNTL', 'SP_PS_MRT_CNTL',
            'RB_PS_OUTPUT_MASK', 'SP_PS_OUTPUT_MASK', 'RB_PS_OUTPUT_CNTL']
BLEND_REGS = ['RB_MRT_CONTROL', 'RB_MRT_BLEND_CONTROL', 'SP_MRT_BLEND_CNTL_REG', 'RB_BLEND_CNTL', 'SP_BLEND_CNTL',
              'RB_DITHER_CNTL']
MESA_CASES = {   # capture: (render function, registers that must equal Mesa's, further explained differences)
    'gl-mrt.rd': ('ColorFillYCbCr', MRT_REGS + BLEND_REGS, set()),
    'gl-blend.rd': ('ShadowCompositeFragment', MRT_REGS + BLEND_REGS, set()),
    'gl-fbfetch.rd': ('InPlaceSover', MRT_REGS + BLEND_REGS + ['GRAS_SC_CNTL', 'SP_PS_CONFIG'], SAMPLE_SHADING),
    'gl-tex.rd': ('SimpleTextureEDRFragment', MRT_REGS + BLEND_REGS, set()),
    'gl-tex1d.rd': ('UberResampleLanczosFragmentBGRA', BLEND_REGS, {'RB_MRT_BUF_INFO'}),   # GL target: RGBA8
    'gl-vid.rd': ('TextureCopy', MRT_REGS + BLEND_REGS + ['VFD_CNTL_0', 'VFD_CNTL_1', 'PC_VS_INPUT_CNTL'], set()),
}


def reg_values(state, name):
    offset, bits, _f, stride, length = X.REGS[name]
    return [tuple(state.get(offset + i * stride + k) for k in range(2 if bits == 64 else 1)) for i in range(length)]


def mesa_draw(capture):
    T67.REFS, saved = RENDER_REFS, T67.REFS
    try:
        ref = T67.ref_walker(capture)
    finally:
        T67.REFS = saved
    draws = [s for s in ref.snaps if s[0] == 'draw']
    return ref, draws


def loaded_descriptors(walker, loads):
    out = {}
    for op, b in loads:
        stype, src, n = (b[0] >> 14) & 3, (b[0] >> 16) & 3, b[0] >> 22
        if src == 2 and op == R.CP_LOAD_STATE6_FRAG:
            size = 16 if stype == 0 else 64
            data = walker.read(b[1] | b[2] << 32, size * n)
            out['samp' if stype == 0 else 'tex'] = [list(struct.unpack_from(f'<{size // 4}I', data, size * i))
                                                     for i in range(n)]
    return out


def masked_tex(d):
    """A T# without its base address (dwords 0 and the BASE_HI bits of 1)."""
    return [0] + [d[1] & ~0x1FFFF] + list(d[2:])


def test_mesa_state():
    for name, digest in (('gl-mrt.rd', 'aa4c68f666edc4ce'), ('gl-blend.rd', 'a4bad4e8c03f7ad6'),
                         ('gl-fbfetch.rd', '6fcb7d9969639f44'), ('gl-tex.rd', '53ab92e3f823ffb9'),
                         ('gl-tex1d.rd', 'c66e2c4b37566fbe'), ('gl-vid.rd', '941007c162b5109a')):
        check(hashlib.sha256((RENDER_REFS / name).read_bytes()).hexdigest().startswith(digest),
              f'{name}: the capture the README describes')
    p = RD.plan(SC.scenes(), SHADERS)
    fns = {f['name']: f for f in p['functions']}
    ours_by_fn = {}
    for capture, (fn, must, extra) in MESA_CASES.items():
        ref, rdraws = mesa_draw(capture)
        cs = RD.build_ib(RD.Window(T3.ARENA), p, fns[fn], 7)
        ours = T67.ours_walker(cs, va=R.RENDER_VA)
        odraws = [s for s in ours.snaps if s[0] == 'draw']
        check(len(rdraws) == len(odraws) == 1, f'{capture}: one draw in each')
        ours_by_fn[fn] = (ours, odraws[0])
        rs, os_ = rdraws[0][1], odraws[0][1]
        for r in must:
            check(reg_values(rs, r) == reg_values(os_, r), f'{capture} vs {fn}: {r} equals Mesa\'s '
                                                           f'({reg_values(rs, r)[:2]} / {reg_values(os_, r)[:2]})')
        differ = set(T67.names_of(T67.diff_state(rs, os_)))
        unexplained = differ - T67.ADDRESS_REGS - METAL_CONVENTIONS - PROGRAM_REGS - {'RB_SAMPLE_COUNTER_CNTL'} - extra
        check(not unexplained, f'{capture} vs {fn}: every other difference is an address, a Metal convention or '
                               f'program state (unexplained: {sorted(unexplained)})')
        kinds = [T67.names_of([r])[0] for r in set(ref.kind) & set(ours.kind) if ref.kind[r] != ours.kind[r]]
        check(not kinds, f'{capture}: same packet kind per register as Mesa ({kinds[:4]})')
    # descriptors: T# for RGBA16F 64x64 and RGBA8 128x96 (gl-tex), 1D 256 and 1D-array 256x6 (gl-tex1d), their S#,
    # and the framebuffer-fetch view (gl-fbfetch, bindless set 4 slot 55)
    ref, rdraws = mesa_draw('gl-tex.rd')
    md = loaded_descriptors(ref, rdraws[0][2])
    tx = {t['name']: t for s in SC.scenes() for t in s['textures']}
    check(masked_tex(md['tex'][0]) == masked_tex(RD.texture_descriptor(0, tx['photo_edr'])),
          'T# RGBA16F 64x64: fdl6_view_init dword for dword (Mesa pitch 512, MIN_LINE_OFFSET 3, layer 32 KiB)')
    check(masked_tex(md['tex'][1]) == masked_tex(RD.texture_descriptor(0, tx['photo128'])),
          'T# RGBA8 128x96: as Mesa\'s')
    check(md['samp'][0] == RD.api_sampler_descriptor({'min': 'nearest', 'mag': 'nearest', 'address': 'repeat'}),
          'S# nearest / repeat: as Mesa\'s')
    wrap_r = [f for f in X.DESC['A8XX_TEX_SAMP_0'][2] if f[0] == 'WRAP_R'][0]
    mask = ((1 << (wrap_r[2] - wrap_r[1] + 1)) - 1) << wrap_r[1]
    ours = RD.api_sampler_descriptor({'min': 'linear', 'mag': 'linear', 'address': 'clamp_to_edge'})
    check(md['samp'][1][0] & ~mask == ours[0] & ~mask and md['samp'][1][1:] == ours[1:],
          'S# linear / clamp_to_edge: as Mesa\'s but WRAP_R (GL defaults REPEAT, Metal clamp_to_edge)')
    ref, rdraws = mesa_draw('gl-tex1d.rd')
    md = loaded_descriptors(ref, rdraws[0][2])
    check(masked_tex(md['tex'][0]) == masked_tex(RD.texture_descriptor(0, tx['wave'])),
          'T# 1D 256 RGBA8: as Mesa\'s (type 1D, depth 1, layer stride 4 KiB)')
    check(masked_tex(md['tex'][1]) == masked_tex(RD.texture_descriptor(0, tx['cc_lut'])),
          'T# 1D array 256x6 RGBA8: as Mesa\'s (depth 6, layer stride 4 KiB)')
    ref, rdraws = mesa_draw('gl-fbfetch.rd')
    rs = rdraws[0][1]
    set4 = reg_values(rs, 'SP_GFX_BINDLESS_BASE_DESCRIPTOR')[B.FS_BINDLESS_SET]
    base = (set4[0] | set4[1] << 32) & ~3
    mesa55 = list(struct.unpack('<16I', ref.read(base + 64 * B.FB_FETCH_BASE, 64)))
    rt = fns['InPlaceSover']['rts'][0]
    ours55 = RD.rt_view_descriptor(0, rt['format'], 96, 64, rt['pitch'])
    check(masked_tex(mesa55) == masked_tex(ours55) and mesa55[0] == reg_values(rs, 'RB_MRT_BASE')[0][0],
          'framebuffer-fetch descriptor (bindless set 4, slot 55): Mesa\'s patch_fb_read_sysmem view of colour 0')
    # A16 run 1: the fetch read 0. Mesa's fbfetch FS reads one varying, the layer nir_lower_fb_read adds, and programs
    # it INTERP_ZERO (the VS does not write gl_Layer); ours does the same at our layer input's location.
    npv = field('VPC_PS_CNTL', reg_values(rs, 'VPC_PS_CNTL')[0][0], 'NUMNONPOSVAR')
    check(npv == 1 and reg_values(rs, 'VPC_VARYING_INTERP_MODE_MODE')[0][0] & 3 == RD.INTERP_ZERO,
          'Mesa gl-fbfetch: its one varying (the layer) is INTERP_ZERO at location 0')
    ours, od = ours_by_fn['InPlaceSover']
    layer = next(i for i in SHADERS['pipelines']['InPlaceSover']['fs']['inputs'] if i['slot'] == RD.VARYING_SLOT_LAYER)
    loc = layer['inloc']
    check((reg_values(od[1], 'VPC_VARYING_INTERP_MODE_MODE')[loc // 16][0] >> (2 * (loc % 16))) & 3 == RD.INTERP_ZERO,
          f'InPlaceSover: the layer input at location {loc} is INTERP_ZERO, as Mesa programs it')
    # A16 run 1: aliased output components came out as garbage. Mesa's blend FS writes two constant components with
    # alias.rt and enables them (SP_PS_OUTPUT_CONST_CNTL 1, MASK 0xc); ours enables the variant's.
    ref, rdraws = mesa_draw('gl-blend.rd')
    rs = rdraws[0][1]
    check(reg_values(rs, 'SP_PS_OUTPUT_CONST_CNTL')[0][0] == 1 and reg_values(rs, 'SP_PS_OUTPUT_CONST_MASK')[0][0] == 0xC,
          'Mesa gl-blend: SP_PS_OUTPUT_CONST_CNTL 1 / MASK 0xc for its alias.rt components')
    ours, od = ours_by_fn['ShadowCompositeFragment']
    check(reg_values(od[1], 'SP_PS_OUTPUT_CONST_CNTL')[0][0] == 1 and reg_values(od[1], 'SP_PS_OUTPUT_CONST_MASK')[0][0] == 0x7,
          'ShadowCompositeFragment: SP_PS_OUTPUT_CONST_CNTL 1 / MASK 0x7 (rgb constant 0 written by alias.rt)')


def test_cli():
    with T3.synthetic_firmware() as fw, tempfile.TemporaryDirectory() as tmp:
        fake = FakeRender()
        ready(fake, fw)
        real = Q.connect
        Q.connect = lambda device=None, wait=0.0, **_: fake
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    a16gpu.main(['render'])
                    check(False, 'render without --write refused')
                except SystemExit as refused:
                    check(refused.code == 2, 'render without --write: refused by the parser')
                path = Path(tmp) / 'render.json'
                status = a16gpu.main(['--out', str(path), 'render', '--write', '--functions',
                                      'SimpleTextureFragment,ColorFillYCbCr'])
                saved = json.loads(path.read_text())
                check(status == 0 and saved['order'][-1] == 'render_matches_goldens'
                      and Path(tmp, 'render.ColorFillYCbCr.rt1.raw').is_file(),
                      'CLI render --functions: JSON and the readback images written')
        finally:
            Q.connect = real


def main():
    for test in (test_assets, test_plan_and_ib, test_p7_equivalence, test_mesa_state, test_probes, test_render,
                 test_mismatch_and_faults, test_refusal_before_writes, test_model_catches_bad_state, test_cli):
        test()
    print(f'PASS: {checks} a16gpu render model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
