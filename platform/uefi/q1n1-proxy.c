/* SPDX-License-Identifier: MIT */
/* m1n1-compatible proxy request loop for q1n1. See q1n1-proxy.h.
 * Target memory is only touched under an exception guard. Transfer data goes
 * through a bounce buffer, so the transport never faults on a bad address and
 * the byte stream stays in sync after a failed memory write. */
#include "q1n1-proxy.h"

#define REQ_NOP 0x00AA55FFu
#define REQ_PROXY 0x01AA55FFu
#define REQ_MEMREAD 0x02AA55FFu
#define REQ_MEMWRITE 0x03AA55FFu
#define REQ_BOOT 0x04AA55FFu
#define REQ_SIZE 64
#define REPLY_SIZE 36

#define ST_OK 0
#define ST_BADCMD -1
#define ST_XFRERR -3
#define ST_CSUMERR -4
#define S_OK 0
#define S_BADCMD -1

#define FEATURE_DISABLE_DATA_CSUMS 1u
#define CHECKSUM_INIT 0xDEADBEEFu
#define CHECKSUM_FINAL 0xADDEDBADu
#define CHECKSUM_SENTINEL 0xD0DECADEu
#define DATA_END_SENTINEL 0xB0CACC10u
#define IDLE_TIMEOUT_SECONDS 5
#define BOUNCE_BYTES 65536

/* m1n1 src/proxy.h opcode numbers. */
enum {
    P_NOP = 0x000, P_EXIT, P_CALL, P_GET_BOOTARGS, P_GET_BASE, P_SET_BAUD, P_UDELAY,
    P_SET_EXC_GUARD, P_GET_EXC_COUNT, P_VECTOR = 0x00b, P_REBOOT = 0x010,
    P_WRITE64 = 0x100, P_WRITE32, P_WRITE16, P_WRITE8, P_READ64, P_READ32, P_READ16, P_READ8,
    P_SET64, P_SET32, P_SET16, P_SET8, P_CLEAR64, P_CLEAR32, P_CLEAR16, P_CLEAR8,
    P_MASK64, P_MASK32, P_MASK16, P_MASK8, P_WRITEREAD64, P_WRITEREAD32, P_WRITEREAD16, P_WRITEREAD8,
    P_MEMCPY64 = 0x200, P_MEMCPY32, P_MEMCPY16, P_MEMCPY8, P_MEMSET64, P_MEMSET32, P_MEMSET16, P_MEMSET8,
    P_IC_IALLUIS = 0x300, P_IC_IALLU, P_IC_IVAU, P_DC_IVAC, P_DC_ISW, P_DC_CSW, P_DC_CISW, P_DC_ZVA,
    P_DC_CVAC, P_DC_CVAU, P_DC_CIVAC,
    P_FB_INIT = 0xd00, P_FB_SHUTDOWN,
};

volatile uint64_t q1n1_exc_guard, q1n1_exc_count;
struct q1n1_proxy_stats q1n1_proxy_stats;
struct q1n1_next_stage q1n1_next_stage;

void q1n1_memcpy64(uint64_t dst, uint64_t src, uint64_t size);
void q1n1_memcpy32(uint64_t dst, uint64_t src, uint64_t size);
void q1n1_memcpy16(uint64_t dst, uint64_t src, uint64_t size);
void q1n1_memcpy8(uint64_t dst, uint64_t src, uint64_t size);
void q1n1_memset64(uint64_t dst, uint64_t value, uint64_t size);
void q1n1_memset32(uint64_t dst, uint64_t value, uint64_t size);
void q1n1_memset16(uint64_t dst, uint64_t value, uint64_t size);
void q1n1_memset8(uint64_t dst, uint64_t value, uint64_t size);

/* Cache maintenance: uploading code needs at least DC CVAU + IC IVAU.
 * The native protocol harness builds without them (EL0 would trap). */
