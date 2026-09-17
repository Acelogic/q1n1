# A16 USB console investigation

> Update 2026-09-17: USB CDC ACM works in UEFI ([A16-USB-DOCK.md](A16-USB-DOCK.md))
> and after ExitBootServices with q1n1's own DWC3 driver
> ([A16-USB-EL2-SERIAL.md](A16-USB-EL2-SERIAL.md)). Statements below that call
> post-ExitBootServices serial unimplemented are historical.

The A16 ran q1n1 after ExitBootServices at EL2 on 2026-09-16. The subsequent
active USB0 role-change test succeeded: firmware reported mode 4 and one USB
Function protocol. Post-ExitBootServices serial remains unimplemented.

A firmware CDC ACM greeting/echo app is now installed separately on the ESP.
See [the serial experiment](A16-USB-SERIAL-TEST.md) for its command, Mac tester,
validation, and remaining physical enumeration/echo test.

The subsequent EFI driver list and offline BIOS analysis found a loaded but
unbound USB function driver. See [USB driver binding evidence](A16-USB-BINDING.md)
for its configuration checks and the separately staged passive role probe.

## Evidence from the Windows capture

The existing capture is `/Volumes/Untitled/A16-capture-20260916`. Its
`evidence/metadata/pnp-present.json` and `signed-drivers.json` report both
`ACPI\QCOM0F8B\0` and `ACPI\QCOM0F8C\1` as working Synopsys USB 3.0 Dual-Role
Controllers, using Microsoft's `urssynopsys.inf` version `10.0.28000.1`.

The exported `QcUsbFnSsFilter8480.inf` matches the function-mode children
`URS\QCOM0F8B&FUNCTION` and `URS\QCOM0F8C&FUNCTION`, includes Microsoft's
`ufxsynopsys.inf`, and installs the Qualcomm USB function filter. This establishes
driver support for device mode, not that the port was in that mode during capture.
No function-mode child was found in that captured present-device inventory.

The ACPI mapping already recovered from SPCR/DBG2/DSDT is:

| Controller | ACPI identity | Controller address | Firmware status |
| --- | --- | --- | --- |
| URS0 | QCOM0F8B | `0x0a600000` | Enabled |
| URS1 | QCOM0F8C | `0x0a800000` | Enabled |
| URS2 | QCOM0FED | `0x0a000000` | Disabled |
| UCS0 | QCOM0F9D | Type-C control device | Present in Windows |
| UBF0 | QCOM0C6D | USB4 host-router bus | Present in Windows |

The fresh Windows inventory after the first EL2 boot identifies the attached
Apple device (`VID_05AC`, `PID_1905`, bus description `Mac`) under
`ACPI(_SB_)#ACPI(URS0)#ACPI(USB0)#ACPI(RHUB)#ACPI(PRT0)`. Thus the currently
connected socket routes through URS0. Its physical left/right position has not
been recorded. Windows is the USB host on this connection; the Mac presents a
USB composite device with NCM Control/Data interfaces. Those NCM interfaces
report errors in Windows, so working USB networking is not established.

URS0 has an active HOST child using `USBXHCI`; URS1 has no active child in this
snapshot. No FUNCTION child is present. The Mac's host-device inventory shows
the Samsung T7 Shield but no A16, and no new `/dev/cu.*` serial node is present.
The GENI UART at `0x00894000` is a separate interface; this cable does not establish
a UART path. A Mac-hosted USB serial connection would require changing the A16
to device mode and implementing its USB transport. A host-side transport to the
Mac's existing device interface would be a different implementation path.

## Passive Windows inventory

Run `tools/collect-a16-usb.ps1` on the A16 while its cable is connected to the Mac.
It captures present USB controller/role/function devices, parent/child and
location properties, matching drivers, SSH service state, and firmware boot
entries. It writes a timestamped JSON file beside the script, or to `-OutputPath`.
An elevated session is needed to include the boot-entry inventory.

The collector does not enable devices, change USB roles, or change boot settings.
The offline capture remains separate from any fresh inventory.

## Passive firmware discovery

Build a separate application, preserving the physically tested `q1n1.efi`:

```sh
make -f platform/uefi/Makefile usb-probe
python3 tools/test-uefi.py --el 2 --usb-probe
```

Output: `build/uefi/q1n1-usb-probe.efi`. Once copied into the ESP's `EFI\q1n1`
directory, run it from the UEFI shell. FS2 was the ESP in the first session;
confirm that mapping again if it changes.

