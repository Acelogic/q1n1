# SPDX-License-Identifier: MIT
"""The Stage 6 render scenes: the 28 WindowServer functions of wscts with realistic inputs (one source of truth).

Stage 6's gate (plan "Stage 6"): the A16 renders the 28 wscts functions -- the 25 SkyLight and 3 CoreDisplay
fragment functions that wscts pairs with a vertex function (Snapintosh scratch
`adreno/windowserver-metal-usage/cts/ref-m3max.txt`, wscts.m) -- *after they are given realistic uniforms*: wscts
itself filled every float buffer with 0.25 and left every function constant unset, so about half of its references
are one flat colour. Here every scene gives each function what WindowServer gives it: a window-like or photo-like
texture, real colour-management curves and matrices (sRGB, Display P3, BT.709), shadow densities, a Lanczos 2x
downscale step, blue noise at the fragment position, a debug fill with its borders, a ripple at a given time, and
it sets every function constant the function declares (0 unless named here; Metal treats an unset constant as
undef, which makes a golden meaningless).

The same bytes go to three places:
  * tools/a16gpu_render.py uploads them to the arena and draws them on the A16 (`render`);
  * `python3 tools/a16gpu_render_scenes.py --write-scenes DIR` writes them for
    tools/a16gpu-render/render_metal_golden.swift, which renders the goldens with the OS metallibs on the M3 Max;
  * tools/a16gpu-render/make-render-shaders.sh reads the function constants from the same scene list.

Coordinates: the target is 96x64; vertices are in pixels (row 0 at the top) and the mvp is the orthographic
matrix Stage 6's scenes use (column-major float4x4, as Metal stores it). Every target starts from a known image
(uploaded, Metal loadAction .load): a background gradient, or for the framebuffer-fetch functions a premultiplied
image with varied alpha (their input). Nothing here imports the rest of the q1n1 tools.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import sys

W, H = 96, 64
SKYLIGHT_METALLIB = '/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/Resources/SkyLightShaders.air64.metallib'
COREDISPLAY_METALLIB = '/System/Library/Frameworks/CoreDisplay.framework/Versions/A/Resources/default.metallib'
PROBES = Path(__file__).resolve().parent / 'a16gpu-render' / 'probes'
LIBS = {'SkyLight': SKYLIGHT_METALLIB, 'CoreDisplay': COREDISPLAY_METALLIB, 'Probe': str(PROBES / 'probes.metallib')}
# sha256 of the OS metallibs (Stage 6 corpus/index.json; macOS 27 26A428, identical on the M3 Max and in the A16's
# BaseSystem). The golden program refuses other bytes.
LIB_SHA256 = {'SkyLight': '9c67e83e8bf9578e8161ed769cfdcabd30e4deeadd84c03a6d223e0200494183',
              'CoreDisplay': 'f6d427d0d645da6dc784ba92ad9658320fa4a6247927f549096c98bfaf846121',
              # the A16 probes (a16gpu-render/probes/make-probes.sh from probes.metal, this Mac's toolchain)
              'Probe': '5a7eb1e12c2f09a622dbec058d2886686ec24ac84a98a7c71093492e1b9f88c5'}
PROBE_AIR_SHA256 = '7b7beec8db1959ca4bf28d86b84736785952e3b7aacabb3fa4b9c26a132c3d03'   # probes.air: what air2ir3 compiles

# Metal pixel formats the scenes use (MTLPixelFormat numbers for the compiler's --rt-format)
RT_FORMATS = {'bgra8': {'mtl': 80, 'bpp': 4}, 'r8': {'mtl': 10, 'bpp': 1}, 'rg8': {'mtl': 30, 'bpp': 2},
              'bgr10_xr': {'mtl': 554, 'bpp': 4, 'class': 1}, 'bgra10_xr': {'mtl': 552, 'bpp': 8, 'class': 2}}
TEX_FORMATS = {'rgba8': 4, 'rgba16f': 8, 'bgr10_xr': 4, 'bgra10_xr': 8, 'a8': 1}
XR_CLASS = {'bgr10_xr': 1, 'bgra10_xr': 2}      # BINDING-ABI 4.5 / CACHE-FORMAT 2.2 format classes


# ------------------------------------------------------------------------------------------------------------
# small packers
# ------------------------------------------------------------------------------------------------------------
def f32(*values):
    return struct.pack(f'<{len(values)}f', *values)


def u32(*values):
    return struct.pack(f'<{len(values)}I', *values)


def i32(*values):
    return struct.pack(f'<{len(values)}i', *values)


def pad(blob, align=16):
    return blob + bytes(-len(blob) % align)


def ortho(w=W, h=H):
    """Pixel space (origin top-left) -> NDC; column-major float4x4 as Metal stores it (Stage 6 scenes.py)."""
    return (2.0 / w, 0.0, 0.0, 0.0, 0.0, -2.0 / h, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, -1.0, 1.0, 0.0, 1.0)


def mat3x4_rows(m):
    """metal::float3x4-ish storage used by ColorCorrection*_FragmentArgs: 3 rows of float4 (row . float4(rgb, 1))."""
    return b''.join(f32(*row) for row in m)


def half(x):
    return struct.unpack('<H', struct.pack('<e', x))[0]


def clamp01(x):
    return min(1.0, max(0.0, x))


def u8(x):
    return int(clamp01(x) * 255.0 + 0.5)


# ------------------------------------------------------------------------------------------------------------
# textures (RGBA in memory, as MTLPixelFormatRGBA8Unorm / RGBA16Float store them)
# ------------------------------------------------------------------------------------------------------------
def _smooth(e0, e1, x):
    t = clamp01((x - e0) / (e1 - e0))
    return t * t * (3 - 2 * t)


def tex_window(w=64, h=64):
    """A premultiplied window: rounded-corner alpha with a 1.5-pixel soft edge, a title bar with three traffic
    lights, a light body with lines of 'text' and a photo-ish inset. Many distinct values, edges both hard and soft."""
    out = bytearray()
    r = 7.0
    for y in range(h):
        for x in range(w):
            cx, cy = x + 0.5, y + 0.5
            dx = max(r - cx, cx - (w - r), 0.0)
            dy = max(r - cy, cy - (h - r), 0.0)
            dist = math.hypot(dx, dy) - r
            a = clamp01(0.5 - dist / 1.5) * (0.92 if y > 12 else 1.0)
            if y < 12:                                            # title bar
                c = (0.86 - 0.004 * y, 0.86 - 0.004 * y, 0.88 - 0.003 * y)
                for k, col in enumerate(((0.99, 0.37, 0.34), (0.99, 0.74, 0.18), (0.16, 0.79, 0.25))):
                    if math.hypot(cx - (8 + 8 * k), cy - 6) < 2.8:
                        c = col
            elif 18 <= x < 42 and 18 <= y < 44:                   # an image inset
                u, v = (x - 18) / 24, (y - 18) / 26
                c = (0.2 + 0.7 * u, 0.35 + 0.5 * math.sin(3.1 * v) ** 2, 0.8 - 0.6 * u * v)
            elif 46 <= x < 60 and (y - 16) % 5 < 2 and y < 58:     # text lines
                c = (0.12 + 0.01 * x % 3, 0.13, 0.16)
            else:
                c = (0.97 - 0.002 * y, 0.97 - 0.001 * x, 0.99)
            out += bytes((u8(c[0] * a), u8(c[1] * a), u8(c[2] * a), u8(a)))
    return bytes(out)


def tex_photo(w=64, h=64, fmt='rgba8', peak=1.0):
    """An opaque 'photo': smooth colour fields plus a sharp-edged disk and a thin diagonal line (detail a filter
    or a downscale must preserve). With fmt rgba16f and peak > 1, extended-range (EDR) highlights."""
    out = bytearray()
    for y in range(h):
        for x in range(w):
            u, v = (x + 0.5) / w, (y + 0.5) / h
            r = 0.5 + 0.45 * math.sin(6.2 * u + 1.1 * v)
            g = 0.45 + 0.4 * math.sin(4.0 * v + 0.7) * math.cos(2.0 * u)
            b = 0.55 + 0.4 * math.cos(5.0 * u * v + 0.3)
            if math.hypot(u - 0.68, v - 0.36) < 0.16:              # a bright disk
                k = peak
                r, g, b = 0.98 * k, 0.9 * k, 0.55 * k
            if abs((x - y * w / h) - w * 0.2) < 0.9:                # a thin line
                r, g, b = 0.08, 0.1, 0.12
            if fmt == 'rgba16f':
                out += struct.pack('<4H', half(r), half(g), half(b), half(1.0))
            else:
                out += bytes((u8(r), u8(g), u8(b), 255))
    return bytes(out)


def tex_shadow_mask(w=64, h=64, soft=0.0):
    """A window silhouette for the shadow passes: white premultiplied alpha of a rounded rectangle inset by 12
    pixels (room for the blur to spread), optionally pre-softened (the composited shadow)."""
    out = bytearray()
    r = 6.0
    for y in range(h):
        for x in range(w):
            cx, cy = x + 0.5, y + 0.5
            dx = max(12 + r - cx, cx - (w - 12 - r), 0.0)
            dy = max(12 + r - cy, cy - (h - 12 - r), 0.0)
            dist = math.hypot(dx, dy) - r
            a = clamp01(0.5 - dist / 1.0) if not soft else 1.0 - _smooth(-soft, soft, dist)
            out += bytes((u8(a), u8(a), u8(a), u8(a)))
    return bytes(out)


SHADOW_HPASS = Path(__file__).resolve().parent / 'a16gpu-render' / 'shadow-hpass-m3max.rgba'
SHADOW_HPASS_SHA256 = '25450fe086ce807150ccc9f973407b83701ec7c380df60154c97f5337b72cb55'   # --derive-shadow-input (README.md)


def tex_shadow_hpass():
    """The vertical shadow passes' input: what the horizontal pass writes (B: the silhouette, G: the horizontal
    blur, R: the rim; the vertical pass reads that encoding), i.e. the M3 Max output of the horizontal scene's quad
    (x 16..80, 64x64) as an RGBA8 texture. A fixed, sha-pinned file: both sides get the same bytes."""
    data = SHADOW_HPASS.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if SHADOW_HPASS_SHA256 and digest != SHADOW_HPASS_SHA256:
        raise ValueError(f'{SHADOW_HPASS} sha256 {digest} is not the pinned {SHADOW_HPASS_SHA256}')
    if len(data) != 64 * 64 * 4:
        raise ValueError(f'{SHADOW_HPASS} is {len(data)} bytes, not 64x64 RGBA8')
    return data


def derive_shadow_input(golden_dir):
    """From the horizontal scene's golden (BGRA8 96x64): the 64x64 quad as RGBA8 (the texture WindowServer's
    vertical pass samples)."""
    raw = (Path(golden_dir) / 'ShadowHorizontalBlurFragment.rt0.raw').read_bytes()
    out = bytearray()
    for y in range(64):
        for x in range(16, 80):
            b, g, r, a = raw[(y * W + x) * 4:(y * W + x) * 4 + 4]
            out += bytes((r, g, b, a))
    return bytes(out)


def tex_noise(w=64, h=64):
    """Blue-ish noise: a hashed value per texel (deterministic), all four channels different."""
    out = bytearray()
    for y in range(h):
        for x in range(w):
            k = (x * 73856093) ^ (y * 19349663) ^ 0x5BD1E995
            k = (k ^ (k >> 13)) * 0x5BD1E995 & 0xFFFFFFFF
            out += bytes(((k >> 0) & 0xFF, (k >> 8) & 0xFF, (k >> 16) & 0xFF, (k >> 24) & 0xFF))
    return bytes(out)


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def tex_lut_1d_array(w=256, layers=6):
    """ColorCorrectionLUT's texture1d_array: layers 0..2 decode R, G, B (sRGB to linear), layers 3..5 re-encode
    (linear to a gamma 2.2 display with a small per-channel white-point trim). The value is in .r (the shader reads
    .r of each layer); .g/.b/.a carry the same ramp scaled, so a swizzle or channel mix-up shows."""
    out = bytearray()
    trim = (1.0, 0.985, 0.95)
    for layer in range(layers):
        for i in range(w):
            x = i / (w - 1)
            if layer < 3:
                v = srgb_to_linear(x)
            else:
                v = trim[layer - 3] * x ** (1 / 2.2)
            out += bytes((u8(v), u8(v * 0.5), u8(1 - v), 255))
    return bytes(out)


def tex_gpupass_lut(w=256, layers=3):
    """GPUPass combinedLUT (texture1d_array, layers R/G/B): a linear-to-display tone curve (gamma 1/2.4 with a toe)."""
    out = bytearray()
    for layer in range(layers):
        for i in range(w):
            x = i / (w - 1)
            v = linear_to_srgb(x) * (1.0, 0.99, 0.97)[layer]
            out += bytes((u8(v), u8(v), u8(v), 255))
    return bytes(out)


def tex_wave(w=256):
    """RippleFragment's wave profile (texture1d): a damped sine in .r, 256 texels."""
    out = bytearray()
    for i in range(w):
        t = i / 32.0
        v = 0.5 + 0.5 * math.sin(t * math.pi) * math.exp(-0.02 * i)
        out += bytes((u8(v), u8(1 - v), 128, 255))
    return bytes(out)


