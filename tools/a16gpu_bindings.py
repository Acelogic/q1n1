# SPDX-License-Identifier: MIT
"""air2ir3 BINDING-ABI v6, the command stream's half (Stage 6 fix round 4 `adreno-stage6/fix4/` plus the A16 render
run 1 fix `adreno-render-fix/air2ir3-abi6.patch`: every UBO index below 16).

Everything `render` (tools/a16gpu_render.py) needs to know about *where* a Metal resource of a compiled variant
goes lives here, and nowhere else: the UBO table (constant buffers at their Metal index, the driver table at the UBO
the sidecar names -- 1 + the highest declared constant buffer -- with device-buffer addresses at 8n; a8xx honours
UBO 0..15 only), the T#/S# counts (NTEX and NSAMP separately, ABI v4) and slots (API textures/samplers at their
index, constexpr samplers at the slots the sidecar names, T# holes zero, S# holes the default sampler, ABI v5),
the framebuffer-fetch descriptors (bindless set 4, 55 + n),
attachment and texture format classes (0 native; 1 BGR10_XR and 2 BGRA10_XR lowered as 4.5 says, dual source and
classes 3-5 refused), private memory (refused: the arena cannot hold the BO), driver UBOs (constant data bound, driver params refused), the pipeline const budget (5.2) and
the varyings linked by name (4.1). The command-stream builder asks these functions and never reads a sidecar itself.

A16 render run 1 (2026-09-25) found the v3 driver table at UBO 31 read back as zeros (CoreDisplay's TextureCopy VS
loaded its vertex pointer from it and faulted at VA 0): freedreno advertises 16 constant buffers and Turnip binds
application UBOs bindless, so no Mesa driver uses a non-bindless UBO above 15. ABI v6 keeps every UBO below 16.
"""
from __future__ import annotations

import struct

ABI_VERSION = 6                   # BINDING-ABI.md version this module implements (fix4 + air2ir3-abi6.patch)
CHIP_ID = '0x44070041'
UBO_SLOTS = 16                    # 3.1/3.9 (v6): the non-bindless UBO descriptors a8xx honours (A16 run 1)
DRIVER_TABLE_BYTES = 384          # 3.2: "Bind 384 bytes"; device-buffer addresses u64 at 8 * metal index
FS_BINDLESS_SET = 4               # 3.6: ir3_shader_descriptor_set(MESA_SHADER_FRAGMENT)
FB_FETCH_BASE = 55                # 3.6: 32 + 32 - 1 - 8
BINDLESS_DESC_COUNT = 64          # IR3_BINDLESS_DESC_COUNT: descriptors in a set (64 bytes each)
MAX_CONST_PIPELINE = 512          # 5.2: max_const_pipeline (vec4)
MAX_TEX = 16                      # T#/S# per stage (3.3/3.4)
DEFAULT_SAMPLER = {'min': 'nearest', 'mag': 'nearest', 'address': 'clamp_to_edge'}   # 3.4 (v5): S# holes below NSAMP
PVTMEM_SUPPORTED = 0              # 3.10: the BO is at least 16 MiB (8 SPs x 4096 fibers x 512 B): not in the arena


class ABIError(Exception):
    """A variant (or a scene) outside what this port of the ABI builds: refused, never approximated."""


def _hex(v):
    return int(v, 16) if isinstance(v, str) else int(v)


def sidecar(v):
    return v['sidecar']


def bindings(v, kind=None):
    out = [b for b in sidecar(v)['reflection']['bindings'] if kind is None or b['kind'] == kind]
    return out


def stats(v):
    return sidecar(v)['ir3']['stats']


