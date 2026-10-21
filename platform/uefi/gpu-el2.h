/* SPDX-License-Identifier: MIT */
/* Adreno Stage 3 support in the EL2 payload (docs/A16-ADRENO-STAGE3.md).
 *
 * Two things the host tool (tools/a16gpu.py gmu-smmu .. quiesce) cannot do
 * through plain register and memory requests:
 *
 *  The GPU arena. The GMU runs its code from a 16 MiB "icache" buffer in DDR,
 *  and the SMMU tables, HFI queues, SQE image and ringbuffer all need memory
 *  that nothing else will reuse while the GPU can still DMA into it. The arena
 *  is one physically contiguous region, published in bootinfo
 *  (gpu_arena_base/size/flags) and inherited by every chainloaded stage, so a
 *  chainload never hands it out. It comes from one of two places:
 *    - the ESP payload reserves it before ExitBootServices (EfiLoaderData,
 *      below 4 GiB), source Q1N1_GPU_ARENA_EFI;
 *    - a stage whose ancestry never reserved one claims it on request from
 *      memory the final EFI map calls Conventional (free after EBS; q1n1
 *      itself allocates nothing after EBS), source Q1N1_GPU_ARENA_CLAIMED.
 *  A later guest must treat the range as reserved (Stage 4).
 *
 *  A guarded SMC. TrustZone's GPU init (qcom_scm_gpu_init_regs, SMC function
 *  0x42002801, x1 = arginfo 1, x2 = ALWAYS_EN | TSENSE_EN = 9) has no MMIO
 *  equivalent that is known to work. The payload forwards exactly that call,
 *  and (version 3) the CP SMMU aperture call adreno_hw_init makes before it
 *  starts the SQE (qcom_scm_set_gpu_smmu_aperture(0): 0x42000c1b, arginfo 4,
 *  0xffff0000 for context bank 0, then 0xffffffff three times; the host
 *  passes the first 0xffffffff as x3 and the payload supplies the other two):
 *  any other function id or argument is refused before an SMC is issued. The
 *  proxy brackets the P_CALL with its SError polls, the SMC itself runs under
 *  Q1N1_GUARD_SKIP so an UNDEFINED SMC (SCR_EL3.SMD) is skipped and reported,
 *  and the Qualcomm INTERRUPTED/EBUSY replies are retried a bounded number of
 *  times (qcom_scm-smc.c). WAITQ_SLEEP needs an interrupt q1n1 does not take,
 *  so it is reported, not serviced. Linux resumes INTERRUPTED without a bound;
 *  giving up (Q1N1_GPU_EINTR) or WAITQ leaves the call pending in TrustZone,
 *  so the host tool refuses a fresh SMC after either until the A16 reboots.
 *
 *  A line-safe store. The arena is Write-Back cacheable in the firmware's EL2
 *  identity map and the GMU is not coherent with it. The HFI queue headers put
 *  words the host writes (cmd write_index, rsp read_index) in the same 64-byte
 *  line as words the GMU writes (cmd read_index, rsp write_index). A proxy
 *  write followed by a separate clean holds a dirty copy of that line for a
 *  whole round trip, and the clean then writes back the GMU's older words over
 *  its newer ones. Q1N1_GPU_STORE32 does clean+invalidate, the 32-bit store and
 *  clean+invalidate back to back on the target, so the line is dirty for the
 *  time of one store and one line fill, not for a USB round trip.
 *
 *  A fresh arena. Every new arena (the ESP reservation and a claim) is zeroed
 *  and cleaned to the point of coherency before it is published. DRAM keeps
 *  its contents over a warm reset, so without this a record the host tool
 *  left in the arena last boot (TrustZone init done, a boot token, ring
 *  pointers) would be read as this boot's.
 *
 * Entry: bootinfo gpu_call, reached with P_CALL (x0 = op). Record: gpu_state. */
#pragma once
#include <stdint.h>

#define Q1N1_GPU_MAGIC UINT64_C(0x31555047314e3151)   /* "Q1N1GPU1" */
#define Q1N1_GPU_VERSION 3   /* 2: Q1N1_GPU_STORE32; 3: the CP SMMU aperture SMC */
#define Q1N1_GPU_ARENA_BYTES (UINT64_C(32) << 20)
#define Q1N1_GPU_RELEASE_KEY UINT64_C(0x455341454c455247)  /* "GRELEASE" */

/* gpu_call op (x0); arguments in x1..x4. */
#define Q1N1_GPU_QUERY 0     /* returns the record address */
#define Q1N1_GPU_CLAIM 1     /* x1 size, x2 exclusive upper limit (0: none), x3 alignment (0: 2 MiB) */
#define Q1N1_GPU_SMC 2       /* x1 function id, x2..x4 SMC x1..x3; result in the record */
#define Q1N1_GPU_RELEASE 3   /* x1 = Q1N1_GPU_RELEASE_KEY: forget a claimed arena (host asserts quiesced) */
#define Q1N1_GPU_STORE32 4   /* x1 physical address (4-aligned, inside the arena), x2 value: see below */

/* gpu_arena_flags / arena_source */
#define Q1N1_GPU_ARENA_NONE 0
#define Q1N1_GPU_ARENA_EFI 1
#define Q1N1_GPU_ARENA_CLAIMED 2

/* Statuses (negative, returned in x0). 0 means done; for Q1N1_GPU_SMC, done
 * means an SMC ran and TrustZone's x0..x3 are in the record. */