# ------------------------------------------------------------------------------------------------------------
# render-target initial contents (uploaded; Metal renders with loadAction .load of the same bytes)
# ------------------------------------------------------------------------------------------------------------
def init_background(fmt):
    """A desktop-like background: vertical blue gradient with a faint horizontal ramp."""
    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b = 0.10 + 0.10 * x / W, 0.20 + 0.06 * y / H, 0.30 + 0.35 * y / H
            if fmt == 'bgra8':
                out += bytes((u8(b), u8(g), u8(r), 255))
            elif fmt == 'rg8':
                out += bytes((u8(0.45 + 0.1 * x / W), u8(0.55 - 0.1 * y / H)))
            else:
                out += bytes((u8(0.2 + 0.5 * y / H),))
    return bytes(out)


def init_premultiplied():
    """The framebuffer-fetch functions' input: a premultiplied image with alpha from 0.02 to 1 in bands, colours
    that exceed alpha after scaling (EDR clamp), and exact zeros (unpremultiply's max(a, 0.001))."""
    out = bytearray()
    for y in range(H):
        for x in range(W):
            a = [1.0, 0.75, 0.5, 0.25, 0.02, 0.0][(x // 16) % 6] if y < 48 else 0.3 + 0.7 * x / (W - 1)
            r = (0.3 + 0.7 * (y / (H - 1))) * a
            g = (0.9 - 0.8 * (x / (W - 1))) * a
            b = (0.5 + 0.5 * math.sin(x * 0.3 + y * 0.2)) * a
            out += bytes((u8(b), u8(g), u8(r), u8(a)))
    return bytes(out)


# ------------------------------------------------------------------------------------------------------------
# extended range (Stage 6 FORMATS.md 3: code n in 0..1023, value (n - 384) / 510)
# ------------------------------------------------------------------------------------------------------------
def xr_n(v):
    return max(0, min(1023, int(math.floor(v * 510.0 + 384.0 + 0.5))))


def xr554(r, g, b):
    """MTLPixelFormatBGR10_XR: one word, B bits 0-9, G 10-19, R 20-29, bits 30-31 set."""
    return struct.pack('<I', (3 << 30) | (xr_n(r) << 20) | (xr_n(g) << 10) | xr_n(b))


def xr552(r, g, b, a):
    """MTLPixelFormatBGRA10_XR: four 16-bit words B, G, R, A, the code in the top 10 bits."""
    return struct.pack('<4H', xr_n(b) << 6, xr_n(g) << 6, xr_n(r) << 6, xr_n(a) << 6)


def tex_xr(fmt, w=64, h=64):
    """An extended-range image (Stage 6 scenes.py xr_texture): ramps over [-0.6, 1.2], a checker in blue that leaves
    [0, 1] both ways, alpha steps (552)."""
    out = bytearray()
    for y in range(h):
        for x in range(w):
            r = -0.6 + 1.8 * x / (w - 1)
            g = 1.2 - 1.8 * y / (h - 1)
            b = 1.1 if ((x // 8) + (y // 8)) % 2 else -0.4
            a = 0.25 + 0.75 * ((x + y) % 16) / 15
            out += xr554(r, g, b) if fmt == 'bgr10_xr' else xr552(r, g, b, a)
    return bytes(out)


def init_xr(fmt):
    """An extended-range target's initial contents (Stage 6 xr_init): values below 0 and above 1, alpha ramp."""
    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g = 0.9 - 1.5 * y / (H - 1), -0.5 + 1.6 * x / (W - 1)
            b = 0.3 if (x // 6) % 2 else 1.15
            out += xr554(r, g, b) if fmt == 'bgr10_xr' else xr552(r, g, b, 0.2 + 0.8 * x / (W - 1))
    return bytes(out)


# ------------------------------------------------------------------------------------------------------------
# geometry
# ------------------------------------------------------------------------------------------------------------
def quad_verts(x0, y0, x1, y1, u0, v0, u1, v1):
    """Two triangles (Metal's front face doesn't matter: culling is off), (x, y, u, v) per vertex."""
    return [(x0, y0, u0, v0), (x1, y0, u1, v0), (x0, y1, u0, v1), (x1, y0, u1, v0), (x1, y1, u1, v1), (x0, y1, u0, v1)]


def rotated_quad(cx, cy, hw, hh, angle, u0, v0, u1, v1):
    c, s = math.cos(angle), math.sin(angle)
    p = lambda dx, dy: (cx + c * dx - s * dy, cy + s * dx + c * dy)
    a, b, cc, d = p(-hw, -hh), p(hw, -hh), p(-hw, hh), p(hw, hh)
    return [(*a, u0, v0), (*b, u1, v0), (*cc, u0, v1), (*b, u1, v0), (*d, u1, v1), (*cc, u0, v1)]


def pos_uv_vertex(verts, extra=None, extra_format='float2'):
    """float2 pos @0, float2 uv @8 [, attribute 2 @16], stride 16 or 24 (SimpleVertex / SimpleVertexShadow /
    BlurCompositeVertex / SimpleTextureLightingVertex / UberCompositeVertex attribute layouts)."""
    data = b''
    for i, (x, y, u, v) in enumerate(verts):
        data += f32(x, y, u, v)
        if extra:
            data += f32(*extra[i]) + bytes(8 - 4 * len(extra[i]))
    attrs = [{'index': 0, 'format': 'float2', 'offset': 0}, {'index': 1, 'format': 'float2', 'offset': 8}]
    if extra:
        attrs.append({'index': 2, 'format': extra_format, 'offset': 16})
    return {'stride': 24 if extra else 16, 'count': len(verts), 'attributes': attrs, 'data': data,
            'triangles_px': [(x, y) for x, y, _u, _v in verts]}


def color_pos_vertex(points):
    """SimpleColorVertex / StyledDebugFillVertex: float4 color @0 (attribute 0), float2 pos @16 (attribute 1)."""
    data = b''.join(f32(*c, x, y) for (x, y, c) in points)
    return {'stride': 24, 'count': len(points), 'data': data,
            'attributes': [{'index': 0, 'format': 'float4', 'offset': 0}, {'index': 1, 'format': 'float2', 'offset': 16}],
            'triangles_px': [(x, y) for x, y, _c in points]}


def color_quad(x0, y0, x1, y1, c00, c10, c01, c11):
    return [(x0, y0, c00), (x1, y0, c10), (x0, y1, c01), (x1, y0, c10), (x1, y1, c11), (x0, y1, c01)]


# ------------------------------------------------------------------------------------------------------------
# colour management constants (realistic WindowServer / CoreDisplay values)
# ------------------------------------------------------------------------------------------------------------
SRGB_TO_P3 = ((0.8225, 0.1774, 0.0000), (0.0332, 0.9669, 0.0000), (0.0171, 0.0724, 0.9108))   # linear sRGB -> P3
BT709_LIMITED = ((0.1826, 0.6142, 0.0620, 16 / 255), (-0.1006, -0.3386, 0.4392, 128 / 255),
                 (0.4392, -0.3989, -0.0403, 128 / 255))                                        # RGB -> Y, Cb, Cr


def color_curve(threshold, a, b, c, d, e, gamma):
    """ColorCurve_FragmentArgs: 7 x float3 (16-byte stride). |x| >= threshold: (a|x| + b)^gamma + c, else d|x| + e."""
    fields = (threshold, a, b, c, d, e, gamma)
    return b''.join(f32(v, v, v, 0.0) for v in fields)


SRGB_DECODE = color_curve(0.04045, 1 / 1.055, 0.055 / 1.055, 0.0, 1 / 12.92, 0.0, 2.4)
GAMMA22_ENCODE = color_curve(0.0031308, 1.0, 0.0, 0.0, 12.92, 0.0, 1 / 2.2)


def cc_parametric(matrix=SRGB_TO_P3):
    """ColorCorrectionParametric_FragmentArgs (272 bytes): float3x4 matrix as 3 rows (rgb, offset), decode curve,
    encode curve."""
    rows = tuple((m[0], m[1], m[2], 0.0) for m in matrix)
    return mat3x4_rows(rows) + SRGB_DECODE + GAMMA22_ENCODE


def cc_lut_args(matrix=SRGB_TO_P3):
    return mat3x4_rows(tuple((m[0], m[1], m[2], 0.0) for m in matrix))


# ------------------------------------------------------------------------------------------------------------
# the 28 scenes
# ------------------------------------------------------------------------------------------------------------
MVP = f32(*ortho())
BLEND_PREMUL_OVER = {'enabled': True, 'rgb_op': 'add', 'alpha_op': 'add', 'src_rgb': 'one',
                     'dst_rgb': 'one_minus_source_alpha', 'src_alpha': 'one', 'dst_alpha': 'one_minus_source_alpha'}
LINEAR_CLAMP = {'min': 'linear', 'mag': 'linear', 'address': 'clamp_to_edge'}


def _tex(index, name, data, w, h=1, kind='2d', fmt='rgba8', layers=1):
    return {'index': index, 'name': name, 'type': kind, 'format': fmt, 'width': w, 'height': h, 'layers': layers,
            'data': data}


def _buf(index, data, device=False):
    return {'index': index, 'data': pad(data, 16), 'device': device}


def _rt(index, fmt, init):
    return {'index': index, 'format': fmt, 'init': init}


def scenes():
    """The 28 scenes, in the order the A16 draws them: the state P7 proved first, then 1D textures, blending,
    colour management, 1D-array LUTs, two attachments, the shadow passes; the paths with no earlier hardware
    evidence last (framebuffer fetch through a bindless descriptor, then CoreDisplay's driver table of device-buffer
    addresses: UBO 31 in A16 run 1, which read zeros; UBO 0 since BINDING-ABI v6), so a hang there costs the fewest
    results. Each: function pair, constants, vertex
    data, buffers per stage, textures, API samplers, render targets with their initial contents, blend, and the
    compare tolerance."""
    win, photo = tex_window(), tex_photo()
    bg = {f: init_background(f) for f in RT_FORMATS}
    pre = init_premultiplied()
    out = []

    def add(fs, vs, lib='SkyLight', fc_fs=None, fc_vs=None, vertex=None, vs_buffers=(), fs_buffers=(), textures=(),
            samplers=(), rts=None, blend=None, tolerance=3, edge_budget=24, why=''):
        out.append({'name': fs, 'lib': lib, 'fs': fs, 'vs': vs, 'fc': {'fs': dict(fc_fs or {}), 'vs': dict(fc_vs or {})},
                    'width': W, 'height': H, 'vertex': vertex, 'buffers': {'vs': list(vs_buffers), 'fs': list(fs_buffers)},
                    'textures': list(textures), 'samplers': list(samplers),
                    'rts': rts or [_rt(0, 'bgra8', bg['bgra8'])], 'blend': blend or {},
                    'tolerance': tolerance, 'edge_budget': edge_budget, 'realistic': why, 'group': 'gate'})

    mvp = _buf(1, MVP)
    # 1. flat-shaded geometry, interpolated colour
    add('SimpleColorFragment', 'SimpleColorVertex',
        vertex=color_pos_vertex(color_quad(8, 4, 88, 60, (0.95, 0.2, 0.2, 1), (0.2, 0.9, 0.3, 1), (0.2, 0.3, 0.95, 1),
                                           (0.9, 0.9, 0.85, 0.8))),
        vs_buffers=[mvp], why='menu-bar/selection highlight: per-vertex colours, a partially transparent corner')
    # 2. the P7 pipeline, now with a window texture
    add('SimpleTextureFragment', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1)),
        vs_buffers=[mvp], textures=[_tex(0, 'window', win, 64, 64)], samplers=[dict(LINEAR_CLAMP, index=0)],
        why='a window surface magnified 80x56 from 64x64 with bilinear filtering')
    add('SimpleTextureEDRFragment', 'SimpleVertex',
        vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, -0.2487, -0.1752, 1.7513, 1.3248)), vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.62))],
        textures=[_tex(0, 'photo_edr', tex_photo(fmt='rgba16f', peak=2.5), 64, 64, fmt='rgba16f')],
        samplers=[{'index': 0, 'min': 'nearest', 'mag': 'nearest', 'address': 'repeat'}],
        why='EDR content (RGBA16F, highlights to 2.5) scaled by the EDR headroom 0.62; repeat addressing, nearest '
            '(coordinates keep every pixel centre >= 0.07 texel from a texel edge: no rounding ties)')
    add('SimpleTextureScaleToSDRFragment', 'SimpleVertex',
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)), vs_buffers=[mvp],
        textures=[_tex(0, 'photo_edr', tex_photo(fmt='rgba16f', peak=2.5), 64, 64, fmt='rgba16f')],
        why='EDR highlights (up to 2.5) tone-mapped to SDR by max(r, g, b, 1)')
    add('SimpleTextureFragmentUV', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)),
        vs_buffers=[mvp], textures=[_tex(0, 'photo', photo, 64, 64)], rts=[_rt(0, 'rg8', bg['rg8'])],
        why='two-channel (RG8) target, as WindowServer uses for chroma')
    add('SimpleTextureTintFragment', 'SimpleVertex',
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 3.25, 5.5, 51.25, 53.5)), vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.95, 0.8, 0.65, 0.9, 1.15))], textures=[_tex(0, 'window', win, 64, 64)],
        why='warm tint (Night Shift-like) at 0.9 opacity and 1.15 EDR gain; 48 texels over 64 pixels: bilinear')
    add('SimpleGrayscale', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)),
        vs_buffers=[mvp], textures=[_tex(1, 'photo', photo, 64, 64)],
        why='accessibility grayscale of a photo (texture 1, nearest, pixel coordinates)')
    add('AlphaTextureFragment', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(8, 0, 88, 64, -8, -8, 72, 72)),
        vs_buffers=[mvp], textures=[_tex(0, 'window', win, 64, 64)],
        why='window alpha as a mask; clamp_to_zero border outside the texture')
    add('GroupFadeTextureFragment', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)),
        vs_buffers=[mvp], fs_buffers=[_buf(0, f32(0.65, 0.9))], textures=[_tex(7, 'window', win, 64, 64)],
        why='a window group fading at 65% with 0.9 dimming (texture 7)')
    add('SimpleTextureLightingFragment', 'SimpleTextureLightingVertex',
        vertex=pos_uv_vertex(quad_verts(12, 2, 84, 62, 0, 0, 64, 64)),
        vs_buffers=[_buf(1, MVP + f32(1, 0, 0, 0, 0, 1, 0, 0, 0.30, 0.20, 0.93, 0, 0, 0, 0, 1))],
        fs_buffers=[_buf(0, f32(1.0, 0.97, 0.9, 1.0, 1.0))], textures=[_tex(0, 'window', win, 64, 64)],
        why='a tilted window (normal (0.30, 0.20, 0.93)) lit by the view light, warm tint')
    add('StyledDebugFillFragment', 'StyledDebugFillVertex',
        fc_fs={'enable_checkerboard': 1, 'enable_border': 1, 'enable_dashed_border': 1},
        vertex=color_pos_vertex(color_quad(8, 4, 88, 60, (0.9, 0.3, 0.3, 1), (0.3, 0.9, 0.3, 1), (0.3, 0.3, 0.9, 1),
                                           (0.9, 0.8, 0.2, 1))),
        vs_buffers=[mvp], fs_buffers=[_buf(0, f32(8.0, 4.0, 88.0, 60.0))],
        why='Quartz Debug "flash screen updates" style fill: checkerboard, dashed border on the rectangle 8,4-88,60')
    add('RippleFragment', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)),
        vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(60.0, 10.0, 0.06, 0.0) + f32(26.0, 26.0, 38.0, 38.0) + f32(0.3, 0.0, 0.0, 0.0))],
        textures=[_tex(6, 'photo', photo, 64, 64), _tex(5, 'wave', tex_wave(), 256, kind='1d')],
        tolerance=6,
        why='the Dashboard-style ripple at t=60 spreading from the box 26..38 (ring radius 10) over a photo; 1D wave profile (texture 5); highlight '
            'pow(x, 200) -- tolerance 6 (pow of a clamped dot product)')
    # colour management and composition
    add('ShadowCompositeFragment', 'SimpleVertex', fc_fs={'enable_fade': 1},
        vertex=pos_uv_vertex(quad_verts(4, 0, 92, 64, -6, -2, 70, 66)), vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.0, 0.55, 1.0))], textures=[_tex(0, 'shadow_soft', tex_shadow_mask(soft=6.0), 64, 64)],
        blend={0: BLEND_PREMUL_OVER},
        why='a window shadow (soft alpha mask, 55% opacity) blended premultiplied source-over onto the desktop')
    add('UberCompositeFragment', 'UberCompositeVertex', fc_fs={'enable_fade': 1, 'enable_pcc': 1},
        vertex=pos_uv_vertex(rotated_quad(48, 32, 38, 26, 0.12, 0, 0, 1, 1), extra=[(1.0,)] * 6, extra_format='float'),
        vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.0, 0.85, 1.0)), _buf(2, cc_parametric())],
        textures=[_tex(0, 'window', win, 64, 64)], samplers=[dict(LINEAR_CLAMP, index=0)], blend={0: BLEND_PREMUL_OVER},
        tolerance=4, edge_budget=48,
        why='the compositor\'s main path: a (slightly rotated) window at 85% opacity, colour-managed sRGB -> Display P3 '
            '(parametric decode, matrix, gamma 2.2 encode), premultiplied source-over; tolerance 4 (two pow curves)')
    add('BlurComposite', 'BlurCompositeVertex', fc_fs={'enable_fade': 1, 'enable_noise': 1},
        vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1),
                             extra=[(u * 0.8 + 0.1, v * 0.8 + 0.1) for _x, _y, u, v in quad_verts(8, 4, 88, 60, 0, 0, 1, 1)]),
        vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.0, 0.92, 1.0, 0.004) + bytes((0, 0, 0, 0))),
                    _buf(3, f32(2.0 / 255, 1.0 / 64, 0.0, 0.0) + f32(0.25, 0.5))],
        textures=[_tex(0, 'window', win, 64, 64), _tex(8, 'photo_blurred', tex_photo(peak=1.0), 64, 64),
                  _tex(4, 'noise', tex_noise(), 64, 64)],
        samplers=[dict(LINEAR_CLAMP, index=0), dict(LINEAR_CLAMP, index=1)], tolerance=4,
        why='a translucent sidebar: window over the blurred desktop, 92% fade, blue-noise dithering at the fragment '
            'position (texture 4, repeat); tolerance 4 (noise +-1 code, rounding at the dither step). The dither '
            'moves the golden by at most 1 code, under the tolerance: this scene checks the fade and the blur, '
            'not the noise path (frag_coord is checked by 22-24)')
    # resampling and YCbCr
    add('UberResampleLanczosFragmentBGRA', 'SimpleVertex', fc_fs={'enable_rescale_down2x': 1, 'enable_cc': 1},
        vertex=pos_uv_vertex(quad_verts(16, 8, 80, 56, 0, 0, 1, 1)), vs_buffers=[mvp],
        fs_buffers=[_buf(4, f32(0.95, 0.0, 1.0 / 128, 0.0)), _buf(1, cc_lut_args())],
        textures=[_tex(0, 'photo128', tex_photo(128, 96), 128, 96),
                  _tex(3, 'cc_lut', tex_lut_1d_array(), 256, kind='1d_array', layers=6)],
        samplers=[dict(LINEAR_CLAMP, index=0)], tolerance=4,
        why='a 2x Lanczos downscale (128x96 -> 64x48, horizontal pass) with LUT colour correction (1D array, 6 layers)')
    add('UberResampleLanczosFragmentYCbCr', 'SimpleVertex', fc_fs={'enable_rescale_down2x': 1},
        vertex=pos_uv_vertex(quad_verts(16, 8, 80, 56, 0, 0, 1, 1)), vs_buffers=[mvp],
        fs_buffers=[_buf(4, f32(1.0, 0.0, 0.0, 1.0 / 96)), _buf(5, b''.join(f32(*r) for r in BT709_LIMITED))],
        textures=[_tex(0, 'photo128', tex_photo(128, 96), 128, 96)], samplers=[dict(LINEAR_CLAMP, index=0)],
        rts=[_rt(0, 'r8', bg['r8']), _rt(1, 'rg8', bg['rg8'])], tolerance=4,
        why='screen recording / AirPlay path: 2x downscale (vertical pass) straight to BT.709 limited-range Y (R8) '
            'and CbCr (RG8) planes')
    add('ColorFillYCbCr', 'SimpleColorVertex',
        vertex=color_pos_vertex(color_quad(8, 4, 88, 60, (0.9, 0.1, 0.1, 1), (0.1, 0.8, 0.2, 1), (0.1, 0.2, 0.9, 1),
                                           (0.95, 0.95, 0.9, 1))),
        vs_buffers=[mvp], fs_buffers=[_buf(5, b''.join(f32(*r) for r in BT709_LIMITED))],
        rts=[_rt(0, 'r8', bg['r8']), _rt(1, 'rg8', bg['rg8'])],
        why='a colour fill into a two-plane BT.709 target (Y in R8, CbCr in RG8)')
    add('ColorFillYCbCr_ChromaOnly', 'SimpleColorVertex',
        vertex=color_pos_vertex(color_quad(8, 4, 88, 60, (0.9, 0.1, 0.1, 1), (0.1, 0.8, 0.2, 1), (0.1, 0.2, 0.9, 1),
                                           (0.95, 0.95, 0.9, 1))),
        vs_buffers=[mvp], fs_buffers=[_buf(5, b''.join(f32(*r) for r in BT709_LIMITED))],
        rts=[_rt(0, 'rg8', bg['rg8'])], why='the chroma-plane half of the fill (RG8 only)')
    # shadows (baked radius-12 Gaussian, soft rim of radius 2)
    shadow_fc = {'is_rim_state_soft': 1, 'is_radius_eq_12': 1, 'is_radius_ge_3': 1, 'is_rim_radius_eq_2': 1}
    fargs = i32(2, 1, 0) + f32(0.55, 0.35, 0.2) + bytes(8) + f32(0.0, 0.0, 0.0, 1.0) + f32(1.0, 1.0, 1.0, 0.5)
    fargs_rgba = i32(2, 1, 0) + f32(0.55, 0.35, 0.2) + bytes(8) + f32(0.16, 0.18, 0.24, 1.0) + f32(1.0, 0.95, 0.85, 0.5)
    mask = tex_shadow_mask()
    add('ShadowHorizontalBlurFragment', 'SimpleVertexShadow', fc_fs=dict(shadow_fc, is_horizontal=1),
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)), vs_buffers=[mvp], fs_buffers=[_buf(1, fargs)],
        textures=[_tex(0, 'shadow_mask', mask, 64, 64)], tolerance=4,
        why='window shadow, horizontal pass: radius 12 with a soft rim of radius 2 (the image depends on the mask '
            'and the blur weights; FS buffer(1) is bound but the horizontal pass does not read it: zeroing it '
            'leaves the Metal image unchanged)')
    add('ShadowVerticalBlurFragment', 'SimpleVertexShadow', fc_fs=dict(shadow_fc),
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)), vs_buffers=[mvp], fs_buffers=[_buf(1, fargs)],
        textures=[_tex(0, 'shadow_hpass', tex_shadow_hpass(), 64, 64)], rts=[_rt(0, 'r8', bg['r8'])], tolerance=4,
        why='window shadow, vertical pass (input: the horizontal pass\'s output) into the single-channel (R8) target')
    add('ShadowVerticalBlurRGBAFragment', 'SimpleVertexShadow', fc_fs=dict(shadow_fc),
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 64, 64)), vs_buffers=[mvp],
        fs_buffers=[_buf(1, fargs_rgba)], textures=[_tex(0, 'shadow_hpass', tex_shadow_hpass(), 64, 64)], tolerance=4,
        why='vertical pass of the horizontal pass\'s output, coloured shadow with an inner glow (rim colour (0.16, 0.18, 0.24), inner (1, 0.95, 0.85, 0.5)), vertical')
    # framebuffer fetch: the target's own contents are the input
    add('InPlaceSover', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1)), vs_buffers=[mvp],
        fs_buffers=[_buf(0, f32(0.20, 0.10, 0.35, 0.45))], rts=[_rt(0, 'bgra8', pre)],
        why='premultiplied colour (0.2, 0.1, 0.35, 0.45) composited over the framebuffer in place (fetch)')
    add('InPlaceAlphaUnpremultiply', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1)),
        vs_buffers=[mvp], rts=[_rt(0, 'bgra8', pre)], tolerance=4,
        why='unpremultiply the framebuffer in place (alpha bands 1 .. 0.02 and 0); tolerance 4: rgb/a at a = 0.02 '
            'amplifies the 8-bit input by 50')
    add('InPlaceColorOrClampEDR', 'SimpleVertex', vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1)),
        vs_buffers=[mvp], fs_buffers=[_buf(0, f32(0.85, 1.6, 1.0) + bytes((0, 0, 0, 0)))], rts=[_rt(0, 'bgra8', pre)],
        tolerance=4,
        why='EDR headroom 1.6 applied in place; values over 1.0 take the clamp branch (mode byte 0)')
    # CoreDisplay: geometry and colour come from device buffers read by vertex_id (the driver table: UBO 0, ABI v6)
    pos = f32(8, 4, 88, 4, 8, 60, 88, 4, 88, 60, 8, 60)
    uvs = f32(*[c * 64 for c in (0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1)])
    uvn = f32(0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1)
    cd_tris = [(8, 4), (88, 4), (8, 60), (88, 4), (88, 60), (8, 60)]
    cd_vertex = {'stride': 0, 'count': 6, 'attributes': [], 'data': b'', 'triangles_px': cd_tris}
    add('TextureCopy', 'ViewportToNDC', lib='CoreDisplay', vertex=cd_vertex,
        vs_buffers=[_buf(0, pos, True), _buf(1, uvs, True), _buf(2, MVP, True)],
        textures=[_tex(0, 'photo', photo, 64, 64)],
        why='CoreDisplay\'s copy of a frame into the scan-out surface (vertices and matrix from device buffers)')
    add('ColorFill', 'ViewportToNDC', lib='CoreDisplay',
        vertex={**cd_vertex, 'triangles_px': [(20, 10), (76, 10), (20, 54), (76, 10), (76, 54), (20, 54)]},
        vs_buffers=[_buf(0, f32(20, 10, 76, 10, 20, 54, 76, 10, 76, 54, 20, 54), True), _buf(1, uvn, True),
                    _buf(2, MVP, True)],
        fs_buffers=[_buf(0, f32(0.12, 0.45, 0.8, 1.0), True)],
        why='CoreDisplay\'s fill of a rectangle (its colour is a device buffer); the fill is flat by design, the '
            'background around it is not, so the golden checks one colour point; VS buffer(1) (the UVs) does not '
            'affect the fill')
    add('GPUPass', 'ViewportToNDC', lib='CoreDisplay', fc_fs={'availableFeatures': 0x17, 'optionalFeatures': 0x10},
        vertex=cd_vertex, vs_buffers=[_buf(0, pos, True), _buf(1, uvn, True), _buf(2, MVP, True)],
        fs_buffers=[_buf(0, u32(0x10)),
                    _buf(1, b''.join(f32(*[v] * 3) for v in (0.04045, 1 / 1.055, 0.055 / 1.055, 0.0, 1 / 12.92, 0.0,
                                                               2.4))),
                    _buf(2, f32(*[SRGB_TO_P3[r][c] if r < 3 else 0.0 for c in range(3) for r in range(4)], 0, 0, 0, 1)),
                    _buf(3, f32(255 / 256)), _buf(4, f32(0.5 / 256)),
                    _buf(5, f32(1.0, 0.0, 0.0, 0.0, 0.0, 0.985, 0.0, 0.0, 0.0, 0.0, 0.96, 0.0, 0.0, 0.0, 0.0, 1.0))],
        textures=[_tex(0, 'photo', photo, 64, 64), _tex(1, 'gpupass_lut', tex_gpupass_lut(), 256, kind='1d_array',
                                                        layers=3)],
        samplers=[dict(LINEAR_CLAMP, index=0)], tolerance=4,
        why='CoreDisplay\'s display pipeline: sRGB linearise, CSC to P3, 1D LUT (texel-aligned), post-gamma CSC (a '
            'runtime-enabled optional feature); tolerance 4 (pow in the curve)')
    return out


