# q1n1 on ASUS Zenbook A16 (UX3607OA)

This is the first standalone Qualcomm UEFI target in the q1n1 fork. It builds an
ARM64 EFI application, exits boot services (with an optional slbounce driver
only when entry is at EL1), reports the actual exception level, draws a framebuffer console and the
new dragon logo, and optionally transmits over a firmware-configured UART.

**Status: physical A16 execution at EL2 after ExitBootServices is confirmed.**
User photographs show successful UEFI preflight at EL2, then the standalone
q1n1 framebuffer with `EL2 CONFIRMED AFTER EXITBOOTSERVICES`, entry/current EL
both 2, a green status bar, and the dragon logo. The direct command was
`fs2:\EFI\q1n1\q1n1.efi --el2`; no slbounce driver was loaded in the prescribed
sequence. Skip sltest/slbounce for this observed boot path. See the
[first hardware boot record](A16-FIRST-EL2-BOOT.md) for evidence and limits.
The existing Apple m1n1 monitor, Python proxy, guest hypervisor, SMP, interrupts,
MMU ownership, and USB drivers are not ported by this target. Do not boot the
original `m1n1.bin` on this machine: it still expects Apple boot arguments/ADT.

## Verified machine and capture

Checked over the existing SSH connection on 2026-09-16:

- ASUS UX3607OA, BIOS `UX3607OA.312`, Windows `10.0.28000`.
- Secure Boot is disabled.
- Installed `tcblaunch.exe`: `10.0.28000.1764`, 970,152 bytes.
- Its SHA-256 is `96e95ac895f7cae1b2aaec0f9391076525314258e73e8d0e9c873716626f4f6f`.
- WinSxS contains the same version and a reverse-delta file, not an identified
  older complete TCB binary.
