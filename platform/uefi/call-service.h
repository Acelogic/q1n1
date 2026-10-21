/* SPDX-License-Identifier: MIT */
/* Out-of-band service for a long P_CALL payload: a host stop request.
 *
 * While a P_CALL runs, the proxy loop is blocked inside it and nothing else
 * services the USB links: q1n1 is polled, and a payload such as Snapintosh's
 * monitor owns the EL2 timer and vectors for the whole call. So the Mac has
 * no way to end a long run early, and killing the host side leaves the call
 * running. This gives the payload a cheap way to look:
 *
 *   q1n1_call_service(op, a1, a2, a3), published as bootinfo call_service and
 *   called only by the CPU that is inside the P_CALL, at a bounded rate:
 *     Q1N1_CALL_ARM     a1 = session (nonzero): clear any latch, start accepting
 *     Q1N1_CALL_POLL    a1 = struct q1n1_stop_latch *, a2 = its size: service
 *                       the host-role NCM links once; 1 and the latch copied
 *                       out the first time a stop is latched, else 0.
 *                       a3 = struct q1n1_console_view * or 0: the console
 *                       stream below is served from it during this poll
 *     Q1N1_CALL_DISARM  stop accepting
 *
 * The Mac sends datagrams to UDP port Q1N1_STOP_PORT on the proxy's own
 * link-local addresses (Snapintosh tools/request-stop.py): QUERY returns the
 * armed session, STOP latches a request for that session. The first STOP
 * wins among delayed requests. A fresh delay-zero STOP can supersede a delayed
 * latch and is delivered again, including after its first delivery. A delay-zero
 * latch is immutable. Other requests get Q1N1_STOP_ALREADY. During a POLL, proxy-port
 * datagrams are dropped and counted -- never queued, and never allowed to
 * re-point the proxy peer -- so the P_CALL's own reply still reaches the
 * client that is waiting for it. Outside a call the stop port still answers,
 * with "not armed", so the host can tell an idle q1n1 from no q1n1.
 *
 * A POLL never waits on the link: its replies are started and left in flight
 * (ncm_send_side), one at a time, and a reply the link cannot take yet is
 * dropped and counted (reply_failures) for the host to retry. A POLL that
 * faults and never returns is undone by the proxy loop once the P_CALL has
 * unwound (main.c managed_usb_poll; counted in `recoveries`).
 *
 * Everything here is stage-local: no shared-across-chainload layout changes. */
#pragma once
#include <stdint.h>

#define Q1N1_STOP_PORT 4920

#define Q1N1_CALL_ARM    1u
#define Q1N1_CALL_POLL   2u
#define Q1N1_CALL_DISARM 3u
#define Q1N1_CALL_E_OP   UINT64_C(0xffffffffffffffff)   /* -1 */
#define Q1N1_CALL_E_ARGS UINT64_C(0xfffffffffffffffe)   /* -2 */

/* The latch the payload receives: Snapintosh compat/early-stop.h
 * struct early_stop_host_request, word for word. */
#define Q1N1_STOP_LATCH_MAGIC UINT64_C(0x3154414c52533151)   /* "Q1SRLAT1" */
struct q1n1_stop_latch {
    uint64_t magic, session, nonce, reason, delay_s, latched_ticks;
    uint64_t text[8];                                    /* NUL padded, byte 63 NUL */
};
_Static_assert(sizeof(struct q1n1_stop_latch) == 14 * 8, "early_stop_host_request layout");

/* Published record (bootinfo call_service_state), all u64. */
#define Q1N1_CALL_STATE_MAGIC UINT64_C(0x4c4c4143314e3151)   /* "Q1N1CALL" */
struct q1n1_call_state {
    uint64_t magic, version, size;
    uint64_t armed, session, arms, disarms, polls;
    uint64_t datagrams, queries, stops, malformed, refused, replies, reply_failures, dropped_proxy;
    uint64_t latched, taken, latch_nonce, latch_reason, latch_delay, latch_ticks, last_status;
    uint64_t text[8];
    uint64_t recoveries;   /* call polls that never returned, undone by the proxy loop */
};
_Static_assert(sizeof(struct q1n1_call_state) == 32 * 8, "call state layout");

