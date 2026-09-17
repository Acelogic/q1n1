# q1n1 at EL2 with the m1n1 proxy, and one-command boots into it

**Status: working on the physical A16 (UX3607OA, BIOS312) on 2026-09-17.**
`q1n1.efi` itself now owns the USB controller after ExitBootServices and serves
m1n1's proxy protocol, and a single Mac command puts the laptop into q1n1 from
Windows, from the boot window, or from q1n1 itself — Windows is no longer on the
path between two q1n1 sessions.

Earlier stages: [first EL2 boot](A16-FIRST-EL2-BOOT.md), [UEFI USB serial](A16-USB-DOCK.md),
[post-ExitBootServices USB serial](A16-USB-EL2-SERIAL.md) (the separate prep and
takeover test apps this payload replaces).

## What runs

`q1n1.efi` keeps its old modes and adds two:

| Command | Behaviour |
| --- | --- |
| `q1n1.efi` | Preflight: entry EL, GOP, SPCR; returns to the shell. |
| `q1n1.efi --el2` | Framebuffer foothold after EBS (`--uart`, `--fault-test`). Unchanged. |
| `q1n1.efi --boot` | A16 boot window, then the EL2 proxy over USB. |
| `q1n1.efi --uart-proxy` | The same proxy over an SPCR PL011. QEMU validation path. |

`--boot` runs in three stages inside one image:

1. **Boot window (boot services).** The firmware's USB Function driver brings
   USB0 up in device mode through the physically proven serial-v3 path and
   enumerates as `1209:316d` serial `A16-Q1N1-BOOT`. The Mac, or the laptop
   keyboard, chooses `proxy`, `proxy once`, `windows` or `shell`. The default on
   timeout, on any refusal, and when no host answers is **Windows**.
2. **Handover.** For `proxy` it arms `BootNext` (see below), snapshots the DWC3
   and qscratch registers while firmware still owns them, stops the firmware
   driver, snapshots again, validates the pair, allocates a 1 MiB DMA arena
   below 4 GiB and a 64 MiB `EfiLoaderCode` heap, then calls ExitBootServices.
3. **Proxy (EL2, no firmware).** `qdwc3_takeover()` restores the captured
   configuration into q1n1's own DWC3 driver, which re-enumerates as serial
   `A16-Q1N1-EL2` with two CDC ACM ports: port 0 is the proxy, port 1 a console.
   The framebuffer shows a live status panel.

## Getting into q1n1: the macvdmtool analogue

There is no hardware side channel on this machine. macvdmtool works because a
Mac's Type-C port controller accepts Apple vendor-defined messages; here the
Dell WD22TB4 terminates USB-PD and the Qualcomm PD stack is not Apple's, so
every transition rides a software channel:

```sh
python3 tools/a16ctl.py status        # where is the A16 right now
python3 tools/a16ctl.py boot q1n1     # from anywhere: end up in the EL2 proxy
python3 tools/a16ctl.py boot windows  # back to Windows
python3 tools/a16ctl.py console       # stream the q1n1 EL2 console port
```

| A16 is in | `boot q1n1` does |
| --- | --- |
| EL2 proxy | `P_REBOOT` over USB, then answers the next boot window with `proxy` |
| boot window | answers it with `proxy` |
| Windows | ssh runs `boot-a16-q1n1.ps1` (arms BootNext, restarts), then answers the window |
| off or hung | waits for a boot window; press the power button |

**The return path.** `BootNext` is a non-volatile UEFI variable that the firmware
consumes on the next boot. When the boot window is told `proxy`, it verifies
that `BootCurrent` is the Windows-created *q1n1 UEFI Shell* entry
(`{cef50169-…}`, pointing at `\EFI\q1n1\shellaa64.efi`) and writes that number
to `BootNext`. Any reset from the q1n1 session therefore lands back in the boot
window, which re-arms it — a loop that never passes through Windows. Because
the window's default is Windows, an unattended laptop still ends up in Windows
about half a minute after a reset, which keeps ssh as the recovery channel.

Runtime `SetVariable` is unsupported on Qualcomm UEFI (Linux needs the
`qseecom` uefisecapp path for EFI variables), so arming happens in boot services
inside the window, never after EBS.

The ESP carries `\startup.nsh`, which the q1n1 shell entry runs:

```text
@echo -off
if not exist fs2:\EFI\q1n1\q1n1-usb.efi then
  fs2:\EFI\Microsoft\Boot\bootmgfw.efi
endif
fs2:\EFI\q1n1\q1n1-usb.efi --boot
if %lasterror% == 0 then
  fs2:\EFI\Microsoft\Boot\bootmgfw.efi
endif
```

Only an explicit `shell` choice returns nonzero, so every other outcome —
including every refusal — boots Windows. `tools/disable-a16-q1n1-boot.ps1`
removes the script again (it is hash-checked first), which the older one-time
probe scripts need, since they refuse to run while a `\startup.nsh` exists.

## The proxy

`platform/uefi/q1n1-proxy.c` implements m1n1's uartproxy wire format: the same
`REQ_NOP/REQ_PROXY/REQ_MEMREAD/REQ_MEMWRITE/REQ_BOOT` framing, 64-byte requests,
36-byte replies, the same checksum, the same opcode numbers. Upstream's
`proxyclient/m1n1/proxy.py` `UartInterface`/`M1N1Proxy` can drive it; the Apple
specific parts of `m1n1.setup` cannot, so this tree ships its own client,
`tools/q1n1proxy.py`, which needs no third-party modules.

Implemented: `P_NOP`, `P_EXIT`, `P_CALL`, `P_GET_BOOTARGS`, `P_GET_BASE`,
`P_UDELAY`, `P_SET_EXC_GUARD`, `P_GET_EXC_COUNT`, `P_REBOOT`, the 8/16/32/64-bit
`READ`/`WRITE`/`SET`/`CLEAR`/`MASK`/`WRITEREAD` family, `MEMCPY`/`MEMSET`, the
cache-maintenance ops, and bulk `MEMREAD`/`MEMWRITE`. Everything Apple-specific
(SMP, DART, PMGR, kboot, hypervisor) returns `S_BADCMD`.

```sh
python3 tools/q1n1proxy.py info                    # bootinfo and counters
python3 tools/q1n1proxy.py peek 0x0a60c120         # live MMIO
python3 tools/q1n1proxy.py dump 0x0a600000 0x200   # hexdump (--live for counters/MMIO)
python3 tools/q1n1proxy.py load build/payload.bin  # upload into the heap
python3 tools/q1n1proxy.py call 0xb9e15000         # run it at EL2
python3 tools/q1n1proxy.py acpi
python3 tools/q1n1proxy.py shell                   # python REPL with p bound
```

