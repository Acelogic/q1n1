# A16 USB serial after ExitBootServices (q1n1-owned DWC3)

> Update 2026-09-17: `q1n1.efi --boot` now does all of this itself (boot
> window, snapshot, takeover) and serves the m1n1 proxy at EL2. The two test
> apps below remain the record of how the takeover was first proven. See
> [A16-Q1N1-PROXY.md](A16-Q1N1-PROXY.md).

**Status, 2026-09-17 12:14Z: physical PASS.** After `ExitBootServices`, q1n1's
own polled DWC3 driver served two CDC ACM ports on USB0. The Mac opened
`/dev/cu.usbmodemA16_Q1N1_EL21` and `…EL23`. Every echo was exact, up to
65,536 bytes, at about 1.6 MiB/s round trip. After the 240-second window the
driver stopped the controller and ResetSystem returned the A16 to Windows
without a power-button press.

This builds on the UEFI result in [A16-USB-DOCK.md](A16-USB-DOCK.md) and uses
the same Dell WD22TB4 front-port cabling.

## Why a takeover works on BIOS312

Firmware-side analysis came first, using the captured drivers in
`build/private/a16-usb-drivers/`:

- `UsbConfigDxe` registers its real ExitBootServices handler at RVA `0x4258`
  (the other one, `0x2ff4`, is a bare `ret`). It walks the five controllers at
  RVA `0xdf00`. It halts xHCI (clears USBCMD Run/Stop) for host modes and powers
  down inactive (`0x10000`/`0x20`) controllers. For **device mode (4)** it only
  clears software fields and closes an event. No clock, GDSC, or PHY change.
- `UsbfnDwc3Dxe`'s global handler (RVA `0x2600`) is `ret`. Its per-controller
  handler (RVA `0x3ce4`, registered only after the function driver starts) calls
  the connect routine at `0x41e8` with 0. The stop path (`0x4524`) clears the
  qscratch VBUS override (`+0x10` bits 20 and 28, `+0x30` bit 24), then
  stops the core and frees memory.
- `UsbConfigDxe` holds the core/qscratch tables. USB0 is `0x0a600000`
  (RVA `0xc5d0`) with qscratch `0x0a6f8800` (RVA `0xc5f8`). This matches the
  URS0 `_CRS` 1 MiB window and Linux `dwc3-qcom` (`SDM845_QSCRATCH_BASE_OFFSET`).
- IORT maps `\_SB.URS0` to the Apps SMMU-500 at `0x15000000`, stream `0x1420`.
  Firmware DWC3 DMA already used a plain physical event buffer
  (`GEVNTADRLO = 0xbe2a0000`), and nothing at ExitBootServices reprograms the
  SMMU. Identity DMA was therefore assumed and verified physically (below).

The physical register snapshot confirmed the disassembly. Stopping the firmware
driver changed only DCTL Run/Stop and those three qscratch bits.

| Register | Firmware running | After firmware stop |
| --- | --- | --- |
| GSNPSID / GVERNUM | `0x33313130` / `0x3230302a` (DWC_usb31 2.00a) | unchanged |
| GCTL | `0x00102001` (PRTCAP device) | unchanged |
| GUSB2PHYCFG / GUSB3PIPECTL | `0x00102400` / `0x0b081402` | unchanged |
| GSBUSCFG0 / GSBUSCFG1 | `0x2222000e` / `0x00007e00` | unchanged |
| GUCTL / GUCTL1 / GFLADJ | `0x0d014802` / `0x81601808` / `0x8c80c8a0` | unchanged |
| GEVNTADRLO / GEVNTSIZ | `0xbe2a0000` / `0x80001000` | unchanged |
| DCFG | `0x00480854` (address 10) | unchanged |
| DCTL | `0x90f00000` | `0x10f00000` (Run/Stop cleared) |
| qscratch `+0x10` HS_PHY_CTRL | `0x10100000` | `0x00000000` |
| qscratch `+0x30` SS_PHY_CTRL | `0x01000000` | `0x00000000` |

## Implementation

