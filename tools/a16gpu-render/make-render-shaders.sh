#!/bin/zsh
# SPDX-License-Identifier: MIT
# Regenerate tools/a16gpu-render/render-shaders.json from the Stage 6 compiler tree (host only, M3 Max).
#   AIR2IR3_TREE=<stage6 tree with build/air2ir3, build/mesa, third_party/mesa, corpus/, cache/>
#   OUT=<scratch dir> tools/a16gpu-render/make-render-shaders.sh
# 1. builds export_render_shaders against the tree's Mesa (as tools/a16gpu-p7/make-p7-shaders.sh);
# 2. make_render_shaders.py compiles/looks up every pipeline of tools/a16gpu_render_scenes.py and exports it.
# The tool pins the JSON's sha256 (a16gpu_render.SHADERS_SHA256); regenerate on purpose only.
set -e
HERE=${0:a:h}
T=${AIR2IR3_TREE:?set AIR2IR3_TREE to the Stage 6 tree}
export OUT=${OUT:-$HERE/../../build/a16gpu-render}
mkdir -p $OUT
M=$T/third_party/mesa
B=$T/build/mesa
INC=(-I$M/src/freedreno/common -I$B/src/freedreno/ir3 -I$M/src/freedreno/ir3 -I$B/src/freedreno -I$M/src/freedreno
     -I$M/src/freedreno/registers -I$B/src/freedreno/registers -I$B/src/freedreno/registers/adreno
     -I$B/src/freedreno/common -I$M/src/freedreno/common -I$B/include -I$M/include -I$B/src -I$M/src
     -I$B/src/compiler/nir -I$M/src/compiler/nir -I$B/src/compiler -I$M/src/compiler -I$B/src/util -I$M/src/util)
DEFS=(-DHAVE_PTHREAD -DHAVE___BUILTIN_CLZ -DHAVE___BUILTIN_CLZLL -DHAVE___BUILTIN_CTZ -DHAVE___BUILTIN_FFS
      -DHAVE___BUILTIN_FFSLL -DHAVE___BUILTIN_POPCOUNT -DHAVE___BUILTIN_POPCOUNTLL -DHAVE___BUILTIN_UNREACHABLE
      -DHAVE___BUILTIN_EXPECT -DHAVE_FUNC_ATTRIBUTE_PACKED -DHAVE_FUNC_ATTRIBUTE_UNUSED -DHAVE_STRUCT_TIMESPEC -DMESA_DEBUG=0)
LIBS=($B/src/freedreno/ir3/libfreedreno_ir3.a $B/src/freedreno/isa/libir3encode.a $B/src/freedreno/isa/libir3decode.a
      $B/src/compiler/isaspec/libisaspec.a $B/src/freedreno/common/libfreedreno_common.a $B/src/compiler/nir/libnir.a
      $B/src/compiler/libcompiler.a $B/src/util/libmesa_util.a $B/src/util/libmesa_util_simd.a
      $B/src/c11/impl/libmesa_util_c11.a $B/src/util/blake3/libblake3.a)
clang++ -std=c++17 -O1 -g -Wno-deprecated-declarations -Wno-c99-designator $INC $DEFS $HERE/export_render_shaders.cpp \
  $LIBS -L/opt/homebrew/opt/zstd/lib -lzstd -lz -o $OUT/export_render_shaders
python3 $HERE/make_render_shaders.py
python3 -c "import json,sys; json.load(open('$OUT/render-shaders.json'))"
