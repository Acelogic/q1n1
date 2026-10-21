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

/* SError accounting around every proxy access to target memory.
 *
 * q1n1 runs with PSTATE.A set (entry.S), so an SError raised by an access -- a
 * posted write to an unclocked block, an error response the core reports
 * asynchronously -- is not taken. It stays pending and fires whenever A is
 * next cleared, in code that has nothing to do with the access. The proxy
 * therefore asks the platform to look for a pending SError right before and
 * right after each access, and consumes it where the core allows:
 *
 *   RAS  ESB with A masked defers the SError into DISR_EL1 and clears the
 *        pending state, so it is recorded without ever being taken.
 *   ISR  no FEAT_RAS: ISR_EL1.A is polled, and a pending SError is taken on
 *        purpose in a one-instruction A-unmasked window whose handler records
 *        ESR and returns with A masked (q1n1_serror_window_spsr): an SError
 *        source that stays asserted is then reported as still pending
 *        (unconsumed) instead of being re-taken on every eret.
 *
 * "before" means it was already pending when the access started (raised by
 * earlier work: another request, the USB driver, a late posted write);
 * "during" means it appeared across the access itself. Neither can be seen if
 * EL3 routes SErrors to itself (SCR_EL3.EA=1); then DISR/ISR read as zero and
 * the firmware decides what happens. Counting is always on and changes no
 * reply. With Q1N1_SERROR_STRICT a request that sees one -- or finds one still
 * pending that the poll could neither defer nor take (counted unconsumed) --
 * fails: a proxy op
 * returns Q1N1_S_SERROR (and is not performed when the SError came first), a
 * memory transfer returns the reply status ST_XFRERR. P_Q1N1_SERROR (0x5131)
 * returns the address of the record below and controls it; see q1n1-proxy.c.
 * A REQ_MEMREAD that lies wholly inside the record is plain payload RAM and is
 * never bracketed: it is not refused in strict mode, and reading the record
 * does not consume a pending SError or overwrite the last_* fields that name
 * the access which raised it. */
#define Q1N1_SERROR_MAGIC UINT64_C(0x3130525245533151) /* "Q1SERR01" */
#define Q1N1_SERROR_NONE 0
#define Q1N1_SERROR_ISR 1
#define Q1N1_SERROR_RAS 2
#define Q1N1_SERROR_STRICT 1u
#define Q1N1_SERROR_BEFORE 1
#define Q1N1_SERROR_DURING 2
#define Q1N1_S_SERROR (-0x53)
struct q1n1_serror_state {
    uint64_t magic, mechanism, flags;
    uint64_t checks;          /* bracketed target accesses */
    uint64_t before, during;  /* SErrors found, by when */
    uint64_t refused;         /* strict: accesses not performed */
    uint64_t unconsumed;      /* polls that left ISR_EL1.A set */
    uint64_t last_syndrome;   /* DISR_EL1 (RAS) or ESR_ELx (ISR) */
    uint64_t last_when, last_opcode, last_address;
};
extern struct q1n1_serror_state q1n1_serror;

/* The SPSR the SError vector returns with when it fires inside the deliberate
 * window of q1n1_platform_serror_poll (main.c). Taking the SError leaves the
 * saved SPSR with A clear, as it was in the window. Whether an SError source
 * is level-sensitive is IMPLEMENTATION DEFINED: one that stays asserted would
 * be taken again after every eret and the core would never leave the window.
 * Setting A makes the poll see it as still pending (ISR_EL1.A) instead. */
#define Q1N1_SPSR_A (UINT64_C(1) << 8)
static inline uint64_t q1n1_serror_window_spsr(uint64_t spsr) { return spsr | Q1N1_SPSR_A; }

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
/* Which of Q1N1_SERROR_{NONE,ISR,RAS} this core supports. */
uint64_t q1n1_platform_serror_mechanism(void);
/* Complete outstanding accesses, then consume a pending SError. Returns 1 with
 * *syndrome filled if there was one; *still_pending reports ISR_EL1.A after. */
int q1n1_platform_serror_poll(uint64_t *syndrome, int *still_pending);

/* Leaf routines in proxy-asm.S (no stack use), safe under Q1N1_GUARD_RETURN. */
uint32_t q1n1_checksum_block(const uint8_t *data, uint64_t length, uint32_t sum);
void q1n1_copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t size);

/* Serve requests until P_EXIT. reason/code/info form the REQ_BOOT start
 * message, sent whenever the host (re)opens the port. Returns the exit value. */
uint64_t q1n1_proxy_run(const struct q1n1_io *io, uint32_t reason, uint32_t code, uint64_t info);