BLEND_SRC_ALPHA = {'enabled': True, 'rgb_op': 'add', 'alpha_op': 'add', 'src_rgb': 'source_alpha',
                   'dst_rgb': 'one_minus_source_alpha', 'src_alpha': 'one', 'dst_alpha': 'one_minus_source_alpha'}


def xr_scenes():
    """Beyond the gate: the XR lowering classes of BINDING-ABI 4.5 on the A16 (Stage 6 checked them on llvmpipe
    against Metal: 24 scenes within 2 codes). WindowServer renders into XR only on extended-range displays; it
    samples XR surfaces from other processes on any display (FORMATS.md). Compared in XR code units (tolerance 2)."""
    mvp = _buf(1, MVP)
    tint = f32(1.4, -0.3, 0.8, 1.0, 1.25)             # an out-of-range tint: outputs leave [0, 1] both ways
    win = tex_window()
    out = []

    def add(name, fs, rt_fmt, textures, blend=None, fs_buffers=(), samplers=(), verts=None, why=''):
        out.append({'name': name, 'lib': 'SkyLight', 'fs': fs, 'vs': 'SimpleVertex', 'fc': {'fs': {}, 'vs': {}},
                    'width': W, 'height': H,
                    'vertex': pos_uv_vertex(verts or quad_verts(16, 0, 80, 64, 0, 0, 64, 64)),
                    'buffers': {'vs': [mvp], 'fs': list(fs_buffers)}, 'textures': list(textures),
                    'samplers': list(samplers), 'rts': [_rt(0, rt_fmt, init_xr(rt_fmt))] if rt_fmt in XR_CLASS
                    else [_rt(0, rt_fmt, init_background(rt_fmt))], 'blend': {0: blend} if blend else {},
                    'tolerance': 2, 'edge_budget': 24, 'realistic': why, 'group': 'xr'})
    add('xr554_tint_none', 'SimpleTextureTintFragment', 'bgr10_xr', [_tex(0, 'window', win, 64, 64)],
        fs_buffers=[_buf(0, tint)], why='class 1 target (BGR10_XR): encode on write, alpha write-masked')
    add('xr554_tint_src_alpha', 'SimpleTextureTintFragment', 'bgr10_xr', [_tex(0, 'window', win, 64, 64)],
        blend=BLEND_SRC_ALPHA, fs_buffers=[_buf(0, tint)],
        why='class 1 target, source-alpha blending on the RB with the lowering\'s offset correction (hw_blend)')
    add('xr552_tint_premul_over', 'SimpleTextureTintFragment', 'bgra10_xr', [_tex(0, 'window', win, 64, 64)],
        blend=BLEND_PREMUL_OVER, fs_buffers=[_buf(0, tint)],
        why='class 2 target (BGRA10_XR), premultiplied source-over blended in the shader (framebuffer fetch of '
            'the R16G16B16A16 view with the B,G,R,A swap)')
    add('xr552_sample_to_xr', 'SimpleTextureFragment', 'bgra10_xr',
        [_tex(0, 'xr552', tex_xr('bgra10_xr'), 64, 64, fmt='bgra10_xr')],
        samplers=[{'index': 0, 'min': 'nearest', 'mag': 'nearest', 'address': 'clamp_to_edge'}],
        verts=quad_verts(16, 0, 80, 64, 0, 0, 1, 1), why='class 2 texture decoded on sample, class 2 target encoded')
    add('xr554_sample_linear', 'SimpleTextureFragment', 'bgra8',
        [_tex(0, 'xr554', tex_xr('bgr10_xr'), 64, 64, fmt='bgr10_xr')],
        samplers=[{'index': 0, 'min': 'linear', 'mag': 'linear', 'address': 'clamp_to_edge'}],
        verts=quad_verts(8, 4, 88, 60, 0, 0, 1, 1),
        why='a client BGR10_XR surface (w30r) sampled with bilinear filtering into BGRA8: decode on sample')
    return out


