/* SPDX-License-Identifier: MIT */
/* RAM-only, read-only NVMe/SHA-256 helper for tools/nvme_hash.py.
 * Failure analysis: docs/A16-NVME-WRITES.md. Never builds a Write opcode.
 * The caller proves DMA ownership and supplies an idle M3-layout I/O queue.
 * Each invocation is bounded; queue cursors and partial progress are returned.
 *
 * Speed (docs/FAST-NATIVE-HASH-20260928.md): up to eight reads are in flight,
 * one per 256 KiB data slot of the M3 layout, and the data is hashed strictly
 * in LBA order as each slot completes. SHA-256 uses the ARMv8 SHA2
 * instructions when ID_AA64ISAR0_EL1 has them, the CPU is at EL2, EL2 does not
 * trap Advanced SIMD and a known block hashes correctly; otherwise the portable
 * sha256.c. Both compute the same function, so every digest is unchanged.
 */
#include <stdint.h>
#include <stddef.h>
#include "sha256.h"

#define MAGIC UINT64_C(0x31485341484d564e)
#define PAGE 4096u
#define ENTRIES 64u
#define DATA_OFF 0x200000u
#define PRP_OFF 0x8000u
#define SLOTS 8u                 /* M3: 2 MiB of data in 256 KiB slots, one PRP page each */
#define SLOT_BYTES 0x40000u
#define STATE_OFF 0x60000u       /* where NativeHash places struct work */
#define TELEMETRY_OFF 0x400u     /* after struct work, in the same state page */
#define TELEMETRY_MAGIC UINT64_C(0x314d4c5448534148) /* "HASHTLM1" */

#ifdef NVME_HASH_HOST_TEST
#define ENTRY_SECTION
uint64_t nvme_hash_host_sysreg(unsigned which);
#else
#define ENTRY_SECTION __attribute__((section(".text.entry")))
#endif

struct work {
    uint64_t magic, bar, window, first_lba, blocks, lba_bytes, max_transfer, nsze;
    uint64_t tail, head, phase, next_cid, stride, io_entries, status, done_blocks;
    uint64_t last_completion, commands, overlay_table, overlay_count;
    struct q1n1_sha256 sha;
    uint8_t digest[32];
};
_Static_assert(sizeof(struct work) == 296, "Python ABI");

/* Written after every call that passes the argument checks; read only by
 * tools/nvme_hash_compare.py. NativeHash ignores it. Times are CNTPCT ticks. */
struct telemetry {
    uint64_t magic, sha_path, isar0, pfr0, el, hcr, cptr, frequency;
    uint64_t depth, commands, total, wait, invalidate, hash, submit, status;
};
_Static_assert(sizeof(struct telemetry) == 128, "telemetry ABI");

enum {                           /* telemetry sha_path */
    PATH_SHA2 = 1,               /* ARMv8 SHA256H/H2/SU0/SU1 */
    NO_SHA2 = 1u << 8,           /* ID_AA64ISAR0_EL1.SHA2 == 0 */
    NO_SIMD = 1u << 9,           /* ID_AA64PFR0_EL1.AdvSIMD == 0xf */
    SIMD_TRAPPED = 1u << 10,     /* CPTR_EL2 traps FP/SIMD at EL2 */
    NOT_EL2 = 1u << 11,          /* CPTR_EL2 is readable only at EL2 */
    SELF_TEST = 1u << 12,        /* a known block hashed wrongly */
};

static inline uint64_t ticks(void)
{
    uint64_t v; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(v)); return v;
}
static inline uint64_t frequency(void)
{
    uint64_t v; __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(v)); return v;
}
static inline void barrier(void) { __asm__ volatile("dsb sy" ::: "memory"); }
static uint64_t cache_line(void)
{
#ifdef NVME_HASH_HOST_TEST
    return 64;                   /* macOS traps EL0 CTR_EL0 reads */
#else
    uint64_t ctr; __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    return UINT64_C(4) << ((ctr >> 16) & 15);
#endif
}
static void clean_invalidate(uint64_t address, uint64_t bytes)
{
    uint64_t line = cache_line();
    uint64_t end = address + bytes;
    for (uint64_t p = address & ~(line - 1); p < end; p += line)
        __asm__ volatile("dc civac, %0" :: "r"(p) : "memory");
    barrier();
}
static uint32_t load32(uint64_t address) { return *(volatile uint32_t *)(uintptr_t)address; }
static void store32(uint64_t address, uint32_t value)
{
    *(volatile uint32_t *)(uintptr_t)address = value;
    barrier();
}

