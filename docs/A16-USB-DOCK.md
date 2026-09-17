# A16 serial through the Dell WD22TB4

> Update: serial after ExitBootServices, using q1n1's own DWC3 driver, passed
> on 2026-09-17. See [A16-USB-EL2-SERIAL.md](A16-USB-EL2-SERIAL.md).

Hardware session: 2026-09-17, UX3607OA BIOS312. The dock's attached host cable
connects to the Mac. The user moved the A16's C-to-C cable to the dock's front
USB-C data port. macOS enumerated the WD22TB4, its USB hubs, and Ethernet.

## Connector roles and first serial attempt

The original serial-v2 app returned before starting USB. The matched firmware
exposed its global configuration and controller 3, but no USB0 interface.
The main log is `build/a16-install/usb-serial-dock-once.log`, SHA-256
`cd2fbd419118225fe1d07813f1dda001b6c9a9ca383da8d7ed6cef65d3029671`.
Windows returned at 10:30:06Z. The exact temporary startup was removed by the
collector at 10:32:59Z. The Mac watcher was stopped after Windows returned;
no serial device appeared and no echo exchange ran.

A separate passive protocol inventory and UCSI status query showed:

- Only USB host controller 3 (`Pci(0x3,0x0)`) was registered.
- No USB Function or Serial I/O protocol was installed.
- UCSI connector 1 reported `0x202b5a46`: connected, PD, local power sink,
  partner DFP/host, hence local device data role. Three MESSAGE_IN samples agreed.
- Connector 2 reported disconnected.

This is the desired data direction. The GLINK cached-response freshness caveat
still applies; connector-role reporting does not prove USB enumeration.
No SET_UOR, power swap, reset, or cancellation command was sent. The query only
temporarily enabled command notifications, queried status/capabilities, ACKed
completion, and disabled those notifications.

Log: `build/a16-install/usb-dock-probe-once.log`, SHA-256
`733e6a1d1fbf26af3b8dfc768a635337c8075a92b70a44dbff4ce1ac19f0c8e1`.
Windows returned at 10:36:13Z; startup removed at 10:37:38Z.

## USB controller RAM snapshot

The new `usb-controller-probe` target reads the exact BIOS312 UsbConfigDxe
controller table even when a controller's protocol is not registered. It checks
the loaded image fingerprint and global ABI, bounds each PHY object against
the firmware RAM map, and verifies its base-getter address before decoding the
stored controller base. It invokes no vendor callbacks and reads no MMIO.

The physical snapshot found:

| Controller | Published references | Mode | Policy | Gate | PHY base |
| --- | ---: | --- | --- | --- | --- |
| 0 | 0 | `0x10000` | 3 | 0 | `0x0a600000` |
| 1 | 0 | `0x10000` | 3 | 0 | `0x0a800000` |
| 2 | 0 | `0x10000` | 5 | 0 | `0x0a000000` |
| 3 | 1 | 1 | 1 | 0 | `0x0a400000` |
| 4 | 0 | `0x10000` | 6 | 0 | `0x0a200000` |

All revisions were `0x20001`, selectors matched the controller index, and PHY
objects were present with verified base getters. This establishes initialized
software objects, not a qualification of PHY clocks or the electrical link.

Probe SHA-256:
`f7a59a10567ffc634d3ea8c9a6879b58072b7c658998f4b4d9baaaf086f29af3`.
Log: `build/a16-install/usb-controller-dock-once.log`, SHA-256
`1dd0c07073aec4e5ca34ede5724220deab23cc81fb4cb5f7b396906dd869b62d`.
Windows returned at 10:44:56Z; startup removed at 10:45:53Z.
Validation: nine ASan/UBSan fixture cases and QEMU unsupported-firmware refusal
with confirmed return to shell. The captured BIOS is never executed by host tests.

## Serial v3 inactive-controller startup

The BIOS312 global `+0x50` method at RVA `0x3ac8` forwards controller and mode to
RVA `0x4ca8`. That implementation verifies the PHY object and policy. At `0x4dcc`
it recognizes prior mode `0x10000` and skips the old-driver disconnect path
before installing/connecting the new device driver.

The separately built `usb-serial-v3.c` enables this already-present firmware
path for the captured USB0 state. It accepts an unpublished USB0 object only at
the known static address, with mode `0x10000`, revision `0x20001`, selector 0,
policy 3, gate 0, verified PHY/base `0x0a600000`, no existing USB Function
protocol, and the existing global fingerprint/callback checks. Unpublished
host/device-mode objects remain refused. Normal registered-controller checks
are preserved. It makes the same `(global, 0, 4)` call as the prior serial app;
it does not write UCSI roles or patch firmware state.

Binary: `build/uefi-usb-serial-v3/q1n1-usb-serial-v3.efi`, SHA-256
`19c5b2414e9736e635d92899ea070c13eef144d69a98854ddb70815e7a115b95`.
Validation passed: 36 combined controller cases, 36 simulated CDC checks,
and QEMU unsupported-firmware refusal with return to shell. These checks do not
establish physical serial operation. The physical v3 result is recorded below.

