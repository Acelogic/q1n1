# A16 BIOS312 UCSI register transport

The legacy EFI UCSI buffer read (`0x800b/0x11`, 48 bytes) returned zeros in
the previous physical tests. A separate register transport is implemented
in both BIOS312 and the currently installed Windows PMIC driver.

## Exact target evidence

The captured DSDT (`ea4dbd66cad51e2eff837e4f97d70a8216d2f66c10792300a4816add360e98d6`)
describes `_SB.UCSI` with a 528-byte region and UCSI 2.1 support. Its methods
access `_SB.PMGK` GenericSerialBus registers: VERSION at `0x20100`, CCI at
`0x20104`, CONTROL at `0x20108`, and MESSAGE_IN at `0x20200`. These are
register identifiers in the PMIC transport, not physical MMIO addresses.
Windows's `_DSM` data-role correction policy is disabled by function 4
returning one; changing the USB controller's `RoleSwitchMode` is therefore
not established as the fix for the EFI serial connection.

The running Windows `qcpmicglink8480.sys` SHA-256 is
`2c7161093c74a830569cf6897a141cb5a0dffa2e4a2f155d2b123413e4f9a95e`.
Its dispatcher at RVA `0xd110` maps IOCTL `0x80332050` to handler `0xce88`:
8 input bytes (register, length), a 1024-byte output buffer, and returned
length equal to the requested length. The read sender at `0x98f0` constructs
GLINK owner `0x8011`, type 1, opcode `0x90`, and a 24-byte request.

`tools/read-a16-ucsi-windows.ps1` fingerprints that driver and allows only
four-byte VERSION and CCI reads. On the A16, it returned:

```text
CapturedUtc: 2026-09-17T08:09:16.0846456Z
VERSION:     0x00000210
CCI:         0x20000000
```

No UCSI command, role request, reset, or registry write was sent. This is
a physical Windows read. Private copies, disassembly,
decompilation and the original JSON are under `build/private/a16-windows-usbc`.
`UCS0.bin` in that directory is an AeoB driver resource, not an AML table.

## Matching firmware interface

The already captured BIOS312 `PmicGlinkDxe.efi` contains **29 callback pointers**
after its `0x1000c` revision at RVA `0x40b0`. Earlier probes checked only the
first 24. The extended read callback at slot 24 / pointer RVA `0x4178` / code
RVA `0x263c` has this ABI:

```c
EFI_STATUS read(uint32_t register_id, void *caller_buffer, uint32_t bytes);
```

It sends the same 24-byte `0x8011/0x90` request, checks the cached remote
error, and copies the requested number of bytes from cache RVA `0x4550`.
The receive handler requires exactly 1040 bytes: a 12-byte GLINK header,
1024 data bytes, then a four-byte remote error stored at RVA `0x4950`.

The firmware's generic receive-completion flag can also be set by unrelated
messages. Its cached remote status check is useful but does not prove a
fresh matching response. The next probe reports that limitation and sends
no UCSI commands. No role-write path is enabled by a successful read.

## EFI test

`platform/uefi/usb-ucsi-reg-probe.c` verifies the complete 29-callback layout,
exact firmware code fingerprint, protocol object, boot-services pointer and
link-ready flag. It reads VERSION and, only if that equals the Windows
reference `0x0210`, CCI. It uses adjacent buffer guards and a 30-second
firmware watchdog, and returns to the shell.

```sh
build/firmware-tools/bin/python tools/audit-a16-ucsi-registers.py
make -f platform/uefi/Makefile OUT=build/uefi-usb-ucsi-reg-probe usb-ucsi-reg-probe
clang -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
  -Iplatform/uefi tools/test-a16-ucsi-reg.c -o build/test-a16-ucsi-reg
build/test-a16-ucsi-reg build/private/a16-typec-drivers/PmicGlinkDxe.mapped
python3 tools/test-uefi.py --usb-ucsi-reg-probe --build-dir build/uefi-usb-ucsi-reg-probe
```