- Capture root on the Mac: `/Volumes/Untitled/A16-capture-20260916`.
- The extraction/setup history is in the Codex task
  [Boot ASUS Zenbook A16 menu](codex://threads/01a0aafd-6501-73e3-959f-92d3acbc91b1).

The Windows image and firmware capture were inspected read-only. A separate
shell entry was subsequently installed on the existing ESP as described below.
No firmware flash or partition resize has been performed. A one-time reboot
into the q1n1 shell succeeded, confirmed by user photographs. The physical
preflight and direct post-ExitBootServices payload both ran successfully at EL2.
Secure Launch was not attempted.

## Installed on the existing NVMe ESP

No USB stick or new partition is needed for the prepared configuration.
Disk 0 partition 12 is the existing 450 MiB EFI System Partition, with about
413 MiB free before installation. It now contains:

```text
\EFI\q1n1\shellaa64.efi
\EFI\q1n1\q1n1.efi
\EFI\q1n1\sltest.efi
\EFI\q1n1\slbounce-always.efi
\tcblaunch.exe
```

The firmware entry is **q1n1 UEFI Shell**, identifier
`{cef50169-b1ff-11f1-94fe-c41375be4bea}`. Windows remains first in the firmware
boot order. The Windows boot-manager configuration matches its saved pre-install
snapshot, and its EFI executable hash is unchanged. The temporary `Q:` ESP mount
was removed after installation.

Installation records, BCD backup, staged kit, and removal script are in
`C:\Users\migue\q1n1-bringup-20260916`. The executable copies passed SHA-256
verification on the ESP. To remove just this installation, run the supplied
`remove-a16-esp.ps1` from that directory in an elevated PowerShell; it checks the
recorded hashes before removing the new entry and its five files.

The observed shell maps the NVMe EFI partition (HD12) as **FS2:**. Run preflight:

```text
fs2:\EFI\q1n1\q1n1.efi
```

This A16 already reports `Entry EL: 2`. Continue **directly**, without slbounce
or sltest:

```text
fs2:\EFI\q1n1\q1n1.efi --el2
```

This first q1n1 run uses the screen, without UART MMIO. Leave `--uart` for a
separate test once the UART's physical connection is established. Selecting the
shell is not itself proof of EL2; the q1n1 screen must explicitly confirm EL2
after EBS. The initially installed binary always prints advice to load slbounce
in preflight; that advice is inapplicable when its own `Entry EL` line says 2.
The local source has been corrected, but this wording change has not yet been
installed on the laptop. Its existing `--el2` execution path is unchanged.

If a later boot instead reports EL1, qualify `sltest` first, then use the
slbounce sequence in the conditional EL1 instructions below. Recheck mappings
with `map fs*` after firmware or disk changes; FS2 is an observation, not a
permanent mapping guarantee.

## Build on this Mac

```sh
make -f platform/uefi/Makefile
sh tools/build-slbounce.sh
sh tools/make-a16-kit.sh build/private/tcblaunch-current.exe
```

The q1n1 target needs Clang and `lld-link`; on this Mac Clang is in Homebrew's
LLVM prefix. Slbounce uses the installed `aarch64-elf-gcc`/binutils toolchain.
The slbounce script pins upstream commit
`c090a8cdafa25e4c99df90f8d6f73f3805d9b397` and builds with
`SLBOUNCE_ALWAYS_SWITCH=1 DEBUG=1`. The ordinary upstream driver may decline the
transition without an EL2-compatible Linux device tree; this payload uses ACPI.
The small ELF header shim allows GNU-EFI's AArch64 relocator to build on macOS.
Upstream's linker emits RWX LOAD-segment warnings for its ELF intermediates.

Outputs are in `build/uefi/` and the removable-media layout in `build/a16-kit/`.
The kit uses the ARM64 EDK2 UEFI Shell from
[pbatard/UEFI-Shell 26H1](https://github.com/pbatard/UEFI-Shell/releases/tag/26H1),
checked against the release asset SHA-256 before packaging.
The TCB executable and the whole boot kit remain ignored under `build/`.
Keep this device's Windows/firmware captures and private boot kit out of Git.

## Alternative removable-media setup

Steps 2 and 3 below apply only to a preflight reporting **EL1**. For **EL2**,
skip both and run `q1n1.efi --el2` directly, as on the observed A16 boot.

If a spare FAT32 USB stick is available later, copy the **contents** of `build/a16-kit/` to its
root; the result includes `EFI/BOOT/BOOTAA64.EFI`, `q1n1.efi`, `sltest.efi`,
`slbounce-always.efi`, and `tcblaunch.exe`. The Samsung archive drive is exFAT;
the kit does not reformat it or turn it into a boot device.

Save work on Windows, shut down, and use the A16's boot menu to select the USB
UEFI entry. This deliberately boots an interactive shell, with no automatic
Secure Launch. Run `map -r` and select the `fsN:` containing the kit. The commands
below use `fs0:` as an example; substitute the mapping actually shown.

1. Run `fs0:\q1n1.efi`. This is preflight only. It reports the entry EL and
   whether GOP and a usable SPCR UART were found, then returns to the shell.
   It does not access UART MMIO or invoke `ExitBootServices`.
2. Run `fs0:\sltest.efi fs0:\tcblaunch.exe`. This is upstream's isolated EL2
   compatibility test. A green line at the top of the screen followed by a
   deliberate halt is its success indication. Photograph the result. A blank
   hang or reboot is not success. Power-cycle afterward.
3. Only after successful `sltest`, boot the shell again and run:

   ```text
   fs0:
   load fs0:\slbounce-always.efi
   fs0:\q1n1.efi --el2
   ```

   Keep `tcblaunch.exe` at the filesystem root. The q1n1 framebuffer should show
   a **green bar** and `EL2 CONFIRMED AFTER EXITBOOTSERVICES`. An **amber bar**
   means it returned at EL1. A red bar is a trapped exception; record ESR/ELR/FAR.
   A small blinking block demonstrates the polling loop is alive. Power-cycle
   to return to firmware. No storage driver or reboot service runs afterward.

4. Once the screen-only path succeeds and the physical UART connection is
   established, add `--uart` to try polled TX using the SPCR description.
   This preserves the firmware's UART setup; it does not configure its clocks,
   pin mux, flow control, baud rate, or power domains. Timeout disables TX.
   Inaccessible MMIO can instead trigger the exception screen.

The current TCB version is **unqualified**, not known-good or proven broken.
[Slbounce's README](https://github.com/TravMurav/slbounce#usage) warns that newer
TCB versions can lack its required error path. The maintainer gives no precise
version cutoff and recommends testing the available binary with `sltest` in
[issue 11](https://github.com/TravMurav/slbounce/issues/11). Static HVC instruction
presence alone does not establish transition-back compatibility. If this binary
fails, use an appropriately sourced older Microsoft-signed ARM64 TCB and test it
separately; do not alter the installed Windows system file.

## Serial and USB evidence

The next discovery tools and captured Windows USB role/function driver mapping
are in [A16 USB console investigation](A16-USB-CONSOLE.md). The separate passive
`q1n1-usb-probe.efi` returns to the shell and preserves the working EL2 payload.

The captured SPCR and DBG2 checksums pass. SPCR and DBG2 identify Qualcomm GENI
UART type `0x13` at **`0x00894000`**, namespace `\_SB.UARD`. The DSDT describes it
as `QUP_2_SE_5,DBG`, HID `QCOM0F16`, with a 0x4000-byte memory resource; DBG2
describes a 0x1000-byte debug window. SPCR requests baud code 7 (115200 baud).
The firmware uses raw GAS access-size value 32 instead of the standard encoded
value 3 for a 32-bit access; the reader accepts this captured quirk.

The DSDT and SPCR interrupt descriptions differ. The first target polls and does
not configure an interrupt controller, so it does not assume either description
is the physical EL2 interrupt wiring.

DBG2 also lists vendor-specific USB debug subtype `0x5143` for:

| Namespace | Controller base | DSDT status |
| --- | --- | --- |
| `\_SB.URS0` | `0x0a600000` | Enabled |
| `\_SB.URS1` | `0x0a800000` | Enabled |
| `\_SB.URS2` | `0x0a000000` | Disabled (`_STA` returns zero) |

Those descriptors are not evidence of USB CDC serial enumeration, a usable
host-facing gadget, or UART routing to USB-C. The Mac showed no new serial port
in the live inventory. Its `cu.debug-console` is not proof of an A16 connection.
The previously working SSH connection belongs to Windows networking. It cannot
provide the post-EBS console while this payload is running. The successful
hardware test omitted `--uart`, so `UART TX DISABLED OR UNAVAILABLE` is expected
and is not a failed physical UART test.

USB serial needs controller/PHY/clock/reset/IOMMU and Type-C role handling for
the A16, then a USB device/CDC transport. The original m1n1 Apple DWC3 setup
cannot be assumed to work. UART also needs a verified external pin/adapter path.

Reproduce the evidence extraction with:

```sh
python3 tools/audit-a16-acpi.py /Volumes/Untitled/A16-capture-20260916 \
  --output build/uefi/a16-acpi.json
```

## Validation and next porting work

The EFI target compiles with `-Wall -Wextra -Werror`. QEMU tests execute the built
EFI binary, inspect serial output, and capture the actual GOP framebuffer:

```sh
python3 tools/test-uefi.py --el 2
python3 tools/test-uefi.py --el 1
python3 tools/test-uefi.py --el 2 --preflight
python3 tools/test-uefi.py --el 2 --fault
```

EL2 and EL1 paths show the correct status, the preflight returns without EBS,
and a deliberate BRK reaches the new exception vectors and paints a red fault
screen. QEMU's PL011 TX works after EBS. These tests do **not** exercise slbounce,
Qualcomm firmware, the physical GENI UART, USB serial, or an EL1-to-EL2 transition.
QEMU's firmware starts at the selected EL. Test results and screenshots are in
`build/uefi/qemu-*`.

With direct physical EL2 entry confirmed, the remaining work is to qualify a
console transport and give the port its own memory map/page tables, GIC and timer handling, PSCI/SMP,
and transport, then adapt the reusable proxy/hypervisor pieces without Apple
register assumptions. For now it stays on one CPU with masked interrupts,
firmware mappings, a dedicated stack, vectors, framebuffer, and bounded UART TX.