`P_GET_BOOTARGS` returns a `struct q1n1_bootinfo` (39 `u64` fields, defined in
`platform/uefi/main.c` and parsed by the client): image base, entry and current
EL, system table, runtime services, ACPI RSDP, SMBIOS3, framebuffer geometry,
the UEFI memory map, the heap and DMA arenas, the DWC3 bases, the register
snapshot, both counter blocks and the BootNext state.

**Exception guards.** `entry.S` now saves a full register frame and can return
from an exception. `P_SET_EXC_GUARD` selects m1n1's semantics: `SKIP` advances
past the faulting instruction, `MARK` also writes `0xacce5515abad1dea` into the
destination register, `RETURN` returns from the current leaf helper. Memory
reads and writes are always guarded, and transfer data passes through a bounce
buffer so a bad address can never fault inside the USB driver or desynchronise
the byte stream. That is what makes poking undocumented Qualcomm MMIO safe.

An unguarded exception is reported to the host as an m1n1 `REQ_BOOT` start
message with reason `START_EXCEPTION` and a pointer to the saved registers; the
target then serves a nested proxy session, so the host can inspect the fault and
resume it (the client skips the instruction by default). Without a host, the
screen shows ESR/ELR/FAR and the machine resets after 30 s — back into the boot
window, because BootNext is still armed.

## Validation

Offline, all passing:

```sh
make -f platform/uefi/Makefile                       # q1n1.efi, warnings as errors
python3 tools/test-q1n1-proxy.py                     # 31 protocol checks, ASan/UBSan
python3 tools/test-uefi.py --el 2 --proxy            # 17 end-to-end checks in QEMU
python3 tools/test-uefi.py --el 2 --boot-window      # refuses unknown firmware, boots Windows
python3 tools/test-uefi.py --el 2                    # EL2 foothold, unchanged
python3 tools/test-uefi.py --el 1                    # EL1 path, unchanged
python3 tools/test-uefi.py --el 2 --preflight        # preflight, unchanged
python3 tools/test-uefi.py --el 2 --fault            # exception screen, unchanged
python3 tools/test-uefi.py --el 2 --usb-ebs          # earlier apps still refuse correctly
python3 tools/test-uefi.py --el 2 --usb-ebs-fixture
python3 tools/test-uefi.py --el 2 --usb-serial-v3
python3 tools/test-a16-usb-host-discovery.py         # 18 Mac discovery fixtures
```

The QEMU `--proxy` run is the important one: it boots the real AArch64 build at
EL2, exits boot services, and drives the live payload over a PL011 with the real
client — bootinfo, a 128 KiB round trip, `GUARD_MARK` on an unmapped address,
a refused `MEMREAD`, uploaded code, an unguarded exception reported and resumed,
an ACPI walk, and `P_REBOOT` ending the machine.

The serial-v3, EBS prep, EBS takeover, fixture, `qcom-dwc3`, `fbcon` and old
`entry.obj` objects were disassembled before and after the shared-source changes
and are **byte-identical**, so the previously proven binaries are unaffected.

## Physical run, 2026-09-17

Installed at 13:04:55Z: `\EFI\q1n1\q1n1-usb.efi` plus `\startup.nsh`, with the
known-good `\EFI\q1n1\q1n1.efi`, the Windows boot manager and the Windows-first
boot order all verified unchanged before and after.

Mac-side USB capture (`build/a16-install/q1n1-boot-first-mac-capture`):

| Time (UTC) | Event |
| --- | --- |
| 13:05:58 | `A16-Q1N1-BOOT` enumerates; the Mac answers `proxy`, reply `OK proxy return=armed` |
| 13:06:02 | firmware USB stops (ExitBootServices) |
| 13:06:03 | `A16-Q1N1-EL2` enumerates — q1n1's own DWC3 driver, 1.2 s later |
| 13:06:49 | `P_REBOOT`; the device disappears |
| 13:07:08 | boot window again, **without passing through Windows** |
| 13:07:13 | EL2 proxy back: a **24.8 s** q1n1-to-q1n1 cycle |
| 13:07:33 | `boot windows`: reset, window answered `windows` |
| 13:08:00 | Windows booted (`LastBootUpTime`), ssh reachable at 13:08:25 |
| 13:10:16 | `boot q1n1` from Windows: proxy up again |

Checks against the live machine at EL2:

- `bootinfo`: `current_el 0x2`, `entry_el 0x2`, framebuffer 2880x1800 at
  `0xf9a20000`, timer 19.2 MHz, 198 memory descriptors, heap `0xb9e05000`,
  DMA arena `0xbde05000`, `return_armed 1`.
- MMIO: `GSNPSID 0x33313130`, `GCTL 0x00102001` (device mode),
  `HS_PHY_CTRL 0x10100000`.
- Guarded read of `1<<48` returned the marker and was counted; the following
  `MEMREAD` of the same address was refused with `ST_XFRERR`; the proxy stayed up.
- 1 MiB write 2.5 MB/s, 1 MiB read 9.3 MB/s, verified byte for byte.
- Uploaded code (`movz x0,#0x1234; ret`) ran at EL2 and returned `0x1234`.
- 16 ACPI tables enumerated by walking RSDP → XSDT from the host.
- Driver counters after the session: 414 events, 54 setups, 1 reset, 1 connect,
  0 stalls, 0 dropped bytes, 0 DMA mismatches, 0 overflows, 0 command failures,
  `init_step 8`.

Record: `build/a16-install/q1n1-proxy-first-run.json`.

## Limits

- The takeover still depends on the firmware bringing USB0 up in the same boot:
  q1n1 does not own the PHY, clocks, GDSC or Type-C policy. High speed only.
- Everything is polled; there are no interrupts, no SMP and no guest yet.
- `P_CALL` runs on the proxy's own stack at EL2 with the firmware's page tables.
  Uploaded code must be flushed (`flush_code`) before it is called.
- A `MEMREAD` of memory that changes between the checksum pass and the data pass
  (counters, MMIO) reports a checksum error; use `--live` or word reads.
- A hang with no exception has no remote recovery: hold the power button. The
  next boot returns to the boot window, which falls back to Windows.
- The `shell` choice has not been exercised on hardware yet.
- `\startup.nsh` and the one-time probe scripts are mutually exclusive.

## Hashes

