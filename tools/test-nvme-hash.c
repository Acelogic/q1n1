/* SPDX-License-Identifier: MIT */
/* Host model for platform/uefi/nvme-hash.c, driven by tools/test-nvme-hash.py.
 *
 * The helper source is included whole (NVME_HASH_HOST_TEST only replaces the
 * EL2 system-register reads and the CTR_EL0 read), so its SHA2 instructions,
 * cache maintenance and queue code run natively on an arm64 host. A second
 * thread models the controller's I/O queue pair: it fetches submissions after
 * the tail doorbell, refuses anything but a namespace-1 Read into the M3 data
 * slots, copies the disk image through PRP1/PRP2/PRP lists and posts
 * completions in order, reversed or shuffled, or injects one failure. Every
 * expected digest comes from Python's hashlib through cases.h.
 */
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define NVME_HASH_HOST_TEST 1
#include "nvme-hash.c"

struct vector_spec { uint64_t offset, length; const char *sha256; };
struct overlay_spec { uint64_t disk_byte, length, payload; };
enum order { IN_ORDER, REVERSED, SHUFFLED };
enum inject { NONE, BAD_STATUS, FOREIGN_CID, WRONG_SQID, FATAL, STALL };
struct case_spec {
    const char *name;
    uint64_t lba_bytes, first, count, call_blocks, transfer;
    int sha2, order;
    uint64_t tail, head, phase, cid;
    unsigned overlay_first, overlay_count;
    int inject;
    uint64_t inject_at;
    uint64_t status;             /* expected helper status; 0 means `sha256` is expected */
    const char *sha256;
};
#include "cases.h"

static unsigned checks;
#define CHECK(cond, ...) do { if (!(cond)) { printf("FAIL %s:%d: ", __FILE__, __LINE__); \
    printf(__VA_ARGS__); printf("\n"); exit(1); } checks++; } while (0)

static uint64_t regs[5];         /* CurrentEL, ID_AA64ISAR0_EL1, ID_AA64PFR0_EL1, HCR_EL2, CPTR_EL2 */
uint64_t nvme_hash_host_sysreg(unsigned which) { return which < 5 ? regs[which] : 0; }

/* The A16's recorded values (Snapintosh pauth-state-live-20260922): SHA2 = 2,
 * HCR_EL2.E2H = 0; CPTR_EL2 is the architectural non-trapping 0x33ff. */
static void a16_registers(int sha2)
{
    regs[0] = 2;
    regs[1] = sha2 ? UINT64_C(0x1221111110212120) : UINT64_C(0x1221111110210120);
    regs[2] = UINT64_C(0x1101000010111111);
    regs[3] = UINT64_C(0x88000010);
    regs[4] = 0x33ff;
}

static void hex(const uint8_t *d, char out[65])
{
    for (unsigned i = 0; i < 32; i++) snprintf(out + 2 * i, 3, "%02x", d[i]);
}

static uint64_t rng = 0x9e3779b97f4a7c15u;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }

static uint8_t *map(const char *path, uint64_t *size)
{
    int fd = open(path, O_RDONLY);
    CHECK(fd >= 0, "open %s", path);
    struct stat st;
    CHECK(!fstat(fd, &st), "stat %s", path);
    *size = (uint64_t)st.st_size;
    void *p = mmap(0, *size ? *size : 1, PROT_READ, MAP_PRIVATE, fd, 0);
    CHECK(p != MAP_FAILED, "mmap %s", path);
    close(fd);
    return p;
}

/* ------------------------------------------------------------- SHA-256 --- */

