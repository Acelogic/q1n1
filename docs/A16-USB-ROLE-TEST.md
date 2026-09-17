# A16 USB0 device-role experiment

The earlier USB probes were passive. They found three host configurations, no
USB Function protocol, and the BIOS312 global configuration interface. This app
adds an explicit call to the captured firmware's role-selection method. It does
not yet publish CDC serial descriptors or transfer bytes to the Mac.

## Commands

From the EFI shell, with the existing ESP mapped as FS2, the active experiment is:

```text
fs2:\EFI\q1n1\q1n1-usb-role-change.efi --device0
```

Omit `--device0` for preflight only. All preflight checks also run automatically
with `--device0`; there is no need for another passive boot first. Keep the
existing Mac cable connected. This targets only controller 0, whose base must
match the captured URS0 address `0x0a600000` at runtime. The Windows inventory
previously placed the Mac on URS0/USB0. Its physical left/right socket location
has not been separately documented.

The expected successful result is:

```text
Loaded UsbConfigDxe code fingerprint matched BIOS312.
Preflight passed: USB0 is the host controller at 0x0A600000.
REQUESTING USB0 DEVICE MODE; the port's host devices will disconnect.
Role-change EFI status: 0x0000000000000000
USB0 resulting mode: 0x0000000000000004
USB FUNCTION handles: 0x0000000000000001
FUNCTION DRIVER PRESENT; cable enumeration is not tested yet.
```

Photograph the complete result, including any refusal or error. Success means
the firmware device driver became available during boot services. A Mac serial
port is not expected yet: descriptors, control requests, and data transfers are
the subsequent test. The app returns to the shell without ExitBootServices.

## Checks and effects

The app finds a loaded image with size `0x13000` and checks its relocation-free
code region, RVA `0x1000` through `0xbfff`, against the captured UsbConfigDxe.
The FNV-1a-64 checksum is `0x5a151951eae7fefb`; it detects accidental version
differences and is not an authentication mechanism. The offline auditor verifies
the source PE's SHA-256 and confirms that this region has no relocations.

Before a role call, the app requires the exact global and USB0 interface
addresses in that image, revision `0x20001`, expected callback addresses, an
initial USB0 host configuration, no existing USB Function interface, and a
permitted firmware policy state. It bounds the private PHY object against the
UEFI memory map, checks its base-getter callback, and reads its stored controller
base. These are RAM reads, not controller register reads or writes.

With `--device0`, it arms a 60-second firmware watchdog and invokes the checked
global interface's `+0x50` callback as `(This, 0, 4)`. The firmware performs the
actual host disconnect and device-driver startup. The app waits one second for
notifications, reports the resulting state, and disables the watchdog. The
watchdog is a fallback, not a guarantee against a firmware hang; power-cycle the
laptop if it stops responding. A reboot lets firmware establish its normal USB
policy again. There is no automatic attempt to switch back using an untested
reverse transition.

The app itself does not write controller MMIO, patch protocol fields, modify
firmware variables, or flash firmware. It remains a first hardware experiment:
physical Type-C negotiation and driver startup can still fail despite a matched
software path. It must not be used as a post-ExitBootServices transport.

## Validation and installation

Build:

```sh
make -f platform/uefi/Makefile usb-role-change
build/firmware-tools/bin/python tools/audit-a16-usb-binding.py
python3 tools/test-uefi.py --el 2 --usb-role-change
```

The native harness `tools/test-a16-usb-role-change.c` passed 18 cases with address
and undefined-behavior sanitizers. It tests the real preflight against the
captured image used only as data, plus the command transaction with a native mock
callback: default no-call behavior, changed code, wrong interfaces/base/policy,
inaccessible PHY data, memory-map errors, invalid options, watchdog rejection,
successful transitions, firmware errors, and successful calls that changed
nothing. No captured BIOS instructions execute in these tests.

QEMU booted the actual new EFI app with `--device0`, verified that it refused
the unsupported firmware before any role call, and verified return to the shell.
These checks do not simulate Qualcomm PHY or Type-C behavior.

The new file is installed separately at `\EFI\q1n1\q1n1-usb-role-change.efi`:

`0dc5f657f601e4bbc7663f738ea6fa994cc645fe0e47d7e74e2471275780f777`

The guarded installer verifies all existing manifest hashes, the known-good EL2
payload, the Windows boot manager, and unchanged BCD state. The new file is
appended to the existing removal manifest. The EL2 payload and passive probes
are retained.

## First physical result

The user's photo confirms the active test completed and returned to the EFI
shell. The loaded UsbConfigDxe code fingerprint matched the captured BIOS312
region. USB0's stored base was `0x0a600000`; firmware policy state was 3 and the
transition gate was 0. The role callback returned EFI_SUCCESS, USB0 changed to
mode 4, and exactly one USB Function interface appeared. No refusal or watchdog
error is shown.

The original photo is `build/a16-install/first-usb-role-change.jpg`, SHA-256
`81784c817e6ebf97e2391a3e6729a4112772476e22e5e5474effbd2e40d4c1cc`.
This verifies this firmware's controller-0 role-selection path and device-driver
binding on this boot. It does not verify physical cable enumeration or serial
transfers: the app deliberately started neither descriptors nor transfers.
The installation report remains the historical pre-execution record.