def check_variant(v, stage, probe=False):
    """The ABI facts a variant must satisfy to be built at all. Returns a list of problems (empty: buildable).
    `probe`: an A16 probe scene compiled with --test-ubo-slots (UBO indices up to 31 accepted, only there)."""
    p = []
    sc = sidecar(v)
    if sc.get('abi_version') != ABI_VERSION:
        p.append(f'{stage}: sidecar abi_version {sc.get("abi_version")}, this builder implements {ABI_VERSION}')
        return p
    slots = stats(v).get('ubo_slots')
    if slots != UBO_SLOTS and not (probe and slots == 32):
        p.append(f'{stage}: compiled for {slots} UBO slots; a8xx honours {UBO_SLOTS} (--test-ubo-slots is for probes)')
    limit = 32 if probe else UBO_SLOTS
    if v['const_state']['num_app_ubos'] > limit:
        p.append(f'{stage}: {v["const_state"]["num_app_ubos"]} UBO descriptors, above the {limit} a8xx honours')
    try:
        dt = driver_table_ubo(v)
    except ABIError as problem:
        p.append(f'{stage}: {problem}')
        dt = None
    if dt is not None and not 0 <= dt < limit:
        p.append(f'{stage}: driver table at UBO {dt}, outside 0..{limit - 1}')
    if sc.get('chip_id') != CHIP_ID:
        p.append(f'{stage}: compiled for {sc.get("chip_id")}, not the X2-90 {CHIP_ID}')
    refl = sc['reflection']
    if any(c > 2 for c in sc['key_inputs'].get('rt_classes', [])) or \
            any(c > 2 for c in sc['key_inputs'].get('texture_classes', [])):
        p.append(f'{stage}: format class 3/4/5 (550, packed YCbCr, XR sRGB): conversion blits, not in this port')
    for lw in refl['rt_lowering']:
        if lw['dual_source']:
            p.append(f'{stage}: attachment {lw["index"]} lowered with dual-source blending: not built here')
        if lw['class'] not in (1, 2):
            p.append(f'{stage}: attachment {lw["index"]} lowering class {lw["class"]}')
    for b in refl['bindings']:
        if b['kind'] in ('storage_texture', 'threadgroup_buffer'):
            p.append(f'{stage}: {b["kind"]} binding {b["metal_index"]}: not in this port (no WindowServer function)')
        if b['kind'] in ('texture', 'sampler', 'constexpr_sampler') and not 0 <= b['slot'] < MAX_TEX:
            p.append(f'{stage}: {b["kind"]} slot {b["slot"]} outside 0..15')
        if b['kind'] == 'ubo' and not 0 <= b['slot'] < limit:
            p.append(f'{stage}: constant buffer slot {b["slot"]} outside 0..{limit - 1} (a8xx honours UBO 0..15)')
    ir3 = sc['ir3']
    if ir3['pvtmem']['pvtmem_size'] > PVTMEM_SUPPORTED or v['pvtmem_size']:
        p.append(f'{stage}: needs {v["pvtmem_size"]} bytes/fiber of private memory (BINDING-ABI 3.10: a BO of '
                 f'{ir3["pvtmem"]["total_size"]} bytes, which the 32 MiB arena cannot hold)')
    for d in ir3['driver_ubos']:
        if d['kind'] != 'constant_data':
            p.append(f'{stage}: driver UBO {d["kind"]} at {d.get("ubo")}: driver params are not built here')
    cs = v['const_state']
    if cs['driver_params_ubo'] != -1 or cs['primitive_map_ubo'] != -1 or cs['primitive_param_ubo'] != -1:
        p.append(f'{stage}: ir3 driver-param / primitive UBOs')
    if v['shared_size'] or v['num_uavs'] or v['has_ssbo']:
        p.append(f'{stage}: shared memory, UAVs or SSBOs')
    if stage == 'vs' and (v['clip_mask'] or v['cull_mask'] or v['stream_outputs'] or v['writes_psize']):
        p.append('vs writes clip/cull distances, stream output or point size')
    if stage == 'fs':
        if sc['reflection'].get('requires_no_attachment'):
            # 3.7 (v5): valid only for pipelines without those attachments; every scene here writes attachment 0
            p.append(f'fs requires no attachment at {sc["reflection"]["requires_no_attachment"]} (unset constant)')
        if v['num_sampler_prefetch'] or v['sample_shading'] or v['frag_face'] or v['reads_primid'] or \
                v['dual_src_blend'] or v['yuv_color'] or v['color0_mrt'] or v['post_depth_coverage']:
            p.append('fs uses prefetch, sample shading, face, primitive id, dual source, yuv or color0_mrt')
        if v['bindless'][1:] != [0, 0, 0] or (v['bindless'][0] and not v['fb_read']):
            p.append('fs uses bindless state other than framebuffer fetch')
    for rng in cs['ubo_ranges']:
        if rng['bindless'] or rng['offset'] + (rng['end'] - rng['start']) > 16 * v['constlen']:
            p.append(f'{stage}: UBO {rng["block"]} bytes {rng["start"]}..{rng["end"]} pushed to const {rng["offset"]} '
                     f'beyond constlen {v["constlen"]} vec4')
    if v['constlen'] > 512 or v['constlen'] % 4:
        p.append(f'{stage}: constlen {v["constlen"]} is not a multiple of 4 up to 512')
    return p


def check_pipeline(pipe):
    """5.2 const budget (the variants actually bound) and 4.1 varyings by name."""
    p = []
    total = pipe['vs']['constlen'] + pipe['fs']['constlen']
    if total > MAX_CONST_PIPELINE:
        p.append(f'VS + FS constlen {total} > {MAX_CONST_PIPELINE} (the trim should have picked safe variants)')
    vs_names = {v['name']: 32 + v['slot'] for v in sidecar(pipe['vs'])['reflection']['varyings']}
    for v in sidecar(pipe['fs'])['reflection']['varyings']:
        if v['name'] not in vs_names:
            p.append(f'FS varying {v["name"]} has no VS output of that name')
        elif {'fs_slot': 32 + v['slot'], 'vs_slot': vs_names[v['name']], 'name': v['name']} not in pipe['varying_map']:
            p.append(f'varying {v["name"]}: the exported linkage does not pair FS {32 + v["slot"]} with VS '
                     f'{vs_names[v["name"]]}')
        if v['interp'] != 'perspective' or v['sampling'] != 'center':
            p.append(f'varying {v["name"]}: {v["interp"]}/{v["sampling"]} interpolation not built here')
    return p


