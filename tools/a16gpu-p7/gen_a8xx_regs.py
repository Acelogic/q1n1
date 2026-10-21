#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate tools/a8xx_regs.py: the A8XX view of the freedreno register XML, for exactly the registers,
PM4 packet dwords, descriptors and enums the P6/P7 command streams use.

  MESA_TREE=<mesa checkout at the Stage 6 revision bbeb2dca9fee> python3 tools/a16gpu-p7/gen_a8xx_regs.py

Offsets and fields come from Mesa's own register database (src/freedreno/registers/adreno/*.xml) read with
Mesa's own parser (gen_header.py); a register with several chip variants resolves to the one whose range
contains A8XX, exactly as gen_header's C++ templates do for `CHIP == A8XX`. Nothing here is typed by hand.
The output is checked in; the A16 tool and the tests never need a Mesa tree.
"""
import os
import sys

MESA = os.environ.get('MESA_TREE')
if not MESA:
    sys.exit('set MESA_TREE to the Mesa checkout (Stage 6: third_party/mesa, revision bbeb2dca9fee)')
REGS_DIR = os.path.join(MESA, 'src', 'freedreno', 'registers')
sys.dont_write_bytecode = True     # the Mesa tree is read-only: no __pycache__ in it
sys.path.insert(0, REGS_DIR)
import gen_header as G  # noqa: E402

ORDER = {'A2XX': 2, 'A3XX': 3, 'A4XX': 4, 'A5XX': 5, 'A6XX': 6, 'A7XX': 7, 'A8XX': 8}

# Registers the P6/P7 command streams write (every one Mesa's gallium a8xx wrote in the reference captures
# of tools/testdata/a16gpu-p6p7, plus the few this tool reads back), by XML name.
REG_NAMES = """
GRAS_A2D_BLT_CNTL GRAS_A2D_DEST_BR GRAS_A2D_DEST_TL GRAS_A2D_SCISSOR_BR GRAS_A2D_SCISSOR_TL GRAS_A2D_SRC_XMAX
GRAS_A2D_SRC_XMIN GRAS_A2D_SRC_YMAX GRAS_A2D_SRC_YMIN GRAS_BIN_FOVEAT GRAS_BIN_FOVEAT_XY_FDM_OFFSET GRAS_CL_ARRAY_SIZE
GRAS_CL_CNTL GRAS_CL_GUARDBAND_CLIP_ADJ GRAS_CL_INTERP_CNTL GRAS_CL_VIEWPORT_XOFFSET GRAS_CL_VIEWPORT_XSCALE
GRAS_CL_VIEWPORT_YOFFSET GRAS_CL_VIEWPORT_YSCALE GRAS_CL_VIEWPORT_ZCLAMP_MAX GRAS_CL_VIEWPORT_ZCLAMP_MIN
GRAS_CL_VIEWPORT_ZOFFSET GRAS_CL_VIEWPORT_ZSCALE GRAS_CL_VS_CLIP_CULL_DISTANCE GRAS_LRZ_BUFFER_BASE
GRAS_LRZ_BUFFER_PITCH GRAS_LRZ_BUFFER_SLICE_PITCH GRAS_LRZ_CB_CNTL GRAS_LRZ_CNTL GRAS_LRZ_CNTL2
GRAS_LRZ_DEPTH_BUFFER_INFO GRAS_LRZ_FAST_CLEAR_BUFFER_BASE GRAS_LRZ_MRT_BUFFER_INFO_0 GRAS_LRZ_PS_INPUT_CNTL
GRAS_LRZ_PS_SAMPLEFREQ_CNTL GRAS_LRZ_QUALITY_LOOKUP_TABLE_REG GRAS_LRZ_VIEW_INFO GRAS_MODE_CNTL GRAS_RAST_CNTL
GRAS_SC_BIN_CNTL GRAS_SC_CNTL GRAS_SC_DEST_MSAA_CNTL GRAS_SC_MSAA_SAMPLE_POS_CNTL GRAS_SC_RAS_MSAA_CNTL
GRAS_SC_SCREEN_SCISSOR_BR GRAS_SC_SCREEN_SCISSOR_CNTL GRAS_SC_SCREEN_SCISSOR_TL GRAS_SC_VIEWPORT_SCISSOR_BR
GRAS_SC_VIEWPORT_SCISSOR_TL GRAS_SC_WINDOW_SCISSOR_BR GRAS_SC_WINDOW_SCISSOR_TL GRAS_SU_CNTL
GRAS_SU_CONSERVATIVE_RAS_CNTL GRAS_SU_DEPTH_BUFFER_INFO GRAS_SU_DEPTH_CNTL GRAS_SU_DEPTH_PLANE_CNTL
GRAS_SU_POINT_MINMAX GRAS_SU_POINT_SIZE GRAS_SU_POLY_OFFSET_OFFSET GRAS_SU_POLY_OFFSET_OFFSET_CLAMP
GRAS_SU_POLY_OFFSET_SCALE GRAS_SU_RENDER_CNTL GRAS_SU_STENCIL_CNTL GRAS_SU_VS_SIV_CNTL PC_ATTR_BUF_GMEM_SIZE
PC_BV_POS_BUF_GMEM_SIZE PC_CNTL PC_DGEN_RAST_CNTL PC_DGEN_SU_CONSERVATIVE_RAS_CNTL PC_HS_BUFFER_SIZE PC_MODE_CNTL
PC_POS_BUF_GMEM_SIZE PC_PS_CNTL PC_RESTART_INDEX PC_STEREO_RENDERING_CNTL PC_TF_BUFFER_SIZE PC_UNKNOWN_980B PC_VS_CNTL
PC_VS_INPUT_CNTL RB_A2D_BLT_CNTL RB_A2D_CLEAR_COLOR_DW0 RB_A2D_CLEAR_COLOR_DW1 RB_A2D_CLEAR_COLOR_DW2
RB_A2D_CLEAR_COLOR_DW3 RB_A2D_DEST_BUFFER_ARRAY_PITCH RB_A2D_DEST_BUFFER_BASE RB_A2D_DEST_BUFFER_INFO
RB_A2D_DEST_BUFFER_PITCH RB_A2D_PIXEL_CNTL RB_ALPHA_TEST_CNTL RB_BIN_FOVEAT RB_BLEND_CNTL RB_BLEND_CONSTANT_ALPHA_FP32
RB_BLEND_CONSTANT_BLUE_FP32 RB_BLEND_CONSTANT_GREEN_FP32 RB_BLEND_CONSTANT_RED_FP32 RB_BUFFER_CNTL RB_CCU_CACHE_CNTL
RB_CLEAR_TARGET RB_CNTL RB_COLOR_FLAG_BUFFER_ADDR RB_COLOR_FLAG_BUFFER_PITCH RB_DEPTH_BOUND_MAX RB_DEPTH_BOUND_MIN
RB_DEPTH_BUFFER_ARRAY_PITCH RB_DEPTH_BUFFER_BASE RB_DEPTH_BUFFER_INFO RB_DEPTH_BUFFER_PITCH RB_DEPTH_CNTL
RB_DEPTH_GMEM_BASE RB_DEPTH_GMEM_DIMENSION RB_DEPTH_PLANE_CNTL RB_DEST_MSAA_CNTL RB_DITHER_CNTL RB_INTERP_CNTL
RB_LB_PARAM_LIMIT RB_LRZ_CNTL RB_LRZ_CNTL2 RB_MODE_CNTL RB_MRT_ARRAY_PITCH RB_MRT_BASE RB_MRT_BASE_GMEM
RB_MRT_BLEND_CONTROL RB_MRT_BUF_INFO RB_MRT_CONTROL RB_MRT_GMEM_DIMENSION_REG RB_MRT_PITCH RB_MSAA_SAMPLE_POS_CNTL
RB_PS_CNTL RB_PS_INPUT_CNTL RB_PS_MRT_CNTL RB_PS_OUTPUT_CNTL RB_PS_OUTPUT_MASK RB_PS_SAMPLEFREQ_CNTL RB_RAS_MSAA_CNTL
RB_RENDER_CNTL RB_SAMPLE_COUNTER_CNTL RB_RESOLVE_CNTL_1 RB_RESOLVE_CNTL_2 RB_RESOLVE_CNTL_3 RB_RESOLVE_CNTL_5 RB_RESOLVE_GMEM_BUFFER_INFO
RB_RESOLVE_WINDOW_OFFSET RB_SRGB_CNTL RB_STENCIL_BUFFER_INFO RB_STENCIL_CNTL RB_STENCIL_GMEM_DIMENSION RB_STENCIL_MASK
RB_STENCIL_REF_CNTL RB_STENCIL_WRITE_MASK RB_UNKNOWN_8818 RB_UNKNOWN_8819 RB_UNKNOWN_881A RB_UNKNOWN_881B
RB_UNKNOWN_881C RB_UNKNOWN_881D RB_UNKNOWN_881E RB_VIEWPORT_ZCLAMP_MAX_REG RB_VIEWPORT_ZCLAMP_MIN_REG
RB_WINDOW_OFFSET SP_A2D_OUTPUT_INFO SP_ALPHA_TEST_CNTL SP_BIN_SIZE SP_BLEND_CNTL SP_DS_CONFIG SP_DS_CONST_CONFIG
SP_GFX_BINDLESS_BASE_DESCRIPTOR SP_GFX_USIZE SP_GS_CONFIG SP_GS_CONST_CONFIG SP_HLSQ_MODE_CNTL SP_HS_CONFIG SP_HS_CONST_CONFIG SP_LB_PARAM_LIMIT
SP_MODE_CNTL SP_MRT_BLEND_CNTL_REG SP_PERFCTR_SHADER_MASK SP_PS_BASE SP_PS_CNTL_0 SP_PS_CNTL_1 SP_PS_CONFIG
SP_PS_CONST_CONFIG SP_PS_INITIAL_TEX_LOAD_CNTL SP_PS_INSTR_SIZE SP_PS_MRT_CNTL SP_PS_MRT_REG SP_PS_OUTPUT_CNTL
SP_PS_OUTPUT_CONST_CNTL SP_PS_OUTPUT_CONST_MASK SP_PS_OUTPUT_MASK SP_PS_OUTPUT_REG SP_PS_PROGRAM_COUNTER_OFFSET
SP_PS_PVT_MEM_BASE SP_PS_PVT_MEM_PARAM SP_PS_PVT_MEM_SIZE SP_PS_PVT_MEM_STACK_OFFSET SP_PS_SAMPLER_BASE
SP_PS_TEXMEMOBJ_BASE SP_PS_TSIZE SP_PS_VGS_CNTL SP_PS_WAVE_CNTL SP_REG_PROG_ID_0 SP_REG_PROG_ID_1 SP_REG_PROG_ID_2
SP_REG_PROG_ID_3 SP_RENDER_CNTL SP_SRGB_CNTL SP_UNKNOWN_A9A8 SP_UNKNOWN_AB01 SP_UNKNOWN_AB23 SP_UPDATE_CNTL SP_VS_BASE
SP_VS_CNTL_0 SP_VS_CONFIG SP_VS_CONST_CONFIG SP_VS_INSTR_SIZE SP_VS_OUTPUT_CNTL SP_VS_OUTPUT_REG
SP_VS_PROGRAM_COUNTER_OFFSET SP_VS_PVT_MEM_BASE SP_VS_PVT_MEM_PARAM SP_VS_PVT_MEM_SIZE SP_VS_PVT_MEM_STACK_OFFSET
SP_VS_VGS_CNTL SP_VS_VPC_DEST_REG SP_WINDOW_OFFSET TPL1_A2D_BIN_SIZE TPL1_A2D_BLT_CNTL TPL1_A2D_SRC_TEXTURE_BASE
TPL1_A2D_SRC_TEXTURE_FLAG_BASE TPL1_A2D_SRC_TEXTURE_FLAG_PITCH TPL1_A2D_SRC_TEXTURE_INFO TPL1_A2D_SRC_TEXTURE_PITCH
TPL1_A2D_SRC_TEXTURE_SIZE TPL1_A2D_WINDOW_OFFSET TPL1_BIN_SIZE TPL1_CS_BORDER_COLOR_BASE TPL1_DEST_MSAA_CNTL
TPL1_GFX_BORDER_COLOR_BASE TPL1_MODE_CNTL TPL1_PS_ROTATION_CNTL TPL1_PS_SWIZZLE_CNTL TPL1_RAS_MSAA_CNTL
TPL1_WINDOW_OFFSET VFD_CNTL_0 VFD_CNTL_1 VFD_CNTL_2 VFD_CNTL_3 VFD_CNTL_4 VFD_CNTL_5 VFD_CNTL_6 VFD_DEST_CNTL_INSTR
VFD_FETCH_INSTR_INSTR VFD_FETCH_INSTR_STEP_RATE VFD_INDEX_OFFSET VFD_INSTANCE_START_OFFSET VFD_MODE_CNTL
VFD_RENDER_MODE VFD_STEREO_RENDERING_CNTL VFD_VERTEX_BUFFER_BASE VFD_VERTEX_BUFFER_SIZE VFD_VERTEX_BUFFER_STRIDE
VPC_ATTR_BUF_GMEM_BASE VPC_ATTR_BUF_GMEM_SIZE VPC_BV_POS_BUF_GMEM_BASE VPC_BV_POS_BUF_GMEM_SIZE VPC_PC_CNTL
VPC_POS_BUF_GMEM_BASE VPC_POS_BUF_GMEM_SIZE VPC_PS_CNTL VPC_PS_RAST_CNTL VPC_RAST_CNTL VPC_RAST_STREAM_CNTL
VPC_REPLACE_MODE_CNTL VPC_ROTATION_CNTL VPC_SO_CNTL VPC_SO_OVERRIDE VPC_UNKNOWN_CNTL VPC_VARYING_INTERP_MODE_MODE
VPC_VARYING_LM_TRANSFER_CNTL_DISABLE VPC_VARYING_REPLACE_MODE_MODE VPC_VS_CLIP_CULL_CNTL VPC_VS_CNTL VPC_VS_SIV_CNTL
""".split()

# PM4 packet payload dwords (adreno_pm4.xml domains, A8XX variants where the packet has several).
PKT_DOMAINS = """
CP_EVENT_WRITE7 CP_DRAW_INDX_OFFSET CP_LOAD_STATE6 CP_SET_DRAW_STATE_ CP_SET_MARKER CP_THREAD_CONTROL CP_BLIT
CP_INDIRECT_BUFFER CP_WAIT_TIMESTAMP CP_SET_MODE
""".split()

# Descriptor layouts (a8xx_descriptors.xml / a6xx_descriptors.xml).
DESC_DOMAINS = ('A8XX_TEX_MEMOBJ', 'A8XX_TEX_SAMP', 'A6XX_UBO')

ENUM_NAMES = """
a3xx_color_swap a3xx_msaa_samples a3xx_rop_code a3xx_rb_blend_opcode a6xx_2d_ifmt a6xx_a2d_pixel_op a6xx_buffers_location
a6xx_ccu_cache_size a6xx_bindless_descriptor_size a6xx_format a6xx_fragcoord_sample_mode a6xx_isam_mode a6xx_coord_round a6xx_nearest_mode
a6xx_lrz_feedback_mask a6xx_marker a6xx_polygon_mode a6xx_raster_direction a6xx_raster_mode a6xx_render_mode
a6xx_rotation a6xx_sequenced_thread_dist a6xx_single_prim_mode a6xx_sp_a2d_output_ifmt_type a6xx_state_block
a6xx_state_src a6xx_state_type a6xx_tex_aniso a6xx_tex_clamp a6xx_tex_filter a6xx_tex_type a6xx_threadsize
a6xx_tile_mode a6xx_ztest_mode a6xx_depth_format a6xx_reduction_mode a6xx_fast_border_color a6xx_shift_amount
a8xx_tex_swiz adreno_compare_func adreno_rb_blend_factor adreno_rb_dither_mode vgt_event_type pc_di_primtype set_marker_mode
pc_di_src_sel pc_di_vis_cull_mode a4xx_index_size a6xx_patch_type adreno_pm4_type3_packets ts_wait_value_src ts_wait_type
event_write_src event_write_dst cp_thread cp_blit_cmd a7xx_blit_clear_mode a6xx_lrz_dir_status a5xx_line_mode a3xx_threadmode a6xx_tess_output
""".split()


def covers(variant, chip='A8XX'):
    if variant is None:
        return True
    for v in variant.split(','):
        if '-' in v:
            a, b = v.split('-')
            if ORDER[a] <= ORDER[chip] and (not b or ORDER[chip] <= ORDER[b]):
                return True
        elif v == chip:
            return True
    return False


def load(xml):
    p = G.Parser()
    p.parse(REGS_DIR, os.path.join(REGS_DIR, 'adreno', xml), False)
    return p


def variant_of(p, reg):
    for v, r in p.variant_regs.get(reg.name, {}).items():
        if r is reg:
            return v
    return reg.array.variant if reg.array is not None else None


def fields(bitset):
    out = []
    for f in bitset.fields:
        out.append((f.name, f.low, f.high, f.shr, f.type or 'uint', getattr(f, 'radix', 0)))
    return tuple(out)


def regs_of(p, chip='A8XX', by_full=False):
    out = {}
    for e in p.file:
        regs = [e] if isinstance(e, G.Reg) else (e.children if isinstance(e, G.Array) else [])
        for r in regs:
            v = variant_of(p, r)
            if not covers(v, chip):
                continue
            info = {'offset': r.total_offset(), 'bits': r.bit_size, 'variant': v, 'fields': fields(r.bitset),
                    'domain': r.domain}
            arr = r.array
            if arr is not None and not arr.fixed_offsets and arr.length > 1:
                info['stride'], info['length'] = arr.stride, arr.length
            key = r.full_name if by_full else r.name
            prev = out.get(key)
            if prev and prev['variant'] and v is None:
                continue
            out[key] = info
    return out


def xml_enums(names):
    """Enum values straight from the XML, keeping only the values whose `variants` cover A8XX (gen_header's Enum
    drops that attribute; vgt_event_type, for one, has chip-specific names on the same numbers)."""
    import xml.etree.ElementTree as ET
    found = {}
    folder = os.path.join(REGS_DIR, 'adreno')
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith('.xml'):
            continue
        root = ET.parse(os.path.join(folder, fname)).getroot()
        for enum in root.iter():
            if enum.tag.split('}')[-1] != 'enum' or enum.get('name') not in names or enum.get('name') in found:
                continue
            values, nxt = {}, 0
            for v in enum:
                if v.tag.split('}')[-1] != 'value':
                    continue
                value = int(v.get('value'), 0) if v.get('value') is not None else nxt
                nxt = value + 1
                if covers(v.get('variants')):
                    values[v.get('name')] = value
            found[enum.get('name')] = values
    missing = [n for n in names if n not in found]
    if missing:
        sys.exit(f'enums not found: {missing}')
    return found


def mesa_notice():
    """The copyright and licence of the register database, verbatim from freedreno_copyright.xml."""
    import xml.etree.ElementTree as ET
    ns = '{http://nouveau.freedesktop.org/}'
    c = ET.parse(os.path.join(REGS_DIR, 'freedreno_copyright.xml')).getroot().find(ns + 'copyright')
    out = [f'Copyright (C) {c.get("year")} by the following authors:']
    for a in c.findall(ns + 'author'):
        out.append(f'- {a.get("name")} <{a.get("email")}> ({" ".join("".join(a.itertext()).split())})')
    return out + [''] + c.find(ns + 'license').text.strip('\n').split('\n')


def main():
    a6 = regs_of(load('a6xx.xml'))
    missing = [n for n in REG_NAMES if n not in a6]
    if missing:
        sys.exit(f'not in a6xx.xml for A8XX: {missing}')
    pm4 = load('adreno_pm4.xml')
    pkt = regs_of(pm4, by_full=True)
    desc = {}
    for xml in ('a8xx_descriptors.xml', 'a6xx_descriptors.xml'):
        p = load(xml)
        for name, info in regs_of(p, by_full=True).items():
            desc.setdefault(name, info)
    enums = xml_enums(ENUM_NAMES)
    lines = ['# SPDX-License-Identifier: MIT', '#',
             '# Generated from Mesa\'s freedreno register database (src/freedreno/registers), which carries',
             '# this notice (freedreno_copyright.xml):', '#',
             *[('# ' + line).rstrip() for line in mesa_notice()], '',
             '"""GENERATED by tools/a16gpu-p7/gen_a8xx_regs.py from Mesa bbeb2dca9fee src/freedreno/registers (the',
             'A8XX variant of every register, with its fields). Do not edit; regenerate. name -> (dword offset, bits,',
             'fields, array stride, array length); a field is (name, low, high, shr, type, radix)."""', '']
    lines.append('REGS = {')
    for n in sorted(REG_NAMES):
        i = a6[n]
        lines.append(f"    {n!r}: ({i['offset']:#07x}, {i['bits']}, {i['fields']!r}, {i.get('stride', 0)}, "
                     f"{i.get('length', 1)}),")
    lines.append('}')
    lines.append('')
    lines.append('PKT = {')
    for n in sorted(pkt):
        if any(d in n for d in PKT_DOMAINS):
            i = pkt[n]
            lines.append(f"    {n!r}: ({i['offset']}, {i['bits']}, {i['fields']!r}),")
    lines.append('}')
    lines.append('')
    lines.append('DESC = {')
    for n in sorted(desc):
        i = desc[n]
        if any(n.startswith(d + '_') for d in DESC_DOMAINS):
            lines.append(f"    {n!r}: ({i['offset']}, {i['bits']}, {i['fields']!r}),")
    lines.append('}')
    lines.append('')
    lines.append('ENUMS = {')
    for n in sorted(enums):
        lines.append(f'    {n!r}: {enums[n]!r},')
    lines.append('}')
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'a8xx_regs.py')
    open(out, 'w').write('\n'.join(lines) + '\n')
    print(f'wrote {out}: {len(REG_NAMES)} registers, {len(enums)} enums')


if __name__ == '__main__':
    main()
