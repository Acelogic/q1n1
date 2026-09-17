# A16 USB-C role diagnosis

The later Dell WD22TB4 front-port test reports the desired A16 device role
and an inactive USB0 controller. See [A16-USB-DOCK.md](A16-USB-DOCK.md) for
that evidence and the separate serial v3 test; the direct-cable results below
remain historical observations.

The available cable is USB-C to USB-C. Windows sees the Mac's Apple USB device
under A16 URS0/USB0; the cable carries data. The first UEFI CDC test switched
USB0 to function mode and received an attach event, but the Mac did not
enumerate q1n1 or create a serial port. During observed cable cycles, the
Mac's AppleT8122USBXDCI instance remained its device-side interface, at address
0 and configuration 0. A Type-C data-role mismatch is a hypothesis, not yet a
confirmed root cause. Local controller mode and negotiated cable roles must
be checked separately.

## Captured firmware evidence

`build/private/a16-typec-drivers/UsbPwrCtrlDxe.efi` came from UX3607OA.312.
SHA-256: `7e51cca7441ba299e222c53a3a9e0a6ce1613387b0f016b24113f0d8df334fcb`.
The adjacent manifest preserves the path within the extracted BIOS.

`tools/audit-a16-usb-typec.py` checks the exact file hash, ARM64 PE layout,
protocol GUID/table, relocation-free code fingerprint, and selected status
decoder instructions. It records disassembly in
`build/private/a16-typec-drivers/typec-evidence.json`. It never executes the
captured driver. The matching runtime checks use FNV-1a-64 as an accidental
version-mismatch guard, not authentication.

- USB power protocol: `e07df17e-e79e-4150-9378-50623a14994a`, static object
  RVA `0x30c0`, revision `0x10004`, fourteen callbacks.
- Code: RVA `0x1000`, length `0x2000`, FNV `0xa599916bef31a4c9`; no relocations
  in this region.
- Status wrapper at `0x1e18` branches to `0x2398`. Its backend-6 path reads
  cached per-port data instead of querying the PMIC protocol.
- Port count: byte at `0x3138`. Configuration pointer: `0x3640`; its first
  bytes after offset 0 contain valid port IDs, checked by the helper at
  `0x223c`. Maximum accepted ID is 5. Configuration records have stride
  `0xc8`, with Type-C backends at offsets `0x30` and `0x34`.
- Cache-ready flag: `0x35fc`; status cache at `0x3698 + id * 16`;
  orientation byte at `0x3608 + id`. Backend 6 checks bit 24 for attachment
  and bit 25 for source power. Its data-role output is derived from several
  fields; this probe deliberately reports no independently established
  USB data role.
- Slots `+0x60` and `+0x68` immediately return unsupported. Their intended
  operation names have not been established. No verified data-role-swap
  setter has been found.

