# A16 firmware USB serial experiment

For the latest Dell WD22TB4 front-port experiments and the separate serial v3
inactive-controller path, see [A16-USB-DOCK.md](A16-USB-DOCK.md). The original
experiments below describe the earlier direct USB-C connection.

The physical BIOS312 role-change test succeeded: USB0 at `0x0a600000`
changed from host mode 1 to function mode 4, returned EFI success, and exposed
one USB Function protocol. See [the role test](A16-USB-ROLE-TEST.md).
That result does not establish cable enumeration or byte transfer.

`q1n1-usb-serial.efi --device0` is the next experiment. It starts a CDC ACM
greeting and binary echo service using firmware's USB Function driver. It
remains inside boot services, runs for up to five minutes, and returns to the
shell on Escape, timeout, or a handled error. A firmware watchdog is also
requested, but cannot guarantee recovery from a blocked vendor callback.

## Run

Keep the Mac connected to the same A16 USB-C port used for the role-change
test. From the A16 UEFI shell (the confirmed ESP mapping is FS2):

```
fs2:\EFI\q1n1\q1n1-usb-serial.efi --device0
```

The app performs the verified role change itself, or accepts USB0 already in
function mode. It checks the loaded UsbConfigDxe code, controller identity,
the loaded UsbfnDwc3Dxe code, revision, and all 19 protocol callback addresses
against BIOS312 before starting USB transfers. The fingerprints check version
compatibility; they are not cryptographic authentication of running firmware.

On the Mac, from the q1n1 repository:

```
python3 tools/test-a16-usb-serial.py --wait 180
```

The host tool opens only a serial child of USB device `1209:316d` with serial
`A16-Q1N1-UEFI`. It disables terminal echo and verifies fresh random binary
messages of 317, 1024, and 1537 bytes. Merely seeing a USB device or a serial
path is not a passing transfer test. `--list` only inspects matching paths.
While waiting it prints every 1209:316d identity, driver, and callout change.
For a full read-only record, run `tools/capture-a16-usb-host.py --out <new dir>`
alongside it; `tools/test-a16-usb-host-discovery.py` checks both tools offline.

The EFI screen reports driver initialization, bus events, configuration,
control lines, transfer counts, and final status. If it reports that stopping
the controller failed, it retains its memory and waits for a power cycle
rather than returning while DMA might still reference freed buffers.
After returning to the shell, `reset` restores normal firmware USB policy and
boots the normal Windows default.

## Implementation and validation

- USB2 and SuperSpeed CDC descriptors; full/high/super-speed packet sizes.
- Firmware-allocated transfer buffers, explicit endpoint-zero data/status
  stages, CDC line coding and DTR/RTS handling, notification endpoint, greeting,
  binary echo, and terminating zero-length packets for aligned bulk transfers.
- AArch64 EFI build with warnings as errors.
- Native AddressSanitizer/UndefinedBehaviorSanitizer host simulation covering
  descriptors, setup requests, control completion, busy buffers, binary echo,
  aligned transfers, reset, unsupported requests, and malformed event sizes.
- QEMU checks that an active request on unsupported firmware refuses to call
  the Qualcomm role method and returns to the shell. QEMU does not validate
  the physical Qualcomm controller or Mac enumeration.

Build and run the simulated host checks:

```
make -f platform/uefi/Makefile usb-serial
clang -std=c11 -Wall -Wextra -Werror -Wno-unused-function \
  -fsanitize=address,undefined -g -Iplatform/uefi \
  tools/test-a16-usb-serial.c -o build/uefi/test-a16-usb-serial
build/uefi/test-a16-usb-serial
python3 tools/test-uefi.py --usb-serial
```