```text
fs2:\EFI\q1n1\q1n1-usb-probe.efi
```

To save the output on the ESP for retrieval from Windows:

```text
fs2:\EFI\q1n1\q1n1-usb-probe.efi > fs2:\EFI\q1n1\usb-probe.txt
```

This application enumerates installed USB Function, USB2 Host Controller, and
Serial I/O protocol instances; prints their device paths and applicable interface
revisions; and returns to the shell. It does not call controller methods, touch
controller MMIO, connect drivers, or exit boot services. Output is capped at 16
instances per protocol and 2048 characters per path. A missing protocol means
it was not installed at that moment, not that the hardware lacks device mode.

Protocol ABIs were checked against EDK2's
[UsbFunctionIo.h](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/UsbFunctionIo.h),
[Usb2HostController.h](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/Usb2HostController.h),
[SerialIo.h](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/SerialIo.h), and
[DevicePathToText.h](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/DevicePathToText.h).

An installed USB Function protocol could support a separate experiment while
firmware services are alive. It is not itself a console transport after
ExitBootServices. The EL2 target still needs ownership of controller/PHY power,
clocks, reset, DMA/IOMMU mapping, Type-C role handling, and a device-side transport.
QEMU checks the discovery ABI and return to the shell, not these Qualcomm details.

## Validation and deployment status (2026-09-16)

The discovery application builds with `-Wall -Wextra -Werror`. Its QEMU test
found one USB host and one serial interface, checked the serial revision and
device-path conversion, reported no USB Function protocol, and returned to the
shell. The existing EL2 boot test also passed after correcting a stale serial-log
race in the test harness.

SSH authentication was restored, and `sshd` is running with automatic startup.
The Windows collector was parsed and executed successfully. The fresh inventory
is saved locally as `build/a16-install/usb-inventory-current.json`.

The probe was installed at `\EFI\q1n1\q1n1-usb-probe.efi` on the existing ESP
using `tools/install-a16-usb-probe.ps1`. Its copy was hash-verified, and the new
file was appended to `esp-install.json` so the existing removal script covers it.
The previous installation manifest was backed up on Windows as
`esp-install-before-usb-probe.json`. The added probe's installation record is
`build/a16-install/usb-probe-install.json`.

The known-good q1n1 payload, Windows boot-manager file, and firmware boot entries
were verified unchanged. Windows remains first in the normal boot order. A
subsequent user-requested one-time reboot into the shell succeeded, and the
physical discovery run completed as recorded below.

The separate probe's SHA-256 is
`22e98b77953f85be946cdb71cf1fdb99df993f516701adfae23add0acbb3b712`.
The archived, physically tested EL2 payload remains
`379558299150a121220ee40b7e8095238aad3474c48a695e0b121bf93c41dd32`.

## Physical discovery result (2026-09-16)

The user ran `fs2:\EFI\q1n1\q1n1-usb-probe.efi` on the A16. The photograph
saved at `build/a16-install/first-usb-probe.jpg` shows:

| Item | Observed result |
| --- | --- |
| Entry exception level | EL2 (`0x2`) |
| USB Function protocol | Not installed |
| USB2 Host Controller protocol | Three instances |
| Host instance 0 device path | `Pci(0x3,0x0)` |
| Host instance 1 device path | `Pci(0x0,0x0)` |
| Host instance 2 device path | `Pci(0x1,0x0)` |
| Serial I/O protocol | Not installed |
| Completion | Returned to `Shell>` |

This is physical evidence of the probe's successful execution and of the
protocols installed in this boot session. It does not establish that no USB
function driver exists in the firmware, that no vendor-specific USB protocol
exists, or that the hardware cannot change roles. The displayed PCI paths must
not be equated to URS0/URS1 or MMIO addresses without further mapping evidence.
There is no installed standard USB Function or Serial I/O interface to reuse
immediately for a pre-boot console. USB after ExitBootServices remains a separate
driver and transport task.

Before returning to Windows, save the shell's driver inventory for inspection:

```text
drivers > fs2:\EFI\q1n1\drivers.txt
```

Then start the existing Windows boot manager directly from this shell:

```text
fs2:\EFI\Microsoft\Boot\bootmgfw.efi
```

These paths use the ESP mapping verified in this photo. The driver list can help
identify loaded USB driver bindings; absence there is not proof that the firmware
image contains no unstarted or vendor-specific driver.
