# Direct USB-C on the A16

## Finding, 2026-09-17

The cable's negotiated data role cannot be inferred from DWC3 `GCTL`.
BIOS312 left USB1 (`0x0a800000`) configured as a host while the attached Mac
also reported host/DFP. Both xHCI ports read `0x2a0` (no connected device).
The Mac's NCM gadget was disconnected. The existing NCM-only discovery path
could therefore wait indefinitely even though the cable was attached.

The same untouched cable passed device-mode serial tests on USB1: a reset,
configuration and both CDC interfaces, with exact echoes from 1 through 65,536
bytes. The Mac USB tree placed this device directly under its host controller;
USB0's existing console remained below the dock's hub. A subsequent integrated
stage served the full q1n1 proxy over direct CDC, including an exact 1 MiB
write/read round trip (0.49 s in this run). Both direct and dock proxy NOPs passed.

The installed EFI subsequently passed a dock-free Windows-to-q1n1 boot with
the direct cable left connected. It reported generation 0, EL2 and an armed
return, and exact memory round trips through 1 MiB passed. The Mac USB tree
contained only the direct `A16-Q1N1-EL2B` device under AppleXHCI.

A further proxy reset was verified by observing the old USB registry device
disappear and a new one appear 51.3 seconds later. The same EL2 and transfer
checks passed. Two consecutive user-operated full power-off / 10-second wait /
power-on tests also passed with the dock absent and cable untouched. The direct
device reappeared 55.1 and 55.3 seconds after disconnect, reported generation 0
and EL2, and passed the same memory transfers (1 MiB round trip: 0.49 s).
These are the observed results for this connector and cable. Opposite-role NCM
cold starts, other connector/cable combinations and unplug/replug are not
qualified by those tests.

After the second cold start, a further 20 random-data round trips verified
11,141,130 payload bytes across ten host opens, including 65,537-byte transfers
that cross the proxy bounce-buffer boundary. Closing the tty for 12 seconds
and reopening it also passed; the USB registry device ID remained unchanged.

## Automatic discovery

The payload first tries the existing xHCI/NCM path. When USB1 has no connected
device, it can temporarily offer CDC device mode instead. The fallback:

- Checks the USB1 core identity, host configuration, two-port layout and both
  connect bits before changing direction. It never borrows USB0 or a connected
  xHCI port.
- Halts xHCI, preserves global/PHY configuration, and uses q1n1's existing
  DWC3 device driver with separate DMA memory.
- Uses serial identity `A16-Q1N1-EL2B`, distinct from USB0's
  `A16-Q1N1-EL2`. Duplicate identities configured successfully but collided in
  macOS's tty naming during the first experiment.
- Keeps a configured CDC link running even when its tty is closed. An
  unanswered device-mode attempt returns to host discovery. USB setup/reset
  activity extends the enumeration window.
- Stops the additional device controller before reboot or chainload so its DMA
  cannot retain pointers into an outgoing stage. Restoring host mode requires
  new xHCI rings.

Each DWC3 instance now owns its EP0 string buffer, so simultaneous descriptor
requests cannot overwrite the other controller's pending response. Host tools
recognize both serial identities. USB1's second interface is currently reserved;
the full proxy is on its first data interface.

This fallback is specific to the inspected BIOS312 USB1 configuration. It is
not a generic Type-C/PD driver and does not change a power contract or send a
PD role swap. NCM remains the path when the Mac negotiates device mode.

## Negative experiments

- Freshly rebuilt and installed baseline code reproduced the failure; a stale
  EFI payload was not its only cause.
- An atomic USB1 DWC3 core/PHY reset did not restore a host connection. The dock
  console survived. `HCCPARAMS1 = 0x0118ffc5` has PPC clear; toggling xHCI's
  port-power bit is not an established way to cycle physical VBUS here.
- UCSI `SET_UOR` requesting A16 host/DFP was rejected. The opposite-end Mac
  `SWUF` request returned task result `3`, and live readback remained host/DFP.
- A PPM reset completed but left connector 2 status zero in later reads. A
  successful transport call is insufficient evidence of current connector state.

The role-swap helper `tools/mac-typec-device.cpp` is a diagnostic only: it
requires a current-boot AppleHPM registry ID, is read-only by default and sends
one request with `--device`. It does not unlock or retry the controller.
The automatic fallback does not require this helper or Mac administrator access.

References: [Linux UCSI command definitions](https://raw.githubusercontent.com/torvalds/linux/master/drivers/usb/typec/ucsi/ucsi.h),
[Linux TI data-role requests](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/tipd/core.c).

## Validation and local evidence

Native ASan/UBSan tests cover refusal of USB0, bad core/layout/alignment,
connected ports, halt timeout, failed takeover cleanup, exact register restore,
and independent pending USB string descriptors. The existing 25 DWC3,
31 proxy, 39 xHCI/NCM and 48 NCM-packet checks pass. QEMU passes 37 EL2 proxy
checks, including two chainloads; QEMU does not model this Type-C hardware.

Ignored local logs: `build/coldboot-direct-device-unique.log`,
`build/coldboot-direct-echo.log`, `build/dualrole-direct-proxy.log`,
`build/coldboot-phy-reset-test.log`, `build/coldboot-ucsi-host-swap.log`,
`build/coldboot-mac-device-swap.log`, `build/uefi-dualrole-qemu.log`.
Dock-free firmware boot evidence: `build/direct-only-boot-1.log` and
`build/direct-only-reboot-verified.log`. An intervening transfer run in
`build/direct-only-boot-2.log` did not independently establish a new boot;
do not count it as one. The verified reset run explicitly tracks USB device IDs.
Physical power-cycle evidence: `build/direct-only-power-cycle-1.log` and
`build/direct-only-power-cycle-2.log`.
Random-data, repeated-open and idle-reopen evidence: `build/direct-only-stress.log`.
