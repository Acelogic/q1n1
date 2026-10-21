# `render` assets: the variants, the M3 Max goldens, how both are made

> `render-shaders.json` is not part of the public repository: it is compiled from shaders shipped with macOS.
> Generate it on your own Mac as described below; `.gitignore` keeps it out of commits.

`tools/a16gpu_render.py` pins the sha256 of `render-shaders.json` and of `goldens/goldens.json` (which pins
every golden image); `render` stops at R0 if either differs, if a scene no longer matches the digest recorded in
both, or if a golden is missing. Regenerate only on purpose, all of it together, then update the two pins in the
same change.

| File | Made by | What |
| --- | --- | --- |
| `wscts-functions.txt` | wscts.m on the M3 Max (Snapintosh, `adreno/windowserver-metal-usage/cts/ref-m3max.txt`) | the 28 functions and their vertex pairing: the gate's list (provenance) |
| `render-shaders.json` | `make-render-shaders.sh` (+ `make_render_shaders.py`, `export_render_shaders.cpp`) | the 78 ir3 variants (39 VS + 39 FS: the 28, the 5 XR scenes and the 6 probes) with sidecars, keys, variant ids, cache provenance, by-name linkage |
| `goldens/*.raw`, `goldens/goldens.json` | `render_metal_golden.swift` on the M3 Max | one image per colour attachment (41), tight rows, Metal byte order (XR: Metal's XR bytes) |
| `probes/probes.metal`, `probes.air`, `probes.metallib` | `probes/make-probes.sh` (this Mac's Metal toolchain; both outputs sha-pinned in the scenes module) | the six single-feature A16 probes (`render --probes`) |
| `shadow-hpass-m3max.rgba` | `a16gpu_render_scenes.py --derive-shadow-input` | the vertical shadow passes' input: the horizontal pass's M3 Max output (pinned in the scenes module) |
| `llvmpipe-vs-m3max.json` | `llvmpipe_predict.py` | host-side prediction: the same scenes on llvmpipe (Stage 6 `nirexec`) vs the goldens (26 of 33 predictable, all within tolerance) |
| `sheet.py` | -- | a contact sheet of a `render` run: A16 readback, golden, difference |

## The scenes (one source of truth)

`tools/a16gpu_render_scenes.py` holds every input: geometry, uniforms (colour-management curves and matrices,
shadow densities, EDR headroom, Lanczos step, blue-noise, ripple, debug fill), textures (window, photo, EDR photo
in RGBA16F, shadow masks, LUTs as 1D arrays, a 1D wave, noise), API samplers, the initial contents of every
target, blend state, and every function constant the function declares (0 unless named). `scenes()` is the
gate's 28; `xr_scenes()` the five XR lowering scenes `render --xr` adds. `--list` prints them with the reason each
is realistic; `--write-scenes DIR` writes the exact bytes the A16 tool uploads (all 33).

## Regenerate (host only, M3 Max)

```shell
T6=<Stage 6 tree: adreno-stage6/fix4 with adreno-render-fix/air2ir3-abi6.patch applied, built (src/build.sh all)
    and swept (tools/sweep.py --out results/fixed --cache cache): BINDING-ABI v6>
# 1. goldens: the scenes rendered by Metal with the OS metallibs (sha256-checked), frame 2 == frame 0
python3 tools/a16gpu_render_scenes.py --write-scenes /tmp/rscenes
swiftc -O tools/a16gpu-render/render_metal_golden.swift -o /tmp/rgold
/tmp/rgold /tmp/rscenes tools/a16gpu-render/goldens
#    (only if the horizontal shadow scene changed: re-derive the vertical passes' input, then repeat step 1)
python3 tools/a16gpu_render_scenes.py --derive-shadow-input tools/a16gpu-render/goldens
# 2. variants: cache lookup + online compile (byte-identical), trim, by-name linkage, export
AIR2IR3_TREE=$T6 OUT=/tmp/rshaders tools/a16gpu-render/make-render-shaders.sh
cp /tmp/rshaders/render-shaders.json tools/a16gpu-render/
# 3. host prediction (optional evidence)
AIR2IR3_TREE=$T6 python3 tools/a16gpu-render/llvmpipe_predict.py /tmp/rlp
cp /tmp/rlp/llvmpipe-vs-m3max.json tools/a16gpu-render/
# 4. pins: SHADERS_SHA256 / GOLDENS_SHA256 in tools/a16gpu_render.py
shasum -a 256 tools/a16gpu-render/render-shaders.json tools/a16gpu-render/goldens/goldens.json
python3 tools/test-a16gpu-render.py
```

`make_render_shaders.py`, per stage: the declared function constants (air2ir3 `--json` on the unspecialised
function), `--key-only`, then an online compile through the same entry point; a cache hit (Stage 6
`cache/entries/<kk>/<key>.json`, used only if the cache manifest's format/ABI/chip/compiler_id equal the
compiler's) must be byte-identical to the online compile, sidecar and blob, or the script stops. Then
`--trim-constlen VS,FS` (BINDING-ABI 5.2; a named stage is recompiled as its safe_constlen variant), the FS->VS
varying map by name from the two sidecars (BINDING-ABI 4.1), and `export_render_shaders`, which reloads every
blob with Mesa's `ir3_retrieve_variant` and runs Mesa's `ir3_link_shaders` for that pairing. For XR attachments
the Metal blend state is key material (`--blend`), for XR textures the draw-time class (`--tex-class`). All 56
stages of the 28 hit the ABI v6 cache (run 1's fix2 variants hit fix2's); two XR fragment keys and the 12 probe
stages are not in the Stage 6 sweep and compile online.

## BINDING-ABI v6 (fix round 4 + the A16 run 1 UBO fix)

The variants are compiled by fix round 4 with `air2ir3-abi6.patch` (every UBO index below 16; A16 run 1 read
zeros from the driver table at UBO 31). `make_render_shaders.py` reads the declared constants with
`air2ir3 --list-fcs` (ABI v4+ refuses an unspecialised compile whose unset constants decide the body) and uses
the cache only when its manifest says format 4 / ABI 6 / this compiler. The probes are compiled from
`probes/probes.air` by entry point; `probe_ubo_hi` with `--test-ubo-slots 32` (the only variant with
`ubo_slots` 32; `render` accepts it for that probe only). The 35 gate/XR goldens re-rendered for v6 are
byte-identical to run 1's; only the probes are new. `llvmpipe_predict.py` skips the probes (their goldens are
checked against analytic values by `test-a16gpu-render.py`).
