# AGENTS.md

Guidance for coding agents (and people) working in this repository.

## What this is

q1n1 is an independent fork of [m1n1](https://github.com/AsahiLinux/m1n1),
based on upstream commit `b4654b3`. It runs as a UEFI application on the ASUS
A16 (Snapdragon X2 Elite). It takes EL2, brings up the Adreno GPU, NVMe, USB
and Wi-Fi far enough for [Snapintosh](https://github.com/Acelogic/Snapintosh)
to boot macOS, and talks to the Mac host over USB-C NCM.

- Native UEFI side: `platform/uefi/`.
- Host tools (Python; the proxy client is under `proxyclient/`): `tools/`.
  - GPU bring-up: `tools/a16gpu_*.py`, `tools/adreno_x2.py`.
  - Shader and render references: `tools/a16gpu-render/`, `tools/a16gpu-p7/`.
- m1n1's own code: `src/`, `proxyclient/`, `rust/`.

## Rules

1. **This is not Asahi Linux.** Do not send q1n1 changes, issues or pull
   requests to AsahiLinux/m1n1. Its maintainers do not accept AI-assisted
   contributions.
2. **Leave upstream m1n1 files as they are** unless the port needs a change.
   Keep their license headers. New files carry
   `SPDX-License-Identifier: MIT`.
3. **GPL boundary.** `tools/adreno_x2_protect.py` is GPL-2.0-only: its tables
   are translated from Linux's `a6xx_catalog.c`. Keep GPL-derived data in that
   file; `tools/adreno_x2.py` stays MIT and imports it. Other Linux and
   Qualcomm sources are references only: cite them, and re-implement.
   Credits are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
4. **Never commit:**
   - vendor firmware (GPU sqe/gmu/zap, QCC2072/ath12k, DSP), ACPI dumps,
     Windows driver files or captures;
   - shader sets compiled from Apple's shaders (`render-shaders.json`,
     `p7-shaders.json`; regenerate them, see below);
   - personal or machine data (paths, host and user names, IP or MAC
     addresses, serial numbers, boot-entry GUIDs). Pass these as arguments,
     e.g. `-ExpectedComputerName`, `-BootEntry`, `--mac`, or
     `MACVDMTOOL_DIR`.
5. **Hardware safety.**
   - Run one hardware client at a time.
   - Keep the laptop on AC or at 50% battery or more.
   - Respect the GPU clock limits in `tools/adreno_x2.py`; the safe top OPP is
     below the table's maximum.
   - Never issue a single `CP_MEMCPY` larger than 1 MiB.
   - Keep new behaviour behind options, with the default unchanged.

## Rebuilding what is not distributed

| Missing input | Produce it with |
| --- | --- |
| `tools/a16gpu-render/render-shaders.json` | `AIR2IR3_TREE=<Snapintosh>/gpu/air2ir3 OUT=build/a16gpu-render zsh tools/a16gpu-render/make-render-shaders.sh` on a Mac running macOS 26A428. It compiles Apple's own shaders with Snapintosh's air2ir3. |
| `tools/a16gpu-p7/p7-shaders.json` | `AIR2IR3_TREE=... OUT=build/a16gpu-p7 zsh tools/a16gpu-p7/make-p7-shaders.sh` |
| Device firmware (GPU sqe/gmu/zap, QCC2072 board/amss) | Take it from your own laptop's Windows on ARM driver packages. The tools identify each file by name and SHA-256 (grep the tools for the expected hashes). Keep it under ignored `build/private/` and never commit it. |
| Mesa register tables (`tools/a8xx_regs.py`) | `tools/a16gpu-p7/gen_a8xx_regs.py` against a Mesa checkout. Its output carries Mesa's notice. |

The tools check each generated file's SHA-256. If you regenerate a file on
purpose, update the pinned hash in the same change. Tests print SKIP when an
input is absent.

## Tests

- `python3 tools/test-<name>.py`, e.g. `test-a16gpu-stage3.py`,
  `test-a16gpu-x290.py`.
- `pytest` per `pytest.ini`. This needs the `construct` module from
  `requirements.txt`.
- `make uefi` builds the UEFI application. Check the stage binaries it
  produces before any hardware run.