def probe_colour(k):
    """The known value in constant buffer(k) of the UBO probes: distinct, non-zero bytes (exact in UNORM8)."""
    return ((k * 37 + 20) % 256, (k * 91 + 60) % 256, k * 8 + 5, 255)


def probe_scenes():
    """Single-feature A16 probes (not the gate; `render --probes` or --functions): one piece of state each, so one
    short session confirms each fix of A16 run 1 separately (docs/A16-ADRENO-STAGE6-RENDER.md). Our own Metal
    functions (a16gpu-render/probes/probes.metal); goldens from the M3 Max like the gate's."""
    mvp = _buf(1, MVP)
    full = pos_uv_vertex(quad_verts(0, 0, 96, 64, 0, 0, 1, 1))
    ubo = {k: _buf(k, f32(*[c / 255 for c in probe_colour(k)])) for k in range(31)}
    out = []

    def add(name, why, fs_buffers=(), textures=(), samplers=(), vertex=None, rts=None, **extra):
        out.append(dict({'name': name, 'lib': 'Probe', 'fs': name, 'vs': 'probe_vs', 'fc': {'fs': {}, 'vs': {}},
                         'width': W, 'height': H, 'vertex': vertex or full,
                         'buffers': {'vs': [mvp], 'fs': list(fs_buffers)}, 'textures': list(textures),
                         'samplers': list(samplers), 'rts': rts or [_rt(0, 'bgra8', init_background('bgra8'))],
                         'blend': {}, 'tolerance': 1, 'edge_budget': 0, 'realistic': why, 'group': 'probe'}, **extra))
    add('probe_ubo_lo', 'UBO 0..15: band k (6 pixels) = constant buffer(k) = UBO k; all must read their value',
        fs_buffers=[ubo[k] for k in range(16)], bands={'first_ubo': 0, 'count': 16, 'width': 6})
    # UBO 16..30. The A16 also gets UBO 0..15 bound to the low probe's values (Metal ignores buffers a function does
    # not declare), so a band that reads UBO k - 16 instead of k shows which colour it got.
    add('probe_ubo_hi', 'UBO 16..30 (compiled with --test-ubo-slots 32): Metal reads all; run 1 predicts zeros on the '
        'A16 (a8xx honours UBO 0..15)', fs_buffers=[ubo[k] for k in range(31)],
        bands={'first_ubo': 16, 'count': 15, 'width': 6}, compile_args=['--test-ubo-slots', '32'],
        bind_undeclared_ubos=True)
    add('probe_alias_rgb', 'output x, y, z are constants (alias.rt, SP_PS_OUTPUT_CONST_MASK 0x7), alpha = u')
    add('probe_alias_zw', 'output z, w are constants (alias.rt, SP_PS_OUTPUT_CONST_MASK 0xc, as Mesa\'s gl-blend)')
    add('probe_fetch', 'framebuffer fetch alone (bindless set 4, layer input INTERP_ZERO): the target read back '
        'with R and B swapped', vertex=pos_uv_vertex(quad_verts(8, 4, 88, 60, 0, 0, 1, 1)),
        rts=[_rt(0, 'bgra8', init_premultiplied())])
    a8 = bytes((x * 4 + y) & 0xFF for y in range(64) for x in range(64))
    add('probe_a8', 'an A8Unorm texture (FMT6_A8_UNORM, pitch 64): the sample is (0, 0, 0, a); nearest at texel '
        'centres', textures=[_tex(0, 'a8', a8, 64, 64, fmt='a8')],
        samplers=[{'index': 0, 'min': 'nearest', 'mag': 'nearest', 'address': 'clamp_to_edge'}],
        vertex=pos_uv_vertex(quad_verts(16, 0, 80, 64, 0, 0, 1, 1)))
    return out


