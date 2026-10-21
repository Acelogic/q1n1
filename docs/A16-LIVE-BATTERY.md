# Live battery in the EL2 proxy, 2026-09-24

**Status:** implemented, host-tested against a simulated SoCCP (25,009
checks), checked in QEMU, and **working on the A16 (2026-09-25, stage
generations 3 and 4, EL2 proxy over udp://en5)**. Logs are in
`build/a16-battery-live-20260925/`:

| Step | Command | Result on the A16 |
|---|---|---|
| Survey | `a16ctl battery --survey`, run twice (`survey1.txt`, `survey2.txt`) | Identical both times. SMEM `0xffe00000+0x200000` is Device (MAIR attr 0x00), 19 partitions, exactly one APPS↔SoCCP partition (hosts 0/19 at `0x11d000`), items 478/479/480 present. All four descriptor words 0 and stable. SMP2P 617 is `0x6` (ready, not fatal). IPCC v`0x20c02` is Device. |
| First live read | `a16ctl --verbose battery --ucsi-version` (`live1.txt`) | **UCSI VERSION `0x0210`**, 8.8 ms. Link restarted the Linux way; VERSION acked both ways; channel open, lcid 1 / rcid 6. 2 requests, 2 replies of 1040 bytes, owner `0x8011`/type 1/opcode `0x90`, error 0. |
| Battery | `a16ctl battery` (`live2.txt`) | `88% discharging on battery, 8191 mW out, ~7h33 left, 61915/70016 mWh, 12720 mV, 5 cycles`. |
| Tick servicing | `a16ctl --verbose battery` after 60 s (`live3-tick.txt`) | Serviced 3,728 times, one router notification drained, 14 requests / 14 replies, 0 protocol errors. The reading moved (61,890 mWh). |
| Release | `a16ctl --verbose battery --release` (`live4-release.txt`) | `RELEASED`: DO_CLEANUP sent and **all four words 0**, the state the firmware leaves. |
| Restart after release | `a16ctl --verbose battery` (`live5-restart.txt`) | Automatic restart from all-zero; new rcid `0xa`; reading continues. |
| Chainload | `q1n1proxy.py chainload build/uefi/q1n1-stage.bin`, then `battery` (`live6-chainload.txt`) | Generation 4 continued the session (`continued` +2, no new restart); 6/6 replies. |

Not yet checked on the A16: the automatic release before a `P_VECTOR` to a
non-q1n1 image, and a Linux boot after a q1n1 release (step 6 of the plan).
The charger was not connected during these runs, so the `_PSR` AC=1 path
has only been checked against the simulator.

**Revised the same day** after a review found four problems in the first
version, all fixed here: it read one word from every page of SMEM and the
headers of other processors' partitions; it restarted a link it did not know
was clean; it left the channel open with nobody reading; and it could be told
to write through a cacheable mapping. It also misdescribed the firmware's
ExitBootServices callback (see "What the firmware does at ExitBootServices").

**Revised again** after a second review: the firmware's callback does zero
APPS's two counters as well (it waits, with no timeout, until the SoCCP has
zeroed its two and then zeroes ours), so the state q1n1 should find is all
four 0 and anything else is a reason to stop; the doorbell now has a store
barrier in front of it; a survey can no longer disturb a live session; linux
and drain restarts over a SoCCP that never cleaned up need `--force`; and the
release no longer mistakes a head that wrapped to 0 for the SoCCP's reset.

## The mechanism, in plain words

The battery numbers live in the PMIC, which the SoC's small power processor
(the **SoCCP**) owns. Windows and the firmware ask the SoCCP for them by
sending a short message over **GLINK**, Qualcomm's mailbox protocol.

GLINK between the main CPU (APPS) and the SoCCP is three blocks in shared
memory (**SMEM**): a 32-byte descriptor with four counters, a ring buffer
from APPS to the SoCCP and a ring buffer back. To send, APPS copies a message
into its ring, moves its "head" counter forward, and rings a doorbell (a
write to the **IPCC** mailbox). The SoCCP copies the message out, moves its
"tail" counter, and answers the same way in the other ring.

Before ExitBootServices the firmware's `PmicGlinkDxe` does this. After it,
that driver is gone, so q1n1 does it itself:

1. find SMEM and the APPS<->SoCCP partition in it, reading only the pages
   Linux reads (the first page, the last page, and our partition);
2. find the three GLINK items and check the SoCCP says it is alive and ready;
3. check the doorbell is the one the firmware used and is mapped Device, and
   that no page we touch is mapped cacheable (both sides share cache lines of
   the counters);