#if defined(__aarch64__) && !defined(Q1N1_PROXY_HOST_TEST)
#define Q1N1_CACHE_OPS 1
static uint64_t cache_line(int instruction)
{
    uint64_t ctr;
    __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    return 4ull << (instruction ? (ctr & 15) : ((ctr >> 16) & 15));
}
#define CACHE_RANGE(op, kind) \
    do { \
        uint64_t line = cache_line(kind), start = a[0] & ~(line - 1), end = a[0] + a[1]; \
        for (uint64_t p = start; p < end; p += line) __asm__ volatile(op ", %0" : : "r"(p) : "memory"); \
        __asm__ volatile("dsb ish" ::: "memory"); \
    } while (0)
#endif

static int default_checksum_state;
static int *checksum_state = &default_checksum_state;
#define data_checksums_off (*checksum_state)
static uint8_t bounce[BOUNCE_BYTES];

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static void put32(uint8_t *p, uint32_t v) { for (unsigned n = 0; n < 4; n++) p[n] = (uint8_t)(v >> (8 * n)); }
static void put64(uint8_t *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }
static uint32_t checksum(const uint8_t *p, uint64_t n) { return q1n1_checksum_block(p, n, CHECKSUM_INIT) ^ CHECKSUM_FINAL; }

static int read_full(const struct q1n1_io *io, uint8_t *p, size_t n)
{
    uint64_t hz = q1n1_platform_hz(), last = q1n1_platform_ticks();
    while (n) {
        if (!io->ready(io->ctx)) return 0;
        size_t got = io->read(io->ctx, p, n);
        if (got) { p += got; n -= got; last = q1n1_platform_ticks(); continue; }
        if (q1n1_platform_ticks() - last > hz * IDLE_TIMEOUT_SECONDS) { q1n1_proxy_stats.timeouts++; return 0; }
        io->poll(io->ctx);
    }
    return 1;
}
static int write_full(const struct q1n1_io *io, const uint8_t *p, size_t n)
{
    uint64_t hz = q1n1_platform_hz(), last = q1n1_platform_ticks();
    while (n) {
        if (!io->ready(io->ctx)) return 0;
        size_t sent = io->write(io->ctx, p, n);
        if (sent) { p += sent; n -= sent; last = q1n1_platform_ticks(); continue; }
        if (q1n1_platform_ticks() - last > hz * IDLE_TIMEOUT_SECONDS) { q1n1_proxy_stats.timeouts++; return 0; }
        io->poll(io->ctx);
    }
    return 1;
}
static int send_reply(const struct q1n1_io *io, uint32_t type, int32_t status, const uint8_t *data)
{
    uint8_t reply[REPLY_SIZE] = {0};
    put32(reply, type);
    put32(reply + 4, (uint32_t)status);
    for (unsigned n = 0; data && n < 24; n++) reply[8 + n] = data[n];
    put32(reply + 32, checksum(reply, 32));
    return write_full(io, reply, sizeof(reply));
}

static void delay(const struct q1n1_io *io, uint64_t microseconds)
{
    uint64_t start = q1n1_platform_ticks(), wait = q1n1_platform_hz() * microseconds / 1000000;
    while (q1n1_platform_ticks() - start < wait) io->poll(io->ctx);
}

#define R64(a) (*(volatile uint64_t *)(uintptr_t)(a))
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define R16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define R8(a) (*(volatile uint8_t *)(uintptr_t)(a))

