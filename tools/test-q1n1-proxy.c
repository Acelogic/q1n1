/* SPDX-License-Identifier: MIT */
/* Native harness: serves platform/uefi/q1n1-proxy.c over a pty so
 * tools/test-q1n1-proxy.py can drive the real request loop under sanitizers.
 * Exception guards are inert here; the AArch64 guard path is covered by the
 * QEMU test (tools/test-uefi.py --proxy). Never part of an EFI build.
 *
 * clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Iplatform/uefi \
 *   tools/test-q1n1-proxy.c platform/uefi/q1n1-proxy.c -o build/test-q1n1-proxy
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <util.h>
#include "q1n1-proxy.h"
#include "q1n1-bootinfo.h"

#define SCRATCH_BYTES (1u << 20)

static int link_fd = -1;
/* Sized from the real struct rather than a literal: a hand-kept 48 here went
 * stale the first time a field was added, and the harness then read past the
 * array -- caught only because ASan was on. */
static uint64_t bootinfo[sizeof(struct q1n1_bootinfo) / sizeof(uint64_t)];
static uint8_t *scratch;

/* The proxy calls these leaf helpers; on AArch64 they live in proxy-asm.S and
 * are resumable under Q1N1_GUARD_RETURN. The arithmetic is identical. */
uint32_t q1n1_checksum_block(const uint8_t *data, uint64_t length, uint32_t sum)
{
    while (length--) sum = sum * 31337u + (*data++ ^ 0x5au);
    return sum;
}
void q1n1_copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t size) { while (size--) *dst++ = *src++; }
#define COPY(name, type) \
    void name(uint64_t dst, uint64_t src, uint64_t size) { \
        size &= ~(uint64_t)(sizeof(type) - 1); \
        for (uint64_t n = 0; n < size; n += sizeof(type)) \
            *(type *)(uintptr_t)(dst + n) = *(const type *)(uintptr_t)(src + n); \
    }
#define FILLER(name, type) \
    void name(uint64_t dst, uint64_t value, uint64_t size) { \
        size &= ~(uint64_t)(sizeof(type) - 1); \
        for (uint64_t n = 0; n < size; n += sizeof(type)) *(type *)(uintptr_t)(dst + n) = (type)value; \
    }
COPY(q1n1_memcpy64, uint64_t)
COPY(q1n1_memcpy32, uint32_t)
COPY(q1n1_memcpy16, uint16_t)
COPY(q1n1_memcpy8, uint8_t)
FILLER(q1n1_memset64, uint64_t)
FILLER(q1n1_memset32, uint32_t)
FILLER(q1n1_memset16, uint16_t)
FILLER(q1n1_memset8, uint8_t)

uint64_t q1n1_platform_ticks(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec;
}
uint64_t q1n1_platform_hz(void) { return 1000000000u; }
uint64_t q1n1_platform_bootargs(void) { return (uintptr_t)bootinfo; }
uint64_t q1n1_platform_base(void) { return 0x140000000u; }
static int console_enabled = 1;
uint64_t q1n1_platform_fb_console(int enabled)
{ uint64_t previous=console_enabled; console_enabled=!!enabled; return previous; }
void q1n1_platform_reboot(void)
{
    fprintf(stderr, "harness: reboot requested\n");
    _exit(0);
}

/* SError model. The proxy polls right before and right after every target
 * access; the host arms a countdown through P_CALL and the n-th poll from then
 * on reports an SError, which places it before or during a chosen request.
 * With `level` set the source stays asserted once it fires: every later poll
 * finds it again and reports it still pending, until the host re-arms. */
static uint64_t serror_countdown, serror_syndrome, serror_polls;
static int serror_level, serror_asserted;
uint64_t q1n1_platform_serror_mechanism(void) { return Q1N1_SERROR_RAS; }
int q1n1_platform_serror_poll(uint64_t *syndrome, int *still_pending)
{
    serror_polls++;
    *still_pending = 0;
    if (serror_countdown && --serror_countdown == 0) serror_asserted = 1;
    if (!serror_asserted) return 0;
    if (serror_level == 2) {
        /* REVIEW-2: pending, but neither deferred by ESB nor taken in the window
         * (for example routed to EL1 while q1n1 runs at EL2). */
        *still_pending = 1;
        return 0;
    }
    *syndrome = serror_syndrome;
    serror_asserted = serror_level;
    *still_pending = serror_level;
    return 1;
}
static uint64_t serror_schedule(uint64_t polls, uint64_t syndrome, uint64_t level, uint64_t d, uint64_t e)
{
    (void)d; (void)e;
    serror_countdown = polls;
    serror_syndrome = syndrome;
    serror_level = level == 2 ? 2 : level != 0;
    serror_asserted = 0;
    return serror_polls;
}

/* A model of the core around the deliberate window in main.c's
 * q1n1_platform_serror_poll: `msr daifclr,#4; isb; nop; msr daifset,#4; isb`,
 * entered with PSTATE.A set and an SError pending. The SError is taken at an
 * instruction boundary whenever A is clear and one is pending; entry saves
 * PSTATE in SPSR and sets A, and the vector (main.c q1n1_exception) erets to
 * the interrupted instruction with the SPSR that `vector_spsr` gives it. A
 * level-sensitive source stays pending after it is taken. */