4. if the link is not already ours, restart it -- by itself only when all
   four counters read 0 -- set our two counters, send `VERSION`, open the
   channel `PMIC_RTR_SOCCP_APPS`, offer the SoCCP receive buffers ("intents");
5. send the firmware's 24-byte register-read request for `_BST`, `_BIX`,
   `_PSR`, each twice, and decode them with `battery.c`;
6. keep reading what the SoCCP sends between calls (every 20 ms, from the
   status tick), so it is never left waiting on APPS;
7. before handing the machine to anything that is not q1n1, send the same
   "clean up after APPS" message the firmware sends at ExitBootServices, and
   leave all four counters at 0.

A chainloaded stage copies the previous stage's session and continues it
without a new handshake, after checking nobody moved our counters; its first
tick picks it up, so the channel is read across the chainload too.

## Use

```shell
python3 tools/a16ctl.py battery --survey          # read-only: where everything is
python3 tools/a16ctl.py battery --ucsi-version    # transport check: expects 0x0210
python3 tools/a16ctl.py battery                   # BATTERY 75% charging on AC, ...
python3 tools/a16ctl.py --verbose battery         # plus every diagnostic field
python3 tools/a16ctl.py battery --refresh on      # panel line refreshed every 30 s
python3 tools/a16ctl.py battery --release         # hand the link back as the firmware does
python3 tools/a16ctl.py battery --mode resync     # restart explicitly (counters not all 0; see below)
```

In the boot window `a16ctl battery` still uses the firmware path; `--live`
forces the EL2 path. The EL2-only options (`--survey`, `--release`, `--mode`,
`--force`, `--ucsi-version`, `--host`, ...) are refused while a boot window is
up instead of being sent to it, where they would silently become the
firmware's reading. Over the proxy the entry is `bootinfo.battery_call`
(P_CALL: flags, timeout ms, SMEM host, SMEM base, SMEM size); the record is
`bootinfo.battery_result` (`struct q1n1_live_battery`, parsed by
`q1n1proxy.Proxy.live_battery`). The panel shows the last result on the row
under the USB lines.

## What is proven, and where

From the BIOS 312 firmware volume (modules extracted locally):

| Fact | Evidence |
|---|---|
| SMEM target info: u64 pointer at physical `0x01FD4000` to `{u32 0x49494953 "SIII", u32 size, u64 base, u16 max_items}` | SmemDxe RVA `0x2830-0x2844` (pointer), `0x2854-0x2860` (magic), `0x2858`/`0x2878`/`0x2898` (fields) |
| `0x01F00000` in SmemDxe is **not** SMEM: it is the TCSR mutex block, used as `+0x42000`/`+0x43000` | SmemDxe `.data 0x4fbc` table added to `0x1f00000` at RVA `0x27c4-0x2810` (corrects the earlier reading) |
| SMEM header: initialized word at `+0xC0` must be 1; heap version in `version[7] >> 16` (`+0x5C`) | SmemDxe RVA `0x28a8`, `0x28b4-0x28bc` |
| IPCC APSS frame `0x03E04000`: table `{protocol 0, client 8, base 0x03E00000}`, frame = base + index of client 8 (4) << 12 | IPCCDxe `.data 0x23b0`, client table `.data 0x2010`, RVA `0x16e8-0x16f8` |
| Doorbell: frame `+0x0C` written as `client << 16 \| signal`; version register at frame `+0` | IPCCDxe RVA `0x1c10-0x1c50`, `0x1700` |
| The GLINK EBS callback is registered only when devcfg `/sw/glink/uefi-feature-config` has `exitcb-defined`; it calls `0x3358(edge, "SMEM")` for `spss`, `lpass`, `mpss`, `soccp`, `oob_tee`, `oob_ns` | GlinkDxe RVA `0x1440-0x14bc`; `0x2a08-0x2a6c` (five calls through the stub `0x796c` = `mov x1, x19; b 0x3358`, the last a tail call); names at `.rdata 0x7cef/0x7d04/0x7cf4/0x7d28/0x7e18/0x7d18`, `"SMEM"` at `0x8302` |
| `0x3358` is the glink_ssr **DO_CLEANUP**, not a counter reset: it opens channel `"glink_ssr"` on that edge, waits for the connected state, and sends one 48-byte message `{version 0, command 0, seq 0, name_len 5, "apss"}` | name: 8 bytes at `.rdata 0x7bd8` (`glink_ss`) + `"r\0"` stored at `0x3388/0x33ac`; open config and `glink_open` at `0x33b4-0x3468`; wait on the state word `0x3470-0x3488` (callback `0x334c` stores the event); message zeroed at `0x33a8/0x340c/0x3410`, `name_len = 5` and `"apss"` (`0x73737061`) stored at `0x3418-0x3424`; `glink_tx(handle, 0, msg, 0x30, 0xb)` at `0x34e4-0x34f8` (byte `[xprt+0xc0]` = 1 is set just before, `0x34dc`; purpose unknown). Layout = downstream `struct do_cleanup_msg`, `GLINK_SSR_DO_CLEANUP` = 0 (`glink_private.h:680-682, 766-772`) |
| After the send, `0x3358` calls the core's `link_down` on each transport of the edge, then **waits with no timeout until all four GLINK words are 0, zeroing APPS's two itself** | `link_down` at `0x352c-0x3578` (`[xprt+0xa8]+8`, ops table `.data 0x9140`/`0x91c8`, slot 1 = `0x4da4`); unbounded loop `0x357c-0x35e8` until transport op `+0x98` returns 1; that op is `0x6570` (stored at `0x1914`), downstream's `wait_link_down`: returns 1 only when word 0 (`[tx_desc+0]`) and word 3 (`[rx_desc+4]`) are 0, then calls `0x64dc`, which stores 0 to word 2 (`[rx_desc]`, `0x6544`) and word 1 (`[tx_desc+4]`, `0x6550`). Word mapping: `0x1a08-0x1a28`, the flag that selects TX FIFO 479 selects `tx_desc = desc+0`, `rx_desc = desc+8`. The callback skips an edge at the gates `0x3428-0x345c` |
| Register read request: `0x8011, 1, 0x90, 0, register, length`; 1040-byte reply (12 header + 1024 data + u32 error) | PmicGlinkDxe RVA `0x263c`, `0x381c`, cache `0x4550`, error `0x4950` |

