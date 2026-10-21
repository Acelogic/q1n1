/* SPDX-License-Identifier: MIT */
/* See call-service.h. Pure bookkeeping and parsing: no MMIO, no allocation,
 * so tools/test-q1n1-call-service.c drives the same object natively. */
#include "call-service.h"

static uint32_t get16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }
static uint32_t get32(const uint8_t *p) { return get16(p) | get16(p + 2) << 16; }
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v); put16(p + 2, v >> 16); }
static void put64(uint8_t *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }
static void bump(uint64_t *n) { if (*n != UINT64_MAX) (*n)++; }

uint32_t q1n1_crc32(const uint8_t *data, uint32_t length)
{
    uint32_t crc = 0xffffffffu;
    for (uint32_t n = 0; n < length; n++) {
        crc ^= data[n];
        for (unsigned bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

void q1n1_call_init(struct q1n1_call_state *s)
{
    uint64_t *w = (uint64_t *)s;
    for (unsigned n = 0; n < sizeof(*s) / 8; n++) w[n] = 0;
    s->magic = Q1N1_CALL_STATE_MAGIC;
    s->version = 1;
    s->size = sizeof(*s);
}

uint64_t q1n1_call_arm(struct q1n1_call_state *s, uint64_t session)
{
    if (!session) return Q1N1_CALL_E_ARGS;
    s->session = session;
    s->latched = s->taken = 0;
    s->latch_nonce = s->latch_reason = s->latch_delay = s->latch_ticks = 0;
    for (unsigned n = 0; n < 8; n++) s->text[n] = 0;
    s->armed = 1;
    bump(&s->arms);
    return 0;
}

uint64_t q1n1_call_disarm(struct q1n1_call_state *s)
{
    s->armed = 0;
    bump(&s->disarms);
    return 0;
}

static uint32_t reply_with(struct q1n1_call_state *s, uint8_t *reply, uint32_t op, uint64_t nonce,
                           int32_t status, uint32_t flags)
{
    for (unsigned n = 0; n < Q1N1_STOP_REPLY_BYTES; n++) reply[n] = 0;
    put32(reply, Q1N1_STOP_REPLY_MAGIC);
    put16(reply + 4, Q1N1_STOP_VERSION);
    put16(reply + 6, op);
    put64(reply + 8, nonce);
    put64(reply + 16, s->armed ? s->session : 0);
    put32(reply + 24, (uint32_t)status);
    flags |= (s->armed ? Q1N1_STOP_ARMED : 0) | (s->latched ? Q1N1_STOP_LATCHED : 0);
    put32(reply + 28, flags);
    put64(reply + 32, s->latched ? s->latch_nonce : 0);
    put64(reply + 40, s->polls);
    put64(reply + 48, s->dropped_proxy);
    put32(reply + 56, q1n1_crc32(reply, 56));
    s->last_status = (uint64_t)(int64_t)status;
    return Q1N1_STOP_REPLY_BYTES;
}

uint32_t q1n1_stop_datagram(struct q1n1_call_state *s, const uint8_t *data, uint32_t length,
                            uint64_t now, uint8_t *reply)
{
    bump(&s->datagrams);
    /* Not even our magic: somebody else's traffic on this port. Silence. */
    if (!data || length < 8 || get32(data) != Q1N1_STOP_REQUEST_MAGIC) {
        bump(&s->malformed);
        return 0;
    }
    uint32_t op = get16(data + 6);
    uint64_t nonce = length >= 16 ? get64(data + 8) : 0;
    if (length != Q1N1_STOP_REQUEST_BYTES || get16(data + 4) != Q1N1_STOP_VERSION ||
        get32(data + 100) || q1n1_crc32(data, 96) != get32(data + 96) || data[95] ||
        (op != Q1N1_STOP_QUERY && op != Q1N1_STOP_STOP)) {
        bump(&s->malformed);
        return reply_with(s, reply, op, nonce, Q1N1_STOP_E_MALFORMED, 0);
    }
    if (op == Q1N1_STOP_QUERY) {
        bump(&s->queries);
        return reply_with(s, reply, op, nonce, Q1N1_STOP_OK, 0);
    }
    bump(&s->stops);
    uint64_t session = get64(data + 16);
    uint32_t reason = get32(data + 24), delay = get32(data + 28);
    int32_t status = !s->armed ? Q1N1_STOP_E_NOT_ARMED :
                     session != s->session ? Q1N1_STOP_E_SESSION :
                     !reason || reason > Q1N1_STOP_REASON_MAX ? Q1N1_STOP_E_REASON :
                     delay > Q1N1_STOP_DELAY_LIMIT ? Q1N1_STOP_E_DELAY : Q1N1_STOP_OK;
    if (status) {
        bump(&s->refused);
        return reply_with(s, reply, op, nonce, status, 0);
    }
    /* A fresh immediate abort may replace a delayed host latch, even after
     * delivery. Retries with the same nonce cannot edit their request; an
     * immediate latch can never be delayed by later traffic. No session rearm. */
    if (s->latched && !(delay == 0 && s->latch_delay != 0 && nonce != s->latch_nonce))
        return reply_with(s, reply, op, nonce, Q1N1_STOP_ALREADY, 0);
    s->taken = 0;
    s->latched = 1;
    s->latch_nonce = nonce;
    s->latch_reason = reason;
    s->latch_delay = delay;
    s->latch_ticks = now;
    for (unsigned n = 0; n < 8; n++) s->text[n] = get64(data + 32 + 8 * n);
    return reply_with(s, reply, op, nonce, Q1N1_STOP_OK, Q1N1_STOP_MINE);
}

int q1n1_console_request(const uint8_t *data, uint32_t length)
{
    return data && length >= 4 && get32(data) == Q1N1_CONSOLE_REQUEST_MAGIC;
}

/* The payload's own memory, so it is checked as an input like any other: a
 * view that would have us write past its staging buffer is no view. */
static int console_view_ok(const struct q1n1_console_view *v)
{
    return v && v->magic == Q1N1_CONSOLE_VIEW_MAGIC && v->version == Q1N1_CONSOLE_VIEW_VERSION &&
           v->out_base && v->out_size && v->in_base && v->in_size &&
           v->in_room <= v->in_size && v->in_fill <= v->in_room;
}

static uint32_t console_reply(struct q1n1_call_state *s, uint8_t *reply, uint32_t op, uint64_t nonce,
                              int32_t status, uint32_t flags, const struct q1n1_console_view *v,
                              uint64_t out_offset, uint32_t out_length)
{
    /* out[] is already in place at Q1N1_CONSOLE_REPLY_HEADER. */
    for (unsigned n = 0; n < Q1N1_CONSOLE_REPLY_HEADER; n++) reply[n] = 0;
    put32(reply, Q1N1_CONSOLE_REPLY_MAGIC);
    put16(reply + 4, Q1N1_CONSOLE_VERSION);
    put16(reply + 6, op);
    put64(reply + 8, nonce);
    put64(reply + 16, s->armed ? s->session : 0);
    put32(reply + 24, (uint32_t)status);
    put32(reply + 28, flags | (s->armed ? Q1N1_CONSOLE_F_ARMED : 0));
    put64(reply + 32, v ? v->in_held : 0);
    put64(reply + 40, out_offset);
    put64(reply + 48, v ? v->out_produced : 0);
    put64(reply + 56, s->polls);
    put16(reply + 64, out_length);
    put16(reply + 66, v ? (uint32_t)(v->in_room - v->in_fill) : 0);
    uint32_t end = Q1N1_CONSOLE_REPLY_HEADER + out_length;
    put32(reply + end, q1n1_crc32(reply, end));
    s->last_status = (uint64_t)(int64_t)status;
    return end + 4;
}

uint32_t q1n1_console_datagram(struct q1n1_call_state *s, struct q1n1_console_view *v,
                               const uint8_t *data, uint32_t length,
                               uint8_t *reply, uint32_t capacity)
{
    bump(&s->datagrams);
    if (!q1n1_console_request(data, length) ||
        capacity < Q1N1_CONSOLE_REPLY_HEADER + 4 || capacity > 0xffff) {
        bump(&s->malformed);
        return 0;
    }
    uint32_t op = length >= 8 ? get16(data + 6) : 0;
    uint64_t nonce = length >= 16 ? get64(data + 8) : 0;
    uint32_t in_length = length >= 50 ? get16(data + 48) : 0;
    if (length < Q1N1_CONSOLE_REQUEST_HEADER + 4 || get16(data + 4) != Q1N1_CONSOLE_VERSION ||
        op != Q1N1_CONSOLE_EXCHANGE || in_length > Q1N1_CONSOLE_IN_MAX ||
        length != Q1N1_CONSOLE_REQUEST_HEADER + in_length + 4 ||
        q1n1_crc32(data, Q1N1_CONSOLE_REQUEST_HEADER + in_length) !=
            get32(data + Q1N1_CONSOLE_REQUEST_HEADER + in_length) ||
        (get32(data + 52) & ~(Q1N1_CONSOLE_TAKEOVER | Q1N1_CONSOLE_PEEK)) ||
        ((get32(data + 52) & Q1N1_CONSOLE_PEEK) &&
         (in_length || (get32(data + 52) & Q1N1_CONSOLE_TAKEOVER))) ||
        !get64(data + 24)) {
        bump(&s->malformed);
        return console_reply(s, reply, op, nonce, Q1N1_STOP_E_MALFORMED, 0, 0, 0, 0);
    }
    uint64_t session = get64(data + 16), owner = get64(data + 24);
    uint64_t in_offset = get64(data + 32), out_ack = get64(data + 40);
    uint32_t out_max = get16(data + 50), flags = get32(data + 52);
    const uint8_t *in = data + Q1N1_CONSOLE_REQUEST_HEADER;
    int view = console_view_ok(v);
    int32_t status = !s->armed ? Q1N1_STOP_E_NOT_ARMED :
                     session != s->session ? Q1N1_STOP_E_SESSION :
                     !view ? Q1N1_CONSOLE_E_NO_VIEW : Q1N1_STOP_OK;
    if (status) {
        bump(&s->refused);
        if (view) bump(&v->refused);
        return console_reply(s, reply, op, nonce, status, 0, view ? v : 0, 0, 0);
    }
    bump(&v->requests);
    int peek = !!(flags & Q1N1_CONSOLE_PEEK);
    if (!peek && v->owner != owner) {
        if (v->owner && !(flags & Q1N1_CONSOLE_TAKEOVER)) {
            bump(&v->refused);
            return console_reply(s, reply, op, nonce, Q1N1_CONSOLE_E_OWNER, Q1N1_CONSOLE_F_VIEW, v, 0, 0);
        }
        if (v->owner) bump(&v->owner_changes);
        v->owner = owner;
    }

    /* Input: append only the bytes that continue what is held. */
    if (in_length) {
        uint64_t end = in_offset + in_length;
        if (end < in_offset || in_offset > v->in_held) {
            bump(&v->in_gaps);
        } else if (end <= v->in_held) {
            bump(&v->in_duplicate);
        } else {
            uint64_t skip = v->in_held - in_offset, want = in_length - skip;
            uint64_t room = v->in_room - v->in_fill, take = want < room ? want : room;
            if (skip) bump(&v->in_duplicate);
            if (take < want) bump(&v->in_overflow);
            uint8_t *staging = (uint8_t *)(uintptr_t)v->in_base;
            for (uint64_t n = 0; n < take; n++) staging[v->in_fill + n] = in[skip + n];
            v->in_fill += take;
            v->in_held += take;
        }
    }

    /* Output: from what the host has, never past what exists, never older
     * than the ring still holds. */
    uint32_t reply_flags = Q1N1_CONSOLE_F_VIEW | (peek ? 0 : Q1N1_CONSOLE_F_OWNER);
    uint64_t produced = v->out_produced, start = out_ack < produced ? out_ack : produced;
    if (produced - start > v->out_size) {
        start = produced - v->out_size;
        reply_flags |= Q1N1_CONSOLE_F_GAP;
        bump(&v->out_gaps);
    }
    uint64_t limit = capacity - Q1N1_CONSOLE_REPLY_HEADER - 4;
    if (limit > Q1N1_CONSOLE_OUT_MAX) limit = Q1N1_CONSOLE_OUT_MAX;
    if (out_max && out_max < limit) limit = out_max;
    uint64_t count = produced - start < limit ? produced - start : limit;
    const uint8_t *ring = (const uint8_t *)(uintptr_t)v->out_base;
    uint64_t at = start % v->out_size;
    for (uint64_t n = 0; n < count; n++) {
        reply[Q1N1_CONSOLE_REPLY_HEADER + n] = ring[at];
        if (++at == v->out_size) at = 0;
    }
    v->out_served += count;
    return console_reply(s, reply, op, nonce, Q1N1_STOP_OK, reply_flags, v, start, (uint32_t)count);
}

uint64_t q1n1_call_take(struct q1n1_call_state *s, void *out, uint64_t size)
{
    if (!s->armed || !s->latched || s->taken) return 0;
    if (!out || size < sizeof(struct q1n1_stop_latch)) return Q1N1_CALL_E_ARGS;
    struct q1n1_stop_latch *l = out;
    l->magic = Q1N1_STOP_LATCH_MAGIC;
    l->session = s->session;
    l->nonce = s->latch_nonce;
    l->reason = s->latch_reason;
    l->delay_s = s->latch_delay;
    l->latched_ticks = s->latch_ticks;
    for (unsigned n = 0; n < 8; n++) l->text[n] = s->text[n];
    s->taken = 1;
    return 1;
}
