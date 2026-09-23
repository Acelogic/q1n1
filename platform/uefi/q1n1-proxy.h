/* SPDX-License-Identifier: MIT */
/* Subset of the m1n1 uartproxy wire protocol for q1n1 (polled, transport-agnostic).
 * Framing, checksums, request/reply sizes and opcode numbers match m1n1's
 * src/uartproxy.c, src/proxy.h and proxyclient/m1n1/proxy.py, so the upstream
 * UartInterface/M1N1Proxy classes can drive it. Apple-specific ops are absent. */
#pragma once
#include <stddef.h>
#include <stdint.h>

struct q1n1_io {
    size_t (*read)(void *ctx, uint8_t *buffer, size_t count);        /* non-blocking */
    size_t (*write)(void *ctx, const uint8_t *buffer, size_t count); /* non-blocking: bytes accepted */
    void (*poll)(void *ctx);                                          /* service the device */
    int (*ready)(void *ctx);                                          /* host has the port open */
    /* Optional. Non-zero leaves the loop, so a transport that can go away --
     * an Ethernet link, unlike the console -- does not strand the machine. */
    int (*abort)(void *ctx);
    void *ctx;
    /* Release transport ownership after a complete or abandoned request. */
    void (*end)(void *ctx);
    /* Optional per-connection checksum negotiation state. */
    int *(*checksum_state)(void *ctx);
};

#define Q1N1_GUARD_OFF 0
#define Q1N1_GUARD_SKIP 1
#define Q1N1_GUARD_MARK 2
#define Q1N1_GUARD_RETURN 3
#define Q1N1_GUARD_TYPE_MASK 0xff
#define Q1N1_GUARD_SILENT 0x100
#define Q1N1_GUARD_MARKER UINT64_C(0xacce5515abad1dea)

#define Q1N1_START_BOOT 0
#define Q1N1_START_EXCEPTION 1
#define Q1N1_EXC_RET_UNHANDLED 1
#define Q1N1_EXC_RET_HANDLED 2

/* The exception vector adjusts these; the proxy sets them around each access. */
extern volatile uint64_t q1n1_exc_guard, q1n1_exc_count;

struct q1n1_proxy_stats {
    uint64_t requests, proxy_calls, memreads, memwrites, read_bytes, write_bytes;
    uint64_t checksum_errors, timeouts, bad_commands, exceptions, announcements;
    uint64_t last_opcode;
};
extern struct q1n1_proxy_stats q1n1_proxy_stats;

/* Set by P_VECTOR: where to jump once the proxy loop returns, and with what. */
struct q1n1_next_stage { uint64_t entry, argument; };
extern struct q1n1_next_stage q1n1_next_stage;

/* Provided by the payload. */
uint64_t q1n1_platform_ticks(void);
uint64_t q1n1_platform_hz(void);
uint64_t q1n1_platform_bootargs(void);
uint64_t q1n1_platform_base(void);
/* Pause/resume q1n1's console drawing without changing GOP scanout.
 * Returns the previous enabled state; ownership survives proxy calls. */
uint64_t q1n1_platform_fb_console(int enabled);
void q1n1_platform_reboot(void) __attribute__((noreturn));

/* Leaf routines in proxy-asm.S (no stack use), safe under Q1N1_GUARD_RETURN. */
uint32_t q1n1_checksum_block(const uint8_t *data, uint64_t length, uint32_t sum);
void q1n1_copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t size);

/* Serve requests until P_EXIT. reason/code/info form the REQ_BOOT start
 * message, sent whenever the host (re)opens the port. Returns the exit value. */
uint64_t q1n1_proxy_run(const struct q1n1_io *io, uint32_t reason, uint32_t code, uint64_t info);