From public sources (Linux `165768bb7026`): SMEM at `0xffe00000` size
`0x200000`, SoCCP SMEM host 19, IPCC client 46 signal 0 (`0x002E0000`), GLINK
items 478/479/480 and the descriptor word order, the command set and framing,
SMP2P 617 `slave-kernel` bit 0 fatal / bit 1 ready. A third party ran Linux's
GLINK registration on an A16 after UEFI and got battery values.

## What the firmware does at ExitBootServices

The first version of this document guessed that the callback "zeroes APPS's
two counters". That is wrong. `0x3358` tells the SoCCP that **APPS is going
away** (the `glink_ssr` protocol's DO_CLEANUP for `"apss"`), and a receiver
built like downstream's SMEM transport answers by zeroing **its own** two
counters -- our words 0 and 3 -- and marking the link down
(`glink_smem_native_xprt.c:1456-1486`); it then treats the link as down until
all four words read 0 (`wait_link_down`, `:1495-1508`). The firmware then
does its own half of the same thing: after `link_down` it loops, with no
timeout, on the transport's `wait_link_down` (`0x6570`), which returns only
once words 0 and 3 are 0 and at that point zeroes words 1 and 2 (`0x64dc`).
So if the callback ran for the soccp edge and ExitBootServices returned,
**all four words are 0** -- and since a SoCCP that never zeroed its words
would hang ExitBootServices for ever, reaching q1n1 at all means it did.

What the survey can show, and what it means:

- all four 0: the expected state; q1n1's restart (Linux's way) needs no flag.
- word 0 or 3 not 0: the callback did not run on the soccp edge
  (`exitcb-defined` unset, or the edge skipped at `0x3428-0x345c`). Stop.
- words 0 and 3 are 0, 1 or 2 not: a completed callback cannot leave this.
  Stop and find out what wrote them; do not restart over it.

This is why Linux's restart (our words to 0, then `VERSION`) works after
UEFI: the SoCCP had already been told APPS was gone. It is also why nothing
may be left behind by q1n1: without the same message a later OS finds the
link up, channel 1 still open, and a downstream-style core ignores a second
`VERSION` ("Negotiation already complete", `glink.c:4114`).

## How a restart treats the counters

Three modes (`glink-smem.h`):

- **linux** -- our head and tail to 0, exactly what Linux does. The SoCCP then
  reads its old tail to the end of our ring before the new frames; if the
  ring is zero there, a zero frame is `VERSION 0`, which GLINK ignores (a
  downstream core may answer each with `VERSION_ACK 0`; Linux ignores those,
  and so does q1n1).
