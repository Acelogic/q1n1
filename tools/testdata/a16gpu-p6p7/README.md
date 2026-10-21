# Mesa's own a8xx command streams for P6/P7

`tools/test-a16gpu-p6p7.py` (`test_reference`) compares what `tools/a16gpu_p6p7.py`
builds with these, register by register at each CP_BLIT / CP_DRAW_INDX_OFFSET,
packet kind per register, and packet/event order (docs/A16-ADRENO-STAGE3.md
"P6 and P7" lists the differences that are expected and why).

| File | sha256 | What |
| --- | --- | --- |
| `gl-cleartex-bgra.rd` | 6f333800ff95b53a... | `glClearTexSubImageEXT` on a 256x256 BGRA8 texture: gallium `fd6_clear_texture` -> 2D fill (P6 clear) |
| `gl-blit-bgra.rd` | aab2d34ac2863f95... | `glBlitFramebuffer` 256x256 BGRA8 -> BGRA8, nearest: `fd6_blit` -> `emit_blit_texture` (P6 copy) |
| `gl-tri-bgra.rd` | 834723c01892e609... | one textured triangle, GLSL compiled by Mesa (VS mvp from a std140 UBO, FS `texture(s, v)`, linear, clamp_to_edge): the GL path's NTEX/NSAMP and PIXLODENABLE |
| `gl-tri-skylight.rd` | c13491698ee56651... | the same draw with the two air2ir3 variants P7 uploads (`P67_VARIANT_VS/FS`) loaded in place of Mesa's compile: Mesa's program state for the exact ir3 code |

Each file is the single submit FD_RD_DUMP wrote for that operation
(`p67cap_submit00000.rd`), gzip as Mesa writes it, renamed.

## How they were made (reproducible, Linux VM, no GPU)

- Mesa: the Stage 6 tree (`adreno-stage6/fix/third_party/mesa`, bbeb2dca9fee
  with Stage 6's patches), copied read-only into an aarch64 Ubuntu VM and
  built with `-Dgallium-drivers=freedreno -Dfreedreno-kmds=msm
  -Dtools=freedreno -Dbuild-tests=false` (drm-shim `freedreno_noop` included).
- `p67_override_patch.py` (run in the Mesa copy, never shipped): a hook in
  `ir3_shader.c` `create_variant` that, when `P67_VARIANT_VS`/`P67_VARIANT_FS`
  name an `ir3_store_variant` blob (air2ir3 `--variant`), loads it through
  `ir3_disk_cache.c`'s `retrieve_variant` instead of compiling.
- `p67cap.c`: EGL surfaceless + GLES 3, the three operations each flushed on
  their own (`p67cap cleartex|blit|tri bgra`).
- Environment of every run:

```shell
LD_PRELOAD=$I/lib/aarch64-linux-gnu/libfreedreno_noop_drm_shim.so MESA_LOADER_DRIVER_OVERRIDE=msm \
FD_CHIP_ID=44070041 GALLIUM_DRIVER=freedreno \
FD_RD_DUMP=enable FD_RD_DUMP_PATH=<dir> FD_MESA_DEBUG=sysmem,noubwc,notile,nolrz,inorder,nofp16 \
./p67cap <mode> bgra
# gl-tri-skylight: add P67_VARIANT_VS=vs.ir3v P67_VARIANT_FS=fs.ir3v (from tools/a16gpu-p7/make-p7-shaders.sh;
# sha256 efa06b47... / 92226db7...)
```

`sysmem,noubwc,notile,nolrz` put gallium on the path P6/P7 take (direct
rendering, linear surfaces, no LRZ); `inorder` keeps one batch per
operation; `nofp16` keeps the GLSL shaders comparable. `cffdump --no-color`
decodes the files for reading.
