# Mesa's own a8xx command streams for the state `render` adds to P7

`tools/test-a16gpu-render.py` (`test_mesa_state`) compares what `tools/a16gpu_render.py` builds with these,
register by register at the draw and descriptor by descriptor, for the state classes P7 (and its reference,
`../a16gpu-p6p7/gl-tri-skylight.rd`) did not have. The shaders here are GLSL compiled by Mesa, so program state
(register footprints, output registers, varyings, vertex fetch, constlen) differs by design; everything else must
be equal or be an address or one of P7's Metal conventions. `test_p7_equivalence` separately checks the
generalised builder against P7 and `gl-tri-skylight.rd` for the exact P7 variants.

| File | sha256 | Operation (rcap.c mode) | Compared with |
| --- | --- | --- | --- |
| `gl-mrt.rd` | aa4c68f666edc4ce... | R8 + RG8 colour attachments 96x64, FS writes both | `ColorFillYCbCr`: MRT formats, swaps, pitches (128 / 256), flag pitch, LRZ MRT0 format, sRGB, MRT count, output masks, per-RT blend/control, **independent blend** |
| `gl-blend.rd` | a4bad4e8c03f7ad6... | BGRA8 96x64, blend ONE, ONE_MINUS_SRC_ALPHA | `ShadowCompositeFragment`: blend factors/opcodes, RB/SP enables, blend reads dest |
| `gl-fbfetch.rd` | 6fcb7d9969639f44... | BGRA8 96x64, EXT_shader_framebuffer_fetch | `InPlaceSover`: **GRAS_SC_CNTL FLUSH_PER_OVERLAP_AND_OVERWRITE**, **SP_PS_CONFIG BINDLESS_TEX**, NTEX/NSAMP 1 with no T#/S# loaded, the set-4 slot-55 descriptor (patch_fb_read_sysmem's view) |
| `gl-tex.rd` | 53ab92e3f823ffb9... | RGBA16F 64x64 nearest/repeat + RGBA8 128x96 linear/clamp | T# for both (pitch 512, MIN_LINE_OFFSET 3 / 2, layer stride), S# nearest-repeat and linear-clamp (WRAP_R: GL REPEAT, Metal clamp) |
| `gl-tex1d.rd` | c66e2c4b37566fbe... | desktop GL: RGBA8 1D 256 + 1D array 256x6 | T# for both (type 1D, depth 1 / 6, layer stride 4 KiB) |
| `gl-vid.rd` | 941007c162b5109a... | no vertex attributes, gl_VertexID | `TextureCopy`: VFD_CNTL_0 (no fetch), VFD_CNTL_1 vertex-id register, PC_VS_INPUT_CNTL sideband 1 |

The four entries in bold were wrong in the first version of `render` and are what these captures found
(`docs/A16-ADRENO-STAGE6-RENDER.md` "What the Mesa comparison changed").

Not covered by any capture (gallium never does it): 32 UBO descriptors with the driver table at UBO 31
(freedreno exposes 16; Turnip binds application UBOs bindless) -- BINDING-ABI 3.9 lists it on the A16 test
list; the three CoreDisplay functions draw last.

## How they were made (reproducible, Linux VM, no GPU)

- Mesa: the Stage 6 tree (`adreno-stage6/fix2/third_party/mesa`, bbeb2dca9fee with Stage 6's ir3 RA patch; the
  build-without-drivers and bison-2.3 patches reverted to upstream in the copy, since the VM has bison 3),
  copied into an aarch64 Ubuntu VM (lima `j813-builder`, `~/a16render-mesacap`) and built with
  `-Dgallium-drivers=freedreno -Dvulkan-drivers= -Dfreedreno-kmds=msm -Dtools=drm-shim -Dplatforms= -Degl=enabled
  -Dgbm=disabled -Dglx=disabled -Dopengl=true -Dgles2=enabled -Dllvm=disabled`.
- The drm-shim's chip table gains the X2-90 (`chip_id 0x44070041`, `gmem_size 21 MiB`, a6xx_catalog.c), as for
  the P6/P7 captures; capture copy only.
- `rcap.c` (this directory): EGL surfaceless, GLES 3.2 (desktop GL 3.3 core for `tex1d`), one flushed draw per
  run. Every run:

```shell
LD_LIBRARY_PATH=$I/lib/aarch64-linux-gnu LD_PRELOAD=$I/lib/aarch64-linux-gnu/libfreedreno_noop_drm_shim.so \
MESA_LOADER_DRIVER_OVERRIDE=msm FD_CHIP_ID=44070041 GALLIUM_DRIVER=freedreno \
FD_RD_DUMP=enable FD_RD_DUMP_PATH=<dir> FD_MESA_DEBUG=sysmem,noubwc,notile,nolrz,inorder,nofp16 ./rcap <mode>
# -> <dir>/rcap_submit00000.rd, renamed gl-<mode>.rd
```

Each reported `GL_RENDERER Adreno (TM) X2-90`.