| Artifact | SHA-256 |
| --- | --- |
| `\EFI\q1n1\q1n1-usb.efi` (installed payload) | `db36cd2af9f016c2c65f6d2e456905e35f7d430e9e0e626978a6431777afcc02` |
| `\startup.nsh` (boot loop) | `8e970563069a61857215c7c8c91f57ea513300c677de35f8706a0d679260726c` |
| `tools/q1n1proxy.py` | `1fe9811457faa3af1f30eefbbc485a24edb96a264773ff5f7cd7b71aea8d2e31` |
| `tools/a16ctl.py` | `1bc33eadb9a94ae3403c2067617695faa5654d4e12f5f7ab9b8a81d5b97fc493` |
| `tools/install-a16-q1n1-boot.ps1` | `5bf6f8913e0ef05e47cb312920083bc6d31994dc0a14ef4e7263d18b909b22e9` |
| `tools/boot-a16-q1n1.ps1` | `a5a6f289885910c7ebecf9547dccb29f686911f235cf4ac818c88d6beb0599d1` |
| `tools/disable-a16-q1n1-boot.ps1` | `f053614c27dcf1c2dda2096f35aa7a212ace7fe531f4c9a52d487535251f3279` |
| `build/a16-install/q1n1-proxy-first-run.json` | `668a589ad634261aae4a768353c7ded002b09bcfbe0911ad9b77dfe24f3c4068` |
| `build/a16-install/q1n1-boot-first-mac-capture.txt` | `8d9120e249d629927404b0cc641aeeb85f1c9cd4a129182c1322590ee946aae8` |
| `…/q1n1-boot-first-mac-capture/events.jsonl` | `ab195beab88877098dd28dcfff0b065030aa8e462f4bbc21b8fd4d2ceff5976f` |
| known-good EL2 foothold payload (unchanged) | `379558299150a121220ee40b7e8095238aad3474c48a695e0b121bf93c41dd32` |

`lld-link` embeds a timestamp, so rebuilding `q1n1.efi` does not reproduce the
payload hash; the installed copy is the reference, and object disassembly is how
the unchanged older binaries were verified.

## UCSI data-role investigation, 2026-09-17 13:45–13:55Z

`q1n1.efi --boot` gained a `hold` command and a UCSI console (`ucsi init`,
`ucsi cmd <control>`), so the firmware's PmicGlinkDxe transport can be driven
one command per line from `tools/a16ucsi.py` while boot services are alive and
a USB link to the Mac is up. That turned a rebuild-install-reboot cycle per
experiment into a line of text, and it resolved the question left open in
[A16-UCSI-REGISTERS.md](A16-UCSI-REGISTERS.md): why `SET_UOR` fails.

Both C-to-C cables were attached: the dock on the connector that maps to USB0
and a direct Mac cable on the connector that maps to USB1 (seen from EL2 as
`PORTSC 0x020206e1`, device connected, port never enabled — the A16 had taken
the host role again).

| Command | Result |
| --- | --- |
| `SET_NOTIFICATION_ENABLE 0x1` | complete — **required first**, or CCI never leaves 0 and every command times out |
| `GET_CAPABILITY` | complete, 2 connectors |
| `GET_CONNECTOR_CAPABILITY` (both) | `0x000077e7` |
| `GET_CONNECTOR_STATUS` (both) | complete, 19 bytes, stable across repeats |
| `GET_CABLE_PROPERTY` | **not supported** (CCI bit 25) |
| `GET_PDOS` | complete with zero-length data |
| `SET_CCOM` → DRP (both) | **not supported** (CCI bit 25) |
| `SET_UOR` → UFP+accept (both) | `CCI 0xc0000000` (ERROR), `GET_ERROR_STATUS` flags `0x0000`, status unchanged |

`0x77e7` decodes as Rp-capable, Rd-capable, DRP, USB2, USB3, alternate mode,
provider, consumer, **swap-to-DFP = 1 (bit 10)** and **swap-to-UFP = 0 (bit 11)**.

**Conclusion: the rejection is firmware policy, not a missing handshake.** The
PPM advertises that it will not swap to UFP, rejects `SET_UOR` accordingly with
no error flag to explain it, and does not implement `SET_CCOM`, so there is no
UCSI route to make this laptop a USB device on a direct cable. The port hardware
is perfectly capable of it — that is exactly what the dock path uses — but the
PD firmware on the DSP only offers "become a host".

Remaining routes, none of them UCSI:

1. Present a host to the A16 (dock, any USB-C hub, or a USB-A-to-C cable with a
   C-to-A adapter on the Mac). The A16 then becomes UFP on the CC lines with no
   PD negotiation to refuse. This is what works today.
2. Drive the PMIC's Type-C block directly over SPMI from EL2, bypassing the DSP's
   PD firmware. The arbiter answers at EL2 (`0x0c400000` → `0x80000002`,
   `0x0c500000` → `0x08000090`), so the path is open, but it means fighting the
   firmware that also owns that block.
3. Accept the host role and write an xHCI host driver in q1n1, enumerating the
   Mac's device mode (it already exposes NCM networking at SuperSpeed Plus when
   attached). This follows the hardware's own policy instead of fighting it.

Evidence: `build/a16-install/a16-ucsi-role-findings.json`.

## EL2 timer tick over GICv3, 2026-09-17 14:55Z

The proxy loop owns the CPU today, which will not be true once a guest runs.
m1n1 solves that with a 1 kHz timer interrupt at EL2 (`src/hv.c` `hv_tick`,
`HV_TICK_RATE 1000`) that services the link from the FIQ path; Apple runs with
VHE, so `CNTP_*` at EL2 is already the EL2 timer there. This machine reports
`HCR_EL2 = 0x88000010`, so **E2H = 0** and the EL2 timer is `CNTHP_*_EL2`,
whose PPI is **INTID 26**. `platform/uefi/gicv3.c` does the equivalent setup:

- MADT walk for the distributor (type `0x0C`) and this CPU's redistributor,
  matched by `GICR_TYPER` affinity against `MPIDR_EL1`, from either the GICC
  entries or a type `0x0E` discovery range. The stride comes from
  `GICR_TYPER.VLPIS` (GICv4 `0x40000`, GICv3 `0x20000`) instead of assuming one.
- Wake the redistributor if asleep, `GICD_CTLR |= ARE_NS | EnableGrp1A | EnableGrp1`,
  put INTID 26 in Group 1 NS, set its priority below `ICC_PMR_EL1`, clear any
  pending state and enable it.
- `ICC_SRE_EL2.SRE`, `ICC_PMR_EL1 = 0xf0`, `ICC_BPR1_EL1 = 0`, `ICC_IGRPEN1_EL1 = 1`.
- `CNTHP_TVAL_EL2` / `CNTHP_CTL_EL2` for the interval, then `daifclr #2`.

The IRQ vector in `entry.S` already produced a full register frame, so the
handler is a few lines in `q1n1_exception()`: acknowledge with `ICC_IAR1_EL1`,
re-arm the timer before the EOI (the PPI is level-triggered), poll the USB
driver unless the polled loop already holds it, then `ICC_EOIR1_EL1`.

