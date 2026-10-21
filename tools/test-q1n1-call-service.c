/* SPDX-License-Identifier: MIT */
/* Native harness for platform/uefi/call-service.c: the host stop request a
 * long P_CALL payload polls for. Parser refusals, first-wins latching, the
 * one-shot hand-off to the payload, arm/disarm, and byte-exact agreement with
 * the datagrams Snapintosh's tools/request-stop.py builds (golden vectors
 * below were produced by that tool; its own test decodes GOLDEN_REPLY).
 *
 *   clang -std=gnu11 -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all \
 *     -Wall -Wextra -Werror -Iplatform/uefi tools/test-q1n1-call-service.c \
 *     platform/uefi/call-service.c -o build/test-q1n1-call-service && build/test-q1n1-call-service
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "call-service.h"

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); exit(1); } } while (0)

static const char GOLDEN_QUERY[] =
    "51315352010001004444333322221111000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000018cf7ec800000000";
static const char GOLDEN_STOP[] =
    "5131535201000200efcdab896745230178563412ed5eed5e0100000000000000636f6e647563746f"
    "723a2072756e206c6f6f6b7320646f6e650000000000000000000000000000000000000000000000"
    "000000000000000000000000000000004f0c152300000000";
static const char GOLDEN_LONG[] =
    "5131535201000200070000000000000078563412ed5eed5e010000001e0000007878787878787878"
    "78787878787878787878787878787878787878787878787878787878787878787878787878787878"
    "7878787878787878787878787878000008780f6900000000";
#define SESSION 0x5eed5eed12345678ull

static uint32_t hex(const char *text, uint8_t *out)
{
    uint32_t n = 0;
    for (; text[2 * n] && text[2 * n + 1]; n++) {
        unsigned value;
        CHECK(sscanf(text + 2 * n, "%2x", &value) == 1);
        out[n] = (uint8_t)value;
    }
    return n;
}
static uint32_t le16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }
static uint32_t le32(const uint8_t *p) { return le16(p) | le16(p + 2) << 16; }
static uint64_t le64(const uint8_t *p) { return le32(p) | (uint64_t)le32(p + 4) << 32; }
static void put32(uint8_t *p, uint32_t v) { for (int n = 0; n < 4; n++) p[n] = (uint8_t)(v >> 8 * n); }
static void put64(uint8_t *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }

static uint32_t build(uint8_t *d, uint32_t op, uint64_t nonce, uint64_t session, uint32_t reason,
                      uint32_t delay, const char *text)
{
    memset(d, 0, Q1N1_STOP_REQUEST_BYTES);
    put32(d, Q1N1_STOP_REQUEST_MAGIC);
    d[4] = Q1N1_STOP_VERSION; d[6] = (uint8_t)op; d[7] = (uint8_t)(op >> 8);
    put64(d + 8, nonce); put64(d + 16, session);
    put32(d + 24, reason); put32(d + 28, delay);
    strncpy((char *)d + 32, text, 63);
    put32(d + 96, q1n1_crc32(d, 96));
    return Q1N1_STOP_REQUEST_BYTES;
}
static void reseal(uint8_t *d) { put32(d + 96, q1n1_crc32(d, 96)); }

/* Every reply: our magic, version, the echoed op/nonce, a valid CRC. */
static int32_t answer(struct q1n1_call_state *s, const uint8_t *d, uint32_t n, uint8_t *reply,
                      uint32_t *flags)
{
    memset(reply, 0xa5, Q1N1_STOP_REPLY_BYTES);
    CHECK(q1n1_stop_datagram(s, d, n, 1234, reply) == Q1N1_STOP_REPLY_BYTES);
    CHECK(le32(reply) == Q1N1_STOP_REPLY_MAGIC && le16(reply + 4) == Q1N1_STOP_VERSION);
    CHECK(le16(reply + 6) == le16(d + 6) && le64(reply + 8) == le64(d + 8));
    CHECK(le32(reply + 56) == q1n1_crc32(reply, 56) && !le32(reply + 60));
    *flags = le32(reply + 28);
    return (int32_t)le32(reply + 24);
}

