#!/bin/sh
# SPDX-License-Identifier: MIT
# Build a pinned upstream driver that always attempts EL2 at ExitBootServices.
set -eu
cd "$(dirname "$0")/.."
root=$PWD
revision=c090a8cdafa25e4c99df90f8d6f73f3805d9b397
dep="$root/build/deps/slbounce"
if [ ! -d "$dep/.git" ]; then
    git clone https://github.com/TravMurav/slbounce.git "$dep"
    git -C "$dep" checkout --detach "$revision"
fi
if [ "$(git -C "$dep" rev-parse HEAD)" != "$revision" ]; then
    echo "Expected slbounce $revision. Check out that revision in $dep." >&2
    exit 1
fi
git -C "$dep" submodule update --init --recursive
cross=${CROSS_COMPILE:-aarch64-elf-}
make_cmd=${MAKE:-make}
command -v gmake >/dev/null 2>&1 && make_cmd=gmake
# Upstream normally links /usr/include/elf.h, which macOS does not provide.
# Override only that build input; retain upstream sources unchanged.
cp tools/uefi-elf.h "$dep/external/gnu-efi/inc/elf.h"
out="$dep/out-q1n1-always-debug"
"$make_cmd" -C "$dep" -j4 CROSS_COMPILE="$cross" OUT_DIR="$out" \
    SLBOUNCE_ALWAYS_SWITCH=1 DEBUG=1 \
    -o "$dep/external/gnu-efi/inc/elf.h"
mkdir -p build/uefi
cp "$out/slbounce.efi" build/uefi/slbounce-always.efi
cp "$out/sltest.efi" build/uefi/sltest.efi
echo "Built slbounce $revision, SLBOUNCE_ALWAYS_SWITCH=1 DEBUG=1"