| File | Role |
| --- | --- |
| `platform/uefi/usb-ebs-prep.c` | Boot services only. Checks the `UsbConfigDxe` core/qscratch tables, runs the proven serial-v3 role change and firmware CDC start, waits (≤60 s) for the Mac to configure, snapshots 36 DWC3 and 26 qscratch registers while running and after stop, stops the firmware driver, stores the snapshot (FNV-checksummed) in the volatile boot-services variable `Q1N1Usb0Takeover`. Returns to the shell, so its output can be redirected safely. |
| `platform/uefi/usb-ebs.c` | Requires `--takeover` and a valid snapshot from the same boot plus a live GSNPSID match, a GOP framebuffer, and a 1 MiB DMA arena below 4 GiB. Calls ExitBootServices, runs the driver for 240 s with an echo loop on both ports and live counters on the framebuffer, stops, then ResetSystem (PSCI `SYSTEM_RESET` fallback). Exceptions show ESR/ELR/FAR, then reset after 60 s. |
| `platform/uefi/qcom-dwc3.c` | Port of m1n1 `src/usb_dwc3.c`: EP0 state machine, the two-port m1n1 CDC descriptors, bulk/interrupt endpoints. Qualcomm changes: identity DMA; qscratch VBUS override; **DCTL device soft reset only** (no GCTL/PHY soft reset), then restore of the firmware's running bus/PHY config; high speed only; per-packet OUT transfers (exact-512 writes complete without a ZLP); ring peek so a failed STARTTRANSFER loses no data; Linux-style STATUS3 after a data stage and SETUP re-arm after an EP0 stall; explicit event decoding (the MS ABI bitfield layout differs). The event buffer is prefilled with `0xaa` so DMA that never lands is counted. |
| `platform/uefi/fbcon.c` | Post-EBS text console (same 5x7 font as `main.c`, which is unchanged). |
| `platform/uefi/usb-serial.c` | Gained a preprocessor-only `Q1N1_CDC_LIBRARY` guard; the v3 object's code is identical. |

Identity is 1209:316D, product `q1n1 A16 EL2 serial`, serial `A16-Q1N1-EL2`.
Port 0 uses interfaces 0/1 (EP 0x81, 0x02/0x82); port 1 uses 2/3 (0x83, 0x04/0x84).

## Validation before hardware

- `tools/test-a16-qcom-dwc3.c`: 25 register-model checks under ASan/UBSan.
  Coverage:
  - Refusing a missing or mismatched core
  - Takeover ordering, and restoring config after the reset clobbers it
  - macOS-style enumeration, including a stalled BOS request that re-arms SETUP
  - Line coding and DTR on both ports
  - OUT per-packet transfers, IN echo with a ZLP, and no loss while an endpoint is busy
  - DMA-miss detection
  - USB reset and re-enumeration, stop, and a controller stuck in halt
- `tools/test-uefi.py --usb-ebs`: on non-A16 firmware, prep refuses at the
  table check and the takeover refuses without a snapshot; both return to the
  shell and firmware is never exited.
- `tools/test-uefi.py --usb-ebs-fixture`: a QEMU-only build (never installed)
  with a fake controller in RAM. It leaves firmware, draws the EL2 status
  screen, takes the soft-reset timeout path, stops, and ResetSystem ends QEMU
  (about 33 s).
- `tools/test-a16-usb-host-discovery.py`: 18 checks, including two-port EL2
  selection by data interface and capture identity.
- Regression: 36 serial-v3 CDC checks and v3's QEMU refusal still pass.

## Physical run, 2026-09-17 (UTC; Mac log clock is EDT)

Runner mode `ebs-takeover`; startup script:

```text
fs2:\EFI\q1n1\q1n1-usb-ebs-prep.efi --device0 > fs2:\EFI\q1n1\usb-ebs-once.log
if %lasterror% == 0 then
  fs2:\EFI\q1n1\q1n1-usb-ebs.efi --takeover
endif
type fs2:\EFI\q1n1\usb-ebs-once.log
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
```

1. 12:13:39Z: restart requested with the one-time shell entry. Windows stayed
   first in the display order.
2. 12:14:09Z: the prep app's firmware CDC device (`A16-Q1N1-UEFI`) enumerated
   at 480 Mbps and bound ACM. Firmware logged configuration 1, 15 setups,
   speed 3. Every prep status was 0; snapshot checksum `0xC859E467D8D7AB19`.
3. 12:14:12.599Z: firmware stop, and macOS reported `hardware connection lost`.
4. 12:14:14.287Z: `enumerated 0x1209/316d/0100 (q1n1 A16 EL2 serial / 10) at 480 Mbps`.
   That is 1.7 s later, including ExitBootServices and the takeover.
   `AppleUSBACMControl` bound INT EP 0x83 and 0x81. The AppleUserECM dext failed
   start on both data interfaces (benign, as in the UEFI run). Both callouts appeared.
