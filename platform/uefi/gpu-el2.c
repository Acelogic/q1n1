/* SPDX-License-Identifier: MIT */
/* Adreno Stage 3: the GPU arena and the allow-listed TrustZone GPU init SMC.
 * See gpu-el2.h for what and why. No libc: this links into the PE payload,
 * the flat stages and the native test alike. */
#include "gpu-el2.h"

#define EFI_CONVENTIONAL 7u
#define PAGE 4096u
#define DEFAULT_ALIGN (UINT64_C(2) << 20)

static struct q1n1_gpu_state state = {
    .magic = Q1N1_GPU_MAGIC, .version = Q1N1_GPU_VERSION, .size = sizeof(struct q1n1_gpu_state),
};
static struct q1n1_gpu_platform platform;

static uint64_t rd64(uint64_t address)
{
    const volatile uint8_t *p = (const volatile uint8_t *)(uintptr_t)address;
    uint64_t v = 0;
    for (unsigned n = 0; n < 8; n++) v |= (uint64_t)p[n] << (8 * n);
    return v;
}
static uint32_t rd32(uint64_t address)
{
    const volatile uint8_t *p = (const volatile uint8_t *)(uintptr_t)address;
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static int overlaps(uint64_t a, uint64_t a_size, uint64_t b, uint64_t b_size)
{
    return a_size && b_size && a < b + b_size && b < a + a_size;
}

int q1n1_gpu_smc_allowed(uint64_t fid, uint64_t x1, uint64_t x2, uint64_t x3)
{
    /* qcom_scm_gpu_init_regs: one value argument (QCOM_SCM_ARGS(1) = 1), a
     * request mask built from the QCOM_SCM_GPU_*_REQ bits, nothing else. */
    if (fid == Q1N1_SMC_GPU_INIT_REGS)
        return x1 == Q1N1_SMC_GPU_ARGINFO && x2 && !(x2 & ~(uint64_t)Q1N1_SMC_GPU_REQ_MASK) && x3 == 0;
    /* qcom_scm_set_gpu_smmu_aperture(0), exactly as adreno_hw_init calls it:
     * four value arguments, context bank 0, the three fill words (x4/x5 are
     * supplied by smc_regs, never by the host). */
    if (fid == Q1N1_SMC_CP_APERTURE)
        return x1 == Q1N1_SMC_CP_APERTURE_ARGINFO && x2 == Q1N1_SMC_CP_APERTURE_CB0 && x3 == Q1N1_SMC_CP_APERTURE_FILL;
    return 0;
}

/* x0..x7 for an allowed call: the aperture call's last two arguments are
 * fixed fill words; everything past the arguments is 0. */
static void smc_regs(uint64_t r[8], uint64_t fid, uint64_t x1, uint64_t x2, uint64_t x3)
{
    r[0] = fid; r[1] = x1; r[2] = x2; r[3] = x3;
    r[4] = r[5] = fid == Q1N1_SMC_CP_APERTURE ? Q1N1_SMC_CP_APERTURE_FILL : 0;
    r[6] = r[7] = 0;
}

/* The highest aligned window of `size` bytes inside one Conventional
 * descriptor, below `limit`, clear of every excluded range. Highest, because
 * payloads and guests are conventionally loaded low. */
int q1n1_gpu_pick(uint64_t map, uint64_t map_size, uint64_t stride, uint64_t size, uint64_t limit,
                  uint64_t align, const uint64_t (*exclude)[2], unsigned exclude_count, uint64_t *base)
{
    if (!map || !map_size || stride < 40 || map_size / stride > 65536) return Q1N1_GPU_ENOMAP;
    if (!size || (size & (PAGE - 1)) || !align || (align & (align - 1)) || align < PAGE) return Q1N1_GPU_EINVAL;
    uint64_t best = 0;
    int found = 0;
    for (uint64_t at = 0; at + stride <= map_size; at += stride) {
        uint64_t d = map + at;
        if (rd32(d) != EFI_CONVENTIONAL) continue;
        uint64_t start = rd64(d + 8), pages = rd64(d + 24);
        if (pages > (UINT64_C(1) << 36) || start & (PAGE - 1)) continue;
        uint64_t end = start + pages * PAGE;
        if (limit && end > limit) end = limit;
        if (end <= start || end - start < size) continue;
        /* Walk down from the top; step past an excluded range that is hit. */
        uint64_t candidate = (end - size) & ~(align - 1);
        while (candidate >= start && candidate + size <= end) {
            uint64_t clash = 0;
            for (unsigned n = 0; n < exclude_count; n++)
                if (overlaps(candidate, size, exclude[n][0], exclude[n][1])) {
                    clash = exclude[n][0];
                    break;
                }
            if (!clash) {
                if (!found || candidate > best) best = candidate;
                found = 1;
                break;
            }
            if (clash < size) break;
            uint64_t next = (clash - size) & ~(align - 1);
            if (next >= candidate) break;
            candidate = next;
        }
    }
    if (!found) return Q1N1_GPU_ENOSPACE;
    *base = best;
    return 0;
}

static void publish(void)
{
    if (platform.boot_base) *platform.boot_base = state.arena_base;
    if (platform.boot_size) *platform.boot_size = state.arena_size;
    if (platform.boot_flags) *platform.boot_flags = state.arena_source;
}

void q1n1_gpu_init(const struct q1n1_gpu_platform *p)
{
    platform = *p;
    /* A chainloaded stage inherits the arena through bootinfo. */
    if (p->boot_base && p->boot_size && *p->boot_base && *p->boot_size) {
        state.arena_base = *p->boot_base;
        state.arena_size = *p->boot_size;
        state.arena_source = p->boot_flags && *p->boot_flags ? *p->boot_flags : Q1N1_GPU_ARENA_EFI;
    }
    publish();
}

const struct q1n1_gpu_state *q1n1_gpu_state_record(void) { return &state; }

static int64_t claim(uint64_t size, uint64_t limit, uint64_t align)
{
    state.claims++;
    if (!size) size = Q1N1_GPU_ARENA_BYTES;
    if (!align) align = DEFAULT_ALIGN;
    if (state.arena_base) {
        /* Idempotent: the same request finds the arena it already has. */
        return state.arena_size >= size ? 0 : Q1N1_GPU_ECONFLICT;
    }
    uint64_t base = 0;
    int status = q1n1_gpu_pick(platform.memory_map, platform.memory_map_size, platform.memory_map_stride, size, limit,
                               align, platform.exclude, platform.exclude_count, &base);
    if (status) return status;
    if (platform.attr) {
        /* The first and last page must be Normal memory in the EL2 tables: the
         * host fills it through the proxy and cleans it to the point of
         * coherency for a non-coherent GPU. */
        uint32_t first = 0, last = 0;
        if (platform.attr(base, &first) || platform.attr(base + size - PAGE, &last)) return Q1N1_GPU_EATTR;
        if (!(first & 0xf0) || !(last & 0xf0)) return Q1N1_GPU_EATTR;
        state.arena_attr = first;
    }
    if (platform.zero) platform.zero(base, size);   /* nothing from an earlier boot is read as this one's */
    state.arena_base = base;
    state.arena_size = size;
    state.arena_source = Q1N1_GPU_ARENA_CLAIMED;
    publish();
    return 0;
}

static int64_t smc(uint64_t fid, uint64_t x1, uint64_t x2, uint64_t x3)
{
    state.smc_fid = fid; state.smc_x1 = x1; state.smc_x2 = x2; state.smc_x3 = x3;
    if (!q1n1_gpu_smc_allowed(fid, x1, x2, x3)) {
        state.smc_refused++;
        return Q1N1_GPU_EREFUSED;
    }
    if (!platform.smc) return Q1N1_GPU_EINVAL;
    state.smc_calls++;
    uint64_t r[8];
    smc_regs(r, fid, x1, x2, x3);
    unsigned resumes = 0, busy = 0;
    for (;;) {
        uint64_t in[8];
        for (unsigned n = 0; n < 8; n++) in[n] = r[n];
        if (platform.smc(r)) {
            state.smc_undefined++;
            return Q1N1_GPU_EUNDEF;
        }
        state.smc_res0 = r[0]; state.smc_res1 = r[1]; state.smc_res2 = r[2]; state.smc_res3 = r[3];
        if (r[0] == Q1N1_SMC_INTERRUPTED) {
            /* __scm_smc_do_quirk: re-issue with x0 = the reply and x6 = the
             * session TrustZone returned (ARM_SMCCC_QUIRK_QCOM_A6). */
            state.smc_interrupted++;
            if (++resumes > Q1N1_SMC_RESUMES) return Q1N1_GPU_EINTR;
            uint64_t session = r[6];
            for (unsigned n = 0; n < 8; n++) r[n] = in[n];
            r[0] = Q1N1_SMC_INTERRUPTED;
            r[6] = session;
            continue;
        }
        if (r[0] == Q1N1_SMC_WAITQ_SLEEP) return Q1N1_GPU_EWAITQ;
        if ((int64_t)r[0] == Q1N1_SMC_EBUSY) {
            state.smc_busy++;
            if (++busy > Q1N1_SMC_BUSY_RETRIES) return Q1N1_GPU_EBUSY;
            if (platform.delay_us) platform.delay_us(Q1N1_SMC_BUSY_WAIT_US);
            smc_regs(r, fid, x1, x2, x3);
            continue;
        }
        if (!r[0]) state.smc_ok++;
        return 0;
    }
}

uint64_t q1n1_gpu_call(uint64_t op, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
{
    int64_t status;
    switch (op) {
    case Q1N1_GPU_QUERY:
        return (uintptr_t)&state;
    case Q1N1_GPU_CLAIM:
        status = claim(a1, a2, a3);
        state.claim_status = (uint64_t)status;
        return (uint64_t)status;
    case Q1N1_GPU_SMC:
        status = smc(a1, a2, a3, a4);
        state.smc_status = (uint64_t)status;
        return (uint64_t)status;
    case Q1N1_GPU_RELEASE:
        if (a1 != Q1N1_GPU_RELEASE_KEY || state.arena_source != Q1N1_GPU_ARENA_CLAIMED)
            return (uint64_t)(int64_t)Q1N1_GPU_EKEY;
        state.arena_base = state.arena_size = state.arena_source = state.arena_attr = 0;
        publish();
        return 0;
    case Q1N1_GPU_STORE32:
        /* Only a word inside the arena: nothing else is the GPU's to share. */
        if (!platform.store32 || !state.arena_base || state.arena_size < 4 || (a1 & 3) || a1 < state.arena_base
            || a1 - state.arena_base > state.arena_size - 4 || a2 > UINT32_MAX)
            return (uint64_t)(int64_t)Q1N1_GPU_EINVAL;
        platform.store32(a1, (uint32_t)a2);
        return 0;
    default:
        return (uint64_t)(int64_t)Q1N1_GPU_EINVAL;
    }
}