def texture_counts(v):
    """(NTEX, NSAMP) for SP_xS_CONFIG and the number of T# / S# descriptors to load (3.4).
    ABI v4+ reports the counts separately (`ir3.stats.ntex` = 1 + the highest non-bindless T#, `ir3.stats.nsamp` =
    1 + the highest S#); ir3's `num_samp` is max(ntex, nsamp) plus one per framebuffer read. This port programs
    Mesa's single count, NTEX = NSAMP = num_samp (gallium sp_xs_config / Turnip), which covers both and is the value
    the A16 proved (P7) and Mesa's captures show (gl-fbfetch: NTEX = NSAMP = 1 for a FS whose only texture access
    is the bindless fetch). The descriptors behind it: T# holes zero (gallium's build_texture_state), S# holes the
    default sampler (v5). Capped at the 16 of a stage (a fetching FS may report one more). Older sidecars (P7's v2
    pair, fix2's v3) keep the v3 rule below."""
    if sidecar(v).get('abi_version', 0) >= 4:
        st = stats(v)
        ntex, nsamp = st['ntex'], st['nsamp']
        if not (0 <= ntex <= MAX_TEX and 0 <= nsamp <= MAX_TEX):
            raise ABIError(f'NTEX {ntex} / NSAMP {nsamp}: a stage has {MAX_TEX} T# and {MAX_TEX} S#')
        t = [b['slot'] for b in bindings(v) if b['kind'] == 'texture' and (b['present'] or b['used'])]
        s = [b['slot'] for b in bindings(v) if b['kind'] in ('sampler', 'constexpr_sampler') and (b['present'] or b['used'])]
        if (t and ntex < 1 + max(t)) or (s and nsamp < 1 + max(s)):
            raise ABIError(f'NTEX {ntex} / NSAMP {nsamp} below the highest bound T# {max(t, default=-1)} / S# '
                           f'{max(s, default=-1)}')
        n = v['num_samp']
        if n < max(ntex, nsamp) or (n > max(ntex, nsamp) and not fb_fetch(v)):
            raise ABIError(f'num_samp {n} is not explained by NTEX {ntex} / NSAMP {nsamp}')
        n = min(n, MAX_TEX)
        return n, n
    n = v['num_samp']
    declared = [b['slot'] for b in bindings(v) if b['kind'] in ('texture', 'sampler', 'constexpr_sampler')
                and (b['present'] or b['used'])]
    if declared and n < 1 + max(declared):
        raise ABIError(f'num_samp {n} is below the highest bound T#/S# {max(declared)}')
    # review: a framebuffer-fetch FS with a constexpr sampler at S#15 reports num_samp 17 in v3 (fix3's
    # BINDING-ABI v4 3.4: "26 variants then asked for 17"; the extra one is the bindless fb read, not a T#/S#).
    # A stage has 16 T# and 16 S#: never program or load more (every declared slot is below 16, checked above
    # and in check_variant).
    if n > MAX_TEX:
        # only the one extra count of the bindless fetch is dropped; anything else above 16 is refused
        if n > MAX_TEX + 1 or not fb_fetch(v):
            raise ABIError(f'num_samp {n} is above the {MAX_TEX} T#/S# of a stage')
        n = MAX_TEX
    return n, n


def texture_slots(v):
    """{T# slot: metal texture index} for every texture binding the code reads (present, or absent but used: 3.7
    binds the app's object there)."""
    return {b['slot']: b['metal_index'] for b in bindings(v, 'texture') if b['present'] or b['used']}


def sampler_slots(v):
    """({S# slot: metal sampler index} for API samplers, {S# slot: a8xx_tex_samp words} for constexpr samplers)."""
    api = {b['slot']: b['metal_index'] for b in bindings(v, 'sampler') if b['present'] or b['used']}
    const = {s['slot']: [_hex(w) for w in s['a8xx_tex_samp']] for s in sidecar(v)['constant_samplers']['samplers']}
    clash = set(api) & set(const)
    if clash:
        raise ABIError(f'API and constexpr samplers share S# {sorted(clash)}')
    mask = sum(1 << s for s in const)
    if mask != _hex(sidecar(v)['reflection']['constexpr_sampler_mask']):
        raise ABIError('constexpr sampler slots differ from constexpr_sampler_mask')
    return api, const