5. 12:14:15Z: `tools/test-a16-usb-serial.py --serial A16-Q1N1-EL2` opened
   `…EL21` and echoed 317, 1024, and 1537 bytes exactly.
6. 12:14:30Z: an identity-verified second session echoed on `…EL21` 1, 511,
   512, 513, 2048, 4096, 16384, and 65536 bytes, and on `…EL23` 317, 512, and
   4096 bytes, all exact. Large transfers ran at about 1.6 MiB/s round trip,
   compared with about 76 KiB/s through the firmware USB Function driver.
7. 12:18:14.008Z: after 240 s, q1n1 stopped the controller and dropped VBUS
   override, so macOS saw a clean detach.
8. 12:18:46Z: Windows booted through ResetSystem, with no user action.
   Collection at 12:19:46Z removed only the matching startup script. No
   bootsequence was pending, `{bootmgr}` was first, and Q: was unmounted.

No framebuffer photo was taken of the post-EBS status screen; the Mac-side
kernel log, callouts, and byte-exact echoes are the evidence.

Installed binaries (ESP `\EFI\q1n1\`, appended to `esp-install.json`):

| File | SHA-256 |
| --- | --- |
| `q1n1-usb-ebs-prep.efi` | `5ad50c56b467d70c038680d4737149aef60fa85163cd8bebfbf1a78d37c701f2` |
| `q1n1-usb-ebs.efi` | `1953a53daed43b16ed7120e98f34a5a2bead166e592ce3c2c41d61b184930bf6` |

The known-good `q1n1.efi` (`379558…41dd32`) and Windows boot manager were
verified unchanged by both installations.

Artifacts in `build/a16-install/`:

| File | SHA-256 |
| --- | --- |
| `usb-ebs-once.log` (UTF-16 prep log with register snapshots) | `c83b3249f9366a3519fd21455f83fb6cbfdba0fef521efa6d043997adca0498b` |
| `usb-ebs-once.startup.nsh` | `900198cb0ba7ef02796df5b77dbc961c7af6e7e6e397ed1ed0e8fbc1eb2337f8` |
| `usb-ebs-once-host.txt` (watcher) | `08d44436d5dd7d67c9cce618b375d02a110a657813079cffaeba94f206a0c667` |
| `usb-ebs-once-extended-echo.txt` | `d959fdaebbe6055e126673850dc40bbb144441d536c7355ac6178204bc409f17` |
| `usb-ebs-once-mac-capture.txt` | `098c4af94941f4c40f41c28a3fff6420cf3139eda5ce5c8c233630940c506a91` |
| `usb-ebs-once-mac-capture/events.jsonl` | `d993f745d45f4e94e00f31596ee8599dc5462a06f534eb0ba7f614e28669cc4b` |
| `usb-ebs-once-mac-capture/unified-log.ndjson` | `f241162bbbf51b149ab68f086676fa7ab90d965c6b0e97e0105543be8f3ed774` |

Also there: the `usb-ebs-once.json`, `-collected.json`, and install reports,
plus six raw `ioreg -l` snapshots. The capture ran with the older
single-identity label, so its `exact=False` for the EL2 serial is cosmetic;
the tool now recognizes both q1n1 identities.

Script hashes after this change: installer
`f3069b4ade983e70aa749cd8ce473112787488b187eca4223665e86b61ba9ad2`, runner
`84b8505ba010adbc44039156f20377a640e66991d94befc76f1a092820cbadc8`, collector
`ca28fd698738241149642e84a1582850f3d1bf6fb4ca8f3f5e064d0ecb29e9d4`. The prior
versions are kept on Windows as `*.before-ebs.bak`.

## Limits and next steps

- The takeover depends on this boot's firmware having powered, clocked, and
  tuned USB0 and its PHY (the prep step). q1n1 does not yet own GCC clocks,
  the GDSC, the eUSB2/QMP PHYs, or the repeater. That is acceptable for a
  debug transport launched from UEFI, not for a cold-start driver.
- High speed only; polled; single core; no GIC interrupt use. The status
  screen redraw pauses polling for a few milliseconds.
- These apps are experiments with a fixed 240-second window. The EL2 payload
  `q1n1.efi` is unchanged and does not include USB yet.
- Next: carry the m1n1 proxy (uartproxy framing over CDC port 0) on this
  driver inside the EL2 payload, keep the firmware prep as a pre-EBS stage,
  and then decide whether to take over clocks/PHY for independence from
  firmware.