static void sha_checks(const uint8_t *messages)
{
    for (unsigned v = 0; v < sizeof VECTORS / sizeof VECTORS[0]; v++) {
        const uint8_t *m = messages + VECTORS[v].offset;
        uint64_t n = VECTORS[v].length;
        uint8_t d[32];
        char text[65];
        q1n1_sha256(m, n, d);
        hex(d, text);
        CHECK(!strcmp(text, VECTORS[v].sha256), "portable vector %u (%llu bytes)", v, (unsigned long long)n);
        struct q1n1_sha256 s;
        q1n1_sha256_init(&s);
        sha_update(&s, m, n, 1);
        q1n1_sha256_final(&s, d);
        hex(d, text);
        CHECK(!strcmp(text, VECTORS[v].sha256), "SHA2 vector %u (%llu bytes)", v, (unsigned long long)n);
        for (unsigned trial = 0; trial < 4; trial++) {
            /* Random pieces, each on a random path, so partial blocks cross paths. */
            q1n1_sha256_init(&s);
            for (uint64_t at = 0; at < n;) {
                uint64_t piece = next() % (trial & 1 ? 300 : 70000) + 1;
                if (piece > n - at) piece = n - at;
                sha_update(&s, m + at, piece, (int)(next() & 1));
                at += piece;
            }
            q1n1_sha256_final(&s, d);
            hex(d, text);
            CHECK(!strcmp(text, VECTORS[v].sha256), "split vector %u trial %u", v, trial);
        }
    }
    /* The block function against the portable transform from arbitrary states. */
    for (unsigned trial = 0; trial < 20000; trial++) {
        uint8_t block[64 * 3];
        for (unsigned i = 0; i < sizeof block; i++) block[i] = (uint8_t)next();
        struct q1n1_sha256 s;
        uint32_t h[8];
        for (unsigned i = 0; i < 8; i++) s.h[i] = h[i] = (uint32_t)next();
        s.bytes = 0;
        unsigned blocks = trial % 3 + 1;
        q1n1_sha256_update(&s, block, 64 * blocks);
        sha2_blocks(h, block, blocks);
        CHECK(!memcmp(h, s.h, sizeof h), "block function trial %u", trial);
    }
}

/* Which path the helper picks for each register state. */
static void path_checks(void)
{
    struct telemetry t;
    a16_registers(1);
    CHECK(sha2_path(&t) == PATH_SHA2, "A16 registers take the SHA2 path");
    CHECK(t.el == 2 && t.isar0 == regs[1] && t.hcr == regs[3] && t.cptr == regs[4], "registers reported");
    regs[1] = UINT64_C(0x1221111110211120);                   /* SHA2 = 1: SHA-256 only */
    CHECK(sha2_path(&t) == PATH_SHA2, "SHA2 = 1 is enough");
    a16_registers(0);
    CHECK(sha2_path(&t) == NO_SHA2, "no SHA2 instructions");
    a16_registers(1); regs[2] |= UINT64_C(0xf) << 20;
    CHECK(sha2_path(&t) == NO_SIMD, "no Advanced SIMD");
    a16_registers(1); regs[0] = 1;
    CHECK(sha2_path(&t) == NOT_EL2, "EL1 cannot read CPTR_EL2");
    a16_registers(1); regs[4] |= 1u << 10;
    CHECK(sha2_path(&t) == SIMD_TRAPPED, "CPTR_EL2.TFP traps at E2H 0");
    a16_registers(1); regs[3] |= UINT64_C(1) << 34;
    for (unsigned fpen = 0; fpen < 4; fpen++) {
        regs[4] = (uint64_t)fpen << 20;
        CHECK(sha2_path(&t) == (fpen & 1 ? PATH_SHA2 : SIMD_TRAPPED), "E2H 1, FPEN %u", fpen);
    }
    regs[4] = UINT64_C(3) << 20 | 1u << 10;                   /* bit 10 means nothing at E2H 1 */
    CHECK(sha2_path(&t) == PATH_SHA2, "E2H 1 ignores bit 10");
}

/* ------------------------------------------------------------ controller --- */

#define WINDOW_BYTES (4u << 20)
static uint8_t *window, *bar, *disk;
static uint64_t disk_bytes;

struct pending { uint16_t cid; uint16_t status; uint32_t sq_head; };
static struct {
    pthread_t thread;
    atomic_int stop;
    const struct case_spec *c;
    uint32_t sq_head, cq_tail, cq_phase;
    struct pending pending[64];
    unsigned npending, idle;
    uint64_t fetched, completed, max_inflight, next_lba;
} dev;