#define Q1N1_GPU_EINVAL (-1)
#define Q1N1_GPU_ENOMAP (-2)     /* no usable EFI memory map in bootinfo */
#define Q1N1_GPU_ENOSPACE (-3)   /* no Conventional region fits */
#define Q1N1_GPU_ECONFLICT (-4)  /* an arena of another size already exists */
#define Q1N1_GPU_EATTR (-5)      /* the EL2 tables map it as Device (or not at all) */
#define Q1N1_GPU_EREFUSED (-6)   /* SMC function or arguments not allow-listed */
#define Q1N1_GPU_EUNDEF (-7)     /* the SMC instruction trapped (UNDEFINED at EL2) */
#define Q1N1_GPU_EWAITQ (-8)     /* TrustZone asked to sleep on a wait queue */
#define Q1N1_GPU_EBUSY (-9)      /* still EBUSY after the retries */
#define Q1N1_GPU_EINTR (-10)     /* still INTERRUPTED after the resumes */
#define Q1N1_GPU_EKEY (-11)      /* release without the key, or of an EFI arena */

/* The SMCs this payload forwards (qcom_scm.c qcom_scm_gpu_init_regs and
 * qcom_scm_set_gpu_smmu_aperture). */
#define Q1N1_SMC_GPU_INIT_REGS UINT64_C(0x42002801)
#define Q1N1_SMC_GPU_ARGINFO 1u
#define Q1N1_SMC_GPU_REQ_MASK 0x0fu  /* QCOM_SCM_GPU_{ALWAYS_EN,..,TSENSE_EN}_REQ BIT(0)..BIT(3) */
#define Q1N1_SMC_CP_APERTURE UINT64_C(0x42000c1b)   /* QCOM_SCM_SVC_MP, QCOM_SCM_MP_CP_SMMU_APERTURE_ID */
#define Q1N1_SMC_CP_APERTURE_ARGINFO 4u               /* QCOM_SCM_ARGS(4) */
#define Q1N1_SMC_CP_APERTURE_CB0 UINT64_C(0xffff0000) /* 0xffff0000 | (CP_APERTURE_REG 0 << 8) | cb 0 */
#define Q1N1_SMC_CP_APERTURE_FILL UINT64_C(0xffffffff)
#define Q1N1_SMC_INTERRUPTED 1
#define Q1N1_SMC_WAITQ_SLEEP 2
#define Q1N1_SMC_EBUSY (-12)
#define Q1N1_SMC_RESUMES 1024   /* Linux resumes without a bound; 1024 is far past any real run */
#define Q1N1_SMC_BUSY_RETRIES 20
#define Q1N1_SMC_BUSY_WAIT_US 30000u

struct q1n1_gpu_state {
    uint64_t magic, version, size;
    uint64_t arena_base, arena_size, arena_source, arena_attr;
    uint64_t claims, claim_status;
    uint64_t smc_calls, smc_refused, smc_ok, smc_status;
    uint64_t smc_fid, smc_x1, smc_x2, smc_x3;
    uint64_t smc_res0, smc_res1, smc_res2, smc_res3;
    uint64_t smc_interrupted, smc_busy, smc_undefined;
};
_Static_assert(sizeof(struct q1n1_gpu_state) == 24 * 8, "gpu state layout (tools/a16gpu_stage3.py)");

/* What the payload provides. Everything is optional except the memory map for
 * a claim and smc for an SMC; the native test fills it with fakes. */
struct q1n1_gpu_platform {
    uint64_t memory_map, memory_map_size, memory_map_stride;
    /* q1n1's own regions [base, base+size): a claim must not overlap them. */
    const uint64_t (*exclude)[2];
    unsigned exclude_count;
    /* MAIR attribute byte the EL2 tables give phys; nonzero return = unmapped. */
    int (*attr)(uint64_t phys, uint32_t *attr);
    void (*delay_us)(uint64_t microseconds);
    /* Issue an SMC with x0..x7 = regs[0..7]; returns x0..x7 in place. Returns
     * nonzero when the instruction trapped instead of reaching EL3. */
    int (*smc)(uint64_t regs[8]);
    /* Q1N1_GPU_STORE32: DC CIVAC, the store, DC CIVAC (with barriers) on
     * phys. The address is already checked to be inside the arena. */
    void (*store32)(uint64_t phys, uint32_t value);
    /* A new claim: zero [phys, phys+size) and clean+invalidate it to the point
     * of coherency, so no stale page table, queue header or host record from
     * an earlier boot survives in the arena, and no dirty line of zeros is
     * left to be evicted over something the GPU writes later. */
    void (*zero)(uint64_t phys, uint64_t size);
    /* bootinfo fields to publish into (gpu_arena_base, _size, _flags). */
    uint64_t *boot_base, *boot_size, *boot_flags;
};

/* Adopt the inherited arena (bootinfo) and remember the platform. */
void q1n1_gpu_init(const struct q1n1_gpu_platform *platform);
const struct q1n1_gpu_state *q1n1_gpu_state_record(void);
uint64_t q1n1_gpu_call(uint64_t op, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4);

/* Pure helpers, exposed for tools/test-q1n1-gpu.c. */
int q1n1_gpu_smc_allowed(uint64_t fid, uint64_t x1, uint64_t x2, uint64_t x3);
int q1n1_gpu_pick(uint64_t map, uint64_t map_size, uint64_t stride, uint64_t size, uint64_t limit,
                  uint64_t align, const uint64_t (*exclude)[2], unsigned exclude_count, uint64_t *base);