Validation: captured ABI audit passed; 24 host cases passed under ASan/UBSan;
QEMU EL2 returned to the shell after refusing unsupported firmware. QEMU
does not exercise the Qualcomm transport.

Installed at `\EFI\q1n1\q1n1-usb-ucsi-reg-probe.efi` (6656 bytes), SHA-256
`1b555e790e75164a35304a393aa0ad6cab64a57aa79ef20b633978ec2dfd65aa`.
Installer confirmed the known-good q1n1 payload, Windows boot manager and
normal boot order were preserved. The installation manifest now has 14 files.

The one-time run uses a temporary `\startup.nsh`, captures output at
`\EFI\q1n1\ucsi-register-once.log`, then invokes Windows Boot Manager.
`tools/run-a16-ucsi-register-once.ps1` records the temporary script's hash for
exact cleanup. The physical EFI run returned VERSION `0x0210` and CCI `0`,
both with EFI status and cached remote status zero. Windows returned at
`2026-09-17T08:19:40.3062600Z`. The log was collected and the exact temporary
startup script removed at `08:21:30Z`; evidence is in
`build/a16-install/ucsi-register-once.log` and `ucsi-register-once-collected.json`.

## Connector query experiment

`usb-ucsi-status.c` reuses the proven read preflight and validates the same full
driver identity before using callback slot 25 / RVA `0x2710`:

```c
EFI_STATUS write(uint32_t register_id, const void *caller_buffer, uint32_t bytes);
```

The exact disassembly sends owner `0x8011`, opcode `0x91`, a 1048-byte packet,
register and length at offsets 16/20, and payload at offset 24. It checks
transport status and cached remote error. The callback copies the caller's
data into the request; it does not retain the stack pointer.

The new app restricts writes to eight exact CONTROL values: command-complete
notification enable/disable, command acknowledgement, GET_CAPABILITY and
GET_CONNECTOR_CAPABILITY/STATUS for connectors 1 and 2. There is no role,
power, mux, cancel or reset command path. It requires initial CCI zero,
polls completion/acknowledgement with bounded waits, validates response
lengths and buffer guards, and logs raw replies before decoding. An
unexpected/busy/error state stops the experiment without recovery commands.
The firmware cache limitation still applies.