The ESP installation completed at 10:51:12Z. The first reboot attempt lost
SSH before execution; a later read-only check confirmed no v3 test metadata,
no pending boot sequence, and the unchanged 10:44:56Z Windows boot time.
The retry created `usb-serial-dock-v3-once.json` at 10:59:20Z and confirmed
the one-time shell boot sequence before SSH disconnected during restart.
Startup SHA-256:
`e94ff9b51d6ca084b615dbe414314d0cd3e8957330b9a92b6077fec79aceb303`.
The Mac listener log is `build/a16-install/usb-serial-dock-v3-host-retry1.txt`.

### Physical v3 result, 2026-09-17 11:05Z

The inactive-controller path passed on the A16. The firmware role call returned
success, mode became 4, and one USB Function protocol was installed. USBFn
initialization and stop both returned success. The event log then recorded
attach, reset, high speed (3), configuration 1, and 15 setup requests. RX and
TX byte counters stayed zero, and there was no logged CDC control-line change.
This is progress beyond the earlier attach-only runs, but serial echo remains
unproven.

The Mac listener never found a matching usable serial port. Its `ports()`
function reports only devices with the exact VID/PID/serial string AND a
descendant existing `/dev/cu.usbmodem*` path. Consequently, its silence does
not establish that no USB device enumerated. Capture the raw device and
interface tree during the next test, then distinguish an identity-filter miss
from a CDC driver-binding or descriptor problem before further role changes.

**Later correction (11:20Z):** that listener could never have matched. It ran
`ioreg -a -r -c IOUSBHostDevice` without `-l`, and ioreg omits all properties of
descendant nodes in that form, so no `IOSerialBSDClient` child ever exposed
`IOCalloutDevice`. This was reproduced on the Mac with `/dev/cu.debug-console`.
The Mac's persisted unified log also shows the A16 was present for the whole
run: CryptoTokenKit saw `0x1209/0x316d` at location `0x112000` at 10:59:58.9Z,
`icdd` listed the `(2,2,0)` and `(a,0,0)` interfaces, and removal followed at
11:04:58.5Z, exactly the app's 300-second window. Whether a tty was created in
that run cannot be recovered; the rerun below settles binding. Extract:
`build/a16-install/usb-serial-dock-v3-mac-unified-log.txt`, SHA-256
`0b696a0821b807ecce44a0888b73cf2a8a3c7bc891e8e10252ceae9b277f530b`.

Windows returned at 11:05:04Z. The collector copied the log and removed the
exact temporary startup script at 11:05:59Z. Local log and startup hashes agree
with the remote collection report. `SerialLogFound: false` is an unused
auxiliary-log flag for this mode; the main `ResultFound` is true.

Result: `build/a16-install/usb-serial-dock-v3-once.log` (UTF-16), SHA-256
`898c62d0bb06d720fab9e9492fe385002575d49625cf99e1e57997cc974e0a3c`.
Metadata: `usb-serial-dock-v3-once-collected.json` in the same directory.
The host listener was stopped after Windows returned; its final interruption
is operator cleanup, not an echo failure. No host payload was sent.

The existing runner/collector now have separate `serial-dock`, `dock-probe`,
`controller-dock`, and `serial-dock-v3` modes with distinct log names. They reuse
the one-time shell entry, preserve Windows-first default order, and remove only
the startup script whose hash matches that test's metadata. The original EL2
payload and older serial binaries remain installed unchanged.

## Physical UEFI serial echo: PASS (v3 rerun, 2026-09-17 11:27Z)

The same installed v3 binary (SHA-256
`19c5b2414e9736e635d92899ea070c13eef144d69a98854ddb70815e7a115b95`) was rerun
unchanged under the separate `serial-dock-v3-r2` runner/collector mode, so the
first v3 log stays preserved. Only the Mac tooling changed:

- `tools/test-a16-usb-serial.py` now uses `ioreg -l` and prints every 1209:316d
  identity/driver/callout change while waiting. Opening still requires the
  exact VID/PID, serial string `A16-Q1N1-UEFI`, and a descendant callout path.
  (Retries for the transient `ioreg: error: can't obtain child` race were added
  to both Mac tools after this run; see the capture note below.)
- `tools/capture-a16-usb-host.py` is a read-only capture that records each
  change in the A16 device subtree (interfaces, bound drivers, dext proxies,
  callout paths), all `IOSerialBSDClient` paths, raw `ioreg -l` snapshots, and
  a filtered unified-log stream. It never opens a serial port.
- `tools/test-a16-usb-host-discovery.py` holds 14 synthetic IORegistry fixture
  checks, including the no-`-l` blind spot, wrong or missing serial strings,
  an ECM dext on the data interface, and ioreg retry behavior.

Sequence (UTC; Mac log clock is EDT):