/* Returns 1 when the request ends this proxy session (P_EXIT). */
static int proxy_request(const struct q1n1_io *io, const uint8_t *request, uint8_t *reply, uint64_t *exit_value)
{
    uint64_t op = get64(request), a[6];
    for (unsigned n = 0; n < 6; n++) a[n] = get64(request + 8 + 8 * n);
    int64_t status = S_OK;
    uint64_t value = 0, guard = q1n1_exc_guard;
    int stop = 0;
    q1n1_proxy_stats.proxy_calls++;
    q1n1_proxy_stats.last_opcode = op;
    switch (op) {
    case P_NOP: break;
    case P_EXIT: *exit_value = a[0] ? a[0] : 1; stop = 1; break;
    case P_CALL:
        value = ((uint64_t (*)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t))(uintptr_t)a[0])(a[1], a[2], a[3], a[4], a[5]);
        break;
    case P_GET_BOOTARGS: value = q1n1_platform_bootargs(); break;
    case P_GET_BASE: value = q1n1_platform_base(); break;
    case P_FB_INIT: value = q1n1_platform_fb_console(1); break;
    case P_FB_SHUTDOWN:
        /* Keep the guest's pixels; restoring a saved logo is not supported. */
        if (a[0]) { status = S_BADCMD; break; }
        value = q1n1_platform_fb_console(0);
        break;
    case P_UDELAY: delay(io, a[0]); break;
    case P_SET_EXC_GUARD: q1n1_exc_count = 0; guard = a[0]; break;
    case P_GET_EXC_COUNT: value = q1n1_exc_count; q1n1_exc_count = 0; break;
    case P_VECTOR:
        /* Reply first, then the caller leaves the loop and jumps. */
        q1n1_next_stage.entry = a[0];
        q1n1_next_stage.argument = a[1];
        *exit_value = 2;
        stop = 1;
        break;
    case P_REBOOT: q1n1_platform_reboot();

    case P_WRITE64: q1n1_exc_guard = Q1N1_GUARD_SKIP; R64(a[0]) = a[1]; break;
    case P_WRITE32: q1n1_exc_guard = Q1N1_GUARD_SKIP; R32(a[0]) = (uint32_t)a[1]; break;
    case P_WRITE16: q1n1_exc_guard = Q1N1_GUARD_SKIP; R16(a[0]) = (uint16_t)a[1]; break;
    case P_WRITE8: q1n1_exc_guard = Q1N1_GUARD_SKIP; R8(a[0]) = (uint8_t)a[1]; break;
    case P_READ64: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R64(a[0]); break;
    case P_READ32: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R32(a[0]); break;
    case P_READ16: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R16(a[0]); break;
    case P_READ8: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R8(a[0]); break;
    case P_SET64: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R64(a[0]) | a[1]; R64(a[0]) = value; break;
    case P_SET32: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R32(a[0]) | (uint32_t)a[1]; R32(a[0]) = (uint32_t)value; break;
    case P_SET16: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R16(a[0]) | (uint16_t)a[1]; R16(a[0]) = (uint16_t)value; break;
    case P_SET8: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R8(a[0]) | (uint8_t)a[1]; R8(a[0]) = (uint8_t)value; break;
    case P_CLEAR64: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R64(a[0]) & ~a[1]; R64(a[0]) = value; break;
    case P_CLEAR32: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R32(a[0]) & (uint32_t)~a[1]; R32(a[0]) = (uint32_t)value; break;
    case P_CLEAR16: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R16(a[0]) & (uint16_t)~a[1]; R16(a[0]) = (uint16_t)value; break;
    case P_CLEAR8: q1n1_exc_guard = Q1N1_GUARD_MARK; value = R8(a[0]) & (uint8_t)~a[1]; R8(a[0]) = (uint8_t)value; break;
    case P_MASK64: q1n1_exc_guard = Q1N1_GUARD_MARK; value = (R64(a[0]) & ~a[1]) | a[2]; R64(a[0]) = value; break;
    case P_MASK32:
        q1n1_exc_guard = Q1N1_GUARD_MARK; value = (R32(a[0]) & (uint32_t)~a[1]) | (uint32_t)a[2]; R32(a[0]) = (uint32_t)value; break;
    case P_MASK16:
        q1n1_exc_guard = Q1N1_GUARD_MARK; value = (R16(a[0]) & (uint16_t)~a[1]) | (uint16_t)a[2]; R16(a[0]) = (uint16_t)value; break;
    case P_MASK8:
        q1n1_exc_guard = Q1N1_GUARD_MARK; value = (R8(a[0]) & (uint8_t)~a[1]) | (uint8_t)a[2]; R8(a[0]) = (uint8_t)value; break;
    case P_WRITEREAD64: q1n1_exc_guard = Q1N1_GUARD_MARK; R64(a[0]) = a[1]; value = R64(a[0]); break;
    case P_WRITEREAD32: q1n1_exc_guard = Q1N1_GUARD_MARK; R32(a[0]) = (uint32_t)a[1]; value = R32(a[0]); break;
    case P_WRITEREAD16: q1n1_exc_guard = Q1N1_GUARD_MARK; R16(a[0]) = (uint16_t)a[1]; value = R16(a[0]); break;
    case P_WRITEREAD8: q1n1_exc_guard = Q1N1_GUARD_MARK; R8(a[0]) = (uint8_t)a[1]; value = R8(a[0]); break;

    case P_MEMCPY64: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memcpy64(a[0], a[1], a[2]); break;
    case P_MEMCPY32: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memcpy32(a[0], a[1], a[2]); break;
    case P_MEMCPY16: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memcpy16(a[0], a[1], a[2]); break;
    case P_MEMCPY8: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memcpy8(a[0], a[1], a[2]); break;
    case P_MEMSET64: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memset64(a[0], a[1], a[2]); break;
    case P_MEMSET32: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memset32(a[0], a[1], a[2]); break;
    case P_MEMSET16: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memset16(a[0], a[1], a[2]); break;
    case P_MEMSET8: q1n1_exc_guard = Q1N1_GUARD_RETURN; q1n1_memset8(a[0], a[1], a[2]); break;
#ifdef Q1N1_CACHE_OPS
    case P_IC_IALLUIS: __asm__ volatile("ic ialluis\n dsb ish\n isb" ::: "memory"); break;
    case P_IC_IALLU: __asm__ volatile("ic iallu\n dsb nsh\n isb" ::: "memory"); break;
    case P_IC_IVAU: CACHE_RANGE("ic ivau", 1); __asm__ volatile("isb" ::: "memory"); break;
    case P_DC_IVAC: CACHE_RANGE("dc ivac", 0); break;
    case P_DC_ZVA: CACHE_RANGE("dc zva", 0); break;
    case P_DC_CVAC: CACHE_RANGE("dc cvac", 0); break;
    case P_DC_CVAU: CACHE_RANGE("dc cvau", 0); break;
    case P_DC_CIVAC: CACHE_RANGE("dc civac", 0); break;
#endif
    default:
        status = S_BADCMD;
        q1n1_proxy_stats.bad_commands++;
        break;
    }
    q1n1_exc_guard = guard;
    put64(reply, op);
    put64(reply + 8, (uint64_t)status);
    put64(reply + 16, value);
    return stop;
}