The minimal ABI is documented in `platform/uefi/usbfn.h` using the
[UEFI USB Function header](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/UsbFunctionIo.h)
and [Qualcomm revision 0x10003 header](https://github.com/Project-Silicium/Mu-Silicium/blob/main/Silicon/Qualcomm/QcomPkg/Include/Protocol/EFIUsbfnIo.h).
The captured BIOS constructor supplies the checked callback addresses.

## Remaining hardware work

Installed on the existing ESP at `\EFI\q1n1\q1n1-usb-serial.efi` on
2026-09-16 at 21:11:39 UTC. SHA-256:
`1a10846a193163485ba85f4f5b9a980c6ca1849fb54b7e612e7d701970c3b450`.
The guarded installer verified all prior manifest files, the known-good q1n1
payload, Windows boot manager, and unchanged normal boot order. No partition
was created or resized. The simulated host passed 36 checks; role guards passed
19 checks; the final EFI binary passed the QEMU unsupported-firmware test.

The first physical run of this binary matched the UsbConfigDxe code but refused
at `Global/USB0 interfaces do not match inspected layout.` It returned to the
shell before calling the role method or initializing CDC. The matching global
and USB0 interface counts were not printed by that build, so the cause remains
unresolved. The earlier physical role-change success remains valid evidence
for that earlier boot. The user then ran the unchanged, previously successful
role-test binary without arguments on the same boot and reported the same
layout refusal. This reproduces the problem outside the new CDC app. The next
passive probe then showed only two USB2 host handles (`Pci(3,0)` and
`Pci(1,0)`) and three configuration handles (selectors 5, 3, 1). Selector 0 and
`Pci(0,0)`, both present during the successful role-change test, are absent on
this boot. USB Function and Serial IO remain absent. This identifies a missing
USB0 interface as a reason the layout check cannot pass; the underlying
firmware/Type-C initialization cause is still unresolved. The next reversible
experiment is reattaching the Mac cable to the same A16 port and repeating the
guarded serial command. No guard has been relaxed and no missing interface has
been synthesized.

Probe photo: `build/a16-install/usb0-missing-role-probe.jpg`, SHA-256
`0ff4b6c4e337f20e8a443d7744fc0ac529b21c47c0d8458117be9ac2aaa37db1`.

Photo: `build/a16-install/first-usb-serial-layout-refusal.jpg`, SHA-256
`1c997c7ddff0cb97c95bf152b628b5e10a998c7c8b1563c69f7ee8e205ea2edc`.
Source now prints expected and observed interface addresses and match counts
on this refusal; this is installed in the separate v2 app described below.

After the user reattached the Mac cable to the same A16 port, a subsequent
photo confirmed that the installed serial binary progressed through all
checks: USB0 base `0x0a600000`, policy 3, gate 0, role-change EFI success,
mode 4, one USB Function interface, USB Function code/callback checks passed,
and `USBFn initialization status: 0`. The app announced its CDC identity and
received `USB attach.` No reset, negotiated speed, or configuration event is
visible in that photo.

Photo: `build/a16-install/first-cdc-initialized-attach.jpg`, SHA-256
`76702c537b43acf67a40234a60c20ba77919728c4fc764b8503edf9029a19aeb`.
The Mac still exposed only the Samsung T7 in its USB host device inventory and
no `/dev/cu.usbmodem*`. One Mac `AppleT8122USBXDCI` instance reported
`OnBus: true`, `DeviceState: Default`, address 0 and unknown connection speed;
the other two were disconnected. This suggests a remaining Type-C data-role
issue but does not prove the cause or identify the physical port by itself.
The next test is reattaching the cable while CDC is actively waiting, observing
both A16 USB events and Mac device/host state.

The bounded Mac observation `build/a16-install/mac-usb-link-20260916T212617Z.jsonl`
then recorded disconnects at 21:26:51 and 21:27:45 UTC and reconnects at
21:27:12 and 21:28:01 UTC. The same Mac XDCI instance (`0x10000044e`) returned
to `OnBus: true`, `DeviceState: Powered`, address 0, configuration 0,
`Super Speed Plus (10 Gbps)`, hardware link state U3. No q1n1 USB host-device
entry or serial path appeared. This associates the observed cable cycles with
the Mac device-controller state; it does not establish successful enumeration
or prove which Type-C policy component needs changing.

The user subsequently reported refusals after repeatedly issuing `--device0`.
The next photo confirms `Global/USB0 interfaces do not match inspected layout`
and shows two configuration entries with selector 0 / mode 4. Their pointer
addresses are not visible, so aliases of the same object remain a hypothesis.
The `--device1` attempt only printed usage; that option is not implemented.
Photo: `build/a16-install/duplicate-usb0-function-configs.jpg`, SHA-256
`fd95f8546b44bafa0668c2d7da0e130d81678a0008b54e7920087808a634a842`.

## Diagnostic revision 2

Installed alongside v1 at `\EFI\q1n1\q1n1-usb-serial-v2.efi` on
2026-09-16 at 21:36:04 UTC; SHA-256
`b2c3a2082fb68d8ddaa2d871ae538cf9dbbc69aadc969e48d01d751165bb913b`.
The retrieved installation report and ten-file ESP manifest agree with the
local binary. Existing files and normal Windows boot order remain verified.
The laptop was in Windows at installation. V2 subsequently passed its physical
preflight, role transition, USBFn initialization and reached USB attach, as
recorded in [A16-USB-TYPEC.md](A16-USB-TYPEC.md). Enumeration/echo remain unproven.

This revision matches the exact static BIOS object addresses rather than
requiring exactly one handle per object. Multiple handles for the identical
pointer are accepted and reported; the same fields at another pointer are
still rejected. All revision, callback, PHY/base, role, and USB Function checks
remain in force. Failure output includes expected and observed pointers.
It also removes the misleading intermediate “returning to shell” text from
the shared role helper when invoked by the serial app.

Validation: 25 native role/guard cases and 36 CDC host-simulation checks passed
with address/undefined-behavior sanitizers. The isolated v2 AArch64 build passes
warnings-as-errors and QEMU's unsupported-firmware refusal/return-to-shell test.
This validates the guard change, not the physical alias hypothesis or serial.

The user has only USB-C-to-USB-C cabling. A fresh Windows inventory
(`build/a16-install/usb-inventory-role-debug.json`) again sees the Apple USB
device beneath A16 `URS0/USB0/RHUB/PRT0`, confirming that the present connection
uses the A16 as host in Windows. The USB-C control stack includes working
`UCM-UCSI ACPI Device` (`ACPI\USBC000\5`) and Qualcomm USB Type-C
(`ACPI\QCOM0F9D`, service `qcusbcucsi_8480`). Captured BIOS modules
`UsbPwrCtrlDxe` and `PmicGlinkDxe` were copied with provenance into
`build/private/a16-typec-drivers` for offline protocol analysis. No calls into
their unknown interfaces or Type-C policy modifications have been made.
The guarded passive Type-C snapshot app was subsequently built, tested, and
installed alongside v2. See [A16-USB-TYPEC.md](A16-USB-TYPEC.md) for the exact
firmware evidence, limits, and next physical check.

**Update 2026-09-17:** physical enumeration and exact binary echo passed with
serial v3 through the Dell WD22TB4 dock, once the Mac tool was fixed to use
`ioreg -l` (it previously could not see any callout path). See
[A16-USB-DOCK.md](A16-USB-DOCK.md#physical-uefi-serial-echo-pass-v3-rerun-2026-09-17-1127z).
Firmware USB Function calls cannot provide a console after ExitBootServices.
(Now done with a q1n1-owned DWC3 driver; see [A16-USB-EL2-SERIAL.md](A16-USB-EL2-SERIAL.md).)
That later stage needs q1n1-owned controller, DMA, event, and endpoint handling,
followed by transport integration into the m1n1 proxy. A successful echo test
would prove the cable/port path and give a protocol reference for that work.
