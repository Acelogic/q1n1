# Native NVMe provisioning from q1n1

2026-09-25: native q1n1 SSD writes were enabled so test-machine
updates no longer require Windows, and accepted that this is a disposable test
installation. The immediate operation remains the prepared Snapintosh J700
Cryptex delta. Do not reboot Windows for that operation.

## Failure analysis before implementation

- Wrong namespace or changed GPT: retain the existing ACPI/BAR/SMMU/namespace
  identity checks and validate both GPT copies and the selected partition.
- An accidental write through the old M3 reader: its default I/O allow-list
  remains read-only. Writes are enabled only on a separate writable controller
  with explicit permitted LBA ranges.
- Offset, length or transfer-size errors: validate the entire requested range
  before the first write, use zero-based NLB and split at the controller MDTS
  and the already verified bounce-buffer size. Exercise PRP-list and split
  transfers on hardware before applying the ticket.
- Dirty/stale CPU data or PRPs: clean payload, PRP list and command before the
  doorbell; use the existing CQ cache maintenance and completion checks.
- Completion without persistence: set FUA for writes, issue namespace Flush,
  read back, then restore and verify the scratch bytes.
- Hashing the wrong/stale bytes: native hashing is read-only, uses the same
  DMA window and queue state, returns queue cursors, and must match host SHA-256
  on a physical sample before a whole-container baseline is accepted.
- Executing code in an XN DMA region: the first attempt demonstrated an
  instruction permission abort before any SSD write. Recovered through the
  exception proxy without a reboot. Place code in the unused executable tail
  of the current stage slot and admit it with a guarded leaf call first;
  keep only data/state in the DMA window. Disable automatic fault skipping.
- Interrupted provisioning: save both versions of changed bytes, invalidate
  the window header first, publish the new header only after full verification,
  and retain a precise progress/failure record. No automatic blind retries.
- Unrelated disk changes: compare GPT and neighbour edges, ISC hashes, the
  complete baseline container and exact changed blocks. Reuse the already
  qualified forward/rollback package; do not regenerate its contents.
- Stranded queue or unsafe DMA memory: only one hardware client; use the proven
  Conventional-memory/SMMU selection, bounded native calls, strict SError
  capture, queue deletion and normal controller shutdown.

## Qualification plan

1. Build and inspect the read-only native hash helper and exercise driver
   refusal paths before hardware writes.
2. On the A16, compare a native hash with host hashing of the same SSD sample.
3. Save a scratch range outside the APFS image but inside the Snapintosh
   partition. Write a deterministic pattern that crosses the transfer limit,
   Flush, read back, restore the saved bytes, Flush and verify restoration.
4. Check the pinned J700 delta against the complete live M5 container, then
   apply and verify using q1n1. Preserve JSON records and original bytes.
5. Re-adopt the read-only SSD window and run the board-corrected XNU boot.

## Physical qualification, 2026-09-25

`build/full-install/j700-cryptex-probe-v3-20260925.json` is **PASS** on the A16.
The 4 MiB physical SSD sample hashes identically on the Mac and on the target:
`34767c2027ae44a3de712a46d76e01f86dc0974537bb98e976ed94551f249d64`.
The RAM helper uses code at `0xb07f0000`, beyond the running stage's allocated
end `0xb00f7d58`; its full immutable text matches the local ELF, and its guarded
leaf execution check passes. State/DMA stays at `0x8b90c60000` / `0x8b90c00000`.

The scratch range starts at LBA **1294424064**, outside the main APFS image but
inside the Snapintosh partition. A **266,240-byte (260 KiB)** pattern exercises
a 256 KiB PRP-list write plus a 4 KiB second write, both with FUA. Flush and
read-back pass, as do native hashing and the rollback-overlay hash. The saved
original bytes are restored by two more FUA writes and a second Flush, then
read back equal. GPT/neighbour edges and the original window header are
unchanged. All SError counters are zero. This qualifies command completion,
Flush/read-back and restoration; it is not a power-loss durability experiment.

The first probe failed at helper entry in XN memory, before any SSD writes.
`build/recover-nvme-hash-entry-20260925.py` bound that instruction-abort frame
and returned the never-entered call to its saved caller, recovering the proxy
without rebooting. Its record is
`build/full-install/nvme-hash-entry-recovery-20260925.json`. The second probe
refused an overbroad `image_size` reservation; the final admission uses linked
allocated sections and live text instead. All failed records are retained.

Write `0x01`, Read `0x02`, Flush `0x00`, and FUA at control bit 14 (CDW12 bit 30)
agree with the [Linux NVMe command definitions](https://github.com/torvalds/linux/blob/master/include/linux/nvme.h).
No foreign driver implementation is copied.

## Installed J700 delta: PASS

`build/full-install/j700-cryptex-apply-20260925.json` completed successfully at
2026-09-26 02:27:23 UTC (2026-09-25 local). Package SHA-256 is
`2e628fd5981b404db782fbd355f96d31532f4653ea343e33ba157609ed9a45b2`.
The complete 62,826,479,616-byte live container matched the M5 baseline before
writing and the prepared candidate after writing:

- Before: `968231c506d89a96fc0eacadd04cb5f018a74e8a1a3c40828ee9fdb830822d3f`.
- After: `75ae88a14fed0cfa9b8e2a5344de62fb5c5bbc0bb9ef1082b4257d22b946cf88`.
- Published header: `9975a1bf344b27c5b0be48d62967e71defcc4c63159caf93359fbfc1606f2a3c`.

There were 31 FUA write commands (29 APFS extents plus header invalidation and
publication), 315,392 bytes of write traffic, and three namespace Flushes.
Every extent read back equal. Full scans sustained about 169 MiB/s. ISC image,
ISC zero tail, both GPTs and neighbour edges remained unchanged. SError before,
during and unconsumed counts are all zero; queues were deleted and the controller
shut down normally. The log is `build/nvme-window-apply-20260925.log`.

The stage was then chainloaded to generation 2 at `0xb0800000`, and the fresh
board-corrected Snapintosh run was launched with an exact apply-record/header
binding. `installed-cryptex-board-300s-20260925` opens the new ticket and reaches
an unimplemented TXM16 duplicate trust-cache UUID query; its full stopped audit
passes. Cryptex acceptance and GUI remain separate milestones. This provisioning
path does not implement writes from the Snapintosh guest.

Repeatable apply/restore commands and the retained rollback package are in
[the Snapintosh SSD proposal](../../Snapintosh/docs/J700-CRYPTEX-SSD-PROPOSAL-20260925.md).
Use a new evidence filename for each operation. An already-applied package is
not a valid `apply` baseline; use read-only inspection or the qualified `restore`
mode as appropriate.
