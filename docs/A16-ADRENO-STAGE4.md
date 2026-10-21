# Adreno X2-90, Stage 4: handing the running GPU to Snapintosh

Status, 2026-09-25: **tooling only; not run on the A16.**

Stage 4 puts the GPU that Stage 3 brings up behind Snapintosh's monitor. **Power stays with q1n1.**
The Stage 3 sequence runs to `cp-fence` unchanged. `adopt` then proves, read-only, that the GPU is
still "CP idle after cp-fence", and writes the record the monitor adopts.

The design is Snapintosh `docs/ADRENO-STAGE4-20260925.md`, and the operator's sequence is its
`docs/ADRENO-STAGE4-RUNBOOK-20260925.md`.

```shell
python3 tools/a16gpu.py --device udp://en5 adopt --record adopt.json    # read-only
```

## What `adopt` checks

It refuses (exit 2), before writing a record, unless every Stage 3 gate holds from reads:

| Step | Checks |
| --- | --- |
| AD0 | the Stage 1 gates and the arena (`prologue`, `arena_prove`) |
| AD1 | a GMU booted from this arena; OOB GPU_SET acknowledged for this boot; `GMU_PWR_CLK_STATUS` bits 0/1 clear |
| AD2 | P6/P7's `p67_ready`: cp-fence's tokens, `CP_SQE_CNTL` 1, the record's WPTR equal to `CP_RB_WPTR` and `CP_RB_RPTR`, RBBM idle, CB0 and the stream table as cp-fence left them, no SMMU fault or error interrupt; no GX-side GBIF halt (`RBBM_GBIF_HALT` 0, `RBBM_GBIF_HALT_ACK` bit 0 clear: a Snapintosh quiesce leaves it set until the next hw_init); the CX AHB timeouts armed on all five ports (the value cp-fence C3 wrote, recorded as `cx_ahb_cntl`); and room on the ring for 64 monitor fences (the ring never wraps) |
| AD3 | no framebuffer rows mapped in CB0; CB0 root entries 1..3 empty; CB0 TTBR0 equal to cp-fence's root |

## The record

The record (`schema` `q1n1-adreno-adopt-v1`) carries:

- the arena, the GIC distributor and the stage generation;
- CB0 TTBR0, TCR and SCTLR;
- the ring (PA, VA, dwords, WPTR) and memptrs;
- the monitor's fence slot, memptrs + `MONITOR_FENCE_OFF` (0x80). That is its own 64-byte line;
  q1n1's fences use +0x40, +0x50 and +0x60;
- the mirror pool;
- `tz_init_done` (host record `tz_ok == 1`): the monitor answers a guest's 0x42002801 from it
  instead of repeating the call.

## Arena layout change

`ARENA_LAYOUT['snap_smmu']` is (0x1400000, 0x603000). It holds the three level-2 and 1,536
level-3 tables the monitor uses to mirror its virtual DART into CB0 root entries 1..3 (GPU VA
1..4 GiB). Only the monitor writes it, and only while it runs. Nothing in q1n1 maps it.

## After a Snapintosh run

The monitor has:
- fenced on the ring, so WPTR moved past this tool's record;
- halted GX-side GBIF (`RBBM_GBIF_HALT` 1);
- unlinked its root entries.

Run `quiesce --write` (graceful). The next `cp-fence` hw_init clears the GX-side halt. Until then,
`adopt`, `blit` and `triangle` refuse because the ring pointers disagree.

## Tests

`python3 tools/test-a16gpu-adopt.py` runs against test-a16gpu-p6p7.py's model. It checks:
- after the modelled cp-fence, adopt writes nothing to the GPU or the arena;
- the record describes the modelled state exactly;
- a blit still runs afterwards;
- each refusal;
- the CLI.
