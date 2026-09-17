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
void q1n1_platform_reboot(void)
{
    fprintf(stderr, "harness: reboot requested\n");
    _exit(0);
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

int main(void)
{
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
    fflush(stdout);
    q1n1_proxy_run(&(struct q1n1_io){.read = io_read, .write = io_write, .poll = io_poll, .ready = io_ready}, Q1N1_START_BOOT, 0,
                   (uintptr_t)bootinfo);
    return 0;
}