static volatile uint32_t *reg32(uint64_t off) { return (volatile uint32_t *)(bar + off); }

static void post(struct pending p)
{
    const struct case_spec *c = dev.c;
    uint64_t index = dev.completed++;
    uint32_t cid = p.cid, sqid = 1, status = p.status;
    if (c->inject && index == c->inject_at) {
        if (c->inject == FOREIGN_CID) cid ^= 0x4000;
        if (c->inject == WRONG_SQID) sqid = 2;
        if (c->inject == BAD_STATUS) status = 0x0281;         /* SCT 2, Unrecovered Read Error */
    }
    volatile uint32_t *e = (volatile uint32_t *)(window + 0x3000 + dev.cq_tail * 16);
    e[0] = 0; e[1] = 0; e[2] = p.sq_head | sqid << 16;
    atomic_thread_fence(memory_order_release);
    e[3] = cid | dev.cq_phase << 16 | status << 17;
    dev.cq_tail = (dev.cq_tail + 1) % 64;
    if (!dev.cq_tail) dev.cq_phase ^= 1;
}

static int in_data(uint64_t address, uint64_t bytes)
{
    uint64_t base = (uint64_t)(uintptr_t)window;
    return address >= base + DATA_OFF && address + bytes <= base + DATA_OFF + SLOTS * SLOT_BYTES;
}

static void fetch(void)
{
    const struct case_spec *c = dev.c;
    const volatile uint32_t *sqe = (const volatile uint32_t *)(window + 0x2000 + dev.sq_head * 64);
    uint32_t dw[16];
    for (unsigned i = 0; i < 16; i++) dw[i] = sqe[i];
    uint64_t lba = dw[10] | (uint64_t)dw[11] << 32, blocks = (dw[12] & 0xffff) + 1;
    uint64_t prp1 = dw[6] | (uint64_t)dw[7] << 32, prp2 = dw[8] | (uint64_t)dw[9] << 32;
    uint64_t bytes = blocks * c->lba_bytes;
    CHECK((dw[0] & 0xffff) == 0x02 && dw[1] == 1, "only namespace-1 Read: dw0 %#x nsid %u", dw[0], dw[1]);
    CHECK(!(dw[12] >> 16) && !dw[13] && !dw[14] && !dw[15] && !dw[2] && !dw[3] && !dw[4] && !dw[5],
          "no flags, metadata or other fields");
    CHECK(lba == dev.next_lba, "reads are submitted in LBA order: %llu, expected %llu",
          (unsigned long long)lba, (unsigned long long)dev.next_lba);
    CHECK(bytes <= c->transfer, "transfer %llu > %llu", (unsigned long long)bytes, (unsigned long long)c->transfer);
    CHECK((lba + blocks) * c->lba_bytes <= disk_bytes, "read past the disk");
    CHECK(!(prp1 & 4095) && in_data(prp1, 4096), "PRP1 %#llx is a data-slot page", (unsigned long long)prp1);
    dev.next_lba = lba + blocks;
    uint64_t pages = (bytes + 4095) / 4096, list = 0;
    if (pages > 2) {
        uint64_t base = (uint64_t)(uintptr_t)window;
        CHECK(!(prp2 & 7) && prp2 >= base + PRP_OFF && prp2 + 8 * (pages - 1) <= base + PRP_OFF + SLOTS * PAGE,
              "PRP list inside the PRP pages");
        list = prp2;
    }
    const uint8_t *from = disk + lba * c->lba_bytes;
    for (uint64_t page = 0; page < pages; page++) {
        uint64_t to = !page ? prp1 : list ? ((const volatile uint64_t *)(uintptr_t)list)[page - 1] : prp2;
        uint64_t n = bytes - page * 4096 < 4096 ? bytes - page * 4096 : 4096;
        CHECK(!(to & 4095) && in_data(to, 4096), "data page %llu inside the slots", (unsigned long long)page);
        memcpy((void *)(uintptr_t)to, from + page * 4096, n);
    }
    dev.pending[dev.npending++] = (struct pending){ (uint16_t)(dw[0] >> 16), 0, 0 };
    dev.sq_head = (dev.sq_head + 1) % 64;
    dev.pending[dev.npending - 1].sq_head = dev.sq_head;
    dev.fetched++;
    if (dev.fetched - dev.completed > dev.max_inflight) dev.max_inflight = dev.fetched - dev.completed;
}