/* ---------------------------------------------------------------- SHA-256 --- */

static const uint32_t K256[64] __attribute__((aligned(16))) = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

/* Four rounds (Arm ARM SHA256H/SHA256H2): v0 = abcd, v1 = efgh, v2 = the
 * abcd that SHA256H2 needs after SHA256H has replaced v0, v3 = W + K. The
 * S form also computes the next four schedule words into the first vector:
 * W[i..i+3] from W[i-16..], W[i-15..] (SU0) and W[i-7..], W[i-2..] (SU1). */
#define ROUNDS(k, w) "add v3.4s, " w ".4s, " k ".4s\n" "mov v2.16b, v0.16b\n" \
    "sha256h q0, q1, v3.4s\n" "sha256h2 q1, q2, v3.4s\n"
#define ROUNDS_S(k, w0, w1, w2, w3) "add v3.4s, " w0 ".4s, " k ".4s\n" \
    "sha256su0 " w0 ".4s, " w1 ".4s\n" "mov v2.16b, v0.16b\n" \
    "sha256h q0, q1, v3.4s\n" "sha256h2 q1, q2, v3.4s\n" \
    "sha256su1 " w0 ".4s, " w2 ".4s, " w3 ".4s\n"

/* `blocks` (> 0) 64-byte blocks into h[8]. Only caller-saved vector registers
 * (v0-v7, v16-v31) are used, so nothing is saved; the proxy and the rest of
 * this helper are general-register code. The block-start state is reloaded
 * from h for the final addition. */
