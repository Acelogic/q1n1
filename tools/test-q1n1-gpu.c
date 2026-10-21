/* SPDX-License-Identifier: MIT */
/* Native test for platform/uefi/gpu-el2.c: the GPU arena claim and the
 * allow-listed TrustZone GPU init SMC, against a fake EFI memory map and a
 * fake secure monitor. No hardware.
 *
 *   clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror \
 *     -Iplatform/uefi tools/test-q1n1-gpu.c platform/uefi/gpu-el2.c -o /tmp/test-q1n1-gpu && /tmp/test-q1n1-gpu
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gpu-el2.h"

static unsigned checks;
#define CHECK(cond, label) do { if (!(cond)) { fprintf(stderr, "FAIL: %s (%s:%d)\n", label, __FILE__, __LINE__); exit(1); } checks++; } while (0)

/* ---- fake EFI memory map (UEFI 2.x descriptors, 48-byte stride as on the A16) ---- */
#define STRIDE 48
static uint8_t map[STRIDE * 16];
static unsigned map_count;
static void put64(uint8_t *p, uint64_t v) { for (unsigned n = 0; n < 8; n++) p[n] = (uint8_t)(v >> (8 * n)); }
static void add(uint32_t type, uint64_t base, uint64_t pages)
{
    uint8_t *d = map + STRIDE * map_count++;
    memset(d, 0, STRIDE);
    d[0] = (uint8_t)type;
    put64(d + 8, base);
    put64(d + 24, pages);
}

/* ---- fake secure monitor ---- */
static unsigned smc_calls, trap_next, interrupted_left, busy_left, waitq;
static uint64_t last_in[8], session = 0x5e55;
static uint64_t reply0;
static int fake_smc(uint64_t r[8])
{
    smc_calls++;
    memcpy(last_in, r, sizeof(last_in));
    if (trap_next) { trap_next--; return 1; }
    if (interrupted_left) {
        interrupted_left--;
        r[0] = Q1N1_SMC_INTERRUPTED;
        r[6] = session;
        return 0;
    }
    if (busy_left) { busy_left--; r[0] = (uint64_t)(int64_t)Q1N1_SMC_EBUSY; return 0; }
    if (waitq) { r[0] = Q1N1_SMC_WAITQ_SLEEP; return 0; }
    r[0] = reply0; r[1] = 0x11; r[2] = 0x22; r[3] = 0x33;
    return 0;
}
static uint64_t delayed_us;
static void fake_delay(uint64_t us) { delayed_us += us; }
static int device_at = -1;
static int fake_attr(uint64_t phys, uint32_t *attr)
{
    *attr = device_at >= 0 && phys >= (uint64_t)device_at * 0x10000000ull ? 0x04 : 0xff;
    return 0;
}

static unsigned stores;
static uint64_t store_at;
static uint32_t store_value;
static void fake_store32(uint64_t phys, uint32_t value) { stores++; store_at = phys; store_value = value; }
static unsigned zeroes;
static uint64_t zero_at, zero_size;
static void fake_zero(uint64_t phys, uint64_t size) { zeroes++; zero_at = phys; zero_size = size; }

static uint64_t boot_base, boot_size, boot_flags;
static uint64_t exclude[2][2];
static void setup(int with_smc)
{
    struct q1n1_gpu_platform p = {
        .memory_map = (uintptr_t)map, .memory_map_size = STRIDE * map_count, .memory_map_stride = STRIDE,
        .exclude = (const uint64_t (*)[2])exclude, .exclude_count = 2, .attr = fake_attr, .delay_us = fake_delay,
        .smc = with_smc ? fake_smc : NULL, .store32 = fake_store32, .zero = fake_zero, .boot_base = &boot_base, .boot_size = &boot_size, .boot_flags = &boot_flags,
    };
    q1n1_gpu_init(&p);
}
static int64_t call(uint64_t op, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
{
    return (int64_t)q1n1_gpu_call(op, a1, a2, a3, a4);
}

static void test_allow_list(void)
{
    CHECK(q1n1_gpu_smc_allowed(0x42002801, 1, 9, 0), "GPU init with ALWAYS_EN|TSENSE_EN is allowed");
    CHECK(q1n1_gpu_smc_allowed(0x42002801, 1, 1, 0), "GPU init with ALWAYS_EN alone is allowed");
    const uint64_t other[] = {0x42000c1b, 0x42000205, 0x84000000, 0xc4000003, 0x42002802, 0xc2002801, 0x02002801,
                              0x42000c1c, 0xc2000c1b};
    for (unsigned n = 0; n < sizeof(other) / sizeof(other[0]); n++)
        CHECK(!q1n1_gpu_smc_allowed(other[n], 1, 9, 0), "any other function id (or arguments) is refused");
    /* qcom_scm_set_gpu_smmu_aperture(0) as adreno_hw_init issues it, and nothing near it. */
    CHECK(q1n1_gpu_smc_allowed(0x42000c1b, 4, 0xffff0000, 0xffffffff), "the CP aperture call for CB0 is allowed");
    CHECK(!q1n1_gpu_smc_allowed(0x42000c1b, 4, 0xffff0001, 0xffffffff), "the aperture for another bank is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42000c1b, 4, 0xffff0100, 0xffffffff), "another aperture register is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42000c1b, 3, 0xffff0000, 0xffffffff), "the aperture with other arginfo is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42000c1b, 4, 0xffff0000, 0), "the aperture without the fill word is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42000c1b, 4, 0, 0), "the aperture with zero arguments is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42002801, 2, 9, 0), "arginfo other than one value argument is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42002801, 1, 0, 0), "an empty request mask is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42002801, 1, 0x19, 0), "a request bit outside QCOM_SCM_GPU_*_REQ is refused");
    CHECK(!q1n1_gpu_smc_allowed(0x42002801, 1, 9, 1), "a second argument is refused");
}