def driver_table_ubo(v):
    """3.2 (v6): the UBO the driver table is bound at, from the sidecar (every device/threadgroup binding names the
    same one; ir3.stats.driver_table_ubo agrees when the code reads it); None without device buffers."""
    at = {b['driver_table_ubo'] for b in bindings(v) if b['kind'] in ('device_buffer', 'threadgroup_buffer')}
    if not at:
        return None
    if len(at) != 1:
        raise ABIError(f'device buffers name different driver tables {sorted(at)}')
    dt = at.pop()
    st = stats(v).get('driver_table_ubo', -1)
    if st not in (-1, dt):
        raise ABIError(f'bindings name driver table UBO {dt}, ir3.stats {st}')
    return dt


def ubo_table(v):
    """The UBO descriptors of the stage: count, and what goes where. Count = const_state.num_app_ubos (ir3 loads that
    many: 1 + the driver table's index when there is one, ABI v6). Returns (count, {slot: ('buffer', metal index) |
    ('driver_table',) | ('constant_data', offset, size)}). Every `ubo` binding is bound, used or not (3.9, step 1)."""
    count = v['const_state']['num_app_ubos']
    table = {}
    for b in bindings(v, 'ubo'):
        if b['present'] or b['used']:
            table[b['slot']] = ('buffer', b['metal_index'])
    dt = driver_table_ubo(v)
    if dt is not None:
        if dt in table:
            raise ABIError(f'driver table UBO {dt} collides with constant buffer({table[dt][1]})')
        table[dt] = ('driver_table',)
    for d in sidecar(v)['ir3']['driver_ubos']:
        if d['kind'] == 'constant_data':
            table[d['ubo']] = ('constant_data', v['info']['constant_data_offset'], 16 * d['size_vec4'])
    if table and max(table) >= count:
        raise ABIError(f'UBO {max(table)} is beyond the {count} descriptors ir3 loads')
    return count, table


def ubo_min_bytes(v, slot):
    """The bytes the code may read from UBO `slot`: the preamble's pushed ranges (so the descriptor size covers them)."""
    ends = [r['end'] for r in v['const_state']['ubo_ranges'] if r['block'] == slot]
    return max(ends, default=0)


def driver_table(v, address_of):
    """The driver table's contents: u64 VA of device buffer n at 8n (3.2), zero elsewhere; threadgroup offsets none."""
    blob = bytearray(DRIVER_TABLE_BYTES)
    for b in bindings(v, 'device_buffer'):
        if b['present'] or b['used']:
            struct.pack_into('<Q', blob, b['slot'], address_of(b['metal_index']))
    return bytes(blob)


def fb_fetch(v):
    """[(colour attachment n, bindless descriptor index 55 + n)] for a FS that reads its attachments (3.6)."""
    out = []
    for b in bindings(v, 'fb_fetch'):
        if b.get('bindless_set', FS_BINDLESS_SET) != FS_BINDLESS_SET or b['slot'] != FB_FETCH_BASE + b['metal_index']:
            raise ABIError(f'framebuffer fetch of colour {b["metal_index"]} at set {b.get("bindless_set")} slot '
                           f'{b["slot"]}: not 4 / 55 + n')
        out.append((b['metal_index'], b['slot']))
    return out


def render_targets(v):
    """[(attachment index, written)] from the reflection (4.4)."""
    return [(r['index'], r['written']) for r in sidecar(v)['reflection']['render_targets']]


def vertex_attributes(v):
    """{VS input number i (VFD instruction i): Metal attribute index} -- ir3's non-sysval inputs in order, slot =
    VERT_ATTRIB_GENERIC0 (15) + attribute (4.2)."""
    out = []
    for i in v['inputs']:
        if i['sysval']:
            continue
        if i['slot'] < 15:
            raise ABIError(f'VS input slot {i["slot"]} is not a generic attribute')
        out.append(i['slot'] - 15)
    return out


def system_values(v):
    return list(sidecar(v)['reflection']['system_values'])


def rt_classes(v):
    """The attachment format classes the variant was compiled for (CACHE-FORMAT 2.2: 0 native, 1 BGR10_XR,
    2 BGRA10_XR); the command stream binds each attachment as that class or refuses."""
    return list(sidecar(v)['key_inputs'].get('rt_classes', [0] * 8))


def texture_classes(v):
    """The draw-time texture classes the variant was compiled for, T# 0..15 (BINDING-ABI 3.3)."""
    return list(sidecar(v)['key_inputs'].get('texture_classes', [0] * 16))


def rt_lowering(v):
    """{attachment: rt_lowering entry} (BINDING-ABI 4.5): the view format, the blend state to program (`hw_blend`,
    off when `blend_in_shader`), dual source, and the write mask to clear."""
    return {lw['index']: lw for lw in sidecar(v)['reflection']['rt_lowering']}