static void sha2_blocks(uint32_t h[8], const uint8_t *p, uint64_t blocks)
{
    const uint32_t *k = K256;
    __asm__ volatile(
        ".arch_extension sha2\n"
        "ld1 {v16.4s-v19.4s}, [%[k]], #64\n"
        "ld1 {v20.4s-v23.4s}, [%[k]], #64\n"
        "ld1 {v24.4s-v27.4s}, [%[k]], #64\n"
        "ld1 {v28.4s-v31.4s}, [%[k]]\n"
        "ld1 {v0.4s, v1.4s}, [%[h]]\n"
        "1:\n"
        "ld1 {v4.16b-v7.16b}, [%[p]], #64\n"
        "rev32 v4.16b, v4.16b\n" "rev32 v5.16b, v5.16b\n"
        "rev32 v6.16b, v6.16b\n" "rev32 v7.16b, v7.16b\n"
        ROUNDS_S("v16", "v4", "v5", "v6", "v7") ROUNDS_S("v17", "v5", "v6", "v7", "v4")
        ROUNDS_S("v18", "v6", "v7", "v4", "v5") ROUNDS_S("v19", "v7", "v4", "v5", "v6")
        ROUNDS_S("v20", "v4", "v5", "v6", "v7") ROUNDS_S("v21", "v5", "v6", "v7", "v4")
        ROUNDS_S("v22", "v6", "v7", "v4", "v5") ROUNDS_S("v23", "v7", "v4", "v5", "v6")
        ROUNDS_S("v24", "v4", "v5", "v6", "v7") ROUNDS_S("v25", "v5", "v6", "v7", "v4")
        ROUNDS_S("v26", "v6", "v7", "v4", "v5") ROUNDS_S("v27", "v7", "v4", "v5", "v6")
        ROUNDS("v28", "v4") ROUNDS("v29", "v5") ROUNDS("v30", "v6") ROUNDS("v31", "v7")
        "ld1 {v2.4s, v3.4s}, [%[h]]\n"
        "add v0.4s, v0.4s, v2.4s\n" "add v1.4s, v1.4s, v3.4s\n"
        "st1 {v0.4s, v1.4s}, [%[h]]\n"
        "subs %[n], %[n], #1\n"
        "b.ne 1b\n"
        : [p] "+r"(p), [n] "+r"(blocks), [k] "+r"(k)
        : [h] "r"(h)
        : "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v16", "v17", "v18", "v19", "v20", "v21",
          "v22", "v23", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31", "cc", "memory");
}

static uint64_t sysreg(unsigned which)
{
    uint64_t v = 0;
#ifdef NVME_HASH_HOST_TEST
    v = nvme_hash_host_sysreg(which);
#else
    switch (which) {
    case 0: __asm__ volatile("mrs %0, CurrentEL" : "=r"(v)); v = (v >> 2) & 3; break;
    case 1: __asm__ volatile("mrs %0, id_aa64isar0_el1" : "=r"(v)); break;
    case 2: __asm__ volatile("mrs %0, id_aa64pfr0_el1" : "=r"(v)); break;
    case 3: __asm__ volatile("mrs %0, hcr_el2" : "=r"(v)); break;
    case 4: __asm__ volatile("mrs %0, cptr_el2" : "=r"(v)); break;
    }
#endif
    return v;
}

/* SHA-256("abc"): one padded block, and the state after it. */
static const uint8_t ABC_BLOCK[64] = { 'a', 'b', 'c', 0x80, [63] = 24 };
static const uint32_t ABC_STATE[8] = {
    0xba7816bf, 0x8f01cfea, 0x414140de, 0x5dae2223, 0xb00361a3, 0x96177a9c, 0xb410ff61, 0xf20015ad
};

/* PATH_SHA2 when the ARMv8 path may run here, else the reasons it may not.
 * EL3 traps cannot be read from EL2: UEFI hands applications FP/SIMD enabled
 * and q1n1 never writes CPTR_EL2. */
static uint64_t sha2_path(struct telemetry *t)
{
    t->el = sysreg(0);
    t->isar0 = sysreg(1);
    t->pfr0 = sysreg(2);
    uint64_t why = 0;
    if (!((t->isar0 >> 12) & 15)) why |= NO_SHA2;
    if (((t->pfr0 >> 20) & 15) == 15 || ((t->pfr0 >> 16) & 15) == 15) why |= NO_SIMD;
    if (t->el != 2) return why | NOT_EL2;
    t->hcr = sysreg(3);
    t->cptr = sysreg(4);
    /* E2H 0: CPTR_EL2.TFP (bit 10) traps. E2H 1: FPEN (bits 21:20) must be 0b01 or 0b11. */
    if ((t->hcr >> 34) & 1 ? !((t->cptr >> 20) & 1) : (t->cptr >> 10) & 1) why |= SIMD_TRAPPED;
    if (why) return why;
    static const uint32_t iv[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    };
    uint32_t h[8];
    for (unsigned i = 0; i < 8; i++) h[i] = iv[i];
    sha2_blocks(h, ABC_BLOCK, 1);
    for (unsigned i = 0; i < 8; i++)
        if (h[i] != ABC_STATE[i]) return SELF_TEST;
    return PATH_SHA2;
}

/* q1n1_sha256_update with whole blocks on the SHA2 path; partial blocks go
 * through the portable buffer, so the state format is unchanged. */
static void sha_update(struct q1n1_sha256 *s, const uint8_t *p, uint64_t size, int sha2)
{
    if (!sha2) { q1n1_sha256_update(s, p, size); return; }
    uint64_t used = s->bytes % 64;
    if (used) {
        uint64_t take = 64 - used < size ? 64 - used : size;
        q1n1_sha256_update(s, p, take);
        p += take; size -= take;
    }
    uint64_t blocks = size / 64;
    if (blocks) {
        sha2_blocks(s->h, p, blocks);
        s->bytes += blocks * 64;
        p += blocks * 64; size -= blocks * 64;
    }
    if (size) q1n1_sha256_update(s, p, size);
}

/* ------------------------------------------------------------------ reads --- */

struct slot { uint64_t lba, count, cid, busy, done; };

static int overlays_valid(const struct work *w)
{
    const uint64_t *overlays = (const uint64_t *)(uintptr_t)w->overlay_table;
    for (uint64_t i = 0; i < w->overlay_count; i++) {
        uint64_t disk_byte = overlays[i * 3], source = overlays[i * 3 + 1];
        uint64_t length = overlays[i * 3 + 2];
        if (!length || length > 0x100000 || source < w->window + 0x70000 ||
            source + length > w->window + 0x170000 ||
            disk_byte > w->nsze * w->lba_bytes || length > w->nsze * w->lba_bytes - disk_byte)
            return 0;
    }
    return 1;
}

/* Restore verification masks only reviewed disk ranges in the RAM read
 * buffer. The SSD is never written by this helper. */
static void apply_overlays(const struct work *w, uint64_t lba, uint64_t data, uint64_t size)
{
    const uint64_t *overlays = (const uint64_t *)(uintptr_t)w->overlay_table;
    for (uint64_t i = 0; i < w->overlay_count; i++) {
        uint64_t disk_byte = overlays[i * 3], source = overlays[i * 3 + 1];
        uint64_t length = overlays[i * 3 + 2];
        uint64_t begin = lba * w->lba_bytes, end = begin + size;
        uint64_t lo = begin > disk_byte ? begin : disk_byte;
        uint64_t hi = end < disk_byte + length ? end : disk_byte + length;
        if (lo < hi) {
            uint8_t *dest = (uint8_t *)(uintptr_t)(data + lo - begin);
            const uint8_t *src = (const uint8_t *)(uintptr_t)(source + lo - disk_byte);
            for (uint64_t j = 0; j < hi - lo; j++) dest[j] = src[j];
        }
    }
}

static uint64_t slot_data(const struct work *w, unsigned slot) { return w->window + DATA_OFF + slot * SLOT_BYTES; }

static void submit(struct work *w, struct slot *s, unsigned slot, uint64_t sq_db)
{
    uint64_t data = slot_data(w, slot);
    uint64_t size = s->count * w->lba_bytes;
    uint64_t pages = (size + PAGE - 1) / PAGE;
    uint64_t prp2 = 0;
    if (pages == 2) prp2 = data + PAGE;
    else if (pages > 2) {
        prp2 = w->window + PRP_OFF + slot * PAGE;
        volatile uint64_t *list = (volatile uint64_t *)(uintptr_t)prp2;
        for (uint64_t i = 1; i < pages; i++) list[i - 1] = data + PAGE * i;
        clean_invalidate(prp2, (pages - 1) * 8);
    }
    clean_invalidate(data, size);
    uint64_t sq = w->window + 0x2000 + w->tail * 64;
    volatile uint32_t *entry = (volatile uint32_t *)(uintptr_t)sq;
    for (unsigned i = 0; i < 16; i++) entry[i] = 0;
    uint32_t cid = (uint32_t)w->next_cid;
    entry[0] = 0x02u | (cid << 16); /* NVM Read only. */
    entry[1] = 1;
    entry[6] = (uint32_t)data; entry[7] = (uint32_t)(data >> 32);
    entry[8] = (uint32_t)prp2; entry[9] = (uint32_t)(prp2 >> 32);
    entry[10] = (uint32_t)s->lba; entry[11] = (uint32_t)(s->lba >> 32);
    entry[12] = (uint32_t)(s->count - 1);
    clean_invalidate(sq, 64);
    s->cid = cid;
    s->busy = 1;
    s->done = 0;
    w->tail = (w->tail + 1) % ENTRIES;
    w->next_cid = w->next_cid % 65535 + 1;
    store32(sq_db, (uint32_t)w->tail);
}

/* Consume one completion if the controller has posted it: 0 none yet, 1 one
 * consumed, else the helper status (2 fatal, 4 foreign entry, 5 command error). */
static uint64_t reap(struct work *w, struct slot *slots, uint64_t cq_db)
{
    uint64_t cq = w->window + 0x3000 + w->head * 16;
    clean_invalidate(cq, 16);
    uint32_t dw3 = load32(cq + 12);
    if (((dw3 >> 16) & 1) != w->phase)
        return load32(w->bar + 0x1c) & 2 ? 2 : 0;
    w->last_completion = dw3;
    struct slot *s = 0;
    for (unsigned i = 0; i < SLOTS; i++)
        if (slots[i].busy && !slots[i].done && slots[i].cid == (dw3 & 65535)) s = &slots[i];
    if (!s || (load32(cq + 8) >> 16) != 1) return 4;
    w->head = (w->head + 1) % ENTRIES;
    if (!w->head) w->phase ^= 1;
    store32(cq_db, (uint32_t)w->head);
    if (dw3 >> 17) return 5;
    s->done = 1;
    return 1;
}

ENTRY_SECTION
uint64_t nvme_hash_run(struct work *w, uint64_t reset, uint64_t final)
{
    uint64_t start = ticks();
    w->status = 1;
    w->done_blocks = 0;
    if (w->magic != MAGIC || w->io_entries != ENTRIES || w->stride > 15 ||
        w->tail >= ENTRIES || w->head >= ENTRIES || w->phase > 1 ||
        !w->next_cid || w->next_cid > 65535 || !w->bar || (w->bar & 4095) ||
        !w->window || (w->window & 65535) ||
        (w->lba_bytes != 512 && w->lba_bytes != 4096) ||
        !w->max_transfer || w->max_transfer > 262144 ||
        w->max_transfer % w->lba_bytes || w->first_lba > w->nsze ||
        w->blocks > w->nsze - w->first_lba ||
        w->blocks > (UINT64_C(64) << 20) / w->lba_bytes || w->overlay_count > 256 ||
        (w->overlay_count && w->overlay_table != w->window + 0x61000))
        return w->status;
    struct telemetry t = { .magic = TELEMETRY_MAGIC, .frequency = frequency(), .depth = SLOTS };
    uint64_t status = w->blocks && !overlays_valid(w) ? 6 : 0;
    int sha2 = 0;
    if (!status) {
        t.sha_path = sha2_path(&t);
        sha2 = t.sha_path == PATH_SHA2;
        if (reset) q1n1_sha256_init(&w->sha);
    }
    uint64_t sq_db = w->bar + 0x1000 + 2 * (UINT64_C(4) << w->stride);
    uint64_t cq_db = w->bar + 0x1000 + 3 * (UINT64_C(4) << w->stride);
    uint64_t per = w->max_transfer / w->lba_bytes;
    uint64_t total = status ? 0 : (w->blocks + per - 1) / per;
    struct slot slots[SLOTS];
    for (unsigned i = 0; i < SLOTS; i++) slots[i].busy = slots[i].done = 0;
    uint64_t submitted = 0, hashed = 0, mark;
    while (hashed < total) {
        /* Keep every free slot reading ahead of the one being hashed. A slot is
         * reused only after its data has been hashed. */
        mark = ticks();
        while (submitted < total && submitted - hashed < SLOTS) {
            struct slot *s = &slots[submitted % SLOTS];
            s->lba = w->first_lba + submitted * per;
            s->count = w->blocks - submitted * per < per ? w->blocks - submitted * per : per;
            submit(w, s, (unsigned)(submitted % SLOTS), sq_db);
            submitted++;
        }
        uint64_t now = ticks();
        t.submit += now - mark;
        struct slot *s = &slots[hashed % SLOTS];
        uint64_t deadline = now + frequency() * 5;
        while (!s->done) {
            uint64_t result = reap(w, slots, cq_db);
            if (result > 1) { status = result; break; }
            if (!result) {
                if (ticks() > deadline) { status = 3; break; }
                __asm__ volatile("yield");
            }
        }
        mark = ticks();
        t.wait += mark - now;
        if (status) break;
        uint64_t data = slot_data(w, (unsigned)(hashed % SLOTS));
        uint64_t size = s->count * w->lba_bytes;
        clean_invalidate(data, size);
        apply_overlays(w, s->lba, data, size);
        now = ticks();
        t.invalidate += now - mark;
        sha_update(&w->sha, (const uint8_t *)(uintptr_t)data, size, sha2);
        t.hash += ticks() - now;
        s->busy = 0;
        w->done_blocks += s->count;
        w->commands++;
        t.commands++;
        hashed++;
    }
    if (!status && final) q1n1_sha256_final(&w->sha, w->digest);
    w->status = status;
    t.status = status;
    t.total = ticks() - start;
    if ((uintptr_t)w == w->window + STATE_OFF)
        *(struct telemetry *)((uintptr_t)w + TELEMETRY_OFF) = t;
    return status;
}