static void test_smc(void)
{
    map_count = 0;
    setup(1);
    const struct q1n1_gpu_state *s = q1n1_gpu_state_record();
    CHECK(s->magic == Q1N1_GPU_MAGIC && s->size == sizeof(*s), "record magic and size");
    CHECK((uintptr_t)s == q1n1_gpu_call(Q1N1_GPU_QUERY, 0, 0, 0, 0), "query returns the record");

    smc_calls = 0;
    CHECK(call(Q1N1_GPU_SMC, 0x42000c1b, 4, 0xffff0000, 0) == Q1N1_GPU_EREFUSED && !smc_calls && s->smc_refused == 1,
          "a refused call never reaches the monitor");

    reply0 = 0;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == 0 && smc_calls == 1, "the GPU init SMC runs once");
    CHECK(last_in[0] == 0x42002801 && last_in[1] == 1 && last_in[2] == 9 && !last_in[3] && !last_in[6],
          "x0..x3 as qcom_scm_gpu_init_regs, x6 (session) zero");
    CHECK(s->smc_ok == 1 && s->smc_res0 == 0 && s->smc_res1 == 0x11 && s->smc_res3 == 0x33 && s->smc_status == 0,
          "TrustZone's reply is recorded");

    reply0 = (uint64_t)-3;   /* e.g. -EINVAL from TZ: recorded, the call still "ran" */
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == 0 && s->smc_res0 == (uint64_t)-3 && s->smc_ok == 1,
          "a TrustZone error is TrustZone's to report: the call ran, the record says what came back");

    reply0 = 0;
    smc_calls = 0;
    interrupted_left = 3;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == 0 && smc_calls == 4 && s->smc_interrupted == 3,
          "INTERRUPTED is resumed");
    CHECK(last_in[0] == Q1N1_SMC_INTERRUPTED && last_in[6] == session && last_in[2] == 9,
          "a resume re-issues with x0 = 1 and TrustZone's session in x6 (QCOM_A6 quirk)");

    smc_calls = 0;
    interrupted_left = Q1N1_SMC_RESUMES + 100;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == Q1N1_GPU_EINTR && smc_calls == Q1N1_SMC_RESUMES + 1,
          "resumes are bounded");
    interrupted_left = 0;

    smc_calls = 0;
    delayed_us = 0;
    busy_left = 2;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == 0 && smc_calls == 3 && delayed_us == 2 * Q1N1_SMC_BUSY_WAIT_US,
          "EBUSY is retried after the qcom_scm 30 ms wait");
    CHECK(last_in[0] == 0x42002801 && last_in[2] == 9, "a busy retry re-issues the original call");
    busy_left = 1000;
    smc_calls = 0;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == Q1N1_GPU_EBUSY && smc_calls == Q1N1_SMC_BUSY_RETRIES + 1,
          "busy retries are bounded");
    busy_left = 0;

    waitq = 1;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == Q1N1_GPU_EWAITQ, "WAITQ_SLEEP is reported, not serviced");
    waitq = 0;

    trap_next = 1;
    uint64_t undefined = s->smc_undefined;
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == Q1N1_GPU_EUNDEF && s->smc_undefined == undefined + 1,
          "an SMC that traps (UNDEFINED at EL2) is reported");

    trap_next = 0;
    CHECK(s->version == 3, "record version 3 (the aperture SMC)");
    reply0 = 0;
    smc_calls = 0;
    CHECK(call(Q1N1_GPU_SMC, 0x42000c1b, 4, 0xffff0000, 0xffffffff) == 0 && smc_calls == 1 && s->smc_status == 0,
          "the CP aperture SMC runs");
    CHECK(last_in[0] == 0x42000c1b && last_in[1] == 4 && last_in[2] == 0xffff0000 && last_in[3] == 0xffffffff
          && last_in[4] == 0xffffffff && last_in[5] == 0xffffffff && !last_in[6] && !last_in[7],
          "x0..x5 as qcom_scm_set_gpu_smmu_aperture(0): the payload supplies the last two fill words");
    busy_left = 1;
    CHECK(call(Q1N1_GPU_SMC, 0x42000c1b, 4, 0xffff0000, 0xffffffff) == 0 && smc_calls == 3
          && last_in[4] == 0xffffffff && last_in[5] == 0xffffffff, "an EBUSY retry keeps the fill words");
    interrupted_left = 1;
    CHECK(call(Q1N1_GPU_SMC, 0x42000c1b, 4, 0xffff0000, 0xffffffff) == 0 && smc_calls == 5
          && last_in[0] == Q1N1_SMC_INTERRUPTED && last_in[4] == 0xffffffff && last_in[6] == session,
          "an INTERRUPTED resume re-issues the aperture call with its fill words");
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == 0 && !last_in[4] && !last_in[5],
          "the GPU init SMC after it carries no fill words");

    setup(0);
    CHECK(call(Q1N1_GPU_SMC, 0x42002801, 1, 9, 0) == Q1N1_GPU_EINVAL, "no SMC path: refused");
}