Observed on the A16, first hardware run:

| Measurement | Result |
| --- | --- |
| GICD / GICR (from the MADT) | `0x17000000` / `0x17080000` (affinity 0 matched) |
| PPI, interval | INTID 26, 19200 counter ticks (19.2 MHz / 1000) |
| Tick rate | **1002 Hz** over 2 s, and 1000.3 Hz over 3 s |
| Spurious / unexpected INTIDs | 0 / 0 |
| Idle behaviour | 26 serviced, 1977 skipped — the polled loop normally owns the driver |
| **Proxy loop blocked 3.0 s in `P_CALL`** | **3001 ticks, 3000 of them serviced the USB driver, 1 skipped** |
| USB counters during that block | rx 1320 → 1960, tx 1752 → 2324 bytes |
| Link afterwards | healthy; `GSNPSID` still `0x33313130` |

That last row is the point: while the payload's own loop was not running at all,
the link stayed alive on interrupts. QEMU covers the same path
(`tools/test-uefi.py --el 2 --proxy` now checks 21 things, including 667 ticks
in a TCG second, INTID 26 and no spurious interrupts).

Evidence: `build/a16-install/q1n1-el2-tick-first-run.json`.

What this does **not** do yet: route interrupts from a lower EL (`HCR_EL2.IMO`
is already set, but there is no guest), virtualise the GIC for a guest, or set
up stage 2. It is the servicing mechanism those will need.

## The Mac also cannot force it, 2026-09-17 15:25Z

UCSI closed the A16-initiated path. The Mac-initiated path is now closed too.
`tools/mac-typec-role.cpp` (HPM plumbing from macvdmtool, Apache-2.0, unmodified
there) reads and commands this Mac's ACE port controllers. With the bare cable
live, `scan` shows:

| Mac port | State |
| --- | --- |
| controller 1 | sink + DFP — the dock: it powers the Mac and the Mac hosts it |
| controller 2 | sink + **UFP** — the bare cable: **the A16 powers the Mac and hosts it** |

The A16's `PORTSC 0x020206e1` (`ccs=1`) and the Mac's one `Powered` XDCI agree.
Register `0x1A` bit 6 is the data role (dock `0xcd` → DFP, A16 port `0x1d` → UFP).

`SWDF` on controller 2 — the Mac's port controller requesting DFP — returns **3**,
TI's "task rejected by port partner", both before and after unlocking the ACE
(`LOCK -> 0`). Nothing changed on either side.

So the A16 refuses the swap whether it asks itself over UCSI or the Mac asks it
over PD. This is a deliberate policy in its PD firmware, consistent with
`GET_CONNECTOR_CAPABILITY` bit 11 = 0.

An A/B register dump of the two ports (`dump 1` vs `dump 2`) leaves one
interesting candidate: `0x29` (Port Configuration on the TPS6598x map the ACE
derives from) is identical except byte 8, `0xa1` on the dock port versus `0xb1`
on the A16 port — a single bit. Forcing the Mac's port to present Rp only would
make the A16 take Rd with no negotiation to refuse, exactly as a hub or an
A-to-C cable does. That write is guesswork against an undocumented Apple
register on a port that is currently carrying power, so it is not attempted here.

Standing conclusion: a bare C-to-C cable cannot make this laptop a USB device.
Use a hub, a dock or a USB-A-to-C cable, or make q1n1 the host (an xHCI driver
enumerating the Mac, which already appears as an NCM device at SuperSpeed Plus).

## Chainloading, and an m1n1-style console, 2026-09-17 15:40–16:10Z

**Chainloading.** `P_VECTOR` hands the machine to a flat stage image uploaded
over the proxy. The payload reserves a 16 MiB region before ExitBootServices
(`0xb0000000` on the A16, `0x4c000000` in QEMU, from a candidate list), and
`tools/q1n1proxy.py chainload` uploads there and jumps. The stage inherits
`bootinfo` — DWC3 snapshot, DMA arena, heap, memory map, framebuffer, ACPI —
re-takes the controller, re-initialises its own GIC tick and increments
`stage_generation`, which is how the host proves the new code is answering.

A stage cannot overwrite the slot it runs from, so images are built in pairs
(`q1n1-stage.bin` at `0xb0000000`, `q1n1-stage-alt.bin` at `0xb0800000`) and the
client picks the free one by reading each image's header: magic `Q1N1STG1` and
link address at offset 8, plus a config word at 24 the host patches (`--logo`).

Measured on hardware: **291–614 KB uploaded in 0.14–0.30 s, a complete cycle in
about 6 s**, repeated ten times in one session. The chainloaded stage is fully
functional: own exception vectors (guard returns the marker), own 1002 Hz tick,
10.0 MB/s reads, live MMIO, 16 ACPI tables.

Getting it wrong is cheap: a stage that overwrote its own slot wedged the
machine, and holding the power button returned it to the boot window, because
BootNext was still armed. That bug — the stage reporting the region base instead
of its real link address — is now caught by the client and by a QEMU test that
chainloads twice and checks the slots differ.

**The console.** `platform/uefi/console.c` renders with m1n1's own font
(`font/font_retina.bin`, Source Code Pro Bold 16x32, one coverage byte per
pixel, chars 0x20..0x7e) and places a centred logo the way `fb_blit_logo` does.
`platform/uefi/sysinfo.c` prints the parallel of m1n1's boot log from tables
firmware already left behind, costing no drivers. The A16 reports:

- 18 of 18 CPU interfaces enabled; MIDR `0x512f0021` (Qualcomm part 0x002 r2p1)
- SVE present, 44-bit PA, 64-byte cache lines, GIC system registers v3
- 48 GiB mapped, 47 GiB free, largest block 29 GiB at `0x8c000000`, 317 descriptors
- three IOMMUs: SMMUv1/v2 at `0x15000000` and `0x03da0000`, SMMUv3 at `0x15480000`
- eight PCIe ECAM segments from `0x400000000` to `0x740000000`
- GTDT: no platform timers, so no SBSA watchdog to report; `el2 timer gsiv 26`
  independently confirms the PPI the tick uses
- Insyde firmware, UEFI 2.7, BIOS `UX3607OA.312` (07/12/2026), ACPI OEM `_ASUS_/Notebook`

The text column is narrowed beside the centred logo (`max cols 80` of a possible
178), which is why m1n1 reports 64 columns on a 2560-wide panel rather than 160.
The status bar is gone; the exception level is stated in words.

