# Adreno X2-90, Stage 6 on the A16: `render` (the 28 wscts pipelines)

Stage 6's gate (Snapintosh plan): *every WindowServer pipeline compiles* (PROVEN on the M3 Max by Stage 6 fix
rounds 1-2) and *the A16 renders the 28 wscts functions, after they are given realistic uniforms, within
tolerance of the M3 Max goldens*. This is the second half. `tools/a16gpu.py render` draws all 28 in one A16
session, one submission and one fence per draw, on the CP that `cp-fence` started. It reads every target back and
compares it with a Metal golden of the same pipeline, inputs and constants. A16 run 1 (2026-09-25) drew 25 and
found three state bugs and one ABI limit; they are fixed here and each has a single-feature probe ("A16 run 1:
diagnosis" below). Runbook: `docs/A16-ADRENO-STAGE6-RENDER-RUNBOOK.md`.

| Piece | Where |
| --- | --- |
| the command (R0 checks and every command stream built in a scratch window, R1 window + uploads, R2 draws, R3 verdict) | `tools/a16gpu_render.py` |
| the binding ABI (BINDING-ABI v6: fix round 4 + the run 1 UBO fix): every slot, count and descriptor rule, in one module | `tools/a16gpu_bindings.py` |
| the scenes: one source of truth for the A16, the Metal goldens and the shader export | `tools/a16gpu_render_scenes.py` |
| the variants and the goldens (both sha-pinned), and how they are made; the probe functions | `tools/a16gpu-render/` (README.md), `tools/a16gpu-render/probes/` |
| the model test (the CP executes the packets; every binding is read through CB0) and the Mesa comparison | `tools/test-a16gpu-render.py`, `tools/testdata/a16gpu-render/` |

Beyond the gate, `render --xr` also draws five scenes for BINDING-ABI 4.5's XR lowering classes (BGR10_XR and
BGRA10_XR targets and textures), after the 28 ("Beyond the gate" below), and `render --probes` draws six
single-feature probes (never the gate; "Probes" below).

## The 28 functions (provenance)

The gate's list is the wscts reference run on the M3 Max (`tools/a16gpu-render/wscts-functions.txt`, copied from
Snapintosh scratch `adreno/windowserver-metal-usage/cts/ref-m3max.txt`, `# pass=28`). It holds every SkyLight (25)
and CoreDisplay (3) fragment function that wscts could pair with a vertex function, and that pairing. wscts
filled every float buffer with 0.25 and left every function constant unset, so 14 of its 28 references share
five hashes (flat colours). Here:

- Every function constant a function declares is set (0 unless the scene names it). An unset constant is LLVM
  undef in Metal, so its golden would depend on how AGX handles undef (BINDING-ABI 3.7).
- Every buffer, texture and sampler carries what WindowServer gives it, and every target starts from an image
  rather than a clear colour. A draw that writes too much, too little or the wrong channel therefore shows. The
  model test requires every golden to have at least 64 distinct values and to differ from the target's initial
  contents.