- **resync** -- our head to the SoCCP's tail, our tail to its head. Both rings
  look empty to both sides whether or not the SoCCP resets; only works if the
  SoCCP accepts a second `VERSION` on a link it considers up (unproven).
- **drain** -- as linux, but skip anything left unread.

**Policy.** A call continues its session when it can. Otherwise q1n1 restarts
by itself **only** when all four words read 0 in two samples 1 ms apart (the
cleaned-up state), and then only in linux mode. Any other state needs exactly
one explicit `--mode`; without it the call refuses with status -15 before it
writes anything. `linux` and `drain` move our head to 0; with word 0 (the
SoCCP's tail in our ring) or word 3 (its head in its own) not 0 -- a SoCCP
that never cleaned up -- it would read our ring from its old tail, and
`linux` would also parse its old frames. Both refuse there with -6 unless
`--force` is given too; `resync` is not affected. Counters that move during
either sample refuse a restart too (-23). The 30 s refresh and the resume of
a chainloaded session never restart (-16).

One session at a time: while a session is up, a call naming another SMEM
base, size or SoCCP host refuses with -7 before binding (binding there would
drop the session the SoCCP still sees); release first. A survey never binds
at all -- it reads the items through a separate view -- so a survey of any
host or base leaves a live session, its ids and its half-received messages
alone.

Every store to our counters is followed by a store barrier (`dsb st`) before
the doorbell, as Linux's `writel()` orders it; without it the SoCCP could take
the interrupt, read the old counter and never be rung for the new one.

After every call q1n1 zeroes the free part of its own ring (also after a
failure, when word 1 is still ours), so a later Linux-style restart reads
zeros.

## Releasing the link

`--release` (P_CALL flag `RELEASE`) does what `0x3358` does, then finishes the
job downstream's local `ssr()` would: it opens `glink_ssr` (lcid 2; if the
SoCCP opened it first, the open was recorded and is acknowledged now, as
Linux does for a channel no driver has claimed), sends the firmware's 48 bytes,
waits until the SoCCP has zeroed words 0 and 3 (a zero tail counts only after
it has moved since the message went out; if it was already 0, q1n1 first moves
its own head off 0 with one `VERSION 0` frame), then zeroes its whole ring and
words 1 and 2. A zero tail also counts only while q1n1's own head is not 0: a
SoCCP that merely read everything up to a head that wrapped to exactly 0
looks the same as one that reset. In that case q1n1 writes one more
`VERSION 0` frame (head 8) and waits 50 ms: a live SoCCP reads it and the
wait goes on; one that reset is waiting for four zero words and reads
nothing. All four words are then 0, which is the state the next q1n1
call, Linux, or the SoCCP's own `wait_link_down` expect. If the SoCCP never
opens `glink_ssr`, nothing but the OPEN was sent and the session stays up. If
it reads the message but never resets, q1n1 leaves its words alone (zeroing
them under a live tail would make it replay), marks the session failed and
reports -26.

`main.c` releases automatically before a `P_VECTOR` whose argument is not
q1n1's bootinfo (a chainloaded q1n1 stage always receives bootinfo and
continues the session instead). A reboot does not release: the SoCCP is
reset and reloaded by the firmware with the rest of the SoC (not verified on
this machine; release first if in doubt).

## Nobody left waiting

While a session is up the SoCCP may send at any time: battery-manager and
UCSI notifications (owners `0x800a`/`0x800b`) arrive in our four receive
buffers, and when those are used up it asks for more. The status tick
services the session every 20 ms (`q1n1_glink_service`): at most 16 frames,
each handled only while our ring has room for every reply it can need, so the
tick never waits and never restarts. Buffers are returned after each
notification and every request for more is answered (declined when all four
are in use). The alternative -- closing the channel or releasing after every
call -- would put a full handshake and a DO_CLEANUP on every read, each an
unproven SoCCP transition; a serviced channel is what Linux keeps.

## What is read, and refusals

SMEM reads are a subset of Linux's: the first 4 KiB (header), the last 4 KiB
(partition table), and the APPS<->SoCCP partition, each page proved readable
by a guarded load first. Other partitions are listed from the table alone;
their headers are never read (the firmware ships an access-control driver,
`SmemAcDxe`, and a stray read into a protected partition may not come back as
a catchable abort). The global partition is not needed and not read.