1. 11:26:24Z runner metadata and startup; 11:26:31Z restart requested with the
   one-time shell entry and Windows-first display order.
2. 11:27:01.448Z `AppleUSBHostPort::enumerateDeviceComplete: enumerated
   0x1209/316d/0100 (q1n1 A16 serial) at 480 Mbps` behind the dock's USB 2 hub
   at `0x112000`; `AppleUSBCDCCompositeDevice selected configuration 1`.
3. `AppleUSBACMControl` bound interface 0 (2/2/0, INT EP 0x81). Apple's
   `AppleUserECM` dext personality (class 10/0/0, IOProbeScore 100000) was
   launched for interface 1, logged `AppleUserECMData::start(IOUSBHostInterface)
   fail`, and IOKit then started `AppleUSBACMData`. That dext launch is benign;
   it also appears in the first v3 run's log.
4. 11:27:02Z `/dev/cu.usbmodemA16_Q1N1_UEFI1` appeared. The watcher opened it and
   verified exact random binary echoes of 317, 1024, and 1537 bytes.
5. 11:27:30Z a second open of the identity-verified port echoed 1, 511, 512,
   513, 2048, 4096, and 16384 bytes exactly (about 76 KiB/s round trip). The
   1-byte case is weak evidence on its own; the others are not.
6. 11:32:01Z the app's 300-second window ended; macOS logged `hardware
   connection lost` and removed the tty. Windows booted at 11:32:06Z.

The EFI log agrees with the host byte for byte: configuration 1, control lines
3/0 twice (two port opens), 0x2B setup requests, **Bulk RX 0x693F = 26,943
bytes** (2,878 + 24,065 sent by the Mac) and **Bulk TX 0x695E = 26,974 bytes**
(the same echo plus the 31-byte greeting once). Serial test and USBFn stop
status were both 0. The watcher reported `greeting observed: False` because
macOS asserts DTR on open and the tool flushes input immediately afterward.

This meets the physical UEFI serial milestone: the actual Mac identified the A16
CDC device, opened the correct port, and verified exact binary echo while EFI
was running. The descriptors, class handling, and firmware USB Function path
need no change for this stage. Post-ExitBootServices serial is still not
implemented; firmware USB Function callbacks are not a supported post-EBS
transport.

Collection at 11:33:07Z removed only the matching startup script. Afterward the
firmware boot manager had no pending `bootsequence`, `{bootmgr}` remained first,
and Q: was unmounted. Remote script backups are
`run-a16-ucsi-register-once.before-v3r2.bak` and
`collect-a16-ucsi-register-once.before-v3r2.bak` in the Windows staging folder.

Artifacts in `build/a16-install/` (SHA-256):

| File | SHA-256 |
| --- | --- |
| `usb-serial-dock-v3-r2-once.log` (UTF-16 EFI log) | `5e320308d719e2e96605dba2100f836043f6ccbd334ecfdca38864c9017e5f34` |
| `usb-serial-dock-v3-r2-once.startup.nsh` | `57ed7867f863804a2d2aec46735922ec7b9ef28738b068c13c3e37d46168d040` |
| `usb-serial-dock-v3-r2-host.txt` (watcher) | `2af1d3938c73c6c09d0db86d6e2aa83f50ba5083fc18b3ed82ac36209c718159` |
| `usb-serial-dock-v3-r2-extended-echo.txt` | `17ec814a834a56a500914b622d3a02164b5cd5f2ca3e48cd8e932e5fb2e04c52` |
| `usb-serial-dock-v3-r2-mac-capture.txt` | `78bbab04e620c064c1ec2788c1c9bcf7ec9361ec7e5e5c75d1d239a3e76c86e2` |
| `usb-serial-dock-v3-r2-mac-capture/events.jsonl` | `0e28ae8b359b898672bf9900090cb88475c71ca795b9487a53957bef9a80cf71` |
| `usb-serial-dock-v3-r2-mac-capture/unified-log.ndjson` | `4809dba6954b433118c2cefd9df416c395179540c6bbd2ffb339bc5e440bde04` |

The capture directory also holds four raw `ioreg -l` snapshots (absent, bound,
tty present, absent). The capture process itself stopped at 11:34:03Z, after
the test window, on the transient ioreg race now handled by retries.
Metadata: `usb-serial-dock-v3-r2-once.json` and `-collected.json`.

Validated script hashes after this change: runner
`2b57d60832a9a55a55430162293bd47834c11648cd36f2a893fccbf50ee881ac`, collector
`57e79ce0fae453dcd9c463316973a9abb6391e52ffb8ad4c17ec1653aa1e22c8` (both
uploaded, hash-matched, and parsed with zero PowerShell errors on the A16).

Rerun recipe (Mac first, then A16 over SSH; pick new output names each time):

```sh
python3 -u tools/capture-a16-usb-host.py --out build/a16-install/<name>-mac-capture --duration 900
python3 -u tools/test-a16-usb-serial.py --wait 600 | tee build/a16-install/<name>-host.txt
```