def all_scenes():
    """The gate's 28, the XR set and the probes: what the goldens and the shader export cover."""
    return scenes() + xr_scenes() + probe_scenes()


# The wscts reference run these 28 functions come from (vertex pairing and provenance).
WSCTS_REF = 'Snapintosh scratch adreno/windowserver-metal-usage/cts/ref-m3max.txt (wscts.m on the M3 Max, # pass=28)'


def fc_resolve(declared, given):
    """Every declared constant set explicitly: 0 unless the scene names it. `declared` is the reflection's
    function_constants list ({name, index, type}); returns {index: value}."""
    by_name = {d['name']: d for d in declared}
    unknown = sorted(set(given) - set(by_name))
    if unknown:
        raise ValueError(f'function constants {unknown} are not declared by the function')
    return {d['index']: int(given.get(d['name'], 0)) for d in declared}


def scene_digest(scene):
    """A digest of everything a scene uploads (what the golden depends on): the tool records it per function."""
    h = hashlib.sha256()
    for key in ('name', 'vs', 'fs', 'lib'):
        h.update(scene[key].encode() + b'\0')
    h.update(json.dumps(scene['fc'], sort_keys=True).encode())
    v = scene['vertex']
    h.update(json.dumps({k: v[k] for k in ('stride', 'count', 'attributes')}, sort_keys=True).encode() + v['data'])
    for stage in ('vs', 'fs'):
        for b in scene['buffers'][stage]:
            h.update(struct.pack('<IB', b['index'], b['device']) + b['data'])
    for t in scene['textures']:
        h.update(json.dumps({k: t[k] for k in t if k != 'data'}, sort_keys=True).encode() + t['data'])
    h.update(json.dumps(scene['samplers'], sort_keys=True).encode())
    for r in scene['rts']:
        h.update(struct.pack('<I', r['index']) + r['format'].encode() + r['init'])
    h.update(json.dumps(scene['blend'], sort_keys=True, default=str).encode())
    if scene.get('compile_args') or scene.get('bind_undeclared_ubos'):          # probes only (gate digests unchanged)
        h.update(json.dumps([scene.get('compile_args'), scene.get('bind_undeclared_ubos')]).encode())
    return h.hexdigest()