static void memory_read(const struct q1n1_io *io, uint32_t type, uint64_t address, uint64_t size)
{
    uint8_t data[24] = {0};
    q1n1_proxy_stats.memreads++;
    /* The guarded checksum pass also proves the whole range is readable before
     * any data is promised, even when data checksums are disabled. */
    q1n1_exc_count = 0;
    q1n1_exc_guard = Q1N1_GUARD_RETURN;
    uint32_t sum = q1n1_checksum_block((const uint8_t *)(uintptr_t)address, size, CHECKSUM_INIT) ^ CHECKSUM_FINAL;
    q1n1_exc_guard = Q1N1_GUARD_OFF;
    if (q1n1_exc_count) { send_reply(io, type, ST_XFRERR, data); return; }
    put32(data, data_checksums_off ? CHECKSUM_SENTINEL : sum);
    if (!send_reply(io, type, ST_OK, data)) return;
    for (uint64_t done = 0; done < size;) {
        uint64_t n = size - done < BOUNCE_BYTES ? size - done : BOUNCE_BYTES;
        q1n1_exc_guard = Q1N1_GUARD_RETURN;
        q1n1_copy_bytes(bounce, (const uint8_t *)(uintptr_t)(address + done), n);
        q1n1_exc_guard = Q1N1_GUARD_OFF;
        if (!write_full(io, bounce, n)) return;
        done += n;
        q1n1_proxy_stats.read_bytes += n;
    }
    if (data_checksums_off) {
        uint8_t sentinel[4];
        put32(sentinel, DATA_END_SENTINEL);
        write_full(io, sentinel, 4);
    }
}