Every first touch (target-info word, those pages, the IPCC frame, the page
tables) is a guarded read: a wrong address is a status, not a crash. Writes
are not covered that way: a posted write to Device memory can fault later as
an SError. The call refuses, before any write, when: SMEM's header is wrong;
the partition is missing or two claim the host; an item is missing,
mis-sized or its list is corrupt; the SoCCP's SMP2P record is missing,
malformed, fatal, or not ready (bit 1); the IPCC version is not 2.x; the
header page, the descriptor page or any page of either FIFO is mapped
cacheable (no override); the doorbell page is not Device (no override); a
page attribute cannot be read (`--attr-unknown-ok` overrides that case only);
the counters move; an index is out of range; a restart is needed and the
counters are not all 0 and no mode was given. The first write is our own
counter rewritten with its current value, guarded. During the protocol, an
unknown command or impossible frame fails the session. Every wait has a
deadline (default 1.5 s, at most 10 s), and the call refuses when the counter
frequency is 0.

Replies carry no register id. A timed-out request is counted as owed (also
across a chainload) and the next reply is discarded against it; a reply is
taken only after its request is in the ring; every value is read twice;
`battery.c` rejects implausible values.

## Hardware checklist (read-only first)

The step-by-step plan with every address, expected values and abort
criteria is kept with the patch (`HARDWARE-PLAN.md`); in short:

1. `a16ctl battery --survey` -- reads the target-info pointer, the SMEM header
   and table pages, the SoCCP partition, the IPCC version register and the
   page tables; writes nothing.
2. Check: SMEM at `0xffe00000`, heap version 11/12, a hosts 0/19 partition,
   items 478 (32 bytes)/479/480, SMP2P `slave-kernel` = 2 (ready, not fatal),
   IPCC `0x0002xxxx`, every attribute non-cacheable and the doorbell Device,
   and the four counters (repeat the survey: they must not move).
3. First write: `a16ctl battery --ucsi-version`, only if the counters were
   all 0 (the firmware's callback leaves them so). Any other state: stop.
   Expect `0x0210`.
4. `a16ctl battery`, compared with the boot window's reading.
5. `a16ctl battery --release`: all four counters 0 afterwards; then
   `a16ctl battery` again restarts by itself.
6. Chainload a stage: its first tick resumes the session (continued +1).
7. Boot Linux after a release and check its battery driver binds.

## Checks

```shell
clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror -DQ1N1_GLINK_HOST_TEST \
  -Iplatform/uefi tools/test-q1n1-live-battery.c platform/uefi/smem.c platform/uefi/glink-smem.c \
  platform/uefi/pmic-router.c platform/uefi/battery.c -o build/test-q1n1-live-battery \
  && build/test-q1n1-live-battery
```

The harness builds an SMEM image (header, TOC with an ADSP, an ADSP<->SoCCP
and optionally a global partition, the SoCCP partition with 479 on the cached
list, SMP2P, target info) and a simulated SoCCP with two channels that
answers the GLINK handshake and PMIC reads, sends notifications, and acts on
DO_CLEANUP as downstream's transport does. It covers the parser's refusals,
the page-table walk, the happy path, a session continued across calls and
across a simulated chainload, both FIFOs wrapping 40 times with replies split
into `TX_DATA_CONT` chunks, frames straddling the ring end at every offset
(and a release from each), the restart policy (zero-word gate, one non-zero
word, moving words, conflicting modes), `VERSION_ACK 0` storms, a silent
remote, a remote that ignores `VERSION`, late, lost, unsolicited and
too-early replies (also across a chainload), bad replies, a remote that
closes or reopens the channel, intent requests in both directions, oversized
intents, the release (the firmware's exact 48 bytes, early and late
`glink_ssr` opens, no intent, a SoCCP that never opens it or never resets,
the handoff path, an adopted session, and every start position of both rings
with a SoCCP that never resets -- none may count as released -- or one that
does), a store barrier between every counter store and the next doorbell
(the simulator checks words 1 and 2 at each doorbell against their values at
the last barrier), surveys of another host leaving a live session untouched
and calls for another host refused while it is up, `linux`/`drain` refused
over non-zero SoCCP words without `--force`, the tick draining notifications,
never blocking and resuming after a chainload, the refresh never restarting,
per-page attribute refusals, and every refusal above with a check that
nothing was written. One scenario makes every SMEM page outside the header,
table and SoCCP partition inaccessible (`mprotect`) and runs a survey, a
read, a release and a restart: a single stray load would crash it. `tools/test-uefi.py --proxy` adds the stage's own code on
QEMU: guarded refusals at the default address, and a synthetic SMEM image in
the heap parsed through the real page tables up to the (absent) IPCC frame.