The sequence follows the optional-reset initialization flow in
[UCSI specification section 4.3](https://www.intel.com/content/dam/www/public/us/en/documents/technical-specifications/usb-type-c-ucsi-spec.pdf).
Field layouts were cross-checked with the
[Linux UCSI definitions](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/ucsi/ucsi.h).

39 combined host cases passed under ASan/UBSan. QEMU EL2 refused unsupported
firmware, sent no commands, and returned to the shell. EFI image SHA-256:
`561a0fa7a6ce2e8b93f6d3293aff5a7b4a70c3dec22a615806feab43a2acb89b`.
Use `run-a16-ucsi-register-once.ps1 -Probe status` and
`collect-a16-ucsi-register-once.ps1 -Probe status` for the logged one-time run.
The first physical connector-query run completed notification enable, its
acknowledgement, GET_CAPABILITY and its acknowledgement. Its 16-byte reply
was `00 10 00 80 04 1d 34 00 00 00 f0 00 03 00 00 00`: the first word equals
the immediately preceding CCI `0x80001000`, and the connector count byte is
four. This is not accepted as a reliable capability payload. The two-port
bound stopped the app before any connector query. Windows returned normally;
the output was collected and temporary startup removed at `08:31:26Z`.
Evidence: `build/a16-install/ucsi-status-once.log` and its collection JSON.

Version 2 matches the DSDT's fixed 64-byte `UMID` read size, instead of using
the shorter UCSI reply length as the transport read size. It takes at least
three same-register samples and requires the final two payloads to agree,
rejecting a prefix equal to the preceding CCI. These checks detect the
observed failure; they do not introduce a GLINK response transaction ID.
42 host cases (including stale and unstable message replies) and QEMU EL2
refusal passed. Version 2 image SHA-256:
`80bbcf862f3c0d197a0276c69fbac7f83cc6054a085c9d0811c8b63fd0908d71`.
The physical v2 run succeeded and returned to Windows. All three message
samples agreed on each reply. Actual GET_CAPABILITY was
`46 40 00 00 02 04 00 00 02 00 20 01 10 03 20 02`, reporting **two** connectors.
Both connector capabilities were `0x77e7`: DFP/UFP/DRP supported, accepting
swap to DFP set, accepting swap to UFP clear. Connector 1 status was
`0x402b5a46` (connected, PD, USB data, local sink and host, partner UFP),
while connector 2 was `0x400b5a46` (connected PD sink/host, no USB data flag).
These identify connector 1 as the candidate corresponding to the previously
observed Mac USB data link; no partner USB identity is supplied by UCSI.
Command notifications were disabled before returning. The log and exact
startup cleanup were collected at `2026-09-17T08:38:47Z`.
Evidence: `build/a16-install/ucsi-status-v2-once.txt` and its raw log/collection JSON.

## One standard data-role request

`usb-ucsi-device0.c --device0` repeats the exact firmware and register checks,
requires two connectors, PD and UFP/DRP capability, then requires the observed
connector-1 class (connected PD USB data, local sink/host, partner UFP).
It sends **one** CONTROL value `0x01010009`: SET_UOR, connector 1, initiate UFP,
incoming swap acceptance left disabled. No connector-2, power-role, CC-mode,
reset or cancellation request exists in the app's allowlist. Failure does
not trigger a retry. A completed rejection is acknowledged and GET_ERROR_STATUS
is queried; success must be followed by connector status confirming partner
DFP, local UFP, still connected PD USB and still sink.

The distinction between accepting an incoming swap (connector capability
bit 11) and requesting initiation (SET_UOR bit 24) comes from UCSI sections
4.5.7 and 4.5.9. Device-mode capability is advertised; whether firmware policy
permits the initiated request is an experiment, not established by that bit.

38 host cases passed under ASan/UBSan, including denial, timeout, wrong cable
class, unchanged post-request role, and malformed activation options. QEMU
refused unsupported firmware and exercised the shell branch that skips serial.
Image SHA-256: `73741c63f408bc2ed775bc3f1c75c2f6f01a141ebefa1a664fc23aa553b30b2d`.
The one-time `-Probe device0` script runs the unchanged serial-v2 image only
on confirmed UFP success; otherwise it goes directly back to Windows.

The physical run on 2026-09-17 sent exactly one SET_UOR after the same
connector-1 status `0x402b5a46` passed preflight. The write callback returned
EFI success, but command CCI was **`0xc0000000` (completed with error)**.
The app acknowledged that completion and queried GET_ERROR_STATUS, whose
CCI was `0x80001000` (16 bytes). Three message samples agreed on:

```text
00 00 2b 40 2c b1 04 13 02 00 40 00 00 00 00 00
```

The standard error flags in the low 16 bits are zero, and the remaining
bytes resemble the preceding connector-status payload. This does not
identify a policy conflict or a partner rejection. Firmware error reporting
and the transport's cached-response behavior remain unresolved; repeated
matching samples alone do not establish the response's freshness. The
command failure is established, but its detailed cause is not.

The app returned EFI_DEVICE_ERROR (`0x8000000000000007`), so the startup
script correctly skipped serial-v2 and invoked Windows Boot Manager.
Windows returned at `08:47:55Z`; collection at `08:48:50Z` found no serial
log and removed the exact temporary startup script. A subsequent live
`bcdedit /enum {fwbootmgr}` showed Windows first and no pending bootsequence.
The Mac serial watcher was stopped after collection because the serial
branch had not run; this is not an echo-test result.

Evidence: `build/a16-install/ucsi-device0-once.txt`, the original UTF-16 log,
and `ucsi-device0-once-collected.json`. The raw log SHA-256 is
`caab1187d740c41ec9f8e04db419ebfbd291e3f73cbfb30cac7f8593a0a5df68`.

The next investigation is the A16 firmware's data-role policy and error
response path. Repeating SET_UOR or the controller-only `--device0` switch
does not resolve the current evidence gap. USB enumeration and serial echo
remain unproven, and post-ExitBootServices USB is still unimplemented.

## Error-selector correction and next experiments

The subsequent source audit found a concrete diagnostic bug: v1 queried
GET_ERROR_STATUS with connector zero (`0x13`), although the failed SET_UOR
addressed connector 1. For UCSI versions newer than 1.2, the
[Linux UCSI command/error path](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/ucsi/ucsi.c)
passes the failed command's connector into GET_ERROR_STATUS. The physical
A16 reports UCSI 2.1. The correct connector-1 request is `0x10013`.
The previous zero error flags therefore do not establish absence of a
connector-specific error. The vendor-defined tail and cached transport still
cannot establish freshness on their own.

The local v2 build fixes that selector, limits the error query to connector 1,
prints its 16-bit flags separately, and explicitly reports zero flags as an
unresolved failure. The original SET_UOR and its single-request limit are
unchanged. 39 host cases passed under ASan/UBSan, including the selector
regression and a completed failure whose error flags remain zero. QEMU EL2
verified unsupported-firmware refusal and the branch that skips serial.
The corrected binary is in `build/uefi-usb-ucsi-device0-v2/` with SHA-256
`ecbfa59aeaf7b9b89258055154ac46498deaa83025b0fbb76827fc03ef93ecf1`.
It was subsequently installed under the separate name
`q1n1-usb-ucsi-device0-v2.efi`, preserving v1. The physical v2 run returned a
different initial connector status: **`0x202b5a46`**, with partner DFP instead
of the earlier UFP. All three samples agreed. The connected/PD/USB-data/sink
checks still passed, but v2's host-only initial-state guard refused before
SET_UOR. Thus this run did not exercise the corrected error query and does
not explain the previous swap error. It does show that the reported data
role can already be the desired direction on a later boot.

Windows returned at `2026-09-17T09:37:34Z`; collection at `09:38:24Z` found no
serial log and removed the exact startup script. Raw log:
`build/a16-install/ucsi-device0-v2-once.log`, SHA-256
`e435c5220b9bccc8f2f319bc2955a8567f6e6a3b5e7a0928ecdef22b70362540`.

Version 3 accepts that already-device state without sending SET_UOR. It
still requires connector 1, a connected PD USB data link, local sink, and
partner DFP; missing USB data or an unexpected source role refuses. The
original reversed-role path still permits only one request and uses the
corrected connector-specific error query. 42 role/guard host cases passed,
as did 42 shared query/guard cases and QEMU unsupported-firmware refusal.
Version 3 SHA-256:
`33e2de941dacdc7db6e8365d8566f59335950e72e95f4a70c4733341f96cc55c`.
It is installed separately as `q1n1-usb-ucsi-device0-v3.efi`. Its physical
run instead began with the old `0x402b5a46` host state. It sent one SET_UOR
and received completed-with-error `0xc0000000`. The corrected connector-1
GET_ERROR_STATUS returned the same zero flags and vendor tail as v1. Thus
the selector fix alone did not recover a meaningful rejection reason.
The already-device branch was not exercised in this run.

Windows returned at `09:42:43Z`; collection at `09:43:35Z` found no serial log
and removed the exact startup script. Raw v3 log SHA-256:
`cad8249df8676fed793f07745fa75fb253f0ace5f5b38e6ce32e414c22d91ce6`.
The read-only Mac HPM snapshot after Windows returned showed HPM0 attached
as UFP (`status 0x108284ad`, power status `0x3d`). That agrees with the latest
A16 host report, but was not simultaneous with the EFI request. HPM1 was
unattached; the previous session's HPM1-specific role tool must not be reused
for this current connection.

Version 4 changes only the reversed-role request's incoming-swap acceptance
bit: `0x03010009`, matching Linux `ucsi_dr_swap` for connector 1 to device.
It remains a single request and preserves the already-device path. This is
an experiment in live data-role policy, not a power-role request or persistent
firmware change. 42 host cases and QEMU refusal passed. Image SHA-256:
`875c5007dd7eff0c77f11bb81ef7ab45763ef4d9f65709d44885d999af7f2198`.
It is installed as `q1n1-usb-ucsi-device0-v4.efi`. The physical run sent that
exact CONTROL value once and again received CCI `0xc0000000`. Connector-1
error flags were still zero; no serial branch ran. Windows returned at
`09:50:07Z`, and collection at `09:51:12Z` removed the exact startup script.
Raw log SHA-256:
`24d438940c4398db3f56b036a8acd83eec3a30bff043f905c3b9c474f8fdd51c`.
The incoming-swap acceptance bit was therefore not sufficient to make this
request succeed on this connection. Neither this nor v3 identifies the
rejection as a Mac decision or an A16 policy conflict.

The simultaneous read-only Mac HPM capture contains 90 snapshots at roughly
two-second intervals (`09:48:40Z` onward), with all 16 register reads succeeding
in each snapshot. HPM0 never reported DFP/host in the samples. It detached
and reattached around `09:49:48Z` and `09:50:15Z`, returning to UFP each time.
Sampling cannot exclude a shorter unobserved transition or identify a PD
packet-level rejection. Evidence: `ucsi-device0-v4-mac-watch.txt` in
`build/a16-install/`. The corresponding Mac serial waiter was stopped after
the collected EFI log confirmed the serial branch had not run; this was not
an executed echo test.

## Firmware-to-OS control-state lead

The exact A16 DSDT supplies an additional initialization sequence in
`\\_SB.PMGK.LKST`: on link-up it writes a four-byte 1 to GIO register `0x100`
(`CTLD`) and a four-byte 4 to `0x180` (`HSWD`). `UCSI._STA` also writes the
control value before reading VERSION. The EFI experiments have not sent
these writes. Their effect and whether they are required for role changes
are not yet established; their names alone do not justify calling them an
ownership grant or reproducing the writes.

`read-a16-ucsi-windows.ps1 -IncludeControlState` now permits read-only GIO
queries of these two DSDT addresses in addition to VERSION/CCI. On the live
A16 at `09:53:44Z`, it returned VERSION `0x0210`, CCI `0x20000000`, register
`0x100 = 0`, and `0x180 = 3`, with successful four-byte reads and the same
verified Windows driver hash. These readbacks differ from the AML write
values, so they must not be treated as ordinary stored configuration values
without establishing their semantics. Evidence:
`build/a16-install/ucsi-windows-control-state.json`.

Next work: determine the two register semantics and compare the actual EFI
state before considering an OS-initialization experiment. The prior corrected
error-query and Linux-style role requests have both been physically tested
and remain unsuccessful. Further identical role requests add no new evidence.

Working theories and discriminating tests, in order:

1. **The diagnostic/firmware error path is incomplete.** The selector bug is
   fixed, but physical v3 and v4 still returned zero error flags. Establish
   response freshness and error-register behavior before assigning a cause
   from that payload.
2. **A16 firmware policy blocks the requested transition.** Device capability
   exists, but it does not prove this PD contract permits switching roles.
   A connector policy-conflict error would support this. Compare the exact
   firmware/Windows initialization and Linux role policy before changing one
   setting. Linux-style incoming-swap acceptance has now also been tested
   and was insufficient. The additional DSDT control initialization is the
   next lead, with register semantics still unresolved.
3. **The Mac rejects the PD swap, or the request never reaches it.** The
   previous Mac-side SWDF did not establish host mode. The simultaneous v4
   Mac capture also never sampled host mode, but does not identify who
   rejected the request. A meaningful controller error or PD packet evidence
   is needed to separate a partner rejection from local policy/transport.
4. **Connector/controller mapping or sequencing blocks enumeration after a
   role change.** If both ends confirm the intended data roles but the Mac
   still gets no descriptors, correlate connector 1 with USB0, then inspect
   USB reset/setup events and descriptor handling. Windows previously seeing
   the Mac USB device makes a charge-only cable less likely.

Each physical run should answer one of these questions and save its evidence
automatically. The serial milestone is Mac enumeration followed by random-byte
echo. Owning the controller after ExitBootServices follows that milestone.
