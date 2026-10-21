# Adreno X2-90, Stage 3: GMU SMMU, GMU boot, HFI/OOB and a CP fence

Status, 2026-09-25: **tooling only; nothing here has run on the A16.** The host
tool has run against an offline model (`tools/test-a16gpu-stage3.py`), the
payload's arena and SMC code against a native harness
(`tools/test-q1n1-gpu.c`), and the payload with those changes boots and
chainloads in QEMU (`tools/test-uefi.py --proxy`): there the ESP reserved the
arena below 4 GiB, both chainloaded stages adopted it, the allow-list refused
0x42000c1b before any SMC, and the allowed call executed a real `smc #0` at EL2
that QEMU's own SMC conduit answered with x0 = -1 (PSCI NOT_SUPPORTED) -- the
payload's SMC path works on a real AArch64 core; what TrustZone on the A16
answers is the open question. In QEMU the ESP arena is also 2 MiB aligned, the
record is version 2, and `GPU_STORE32` stores one arena word through the real
clean/store/clean sequence and refuses anything outside the arena. Every A16 value below is an
expectation unless it is marked PROVEN, and PROVEN means one of: read on this
unit (the `build/a16-gpu/stage1-*.json` logs), byte-for-byte in the upstream
Linux source or the sha-pinned firmware. INFERRED means reasoned, not shown.
The operator's sequence, expected values, abort criteria and recovery are in
`docs/A16-ADRENO-STAGE3-RUNBOOK.md`.

Stage 3 continues from Stage 1 (`docs/A16-ADRENO-STAGE1.md`): the CX side is
on, the chip is identified (0x44070041, 4 slices, speed bin 1), and the Adreno
SMMU is an SMMU-500 v2.4 with 25 context banks and every stream set to fault.

## What each step does

Each step is a subcommand of `tools/a16gpu.py` (code in
`tools/a16gpu_stage3.py`), each a fresh process that first re-proves Stage 1's
gates from reads, and each has sub-step ids for `--stop-after`:

| cmd | ids | what |
| --- | --- | --- |
| `arena` | A0 | bootinfo `gpu_arena_*`; `--claim` takes 32 MiB from Conventional memory |
| `status`, `rsc-survey` | T*, R1 | read-only: arena, CB0/CB1, streams, GMU state; apps RSC TCS contents |
| `gmu-smmu` | G1-G4 | P2: LPAE tables for SID 5, CB1, S2CR then SMR, USFCFG, stream table verified |
| `gmu-boot` | B0-B9 | P3: RPMh MX/CX, PLL0 + RCGs, TZ SMC, IRQs + FAL, firmware, GMU registers, CM3 start, HFI init |
| `hfi` | H1-H5 | P4: TABLE (lowest OPP), BW_TABLE, CORE_FW_START, START, GX_BW_PERF_VOTE |
| `oob` | O1-O2 | P4: OOB GPU_SET, ack, GX on proven from GMU_PWR_CLK_STATUS |
| `cp-fence` | C1-C6 (C3, C3P, C3S) | P5: CB0 for SIDs 0 and 1, SQE, a8xx hw_init with the CP aperture SMC (C3), read-only pre-start snapshot (C3P), SQE start and samples (C3S), CP_ME_INIT, SECVID 0, CP_MEM_WRITE fence; `--sqe-probe fault` starts the SQE with nothing mapped |
| `reset-reason` | R0 | read-only, AO: the IMEM pil-reloc word; `--inferred-imem` adds the INFERRED msm-imem words (download cookie, restart reason, boot stats), never in a GPU session |
| `quiesce` | Q0-Q4 | undo: OOB clear, GBIF halt, slumber (or force off), CM3 reset, SMMU fault-all, clocks to XO |

The rules every step obeys:
- **GX is refused** before a byte is sent until the GMU itself reports GX on in
  the same process (`Session3._check`: OOB GPU_SET acknowledged and
  GMU_PWR_CLK_STATUS 0x03d7dfa0 bits 0/1 clear, gate `gx`). Stage 1's rule stays:
  its `Session` refuses every GX register.
- **Strict SError guard** on every access (`a16gpu.arm_guard`), exit 3 at the
  access that raised or found one, nothing sent after it.
- **Bounded polls**: every poll has a read limit and a wall-clock budget and
  makes at least two reads (`POLL` in `a16gpu_stage3.py`).
- **Idempotent**: a step reads what is already there and writes only what
  differs (SMMU banks read back identical, a running GMU, a started HFI, a held
  OOB, an initialised CP are all left alone). A started SQE is never
  re-initialised: cp-fence resumes at C4 after C3 or a C4 timeout, and
  refuses one it did not start. A CM3 out of reset (booted or not) blocks
  every SMMU rewrite, and a bank whose stream is routed is faulted before it
  is touched (SCTLR.M = 0 would pass addresses through untranslated). The arena holds a host record
  (tokens per GMU boot) that is bookkeeping, never evidence: the hardware
  state it depends on is re-read first.
- **DRAM discipline**: the only writable DRAM is the arena; every write is
  followed by DC CIVAC and every read preceded by one, because the GPU and GMU
  are treated as non-coherent (IORT says so; the DT's `dma-coherent` is not
  trusted yet). The model proves this matters: with a clean that does nothing,
  the tool's own read-back still matches, but the GMU walks stale tables and
  never boots.
- **The HFI index line.** The queue-table header puts all four index words of
  the command and response queues (cmd read/write at +0x40/+0x44, rsp
  read/write at +0x70/+0x74 of the HFI block) in one 64-byte line, and the GMU
  writes two of them. Clean after a write is not enough here: a proxy
  `writemem` leaves the whole line dirty in the CPU cache for a USB round trip,
  and the later clean writes back the GMU's older words over its newer ones.
  So the host's two index words go through `gpu_call(STORE32)` (clean and
  invalidate, one 32-bit store, clean and invalidate, back to back on the
  target), and only once the command queue has drained (read index == write
  index, a bounded poll): with one command in flight the GMU then has no reason
  to write that line. The model loses a GMU index write with the Stage 3
  build's writemem + clean and no drain, and none with the fix.

## The GPU arena (q1n1)

`platform/uefi/gpu-el2.c`, published through bootinfo fields appended after
`battery_session`: `gpu_call`, `gpu_state`, `gpu_arena_base`, `gpu_arena_size`,
`gpu_arena_flags` (bootinfo is now 64 words).

