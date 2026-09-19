# Port-independent USB transport work, 2026-09-19

Status: implementation passes native/QEMU checks. All six direct-cable
Mac/A16 pairings pass; the dock passes both A16 ports through the second Mac
port. Physical port-pairing tests stopped at the user's request. The tested EFI
was installed on the ESP and independently hash-verified through Windows SSH
at 21:09 UTC. Verification of the next firmware boot is pending.

## Physical evidence before changes

- User topology: Mac -> dock -> one A16 USB-C port, plus a separate Mac -> other A16 USB-C port.
- The working connection is USB1 (`0x0a800000`), serial `A16-Q1N1-EL2B`, below a USB hub. The Mac reaches the generation-0 payload at EL2 and repeatedly reads bootinfo and mapped memory.
- USB0 (`0x0a600000`) has GCTL `0x102001` (device), DCTL `0x10f00000` (stopped), USBCMD `0`, USBSTS `1`. The other Mac port reports HPM status `0x108284ad`, whose data-role bit 6 is clear (Mac device/UFP).
- The IORegistry `IOAccessoryUSBConnectString` describes the connected partner. `Host` was initially misinterpreted as the Mac's role; the raw HPM status and simultaneous known dock-host connection correct that interpretation. Direct HPM register reads require administrator access, which was unavailable; these HPM values are the kernel-published IORegistry snapshots.
- The running PE `.text` (88,688 bytes) exactly matches the local EFI build: SHA-256 `f74b9faa8dba7c075b4e0b22b528428f543ece7f8f25ae2607c60b46fe764d7d`.
- A bounded, guarded USB0 host experiment used separate DMA scratch at `0xb9280000`, kept USB1 responsive, and enumerated the Mac as `05ac:1905`. USB0 PORTSC reached `0x00000e03` (connected, enabled, high speed). USB0 was halted and its original GCTL restored afterward.
- A follow-up NCM experiment received 33 Ethernet frames but received 0/3 ICMP echo replies; teardown reported a USB transaction error. This is receive-path evidence, not a verified bidirectional NCM link. The dock proxy remained responsive and USB0 was restored.
- Reproducible bounded experiment: `build/secondary-usb/usb0-host-probe.py` (read-only by default; `--probe`, optionally `--ncm`, performs the temporary experiment).

## Confirmed implementation problems

1. USB0 can be left stopped in device mode after the firmware boot window, even when the Mac is the device and needs the A16 to be the host.
2. The payload's existing secondary transport pointer is used for either direct CDC or NCM. Once occupied, discovery does not inspect the other controller.
3. NCM link-up checks use initialization state rather than current port connectivity. A detached link can remain selected and prevent discovery.
4. Direct CDC recovery is specific to USB1. USB0 does not have equivalent post-boot role discovery.
5. The main Mac reader searches serial ports only. The NCM helper defaults to a fixed interface name. These assumptions fail across transport and Mac-port changes.

## Implementation and qualification plan

- Introduce independent per-controller host/device discovery with separate DMA and transport state, stable-link preservation, bounded role attempts, physical disconnect detection, and clean retry state.
- Keep controller direction separate from negotiated Type-C role: discover a working USB data path without sending PD policy or power-role commands.
- Preserve live NCM state safely across compatible stage handoffs; stop device DMA before replacing executable code.
- Prevent proxy requests from interleaving across transports; recover a disconnected partial request without replaying a mutating command.
- Discover both known CDC identities and eligible Apple USB-device NCM interfaces on the Mac. Validate candidates with a protocol handshake, close failed transports, and report the actual selected path.
- Test lifecycle transitions and host discovery with native fixtures, run the repository's required native suites and QEMU, then chainload and verify physical packet/memory round trips.
- Physical qualification must cover each usable Mac USB-C port against each A16 USB-C port for the direct cable and the current Thunderbolt-dock route, including reconnects. Software tests do not establish those physical combinations.
- Native Thunderbolt networking is outside this transport design; the dock route uses the USB data path carried through the Thunderbolt dock.

