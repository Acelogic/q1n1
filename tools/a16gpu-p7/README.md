# P7 assets: the ir3 variants, the Metal golden, the register tables

> `p7-shaders.json` is not part of the public repository: it is compiled from shaders shipped with macOS.
> Generate it on your own Mac as described below; `.gitignore` keeps it out of commits.

`tools/a16gpu_p6p7.py` pins the sha256 of `p7-shaders.json` and
`p7-golden-m3max.bgra`; `triangle` stops at T0 if either differs. Regenerate
only on purpose, then update the pins in the same change.

| File | Made by | Pin |
| --- | --- | --- |
| `p7-shaders.json` | `make-p7-shaders.sh` (+ `export_p7_shaders.cpp`) | 88aab4e2d9be944904ad700f32850f152e331e435a398494bf86390bc4004b31 |
| `p7-golden-m3max.bgra` | `p7_metal_golden.swift` on the M3 Max | cb9c7424cb3ced93d2fdafe32ee1939c809b67f238fb9db2d27df6c3729e6ea0 |
| `../a8xx_regs.py` | `gen_a8xx_regs.py` | (generated source, reviewed as code) |

## The shaders

```shell
AIR2IR3_TREE=<stage6 tree: build/air2ir3, build/mesa, third_party/mesa, corpus/SkyLight> \
OUT=build/a16gpu-p7 tools/a16gpu-p7/make-p7-shaders.sh
cp build/a16gpu-p7/p7-shaders.json tools/a16gpu-p7/
```

air2ir3 compiles SkyLight's `SimpleVertex` and `SimpleTextureFragment`
(colour attachment 0 = MTLPixelFormat 80, BGRA8Unorm) into
`ir3_store_variant` blobs plus BINDING-ABI v2 sidecars; `export_p7_shaders`,
built against the same Mesa, reloads each blob with `ir3_retrieve_variant`
and writes what the tool programs from: `info`, `instrlen`, `constlen`,
`branchstack_hw`, `mergedregs`, inputs/outputs and their regids, sysvals,
`const_state` (UBO ranges pushed to consts, driver UBOs), the sidecar, the
disassembly, the code bytes, and `ir3_link_shaders`'s VS->FS linkage. The
current file came from air2ir3 c1d8fcf04b9eef02, Mesa bbeb2dca9fee (+ Stage 6
patches), LLVM 22.1.8 (`compiler` field).

## The golden

```shell
python3 tools/a16gpu_p6p7.py --write-scene /tmp/p7scene
swiftc -O tools/a16gpu-p7/p7_metal_golden.swift -o /tmp/p7gold
/tmp/p7gold /tmp/p7scene tools/a16gpu-p7/p7-golden-m3max.bgra
```

`--write-scene` writes the exact bytes the A16 tool uploads (vertices, mvp,
texture, sampler, clear); the Swift program renders them with the OS SkyLight
metallib (sha256 pinned in the scene, the Stage 6 corpus's) in three command
buffers and writes frame 2 (it must equal frame 0). 96x64 BGRA8, 24576 bytes.

## The register tables

```shell
MESA_TREE=<stage6 tree>/third_party/mesa python3 tools/a16gpu-p7/gen_a8xx_regs.py   # writes tools/a8xx_regs.py
```

It uses Mesa's own `gen_header.py` parser on `a6xx.xml`, `adreno_pm4.xml`
and the descriptor XML, keeps the A8XX
variant of every listed register, packet field and descriptor, and the enum
values whose `variants` cover A8XX.