static void memory_write(const struct q1n1_io *io, uint32_t type, uint64_t address, uint64_t size, uint32_t expected)
{
    uint8_t data[24] = {0};
    uint32_t sum = CHECKSUM_INIT;
    q1n1_proxy_stats.memwrites++;
    q1n1_exc_count = 0;
    for (uint64_t done = 0; done < size;) {
        uint64_t n = size - done < BOUNCE_BYTES ? size - done : BOUNCE_BYTES;
        /* A stalled host leaves the stream desynchronized; the magic scan resyncs. */
        if (!read_full(io, bounce, n)) return;
        sum = q1n1_checksum_block(bounce, n, sum);
        if (!q1n1_exc_count) {
            q1n1_exc_guard = Q1N1_GUARD_RETURN;
            q1n1_copy_bytes((uint8_t *)(uintptr_t)(address + done), bounce, n);
            q1n1_exc_guard = Q1N1_GUARD_OFF;
        }
        done += n;
        q1n1_proxy_stats.write_bytes += n;
    }
    int32_t status = q1n1_exc_count ? ST_XFRERR : ST_OK;
    sum = data_checksums_off ? CHECKSUM_SENTINEL : sum ^ CHECKSUM_FINAL;
    if (sum != expected) status = ST_XFRERR;
    if (data_checksums_off) {
        uint8_t sentinel[4];
        if (!read_full(io, sentinel, 4) || get32(sentinel) != DATA_END_SENTINEL) status = ST_XFRERR;
    }
    put32(data, sum);
    send_reply(io, type, status, data);
}

uint64_t q1n1_proxy_run(const struct q1n1_io *io, uint32_t reason, uint32_t code, uint64_t info)
{
    uint32_t window = 0;
    int was_ready = 0;
    uint64_t last_byte = q1n1_platform_ticks();
    int partial = 0;
    for (;;) {
        io->poll(io->ctx);
        if (io->abort && io->abort(io->ctx)) return 0;
        int ready = io->ready(io->ctx);
        if (!ready || (partial && q1n1_platform_ticks() - last_byte >
                        q1n1_platform_hz() * IDLE_TIMEOUT_SECONDS)) {
            window = 0;
            partial = 0;
            if (io->end) io->end(io->ctx);
        }
        if (ready && !was_ready) {
            uint8_t start[24] = {0};
            put32(start, reason);
            put32(start + 4, code);
            put64(start + 8, info);
            send_reply(io, REQ_BOOT, ST_OK, start);
            q1n1_proxy_stats.announcements++;
        }
        was_ready = ready;
        uint8_t byte;
        if (io->read(io->ctx, &byte, 1) != 1) continue;
        last_byte = q1n1_platform_ticks();
        partial = 1;
        window = window >> 8 | (uint32_t)byte << 24;
        if ((window & 0xffffff) != 0xAA55FF) continue;

        uint8_t request[REQ_SIZE], data[24] = {0};
        put32(request, window);
        window = 0;
        partial = 0;
        checksum_state = io->checksum_state ? io->checksum_state(io->ctx) : &default_checksum_state;
        if (!read_full(io, request + 4, REQ_SIZE - 4)) {
            if (io->end) io->end(io->ctx);
            continue;
        }
        q1n1_proxy_stats.requests++;
        uint32_t type = get32(request);
        if (checksum(request, REQ_SIZE - 4) != get32(request + REQ_SIZE - 4)) {
            q1n1_proxy_stats.checksum_errors++;
            send_reply(io, type, ST_CSUMERR, data);
            if (io->end) io->end(io->ctx);
            continue;
        }
        switch (type) {
        case REQ_NOP: {
            uint64_t features = get64(request + 4) & FEATURE_DISABLE_DATA_CSUMS;
            data_checksums_off = features != 0;
            put64(data, features);
            send_reply(io, type, ST_OK, data);
            break;
        }
        case REQ_PROXY: {
            uint64_t exit_value = 0;
            int stop = proxy_request(io, request + 4, data, &exit_value);
            send_reply(io, type, ST_OK, data);
            if (stop) {
                if (io->end) io->end(io->ctx);
                return exit_value;
            }
            break;
        }
        case REQ_MEMREAD:
            if (get64(request + 12)) memory_read(io, type, get64(request + 4), get64(request + 12));
            else send_reply(io, type, ST_OK, data);
            break;
        case REQ_MEMWRITE:
            memory_write(io, type, get64(request + 4), get64(request + 12), get32(request + 20));
            break;
        default:
            send_reply(io, type, ST_BADCMD, data);
            break;
        }
        if (io->end) io->end(io->ctx);
    }
}