## Verified implementation, 2026-09-19

- Both controllers now have independent role discovery, DMA, transport state,
  reconnect counters and diagnostics. A configured CDC connection survives a
  closed reader; a physically detached link is debounced and rediscovered.
- Proxy framing pins each request to its originating controller, releases
  abandoned prefixes (including all-zero input), and keeps checksum negotiation
  per controller. Client discovery resets negotiation on every fresh connection.
- The reader discovers both CDC serial identities and all active interfaces
  belonging to Apple's USB-device NCM driver. It handshakes candidates and closes
  failed endpoints. `--device udp://enN` selects a specific direct connection.
  Mutating requests are not replayed automatically after disconnects.
- Raw `a16xhci.py` takeover is refused for payload-managed controllers and active
  device controllers, preventing accidental destruction of the working link.
- New EFI and all four stage variants build with warnings as errors in
  `build/uefi-portable`. Native tests pass: 29 port lifecycle assertions,
  8 reader/discovery cases, 31 proxy checks, 42 xHCI/NCM checks, 48 NCM packet
  checks, four automatic boot choices, USB1 fallback guards, and controller
  identity/EP0 checks.
- `test-uefi.py --proxy --build-dir build/uefi-portable` passes, including guarded
  and unguarded exception handling and two successive alternate-slot chainloads.
- Hardware stage generation 1 at `0xb0000000` responds at EL2. USB0 automatically
  becomes host/NCM (`udp://en4`); USB1 becomes device/CDC
  (`/dev/cu.usbmodemA16_Q1N1_EL2B1`). Both report connected with no port errors.
- Each physical path passed a 262,144-byte write/read comparison; both returned
  SHA-256 `2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9`.
  200 alternating NOPs and 40 interleaved 4-KiB reads passed with different
  checksum negotiation on the two connections.
- With the dock unplugged, auto-discovery selected the direct NCM cable and
  passed another 256-KiB comparison and 100 NOPs.

## Failures found by physical movement and handoff tests

1. Moving the direct cable to the other A16 port exposed a real device-mode
   disconnect case: `DSTS=0x8e4db0` reports U3/Suspend, while configured remains
   latched. Looking only for Disconnected is insufficient when session-valid is
   forced. The manager now permits short suspends but re-probes a configured
   port after eight seconds of sustained U3, followed by disconnect debounce.
   A genuinely sleeping host may re-enumerate after this interval. The native
   model covers short-suspend preservation and sustained-suspend recovery.
2. Changing the shared manager structure during development made a subsequent
   stage reset an active NCM host controller. This triggered the Mac's previously
   documented Full-speed/no-address-response failure. A physical Mac-end replug
   restored the link and a 256-KiB transfer passed. Stage images now publish a
   USB state contract; the reader refuses an incompatible live-NCM handoff
   before upload. Compatible NCM state is adopted without resetting the host.
3. Device halt can time out with pending control work. The handoff guard refused
   to reuse DMA, leaving the old generation running. Device stop now falls back
   to a bounded local controller soft reset; the guard still checks halted
   state. Native stop tests cover graceful stop, reset recovery and a stuck reset.
4. xHCI port reset accepted old PRC/PED status. It now clears prior completion,
   requires a fresh completion with PR clear and PED set, and rejects timeout
   even if PED was previously latched. Three regression assertions cover this.
5. The QEMU test accepted `--build-dir` for EFI but still chainloaded stages
   from `build/uefi`. Its stage selection now uses the same isolated directory.
   The corrected test passes using the new EFI and both new QEMU stages.

After those fixes, a compatible hardware chainload from generation 1 to 2
at `0xb0800000` succeeded with both cables still attached. CDC and direct NCM
each answered generation 2 and passed 4-KiB memory round trips without a replug.

## Direct-cable port movement results