Three defects found and fixed while building this, all in new code: the EFI
system table's vendor pointer read from the header CRC offset (crashed QEMU),
addresses truncated to 8 hex digits (hid the real ECAM bases), and the live
panel scrolling the console on screens too short to hold it.

Screenshot, read out of the A16's framebuffer over the proxy rather than
photographed: `build/a16-install/q1n1-console-m1n1-style.png`.

## xHCI host on USB1: the Mac enumerates, 2026-09-17 16:30–17:05Z

UCSI closed the A16-as-device path and the Mac's ACE closed the Mac-initiated
path, leaving route 3 from that list: accept the host role and write an xHCI
driver. That now works, and the bare C-to-C cable carries a real USB link.

**The driver runs on the host, not the target.** `tools/a16xhci.py` drives every
register over the proxy, the way m1n1's proxyclient drives Apple hardware. An
experiment costs a millisecond instead of a rebuild-and-chainload cycle, which
is why the whole bring-up fit in one session. Porting it into a stage is a
later step, once the sequence is settled. `init` and `stop` refuse to run
against USB0, because that controller carries the console the tool talks over.

### The controller was waiting to be taken

| | USB0 (dock) | USB1 (Mac cable) |
| --- | --- | --- |
| `GCTL.PRTCAPDIR` | 2 — device | **1 — host** |
| `GSTS.CURMOD` | 0 — device | 1 — host |
| `USBSTS` | `0x1` halted | `0x1` halted |
| Windows driver | `USBXHCI` child | **none** |

Both are DWC_usb31 cores (`GSNPSID 0x33313130`), xHCI 1.2, 64 slots, 2 ports,
64-byte contexts, 2 scratchpad buffers. Port 1 is USB 2.0 and port 2 is USB 3.1
— the two halves of one Type-C connector. Firmware had already put USB1 in host
mode and then left it halted with nothing bound, so q1n1 could take it outright.

### Bring-up

DMA structures live in a 1 MiB window at the top of the 64 MiB proxy heap
(`0xbdcf0000`), which no target code uses: DCBAA, command ring, event ring,
ERST and the scratchpad array. DMA on this SoC is cache-coherent — the DWC3
gadget services its rings with a bare `dmb` and no cache maintenance — so the
proxy's ordinary writes are visible to the controller.

The checkpoint that mattered was a **No Op Command returning Success**. An SMMU
silently eating the DMA was the plausible way for all of this to fail quietly;
that one command proves the command ring, the event ring and the controller's
view of our memory all agree. `CRCR` then reads back `0x8` (CRR set).

A port reset took port 1 from `0x000206e1` (`ccs=1 ped=0 pls=Polling`) to
`0x00000e03`: **enabled, link state U0, High speed**. A real
`PortStatusChange` event arrived in our event ring, from hardware.

### The Mac enumerates

This was the genuine unknown — whether Apple's device mode cooperates with a
non-Apple host. It does, completely:

| | |
| --- | --- |
| VID:PID | `05ac:1905`, "Apple Inc." / "Mac" |
| USB / speed | 2.1, High (480 Mbps), MPS0 64, address 1, slot 1 |
| Configuration | 1 config, 4 interfaces, 149 bytes, 384 mA |

Two independent CDC-NCM 1.0 Ethernet functions, each with its own MAC:

| Function | Control | Data | MAC | Endpoints |
| --- | --- | --- | --- | --- |
| A | interface 0 | interface 1 alt 1 | `12:77:60:EB:84:A7` | 1 IN / 1 OUT bulk, 512 |
| B | interface 2 | interface 3 alt 1 | `12:77:60:EB:84:87` | 2 IN / 2 OUT bulk, 512 |

Both report `wMaxSegmentSize` 16014 and network capabilities `0xbb`. String
descriptors read cleanly, including the serial and both MAC addresses.

**Only the USB2 port sees the Mac.** Port 2 stays in `RxDetect` with no far-end
termination, so the Type-C SuperSpeed mux is evidently not routed to this
controller while the PD firmware believes nothing needs it. High speed is ample
for a proxy link, so this is recorded rather than chased.

### What is not done

`SET_CONFIGURATION`, selecting alternate setting 1 on a data interface,
configuring the bulk endpoints, NCM framing, and the proxy itself over that
link. Because NCM carries Ethernet frames, that transport needs no IP stack in
q1n1 — a raw framed protocol with a BPF socket on the Mac side is enough.

Evidence: `build/a16-install/q1n1-xhci-enumeration.json`,
`build/a16-install/q1n1-xhci-first-enumeration.txt`.

| File | sha256 |
| --- | --- |
| `tools/a16xhci.py` | `fd148a09536e8c00…` |
| `…/q1n1-xhci-first-enumeration.txt` | `f4f34e1baff70bdc…` |

## NCM over the bare cable: q1n1 and the Mac exchange IPv6, 2026-09-17 17:10–17:50Z

The xHCI host driver now carries real network traffic. `tools/a16xhci.py link`
brings USB1 up, configures the Mac's CDC-NCM function, learns the Mac's
addresses from its own traffic, and pings it — over the bare C-to-C cable, with
the dock out of the picture entirely.

### Configuring the function

| Step | Result |
| --- | --- |
| `SET_CONFIGURATION(1)` | accepted |
| Configure Endpoint (DCI 2 bulk OUT, DCI 3 bulk IN) | Success |
| `SET_INTERFACE(1, alt 1)` | accepted |
| `GET_NTB_PARAMETERS` | NTB16+NTB32, in/out max 32764, divisor 4, align 4, 512 datagrams |
| `SET_ETHERNET_PACKET_FILTER(0x0f)` | accepted |

The xHCI Configure Endpoint command has to come **before** `SET_INTERFACE`, the
order Linux's `xhci-hcd` uses, because the endpoints only exist in alternate
setting 1 and the controller needs their contexts first.

### Receiving

The first listen took 31 NTBs off the bulk IN endpoint in eight seconds: mDNS,
MLDv2 reports, a router solicitation, and a DHCP DISCOVER. On the Mac these come
from **`en5` (`12:77:60:eb:84:58`), UP and RUNNING** — macOS brings its
device-mode interface up as soon as a host configures the function.

### Transmitting

NTB16 blocks are built host-side — NTH16, then 4-byte-aligned datagrams, then
the NDP16 with its terminating entry — and pushed to bulk OUT, with the
zero-length packet a size-aligned block needs.

The Mac's link-local turned out to be an RFC 7217 stable-privacy address,
`fe80::107d:f14d:6e8d:bebe`, **not** EUI-64 from its MAC, so it is learned from
its own traffic rather than computed. With an unsolicited Neighbor Advertisement
to seed its cache, and an answer to the Neighbor Solicitation it sends back:

```
  echo request #0 sent
    answered a Neighbor Solicitation
    ECHO REPLY #0 from fe80::107d:f14d:6e8d:bebe, 71 bytes
  echo request #1 sent
    ECHO REPLY #1 from fe80::107d:f14d:6e8d:bebe, 71 bytes
```

### One session per replug

**This Mac tolerates exactly one host session per physical replug.** Resetting
the host controller under a configured device leaves its XDCI enabling at Full
speed instead of High — it stops chirping and stops answering control transfers
(`Address Device` → `USBTransaction`), and no amount of port reset, port disable
or settle time recovers it. Unwinding the configuration first (`SET_INTERFACE`
alt 0, `SET_CONFIGURATION(0)`, Disable Slot) does not prevent it either.

That is why `link` does everything in one process, and it is the argument for
moving the driver into a chainloaded stage: there the controller is initialised
once and stays up across host tool invocations, so the failure mode disappears.

### A link fix on the way through

Under xHCI polling the console link corrupted one inbound payload. The target
reported **zero** checksum errors over 2284 requests, so the corruption was
inbound-only with the stream still in sync, which makes reissuing the request
safe. `q1n1proxy.readmem` now retries up to three times and counts it; the link
session above recorded one retry.

Evidence: `build/a16-install/q1n1-xhci-ncm-link.json`,
`build/a16-install/q1n1-xhci-link-session.txt`.

| File | sha256 |
| --- | --- |
| `tools/a16xhci.py` | `bb8a52d1bfab5504…` |
| `tools/q1n1proxy.py` | `ea281ad168c1f0fd…` |

## Phase A1: the driver moves into q1n1, 2026-09-17 17:50–19:10Z

The host-side Python proved the sequence; this puts it in the target. q1n1 now
owns USB1 itself, in C, in a chainloaded stage — no host driver involved.

| File | |
| --- | --- |
| `platform/uefi/xhci.{h,c}` | polled xHCI: init, rings, commands, ports, slots, control and bulk transfers |
| `platform/uefi/ncm.{h,c}` | CDC-NCM: descriptor walk, endpoint configuration, NTB16 both ways |
| `tools/test-q1n1-xhci.{c,py}` | simulated controller and device, 39 checks under ASan/UBSan |

Bootinfo grew from 45 to 48 fields (`xhci_base`, `xhci_stats`, `ncm_stats`),
matched in the struct, the client and the native harness.

### What it does on hardware

`q1n1_xhci_retry` is callable with `P_CALL`, so a bring-up costs one proxy call
rather than a chainload — which matters, because each attempt against a wedged
far end costs a physical replug. With the cable freshly replugged it returns 0,
`PORTSC` reads `0x00000e03` (enabled, U0, **High** speed), and `ncm_open`
reaches step 9.

Calling `ncm_receive` and `ncm_send` on the target through `P_CALL`:

```
  12:77:60:eb:84:58 -> 33:33:00:00:00:16  IPv6, 110 bytes    (MLDv2)
  12:77:60:eb:84:58 -> 33:33:ff:8d:be:be  IPv6, 86 bytes     (neighbour solicitation)
  12:77:60:eb:84:58 -> ff:ff:ff:ff:ff:ff  IPv4, 342 bytes    (DHCP DISCOVER)
  ...
  echo request #0 sent (71 bytes)
    answered a Neighbor Solicitation
    ECHO REPLY #0, 71 bytes
  echo request #1 sent (71 bytes)
    ECHO REPLY #1, 71 bytes
```

138 frames in and 6 out, 61 KB received, **zero transfer failures, zero command
failures, zero stalls**. The MAC the device assigns the host
(`12:77:60:eb:84:a7`) is parsed from the Ethernet functional descriptor's string.

### Two design points

**A deferred event cache.** Waiting on one completion must not discard the
others: a bulk transfer can finish while a command is outstanding. Unmatched
events go to an eight-entry cache the next waiter checks before polling. The
Python driver had this bug — it logged and dropped them.

**`xhci_post` and `xhci_reap` are split**, so one bulk IN stays parked across
poll cycles instead of queueing a fresh TRB each time. That is what will let the
EL2 tick service this link in A3.

### An open counter, and a spec gap it turned up

The first hardware run recorded `malformed 86` against 138 good frames. It then
stayed static across 25 further receives while frames kept arriving, with no
transfer failures, and a block dumped from the driver's own buffer parsed clean
(Apple places the NDP *before* the datagram; the walk handles that). So it was a
burst during heavy traffic, not a standing fault.

Rather than guess, the counter is now split into `bad_signature`, `bad_ndp` and
`bad_entry`, so the next burst says which check fired. Looking for the cause
also turned up a real omission: the driver never sent **`SET_NTB_INPUT_SIZE`**,
so the device was entitled to use its own `dwNtbInMaxSize` of 32764 — twice the
buffer this driver posts. That request is now sent, and the harness checks it.

### What is not done

The link is driven by `P_CALL` from the host, not yet by the stage itself, and
the proxy does not yet run over it (A2). Servicing it from the EL2 tick is A3.

Evidence: `build/a16-install/q1n1-xhci-c-driver.json`.

| File | sha256 |
| --- | --- |
| `platform/uefi/xhci.c` | `79423625520ff0ca…` |
| `platform/uefi/ncm.c` | `19124b74f6761d68…` |
| `tools/test-q1n1-xhci.c` | `4ae08400a8b443c5…` |

## Phase A2: the proxy over the bare cable, 2026-09-17 19:20–20:40Z

The m1n1 proxy now runs over the NCM link. `tools/a16ncm.py talk` drives q1n1
across a bare USB-C cable with the dock out of the data path entirely, and
without root.

### UDP, not a private ethertype

The first design carried the proxy stream in raw Ethernet frames with ethertype
`0x88B5`. The proxy protocol has its own framing, checksums and resynchronisation,
so a datagram pipe is all it needs and that kept q1n1 free of an IP stack.

It also needed BPF on the host, which needs root every session. That is a
transport nobody uses. Speaking UDP costs the target about a hundred lines --
answer neighbour solicitations, checksum a pseudo-header -- and makes the host
side an ordinary unprivileged socket. `platform/uefi/ncm-proxy.c` does that.

The target's address is the EUI-64 link-local of the MAC the NCM Ethernet
descriptor assigns the host (`fe80::1077:60ff:feeb:84a7`), so the host computes
it rather than being told.

### Recoverable by construction

`struct q1n1_io` gained an optional `abort` hook. The proxy loop leaves when it
returns non-zero, and this transport returns non-zero once the link has been
idle for a configured time. So `q1n1_ncm_proxy_run`, called with `P_CALL` over
the console, hands the proxy to the Ethernet link and the console gets it back
by itself when the experiment ends or fails. That was exercised repeatedly and
is why none of this needed the power button.

