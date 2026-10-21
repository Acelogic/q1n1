#!/bin/sh
# SPDX-License-Identifier: MIT
# Stage files only. Does not format a drive, install to an ESP, or alter boot order.
set -eu
cd "$(dirname "$0")/.."
tcb=${1:-build/private/tcblaunch-current.exe}
test -f "$tcb"
make -f platform/uefi/Makefile
test -f build/uefi/sltest.efi
test -f build/uefi/slbounce-always.efi
kit=build/a16-kit
mkdir -p "$kit/EFI/BOOT" "$kit/licenses"
shell=build/private/shellaa64-26H1.efi
shell_hash=1569b6db4e391c3c59194aa3319a3945efb800fb25349eb9d36ff3d258517ea6
if [ ! -f "$shell" ]; then
    curl --fail --location --output "$shell" \
        https://github.com/pbatard/UEFI-Shell/releases/download/26H1/shellaa64.efi
fi
actual=$(shasum -a 256 "$shell" | cut -d ' ' -f 1)
test "$actual" = "$shell_hash" || { echo 'UEFI Shell hash mismatch' >&2; exit 1; }
cp "$shell" "$kit/EFI/BOOT/BOOTAA64.EFI"
cp build/uefi/q1n1.efi build/uefi/sltest.efi build/uefi/slbounce-always.efi "$kit/"
cp "$tcb" "$kit/tcblaunch.exe"
cp docs/A16-BOOT.md "$kit/README.md"
cp LICENSE "$kit/licenses/q1n1-MIT.txt"
cp build/deps/slbounce/LICENSE "$kit/licenses/slbounce-BSD-3-Clause.txt"
# This is a private local testing kit: the Microsoft-signed binary is not
# committed or uploaded. Consult upstream notices before redistribution.
(cd "$kit" && shasum -a 256 *.efi tcblaunch.exe EFI/BOOT/BOOTAA64.EFI > SHA256SUMS)
echo "Prepared $kit. Copy its contents to the root of a FAT32 USB stick."