The corrected stage stayed at generation 2 through the subsequent port moves.
Every completed pairing below passed a 256-KiB memory write/read comparison and
100 NOPs. Interface names identify the observed Mac port in this session, not
names that the reader relies on.

| Mac port observed as | A16 USB0 | A16 USB1 |
| --- | --- | --- |
| `en4` | PASS | PASS |
| `en5` | PASS | PASS |
| `en6` | PASS | PASS |

The dock remained disconnected for these direct-only tests. Moving the direct
cable from USB0 to USB1 and back recovered automatically after the suspend fix.
All six direct-cable pairings are complete.

The second Mac port also passed the dock-only route through both A16 ports,
selecting `A16-Q1N1-EL2` on USB0 and `A16-Q1N1-EL2B` on USB1. When the dock's
Mac connection moved to the third port, no dock/USB hub appeared in the Mac's
USB tree and no proxy endpoint appeared. That dock pairing is unverified;
the direct cable had already passed on the same Mac port. The user asked to
end physical testing. The first Mac port's dock pairings were not completed.

The running generation-2 stage's 57,364-byte `.text` matches the isolated
`hardware-alt.elf` build exactly: SHA-256
`822f4b4b1c22ceb33d38b01e4ff01b4f5e180b329e31447dbf036562643525a7`.
The installed EFI artifact is 678,912 bytes, SHA-256
`00ae6161ab2a588f40a0f407238b87103265c345ecb79950851db6d9892d3383`.

Run `python3 tools/q1n1proxy.py info` to display the selected endpoint and both
port states. Current payloads serve both transports continuously; no NCM
`serve` handoff or `--xhci` switch is required.

Run `python3 tools/test-q1n1-ports.py` for the new lifecycle, device-halt,
boot-choice and reader regressions, alongside the existing protocol/xHCI/NCM
and QEMU suites.

## Persistent installation, 2026-09-19

Windows SSH confirmed administrator access, model UX3607OA, BIOS 312, the
expected Samsung boot disk, and the existing 450-MiB ESP on disk 0, partition
12. The guarded installer completed at `2026-09-19T21:09:17.7057071Z`.

- Installed `\EFI\q1n1\q1n1-usb.efi`: SHA-256
  `00ae6161ab2a588f40a0f407238b87103265c345ecb79950851db6d9892d3383`.
- Previous payload backed up as `q1n1-usb-e0aee7e7c736.efi` in the Windows
  bring-up directory; backup SHA-256
  `e0aee7e7c736831c48c039f68787c99deac6562cab9f28ab4fe5f00711d1215f`.
- `\startup.nsh` SHA-256:
  `2b2f169f5afdace01bd0efe68e0e574a928ad55e51f9ae42805d22ce83260a00`.
- The known-good `\EFI\q1n1\q1n1.efi`, Windows boot manager, and firmware
  boot order were verified unchanged. Windows remains first in normal boot
  order. The prior installation records were also backed up.
- After the installer unmounted the ESP, a separate mount and read verified
  the new payload, startup script, old payload backup, and preserved loaders.
- The verified boot helper armed the one-time q1n1 entry and requested a
  Windows restart. No q1n1 USB endpoint appeared within the initial 120-second
  wait. This does not yet establish the screen state or a payload failure;
  successful execution of the installed image has not been confirmed.

## Qualification still required

The successful simultaneous and direct-only tests do not prove every physical
Mac/A16 dock pairing or booting the new EFI from the ESP. Remaining dock
pairings were deferred at the user's request. The persistent installation is
verified, but a fresh boot of that EFI still needs runtime confirmation.
Keep successful RAM-stage qualification separate from the installed image's
pending boot qualification.

## Build isolation

The known-good `build/uefi` binaries were preserved throughout this work.
New firmware and stages were built in `build/uefi-portable`; the corrected
QEMU runner uses that directory for both the EFI and its chainloaded stages.