static void *controller(void *unused)
{
    (void)unused;
    const struct case_spec *c = dev.c;
    while (!atomic_load(&dev.stop)) {
        uint32_t tail = *reg32(0x1000 + 2 * (4u << 0));
        atomic_thread_fence(memory_order_acquire);
        int fetched = 0;
        while (dev.sq_head != tail) { fetch(); fetched = 1; }
        dev.idle = fetched ? 0 : dev.idle + 1;
        if (!dev.npending) continue;
        if (c->inject && dev.completed == c->inject_at && (c->inject == FATAL || c->inject == STALL)) {
            if (c->inject == FATAL) *reg32(0x1c) |= 2;        /* CSTS.CFS */
            continue;
        }
        /* In order: post at once. Otherwise gather while the host keeps submitting. */
        if (c->order != IN_ORDER && dev.npending < SLOTS && dev.idle < 2000) continue;
        if (c->order == REVERSED)
            for (unsigned i = 0; i < dev.npending / 2; i++) {
                struct pending p = dev.pending[i];
                dev.pending[i] = dev.pending[dev.npending - 1 - i];
                dev.pending[dev.npending - 1 - i] = p;
            }
        if (c->order == SHUFFLED)
            for (unsigned i = dev.npending; i > 1; i--) {
                unsigned j = (unsigned)(next() % i);
                struct pending p = dev.pending[i - 1];
                dev.pending[i - 1] = dev.pending[j];
                dev.pending[j] = p;
            }
        unsigned n = dev.npending;
        if (c->order == SHUFFLED && n > 1) n = (unsigned)(next() % n) + 1;
        for (unsigned i = 0; i < n; i++) {
            if (c->inject && dev.completed == c->inject_at && (c->inject == FATAL || c->inject == STALL)) {
                n = i;
                break;
            }
            post(dev.pending[i]);
        }
        memmove(dev.pending, dev.pending + n, (dev.npending - n) * sizeof dev.pending[0]);
        dev.npending -= n;
    }
    return 0;
}

/* ---------------------------------------------------------------- cases --- */

/* The overlay rows and payload, where NativeHash.digest writes them. */
static void load_overlays(const struct case_spec *c, const uint8_t *payloads)
{
    uint64_t *table = (uint64_t *)(window + 0x61000), payload = 0;
    for (unsigned i = 0; i < c->overlay_count; i++) {
        const struct overlay_spec *o = &OVERLAYS[c->overlay_first + i];
        table[3 * i] = o->disk_byte;
        table[3 * i + 1] = (uint64_t)(uintptr_t)window + 0x70000 + payload;
        table[3 * i + 2] = o->length;
        memcpy(window + 0x70000 + payload, payloads + o->payload, o->length);
        payload += o->length;
    }
}

/* What NativeHash.digest does: 16 MiB (here call_blocks) per call, SHA state
 * carried between calls, queue cursors taken back after each call. sha2 == 2
 * alternates the SHA2 and portable paths from call to call. */