struct window_run { unsigned taken, steps; int left, still_pending; };
static uint64_t spsr_unchanged(uint64_t spsr) { return spsr; }
static struct window_run window_model(int level, uint64_t (*vector_spsr)(uint64_t))
{
    enum { DAIFCLR, ISB1, NOP, DAIFSET, ISB2, DONE };
    const uint64_t el2h = 0x9, daif = UINT64_C(0xf) << 6;
    uint64_t pstate = el2h | daif;
    int pc = DAIFCLR, pending = 1;
    struct window_run run = {0, 0, 0, 0};
    while (pc != DONE && run.steps < 10000) {
        run.steps++;
        if (pending && !(pstate & Q1N1_SPSR_A)) {
            uint64_t spsr = pstate;
            pstate |= daif;                 /* exception entry masks DAIF */
            run.taken++;
            pending = level;                /* edge: taking it consumes it */
            pstate = vector_spsr(spsr);     /* eret: PSTATE = SPSR, PC = ELR */
            continue;
        }
        if (pc == DAIFCLR) pstate &= ~Q1N1_SPSR_A;
        if (pc == DAIFSET) pstate |= Q1N1_SPSR_A;
        pc++;
    }
    run.left = pc == DONE;
    run.still_pending = pending;            /* what ISR_EL1.A reads after */
    return run;
}
static int window_selftest(void)
{
    int failures = 0, passed = 0;
#define WINDOW_CHECK(cond, label) do { if (cond) passed++; else { failures++; printf("FAIL: %s\n", label); } } while (0)
    struct window_run run = window_model(1, q1n1_serror_window_spsr);
    WINDOW_CHECK(run.left && run.taken == 1, "level-sensitive SError: taken once, the core leaves the window");
    WINDOW_CHECK(run.still_pending, "level-sensitive SError: reported still pending (unconsumed) after the poll");
    run = window_model(0, q1n1_serror_window_spsr);
    WINDOW_CHECK(run.left && run.taken == 1 && !run.still_pending, "edge SError: taken once, consumed");
    run = window_model(1, spsr_unchanged);
    WINDOW_CHECK(!run.left && run.taken > 1000, "control: returning with A clear re-takes a level SError forever");
    run = window_model(0, spsr_unchanged);
    WINDOW_CHECK(run.left && run.taken == 1, "control: an edge SError leaves either way");
    WINDOW_CHECK(q1n1_serror_window_spsr(0x3c9 & ~Q1N1_SPSR_A) == 0x3c9 && q1n1_serror_window_spsr(0x3c9) == 0x3c9
                 && q1n1_serror_window_spsr(UINT64_C(0x20000009)) == UINT64_C(0x20000109),
                 "window SPSR: sets A (bit 8) and keeps every other bit");
#undef WINDOW_CHECK
    printf("window %d %d\n", passed, failures);
    return failures != 0;
}

/* A target function the host can invoke with P_CALL. */
static uint64_t adder(uint64_t a, uint64_t b, uint64_t c, uint64_t d, uint64_t e)
{
    return a + b + c + d + e;
}

static size_t io_read(void *ctx, uint8_t *buffer, size_t count)
{
    (void)ctx;
    ssize_t got = read(link_fd, buffer, count);
    return got > 0 ? (size_t)got : 0;
}
static size_t io_write(void *ctx, const uint8_t *buffer, size_t count)
{
    (void)ctx;
    ssize_t sent = write(link_fd, buffer, count);
    return sent > 0 ? (size_t)sent : 0;
}
static void io_poll(void *ctx) { (void)ctx; usleep(200); }
static int io_ready(void *ctx) { (void)ctx; return 1; }

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "--window")) return window_selftest();
    int primary = -1, secondary = -1;
    char name[256];
    if (openpty(&primary, &secondary, name, NULL, NULL)) {
        perror("openpty");
        return 1;
    }
    link_fd = primary;
    fcntl(link_fd, F_SETFL, O_NONBLOCK);
    scratch = calloc(1, SCRATCH_BYTES);
    if (!scratch) return 1;
    bootinfo[0] = UINT64_C(0x4f464e49314e3151); /* magic "Q1N1INFO" */
    bootinfo[1] = 1;
    bootinfo[2] = sizeof(bootinfo);
    bootinfo[24] = (uintptr_t)scratch;           /* heap_base */
    bootinfo[25] = SCRATCH_BYTES;                /* heap_size */
    bootinfo[32] = (uintptr_t)&q1n1_proxy_stats; /* proxy_stats */
    bootinfo[38] = q1n1_platform_hz();           /* timer_hz */
    printf("%s\n", name);
    printf("%p %p %p\n", (void *)scratch, (void *)(uintptr_t)adder, (void *)bootinfo);
    printf("%p\n", (void *)(uintptr_t)serror_schedule);
    fflush(stdout);
    q1n1_proxy_run(&(struct q1n1_io){.read = io_read, .write = io_write, .poll = io_poll, .ready = io_ready}, Q1N1_START_BOOT, 0,
                   (uintptr_t)bootinfo);
    return 0;
}