### What it costs

| | CDC console (dock) | UDP over NCM (bare cable) |
| --- | --- | --- |
| read 4 MiB | **11.12 MB/s** | 8.71 MB/s |
| write 1 MiB | 2.65 MB/s | **5.08 MB/s** |
| round trip | **0.209 ms** (4773/s) | 0.459 ms (2177/s) |

**The new link is not faster.** It is better at writes, worse at reads and
latency. Its value is that it does not need the dock.

Sustained writes need 4 KiB chunks: the target keeps a single outstanding NCM
receive transfer, so while it is parsing one block the device has nowhere to put
the next. A single 32 KiB write survives; back-to-back ones do not. Giving the
driver a queue of receive TRBs is what would lift that, and reads -- which
stream out of the target rather than into it -- are already clean to 4 MiB
unchunked at 8.7 MB/s.

### The bug, and two wrong turns

`send_advertisement` built its reply in the same buffer the solicitation arrived
in, while still holding pointers into that buffer for the asker's MAC and
address. Writing the IPv6 header overwrote the asker's address with our own, so
**every neighbour advertisement was addressed to ourselves** and macOS dropped
all of them. The hardware counters said exactly that -- `solicitations 18`,
`advertisements 19`, `frames_in 0` -- but the cause was found offline, in
`tools/test-q1n1-ncmproxy.c`, not over the cable.

Two things that looked like findings and were not:

- A 1 MiB read seemed to lose datagrams, and a chunking workaround was written
  for it. It was not loss: the range contained `ncm_link` and `xhci_host`, whose
  counters the transport itself mutates, and `readmem` checksums a range before
  sending it. Live memory needs `live=True`. Reads are clean to 4 MiB.
- Three of the four initial harness failures were the harness asserting the
  wrong property. A checksum recomputed over a packet that already carries it
  folds to zero; that is what a receiver checks, and what the harness now checks.

Evidence: `build/a16-install/q1n1-proxy-over-udp.json`.

## Both links at once, 2026-09-17 21:00–21:30Z

The stage now brings the NCM link up in its own startup path and serves the
proxy on **both** links simultaneously.

The first design served one and then the other, with the transport's watchdog
handing control back. That works but stalls whichever host is not holding the
loop, by however long the idle timeout is. `dual_io` in `platform/uefi/main.c`
polls both transports every pass and replies on whichever one a request arrived
from. A request is read whole before its reply goes out, so the active link
cannot change mid-request.

```
console: retry -> 0, ncm step 9
console: generation 19 answering
udp:     generation 19 at fe80::1077:60ff:feeb:84a7
  pass 0: console 0x14000008  udp 0x14000008  agree
12 interleaved write/read rounds on both links in 0.5s
  mismatches: 0     console retries 0, udp retries 0
```

48 interleaved bulk operations, no corruption, no retries. `q1n1_xhci_retry`
rebuilds `ncm_link` from scratch, so it now rebuilds the transport around it and
re-publishes it to the multiplexer.

**What this does and does not buy.** The dock is now optional *while q1n1 runs*,
and it never has to be disconnected for that to be true. It is not optional for
getting q1n1 running: the boot window needs a USB host on USB0, and a bare cable
cannot be one -- that is the PD firmware policy closed from both ends earlier.
The ESP payload also predates all of this, so a cold boot has no xHCI until a
stage is chainloaded; making the link available from boot needs that payload
updated, which is one Windows trip.

One host-side wrinkle: the first UDP `sendto` after a replug can fail with
`ENOHOST` while macOS is still resolving the neighbour. The client retries.

Evidence: `build/a16-install/q1n1-dual-transport.json`.

## The link survives chainloading, 2026-09-17 21:45–22:15Z

Every replug this session came from one cause: a chainloaded stage reset USB1
out from under a configured device, and `HCRST` under a configured device is
what leaves this Mac's XDCI at Full speed refusing to chirp.

The cause was structural. `struct xhci`, `struct ncm` and the transport lived in
each stage's `.bss`, so a new stage had nowhere to inherit them from and had no
choice but to rebuild everything from a reset controller.

They now live in a `struct q1n1_usb_state` at the base of the DMA arena, in the
proxy heap, which persists across chainloads; the rings and buffers are carved
after it. A starting stage checks a magic word, that the controller is out of
reset with `init_step 4`, that NCM reached `init_step 9` with a slot addressed,
and that `PORTSC` still reports the port enabled. If all of that holds it adopts
the link untouched and only renews the transport's function pointers -- those
point into the previous stage's text, which the chainload is about to overwrite.

Adoption is tried **before** the `STAGE_XHCI` flag, so a stage inherits a live
link whether or not it was asked to bring one up.

```
stage answering: generation 21 ... new stage: xhci_base 0xa800000, port High
udp answers the NEW stage: generation 21
stage answering: generation 22, 23, 24
after 3 more chainloads: generation 24, port High
udp still serving generation 24; 16 KiB round trip MATCH
replugs needed across 4 chainloads: 0
```

One transition replug was needed to move the state out of `.bss` and into the
arena. From generation 21 on, none.

Evidence: `build/a16-install/q1n1-link-survives-chainload.json`.

## Booting without the dock, 2026-09-17 22:30–23:15Z

Everything above still needed the dock to *start* q1n1: the boot window runs on
USB0's CDC console, and it times out to Windows, so with no dock nobody ever
types `proxy`.

`q1n1.efi --boot --auto` changes that. When the window times out and **no USB0
host ever enumerated**, it takes a new choice, `Q1N1_BOOT_PROXY_NCM`:

- `usb_ebs_saved`, the GSNPSID check and `qdwc3_takeover` are all skipped. The
  device-mode controller is left exactly as firmware had it, so none of the
  existing safety checks had to be relaxed -- there is simply nothing to take
  over. A null transport fills the multiplexer's first slot.
- The payload itself (not only a stage) brings USB1 up after ExitBootServices,
  with a 3 s port wait so a dock-only boot loses nothing.
- It **re-arms BootNext**. That variable is one-shot, so re-arming each time is
  what makes the machine keep returning to q1n1 on the bare cable alone.

Ways back to Windows, in order of convenience: plug the dock in during the boot
window and type `windows`; pick Windows from the firmware boot menu at power-on;
run `tools/disable-a16-q1n1-boot.ps1`. And a crash *before* the window arms
BootNext falls through to Windows by itself, because `startup.nsh` chains to
`bootmgfw.efi` whenever the payload returns 0.