def write_scenes(directory):
    """scenes.json + data/*.bin for render_metal_golden.swift: exactly the bytes the A16 tool uploads."""
    d = Path(directory)
    (d / 'data').mkdir(parents=True, exist_ok=True)
    index = []
    for n, s in enumerate(all_scenes()):
        files = {}

        def put(tag, blob):
            path = d / 'data' / f'{s["name"]}.{tag}.bin'
            path.write_bytes(blob)
            files[tag] = str(path.relative_to(d))
            return files[tag]
        entry = {'order': n, 'name': s['name'], 'lib': s['lib'], 'metallib': LIBS[s['lib']],
                 'metallib_sha256': LIB_SHA256.get(s['lib']), 'vs': s['vs'], 'fs': s['fs'], 'fc': s['fc'],
                 'width': s['width'], 'height': s['height'], 'digest': scene_digest(s),
                 'vertex': {'stride': s['vertex']['stride'], 'count': s['vertex']['count'],
                            'attributes': s['vertex']['attributes'],
                            'file': put('vertex', s['vertex']['data']) if s['vertex']['data'] else None},
                 'buffers': {st: [{'index': b['index'], 'device': b['device'], 'file': put(f'{st}buf{b["index"]}', b['data'])}
                                  for b in s['buffers'][st]] for st in ('vs', 'fs')},
                 'textures': [{**{k: t[k] for k in t if k != 'data'}, 'file': put(f'tex{t["index"]}', t['data'])}
                              for t in s['textures']],
                 'samplers': s['samplers'],
                 'rts': [{'index': r['index'], 'format': r['format'], 'file': put(f'rt{r["index"]}init', r['init'])}
                         for r in s['rts']],
                 'blend': {str(k): v for k, v in s['blend'].items()}}
        index.append(entry)
    (d / 'scenes.json').write_text(json.dumps({'format': 1, 'width': W, 'height': H, 'scenes': index}, indent=1) + '\n')
    return d


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write-scenes':
        print(write_scenes(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == '--derive-shadow-input':
        blob = derive_shadow_input(sys.argv[2])
        SHADOW_HPASS.write_bytes(blob)
        print(SHADOW_HPASS, hashlib.sha256(blob).hexdigest())
    elif len(sys.argv) == 2 and sys.argv[1] == '--list':
        for n, s in enumerate(all_scenes()):
            print(f'{n:2d} {s["name"]:34s} vs {s["vs"]:28s} {s["realistic"]}')
    else:
        sys.exit('usage: a16gpu_render_scenes.py --write-scenes DIR | --list | --derive-shadow-input GOLDENDIR')