static uint64_t digest(const struct case_spec *c, uint8_t out[32], struct telemetry *last, uint64_t *calls)
{
    struct work *w = (struct work *)(window + STATE_OFF);
    uint64_t base = (uint64_t)(uintptr_t)window;
    uint64_t tail = c->tail, head = c->head, phase = c->phase, cid = c->cid, commands = 0, done = 0;
    uint8_t state[136] = { 0 };
    *calls = 0;
    while (done < c->count) {
        int sha2 = c->sha2 == 2 ? (int)(*calls & 1) : c->sha2;
        a16_registers(sha2);
        uint64_t n = c->count - done < c->call_blocks ? c->count - done : c->call_blocks;
        uint64_t fields[20] = { MAGIC, (uint64_t)(uintptr_t)bar, base, c->first + done, n, c->lba_bytes,
                                c->transfer, disk_bytes / c->lba_bytes, tail, head, phase, cid, 0, 64, 0, 0,
                                0, commands, c->overlay_count ? base + 0x61000 : 0, c->overlay_count };
        memcpy(w, fields, sizeof fields);
        memcpy((uint8_t *)w + sizeof fields, state, sizeof state);
        uint64_t result = nvme_hash_run(w, !done, done + n == c->count);
        ++*calls;
        memcpy(fields, w, sizeof fields);
        memcpy(state, (uint8_t *)w + sizeof fields, sizeof state);
        memcpy(last, window + STATE_OFF + TELEMETRY_OFF, sizeof *last);
        tail = fields[8]; head = fields[9]; phase = fields[10]; cid = fields[11]; commands = fields[17];
        if (result || fields[14] || fields[15] != n) {
            CHECK(result == fields[14], "return value is the status");
            return result;
        }
        uint64_t per = c->transfer / c->lba_bytes;
        CHECK(last->magic == TELEMETRY_MAGIC && last->commands == (n + per - 1) / per && !last->status,
              "telemetry for call %llu", (unsigned long long)*calls);
        CHECK(last->sha_path == (uint64_t)(sha2 ? PATH_SHA2 : NO_SHA2), "path %#llx",
              (unsigned long long)last->sha_path);
        done += n;
    }
    memcpy(out, state + 104, 32);
    return 0;
}

/* Bytes of the window the helper or the controller may change during a digest:
 * the I/O SQ and CQ, the PRP pages, struct work and the telemetry, the data slots. */
static int writable(uint64_t off)
{
    return (off >= 0x2000 && off < 0x3400) || (off >= PRP_OFF && off < PRP_OFF + SLOTS * PAGE) ||
           (off >= STATE_OFF && off < STATE_OFF + sizeof(struct work)) ||
           (off >= STATE_OFF + TELEMETRY_OFF && off < STATE_OFF + TELEMETRY_OFF + sizeof(struct telemetry)) ||
           off >= DATA_OFF;
}

static void run_case(const struct case_spec *c, const uint8_t *payloads, uint8_t *before)
{
    memset(window, 0, WINDOW_BYTES);
    memset(bar, 0, 0x2000);
    for (unsigned i = 0; i < 64; i++)                        /* the previous lap's phase from head on */
        ((uint32_t *)(window + 0x3000))[4 * i + 3] = (uint32_t)(i >= c->head ? !c->phase : c->phase) << 16;
    for (uint64_t off = 0; off < WINDOW_BYTES; off += 8)     /* sentinels everywhere else */
        if (!writable(off) && (off < 0x61000 || off >= 0x170000)) *(uint64_t *)(window + off) = off * 0x9e37u;
    load_overlays(c, payloads);
    *reg32(0x1000 + 2 * 4) = (uint32_t)c->tail;
    memset(&dev, 0, sizeof dev);
    dev.c = c;
    dev.sq_head = (uint32_t)c->tail;
    dev.cq_tail = (uint32_t)c->head;
    dev.cq_phase = (uint32_t)c->phase;
    dev.next_lba = c->first;
    memcpy(before, window, WINDOW_BYTES);
    struct telemetry t;
    uint8_t d[32];
    uint64_t calls;
    CHECK(!pthread_create(&dev.thread, 0, controller, 0), "thread");
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    uint64_t status = digest(c, d, &t, &calls);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    atomic_store(&dev.stop, 1);
    pthread_join(dev.thread, 0);
    double seconds = (double)(t1.tv_sec - t0.tv_sec) + (double)(t1.tv_nsec - t0.tv_nsec) * 1e-9;
    CHECK(status == c->status, "%s: status %llu, expected %llu", c->name, (unsigned long long)status,
          (unsigned long long)c->status);
    if (!status) {
        char text[65];
        hex(d, text);
        CHECK(!strcmp(text, c->sha256), "%s: digest %s, expected %s", c->name, text, c->sha256);
        CHECK(dev.next_lba == c->first + c->count && dev.completed == dev.fetched, "%s: every block read once",
              c->name);
        /* A controller that holds completions shows the helper's full depth. */
        uint64_t per = c->transfer / c->lba_bytes;
        uint64_t most = ((c->call_blocks < c->count ? c->call_blocks : c->count) + per - 1) / per;
        CHECK(dev.max_inflight <= SLOTS && (c->order == IN_ORDER || most < SLOTS || dev.max_inflight == SLOTS),
              "%s: %llu reads in flight", c->name, (unsigned long long)dev.max_inflight);
    } else {
        CHECK(t.status == status, "%s: telemetry status", c->name);
        CHECK(status != 6 || !dev.fetched, "%s: overlays are refused before any read", c->name);
    }
    for (uint64_t off = 0; off < WINDOW_BYTES; off++)
        if (!writable(off) && window[off] != before[off])
            CHECK(0, "%s: helper changed window offset %#llx", c->name, (unsigned long long)off);
    printf("case %-28s status %llu calls %3llu commands %5llu depth %llu %7.1f MiB/s path %#llx\n", c->name,
           (unsigned long long)status, (unsigned long long)calls, (unsigned long long)dev.completed,
           (unsigned long long)dev.max_inflight, (double)(c->count * c->lba_bytes) / seconds / (1 << 20),
           (unsigned long long)t.sha_path);
}