static void test_claim(void)
{
    map_count = 0;
    add(7, 0x80000000, 0x1000);            /* 16 MiB: too small */
    add(2, 0x81000000, 0x10000);           /* LoaderData: never taken */
    add(7, 0x90000000, 0x10000);           /* 256 MiB, top half excluded below */
    add(0, 0xa0000000, 0x10000);           /* Reserved */
    add(7, 0x880000000, 0x100000);         /* 4 GiB above 32 GiB: over the limit */
    exclude[0][0] = 0x98000000; exclude[0][1] = 0x08000000;   /* e.g. a stage slot */
    exclude[1][0] = 0; exclude[1][1] = 0;
    boot_base = boot_size = boot_flags = 0;
    device_at = -1;
    setup(1);
    const struct q1n1_gpu_state *s = q1n1_gpu_state_record();
    CHECK(!s->arena_base, "no inherited arena");

    uint64_t base = 0;
    CHECK(q1n1_gpu_pick((uintptr_t)map, STRIDE * map_count, STRIDE, 32 << 20, 0x100000000ull, 2 << 20,
                        (const uint64_t (*)[2])exclude, 2, &base) == 0 && base == 0x96000000,
          "the highest 2 MiB-aligned window of Conventional memory below 4 GiB, clear of exclusions");
    CHECK(q1n1_gpu_pick((uintptr_t)map, STRIDE * map_count, STRIDE, 32 << 20, 0, 2 << 20,
                        (const uint64_t (*)[2])exclude, 2, &base) == 0 && base == 0x97e000000ull,
          "without a limit the region above 32 GiB wins");
    CHECK(q1n1_gpu_pick((uintptr_t)map, STRIDE * map_count, STRIDE, 512ull << 20, 0x100000000ull, 2 << 20,
                        (const uint64_t (*)[2])exclude, 2, &base) == Q1N1_GPU_ENOSPACE, "nothing fits: ENOSPACE");
    CHECK(q1n1_gpu_pick((uintptr_t)map, STRIDE * map_count, STRIDE, 32 << 20, 0, 3 << 20,
                        (const uint64_t (*)[2])exclude, 2, &base) == Q1N1_GPU_EINVAL, "a bad alignment: EINVAL");
    CHECK(q1n1_gpu_pick(0, 0, STRIDE, 32 << 20, 0, 2 << 20, NULL, 0, &base) == Q1N1_GPU_ENOMAP, "no map: ENOMAP");

    zeroes = 0;
    CHECK(call(Q1N1_GPU_CLAIM, 32 << 20, 0x100000000ull, 0, 0) == 0 && s->arena_base == 0x96000000
          && s->arena_size == 32u << 20 && s->arena_source == Q1N1_GPU_ARENA_CLAIMED && s->arena_attr == 0xff,
          "claim: base, size, source and the EL2 attribute are recorded");
    CHECK(zeroes == 1 && zero_at == 0x96000000 && zero_size == 32u << 20,
          "claim: the new arena is zeroed and cleaned before it is published (no record from a warm reset)");
    CHECK(boot_base == 0x96000000 && boot_size == 32u << 20 && boot_flags == Q1N1_GPU_ARENA_CLAIMED,
          "claim: published into bootinfo for the next stage");
    CHECK(call(Q1N1_GPU_CLAIM, 32 << 20, 0x100000000ull, 0, 0) == 0 && s->arena_base == 0x96000000 && zeroes == 1,
          "claim is idempotent (and does not zero the arena in use)");
    CHECK(call(Q1N1_GPU_CLAIM, 64 << 20, 0, 0, 0) == Q1N1_GPU_ECONFLICT, "a larger second claim is a conflict");

    CHECK(call(Q1N1_GPU_RELEASE, 0, 0, 0, 0) == Q1N1_GPU_EKEY && s->arena_base, "release needs the key");
    CHECK(call(Q1N1_GPU_RELEASE, Q1N1_GPU_RELEASE_KEY, 0, 0, 0) == 0 && !s->arena_base && !boot_base,
          "release with the key forgets the claim everywhere");

    device_at = 9;                          /* 0x90000000 and up read as Device */
    CHECK(call(Q1N1_GPU_CLAIM, 32 << 20, 0x100000000ull, 0, 0) == Q1N1_GPU_EATTR && !s->arena_base && !boot_base
          && zeroes == 1, "a Device-mapped window is refused, nothing is written or published");
    device_at = -1;

    /* A chainloaded stage adopts what bootinfo carries. */
    boot_base = 0x87000000; boot_size = 32u << 20; boot_flags = Q1N1_GPU_ARENA_EFI;
    setup(1);
    CHECK(s->arena_base == 0x87000000 && s->arena_source == Q1N1_GPU_ARENA_EFI, "an inherited arena is adopted");
    CHECK(call(Q1N1_GPU_CLAIM, 32 << 20, 0, 0, 0) == 0 && s->arena_base == 0x87000000 && zeroes == 1,
          "a claim finds the inherited arena and leaves its contents alone");
    CHECK(call(Q1N1_GPU_RELEASE, Q1N1_GPU_RELEASE_KEY, 0, 0, 0) == Q1N1_GPU_EKEY && s->arena_base == 0x87000000,
          "the ESP's EFI arena is never released");
    CHECK(call(99, 0, 0, 0, 0) == Q1N1_GPU_EINVAL, "an unknown op is refused");
}

