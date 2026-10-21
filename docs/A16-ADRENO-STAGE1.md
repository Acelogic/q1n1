# Adreno X2-90, Stage 1: identify the GPU and power its CX side


## On the A16, 2026-09-25 (stage generation 5, udp://en5)

Logs are in `build/a16-gpu/`. The GPU is **identified**.

| Run | What happened |
|---|---|
| `stage1-guard.json` | Guard present, mechanism RAS (ESB + DISR_EL1), no SError pending. |
| `stage1-survey-S2.json` | cmd-db, GIC and GCC read cleanly. The GPU bus clocks at `0x171024`/`0x17102c` read `CLK_OFF`. |
| `stage1-survey-S3.json` | **First GPU clock-controller read on this unit**, clean. CX GDSC on (`0x03d99084` bit 16), SW-controlled; critical and GMU clocks running. |
| `stage1-survey.json` | Survey complete. It stops before CX_MISC/SMMU because `GPU_CC_AHB_CBCR` reads disabled. |
| `stage1-cx-on.json` | Stopped at P3: `GPU_CC_CXO_AON_CBCR` does **not latch CLK_ENABLE** (wrote `0x08000001`, read `0x08000000`), yet `CLK_OFF` is clear before and after. Linux never reads it back. The tool now accepts a critical branch that is running. |
| `stage1-cx-on-2.json` | **First GPU-block read: FREQ_LIMIT `0x184` = 388 → speed bin 1 (1.85 GHz allowed)**. `GPU_CC_AHB_CBCR` does not latch either, so `cx_gmu` can never be proven from it. Writes do land elsewhere: the SMMU vote `0x03d97000` went from 0 to 1. |
| `stage1-cx-on-3.json` | New gate `cx_misc`: a clean FREQ_LIMIT read under the strict guard, the CX GDSC on and SW-controlled, and every GMU clock except AHB running. **Slice mask `0xf` → 4 slices, runtime chip id `0x44070041` (Adreno X2-90), TCM_RET_CNTL 0.** 0 SErrors. |

| `stage1-cx-on-4.json` | `cx_smmu` now also accepts, in place of the unlatchable AHB enable, the GPU AHB path shown by those clean CX_MISC reads. **`stage1_gate_met: true`.** Adreno SMMU v2.4 (SMMU-500): 25 context banks, 25 stream-match groups, S1+S2+nested, 4 KiB pages, sCR0 `0x00270406` (CLIENTPD clear). Every SMR 0 and S2CR `0x00020000` (fault), so nobody is using the GPU. 0 SErrors. |

**Stage 1 gate met.** The accepted departures from the plan are:
- the critical branch `CXO_AON` and the HALT_DELAY `AHB` branch do not latch
  CLK_ENABLE on this SoC;
- their evidence is `CLK_OFF` clear (`CXO_AON`) and clean guarded reads of the
  blocks behind them (`AHB`);
- the summary records `ahb_enable_latched: false`.

If the AHB path had been off, the first CX_MISC identity read would have
raised an SError and the strict guard would have stopped the run
(`test-a16gpu.py` models this). A bus hang is the residual risk; it did not
happen.

Status, 2026-09-25: **tooling only; nothing here has run on the A16 yet.** The
SError guard has run in QEMU (`virt`, `-cpu max` and `-cpu cortex-a72`) and in
the native proxy harness; the GPU tool has run against an offline register and
power model. Every hardware value below is an expectation, not a result.

## What Stage 1 does

`tools/a16gpu.py` drives the GPU subsystem through the proxy in a fixed order,
and each subcommand repeats the read-only steps before it:

| step | domain | what |
| --- | --- | --- |
| S0 | AO | EFI memory map: cmd-db 0x81c60000 still reserved (not read if the map does not describe it); parse gfx/gmxc/mx/cx.lvl, SH0/MC0/ACV |
| S1 | AO | GICD state of INTIDs 0x14c 0x14d 0x150 0x151 0x2c2 0x2c6 |
| S2 | AO | GCC 0x171000 BCR, 0x171004 CFG_AHB, 0x171008, 0x171010 GEMNOC, 0x171024/0x17102c, vote 0x162038 |
| S3 | CX | GPUCC status: CX GDSCR/CFG_GDSCR/HW_CTRL, CBCRs, GMU/HUB/FF RCGs, PLL0 mode |
| P1, P3 | AO/CX | `cx-on` only: GPLL0 votes (P1); CLK_ENABLE on the critical CBCRs CB 0x93a4, CXO_AON 0x9008, RSCC_XO_AON 0x9004, re-read (P3) |
| P5 | CX | `cx-on` only, **before P4**: the shared RCGs GMU 0x9318, HUB 0x93f0 and FF 0x9474 parked on XO (CFG = 0) as `clk_rcg2_shared_init` does at gpucc registration; ROOT_EN released on failure, and the run stops before the CX GDSC |
| P4 | CX | `cx-on` only: CX GDSC (always clear SW_COLLAPSE, poll 0x03d99084 bit 16), RETAIN_FF, re-read |
| S4 | CX | CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS 0x03d9f014: the first GPU-block read, gives the speed bin. Only once the three critical CBCRs read enabled **and** the CX GDSC reads on, not under HW control: `survey` skips it otherwise, `cx-on` reads it right after P4 |
| P6-P7 | CX | `cx-on` only: the GMU clock set in DT order with GCC GEMNOC_GFX, the SMMU vote clock. There is no P2 |
| S5 | CX | slice mask 0x03d9e21c, TCM_RET_CNTL 0x03d9e0e4, SMMU sCR0/IDR0/1/2/7, every SMR/S2CR |

`survey` never writes and reads S5 only when the GPUCC status registers show
that firmware left the CX GDSC and the right clocks on, and the GDSC is not
under HW control (GDSCR bit 1). `cx-on` requires `--write`. No GX register is
touched by either; the tool refuses GX, resets, and any register that
`tools/adreno_x2.py` does not name, before a request is sent. Each register
there carries its block, its domain (AO/CX/GX), the gate that must be proven
first, and the evidence for all three.

**Why S4 waits for the critical CBCRs.** Linux reads FREQ_LIMIT in
`a6xx_gpu_init` (a6xx_gpu.c:2613) with no GPU vote of its own, but always after
gpucc probe has set CLK_ENABLE on CB, CXO_AON and RSCC_XO_AON
(gpucc-glymur.c:563-565): the gpu node's `iommus` needs `adreno_smmu`, which
needs gpucc, so probe order forces it. It is also read while genpd still holds
a CX GDSC that firmware left on (genpd keeps boot-on domains on until
sync_state), so the precedent says nothing about a collapsed GDSC -- and the
A16's HWIO names the block GPU_CX_MISC, not one of the GPU_GMU_AO blocks. The
gate is therefore both (`cx_fuse` in `tools/adreno_x2.py`), and `cx-on` reads
FREQ_LIMIT only after P4.

**Why P5 runs before P4.** `clk_rcg2_shared_init` parks every shared RCG on XO
when gpucc registers its clocks, which is before anything can enable the CX
GDSC: "A GDSC can wedge if is turned on and the RCG is stuck on" (clk-rcg2.c).
Parking writes CFG = 0 only; no frequency-table value is programmed.

**Why HW control withholds S5.** Under HW control the GMU or the RSC may
collapse the CX GDSC between the status read and a CX_MISC or SMMU read, which
would be an access to an unpowered block. `cx-on` stops at P4 in that case
unless `--take-hw-control` is passed; P4 then clears HW_CONTROL as `gdsc_init`
does and, as `gdsc_enable` does, always clears SW_COLLAPSE and polls, because a
GDSC that was on only through HW control starts to collapse the moment
HW_CONTROL goes.

The corrections from the reviews are built in: GPU_CC_AHB is
`BRANCH_HALT_DELAY`, so nothing waits on its CLK_OFF bit; GDSC completion is
CFG_GDSCR bit 16 at 0x03d99084, not GDSCR bit 31; GBIF 0x03d0f000 is CX;
SPTPRAC and the legacy GMU DTCM defines are gone; runtime chip id is
`0x44070001 | popcount(SLICE_ENABLE_FINAL & 0xf) << 4`; TCM_RET_CNTL must be 0.

## The SError guard

**The problem.** q1n1 runs with PSTATE.A set (`entry.S` masks DAIF on entry).
An access to a block whose clock or power is off can end as an SError: a
posted write that errors after the store retired, or an error response the
core reports asynchronously. With A masked the SError is not taken. It stays
pending, and it fires later, whenever something clears A -- a guest kernel
that unmasks it, say -- in code that has nothing to do with the access.

**The mechanism.** The proxy now brackets every request that touches target
memory (the 0x1xx register ops, the 0x2xx memcpy/memset ops, `P_CALL`,
`REQ_MEMREAD`, `REQ_MEMWRITE`) with a poll, right before and right after the
access. The poll is `q1n1_platform_serror_poll()` in `main.c`:

1. `dsb sy; isb`, so the access has completed and any error response is back.
2. With FEAT_RAS (ID_AA64PFR0_EL1.RAS != 0): `esb`. With A masked, ESB turns a
   pending physical SError into a deferred one: the syndrome goes to
   `DISR_EL1` and the SError stops being pending. It is never taken.
3. If `ISR_EL1.A` is still set (no RAS, or an SError ESB cannot synchronise),
   A is cleared for one instruction. The SError is taken there, on purpose, and
   the SError vector records ESR and returns **with SPSR.A set**
   (`q1n1_serror_window_spsr`). Whether an SError source is level-sensitive is
   IMPLEMENTATION DEFINED; one that stays asserted would otherwise be taken
   again after every `eret`, and the core would never leave the window. With A
   set the poll sees it as still pending (`unconsumed`) and the link survives.

A found SError is counted as **before** (already pending when the request
started: raised by earlier work -- another request, the USB driver, a late
posted write) or **during** (appeared across the access). The record
(`struct q1n1_serror_state`, magic `Q1SERR01`) keeps the counts and the last
syndrome, request and address.

**The protocol.** `P_Q1N1_SERROR` (0x5131, outside m1n1's opcode groups):
`a0 = 0` query, `1` zero the counters, `2` set the flags to `a1`. It returns the
record's address; `REQ_MEMREAD` reads it. A read wholly inside the record is
never bracketed: it is plain payload RAM, it is not refused in strict mode, and
reading it neither consumes a pending SError nor overwrites the `last_*` fields
that name the access which raised it. Counting is always on and changes no
reply. With the strict flag, a request that sees an SError fails: a proxy op
returns status `-0x53` and is not performed if the SError came first; a
memory transfer replies `ST_XFRERR`. `q1n1proxy.Proxy.serror_state()`,
`serror_reset()` and `serror_strict()` wrap it; `ProxyRemoteError.status` now
carries the status. An older payload rejects the opcode, which is how
`a16gpu.py` knows to refuse it.

**What it cannot see.** If EL3 routes SErrors to itself (`SCR_EL3.EA = 1`),
they go to the firmware whatever PSTATE.A says, and `DISR_EL1`/`ISR_EL1` read
as zero at EL2. Whether this machine's EL3 does that is not known. A bus hang
-- a read that never completes -- is not an SError either; nothing in the
payload can recover from one.

**Tested.** `tools/test-q1n1-proxy.py`: the harness models a pending SError as
a countdown of platform polls and places one before or during a chosen
request, in both modes, for register ops, memreads and memwrites, and checks
that a refused write did not happen and that the stream stays in step. A
level-sensitive SError (found again on every poll) checks that strict mode
refuses every access but the record read, which still names the access that
raised it. QEMU raises no SErrors, so the window itself is checked in a model
of the core (`test-q1n1-proxy --window`) that runs the payload's own
`q1n1_serror_window_spsr`: a level source is taken once and left pending; the
control (SPSR unchanged) never leaves the window.
`tools/test-uefi.py --proxy`: under QEMU `-cpu max` the payload and both
chainloaded stages report mechanism 2 (RAS), bracket every access and
find nothing; strict mode passes a clean access. The same run under
`-cpu cortex-a72` (no RAS) reports mechanism 1 and passes.

## Tests

```shell
python3 tools/test-a16gpu.py          # register/power model, offline
python3 tools/test-q1n1-proxy.py      # proxy protocol incl. SError bracketing
python3 tools/test-uefi.py --proxy    # QEMU: ESB/DISR/ISR at EL2, stages
```

`test-a16gpu.py` runs every subcommand against a model that turns a GPUCC
access without GCC_GPU_CFG_AHB into a bus hang, FREQ_LIMIT read before the
critical CBCRs are enabled into a bus hang, a CX_MISC or SMMU access while the
CX GDSC is under HW control or collapsing into a bus hang, one without the CX
GDSC and its clocks into an SError, and any GX access into a violation. It
checks that the survey writes nothing and never touches GX, that steps run in
plan order, that every poll stops within its bound and nothing is written after
a failed one, that FREQ_LIMIT is never read with the CX GDSC off, under HW
control or collapsing, that the CX GDSC is never enabled with a shared RCG off
XO, that a still-pending (unconsumed) SError or one arriving after the last
access fails the run, and that an SError before or during an access stops the run at
that access.