/* Wire format, little-endian. Request (Q1N1_STOP_REQUEST_BYTES):
 *    0 u32 magic "Q1SR"   4 u16 version 1   6 u16 op (QUERY 1, STOP 2)
 *    8 u64 nonce          (the requester's own; echoed)
 *   16 u64 session        (STOP: the armed session; QUERY: 0)
 *   24 u32 reason         (1 = operator; 1..0xffff)
 *   28 u32 delay_s        (<= Q1N1_STOP_DELAY_LIMIT; the monitor clamps it further)
 *   32 u8  text[64]       (UTF-8, NUL padded; byte 95 must be NUL)
 *   96 u32 crc32          (IEEE, as zlib.crc32, over bytes 0..95)
 *  100 u32 zero
 * Reply (Q1N1_STOP_REPLY_BYTES), sent from Q1N1_STOP_PORT to the sender:
 *    0 u32 magic "Q1SA"   4 u16 version 1   6 u16 op (echo)
 *    8 u64 nonce (echo)  16 u64 session (armed, else 0)
 *   24 i32 status        28 u32 flags (Q1N1_STOP_ARMED/_LATCHED/_MINE)
 *   32 u64 latch nonce   40 u64 polls   48 u64 proxy datagrams dropped
 *   56 u32 crc32 over bytes 0..55      60 u32 zero */
#define Q1N1_STOP_REQUEST_MAGIC 0x52533151u   /* "Q1SR" */
#define Q1N1_STOP_REPLY_MAGIC   0x41533151u   /* "Q1SA" */
#define Q1N1_STOP_VERSION 1u
#define Q1N1_STOP_QUERY 1u
#define Q1N1_STOP_STOP  2u
#define Q1N1_STOP_REQUEST_BYTES 104u
#define Q1N1_STOP_REPLY_BYTES 64u
#define Q1N1_STOP_TEXT 64u
#define Q1N1_STOP_DELAY_LIMIT 3600u
#define Q1N1_STOP_REASON_MAX 0xffffu

#define Q1N1_STOP_OK           0
#define Q1N1_STOP_ALREADY      1    /* an earlier STOP holds the latch; this one is not recorded */
#define Q1N1_STOP_E_MALFORMED (-1)
#define Q1N1_STOP_E_NOT_ARMED (-2)
#define Q1N1_STOP_E_SESSION   (-3)
#define Q1N1_STOP_E_REASON    (-4)
#define Q1N1_STOP_E_DELAY     (-5)
#define Q1N1_STOP_ARMED   1u
#define Q1N1_STOP_LATCHED 2u
#define Q1N1_STOP_MINE    4u

/* Console stream (Snapintosh docs/GUEST-SHELL-DESIGN-20261016.md). The payload
 * hands POLL a console view in a3 (0 for none, which is what an older payload
 * passes): its console ring to read and a staging buffer to append host input
 * to, both in its own memory. Requests with magic "Q1SC" on the stop port are
 * answered from that view during the POLL; outside one, or without a view,
 * they are refused. Every offset is absolute (bytes since the run began), so a
 * lost, repeated or reordered datagram is recovered by sending it again.
 *
 * Request, little-endian (Q1N1_CONSOLE_REQUEST_HEADER + in_length + 4 bytes):
 *    0 u32 magic "Q1SC"   4 u16 version 1   6 u16 op (EXCHANGE 1)
 *    8 u64 nonce          (the requester's own; echoed)
 *   16 u64 session        (the armed session)
 *   24 u64 owner          (nonzero; the first owner holds the console)
 *   32 u64 in_offset      (absolute offset of in[0] in the host's input)
 *   40 u64 out_ack        (console bytes the host already has)
 *   48 u16 in_length      (<= Q1N1_CONSOLE_IN_MAX)
 *   50 u16 out_max        (most output bytes wanted; 0 = as many as fit)
 *   52 u32 flags          (Q1N1_CONSOLE_TAKEOVER or Q1N1_CONSOLE_PEEK; others zero)
 *   56 u8  in[in_length]
 *      u32 crc32          (IEEE, as zlib.crc32, over everything before it)
 * Reply (Q1N1_CONSOLE_REPLY_HEADER + out_length + 4 bytes):
 *    0 u32 magic "Q1SD"   4 u16 version 1   6 u16 op (echo)
 *    8 u64 nonce (echo)  16 u64 session (armed, else 0)
 *   24 i32 status        28 u32 flags (Q1N1_CONSOLE_F_*)
 *   32 u64 in_held       (host input bytes accepted so far)
 *   40 u64 out_offset    (absolute offset of out[0])
 *   48 u64 produced      (console bytes the payload has produced)
 *   56 u64 polls         64 u16 out_length   66 u16 in_room (left this poll)
 *   68 u32 zero          72 u8 out[out_length]
 *      u32 crc32 over everything before it */