static void test_store32(void)
{
    boot_base = 0x87000000; boot_size = 32u << 20; boot_flags = Q1N1_GPU_ARENA_EFI;
    setup(1);
    const struct q1n1_gpu_state *s = q1n1_gpu_state_record();
    CHECK(s->version >= 2, "record version 2+ carries Q1N1_GPU_STORE32");
    stores = 0;
    CHECK(call(Q1N1_GPU_STORE32, 0x87000000 + 0x100044, 0x12345678, 0, 0) == 0 && stores == 1
          && store_at == 0x87100044 && store_value == 0x12345678, "a word inside the arena is stored");
    CHECK(call(Q1N1_GPU_STORE32, 0x87000000 + (32u << 20) - 4, 1, 0, 0) == 0 && stores == 2, "the last word is inside");
    CHECK(call(Q1N1_GPU_STORE32, 0x87000000 + (32u << 20), 1, 0, 0) == Q1N1_GPU_EINVAL, "one past the end is refused");
    CHECK(call(Q1N1_GPU_STORE32, 0x87000000 + (32u << 20) - 2, 1, 0, 0) == Q1N1_GPU_EINVAL, "a straddling word is refused");
    CHECK(call(Q1N1_GPU_STORE32, 0x86fffffc, 1, 0, 0) == Q1N1_GPU_EINVAL, "below the arena is refused");
    CHECK(call(Q1N1_GPU_STORE32, 0x87000002, 1, 0, 0) == Q1N1_GPU_EINVAL, "an unaligned word is refused");
    CHECK(call(Q1N1_GPU_STORE32, 0x87000000, UINT64_C(1) << 32, 0, 0) == Q1N1_GPU_EINVAL, "a value over 32 bits is refused");
    CHECK(call(Q1N1_GPU_STORE32, UINT64_C(0xffffffffffffff00), 1, 0, 0) == Q1N1_GPU_EINVAL, "a wrapping address is refused");
    CHECK(stores == 2, "no refused request reached the store");
    boot_base = boot_size = boot_flags = 0;
}

int main(void)
{
    test_allow_list();
    test_smc();
    test_claim();
    test_store32();
    printf("PASS: %u q1n1 GPU arena/SMC checks (native)\n", checks);
    return 0;
}