- The ESP payload reserves 32 MiB below 4 GiB as EfiLoaderData before
  ExitBootServices (flags = 1). AllocatePages only aligns to 4 KiB, so it takes
  34 MiB, keeps the 2 MiB-aligned 32 MiB inside and frees the head and tail.
  That takes effect only after the ESP image is reinstalled.
- A stage without one (today's A16: the running ESP image predates this) is
  asked to claim it: `gpu_call(CLAIM)` walks the EFI map captured at EBS, takes
  the highest 2 MiB-aligned 32 MiB window of **Conventional** memory below the
  limit (default 4 GiB), clear of q1n1's own regions, checks that the EL2
  tables map it Normal (not Device), and publishes it (flags = 2). q1n1
  allocates nothing after EBS, so Conventional memory is unused in the q1n1
  world; a guest must treat the range as reserved (Stage 4).
- Every chainloaded stage inherits it through bootinfo, so a chainload never
  reuses memory the GPU may still DMA into.
- A new arena (the ESP reservation, or a claim) is zeroed and cleaned to the
  point of coherency before it is published; an inherited one is left alone.
  DRAM keeps its contents over a warm reset, and the host record in the arena
  says "TrustZone init done", carries boot tokens and ring pointers: read in the
  next boot, it would skip the SMC or resume a CP that no longer exists. The
  clean matters as much as the zero: a dirty line of zeros evicted later would
  land on something the GMU wrote.
- `gpu_state.version` is 3: version 2 adds `gpu_call(STORE32, pa, value)` (op 4,
  a 4-byte-aligned word inside the arena, refused otherwise) and the zeroing;
  version 3 forwards the CP aperture SMC (below). The tool refuses a stage with
  version 1 at A0, before anything is written, and version 2 at C3, before the
  SQE start (unless `--no-aperture`).

Layout (`adreno_x2.ARENA_LAYOUT`): tables 1 MiB (CB1 half, CB0 half), host
record, GMU dummy 16 KiB, debug 28 KiB, log 16 KiB, HFI 16 KiB, SQE 128 KiB,
ring 32 KiB, memptrs 4 KiB, and the GMU icache 16 MiB - 16 KiB at +2 MiB.

## The guarded SMC (q1n1)

`gpu_call(SMC, fid, x1, x2, x3)` issues exactly one SMC: `0x42002801` with
x1 = 1 (one value argument) and x2 a non-empty subset of the
`QCOM_SCM_GPU_*_REQ` bits (the tool sends 9 = ALWAYS_EN | TSENSE_EN, as
`a6xx_gmu_secure_init`), x3 = 0. Anything else is refused before an SMC. The
proxy brackets the P_CALL with its SError polls; the SMC runs under
`Q1N1_GUARD_SKIP`, so an SMC that is UNDEFINED at EL2 (SCR_EL3.SMD) is stepped
over and reported (-7) instead of stopping the payload; INTERRUPTED is resumed
with x0 = 1 and x6 = TrustZone's session (the QCOM_A6 quirk), EBUSY retried
after 30 ms, both bounded; WAITQ_SLEEP needs an interrupt q1n1 does not take and
is reported. TrustZone's x0..x3 land in the record. The tool calls it once per
boot (the arena record keeps that across a chainload, since the new stage's
record starts at zero), as Linux's GMU_STATUS_SECURE_INIT does.

Linux resumes INTERRUPTED without a bound. The payload stops after 1024 resumes
(-10), and on that or WAITQ (-8) the call is still open in TrustZone. Linux
never issues a fresh call on top of an open one, so the tool records
`tz_ok = 2` (pending) in the arena record and refuses B3 until a reboot
(the ESP zeroes the arena, which clears it); `--force-smc` is the operator's
override, not a retry to try casually.

## Values, with evidence

GMU register offsets on a8xx are GPU-base-relative dwords: `gmu_read` subtracts
`mmio_offset` (a6xx_gmu.h `GMU_BYTE_OFFSET`), so an XML offset `d` is
0x03d00000 + 4d. PROVEN by `CORE_FW_VERSION` 0x1d3f8 -> 0x03d74fe0, the plan's
firmware version word.

| Step | Register / value | Evidence | Status |
| --- | --- | --- | --- |
| G | CB base 0x03dc0000 (+0x1000 per bank), GR1 0x03da1000 | A16 IDR1 0x40000019: 4 KiB pages, NUMPAGENDXB 4 | PROVEN |
| G | CB1 for SID 5, CB0 for SID 0 | arm-smmu-qcom.c qcom_adreno_smmu_alloc_context_bank; glymur.dtsi iommus | PROVEN (Linux) |
| G | ACTLR 0x303, CBA2R 1, CBAR 0x1f300, TCR2 0x38012, MAIR0 0xf404ff44, SCTLR 0x1167 (HUPCF) | arm-smmu.c write_context_bank, qcom client_match, io-pgtable-arm.c | PROVEN (Linux) |
| G | TCR 0x802020: T0SZ 32, SH OS, NC walks, EPD1 | io-pgtable non-coherent path; 32-bit VA chosen here | INFERRED (choice) |
| G | S2CR before SMR; SMR1 = VALID \| 5; SIDs 2,4,7 unmatched; sCR0.USFCFG | arm_smmu_write_sme; A16 sCR0 0x00270406 already has USFCFG | PROVEN |
| C | SMR0 = VALID \| 0 and SMR2 = VALID \| 1, both to CB0 | glymur.dtsi gpu `iommus = <&adreno_smmu 0 0x0>, <&adreno_smmu 1 0x0>` (one domain, one SME per stream ID); SID 1 was left unmatched before the SQE-reset fixes | PROVEN (Linux) |
| G | icache VA 0x4000 (16M-16K), dummy 0x60000000 (16K), debug 0x60400000 (28K): privileged; log/HFI unprivileged | a6xx_gmu_init, a6xx_gmu_memory_alloc (MSM_BO_MAP_PRIV for fixed iovas) | PROVEN (Linux) |
| G | log 0x60004000, HFI 0x60008000 | first drm_mm holes; the GMU is told both | INFERRED (harmless) |
| B1 | MX/CX vote to >= LOW_SVS (hlvl 2 on the A16's lists), never below an existing RSC vote; refused when no wake TCS (or still-enabled active TCS) shows drv-2's vote for a rail -- a sleep-TCS vote is not the live level (review 2) -- (`--rail-vote-unseen` overrides); DDR BCMs only with `--host-bw` | gpucc required-opps; rpmh-rsc.c TCS sequence; A16 cmd-db cx/mx.lvl | values PROVEN, the need INFERRED |
| B2 | PLL0 L 0x0044003b, alpha 0xe555, CONFIG 0x25c400e7/0x0a8060e0/0xf51dea20, USER 0x408/0x2; configure, calibrate (MODE bit 8), RUN, lock bit 31, outputs, OUTCTRL | gpucc-glymur.c:43-51, clk_lucid_evo_pll_configure / _prepare / _enable (Taycan EKO_T = Lucid EVO ops); A16 MODE 0x100 | PROVEN (Linux); lock INFERRED |
| B2 | GMU RCG CFG 0x201 (PLL0_OUT_EVEN /1 = 575 MHz), HUB RCG CFG 0x505 (GPLL0 /3 = 200 MHz) | clk_set_rate(200 MHz / 150 MHz) ceil in ftbl_gpu_cc_gmu/hub_clk_src; clk_rcg2_shared_force_enable_clear | PROVEN (Linux) |
| B3 | SMC 0x42002801, x1 1, x2 9; then CX_MISC_SW_FUSE_VALUE read | a6xx_gmu_secure_init; qcom_scm_gpu_init_regs; the A16 is in the SCM allow list | call PROVEN; TZ accepting it from EL2 INFERRED |
| B6 | AO_HOST_INTERRUPT_CLR ~0, MASK 0xffffffd6; FALNEXT 1 then FAL 1; then, after a quiesce (host record `pdc_sleep`, or RSC_STATUS0_DRV0 bit 16 set), RSCC_CONTROL_REQ 2, CONTROL_ACK bit 1, SEQ_BUSY_DRV0 0, REQ 0 | a6xx_gmu_resume, a6xx_gmu_fw_start (a8xx), a6xx_rpmh_start (GMU_STATUS_PDC_SLEEP) | PROVEN (Linux) order and values; INFERRED that bit 16 stays set while the RSC sleeps |
| B7 | CM3_SYSRESET 1 held while ITCM (132 words), DTCM (16 words) load; icache block 0x1c80c bytes at VA 0x4000; buffers zeroed; CORE_FW_VERSION reads 0x50020026 | a6xx_gmu_fw_load; gen80100_gmu.bin parsed | PROVEN (file); the extra SYSRESET=1 before the load INFERRED harmless (Linux writes it in force_off and a6xx_gmu_start) |
| B8 | INIT_RESULT 0, BOOT_CONFIG 2, QTBL_ADDR 0x60008000, QTBL_INFO 1, AHB_FENCE_RANGE_0 0x80c808c0, CM3_CFG 0x4052, GENERAL_10 0x8010100, GENERAL_8 0x60004003, GBIF_QSB_SIDE0-3 0x00071e20, GBIF_CX_CONFIG 0x20023000 (twice), MRC_GBIF_QOS 0x33, SYS_BUS/ICACHE/DCACHE 1 | a6xx_gmu_fw_start, a840_gbif, a6xx_gmu_power_config | PROVEN (Linux) |
| B9 | version word first; SYSRESET 1, GENERAL_9 0, SYSRESET 0; INIT_RESULT & 0x1ff == 0x100 within 100 ms; HFI_CTRL_INIT 1, STATUS bit 0; GMU2HOST CLR ~0, MASK ~CM3_FAULT | a6xx_gmu_start, a6xx_gmu_hfi_start, a6xx_gmu_resume | PROVEN (Linux) |
| H1 | TABLE (15), GPU_PERF: [0, 310 MHz] x [gx vote, dep vote, kHz]; [0, 575 MHz] x [cx vote, kHz]. A16 votes: 0x00380201, 0x0fffc2ff, 0x00400202 | a8xx_hfi_send_perf_table, a6xx_gmu_rpmh_arc/dep_votes_init; A16 cmd-db; glymur OPPs | PROVEN (Linux + A16 cmd-db); lowest OPP only is our restriction |
| H2 | BW_TABLE (3): 2 levels, SH0 0x50004 / MC0 0x50000 / ACV 0x50068; 2136719 kBps -> SH0 1805, MC0 1831, ACV perfmode 4; CN0 from cmd-db | a6xx_generate_bw_table, a6xx_gmu_rpmh_bw_votes_init | PROVEN (Linux + A16 cmd-db); CN0 not read on the A16 yet |
| H3-H5 | CORE_FW_START (14), START (10), GX_BW_PERF_VOTE (30) ack 1, perf 1, bw 1 | a6xx_hfi_start, a6xx_gmu_set_initial_freq | PROVEN (Linux) |
| O | HOST2GMU_INTR_SET bit 30; ack GMU2HOST_INTR_INFO bit 31, clear it; PWR_CLK_STATUS bits 0/1 clear | a6xx_gmu_set_oob (set_new/ack_new), a8xx_gmu_gx_is_on | PROVEN (Linux) |
| C | hw_init writes in a8xx order: aperture, GBIF/RBBM_GBIF halt 0, SECVID TSB, GMEM ranges 64 MiB, UCHE trap 0x1fffffffff000, perfctrs, bicubic table, UCHE_CLIENT_PF 0x81, x285 non-context list per pipe, hang detect, CX busy mask, power counter, CP protect (52 + pipe 15), GMEM save/restore, APRIV 0x6f/0xf, interrupt masks, **SMC 0x42000c1b (4, 0xffff0000, 0xffffffff x3)**, SQE base, RB base/CNTL 0x20c/rptr shadows; then the snapshot (C3P); SQE_CNTL 1 (C3S) | a8xx_gpu.c hw_init, adreno_gpu.c adreno_hw_init (qcom_scm_set_gpu_smmu_aperture(0)), x285 catalog | PROVEN (Linux); skipped: UBWC (needs master ubwc.h), preemption (one ring), pwrup list, HW clock gating (Linux: after CP_ME_INIT) |
| B2, C3 | CX_MISC_CX_AHB_{AON,GMU,CP,VBIF_SMMU,HOST}_CNTL 0x03d9e040..50 = 0x1b12 (ERRORSTATUSENABLE, RESPONDERROR, TIMEOUTENABLE, RELEASE; timeout 2^19 HUB cycles = 2.6 ms at 200 MHz) | KGSL adreno_gen8.c gen8_enable_ahb_timeout_detection, adreno.c adreno_get_ahb_timeout_val, gen8_reg.h; noc_timeout_us 3410 (gen8_0_x/gen8_4_0) | KGSL (not upstream Linux); INFERRED for the X2-90; read back checked |
| C | CP_THREAD_CONTROL BIT(27); CP_ME_INIT mask 0x4b (BIT(8) clear), 3, 0x20000000, 2, 0, 0, 0x80000000; fenced WPTR 10 | a8xx_cp_init, a6xx_fenced_write | PROVEN (Linux) except BIT(8) (PLAN) |
| C | SECVID_TRUST_CNTL 0x03d3d000 = 0 after CP_ME_INIT, read back 0; RB_GC_GMEM_PROTECT (BR) 0x15000000 | a8xx hw_init zap-less path; no zap node on UX3607OA | PROVEN (Linux) |
| C | CP_MEM_WRITE (0x703d8003) of 0xf3a0000n to VA 0x10200040 | adreno_pm4.xml | PROVEN (encoding) |
| Q | OOB clear bit 31 (or, after HFI START without an OOB this boot, a dummy GPU_SET/ack/clear: a6xx_gmu_shutdown, review 2); wait RPMH_POWER_STATE 0; GBIF_HALT 1/ack 1, 2/ack 2, 0; POWER_COUNTER_ENABLE 0; PREPARE_SLUMBER (33); AO_AHB_FENCE_CTRL 0; FALNEXT 1; CX_BUSY bit 23 clear; masks; SYSRESET 1; HFI stop (only if SYSRESET reads back 1); RSCC_CONTROL_REQ 1 (recorded as `pdc_sleep` for the next gmu-boot's wake), RSC_STATUS0 bit 16, 0; SMRs invalid, S2CR fault, CBs off, sCR0.USFCFG set | a6xx_gmu_shutdown, a6xx_gmu_notify_slumber, a8xx_bus_clear_pending_transactions, a6xx_rpmh_stop | PROVEN (Linux); the force path follows a6xx_gmu_force_off minus the GX sw reset; HFI stop after the reset is ours (below) |

## A16 run 1 (2026-09-25)

Logs: `build/a16-gpu/stage3-run1-*.json`. Stage generation 6, arena 0xfcc00000.

| Step | Result |
| --- | --- |
| guard, cx-on | clean; `stage1_gate_met` |
| status, rsc-survey | CM3 in reset, INIT_RESULT 0, no SMR valid; RSC v4.0 (`_V3` offsets), 8 TCSes, 16 cmds, **every TCS empty** |
| arena --claim | 32 MiB at 0xfcc00000 (Conventional), record version 2 |
| gmu-smmu | CB1 / SID 5; CB1 FSR reads 0x400 (FORMAT = AArch64, no fault bit) |
| gmu-boot B1 | stopped at B0 on a read-back mismatch (see corrections); after the fix, MX/CX voted NOM (index 5) with `--rail-vote-unseen --rail-level nom`, link unaffected |
| B2 | PLL0 calibrated and locked at 1150 MHz (MODE 0xe0000005); GMU RCG 0x201, HUB RCG 0x505 |
| B3 | TZ SMC ok, x0 0, no resumes; SW fuse 0x7 |
| full gmu-boot | firmware 0x50020026, INIT_RESULT 0x100, HFI ready, CB1 clean |
| hfi | first run stopped at H1 (tool bug, see corrections); rerun: all five messages acked, error 0 |
| oob | GPU_SET acked, GMU_PWR_CLK_STATUS 0 (GX on) |
| cp-fence --stop-after C3 | CB0 / SID 0 programmed, buffers written, hw_init written through CP_RB_RPTR_ADDR_BV_HI; **the CP_SQE_CNTL = 1 write never returned**: exit 5, the A16 left USB and came back in Windows (BootNext spent). No SError recorded. See "The SQE start (after run 1)" |

The C3 row's reset is analysed in the next section; the cp-fence procedure
after it is runbook section 7.

## The SQE start (after run 1)

In short: every register `hw_init` writes before `CP_SQE_CNTL`
matched Linux in value and order; the A16 went down at the start itself with
no SError. The missing reply only says the SoC died (or the core stopped)
before the proxy's reply left over USB, which takes far longer than the
microseconds a fatal error from the SQE's first fetch would need: a stall that
ends in a NoC timeout, a TrustZone-reported access violation and a GMU/RPMh
fault all fit it equally; a context-bank fault does not (it would have been
read back). Nothing in run 1 ranks one of those above the others. What was different from Linux or KGSL before the start,
and what the tool does now:

- **CX AHB timeouts** (KGSL arms them on every gen8 GMU boot, "to catch any
  register access taking longer ... before NOC timeout gets detected"; upstream
  Linux does not): armed at B2, checked at C3. A stuck GPU register access now
  ends as an AHB error, an SError the guard reports (exit 3), instead of a NoC
  timeout, but only for a CPU access that is in flight when it stalls; it
  does nothing for an error raised by the GPU's own traffic. The tool writes
  them only when the values it finds have no bits outside KGSL's fields.
  `--no-ahb-timeout` if the X2-90 does not take them.
- **`qcom_scm_set_gpu_smmu_aperture(0)`**: made where `adreno_hw_init` makes it
  (payload v3 allow-lists exactly that call; TrustZone's x0 is recorded and, as
  in Linux, a refusal does not stop the start). `--no-aperture` takes it out.
- **SID 1** is routed to CB0 as Linux does (glymur.dtsi lists SIDs 0 and 1).
- Still different, on purpose: no UBWC, no LLCC slice, no HW clock gating
  before CP_ME_INIT (Linux order), no apps-side `gfx-mem` DDR vote (Linux
  votes it in `a6xx_gmu_set_initial_bw`; here the GMU's BW table votes the same
  BCMs; `gmu-boot --host-bw` is the bisection step if a real start stalls).
- The SQE image matches Linux: file minus its first dword, 119824 bytes at a
  4 KiB-aligned VA, read-only; `CP_SQE_INSTR_BASE` is that VA (Linux does not
  add 4). No AQE on this chip in Linux, so none here.

The start is instrumented: C3P reads (and writes to `<out>.prestart.json`)
the SMMU global and context-bank fault registers, the stream table, GMU/GX
power, GBIF halts, SECVID/TSB, the bases, `CP_AQE_INSTR_BASE_0`, a walk of
CB0's tables from DRAM for every CP buffer, the SQE digest and the AHB
timeouts, and refuses to start on any problem. C3S writes `CP_SQE_CNTL = 1`
and takes timed samples, SMMU and GMU first. `--sqe-probe fault` starts the
SQE with an empty CB0 root table, so no GPU access reaches memory: a probe
that survives with a CB0 translation fault at 0x10000000 separates "starting
the SQE is fatal" from "a fetch that reaches memory is fatal". `reset-reason`
and `tools/read-a16-reset-reason-windows.ps1` (read-only) collect what the
IMEM and, after a reset, Windows and the firmware's logs say about it.

## A16 run 2 (2026-09-25): the gate is met

Logs: `build/a16-gpu/stage3-run2-*.json`, USB watch `build/a16-gpu/usb-watch-20260925-run2.txt`.
Fresh q1n1 (proxy reset, back in q1n1 in about 60 s), gpu-el2 version 3, arena 0xfcc00000.

| Step | Result |
| --- | --- |
| B2 | CX AHB timeouts armed: all five ports read back 0x1b12 (2.6 ms at HUB 200 MHz). The X2-90 has KGSL's gen8 registers (PROVEN) |
| B3, gmu-boot, hfi, oob | as run 1; HFI acked first time with the own-id ack fix |
| C3 | CB0 for SIDs 0 and 1; `qcom_scm_set_gpu_smmu_aperture(0)` ok, x0 0 |
| C3P (first) | refused: GBIF_HALT 0 but GBIF_HALT_ACK 0x3 (see corrections) |
| quiesce, gmu-boot | graceful; the GPU RSC woke after quiesce (`rscc_start.woken`, review-2 fix PROVEN); KGSL's GBIF_HALT = BIT(3) left the ack at 0x3 |
| C3P | passes; the ack recorded as a note |
| `--sqe-probe fault` | SQE started with an empty CB0: translation fault at 0x10000000, stream 0, FSR 0x402; A16 stayed up |
| quiesce, power-up, C3S | **SQE started with the image mapped**: no fault, no interrupt, CP_SQE_CNTL 1 for 26 ms of samples |
| C5 | CP_ME_INIT done, RBBM_STATUS 0x1, INT0 0; SECVID 0 (no zap needed) |
| fence x3 | 0xf3a00001/2/3 written by the CP and read back; WPTR 14/18/22; CB0 clean |
| quiesce | graceful |

What changed between run 1 (reset at CP_SQE_CNTL = 1) and run 2 (started): the CX AHB
timeouts, the aperture SMC, SID 1 routed to CB0, and GBIF_HALT held at KGSL's BIT(3) instead of
Linux's 0. Which of these mattered is not isolated; all four stay.

## A16 run 3 (2026-09-25): P6 and P7 pass

Logs: `build/a16-gpu/stage3-run3-*.json`, the render target `stage3-run3-t3.p7.bgra`, a
side-by-side `stage3-run3-p7-a16-vs-m3max.png`. Same q1n1 stage and arena as run 2.

| Step | Result |
| --- | --- |
| proven sequence | guard, cx-on, arena, gmu-smmu, gmu-boot, hfi, oob, C3S, C5, fence 0xf3a00004 (wptr 14): all first time |
| D0 | framebuffer from bootinfo: 0xf9a20000, 2880x1800, stride 2880 (pitch 11520), BGRX; EFI `Reserved`, fully covered |
| D2 | 2D clear 208x224 in the arena: 0 bad pixels, guards intact; ring, IB and BV fences all 0xf3a00005 |
| D3 | 2D copy identical, sha256 4c2177c0...873c31 as the model; fences 0xf3a00006/7 |
| T0 | shaders sha 88aab4e2...; the two air2ir3 gather_info findings as expected (NTEX/NSAMP and PIXLOD programmed from reflection) |
| T3 | **first triangle**: SkyLight SimpleVertex + SimpleTextureFragment via air2ir3, max difference 1/255 against the M3 Max Metal golden, mean 0.09, no pixel over tolerance even on edges |
| D4 | on the panel at (2560, 64), 256x256: clear and blit 0 bad pixels, 0 bytes changed outside the rectangle, pages unmapped from CB0 afterwards |
| quiesce | graceful |

## Corrections found on the way

- **The DTCM window is not legacy.** Stage 1 dropped `GMU_CM3_DTCM_START` as a
  legacy a6xx define (REVIEW). a8xx still loads its DTCM blocks through it:
  `a6xx_gmu_fw_load` writes `REG_A6XX_GMU_CM3_DTCM_START + offset` for blocks at
  GMU VA 0x10004000. The registry now lists `GMU_CM3_DTCM` at 0x03d71000; entry
  0xff8 is `GMU_CORE_FW_VERSION` 0x03d74fe0.
- **`cx_gmu` cannot come from the AHB enable on this unit** (it never latches),
  so Stage 3 proves it the way Stage 1 proved `cx_smmu`: the clean CX_MISC read
  under the strict guard, the CX GDSC on and not under HW control, every other
  GMU clock running (`prove_cx_gmu`). The first GMU-block read is then
  `GMU_CM3_SYSRESET` (in `status`).
- **The X2-90 GMU runs from DDR**: `gen80100_gmu.bin` puts 0x1c80c bytes of code
  at GMU VA 0x4000 and describes sections up to 0x3297d0, all inside the 16 MiB
  icache buffer, which is why the arena is 32 MiB.
- The X2-90 catalog entry has no AQE firmware and no IFPC quirk; the tool loads
  neither.
- **The A16's SMMU fixes three context-bank bits** (run 1): SCTLR.ASIDPNE reads
  0 and TCR2 bits 5/6 (TBI0/TBI1) read 1 whatever is written. `gmu-smmu` wrote
  the bank without reading it back and `gmu-boot`'s B0 compared exactly, so B0
  stopped. Comparisons now ignore `CB_READBACK_FIXED`, and `smmu_program` reads
  the bank back before routing the stream to it. Neither bit changes
  translation here (one bank per stream, VAs below 4 GiB).
- **Firmware 0x50020026 acks HFI commands with the command's own id and header
  type 1**, not id 126 (run 1: TABLE seq 1 answered `0x0001030f 0x0010110f 0`).
  The tool now accepts both forms, matching Linux's `a6xx_hfi_wait_for_ack`
  (it matches on the returned seqnum), and logs every reply it skips.
- **GBIF_HALT_ACK is not the halt state on the A16** (run 2): it reads 0x3 with
  GBIF_HALT 0 or BIT(3), while the GMU runs from DDR through the same GBIF. The
  pre-start check now stops only on a halt request (GBIF_HALT bits 0/1) or a GX
  side acknowledgement. `gmu-boot` writes KGSL's GBIF_HALT = BIT(3) after the GBIF
  list, and `hw_init` keeps BIT(3) where Linux writes 0, as KGSL gen8 does.
- **No TCS shows drv-2's rail votes on this unit**, so B1 cannot see what it
  would replace. `--rail-level nom` votes MX/CX at NOM, at or above what
  firmware keeps for USB and the display, and B1 never lowers a vote the tool
  already sent on this arena.

## Deviations from Linux, on purpose

- Only the lowest OPP is offered to the GMU (`--max-opps` widens it).
- OOB GPU_SET stays set after `cp-fence` (Linux clears it at the end of
  hw_init); `quiesce` clears it before slumber.
- The host DDR vote is off by default: apps drv-2 BCM votes replace every
  other drv-2 client's aggregate, and the display scans out of DDR on the same
  path. The GMU votes DDR itself from the BW table.
- After quiesce the GBIF halt is cleared as Linux does; `--leave-halted` keeps
  it for exit/boot, and the next `cp-fence` clears it as hw_init does.
- Quiesce does not collapse GX (the GX GDSC belongs to the GMU); it records
  GMU_PWR_CLK_STATUS afterwards.
- Quiesce zeroes the HFI indices after CM3_SYSRESET reads back 1 (Linux does it
  before masking the IRQs, with the GMU slumbering): the host writes that line
  with a plain write + clean, which is only safe with nothing else writing it.
  If the reset does not read back, the headers are left alone, the SMMU step
  still faults SID 0/1/5 and clocks are parked, and quiesce exits 2 at Q2.
- After quiesce SID 0 and 5 match no SMR, so they fault only while
  sCR0.USFCFG is set: Q3 sets it again if something cleared it, and Q3 and
  `arena --release` treat an unmatched stream with USFCFG clear as reaching
  memory (bypass).
- Before queueing on the ring, the record's WPTR must equal both CP_RB_WPTR and
  CP_RB_RPTR; a C4 resume must see CP_RB_WPTR equal the record. Linux keeps its
  WPTR in the kernel and never has to ask; here the record lives in DRAM and a
  lost write would make the CP run from its RPTR through stale ring words.

## Risks that remain

- TrustZone may refuse or trap the SMC from EL2 (-7 / nonzero x0): the tool
  stops at B3 and loads no firmware.
- The GMU could fail silently if a vote, clock or table is wrong: B9 reports
  INIT_RESULT, AO_HOST_INTERRUPT_STATUS, GMU2HOST_INTR_INFO and CB1's fault
  registers when it times out.
- The HFI index line (above): `STORE32` narrows the window in which the line is
  dirty to one store and one line fill on the target, and the drain removes
  the GMU's reason to write the line during it. An unsolicited GMU message
  (F2H error or debug) that lands inside that window of a few hundred
  nanoseconds would still lose its write index. INFERRED rare, not zero; the
  way to close it completely is a Normal Non-cacheable mapping of the HFI page,
  as Linux uses (write-combined GMU memory), which q1n1's EL2 tables do not
  offer yet.
- `CP_RB_WPTR` is read back for the ring check. It reads back the written value
  on a6xx-class CPs (INFERRED for X2-90); if it reads 0 the check stops C4/C6
  with "disagree" and nothing is queued: a false stop, not a hazard.
- SECVID: writing 0 without a zap shader relies on the UX3607OA having no zap
  node; the read-back check comes after the write. A locked register reads 1
  (exit 2 at C5); a firewall that faults the write would show as exit 3.
- `gpu_smc` and `live_attr` reset `q1n1_exc_count` around their guarded access,
  so a stepped-over SMC trap shows in `gpu_state.smc_undefined`, not in the
  global exception counter.
- The apps RSC register layout version is read at run time (major >= 3 picks
  the v3 offsets); `rsc-survey` shows it before any vote.
- A bus hang (a read that never completes) is outside what the SError guard
  can see; exit 5 means power-button recovery.

## P6 and P7: the first 2D blits and the first triangle

Status, 2026-09-25: **tooling only; neither has run on the A16.** Both run
after the sequence run 2 proved (cx-on ... oob, cp-fence) and reuse its ring,
memptrs and fence. The operator's steps, expected values and abort criteria
are in `docs/A16-ADRENO-STAGE3-P6P7-RUNBOOK.md`; the code is
`tools/a16gpu_p6p7.py`, the model and the Mesa comparison
`tools/test-a16gpu-p6p7.py`.

| Command | Steps | What the GPU does |
| --- | --- | --- |
| `blit --write` | D0 checks, D1 window + uploads, D2 clear, D3 copy | 2D-engine clear of a rectangle in a 256x256 BGRA8 arena target, read back; 2D copy of a 256x256 arena image into it, read back |
| `blit --write --to-framebuffer` | D0..D3, then D4 | the same two operations into one rectangle of the GOP framebuffer: only that rectangle's rows are mapped into CB0, for two submissions, then removed |
| `triangle --write` | T0 variant checks, T1 uploads, T2 draw, T3 compare | SkyLight `SimpleVertex` + `SimpleTextureFragment` (air2ir3, BINDING-ABI v2), one textured triangle into a 96x64 BGRA8 arena target, compared with the M3 Max Metal render of the same pipeline and bytes |

### Where the command streams come from

Every register offset, field and enum comes from Mesa's a8xx XML
(`tools/a8xx_regs.py`, generated by `tools/a16gpu-p7/gen_a8xx_regs.py` from the
Stage 6 Mesa tree, bbeb2dca9fee, A8XX variants only). The sequences are
gallium's a8xx sysmem path, cited per function in the code:
`fd6_emit_restore` + static context/non-context registers, the sysmem
`gmem_cache_cntl` (`fd6_calc_gmem_cache_offsets` for the X2-90: 8 CCUs, 21
MiB, depth fraction THREE_QUARTER, `RB_CCU_CACHE_CNTL` 0xd0c01240),
`fd6_clear_texture`/`fd6_clear_surface` (P6 clear, P7's clear), `fd6_blit` ->
`emit_blit_texture` (P6 copy), and for P7 `fd6_emit_sysmem_prep`,
`fd6_program.cc` (VS + FS only: `emit_shader_regs`, `emit_vfd_dest`,
`emit_vpc`, `emit_fs_inputs`, `emit_fs_outputs`, `emit_interp_state`), the
draw-state groups in gallium's order (inlined, not CP_SET_DRAW_STATE),
`CP_DRAW_INDX_OFFSET`, `fd6_emit_flushes` (gen8 DUMMY/SUBPASS_FENCE/DUMMY) and
the batch's CACHE_FLUSH_TS fence. The ring side is Linux's `a7xx_submit`
(a8xx uses it) without what one context needs none of: no pagetable switch,
no IFPC markers, no IRQ bit, no stats. Each submission ends with CACHE_CLEAN
fences on the BV and BR threads (BR waits for BV with CP_WAIT_TIMESTAMP), so
every byte the GPU wrote is in DRAM before the fence value is.

**Mesa as ground truth.** `tools/testdata/a16gpu-p6p7/*.rd` are Mesa's own
command streams for the same three operations, captured from the real
gallium driver for chip 0x44070041 on drm-shim (FD_RD_DUMP), the triangle
with the exact air2ir3 variants loaded in place of Mesa's compile. The test
walks both streams and compares register state at the CP_BLIT / CP_DRAW, the
packet kind per register, the packet/event order and every non-register
packet's payload (addresses and fence values masked):

- P6 clear and copy: identical at the CP_BLIT except the addresses.
- P7: 200+ registers identical at the draw; the differences are all listed
  and explained (`P7_EXPECTED`): addresses; the 96x64 target (scissors,
  pitches); Metal conventions (clip z in [0, 1] via `GRAS_CL_CNTL`
  clip_halfz, `GRAS_CL_VIEWPORT_YSCALE` = -H/2, ZSCALE 1 / ZOFFSET 0,
  provoking vertex first: `PC_CNTL`, `VPC_PC_CNTL`); Metal's vertex descriptor
  (one buffer, attributes at 0 and 8); the two Stage 6 findings below
  (`SP_PS_CONFIG`, `SP_PS_CNTL_0`, `RB_PS_CNTL`); the sample counter left out.
  T#0 equals Mesa's `fdl6_view_init<A8XX>` descriptor dword for dword; S#0
  equals Mesa's except WRAP_R (GL defaults REPEAT, Metal clamp_to_edge); the
  VS UBO state (UBO 0 empty, UBO 1 = 4 vec4) equals Mesa's.
- Left out on purpose everywhere: CP_SET_AMBLE (preemption), ZPASS_DONE and
  `RB_SAMPLE_COUNTER_CNTL` (occlusion query/autotune), IB2 nesting and
  CP_SET_DRAW_STATE (the groups run inline in one IB).

### Memory rules (added to the arena's)

- Eight new arena regions above the GMU icache (`adreno_x2.ARENA_LAYOUT`
  `p67_ib`, `p67_misc`, `p6_src`, `p6_dst`, `p7_code`, `p7_data`, `p7_tex`,
  `p7_rt`, together `P67_ARENA_WINDOW` = arena+0x1200000, 0xC0000 bytes). Only
  `p6_dst` and `p7_rt` are GPU-writable; the rest are mapped read-only (AP
  RDONLY). They appear in CB0 at `P67_VA` 0x11000000 only when blit/triangle
  install them; cp-fence's own entries (SQE, ring, memptrs) never change.
- A command stream is built through a `Window`: every address it carries is
  requested by region, offset, length and access, and anything outside a
  mapped region, or a write to a read-only one, is refused while building.
  Before each submission the tables are read back from DRAM and every
  reference is walked through them as the SMMU will (both ends, arena or
  framebuffer span only, write permission where the GPU writes).
- Every CPU write is an arena write (IBs, uploads, canaries, table pages);
  only table pages that differ are written, then TLBIASID on CB0.
- Both render targets sit between guard bands filled with a canary; the
  readback checks the guards and, for the P6 clear, every pixel outside the
  rectangle.
- The framebuffer (D4, opt-in): read-only discovery from bootinfo (GOP base,
  size, width, height, stride, format 0 RGBX / 1 BGRX), refused if it
  overlaps the arena or q1n1's own regions (image, heap, DMA, stage, memory
  map, preload cache), is not page/64-byte aligned, or the EFI map does not
  describe all of it as Reserved (not described, partly described, a type a
  guest could be given, or Runtime/ACPI/PalCode/Persistent:
  `--fb-any-type` to go on, after reading D0's `efi_types`; MMIO/MMIOPort
  is refused even with it: device space). Only the pages of the rectangle's rows go into CB0 (at `FB_VA`
  0x20000000, at most 128 MiB), for exactly two submissions; the step then
  removes them and checks from the tables in DRAM that nothing under `FB_VA`
  remains (table pages are written highest first, so a new L3 page is in
  DRAM before the L2 entry that reaches it, and an unmap clears the leaves
  before the parent entry). q1n1's console is stopped first (`P_FB_SHUTDOWN`, pixels stay);
  every byte of the span outside the rectangle is compared before and after.
  An interrupted run (link lost with the pages mapped) is cleaned up by the
  next blit/triangle before it submits anything.

### Discipline carried over from Stage 3

The CP must be the one `cp-fence` started for this GMU boot (tokens,
`CP_SQE_CNTL` 1, record WPTR = `CP_RB_WPTR` = `CP_RB_RPTR`, RBBM idle, CB0 and
the stream table as cp-fence left them, no SMMU fault recorded), else the
command stops at D0/T0 without writing. The ring is never wrapped: each
submission takes 35 dwords, and the command refuses when they do not fit.
The fence wait is a bounded poll of memptrs+0x40 (`POLL['fence']`), then
`cp_idle`, CB0 FSR, `CP_INTERRUPT_STATUS_GLOBAL` (HWFAULT and SWFAULT, bits
0-6 and 16-22: the run-2 SQE probe fault read 0x10000) and the error bits of
`RBBM_INT_0_STATUS` (CP_HW_ERROR, UCHE_OOB_ACCESS, UCHE_TRAP_INTR, AHB/GPC/
TSB errors, hang detect, SW fuse: the probe read 0x200), and the BV fence and
the IB's own CACHE_FLUSH_TS fence must carry the value too (the IB ran to its
end). These interrupt bits are sticky (no handler clears them), so D0/T0 also
refuse to start with one set. A fence that does not come records read-only
diagnostics and stops (exit 2): `quiesce --write`. In D4 the first failure
stays the reason; an unmap failure after it is recorded in
`framebuffer.unmap_error`, and the record keeps `fb_mapped` until the tables
in DRAM show the pages gone. If the bus or the link fails during that
cleanup (exit 3/5), that becomes the exit and the first failure is kept in
`framebuffer.first_failure`.
`quiesce` clears the P6/P7 record fields; the model runs a second full cycle
after it. Exit codes, JSON logs and `--stop-after` are Stage 3's.

### The P7 inputs (one source of truth)

`a16gpu_p6p7.P7` holds the scene: 96x64 BGRA8 target cleared to (0.1, 0.2,
0.3, 1); vertices (8,4)/(88,4)/(8,60) in pixels with texcoords (0,0)/(1,0)/
(0,1), stride 16; UBO 1 = the orthographic mvp; a 64x64 RGBA8 texture of ramps
and a +-24 checker (`p7_texture`), linear, clamp_to_edge. `python3
tools/a16gpu_p6p7.py --write-scene DIR` writes the same bytes for
`tools/a16gpu-p7/p7_metal_golden.swift`, which renders them with the OS
SkyLight metallib (sha-pinned) on the M3 Max: `p7-golden-m3max.bgra` (sha256
cb9c7424..., frame 0 = frame 2). An independent Python rasteriser matches the
golden within 1 code. The tool accepts per channel |diff| <= 3 everywhere,
except at most 16 pixels within one pixel of an edge.

The ir3 variants are `tools/a16gpu-p7/p7-shaders.json` (sha-pinned), made by
`make-p7-shaders.sh` from the Stage 6 compiler (air2ir3 c1d8fcf04b9eef02, Mesa
bbeb2dca9fee): VS instrlen 1, constlen 8 vec4, UBO 1 bytes 0..32 and 48..64
pushed to consts 0 and 32 (inside constlen); FS double threadsize, one
varying (2 components), no private memory, no driver UBOs. T0 refuses any
variant outside what the port covers (private/shared memory, ir3 driver UBOs,
bindless, prefetch, sysvals in the VS, constlen or UBO pushes beyond the
budget, bindings other than buffer(1) float4x4 / T#0 / S#0).

**Stage 6 findings** (logged in `p7.findings`, not worked around silently):
air2ir3 never runs `nir_shader_gather_info`, so the FS variant says
`num_samp` 0 and `need_pixlod` 0 although it samples T#0/S#0 with implicit
LOD. Programmed as-is, `SP_PS_CONFIG` NTEX/NSAMP would be 0. The tool takes
NTEX/NSAMP from the binding reflection and sets PIXLODENABLE the way Mesa's
GLSL path does for the same shader (the reference capture `gl-tri-bgra.rd`).
The fix belongs in air2ir3.

## Tests

```shell
python3 tools/test-a16gpu.py            # Stage 1 (still passes with the Stage 3 registry)
python3 tools/test-a16gpu-stage3.py     # Stage 3 tool vs the model; A16_GPU_FW_DIR=... adds the real firmware
python3 tools/test-a16gpu-p6p7.py       # P6/P7 vs the model (CP executing the new packets) and Mesa's captured streams
clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror \
  -Iplatform/uefi tools/test-q1n1-gpu.c platform/uefi/gpu-el2.c -o /tmp/test-q1n1-gpu && /tmp/test-q1n1-gpu
python3 tools/test-uefi.py --proxy      # QEMU: arena reserved (2 MiB aligned, zeroed) and inherited, gpu_call, a real guarded SMC, STORE32
```

The model (`FakeStage3`) adds to Stage 1's: a write-back CPU cache in front of
DRAM, the SMMU's stream matching and LPAE walk, PLL0 and the RCG switch, the
apps RSC, the SMC, the GMU (TCM load only in reset, boot checks against every
register and mapping above, HFI queues and acks, OOB), GX that hangs when off,
and a CP that fetches the SQE and ring through CB0 and executes PKT7s. It
checks the order and values above, every refusal, every bounded poll, the
failure paths (TZ refusal or trap, no PLL lock, no GMU init, HFI error, no OOB
ack, GX off after ack, SECVID locked, CP stuck, SError at a GMU write, busy
TCS, foreign SMR, no arena, no gpu_call), idempotent reruns, quiesce from every
state, and a second full cycle after quiesce. Since review 1 it also models
the whole-line write-back (a byte the CPU did not write but a device changed is
reported as clobbered), GMU index writes that land late, `STORE32`, a stage
record of version 1, a TrustZone call left open, a CM3 reset that does not
latch and USFCFG cleared under a quiesced SMMU. Since review 2 it models the GPU RSC's sleep
after quiesce's a6xx_rpmh_stop (a GMU started on a sleeping RSC never
initialises, so the second cycle needs a6xx_rpmh_start) and sleep-TCS votes
that sit below drv-2's live level. Since the SQE reset (`test_sqe_reset`) it
models the aperture SMC (payload v2 refuses it, TrustZone refusing or leaving
it open), the CX AHB timeout registers (and ones that do not take a value),
sGFSR/USF with the stream ID and CBFRSYNRA, IMEM, SID 1 on CB0, a probe start
whose fetch faults, a fault already recorded before the start, and run 1
itself (the link dies at `CP_SQE_CNTL = 1`: exit 5, the snapshot already
written to the checkpoint file).

`tools/test-a16gpu-p6p7.py` extends `FakeStage3` (`FakeP67`): the CP now
executes every packet P6/P7 emit, from the ring into IB1 (a7xx_submit's
thread control, CP_INDIRECT_BUFFER, CP_WAIT_TIMESTAMP, CP_EVENT_WRITE7 with
CACHE_CLEAN/CACHE_FLUSH_TS writes), PKT4 / CP_CONTEXT_REG_BUNCH /
CP_NON_CONTEXT_REG_BUNCH into a register file that refuses unknown offsets
and CP_PROTECTed ones, the 2D engine (CP_BLIT fill and copy with the swaps),
CP_LOAD_STATE6 and a draw that checks the program-state invariants
(instrlen/constlen against the variant, VPC linkage, VFD, RT, viewport,
T#/S#, UBO) and rasterises the triangle in software. Every GPU access goes
through the model's SMMU walk of CB0 in model DRAM, so an address outside the
window faults as it would on the chip; a GOP framebuffer lives in DRAM with
its bootinfo/EFI-map entries and q1n1's P_FB_SHUTDOWN. It checks each
command's order and values, the readbacks, that every CPU write is an arena
write, the framebuffer rows-only mapping and its removal (also after an
interrupted run), every refusal (no cp-fence, a probed SQE, ring pointers
that disagree, no room on the ring, framebuffer overlaps/EFI type/rectangle,
a variant outside the port), a fence that never comes (bounded, then
quiesce), a second cycle after quiesce, the CLI, and the IBs against Mesa's
captured streams (`tools/testdata/a16gpu-p6p7/README.md`).