int main(void)
{
    static const uint8_t check_value[] = "123456789";
    CHECK(q1n1_crc32(check_value, 9) == 0xcbf43926u);            /* CRC-32/ISO-HDLC */
    CHECK(q1n1_crc32(check_value, 0) == 0);

    struct q1n1_call_state s;
    memset(&s, 0xee, sizeof s);
    q1n1_call_init(&s);
    CHECK(s.magic == Q1N1_CALL_STATE_MAGIC && s.version == 1 && s.size == sizeof s);
    CHECK(!s.armed && !s.latched && !s.polls && !s.datagrams && !s.text[7]);

    uint8_t d[128], reply[Q1N1_STOP_REPLY_BYTES], latch_bytes[sizeof(struct q1n1_stop_latch)];
    uint32_t flags;

    /* The tool's QUERY, before any run is armed. */
    uint32_t n = hex(GOLDEN_QUERY, d);
    CHECK(n == Q1N1_STOP_REQUEST_BYTES);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_OK && flags == 0 && !le64(reply + 16));
    CHECK(s.queries == 1 && !s.latched);

    /* The tool's STOP, not armed: refused, nothing latched. */
    n = hex(GOLDEN_STOP, d);
    CHECK(n == Q1N1_STOP_REQUEST_BYTES);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_E_NOT_ARMED && flags == 0);
    CHECK(s.refused == 1 && !s.latched);
    CHECK(q1n1_call_take(&s, latch_bytes, sizeof latch_bytes) == 0);

    /* Arm needs a session. */
    CHECK(q1n1_call_arm(&s, 0) == Q1N1_CALL_E_ARGS && !s.armed);
    CHECK(q1n1_call_arm(&s, SESSION) == 0 && s.armed && s.arms == 1);
    n = hex(GOLDEN_QUERY, d);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_OK && flags == Q1N1_STOP_ARMED &&
          le64(reply + 16) == SESSION);

    /* The tool's STOP for the armed session latches, first wins. */
    s.polls = 3; s.dropped_proxy = 2;
    n = hex(GOLDEN_STOP, d);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_OK);
    CHECK(flags == (Q1N1_STOP_ARMED | Q1N1_STOP_LATCHED | Q1N1_STOP_MINE));
    CHECK(le64(reply + 32) == 0x0123456789abcdefull && le64(reply + 40) == 3 && le64(reply + 48) == 2);
    printf("GOLDEN_REPLY ");
    for (unsigned k = 0; k < Q1N1_STOP_REPLY_BYTES; k++) printf("%02x", reply[k]);
    printf("\n");
    CHECK(s.latched && s.latch_nonce == 0x0123456789abcdefull && s.latch_reason == 1 &&
          s.latch_delay == 0 && s.latch_ticks == 1234 && s.stops == 2);
    CHECK(!memcmp(s.text, "conductor: run looks done", 26));

    /* The payload receives it exactly once, in the monitor's layout. */
    CHECK(q1n1_call_take(&s, NULL, sizeof latch_bytes) == Q1N1_CALL_E_ARGS);
    CHECK(q1n1_call_take(&s, latch_bytes, sizeof latch_bytes - 1) == Q1N1_CALL_E_ARGS);
    CHECK(!s.taken);
    memset(latch_bytes, 0xcc, sizeof latch_bytes);
    CHECK(q1n1_call_take(&s, latch_bytes, sizeof latch_bytes) == 1);
    struct q1n1_stop_latch latch;
    memcpy(&latch, latch_bytes, sizeof latch);
    CHECK(latch.magic == Q1N1_STOP_LATCH_MAGIC && latch.session == SESSION &&
          latch.nonce == 0x0123456789abcdefull && latch.reason == 1 && latch.delay_s == 0 &&
          latch.latched_ticks == 1234 && !memcmp(latch.text, "conductor: run looks done", 26) &&
          !(latch.text[7] >> 56));
    CHECK(q1n1_call_take(&s, latch_bytes, sizeof latch_bytes) == 0);

    /* Later requests are answered but not recorded. */
    build(d, Q1N1_STOP_STOP, 99, SESSION, 1, 5, "second");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_ALREADY);
    CHECK(flags == (Q1N1_STOP_ARMED | Q1N1_STOP_LATCHED) && le64(reply + 32) == 0x0123456789abcdefull);
    CHECK(s.latch_nonce == 0x0123456789abcdefull && s.latch_delay == 0);

    /* Refusals for the armed run, in check order. */
    build(d, Q1N1_STOP_STOP, 5, SESSION ^ 1, 1, 0, "stale");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_SESSION);
    build(d, Q1N1_STOP_STOP, 5, SESSION, 0, 0, "no reason");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_REASON);
    build(d, Q1N1_STOP_STOP, 5, SESSION, 0x10000, 0, "big reason");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_REASON);
    build(d, Q1N1_STOP_STOP, 5, SESSION, 0xffff, Q1N1_STOP_DELAY_LIMIT + 1, "long");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_DELAY);
    CHECK(s.refused == 5);

    /* Malformed but ours: answered MALFORMED so the sender learns why. */
    uint64_t malformed = s.malformed;
    build(d, Q1N1_STOP_QUERY, 6, 0, 0, 0, "");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES - 1, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES + 1, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    d[4] = 2; reseal(d);                                       /* version */
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    build(d, Q1N1_STOP_QUERY, 6, 0, 0, 0, "");
    d[96] ^= 1;                                                /* CRC */
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    build(d, Q1N1_STOP_QUERY, 6, 0, 0, 0, "");
    d[100] = 1;                                                /* reserved word */
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    build(d, Q1N1_STOP_STOP, 6, SESSION, 1, 0, "");
    memset(d + 32, 'z', 64); reseal(d);                        /* text not NUL terminated */
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    build(d, 3, 6, SESSION, 1, 0, "");                         /* unknown op */
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_MALFORMED);
    CHECK(s.malformed == malformed + 7);
    /* Not ours at all: silence, but counted. */
    build(d, Q1N1_STOP_QUERY, 6, 0, 0, 0, "");
    d[0] ^= 0xff;
    CHECK(q1n1_stop_datagram(&s, d, Q1N1_STOP_REQUEST_BYTES, 0, reply) == 0);
    CHECK(q1n1_stop_datagram(&s, d, 7, 0, reply) == 0);
    CHECK(q1n1_stop_datagram(&s, NULL, 104, 0, reply) == 0);
    CHECK(s.malformed == malformed + 10);

    /* The tool's longest text: cut to 62 bytes on a character boundary. */
    CHECK(q1n1_call_arm(&s, SESSION) == 0 && !s.latched && !s.taken && s.arms == 2);
    n = hex(GOLDEN_LONG, d);
    CHECK(n == Q1N1_STOP_REQUEST_BYTES);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_OK && s.latch_delay == 30 && s.latch_nonce == 7);
    char text[65] = {0};
    memcpy(text, s.text, 64);
    CHECK(strlen(text) == 62 && text[0] == 'x' && text[61] == 'x');

    /* Re-arming for a new run forgets the old latch and its session. */
    CHECK(q1n1_call_arm(&s, SESSION + 1) == 0 && !s.latched && !s.text[0]);
    n = hex(GOLDEN_STOP, d);
    CHECK(answer(&s, d, n, reply, &flags) == Q1N1_STOP_E_SESSION);
    build(d, Q1N1_STOP_STOP, 42, SESSION + 1, 7, 12, "next run");
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_OK && s.latch_reason == 7);

    /* Disarmed: nothing is handed out, stops are refused. */
    CHECK(q1n1_call_disarm(&s) == 0 && !s.armed && s.disarms == 1);
    CHECK(q1n1_call_take(&s, latch_bytes, sizeof latch_bytes) == 0);
    CHECK(answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags) == Q1N1_STOP_E_NOT_ARMED);
    CHECK(!(flags & Q1N1_STOP_ARMED) && !le64(reply + 16));

    /* Counters saturate instead of wrapping. */
    s.datagrams = UINT64_MAX;
    build(d, Q1N1_STOP_QUERY, 1, 0, 0, 0, "");
    answer(&s, d, Q1N1_STOP_REQUEST_BYTES, reply, &flags);
    CHECK(s.datagrams == UINT64_MAX);

    printf("PASS: %u q1n1 call-service checks (native harness, sanitizers on)\n", checks);
    return 0;
}