The older Qualcomm-authored
[EFIUsbPwrCtrl.h](https://github.com/Rivko/android-firmware-qti-sdm670/blob/main/boot_images/QcomPkg/Include/Protocol/EFIUsbPwrCtrl.h)
(blob `b9da9e37cc265d0a45ec0c6366062bad1904c6af`) provides names for the first
eleven callbacks but describes revision `0x10001`. Its hardware-info layout
does not match this target. The older PMIC USB interface also differs from
the offsets used by BIOS312. These old declarations are not a runtime ABI
for this machine and must not be used to guess a setter offset.

## Passive snapshot app

`platform/uefi/usb-typec-probe.c` locates the exact loaded power driver and
installed static protocol, checks all fourteen callback addresses, bounds the
configuration allocation using the UEFI memory map, and reads cached RAM.
It calls no vendor methods, writes no registers or firmware fields, changes
no USB roles, and returns to the shell. Unknown backend states remain raw.
The cache can be stale or updated during the snapshot, and the power-driver
port IDs are not yet mapped to physical connectors.

Build and validate:

```sh
make -f platform/uefi/Makefile usb-typec-probe OUT=build/uefi-usb-typec-probe
build/firmware-tools/bin/python tools/audit-a16-usb-typec.py \
  --mapped-output build/private/a16-typec-drivers/UsbPwrCtrlDxe.mapped
clang -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -g \
  tools/test-a16-usb-typec.c -o build/test-a16-usb-typec
build/test-a16-usb-typec build/private/a16-typec-drivers/UsbPwrCtrlDxe.mapped
python3 tools/test-uefi.py --usb-typec-probe --build-dir build/uefi-usb-typec-probe
```

Result: 21 native snapshot/guard cases passed with ASan/UBSan; the ARM64 build
passed warnings-as-errors; QEMU correctly refused nonmatching firmware and
returned to the shell. Captured firmware bytes were data in the host tests.
These checks do not validate physical Type-C state.

Installed at `\EFI\q1n1\q1n1-usb-typec-probe.efi` on 2026-09-16 at
21:54:29 UTC. SHA-256:
`4b182b02ec1db1b6a8c0449af72cb541e4a788504579f550c7ca3b188dd10e83`.
The guarded installer preserved existing files and normal Windows boot order.
The existing EL2 payload and serial-v1 remain intact; serial-v2 is installed
alongside them. Neither this new snapshot nor serial-v2 has been run on the
physical laptop yet.

After installation verification, the existing one-time shell boot script was
invoked over Windows SSH. Firmware bootsequence selected the q1n1 shell, with
Windows still first in displayorder. A shell screenshot and the physical
snapshot result are pending; the reboot request alone does not prove arrival.

## First physical snapshot

The user's next photograph confirms arrival at the shell and successful probe
completion. Saved as `build/a16-install/first-typec-cache-snapshot.jpg`, SHA-256
`57dd813f20533396bd8b2965a6bf6391236070761568725f6c6adbe2f6e7dc00`.

- Port count 2; cache-ready flag 1.
- Both ports: status backend 6, alternate backend 2; decoded cached power
  role sink; cached extra 0.
- Port 0: cached status `0xff0000004d010100`, orientation byte 1.
- Port 1: cached status `0xff0000004d010000`, orientation byte 0.
- No vendor callbacks or hardware writes were made by the snapshot app.

The contemporaneous Mac inventory shows XDCI registry ID `0x10000044e` on-bus
and **Configured**, address 1, configuration 1, High Speed (480 Mbps). The only
Mac host-side USB device is the Samsung T7; no USB serial path exists. Thus
the Mac currently has an enumerated device-side link while the A16 is at its
shell. Cached sink power on the A16 does not establish its USB data role.
The two power port IDs still have no confirmed physical connector mapping.

Next: run the installed serial-v2 once on this fresh boot and compare Mac
enumeration while it waits. Capture any refusal instead of retrying the
role-change operation repeatedly.

## Serial-v2 physical result on the same boot

The next photograph confirms serial-v2 ran with `--device0`. Both static
configuration objects had exactly one handle reference, so this run does not
exercise the alias fix. Preflight passed with base `0x0a600000`, policy 3,
gate 0 and initially zero USB Function handles. The role callback returned
success, resulting mode was 4 and one USB Function handle appeared. USBFn
fingerprint/callback checks and initialization passed; CDC descriptors were
active and an attach event arrived. No reset/configuration event is visible.

Photo: `build/a16-install/serial-v2-initialized-attach.jpg`, SHA-256
`e01fbe11a8f777c3a4de42a41b1389aea3e57c9bbdb8d01e5fb3fae077becfa9`.

The Mac again shows XDCI `0x10000044e` on-bus but Default, address 0,
configuration 0, unknown speed. Its host inventory still contains only the
Samsung T7 and no serial path. The host tester had not opened a device or
received bytes at the time of this observation. This confirms v2 initialization,
not serial enumeration or the root cause of the connection failure.

Next comparison: stop the serial app with Escape, keep the cable connected,
and run the passive cached-state snapshot once more. Stopping the app stops
the function controller, so the second snapshot is explicitly after cleanup,
not an atomic view while CDC was running.

The follow-up photograph confirms clean serial termination: test status 0,
setup requests 0, bulk RX/TX 0, USBFn stop status 0. The post-cleanup probe
matches the original snapshot in every displayed field, including raw cached
status, extra data and orientation for both ports. No enumeration occurred.
Photo: `build/a16-install/serial-v2-stopped-typec-unchanged.jpg`, SHA-256
`3f02ffbca3445b8e4dc441fbc9ab15f1a2a6cb687afdb8b89783fca8e79956ad`.

## Mac-side data-role investigation

The existing macvdmctl service supports Apple-specific VDM operations, not a
generic data-role request. Its no-op path also enters/leaves debug mode, so
it was not used for this read-only investigation. No changes were made to
that service, its allowlist or the Aurora repository.

`tools/mac-typec-read.cpp` inventories HPM mode/status/power/data registers
through the existing AppleHPMLib interface. It contains no Command, Write,
unlock or DBMa operation. Unprivileged plugin creation failed. The native
macOS administrator dialog was then used to run this read-only helper;
all sixteen register reads succeeded.

On this Mac boot, HPM1 registry ID `0x10000060d` has status `0x108284bd`,
power bytes `3d 0f 00 00 00 00`, data bytes
`01 00 88 20 21 00 00 00 11 32 00 00 00 00`, mode `APP `.
The status register's data-role bit 6 is clear (UFP/device); the power
register describes an attached PD source link. HPM0 has status `0x100248fd`
(DFP/host) and carries the Samsung drive. HPM2 is disconnected. HPM5 is a
separate power connection.

The live device-tree/IOService mapping ties `usb-drd1-port-hs` and `-ss`
to `hpm1@A/.../Port-USB-C@2` through their `UsbTransportState` property.
HPM1's provider has `port-location=left-front` and `port-number=2`.
USB DRD1 is the on-bus XDCI instance associated with the earlier A16 cable
cycles. The target is therefore selected explicitly, not by choosing the
first controller or the default RID0 used by macvdmtool.

The standard `SWDF` data-role request is described by
[TI SLVA843A](https://www.ti.com/lit/an/slva843a/slva843a.pdf) and implemented
with status verification in
[Linux tipd/core.c](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/tipd/core.c)
(inspected blob `f76f563dc42b8fc0e632742247b2957b1e573f01`). Bit definitions
come from `tipd/tps6598x.h`, blob
`11ab58ba9a1826119a54c8978fee778098404d99`. Their application to this Mac
firmware is an experiment; a supported family command does not guarantee
the partner will accept it or macOS will establish a host connection.

`tools/mac-a16-data-role.cpp` is a separate, narrowly targeted helper.
No arguments perform preflight only. `--host` requests `SWDF` once after
checking this boot's registry ID, RID1, exact service type, left-front
provider, APP firmware mode, register sizes, attachment and PD source power.
An already-host port is left alone. It makes no unlock/debug-mode/power-swap
requests and performs no register writes. It reads the task result and
polls status for up to two seconds; there is no command retry. The hardcoded
registry identity deliberately prevents reuse after a Mac reboot.

Both helpers build with Clang warnings-as-errors using the separately
licensed `AppleHPMLib.h` from the existing macvdmtool checkout:

```sh
clang++ -std=c++17 -Wall -Wextra -Werror \
  -I/Users/mcruz/Developer/aurora-silicon/macvdmtool \
  tools/mac-a16-data-role.cpp -framework IOKit -framework CoreFoundation \
  -o build/mac-a16-data-role
```

The one-shot request ran through macOS native administrator authentication.
`build/a16-install/mac-data-role-swap.log` records API success (`0`), task
result first byte `0x03`, and unchanged status `0x108284bd`. No DFP/host role
was confirmed and no retry was sent. The task value matches the rejection
code used by the cited Linux/TI interface; it does not establish which side
of the negotiation rejected the request. The user subsequently returned the
A16 to Windows for the next diagnostic installation.

## UCSI transport read diagnostic

Captured `PmicGlinkDxe.efi` has SHA-256
`d9eb488f75923db967a05f081313864157e74f6b72d9b369be238921c7a3b87e`,
image size `0x6000`, protocol GUID `7429eb07-f2e3-4a6b-aa96-c909cd956ef7`,
static interface RVA `0x40b0`, revision `0x1000c` and 24 callbacks.
`tools/audit-a16-ucsi.py` verifies those values and code FNV
`0x391a635ac9656c89` over RVA `0x1000`, length `0x3000`, with no relocations.
It saves the relevant disassembly in `build/private/a16-typec-drivers/ucsi-evidence.json`.

The first callback at `0x19e4` takes a pointer to a caller-owned buffer pointer.
It sends GLINK owner `0x800b`, opcode `0x11`, then copies exactly 48 bytes from
the driver's response cache at `0x4408`. The transport readiness byte is at
`0x43e1`, and its boot-services pointer at `0x4238`. These target-specific
observations agree with the older Qualcomm-authored
[EFIPmicGlink.h](https://github.com/mobstyle10-arch/BOOT.MXF.2.5.1/blob/main/buildpath0/boot_images/boot/QcomPkg/Include/Protocol/EFIPmicGlink.h)
read/write prefix (inspected blob `36930661352471d0d2474bb9b42bcbaec8713513`),
but that header's revision is `0x1000b`, so the complete current table is
checked against the captured target instead of assumed from that header.

`platform/uefi/usb-ucsi-probe.c` matches the loaded driver, exact installed
interface, revision and all 24 callbacks. It requires transport readiness
and the expected boot-services reference, arms a 30-second firmware watchdog,
requests one buffer read, and prints raw VERSION/CCI/CONTROL/message data.
The read request communicates with the existing firmware; it does not issue
a UCSI reset, notification, connector-status or role command. Existing
message data can be stale. The output cannot by itself identify an active
connector or prove SET_UOR support. A watchdog is a fallback, not a guarantee
against all firmware hangs.

Validation: isolated AArch64 warnings-as-errors build, 15 native mock cases
with ASan/UBSan, and QEMU unsupported-firmware refusal/return-to-shell all
passed. Captured firmware was never executed by the native tests.
Artifact: `build/uefi-usb-ucsi-probe/q1n1-usb-ucsi-probe.efi`, SHA-256
`5f023ecbddfc0c5c62d00a9bc509abc7fe0eafc2f56c3931bb276d7597ecbb49`.
Installed on the existing ESP at `\EFI\q1n1\q1n1-usb-ucsi-probe.efi`
on 2026-09-16 at 22:21:15 UTC. The downloaded installation report and
12-file manifest agree with the local artifact hash. The installer verified
the known-good EL2 payload and Windows boot manager hashes and preserved
Windows as the default. Evidence is in
`build/a16-install/usb-ucsi-probe-install.json` and `esp-install.json`.
The existing one-time shell script then verified the q1n1 shell bootsequence
and requested a reboot; SSH disconnected. The user subsequently photographed
the probe completing in the shell: ready flag 1, transport status 0, and
VERSION, CCI, CONTROL and both messages all zero. It returned to the shell.
Evidence: `build/a16-install/ucsi-read-zero-buffer.jpg`, SHA-256
`a3c203b426472fa6941e2b402bb22e90d3860c5320a3722cb8e1be763e7238e8`.

Further inspection limits what status 0 proves. The receive handler at
`0x2bdc` checks opcode `0x11` and packet length `0x40`, copies 48 bytes
from packet+12 to cache `0x4408`, and reaches the generic completion path.
It does not check the trailing remote return code. Other messages and
wrong-length responses can also reach generic completion without updating
this cache. The read callback then copies the cache into the caller buffer.
Therefore this photograph does not establish a fresh successful UCSI
response or usable UCSI initialization, and is not a basis for SET_UOR.
The upstream [Linux GLINK UCSI receive implementation](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/ucsi/ucsi_glink.c)
checks response lengths and the remote return code before completing a read.

## Separate Qualcomm USB-C status buffer

The next diagnostic, `q1n1-usb-usbc-probe.efi`, uses the same exact BIOS312
driver/interface guards but invokes callback 2, RVA `0x1b1c`. Inspection
confirms `EFI_STATUS (UINT8 **, UINT8)`, a caller-owned 132-byte output,
GLINK owner `0x800c`, opcode `0x14`, a 148-byte request, and a fixed 132-byte
copy from cache `0x4438`. The receive handler at `0x2b5c` requires a
148-byte packet and copies its 132-byte payload; it shares the remote-error
and generic-completion limitations above. Evidence is captured by
`tools/audit-a16-ucsi.py` in `build/private/a16-typec-drivers/usbc-evidence.json`.

The diagnostic sends one status-buffer read, uses a 30-second watchdog,
checks contiguous guards on both sides of the output, and prints all 132
bytes as offset-labelled little-endian words. A nonzero caller sentinel
detects a completely untouched output; it cannot establish fresh remote
data. No role, reset, write, connect or notification callback is invoked.
Raw data is not yet interpreted as a connector identity or negotiated role.

Validation: AArch64 warnings-as-errors build, 19 native mock cases for each
buffer size (38 total) under ASan/UBSan, and QEMU unsupported-firmware refusal
and shell return passed. The tests cover output-pointer changes, adjacent
buffer corruption, no copy and an all-zero copy. Captured firmware was not
executed by the native tests. The original installed UCSI EFI is preserved.

Artifact: `build/uefi-usb-usbc-probe/q1n1-usb-usbc-probe.efi`, 6144 bytes,
SHA-256 `10aa5d23020390514320bfac7afb38fbbc34e73d149a9ebdb4c6e33a092fab6c`.
Installed on the existing ESP on 2026-09-16 at 22:34:52 UTC. Downloaded
`build/a16-install/usb-usbc-probe-install.json` and the 13-file manifest agree
with the local hash; the installer verified the previous payloads and
preserved the Windows default. The one-time shell bootsequence was verified
and a reboot requested. The user subsequently ran the installed probe.

### First physical USB-C buffer result

The photograph `build/a16-install/usbc-status-nonzero.jpg` (SHA-256
`f32152201804405926809b2b390a2e1d4e428dbec7ad303f2c6c2deb8b70609c`)
shows ready flag 1, EFI status 0, all 132 bytes differing from the caller
sentinel, intact output guards, and return to the shell. This proves the
callback populated the output; it does not resolve the remote-completion
limitation above or prove USB enumeration.

The photographed little-endian words are:

```text
+000 4c01010000000001
+008 00000000ff000000
+010 4c01000100000000
+018 00000000ff000000
+020 0000020200000000
+028 00000000ff000000
+030 0000020300000000
+038 00000000ff000000
+040 through +080: zero
```

The older Qualcomm header
[`UsbPwrCtrlLibPmUcsi.h`](https://github.com/mobstyle10-arch/BOOT.MXF.2.5.1/blob/main/buildpath0/boot_images/boot/QcomPkg/Library/UsbPwrCtrlLib/wp/UsbPwrCtrlLibPmUcsi.h)
(Git blob `8ed48d2883e8477d649504a193885255cc3b2c91`) identifies selector 1
as pin-assignment data, followed by 16-byte records. Its common bitfields
align with the BIOS312 cached-status decoder at `0x24dc` onward. Applying
that layout provisionally gives:

| Record | Port ID | Orientation | Mux | Raw connect bit | Power bit | Power mode | Partner type |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 1 | 1 | 0 | 0 | 3 | 2 |
| 1 | 1 | 0 | 1 | 0 | 0 | 3 | 2 |
| 2 | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| 3 | 3 | 2 | 0 | 0 | 0 | 0 | 0 |

Do not interpret raw connect=0 as proof the cables are detached. The older
Qualcomm consumer
[`UsbPwrCtrlLibPmUcsi.c`](https://github.com/mobstyle10-arch/BOOT.MXF.2.5.1/blob/main/buildpath0/boot_images/boot/QcomPkg/Library/UsbPwrCtrlLib/wp/UsbPwrCtrlLibPmUcsi.c)
(blob `6cdf9f934b10499f8b6f06301303ab4c2b8ae440`, lines 198-216) explicitly
replaces that flag with `mux_ctrl != 0` when constructing its cached status.
It also does not copy the raw port-ID field. That behavior would explain
the earlier cached `4d010100`/`4d010000` versus raw `4c010100`/`4c010001`.
The analogous BIOS312 cache-update sequence has not yet been fully traced;
this is a source-supported explanation, not a confirmed target write trace.

The old enum names power mode 3 as PD and partner type 2 as UFP. Neither
record should be reported as a current negotiated data role until freshness
and physical connector mapping are established. There are only two physical
Type-C ports in the prior power-driver inventory; additional raw records
must not be counted as additional laptop connectors.

Next physical comparison: leave charger and unrelated USB devices unchanged,
unplug only the Mac-A16 cable, run the same USB-C probe, and compare the raw
records. Then reconnect the same cable/ports and repeat if the first
comparison identifies useful changes. No additional role write is needed
for this mapping experiment.

### Mac cable unplug comparison

The next user photograph contains the previous connected output and a new
run after the requested Mac-A16 cable unplug. Evidence:
`build/a16-install/usbc-status-mac-unplugged.jpg`, SHA-256
`3355434af2b57740264010b87cde654fc4e9dd002a0af1ce27bd0e17e30d20ad`.
The new run again returned EFI status 0, populated all 132 bytes, and
returned to the shell. Only `RAW +000` changed, from
`4c01010000000001` to `0000020000000001`; all other displayed words match.

Thus the selector remains 1 and record 0's common word changes from
`ff0000004c010100` to `ff00000000000200`: orientation 1 -> 2 (open), mux
1 -> 0, power mode 3 -> 0, and partner type 2 -> 0. Record 1 is unchanged.
This controlled unplug ties firmware Type-C record/port ID 0 to the A16
connector currently used for the Mac cable, and demonstrates that this
status path responds to a physical change. It does not prove that every
transport success represents a newly matched response or that the Type-C
port ID equals the USB controller ID. No role-switch command was sent in
these status-only comparisons.

### Reconnection and another serial attempt

The subsequent photograph `build/a16-install/usbc-reconnected-serial-attach.jpg`
(SHA-256 `a9a65d4829da8a5801833dead21bcbcfaccee28d57cc324c4f5b9fe73de0bcdf`)
shows record 0 restored to its original values on reconnect. It also shows
serial-v2 passing its guards, switching USB0 to function mode, initializing
USBFn and reaching USB attach. The Mac still exposed no A16 USB modem.
A 45-second host echo wait timed out without a serial node.

The user then reconnected the cable while asked to leave serial-v2 running.
The Mac peripheral controller remained active: Powered, address 0,
configuration 0, with a reported Super Speed Plus link and hardware U3.
The 120-second host wait in `build/a16-install/serial-v2-replug-host.log`
also ended with `A16 serial port did not appear`. This is not an echo pass.

After the break on 2026-09-17, the user reported Windows running with the
same Mac connection. A fresh Mac-side check showed an active peripheral
controller in Configured state, address 1, configuration 1, High Speed
(480 Mbps), and no USB modem nodes. This supports Windows/A16 as host and
the Mac as peripheral in that session, and confirms a data-capable cable
connection. SSH to the A16's previous Wi-Fi address timed out; the current one
was requested. No reboot or role change was performed during this status
refresh.

The user subsequently supplied the new address. SSH connected with
`HostKeyAlias` set to the old one, so strict checking still ran against the
A16's previously trusted ED25519 key and the existing known-host entry was not
overwritten.
The fresh Windows inventory captured at `2026-09-17T07:49:15.6685497Z`
again shows the Mac (`05ac:1905`) under
`ACPI(_SB_)#ACPI(URS0)#ACPI(USB0)#ACPI(RHUB)#ACPI(PRT0)`.
The composite parent is OK; its NCM interfaces have driver errors, which
does not undo the evidence that the A16 enumerated the Mac as a peripheral.

Both `ACPI\\USBC000` (`UcmUcsiAcpiClient`, version `10.0.28000.1`) and
`ACPI\\QCOM0F9D` (`qcusbcucsi_8480`, version `685.13804.30.0`) are present
and OK. Their paths are `_SB_.UCSI` and `_SB_.UCS0`, respectively.
Read-only copies of the Qualcomm driver, INF, resource `UCS0.bin`, and
inventory are in `build/private/a16-windows-usbc/`, with hashes and source
provenance in `manifest.json`. Despite its ACPI-style filename, `UCS0.bin`
is an INF-referenced driver resource (`Resources/BinaryPath`), not a
standard ACPI table accepted by `iasl`. Its format and any relevant role
policy still need analysis. Windows remains running; no driver settings,
boot configuration, or USB roles were changed during this refresh.

## Reuse from Aurora m1n1

The checked Aurora checkout is
`/Users/mcruz/Developer/aurora-silicon/m1n1-aurora`.
Its `src/usb_dwc3.c` implements DWC3 CDC ACM descriptors, transfer/event rings,
buffering and reads/writes. `src/usb.c` performs Apple PHY/HPM setup and
registers the USB I/O device. `proxyclient/m1n1/proxy.py` opens the Mac's
`/dev/cu.usbmodem...` serial device and carries the proxy protocol.

This is useful for a q1n1-owned post-ExitBootServices transport. The DWC3 core
logic and proxy framing are candidates for reuse; the current DWC3 allocation
path calls Apple DART mapping routines, and platform setup uses Apple ADT,
ATC PHY and HPM hardware. Those dependencies require Qualcomm replacements.
The current EFI CDC app uses firmware's USB Function protocol and does not
yet run the full m1n1 proxy. Neither CDC descriptors nor proxy framing fix a
USB-C link whose host/peripheral roles remain wrong.

## Next milestone

The cached-state, raw USB-C, unplug/reconnect and serial-v2 comparisons
above are complete. Repeating controller role changes has not established
a Mac-host/A16-peripheral link. The modern register transport has now been
identified in the exact DSDT and both Windows/EFI drivers. Physical Windows
and EFI reads returned UCSI VERSION `0x0210`; the old all-zero buffer was a
different transport. See [UCSI register evidence and connector queries](A16-UCSI-REGISTERS.md).
Connector queries now succeed and identify connector 1 as a connected PD
USB data link with the A16 acting as sink and USB host. One initiated UFP
request completed with error (`CCI 0xc0000000`). Its error payload did not
provide a usable standard error code. The conditional serial test was
skipped, Windows returned normally, and the temporary startup was removed.
The next investigation is the firmware's data-role policy and error response
path; a Mac-host/A16-peripheral link remains unproven.

The installed serial-v2 command is:

```text
fs2:\EFI\q1n1\q1n1-usb-serial-v2.efi --device0
```

Serial requires actual host enumeration and the host tool's random-byte echo
checks. An attach event or successful role callback alone is insufficient.
Post-ExitBootServices USB still requires a q1n1-owned controller implementation.
