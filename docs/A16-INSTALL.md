# Installing and running q1n1 on the ASUS Zenbook A16

q1n1 is a fork of [m1n1](https://github.com/AsahiLinux/m1n1) retargeted at a
Snapdragon laptop. Where m1n1 boots on Apple Silicon from iBoot, q1n1 is a UEFI
application: firmware loads it, it takes the machine at EL2 after
`ExitBootServices`, and then serves the m1n1 proxy protocol so a host machine can
drive it interactively.

Everything below is what was actually done on one machine. Read
[the porting record](A16-Q1N1-PROXY.md) for why each piece is the way it is.

## What this was built and tested on

| | |
| --- | --- |
| Target | ASUS Zenbook A16, UX3607OA, Snapdragon X2E-96-100, BIOS 312 |
| Target OS | Windows on ARM (stays installed; q1n1 boots beside it) |
| Host | macOS on Apple Silicon |
| Link | USB-C, either a Dell WD22TB4 dock or a bare C-to-C cable |

**Nothing here is portable to another machine as-is.** The UEFI boot entry GUID,
the shell's `fs2:` mapping for the ESP, the DWC3 base addresses and the firmware
version check are all specific to this laptop. The sections below mark the
values you would have to re-derive.

## Host prerequisites

```sh
brew install llvm lld qemu          # clang targeting aarch64, lld, qemu-system-aarch64
```

The build looks for `/opt/homebrew/opt/llvm/bin/clang` and falls back to
whatever `clang` is on `PATH`; override with `make LLVM=/path/to/llvm/bin`. The
Python tools need only the standard library — no `pip install`, and
deliberately no `pyserial`.

## Build

```sh
make uefi                 # build/uefi/q1n1.efi plus the chainloadable stages
```

Artifacts:

| file | what it is |
| --- | --- |
| `build/uefi/q1n1.efi` | the UEFI application; this is what goes on the ESP |
| `build/uefi/q1n1-stage.bin` | flat stage linked for `0xb0000000` |
| `build/uefi/q1n1-stage-alt.bin` | same, linked for `0xb0800000` |
| `build/uefi/q1n1-stage-qemu*.bin` | the same two for QEMU's memory map |

Two stage slots exist so a chainload never overwrites the image it is running
from; the tooling picks whichever slot is free.

## Test before touching hardware

```sh
python3 tools/test-uefi.py --proxy       # boots the real EFI binary in QEMU at EL2
python3 tools/test-q1n1-proxy.py         # proxy protocol, native, ASan + UBSan
python3 tools/test-q1n1-xhci.py          # xHCI and NCM against a simulated controller
python3 tools/test-q1n1-ncmproxy.py      # IPv6/UDP framing, packet by packet
```

The QEMU test boots the actual `q1n1.efi`, reaches EL2 after `ExitBootServices`,
and exercises the proxy including two chainload generations. The three native
suites run the real driver sources against simulators under sanitizers. All four
pass on a clean tree; if they do not, do not put anything on the laptop.

`--firmware` defaults to `/opt/homebrew/share/qemu/edk2-aarch64-code.fd`.

## Install on the A16

The payload lives on the existing EFI System Partition next to the Windows boot
manager. Windows stays first in the firmware boot order — q1n1 is only reached
when something arms `BootNext`, and the startup script always falls back to
Windows. That fallback is the safety net: a failed payload costs a power cycle,
not an install.

Copy `build/uefi/q1n1.efi` (as `q1n1-usb.efi`) and
`tools/install-a16-q1n1-boot.ps1` to the A16, then from an **elevated**
PowerShell in that directory:

```powershell
$hash = (Get-FileHash .\q1n1-usb.efi -Algorithm SHA256).Hash.ToLower()
.\install-a16-q1n1-boot.ps1 -PayloadSHA256 $hash
```

The script refuses to run unless the hash you pass matches the file, which is
what stops a half-copied payload from being installed. It writes:

- `\EFI\q1n1\q1n1-usb.efi` — the payload
- `\startup.nsh` — runs the payload with `--boot --auto`, and chains to
  `\EFI\Microsoft\Boot\bootmgfw.efi` if the payload is missing or returns 0

**Machine-specific values inside that script**: the UEFI Shell boot entry GUID
(`{cef50169-…}`) and the `fs2:` shell mapping for the NVMe ESP. On another
machine, find the shell entry with `bcdedit /enum {fwbootmgr}` and the ESP
mapping with `map` at the shell prompt.

## Boot into q1n1

`BootNext` is one-shot, and firmware here does not support runtime
`SetVariable`, so it has to be armed from Windows or from q1n1's own boot
window:

```sh
export A16_SSH_TARGET=user@a16-address     # only needed for the Windows-side reboot
python3 tools/a16ctl.py boot q1n1          # arms BootNext and reboots; ~25 s
python3 tools/a16ctl.py boot windows       # go back
python3 tools/a16ctl.py status             # where is it now
```

`a16ctl.py` needs SSH to the A16 only to arm the reboot from Windows. Once q1n1
is running it is not involved.

On boot the payload shows a boot window for 30 s on the USB CDC console
(`A16-Q1N1-BOOT`). `a16ctl.py claim q1n1` answers it without a reboot. With
`--auto` and no USB host attached it picks the proxy itself on timeout, which is
what makes a dock-free boot work.

## Connect

Two independent transports, and they work at the same time:

**USB0 CDC console** — appears as `/dev/cu.usbmodem*A16_Q1N1_EL2*`. Needs the
dock, because that is what makes this machine a USB device.

```sh
python3 tools/q1n1proxy.py shell            # interactive
python3 tools/q1n1proxy.py acpi             # one-shot commands
```

**USB1 over a bare C-to-C cable** — q1n1 drives its own xHCI controller,
enumerates the Mac as a CDC-NCM device, and speaks UDP over IPv6 to it. No dock
and no root.

```sh
python3 tools/udplink.py en5                # check the link; en5 is the NCM interface
```

q1n1 answers on a fixed address, `fe80::4919`, port 4919. Do not derive the
address from the local interface's MAC — that is not the MAC the NCM descriptor
carries, and the two are not required to match.

A bare cable cannot make the A16 a USB *device* (PD firmware policy), which is
why this direction is the host driving the Mac rather than the reverse.

## The edit-test loop

A chainload replaces the running EL2 code without rebooting:

```sh
python3 tools/q1n1proxy.py chainload --xhci build/uefi/q1n1-stage.bin
```

629 KB uploads, verifies and jumps in about 0.2 s over either transport. The new
stage inherits bootinfo, bumps `stage_generation` so you can prove the new code
is the one answering, and adopts a running NCM link in place rather than
resetting the controller — resetting is what makes the far end stop enumerating
until the cable is physically replugged.

`--xhci` asks the stage to bring up USB1 and open the NCM link itself.

## When it goes wrong

| symptom | what it means |
| --- | --- |
| Screen shows `q1n1: unhandled exception … halted` | faulted with no console to report to. Power-cycle; that spends the one-shot `BootNext` and returns to Windows. |
| Boot loops | the boot window re-armed `BootNext` before faulting. Plug the dock in, catch the boot window, send `windows`. |
| `PORTSC` reads `0x603` (Full) instead of `0xe03` (High) | the far end wedged after a controller reset. Physically replug the cable. |
| Neighbour entry stays `(incomplete)` | nothing is answering at that address — check you are using `fe80::4919`. |
| Target values read as plausible nonsense | stale objects. `rm -rf build/uefi && make uefi` before debugging further. |

Recovery never needs more than the power button: the firmware boot order still
has Windows first, and `BootNext` only ever survives one boot.

## Layout

```
platform/uefi/     the UEFI application and the EL2 payload
  main.c           entry, boot window, proxy loop, transport multiplexer
  xhci.c/.h        polled xHCI host driver
  ncm.c/.h         CDC-NCM function driver
  ncm-proxy.c/.h   the proxy stream as UDP over IPv6 link-local
  q1n1-proxy.c     the m1n1 proxy request loop
  qcom-dwc3.c      the USB0 device-side CDC console
tools/             host-side clients and tests
  q1n1proxy.py     the proxy client (m1n1 wire format, no pyserial)
  a16ctl.py        reboot the A16 into q1n1 or Windows
  udplink.py       the UDP-over-NCM transport
  a16xhci.py       drives the xHCI controller from the host, for bring-up
docs/              the record of how each piece was established
```

## Licence and attribution

MIT, inherited from m1n1. This is a fork of the
[Asahi Linux](https://asahilinux.org/) project's m1n1 — the proxy protocol, the
host client structure and much of the surrounding tree are theirs. New files
under `platform/uefi/` and the A16 tools carry `SPDX-License-Identifier: MIT`.
