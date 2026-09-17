# ASUS A16 USB driver binding evidence

The USB function driver exists in the A16 firmware, but the physical role probe
found three host configurations and no configuration satisfying its function-mode
binding check. A fourth, pathless interface matches the driver's global
initialization values. No role change has been attempted.

## Physical EFI driver inventory

After the first USB probe, the user saved `drivers.txt` in the ESP and returned
to Windows. It was retrieved over SSH on 2026-09-16. The original UTF-16 file is
`build/a16-install/firmware-logs-20260916-194520/drivers.txt`, 9,014 bytes, SHA-256
`6e6fa9e4337b0af78b029747a58ae324db13d5dd8ad50263f8ea7303bdd5a8fd`.

Relevant rows from this boot:

| Shell handle | Image name | Binding version | Devices managed | Children |
| --- | --- | --- | --- | --- |
| EA | UsbfnDwc3Dxe | `0x30` | None shown | None shown |
| EB | XhciPciEmulation | `0x30` | 3 | None shown |
| EC | XhciDxe | `0x30` | 3 | None shown |
| ED | UsbBusDxe | `0x0a` | 3 | 5 |
| 142 | TerminalDxe | `0x10000` | None shown | None shown |

Handle numbers are specific to this boot. The loaded but unbound function
driver explains why a missing USB Function protocol is not evidence that the
driver is absent. This list alone does not identify the reason it remained
unbound.

## Offline BIOS312 analysis

The Windows driver export contains the matching BIOS update package:

`/Volumes/Untitled/A16-capture-20260916/evidence/drivers/ux3607oa_312.inf_arm64_9e61646d4bf5e5d7/UX3607OA.312`