| # | fragment function | what the scene gives it (why it is realistic) |
| --- | --- | --- |
| 0 | `SimpleColorFragment` | menu-bar/selection highlight: per-vertex colours, a partially transparent corner |
| 1 | `SimpleTextureFragment` | a window surface magnified 80x56 from 64x64 with bilinear filtering |
| 2 | `SimpleTextureEDRFragment` | EDR content (RGBA16F, highlights to 2.5) scaled by the EDR headroom 0.62; repeat addressing, nearest (coordinates keep every pixel centre >= 0.07 texel from a texel edge: no rounding ties) |
| 3 | `SimpleTextureScaleToSDRFragment` | EDR highlights (up to 2.5) tone-mapped to SDR by max(r, g, b, 1) |
| 4 | `SimpleTextureFragmentUV` | two-channel (RG8) target, as WindowServer uses for chroma |
| 5 | `SimpleTextureTintFragment` | warm tint (Night Shift-like) at 0.9 opacity and 1.15 EDR gain; 48 texels over 64 pixels: bilinear |
| 6 | `SimpleGrayscale` | accessibility grayscale of a photo (texture 1, nearest, pixel coordinates) |
| 7 | `AlphaTextureFragment` | window alpha as a mask; clamp_to_zero border outside the texture |
| 8 | `GroupFadeTextureFragment` | a window group fading at 65% with 0.9 dimming (texture 7) |
| 9 | `SimpleTextureLightingFragment` | a tilted window (normal (0.30, 0.20, 0.93)) lit by the view light, warm tint |
| 10 | `StyledDebugFillFragment` | Quartz Debug "flash screen updates" style fill: checkerboard, dashed border on the rectangle 8,4-88,60 |
| 11 | `RippleFragment` | the Dashboard-style ripple at t=60 spreading from the box 26..38 (ring radius 10) over a photo; 1D wave profile (texture 5); highlight pow(x, 200) -- tolerance 6 (pow of a clamped dot product) |
| 12 | `ShadowCompositeFragment` | a window shadow (soft alpha mask, 55% opacity) blended premultiplied source-over onto the desktop |
| 13 | `UberCompositeFragment` | the compositor's main path: a (slightly rotated) window at 85% opacity, colour-managed sRGB -> Display P3 (parametric decode, matrix, gamma 2.2 encode), premultiplied source-over; tolerance 4 (two pow curves) |
| 14 | `BlurComposite` | a translucent sidebar: window over the blurred desktop, 92% fade, blue-noise dithering at the fragment position (texture 4, repeat); tolerance 4 (noise +-1 code, rounding at the dither step). The dither moves the golden by at most 1 code, under the tolerance: this scene checks the fade and the blur, not the noise path (frag_coord is checked by 22-24) |
| 15 | `UberResampleLanczosFragmentBGRA` | a 2x Lanczos downscale (128x96 -> 64x48, horizontal pass) with LUT colour correction (1D array, 6 layers) |
| 16 | `UberResampleLanczosFragmentYCbCr` | screen recording / AirPlay path: 2x downscale (vertical pass) straight to BT.709 limited-range Y (R8) and CbCr (RG8) planes |
| 17 | `ColorFillYCbCr` | a colour fill into a two-plane BT.709 target (Y in R8, CbCr in RG8) |
| 18 | `ColorFillYCbCr_ChromaOnly` | the chroma-plane half of the fill (RG8 only) |
| 19 | `ShadowHorizontalBlurFragment` | window shadow, horizontal pass: radius 12 with a soft rim of radius 2 (the image depends on the mask and the blur weights; FS buffer(1) is bound but the horizontal pass does not read it: zeroing it leaves the Metal image unchanged) |
| 20 | `ShadowVerticalBlurFragment` | window shadow, vertical pass (input: the horizontal pass's output) into the single-channel (R8) target |
| 21 | `ShadowVerticalBlurRGBAFragment` | vertical pass of the horizontal pass's output, coloured shadow with an inner glow (rim colour (0.16, 0.18, 0.24), inner (1, 0.95, 0.85, 0.5)), vertical |
| 22 | `InPlaceSover` | premultiplied colour (0.2, 0.1, 0.35, 0.45) composited over the framebuffer in place (fetch) |
| 23 | `InPlaceAlphaUnpremultiply` | unpremultiply the framebuffer in place (alpha bands 1 .. 0.02 and 0); tolerance 4: rgb/a at a = 0.02 amplifies the 8-bit input by 50 |
| 24 | `InPlaceColorOrClampEDR` | EDR headroom 1.6 applied in place; values over 1.0 take the clamp branch (mode byte 0) |
| 25 | `TextureCopy` | CoreDisplay's copy of a frame into the scan-out surface (vertices and matrix from device buffers) |
| 26 | `ColorFill` | CoreDisplay's fill of a rectangle (its colour is a device buffer); the fill is flat by design, the background around it is not, so the golden checks one colour point; VS buffer(1) (the UVs) does not affect the fill |
| 27 | `GPUPass` | CoreDisplay's display pipeline: sRGB linearise, CSC to P3, 1D LUT (texel-aligned), post-gamma CSC (a runtime-enabled optional feature); tolerance 4 (pow in the curve) |
| 28 | `xr554_tint_none` | class 1 target (BGR10_XR): encode on write, alpha write-masked |
| 29 | `xr554_tint_src_alpha` | class 1 target, source-alpha blending on the RB with the lowering's offset correction (hw_blend) |
| 30 | `xr552_tint_premul_over` | class 2 target (BGRA10_XR), premultiplied source-over blended in the shader (framebuffer fetch of the R16G16B16A16 view with the B,G,R,A swap) |
| 31 | `xr552_sample_to_xr` | class 2 texture decoded on sample, class 2 target encoded |
| 32 | `xr554_sample_linear` | a client BGR10_XR surface (w30r) sampled with bilinear filtering into BGRA8: decode on sample |

Two scenes feed each other through a fixed file. The vertical shadow passes sample what the horizontal pass
writes (B the silhouette, G the blur, R the rim: the encoding the vertical pass decodes). That input is taken
from the horizontal scene's M3 Max golden (`shadow-hpass-m3max.rgba`, sha-pinned).

## From the library to the command stream

**Variants.** `make-render-shaders.sh` computes each stage's cache key (`air2ir3 --key-only`) and looks it up in
the Stage 6 precompiled cache: fix round 4 plus `adreno-render-fix/air2ir3-abi6.patch` (BINDING-ABI v6, cache
format 4, compiler `air2ir3-src-a51400fcd94cea44`, the cache rebuilt by that tree's `tools/sweep.py`). It also
compiles each stage online through the same entry point and requires the sidecar and blob to be byte-identical.
**All 56 stages of the 28 hit the cache, and all are byte-identical to the online compile.** Two XR fragment keys
are not in the sweep (a class-2 target with a class-2 texture; a class-1 target with source-alpha blending), nor
are the probes; they are compiled online. Against run 1's fix2 variants the gate's code differs only in the
constexpr sampler's S# immediate (ABI v4: the lowest free slot instead of S#15) and, in the three CoreDisplay
functions, the driver table's UBO index (`ldc ..., 31` became `ldc ..., 0`). The pipeline const budget
(`--trim-constlen`, BINDING-ABI 5.2) trims nothing. No variant needs private memory, driver UBOs or constant
data. Every UBO index is below 16 (ABI v6; probe_ubo_hi excepted, on purpose). Varyings are paired by name (BINDING-ABI 4.1: each stage ranks its
own names), and Mesa's `ir3_link_shaders` runs on that pairing.

**Binding ABI.** `tools/a16gpu_bindings.py` is the only place that reads a sidecar's reflection.

| ABI | what `render` programs | functions |
| --- | --- | --- |
| 3.1 constant buffers | UBO n = `buffer(n)`; every declared `ubo` binding is bound; its size covers the preamble's pushed ranges | 26 (all but TextureCopy, ColorFill) |
| 3.1 / 3.9 UBO table (v6) | every index below 16: a8xx most likely honours 16 non-bindless UBO descriptors (inferred from A16 run 1, freedreno's 16 and Turnip's bindless UBOs; `probe_ubo_hi` measures it) | all |
| 3.2 device buffers | the driver table at UBO `driver_table_ubo` (v6: 1 + the highest declared constant buffer; 0 for CoreDisplay's; run 1 used 31 and read zeros), 384 bytes, the u64 VA of `buffer(n)` at byte 8n; `num_app_ubos` descriptors loaded (1 for ViewportToNDC; run 1: 32) | TextureCopy, ColorFill, GPUPass (VS); ColorFill (FS) |
| 3.3 / 3.4 T#, S# | NTEX = NSAMP = `num_samp` (Mesa's single count, which covers v4's separate `ntex` / `nsamp`; gl-fbfetch shows 1 for a fetch-only FS), capped at 16; that many descriptors of each, T# holes zero, S# holes the default sampler (v5); nothing loaded when the FS binds no non-bindless T# or S# | 20 (7 with API samplers) |
| 3.5 constexpr samplers | the sidecar's `a8xx_tex_samp` at its slot (v4: the lowest free S#; the most any gate function loads is 9) | 16 |
| 3.6 framebuffer fetch | bindless set 4 (64 descriptors of 64 bytes); slot 55 + n = the attachment's view; `SP_PS_CONFIG.BINDLESS_TEX`; `GRAS_SC_CNTL` FLUSH_PER_OVERLAP_AND_OVERWRITE; the FS's layer input (the fetch's third coordinate) INTERP_ZERO, as Mesa's emit_interp_state (run 1 programmed FLAT) | InPlaceSover, InPlaceAlphaUnpremultiply, InPlaceColorOrClampEDR |
| 4.4 outputs: constant components | `SP_PS_OUTPUT_CONST_CNTL/MASK` from the outputs' `aliased_components` (ir3's alias.rt), as Mesa's emit_fs_outputs (run 1 left them 0) | AlphaTextureFragment (0x7), ShadowCompositeFragment (0x7), ShadowHorizontalBlurFragment (0x8) |
| 4.2 / 4.3 vertex input, system values | VFD instruction i = the VS's i-th generic input (attribute = slot - 15); vertex id in `VFD_CNTL_1` with one sideband; frag coord in `SP_REG_PROG_ID_2` | vertex id: the 3 CoreDisplay; frag coord: BlurComposite |
| 4.4 / 4.5 outputs, formats | up to 2 attachments in BGRA8, R8 or RG8; Metal's per-attachment blend = independent blend when there is more than one | 5 write R8/RG8 |
| 4.5 XR lowering (`--xr` only) | class 1 (554) as `B10G10R10A2_UNORM` (RB `10_10_10_2_UNORM_DEST`, WXYZ), class 2 (552) as `R16G16B16A16_UNORM` with WXYZ; `hw_blend` instead of the API blend (off when `blend_in_shader`, whose fetch is the same view), alpha write-masked where `hw_write_mask_clear` says; XR textures as the same views; dual source, classes 3-5 and border addressing on XR textures refused | the 5 XR scenes |

**Command stream.** The sequence is P7's (gallium's a8xx sysmem batch, proven on the A16 in run 3), generalised
the way the same Mesa functions generalise: emit_mrt's loop over attachments, the per-RT blend loop,
build_texture_state's holes, emit_vfd_dest's sideband, fd6_build_bindless_state's set 4, sp_xs_config's bindless
bits and build_prim_mode. One difference from P7 is on purpose: there is no 2D clear, because every target's
contents are uploaded (Metal loadAction .load). Render targets use Mesa's linear pitch (96 BGRA8 texels: 512
bytes; R8: 128; RG8: 256).

**Evidence against Mesa** (`tools/test-a16gpu-render.py`):

- `test_p7_equivalence`: given P7's pipeline and inputs, the generalised builder emits P7's register state at
  the draw, except addresses, the pitch (Mesa's 512 against P7's 384) and P7's clear. Against Mesa's own capture
  of the same two variants (`gl-tri-skylight.rd`) it differs only in P7's explained registers and the clear; 231
  registers are identical.
- `test_mesa_state`: six new captures from the real gallium driver for this chip (drm-shim, `rcap.c`), one per
  state class that `render` adds: two attachments (R8 + RG8), premultiplied source-over, framebuffer fetch,
  RGBA16F and 128x96 textures, 1D and 1D-array textures, and vertex id without attributes. The MRT formats,
  pitches and masks, the blend state, the framebuffer-fetch state and its slot-55 descriptor, every T# (dword for
  dword) and every S# (except GL's default WRAP_R) equal Mesa's. Every other difference is an address, a Metal
  convention or program state.