**QEMU caught the one real bug.** The first version had the payload probe USB1
whenever `stage_generation` was 0, which faults under `--uart-proxy` where no
controller exists at `0x0a800000`. The automatic bring-up is now restricted to
`MODE_USB_PROXY`.

This payload is built and tested but **not installed**: putting it on the ESP
needs a Windows trip, and until then a cold boot still runs the old one.

Evidence: `build/a16-install/q1n1-dock-free-boot.json`.

## Installing it, and the crash that followed, 2026-09-17 23:20–00:40Z

The dock-free payload went onto the ESP, the first dock-free boot crashed, and
it boot-looped. All three of those are worth recording.

### The crash

`q1n1: unhandled exception`, roughly a millisecond after the GIC tick started.

The 1 kHz tick called `qdwc3_poll(&usb)` whenever `mode == MODE_USB_PROXY`. The
dock-free path deliberately never takes the gadget over -- there is no host to
be a device for -- so the poll dereferenced a base that was never set. The tick
now guards on the driver (`usb.stats.init_step >= 2`) instead of the mode.

`reset_now()` already used exactly that guard for exactly that reason. That it
was there and the tick did not use it is the tell: the mode was never a safe
proxy for "the gadget exists", and adding a path where the two diverged was
enough to break it.

### The loop

The fault handler resets after 30 s. But the boot window had already re-armed
`BootNext`, so the reset came straight back to the same fault -- the risk that
the "re-arm each time" boot policy carries by construction.

The escape worked first try: plug the dock in, the boot window appears, `windows`.
The handler now **halts** instead of resetting when there is no console, so the
screen stays readable as the only diagnostic and the one-shot `BootNext` is
spent -- a power cycle returns to Windows on its own.

### Hot-plug, which the cold boot made necessary

The payload came up and initialised the controller by itself, and found nothing:
the far end had not re-presented within the 3 s port wait after this machine
power-cycled the port. Requiring the cable at boot was simply the wrong design.

The poll loop now checks every 250 ms and brings the link up whenever a cable
appears. Measured, with the dock attached and the bare cable plugged in live:

```
16:25:10  port1 ccs=0                      ncm step 1
16:25:19  port1 ccs=1 ped=0 speed=Full     ncm step 1
16:25:19  port1 ccs=1 ped=1 speed=High     ncm step 9   6 frames
```

Plug to enumerated link inside one second.

### Either connector

`xhci_candidate()` takes the first **host-mode** controller and skips whichever
one carries the CDC console. Firmware sets `GCTL.PRTCAPDIR` from what PD
negotiated, so "host mode" is exactly "a bare cable here, not a dock". The cable
now works in USB0 or USB1.

### The tick services the NCM link too

Without it the only link on a dock-free boot would stall whenever the proxy loop
was busy -- the same reason the gadget is serviced from the tick. It rides a
different controller, so it takes its own re-entrancy guard.

### State

The ESP carries the payload; 33 stale probe binaries and logs were removed
(584 KB) and `esp-install.json` pruned of their entries, with originals backed
up on the A16. Verified with the dock attached: the ESP payload boots, takes
USB1, hot-plugs the cable, and serves the proxy on the console and over UDP at
the same time. The dock-free boot itself is still unverified, because the crash
fix landed after that attempt.

Evidence: `build/a16-install/q1n1-dock-free-install.json`.

## The link was not fragile; the build was

Three separate failures this session traced to one cause, and none of them were
where they appeared to be.

### The host addressed a machine that was not there

`tools/udplink.py` derived the target's link-local address from the *local*
interface's MAC, on the assumption that it equals the MAC in the NCM function's
Ethernet descriptor. It does not. This Mac advertises `12:77:60:eb:84:a7` in the
descriptor and gives its own interfaces `...:84:57/58/59`. They matched once, by
luck, which is why the assumption survived.

When they diverge the failure is silent and total: the neighbour solicitation
goes unanswered, the entry stays `(incomplete)`, and a perfectly healthy target
is indistinguishable from a dead one. Two rounds of "q1n1 has halted" and "the
link is half-open" were diagnosed off this, and both were wrong.

q1n1 now answers on a second, fixed address as well -- `fe80::4919`, the port
number -- and replies are sourced from whichever of its two addresses the peer
used (`struct ncm_proxy.local`). The host hardcodes the fixed one and never
guesses a MAC it cannot see.

### The Makefile did not track headers

`$(OUT)/%.obj: platform/uefi/%.c` -- the header was not a prerequisite, so
changing a `.h` rebuilt nothing. These objects share structs that live in shared
memory across a chainload, so a partial rebuild leaves translation units
disagreeing about where fields are. The result is not a link error, it is
plausible nonsense: `xhci.stats.transfers` reading back as `0xbdd101d0`, a
counter block that turned out to be TRB pointers, a stage that cannot parse the
state block its predecessor wrote.

This is what the earlier "XHCI_DEFERRED 8->32 changed sizeof" crash really was,
and what "reused stale struct offsets" was both previous times. `-MMD -MP` plus
`-include` fixes it; `make clean` now removes the stage objects and depfiles
too. Touching `xhci.h` correctly rebuilds `main.c` now, which it never did.

After a clean rebuild the same link that had failed a 629 KB upload three times
moved 4.19 MB in each direction with zero drops.

### Stop computing target offsets on the host

Locating `ncm_proxy.stats` by adding up `sizeof()` on the host gave a plausible
wrong answer three times in one session -- the last one off by exactly 16 bytes,
which read as neighbouring fields rather than failing. The target publishes the
pointer now (`bootinfo.ncm_proxy_stats`), as it already did for the xHCI and NCM
counters, and `tools/test-q1n1-proxy.c` sizes its bootinfo array from the real
struct instead of a hand-kept `48`.

### Measured, generation 4, dock plus bare cable

| transfer | write | read |
| --- | --- | --- |
| 256 KiB | 4.55 MB/s | 8.48 MB/s |
| 1 MiB | 4.65 MB/s | 8.44 MB/s |
| 4 MiB | 3.69 MB/s | 8.51 MB/s |

Zero `dropped`, zero `bad_checksum`, every round trip matched. A full chainload
over the bare cable -- 629,672 bytes, upload, verify and jump -- takes 0.22 s.

### What is still capped

Host-to-target writes still need `max_write = 4 KiB`. At 16 KiB and above they
time out, and the proxy's own `dropped` counter stays at zero, so the loss is
upstream: the Mac's NCM queue backs up while q1n1 has no IN TRB posted.
`xhci_post` keeps exactly one outstanding TRB per endpoint (`d->pending[dci]`)
and `ncm_receive` only posts the next one after the previous block is fully
consumed. Giving the IN endpoint a queue of buffers is the change that would
lift writes; reads already run at 8.5 MB/s because they do not depend on it.