/* Argument checks are unchanged: each refusal leaves status 1 and no I/O. */
static void refusal_checks(void)
{
    struct work *w = (struct work *)(window + STATE_OFF);
    uint64_t base = (uint64_t)(uintptr_t)window;
    uint64_t good[20] = { MAGIC, (uint64_t)(uintptr_t)bar, base, 0, 8, 512, 262144, 1 << 20, 0, 0, 1, 1, 0, 64,
                          0, 0, 0, 0, 0, 0 };
    struct { unsigned field; uint64_t value; } bad[] = {
        { 0, 0 }, { 13, 32 }, { 12, 16 }, { 8, 64 }, { 9, 64 }, { 10, 2 }, { 11, 0 }, { 11, 65536 },
        { 1, 0 }, { 1, 0x10 }, { 2, 0 }, { 2, base + 0x1000 }, { 5, 1024 }, { 6, 0 }, { 6, 524288 },
        { 6, 1000 }, { 3, (1 << 20) + 1 }, { 4, (1 << 20) + 1 }, { 4, 131073 }, { 19, 257 }, { 19, 1 },
    };
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        uint64_t fields[20];
        memcpy(fields, good, sizeof fields);
        fields[bad[i].field] = bad[i].value;
        memset(window + STATE_OFF, 0, PAGE);
        memcpy(w, fields, sizeof fields);
        CHECK(nvme_hash_run(w, 1, 1) == 1 && w->status == 1, "refusal %u (field %u)", i, bad[i].field);
        CHECK(!*(uint64_t *)(window + STATE_OFF + TELEMETRY_OFF), "refusal %u writes no telemetry", i);
    }
}

int main(int argc, char **argv)
{
    CHECK(argc == 4, "usage: test-nvme-hash DISK OVERLAYS VECTORS");
    uint64_t overlay_bytes, vector_bytes;
    disk = map(argv[1], &disk_bytes);
    const uint8_t *payloads = map(argv[2], &overlay_bytes);
    const uint8_t *messages = map(argv[3], &vector_bytes);
    sha_checks(messages);
    path_checks();
    printf("SHA-256: %zu vectors, portable, SHA2 and mixed splits; block function; path selection\n",
           sizeof VECTORS / sizeof VECTORS[0]);
    window = aligned_alloc(65536, WINDOW_BYTES);
    bar = aligned_alloc(4096, 0x2000);
    uint8_t *before = malloc(WINDOW_BYTES);
    CHECK(window && bar && before, "allocation");
    refusal_checks();
    for (unsigned i = 0; i < sizeof CASES / sizeof CASES[0]; i++) run_case(&CASES[i], payloads, before);
    printf("PASS %u checks, %zu cases\n", checks, sizeof CASES / sizeof CASES[0]);
    return 0;
}
