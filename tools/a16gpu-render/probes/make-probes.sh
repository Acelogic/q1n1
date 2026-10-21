#!/bin/zsh
# SPDX-License-Identifier: MIT
# Rebuild probes.air and probes.metallib from probes.metal with this Mac's Metal toolchain (host only), then
# re-pin PROBE_AIR_SHA256 / LIB_SHA256['Probe'] in tools/a16gpu_render_scenes.py (regenerate on purpose only).
set -e
HERE=${0:a:h}
# compiled from stdin so the AIR records no local source path
xcrun -sdk macosx metal -c -x metal - -o $HERE/probes.air < $HERE/probes.metal
xcrun -sdk macosx metallib $HERE/probes.air -o $HERE/probes.metallib
shasum -a 256 $HERE/probes.air $HERE/probes.metallib