The package is 17,560,041 bytes, SHA-256
`b8503236a028f1ed5c599fc787e4ba1ffeedda8c6258bca09bc79dd3e4249a62`.
Its outer FFS volume starts at byte `0xb69`, is `0x10be680` bytes long, and has a
valid 16-bit firmware-volume header checksum. The generic parser's brute scan
did not find this unaligned capsule payload; carving the validated volume allowed
extraction with [uefi-firmware-parser](https://github.com/theopolis/uefi-firmware-parser)
1.16. No captured source file or laptop firmware was modified.

Extraction artifacts are under `build/private/bios312`. Five relevant original
PE files and their dependency expressions were copied to
`build/private/a16-usb-drivers`, with a provenance manifest:
`UsbfnDwc3Dxe`, `UsbConfigDxe`, `UsbInitDxe`, `UsbDeviceDxe`, and `XhciPciEmulation`.
These are private analysis artifacts under the ignored build directory.

Capstone 5.0.9 and pefile 2024.8.26 were used to inspect the AArch64 drivers.
The function driver and host emulation driver both open protocol
**`e722b03f-b250-42ce-8ebd-5bd51812d037`** on the candidate controller with
`EFI_OPEN_PROTOCOL_BY_DRIVER`, inspect two 32-bit fields, and close the protocol.

| Driver | Supported callback RVA | Field at `+0x88` | Field at `+0x8c` |
| --- | --- | --- | --- |
| UsbfnDwc3Dxe | `0x2898` | Unsigned value <= 5 | Must equal 4 |
| XhciPciEmulation | `0x1868` | Unsigned value <= 5 | Must equal 1 |

The field names are not established from source. The probe calls the first a
selector and the second a configuration mode based on these checks. Matching
the values alone does not prove that OpenProtocol can succeed, that the driver
can start, or that the cable has negotiated the corresponding physical role.
Writing these fields directly is not an implemented role-switch sequence.

The original PE hashes are:

- UsbfnDwc3Dxe: `424e7089d82fa00dda6cd91a2546d9ff26c47feb8cc2ba0bd94da67cbad63881`
- XhciPciEmulation: `33ada521f151add5684e83a26c0b5e13b4cc2e007436fedf328add1313385a6e`

Reproduce the targeted static checks with:

```sh
build/firmware-tools/bin/python tools/audit-a16-usb-binding.py
```

The script refuses different binary hashes and saves the callback disassembly,
checked offsets, and UsbConfigDxe initialization evidence to
`build/private/a16-usb-drivers/binding-evidence.json`.
This analysis is of the captured BIOS312 package; the loaded driver bytes have
not been read back and compared against it.

## Separate passive role probe

`q1n1-usb-role-probe.efi` adds enumeration of the Qualcomm configuration protocol
to the previous standard-protocol inventory. It prints each instance's two data
fields and device path, and compares the fields with the checks above. It does
not call vendor methods or DriverBinding.Supported, connect or disconnect
drivers, change roles, access controller MMIO, or exit boot services.

```sh
make -f platform/uefi/Makefile usb-role-probe usb-role-fixture
python3 tools/test-uefi.py --el 2 --usb-role-probe
```

The QEMU-only fixture publishes three synthetic interfaces with null methods:
a host configuration, a function configuration, and an out-of-range selector.
The test passed all three classifications and verified the return to the shell.
The original standard-protocol probe's QEMU test also passed. This validates the
reader and firmware-service ABI, not Qualcomm runtime state or role switching.
**Never deploy `usb-role-fixture.efi` to hardware.** It was not transferred to the
A16 and is not included in any hardware kit.

The role probe is staged separately at `\EFI\q1n1\q1n1-usb-role-probe.efi`.
Its SHA-256 is
`959253791348804cb8066bc6812352c5cb198845fe0654ea7e1f90b51af7c0ce`.
It was run from the EFI shell with the ESP mapped as FS2:

```text
fs2:\EFI\q1n1\q1n1-usb-role-probe.efi
```

## Physical role result, 2026-09-16

The user photographed the `q1n1 A16 BIOS312 USB role discovery v1` output. It
entered at EL2, completed, and returned to the shell. The original image is
`build/a16-install/first-usb-role-probe.jpg`, SHA-256
`e68997747ff83eb4008b1393fabe41660515c86cf6fa82d040dfbc1994015d8c`.

| Instance | Field `+0x88` | Field `+0x8c` | Device path | Binding data check |
| --- | --- | --- | --- | --- |
| 0 | 5 | `0x10000` | Unavailable | Neither |
| 1 | 3 | 1 | `Pci(0x3,0x0)` | Host |
| 2 | 0 | 1 | `Pci(0x0,0x0)` | Host |
| 3 | 1 | 1 | `Pci(0x1,0x0)` | Host |

The standard inventory again reported three USB2 host handles and no USB
Function or Serial IO protocol. This establishes the configurations visible
during this boot, not a permanent hardware limitation. None currently exposes
the mode 4 configuration needed by the function driver's Supported callback.
The installation JSON predates this photo and remains a historical installation
record; its original `HardwareProbeRun` value is not a current result.

## Global interface and candidate role-selection path

In the captured UsbConfigDxe binary, SHA-256
`5b440b75bcda81a0c21cd6a82aa1ede1b6b060bdf23b1969575cb512e2992a4c`,
the initialization code at RVA `0x173c` copies a 256-byte template from RVA
`0xc458` to a separate interface at RVA `0xe590`. The template revision is
`0x20001`. The initializer explicitly writes selector 5 and mode `0x10000` into
that interface's `+0x88` and `+0x8c` fields, then installs the configuration
protocol GUID. These values exactly match the pathless physical instance. This
supports identifying it as the global interface; it does not establish another
usable physical controller. Runtime driver bytes are still not read-back verified.

The actual configuration template has a callback at offset `+0x50`, pointing to
RVA `0x3ac8`. Its wrapper passes the second and third arguments as 32-bit values
to RVA `0x4ca8`; the inferred arguments are a controller selector and a requested
mode. That implementation checks controller state and mode, can disconnect the
existing controller driver, updates its configuration, installs the host or
function handle, and calls `ConnectController`. The function branch selects mode
4. Thus a driver-managed path exists in this captured binary; changing the
exposed data fields alone would bypass it.

The separate role-change test has now invoked this callback on the A16 after
checking the loaded code and controller base. It returned success and produced
USB0 mode 4 with one USB Function interface. This validates the tested transition,
not a general public API or physical cable negotiation. Complete cable policy
and connector behavior remain to be characterized.
The table at RVA `0xedd8` is a different internal callback table and must not be
used as the installed configuration-protocol ABI.

The separate [role-change experiment](A16-USB-ROLE-TEST.md) now checks the loaded
code, interface layout, and USB0 base before invoking this candidate callback.
Its build, native refusal/transaction tests, and QEMU unsupported-firmware refusal
passed. The user's photo confirms that the physical transition also succeeded.
USB Function protocol binding establishes a
firmware boot-services transport opportunity. q1n1 still needs its own USB
transport after ExitBootServices; USB serial remains unimplemented.