#define Q1N1_CONSOLE_REQUEST_MAGIC 0x43533151u   /* "Q1SC" */
#define Q1N1_CONSOLE_REPLY_MAGIC   0x44533151u   /* "Q1SD" */
#define Q1N1_CONSOLE_VERSION 1u
#define Q1N1_CONSOLE_EXCHANGE 1u
#define Q1N1_CONSOLE_REQUEST_HEADER 56u
#define Q1N1_CONSOLE_REPLY_HEADER 72u
#define Q1N1_CONSOLE_IN_MAX 1024u
/* One reply fits one 1500-byte IPv6 packet: 40 + 8 + 1440. */
#define Q1N1_CONSOLE_REPLY_MAX 1440u
#define Q1N1_CONSOLE_OUT_MAX (Q1N1_CONSOLE_REPLY_MAX - Q1N1_CONSOLE_REPLY_HEADER - 4u)
#define Q1N1_CONSOLE_TAKEOVER 1u   /* replace the current owner */
#define Q1N1_CONSOLE_PEEK     2u   /* watch only: no input, never binds or needs the owner */
#define Q1N1_CONSOLE_F_ARMED 1u
#define Q1N1_CONSOLE_F_VIEW  2u   /* the payload passed a valid console view */
#define Q1N1_CONSOLE_F_OWNER 4u   /* this request's owner holds the console */
#define Q1N1_CONSOLE_F_GAP   8u   /* out_offset skips bytes the ring no longer holds */
#define Q1N1_CONSOLE_E_OWNER   (-6)
#define Q1N1_CONSOLE_E_NO_VIEW (-7)

/* The view the payload passes in POLL's a3: Snapintosh compat/console-link.h
 * struct console_link_view, word for word. The payload fills magic..in_fill
 * before each POLL (in_fill = 0) and drains in_base[0..in_fill) after it;
 * q1n1 keeps the rest, which live as long as the payload's view does. */
#define Q1N1_CONSOLE_VIEW_MAGIC UINT64_C(0x3156534e4f433151)   /* "Q1CONSV1" */
#define Q1N1_CONSOLE_VIEW_VERSION 1u
struct q1n1_console_view {
    uint64_t magic, version;
    uint64_t out_base, out_size, out_produced;   /* console ring and its byte count */
    uint64_t in_base, in_size, in_room, in_fill; /* staging buffer, room this poll, bytes appended */
    uint64_t in_held;                            /* absolute host input bytes accepted */
    uint64_t owner, owner_changes;
    uint64_t requests, refused, in_duplicate, in_gaps, in_overflow, out_served, out_gaps;
    uint64_t polls_seen;                         /* POLLs that carried this view */
};
_Static_assert(sizeof(struct q1n1_console_view) == 20 * 8, "console_link_view layout");

void q1n1_call_init(struct q1n1_call_state *s);
uint64_t q1n1_call_arm(struct q1n1_call_state *s, uint64_t session);
uint64_t q1n1_call_disarm(struct q1n1_call_state *s);
/* One stop-port datagram. Fills reply[Q1N1_STOP_REPLY_BYTES] and returns its
 * length, or 0 for no reply (not addressed to this protocol at all). */
uint32_t q1n1_stop_datagram(struct q1n1_call_state *s, const uint8_t *data, uint32_t length,
                            uint64_t now, uint8_t *reply);
/* One console datagram against the payload's view `v` (NULL outside a POLL or
 * when the payload passed none). Fills reply (at most `capacity` bytes) and
 * returns its length, or 0 for no reply. */
uint32_t q1n1_console_datagram(struct q1n1_call_state *s, struct q1n1_console_view *v,
                               const uint8_t *data, uint32_t length,
                               uint8_t *reply, uint32_t capacity);
/* 1 when the first four bytes are the console request magic. */
int q1n1_console_request(const uint8_t *data, uint32_t length);
/* 1 and the latch copied into `out` the first time; 0 when nothing new. */
uint64_t q1n1_call_take(struct q1n1_call_state *s, void *out, uint64_t size);
uint32_t q1n1_crc32(const uint8_t *data, uint32_t length);