### What the Mesa comparison changed

The first version of `render` passed its own model. The captures found four differences, all now fixed:

| capture | was | Mesa (now) |
| --- | --- | --- |
| gl-fbfetch | `SP_PS_CONFIG` without `BINDLESS_TEX` | sp_xs_config sets it from the variant's `bindless_tex` (the fetch is a bindless T#) |
| gl-fbfetch | `GRAS_SC_CNTL` NO_FLUSH | FLUSH_PER_OVERLAP_AND_OVERWRITE for a sysmem framebuffer-fetch FS (build_prim_mode) |
| gl-fbfetch | one zero T# and S# loaded (NTEX 1) | nothing loaded; NTEX/NSAMP still `num_samp` |
| gl-mrt | independent blend off | on with two attachments (Metal's blend is per attachment) |

The model now checks all four.

## Memory and safety (P6/P7's rules, unchanged)

- Six arena regions above the Stage 4 pool (`adreno_x2.ARENA_LAYOUT` `r_ib`, `r_misc`, `r_code`, `r_data`,
  `r_tex`, `r_rt`; 0x1A10000..0x1FB0000, room for 40 functions) are mapped on CB0 at `RENDER_VA` 0x12000000 by
  `render` only. That VA is below the Stage 4 mirror at 1 GiB and clear of the CP's and P6/P7's VAs. Only `r_rt`
  is GPU-writable.
- Every address an IB carries, and every address inside the descriptors it loads (T# bases, the driver table's
  device-buffer VAs, the framebuffer-fetch view), comes from the window and is walked through CB0's tables, read
  back from DRAM, before each submission. R1 installs the tables (comparing the whole half-pool); each later
  submission re-reads the table image and stops if it changed. Every CPU write is an arena write.
- The CP must be the idle one `cp-fence` left for this GMU boot (P6/P7's `p67_prologue`): tokens, CP_SQE_CNTL,
  ring pointers, RBBM idle, CB0 and the stream table as cp-fence left them, no recorded fault or sticky error
  interrupt, room on the ring for every submission, and any leftover framebuffer mapping removed.
- Each submission uses a7xx_submit's ring words, then the bounded fence poll, the BV fence and the IB's own
  CACHE_FLUSH_TS, idle, CB0 FSR, and the error bits of CP_INTERRUPT_STATUS_GLOBAL and RBBM_INT_0_STATUS. Any of
  these stops the command (exit 2) with read-only diagnostics, and nothing more is submitted.
- Each target sits after a 4 KiB canary guard. Its row padding and every guard are checked after its draw; a
  write outside a target also stops the command.
- Every command stream and descriptor set is built in R0, in a scratch window, before anything is written: a
  refusal of the builder stops R0, never a run after R1 or between draws.
- An image outside tolerance is a *result*, not a fault: the remaining functions still draw, and R3 exits 2
  naming them.
- `render.verdict.gate_met` is the gate: all 28 drawn and every one within tolerance. `gate` only says all 28
  were drawn. The XR scenes and a `--functions` subset never decide it.

## The draws (the 28, then the 5 XR scenes of `--xr`)

Figures from the model (`tools/test-a16gpu-render.py`, a `--xr` run), after one cp-fence fence. Tolerance rule
(P7's): per
channel |A16 - golden| <= tol everywhere, except at most `edge budget` pixels within one pixel of a triangle
edge. 3 is the Stage 6 goldens' tolerance; 4 where the function applies pow curves, a divide by alpha, a
many-tap filter or a dither step; 6 for the ripple's pow(x, 200). "llvmpipe max" is the same scene on Mesa
llvmpipe running the NIR that air2ir3 builds from the same AIR (`llvmpipe_predict.py`): the max |diff| against
the golden. All 26 scenes llvmpipe can take are within tolerance (21 of the 28, all 5 XR); n/a marks RG8
targets, two targets or 1D textures, which nirexec does not take. XR targets are compared in XR codes (0..1023,
BGR10_XR alpha bits ignored), tolerance 2 (Stage 6's XR scenes: within 2 codes).

| # | scene (lib) | VS | VS / FS variant id | cache | IB dwords | refs walked | fence, wptr (after cp-fence's 0xf3a00001 / 14) | targets | tol. (edge budget) | llvmpipe max | golden sha256 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `SimpleColorFragment` (SkyLight) | `SimpleColorVertex` | 0x560067a216a5ab79 / 0x6215fa85a6763c05 | hit | 1023 | 11 | 0xf3a00002, 49 | bgra8 | 3 (24) | 1 | 7bfd4d4b7c35... |
| 1 | `SimpleTextureFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x31757a95c3b5f88d | hit | 1031 | 14 | 0xf3a00003, 84 | bgra8 | 3 (24) | 1 | 1939a3844f5a... |
| 2 | `SimpleTextureEDRFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xa392e1c1821612de | hit | 1031 | 15 | 0xf3a00004, 119 | bgra8 | 3 (24) | 0 | aafa25e27c03... |
| 3 | `SimpleTextureScaleToSDRFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x7d97b97b56130b74 | hit | 1031 | 14 | 0xf3a00005, 154 | bgra8 | 3 (24) | 0 | d127e70d7874... |
| 4 | `SimpleTextureFragmentUV` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x2d2be0bf7b15cb86 | hit | 1031 | 14 | 0xf3a00006, 189 | rg8 | 3 (24) | n/a | 4c2054048c00... |
| 5 | `SimpleTextureTintFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xe0de26ac8dc52521 | hit | 1031 | 15 | 0xf3a00007, 224 | bgra8 | 3 (24) | 1 | ce33534c7015... |
| 6 | `SimpleGrayscale` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x8999fda0e8ac447a | hit | 1031 | 14 | 0xf3a00008, 259 | bgra8 | 3 (24) | 0 | ec288f007677... |
| 7 | `AlphaTextureFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xfb1a5923e38beff7 | hit | 1031 | 14 | 0xf3a00009, 294 | bgra8 | 3 (24) | 0 | a98c520d0f84... |
| 8 | `GroupFadeTextureFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xfad7359865e20718 | hit | 1031 | 15 | 0xf3a0000a, 329 | bgra8 | 3 (24) | 1 | df17c327f244... |
| 9 | `SimpleTextureLightingFragment` (SkyLight) | `SimpleTextureLightingVertex` | 0x452ef3b7b95e6aa4 / 0x94c356171f466583 | hit | 1033 | 15 | 0xf3a0000b, 364 | bgra8 | 3 (24) | 1 | aecfae79033e... |
| 10 | `StyledDebugFillFragment` (SkyLight) | `StyledDebugFillVertex` | 0x78bacf02060f1664 / 0x53ca55ce815461c2 | hit | 1025 | 12 | 0xf3a0000c, 399 | bgra8 | 3 (24) | 1 | fc825d5b755a... |
| 11 | `RippleFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xcb48316270708571 | hit | 1031 | 16 | 0xf3a0000d, 434 | bgra8 | 6 (24) | n/a | 7c78803aebfd... |
| 12 | `ShadowCompositeFragment` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x8ab1e4863a03a4f9 | hit | 1045 | 15 | 0xf3a0000e, 469 | bgra8 (blend) | 3 (24) | 1 | 7d40ef00f456... |
| 13 | `UberCompositeFragment` (SkyLight) | `UberCompositeVertex` | 0x99b9467b6137f0bf / 0xdf5fe1f4ce78f270 | hit | 1043 | 16 | 0xf3a0000f, 504 | bgra8 (blend) | 4 (48) | 2 | 2034631207b0... |
| 14 | `BlurComposite` (SkyLight) | `BlurCompositeVertex` | 0xa56069c0ad17f4a9 / 0x4bd11cc295032a5e | hit | 1051 | 18 | 0xf3a00010, 539 | bgra8 | 4 (24) | 1 | 2ba75068c8be... |
| 15 | `UberResampleLanczosFragmentBGRA` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x9393235003e0dea9 | hit | 1039 | 17 | 0xf3a00011, 574 | bgra8 | 4 (24) | n/a | 74aff63d317d... |
| 16 | `UberResampleLanczosFragmentYCbCr` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xe5d74eafc401690d | hit | 1069 | 17 | 0xf3a00012, 609 | r8 + rg8 | 4 (24) | n/a | aa2eec291ea5, 3a5598741a7c... |
| 17 | `ColorFillYCbCr` (SkyLight) | `SimpleColorVertex` | 0x560067a216a5ab79 / 0xae15468096c2469c | hit | 1061 | 13 | 0xf3a00013, 644 | r8 + rg8 | 3 (24) | n/a | b5b8ca3aa831, 635d48774da1... |
| 18 | `ColorFillYCbCr_ChromaOnly` (SkyLight) | `SimpleColorVertex` | 0x560067a216a5ab79 / 0x8264733ec3998ae2 | hit | 1033 | 12 | 0xf3a00014, 679 | rg8 | 3 (24) | n/a | 635d48774da1... |
| 19 | `ShadowHorizontalBlurFragment` (SkyLight) | `SimpleVertexShadow` | 0xd2e599bec7a47631 / 0x63357ee13a87dfce | hit | 1037 | 15 | 0xf3a00015, 714 | bgra8 | 4 (24) | 1 | 1a54f5bb8081... |
| 20 | `ShadowVerticalBlurFragment` (SkyLight) | `SimpleVertexShadow` | 0xd2e599bec7a47631 / 0xe468034f6e0f83b7 | hit | 1037 | 15 | 0xf3a00016, 749 | r8 | 4 (24) | 0 | b0b5aad51818... |
| 21 | `ShadowVerticalBlurRGBAFragment` (SkyLight) | `SimpleVertexShadow` | 0xd2e599bec7a47631 / 0x5aa3884f74b8f0f8 | hit | 1037 | 15 | 0xf3a00017, 784 | bgra8 | 4 (24) | 0 | e30a9e075270... |
| 22 | `InPlaceSover` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x002f3cf0ef550b03 | hit | 1023 | 14 | 0xf3a00018, 819 | bgra8 (fetch) | 3 (24) | 1 | a26d1ddc8126... |
| 23 | `InPlaceAlphaUnpremultiply` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x04be673b15d1212a | hit | 1023 | 13 | 0xf3a00019, 854 | bgra8 (fetch) | 4 (24) | 1 | 3d368ea8db92... |
| 24 | `InPlaceColorOrClampEDR` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xb1ed581c1fe06f35 | hit | 1023 | 14 | 0xf3a0001a, 889 | bgra8 (fetch) | 4 (24) | 1 | d2e7a5b26d6e... |
| 25 | `TextureCopy` (CoreDisplay) | `ViewportToNDC` | 0x83f9ce8ce75dda47 / 0x7d5ac6d003e5d22e | hit | 1007 | 16 | 0xf3a0001b, 924 | bgra8 | 3 (24) | 1 | 91cdb39bdcfc... |
| 26 | `ColorFill` (CoreDisplay) | `ViewportToNDC` | 0x83f9ce8ce75dda47 / 0x3405cc273565f923 | hit | 999 | 15 | 0xf3a0001c, 959 | bgra8 | 3 (24) | 0 | 7f9ace1113d6... |
| 27 | `GPUPass` (CoreDisplay) | `ViewportToNDC` | 0x83f9ce8ce75dda47 / 0x09f08d8680da0fa1 | hit | 1017 | 23 | 0xf3a0001d, 994 | bgra8 | 4 (24) | n/a | 7ef70779193a... |
| 28 | `xr554_tint_none` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xddd0baf51463bd20 | hit | 1031 | 15 | 0xf3a0001e, 1029 | bgr10_xr | 2 codes (24) | 1 | ebe8136ab22c... |
| 29 | `xr554_tint_src_alpha` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xc12e27700f75fda3 | FS online | 1031 | 15 | 0xf3a0001f, 1064 | bgr10_xr (blend) | 2 codes (24) | 1 | 6431b2cc1b98... |
| 30 | `xr552_tint_premul_over` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0xa787cbb2173cdf4c | hit | 1031 | 17 | 0xf3a00020, 1099 | bgra10_xr (blend, fetch) | 2 codes (24) | 1 | 7180f3ce4bf7... |
| 31 | `xr552_sample_to_xr` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x27c50ef170c43194 | FS online | 1031 | 14 | 0xf3a00021, 1134 | bgra10_xr | 2 codes (24) | 0 | de2a5b219215... |
| 32 | `xr554_sample_linear` (SkyLight) | `SimpleVertex` | 0xbedc799beebe2a11 / 0x4d4e3df3f1f22c24 | hit | 1031 | 14 | 0xf3a00022, 1169 | bgra8 | 2 codes (24) | 1 | 1a648f1a73ae... |
| p0 | `probe_ubo_lo` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x06f9fa54d27a39a5 | online | 1053 | 27 | 0xf3a00002*, 49 | bgra8 | 1 (0) | n/a | 554a0639cd09... |
| p1 | `probe_alias_rgb` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x6fba6dc4513af1f4 | online | 1023 | 11 | 0xf3a00003*, 84 | bgra8 | 1 (0) | n/a | 34356119fae4... |
| p2 | `probe_alias_zw` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x072827d335359f68 | online | 1023 | 11 | 0xf3a00004*, 119 | bgra8 | 1 (0) | n/a | c07294ae9b1f... |
| p3 | `probe_fetch` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x144bfec972663255 | online | 1023 | 13 | 0xf3a00005*, 154 | bgra8 (fetch) | 1 (0) | n/a | 8da11b893dea... |
| p4 | `probe_a8` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x70bed7f6ee675d31 | online | 1031 | 14 | 0xf3a00006*, 189 | bgra8 | 1 (0) | n/a | 8970cc1a6fd9... |
| p5 | `probe_ubo_hi` (Probe) | `probe_vs` | 0x4aae0fdf3d6e8fd0 / 0x17f8a31b2a8b1c72 | online | 1083 | 42 | 0xf3a00007*, 224 | bgra8 | 1 (0) | n/a | ba5e4f3c3837... |

`*` probes: the fence and wptr of `render --probes` right after cp-fence (they draw alone). `probe_ubo_hi`, the one
probe outside Mesa's envelope (31 FS UBO descriptors, `ldc` from UBO 16..30), draws last in any command that selects it.

Every draw uses 35 ring dwords (a7xx_submit) and walks 11 to 23 references through CB0: the IB's own and those
inside its descriptors (T# bases, the driver table's device-buffer VAs, the framebuffer-fetch view).

## Tests

```shell
python3 tools/test-a16gpu-render.py      # PASS: 493 a16gpu render model checks (offline, no hardware)
```

The model (`FakeRender`, which extends P6/P7's `FakeP67`) identifies each draw's pipeline by the code at
SP_VS_BASE and SP_PS_BASE. Then it checks, through CB0 in model DRAM:

- instruction sizes, register footprints, const lengths, and that no private memory is programmed;
- every UBO descriptor against its buffer and the pushed ranges; the driver table and the device buffers it
  points at;
- NTEX, NSAMP, TSIZE and the bindless bits; every T# (format, swap, type, depth, pitch, layer stride) against
  the texels behind it; every S# (API: filter, wrap, coordinates; constexpr: the sidecar's words); the fetch
  descriptor against its attachment;
- vertex fetch against the vertex descriptor and the vertex bytes; the vertex-id sideband; the linkage by name;
- FS outputs and output masks, the alias.rt output constants (SP_PS_OUTPUT_CONST_CNTL/MASK); MRT formats, swaps
  and pitches; blend (enables, factors, independent); single-prim mode; the viewport;
- every UBO index below 16; a layer (or viewport) input the VS does not write programmed INTERP_ZERO.

When all of that holds it writes the golden through CB0 (the model cannot execute ir3; the pixels are the A16's
experiment). The tests break the builder one binding at a time (a T# address, an S# wrap, the fetch view, the
driver table, two crossed varyings) and see the draw refused. They inject a fence that never comes, a CP fault,
a sticky error, a write into a guard band and a golden mismatch (all 28 still drawn, R3 names the function).
They check the refusals: before cp-fence, without ring room, an unknown function, unpinned assets, a scene that
no longer matches its variants, CB0 tables that changed after R1, and a command stream the builder refuses for
the last function (it stops R0: nothing written, nothing drawn). The model refuses a load of more than 16 T#/S#, and `num_samp` 17 without a fetch (or 18 with one) is refused. And they run a subset, a second render, quiesce and a second cycle, P7 after
render, and the CLI.

## Beyond the gate: the XR lowering classes (`render --xr`)

WindowServer renders into BGR10_XR / BGRA10_XR only on extended-range displays, but it samples such surfaces from
other processes on any display (Stage 6 FORMATS.md). Five scenes exercise BINDING-ABI 4.5 on the A16: a class-1
target with plain writes (alpha masked), a class-1 target with source-alpha blending on the RB (`hw_blend`), a
class-2 target with premultiplied source-over blended in the shader (framebuffer fetch of the class-2 view), a
class-2 texture into a class-2 target, and a class-1 texture filtered into BGRA8. Their Metal goldens are XR
bytes; llvmpipe (whose nirexec runs the same lowering) is within 2 codes on all five. They are not part of the
gate and draw after the 28, so a failure there costs nothing of the gate's result. No Mesa capture covers them:
the RB formats come from Mesa's format table (`B10G10R10A2_UNORM` renders as `10_10_10_2_UNORM_DEST`), and the
B,G,R,A swap on a 16-bit format is on BINDING-ABI 4.5's A16 test list.

## A16 run 1 (2026-09-25)

Logs are `build/a16-gpu/render-run1-*.json`, with the readbacks in `render-run1-r25.<fn>.rt<i>.raw`. The contact
sheet is `render-run1-r25-sheet.png` (A16 | M3 Max | diff x8). The arena was at 0xb7e00000 on the fresh ESP boot of
payload 54f6cb03.

- **The full 28 (`render-run1-all28`):** 25 draws completed. Then TextureCopy (CoreDisplay, VS ViewportToNDC)
  hit a CB0 translation fault at **FAR 0x0**, with the fence written, no SError and the CP idle. It reads its vertex
  buffer's address from the driver table at **UBO 31**. That read returned 0 (most likely because a8xx honours UBO
  0..15 only; see the diagnosis), so the load used VA 0. The
  run stopped before R3.
- **The other 25 (`render-run1-r25`):** 19 are within tolerance (max 0-4). Failed:
  - InPlaceSover, InPlaceAlphaUnpremultiply and InPlaceColorOrClampEDR: framebuffer fetch through bindless
    set 4 returns flat colour or black.
  - AlphaTextureFragment: blocky garbage (first reading: the alpha-only texture; wrong, the texture is RGBA8 and
    the alpha came out exact).
  - ShadowCompositeFragment: striped garbage (first reading: a fix2 compile guessing an unset function constant;
    wrong, every constant is set).
  - ShadowHorizontalBlurFragment: visually right, max 255 (first reading: a few pixels; in fact the alpha of all
    4096 pixels of the quad, which the contact sheet does not show).
  - The diagnosis below replaces these first readings.
- **Proven on the X2-90:** the P7 path generalised to real WindowServer pipelines. That covers SkyLight
  textured, EDR, tint, grayscale, lighting, group-fade, the styled debug fill, ripple, UberComposite (blend),
  BlurComposite, Lanczos BGRA and YCbCr (two targets), the YCbCr colour fills, and the vertical shadow blurs.

### A16 run 1: diagnosis

Every IB of both runs was rebuilt offline from the tool and the pinned assets of that day, byte-identical to the
sha256 each submission recorded (51 of 51), and walked as the CP would. The readbacks were compared with the
goldens per channel. Four causes. Each IB difference from Mesa is proven from the recorded streams; two points are
inferences, marked in the table, and the fixes do not depend on them (the probes in section 4c of the runbook test
both):

| failure | what the A16 wrote | cause | evidence | fix |
| --- | --- | --- | --- | --- |
| TextureCopy (then ColorFill, GPUPass not drawn): CB0 FSR 0x402 at FAR 0x0 | fence written, CP idle | the driver table at **UBO 31** reads zeros. Most likely a8xx honours UBO 0..15 only (inferred: gallium advertises `max_const_buffers` 16); the other reading that fits is a 32-unit `CP_LOAD_STATE6` dropped or truncated. v6 avoids both (one descriptor, UBO 0) | IB `e59c2802...` loads 32 VS UBO descriptors, descriptor 31 = the table (VA 0x12164300, 24 vec4) holding 0x12164000/100/200; the VS's `ldc.2.k.imm c[a1.x], 0, 31` then `ldg g[c4.x + 8 vid]` read VA 0; the same `ldc.k` path works from VS UBO 1 and FS UBO 0, 2, 5 in all 19 passing draws, so the index is the only difference; freedreno advertises 16 constant buffers and Turnip binds app UBOs bindless (ir3's own driver UBOs can land above 15 via `ir3_get_driver_ubo`, so "no Mesa stream uses >15" is an inference, not shown by the source) | BINDING-ABI v6 (compiler patch): the table at 1 + the highest declared constant buffer (UBO 0 here), every index below 16; `render` refuses anything else |
| InPlaceSover, InPlaceAlphaUnpremultiply, InPlaceColorOrClampEDR | inside the quad one value in all 4212 pixels: the source colour (0x59,0x1a,0x33,0x73) for Sover, (0,0,0,0) for the other two | the fetch returned 0: its coordinate is (x, y, **layer**) (nir_lower_fb_read) and the layer input, which the VS never writes, was programmed FLAT at location 4, a location the VS does not fill: an out-of-range layer | Sover's output = src + 0 x (1 - a) exactly; Mesa's gl-fbfetch capture programs its one varying, the layer, INTERP_ZERO (VPC_VARYING_INTERP_MODE 0x2) where run 1 had 0x100 (FLAT at 4); every other fetch register already equal to Mesa's. The difference is proven; that it is the cause is not: a stale bindless set-4 descriptor explains the same data, and `probe_fetch` (first draw, then again after three draws with set 4 at the zeroed page) separates the two | `interp_regs`: Mesa's emit_interp_state (layer/viewport not written by the VS -> INTERP_ZERO) |
| AlphaTextureFragment | alpha exact (max diff 0), rgb garbage on 4900 pixels | ir3 writes the constant rgb (0, 0, 0) with **alias.rt** (aliased_components 0x7); `SP_PS_OUTPUT_CONST_CNTL/MASK` were 0, so rgb came from registers the code never writes (what earlier waves left: blocks of photo, lines) | per-channel diff; the variant's outputs; Mesa's gl-blend capture programs CONST_CNTL 1 / MASK 0xc for its own alias.rt | `fs_outputs`: Mesa's emit_fs_outputs (mask, enable, outputs counted by aliased components) |
| ShadowCompositeFragment | rgb garbage (premultiplied source-over of it), alpha exact | the same alias.rt constants (rgb 0, 0x7). Not an unset-constant compile: its four constants are all set (`defined` true in the sidecar), and fix round 4 + ABI v6 compiles the same code except the constexpr S# immediate | as above; fix2 vs ABI 6 disassembly | as above; the scene keeps `enable_fade` only (nothing shows WindowServer setting the others) |
| ShadowHorizontalBlurFragment | rgb within 1; **alpha 255 on all 4096 pixels of the quad**, golden 0 (the contact sheet shows rgb only) | the same, for alpha (aliased 0x8, constant 0) | per-channel diff | as above |

The 19 that passed use none of these three states (no fetch, no alias.rt, no device buffers). The run proved the
P7 path generalised to real WindowServer pipelines; A16 run 2 has to re-prove them with the ABI v6 variants,
whose gate code differs from run 1's only in constexpr S# immediates and the CoreDisplay UBO index. The command
streams differ more than the code. Rebuilt with run 1's arena base and fences, 8 of the 19 IBs are byte-identical
(SimpleColor, SimpleTexture, SimpleTextureEDR, StyledDebugFill, UberComposite, UberResampleLanczosYCbCr,
ColorFillYCbCr, ColorFillYCbCr_ChromaOnly). The other 11 (SimpleTextureScaleToSDR, SimpleTextureFragmentUV,
SimpleTextureTint, SimpleGrayscale, GroupFadeTexture, SimpleTextureLighting, Ripple, BlurComposite,
UberResampleLanczosBGRA, ShadowVerticalBlur, ShadowVerticalBlurRGBA) change only in the FS texture state that
follows from ABI v4's lowest-free constexpr S#: `SP_PS_CONFIG` NTEX/NSAMP 16 -> 1..9, `SP_PS_TSIZE` 16 -> 1..9,
the unit counts of the two `CP_LOAD_STATE6_FRAG` SB6_FS_TEX loads 16 -> 1..9, `SP_PS_SAMPLER_BASE` (the S# array
follows a shorter T# array), the FS code (S# immediates) and the S# array (the constexpr sampler moves from S#15
to the lowest free slot; holes hold the default sampler, ABI v5). The three alias.rt failures carry the same
texture-state change besides CONST_CNTL/MASK. One T# and one S# is the configuration P7 and SimpleTextureFragment
proved.

### Probes (`render --probes`, never the gate)

Six draws of our own Metal functions (`tools/a16gpu-render/probes/probes.metal`, built by `make-probes.sh`,
sha-pinned), each isolating one piece of state; their M3 Max goldens equal their analytic values (checked by
the model test). Tolerance 1, no edge budget. The table is in draw order: `probe_ubo_hi` programs the only state
Mesa never emits (31 FS UBO descriptors, `ldc` from UBO 16..30), so `select()` draws it after everything else in
any command (`--probes` or `--functions`), and the runbook runs it on its own after the gate (section 4c, step 5).

| probe | state it isolates | pass means | if it fails |
| --- | --- | --- | --- |
| `probe_ubo_lo` | UBO 0..15 through the preamble (`ldc.k`): band k (6 pixels wide) = constant buffer(k) | all 16 UBO slots work (`bands[k].reads` "own value") | a band "zero" = that slot is not honoured; every band "UBO 0 value" = the frag coord (the band select) is wrong |
| `probe_alias_rgb` | alias.rt of x, y, z (mask 0x7), alpha from a varying | the output-constant fix | garbage rgb = CONST_CNTL/MASK still not taking effect |
| `probe_alias_zw` | alias.rt of z, w (mask 0xc, Mesa gl-blend's own mask) | as above | as above |
| `probe_fetch` | framebuffer fetch alone: the target read back with R and B swapped | the layer INTERP_ZERO fix | 0 inside the quad: run `--functions probe_fetch` as the first draw after cp-fence too; if only the later one (after three draws that point set 4 at the zeroed page) fails, suspect the SP's bindless descriptor cache (Mesa a8xx emits no bindless invalidate either). The first-draw check assumes no stale set-4 descriptor survives from before the session |
| `probe_a8` | an A8Unorm texture (FMT6_A8_UNORM, identity swizzle, pitch 64) sampled at texel centres | A8 textures work (not used by the 28; asked for by run 1's AlphaTexture hypothesis) | red channel off = format / swizzle; blocky = pitch |
| `probe_ubo_hi` | UBO 16..30 (compiled with `--test-ubo-slots 32`; UBO 0..15 bound to the low probe's values) | **expected to fail**: the bands record what the hardware does with indices above 15: "zero" (ignored, run 1's reading) or "UBO k-16 value" (index wrapped) | "own value" everywhere would mean the table is 32 deep and run 1 had another cause |

## A16 run 2 (2026-09-25): the gate is met

Logs: `build/a16-gpu/render-run2-*.json`; sheet `render-run2-gate-sheet.png`. The render-fix patch is applied
(binding ABI v6, which puts the driver table at 1 + the highest declared UBO; `interp_regs`; `fs_outputs`).
Variants come from fix4 plus air2ir3-abi6. Stage generation 1, with the arena at 0xb7e00000 reserved by the ESP payload.

- **probe_fetch alone:** passes. So does the second fetch, after other draws. The framebuffer fetch fix
  (INTERP_ZERO for the layer input) is PROVEN.
- **In-envelope probes:** alias_rgb, alias_zw (output constants) and a8 pass. probe_ubo_lo fails: every slot 1..15
  reads UBO 0's value.
- **The nine run-1 failures:** 9/9 within tolerance, including CoreDisplay TextureCopy, ColorFill and GPUPass.
- **The gate, `render --write`:** 28/28 within tolerance, `gate_met` true. **Stage 6's A16 half is MET.**
- **probe_ubo_hi (out of envelope, by design last):** slot 16 reads its own value, and slots 17..30 read UBO 16's.

**Open finding.** In both UBO probes the first slot reads its own value and every later slot reads the
first slot's. Slot 16 is readable, so "a8xx honours only UBO 0..15" is not established. The pattern points at
the probes' descriptor layout, for example packed UBO bases truncated by an alignment rule, or at descriptor-array
indexing. It does not affect the gate, which uses UBO indices 0..5 with separate buffers. Settle it before the
plug-in relies on more than a handful of UBOs.

## Open for the A16 (the experiment)

- Run 2: the ABI v6 variants for all 28 (the CoreDisplay three have never drawn), the three state fixes above, and
  the probes (runbook section 4c).
- `--xr` only: the WXYZ swap on `16_16_16_16_UNORM` (class 2) and the `_DEST` RB format for class 1.
- Everything else is P7's proven state, or state equal to Mesa's for the same operation.

## Fix round 4 and BINDING-ABI v6

The variants are fix round 4's compiler (`adreno-stage6/fix4/`) with `adreno-render-fix/air2ir3-abi6.patch`
applied: BINDING-ABI v6 = v5 + every UBO index below 16. What v4/v5 changed lands in `tools/a16gpu_bindings.py`
only: separate NTEX/NSAMP in the sidecar (this port still programs Mesa's single `num_samp`, which covers them),
constexpr samplers in the lowest free S#, the default sampler in S# holes, `requires_no_attachment` (refused;
no scene has it), the per-sample flag (no scene is multisampled). `check_variant` requires `abi_version` 6 and
`ubo_slots` 16 (probe_ubo_hi excepted).

## Review fixes

- **16 T#/S# at most.** v3's `num_samp` is 17 for `xr552_tint_premul_over` (T#0, the constexpr sampler at S#15
  and the bindless framebuffer read). `texture_counts` now drops that one extra count when the FS fetches the
  framebuffer and refuses any other count above 16; the model flags any CP_LOAD_STATE6 of more than 16 T#/S#. Only that function's IB
  changes (NTEX/NSAMP 16, 16 T# and 16 S# loaded); the 28's IBs are byte-identical.
- **Import order.** `a16gpu_stage3` imports `a16gpu_render` only once `a16gpu_p6p7` has finished loading, so
  `import a16gpu_p6p7` first works again; `add_parsers` still registers `render`.
- **No late refusals.** R0 builds every command stream and descriptor set in a scratch window.
- **`verdict.gate_met`** (all 28 drawn, all within tolerance) is the gate; `verdict.gate` only means all 28 drawn.
- **Scene descriptions** no longer claim what the goldens do not check (ShadowHorizontalBlurFragment's
  buffer(1), BlurComposite's noise, ColorFill's single colour point).
- **Known gap in `adopt` (not changed here):** its docstring says it refuses when a render window is mapped,
  but it probes only the framebuffer rows and root entries 1..3. After `render` (or P6/P7) their arena-only
  windows at 0x11000000 / 0x12000000 are still in CB0 at adopt. Harmless (snap_smmu is not mapped); adopt
  should probe those two addresses.
