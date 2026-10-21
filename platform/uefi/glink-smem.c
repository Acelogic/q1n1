/* SPDX-License-Identifier: MIT */
/* See glink-smem.h. The wire behaviour follows Linux drivers/rpmsg/
 * qcom_glink_smem.c and qcom_glink_native.c (pinned in the header comments);
 * the code is written for a single-threaded, polled, freestanding stage. */
#include "glink-smem.h"

#define DESC_TX_TAIL 0      /* remote's read index into our FIFO */
#define DESC_TX_HEAD 1      /* ours */
#define DESC_RX_TAIL 2      /* ours */
#define DESC_RX_HEAD 3      /* remote's write index into its FIFO */
#define TX_RESERVE 16u      /* FIFO_FULL_RESERVE + TX_BLOCKED_CMD_RESERVE, qcom_glink_smem.c:30-32 */
#define CONTROL_WAIT_US 50000u
#define FRAMES_PER_POLL 256
#define VERSION_RESENDS 4u
#define OPEN_FALLBACK_US 100000u  /* send our OPEN this long after VERSION even without an ACK */
/* The most one received frame makes us write: OPEN -> OPEN_ACK (8) + OPEN
 * (8 + 32); INTENT_REQ -> INTENT (16) + ACK (8); TX_DATA -> RX_DONE (8) +
 * INTENT (16); CLOSE -> CLOSE_ACK (8) + CLOSE (8). The tick handles a frame
 * only with this much room, so its replies never wait. */
#define SERVICE_TX_ROOM 64u
#define RELEASE_CONFIRM_US 1000u
#define RELEASE_PAD_CONFIRM_US 50000u

#if defined(__aarch64__) && !defined(Q1N1_GLINK_HOST_TEST)
#define WMB() __asm__ volatile("dsb st" ::: "memory")
#define RMB() __asm__ volatile("dsb ld" ::: "memory")
#define MB() __asm__ volatile("dsb sy" ::: "memory")
#else
/* Host tests: a real fence, plus a hook that lets the simulator check that
 * every store to our index words is ordered before the next doorbell. */
void (*q1n1_glink_barrier_hook)(int orders_stores);
#define HOOK(stores) do { if (q1n1_glink_barrier_hook) q1n1_glink_barrier_hook(stores); } while (0)
#define WMB() do { __atomic_thread_fence(__ATOMIC_SEQ_CST); HOOK(1); } while (0)
#define RMB() do { __atomic_thread_fence(__ATOMIC_SEQ_CST); HOOK(0); } while (0)
#define MB() do { __atomic_thread_fence(__ATOMIC_SEQ_CST); HOOK(1); } while (0)
#endif

static uint32_t get16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }
static uint32_t get32(const uint8_t *p) { return get16(p) | get16(p + 2) << 16; }
static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v); put16(p + 2, v >> 16); }
static uint32_t align8(uint32_t v) { return (v + 7u) & ~7u; }
static void header(uint8_t *b, uint32_t cmd, uint32_t p1, uint32_t p2) { put16(b, cmd); put16(b + 2, p1); put32(b + 4, p2); }

static uint64_t now(struct q1n1_glink *g) { return g->ops->now_us(g->ops->ctx); }
static void relax(struct q1n1_glink *g) { if (g->ops->relax) g->ops->relax(g->ops->ctx); }

static int fail(struct q1n1_glink *g, int error, uint32_t command)
{
    g->s.state = Q1N1_GLINK_FAILED;
    g->s.last_error = error;
    g->s.last_command = command;
    if (error == Q1N1_GLINK_EPROTO) g->s.protocol_errors++;
    return error;
}

void q1n1_glink_cleanup_message(uint8_t out[Q1N1_GLINK_CLEANUP_BYTES])
{
    /* GlinkDxe 0x3358: the 48 bytes at x29-0x50 are zeroed (0x340c-0x3410),
     * then name_len = 5 and name = "apss" are stored at +0x0c/+0x10
     * (0x3418-0x3424); version, command and seq stay 0. */
    for (unsigned i = 0; i < Q1N1_GLINK_CLEANUP_BYTES; i++) out[i] = 0;
    put32(out + 12, 5);
    out[16] = 'a'; out[17] = 'p'; out[18] = 's'; out[19] = 's';
}

/* ---- channels ---- */

static struct q1n1_glink_channel *by_rcid(struct q1n1_glink *g, uint32_t rcid)
{
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++)
        if (g->s.ch[c].remote_opened && g->s.ch[c].rcid == rcid) return &g->s.ch[c];
    return 0;
}

static struct q1n1_glink_channel *by_lcid(struct q1n1_glink *g, uint32_t lcid)
{
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++)
        if (g->s.ch[c].lcid && g->s.ch[c].lcid == lcid) return &g->s.ch[c];
    return 0;
}

static unsigned index_of(struct q1n1_glink *g, const struct q1n1_glink_channel *ch)
{
    return (unsigned)(ch - g->s.ch);
}

/* Where a local intent's bytes land, and how many it can hold here. */
static uint8_t *local_buffer(struct q1n1_glink *g, unsigned c, unsigned slot, uint32_t *capacity)
{
    if (c == Q1N1_GLINK_CH_CLIENT && slot < Q1N1_GLINK_LOCAL_INTENTS) {
        *capacity = Q1N1_GLINK_INTENT_BYTES;
        return g->buffer[slot];
    }
    if (c == Q1N1_GLINK_CH_SSR && slot == 0) {
        *capacity = Q1N1_GLINK_SSR_INTENT_BYTES;
        return g->ssr_buffer;
    }
    *capacity = 0;
    return 0;
}

static int channel_table_ok(const struct q1n1_glink_session *s)
{
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++) {
        const struct q1n1_glink_channel *ch = &s->ch[c];
        uint32_t max_count = c == Q1N1_GLINK_CH_CLIENT ? Q1N1_GLINK_LOCAL_INTENTS : 1;
        uint32_t max_size = c == Q1N1_GLINK_CH_CLIENT ? Q1N1_GLINK_INTENT_BYTES : Q1N1_GLINK_SSR_INTENT_BYTES;
        if (ch->local_count > max_count || ch->local_size > max_size) return 0;
        for (unsigned i = 0; i < Q1N1_GLINK_LOCAL_INTENTS; i++)
            if (ch->local[i].valid && (i >= ch->local_count || ch->local[i].size > max_size)) return 0;
    }
    return 1;
}

static void reset_channel(struct q1n1_glink *g, unsigned c)
{
    struct q1n1_glink_channel *ch = &g->s.ch[c];
    ch->rcid = 0;
    ch->ack_sent = ch->open_sent = ch->open_acked = ch->remote_opened = ch->intents_sent = ch->close_sent = 0;
    ch->intent_req_pending = ch->intent_req_granted = 0;
    for (unsigned i = 0; i < Q1N1_GLINK_LOCAL_INTENTS; i++) ch->local[i].valid = 0, g->filled[c][i] = 0;
    for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++) ch->remote[i].valid = 0;
}

/* ---- FIFOs ---- */

static int index_ok(uint32_t index, uint32_t length) { return index < length && !(index & 7); }

static int releasing;   /* set while q1n1_glink_release waits for the remote's cleanup */

static uint32_t rx_avail(struct q1n1_glink *g, int *bad)
{
    uint32_t head = g->desc[DESC_RX_HEAD], tail = g->s.rx_tail, length = g->s.rx_length;
    if (!index_ok(head, length)) { *bad = 1; return 0; }
    /* During a release, a head back at 0 under our non-zero tail is the
     * remote resetting its words, not new data: parse nothing past it. */
    if (releasing && head == 0 && tail != 0) return 0;
    return head >= tail ? head - tail : length - tail + head;
}

static void rx_peek(struct q1n1_glink *g, uint32_t offset, uint8_t *dst, uint32_t count)
{
    uint32_t length = g->s.rx_length, at = (uint32_t)(((uint64_t)g->s.rx_tail + offset) % length);
    RMB();
    for (uint32_t i = 0; i < count; i++) {
        dst[i] = g->rx[at];
        if (++at == length) at = 0;
    }
}

static void rx_advance(struct q1n1_glink *g, uint32_t count)
{
    uint32_t tail = g->s.rx_tail + count;
    if (tail >= g->s.rx_length) tail -= g->s.rx_length;
    MB();                      /* payload reads complete before the space is released */
    g->desc[DESC_RX_TAIL] = tail;
    g->s.rx_tail = tail;
}

static uint32_t tx_avail(struct q1n1_glink *g, int *bad)
{
    uint32_t head = g->s.tx_head, tail = g->desc[DESC_TX_TAIL], length = g->s.tx_length;
    if (!index_ok(tail, length)) { *bad = 1; return 0; }
    uint32_t avail = tail <= head ? length - head + tail : tail - head;
    return avail < TX_RESERVE ? 0 : avail - TX_RESERVE;
}

static uint32_t tx_copy(struct q1n1_glink *g, uint32_t head, const uint8_t *src, uint32_t count)
{
    for (uint32_t i = 0; i < count; i++) {
        g->tx[head] = src[i];
        if (++head == g->s.tx_length) head = 0;
    }
    return head;
}

/* Every caller has just stored an index word (our head after a frame, our
 * tail after consuming one). That store is to SMEM, the doorbell is a Device
 * write to IPCC: without a barrier between them the SoCCP can take the
 * interrupt, read the old index, and never be rung for the new one. Linux
 * gets the same order from writel()'s __iowmb() in qcom_ipcc_mbox_send_data;
 * the platform write (live_guarded) has its dsb only after the store. */
static int ring(struct q1n1_glink *g)
{
    WMB();
    g->s.doorbells++;
    if (g->ops->doorbell(g->ops->ctx, g->s.doorbell_address, g->s.doorbell_value))
        return fail(g, Q1N1_GLINK_EDOORBELL, 0xffff);
    return Q1N1_GLINK_OK;
}

/* One frame, header then payload, head rounded up to 8, published after a
 * store barrier, then the doorbell (qcom_glink_smem.c:136-209). When the FIFO
 * is full this waits until deadline; with drain set it keeps consuming RX so
 * a remote blocked on its own FIFO cannot deadlock us. */
static int write_frame(struct q1n1_glink *g, const uint8_t *hdr, uint32_t hlen, const void *data, uint32_t dlen,
                       uint64_t deadline, int drain)
{
    if ((uint64_t)hlen + dlen >= g->s.tx_length) return fail(g, Q1N1_GLINK_ENOSPC, get16(hdr));
    for (;;) {
        int bad = 0;
        uint32_t avail = tx_avail(g, &bad);
        if (bad) return fail(g, Q1N1_GLINK_ECORRUPT, get16(hdr));
        if (avail >= hlen + dlen) break;
        if (now(g) > deadline) return Q1N1_GLINK_ETIMEDOUT;
        if (drain) {
            int handled = q1n1_glink_poll(g);
            if (handled < 0) return handled;
        }
        relax(g);
    }
    uint32_t head = tx_copy(g, g->s.tx_head, hdr, hlen);
    if (dlen) head = tx_copy(g, head, data, dlen);
    head = align8(head);
    if (head >= g->s.tx_length) head -= g->s.tx_length;
    WMB();
    g->desc[DESC_TX_HEAD] = head;
    g->s.tx_head = head;
    g->s.tx_frames++;
    return ring(g);
}

/* Control replies from inside the RX handler: bounded, and never recursing
 * into the RX handler. A full FIFO here means the remote stopped reading. */
static int control(struct q1n1_glink *g, uint32_t cmd, uint32_t p1, uint32_t p2)
{
    uint8_t b[8];
    header(b, cmd, p1, p2);
    int status = write_frame(g, b, 8, 0, 0, now(g) + CONTROL_WAIT_US, 0);
    return status == Q1N1_GLINK_ETIMEDOUT ? fail(g, Q1N1_GLINK_ENOSPC, cmd) : status;
}

static int send_version(struct q1n1_glink *g) { return control(g, Q1N1_GLINK_VERSION, 1, g->s.features); }

static int send_open(struct q1n1_glink *g, struct q1n1_glink_channel *ch)
{
    uint8_t b[8 + Q1N1_GLINK_NAME_BYTES] = {0};
    uint32_t n = 0;
    while (n < Q1N1_GLINK_NAME_BYTES - 1 && ch->name[n]) n++;
    header(b, Q1N1_GLINK_OPEN, ch->lcid, n + 1);   /* name length includes the NUL, priority 0 */
    for (uint32_t i = 0; i < n; i++) b[8 + i] = (uint8_t)ch->name[i];
    int status = write_frame(g, b, 8 + align8(n + 1), 0, 0, now(g) + CONTROL_WAIT_US, 0);
    if (status == Q1N1_GLINK_ETIMEDOUT) return fail(g, Q1N1_GLINK_ENOSPC, Q1N1_GLINK_OPEN);
    if (!status) ch->open_sent = 1;
    return status;
}

static int advertise(struct q1n1_glink *g, struct q1n1_glink_channel *ch, unsigned slot)
{
    struct q1n1_glink_intent *intent = &ch->local[slot];
    uint8_t b[16];
    intent->id = g->s.next_liid++;
    if (!g->s.next_liid) g->s.next_liid = 1;
    intent->size = ch->local_size;
    intent->used = 0;
    intent->valid = 1;
    g->filled[index_of(g, ch)][slot] = 0;
    header(b, Q1N1_GLINK_INTENT, ch->lcid, 1);      /* one {size, id} pair, as Linux sends */
    put32(b + 8, intent->size);
    put32(b + 12, intent->id);
    int status = write_frame(g, b, 16, 0, 0, now(g) + CONTROL_WAIT_US, 0);
    return status == Q1N1_GLINK_ETIMEDOUT ? fail(g, Q1N1_GLINK_ENOSPC, Q1N1_GLINK_INTENT) : status;
}

/* Bring a wanted channel forward one step: acknowledge the remote's OPEN,
 * send ours once the version is settled (or shortly after VERSION when the
 * remote keeps quiet about it: an already-negotiated remote may ignore a
 * second VERSION), then offer our intents. A channel whose CLOSE we sent
 * waits for the CLOSE_ACK before it reopens. */
static int drive(struct q1n1_glink *g, unsigned c)
{
    struct q1n1_glink_session *s = &g->s;
    struct q1n1_glink_channel *ch = &s->ch[c];
    int status = Q1N1_GLINK_OK;
    if (!ch->wanted || ch->close_sent || s->state != Q1N1_GLINK_STARTED) return status;
    if (ch->remote_opened && !ch->ack_sent) {
        status = control(g, Q1N1_GLINK_OPEN_ACK, ch->rcid, 0);   /* carries the peer's id */
        if (status) return status;
        ch->ack_sent = 1;
    }
    if (!ch->open_sent && (s->version_acked || s->remote_version || ch->remote_opened ||
                           now(g) - s->started_us > OPEN_FALLBACK_US)) {
        status = send_open(g, ch);
        if (status) return status;
    }
    if (ch->open_acked && ch->remote_opened && !ch->intents_sent) {
        for (unsigned i = 0; i < ch->local_count; i++) {
            status = advertise(g, ch, i);
            if (status) return status;
        }
        ch->intents_sent = 1;
    }
    return status;
}

/* ---- RX handlers ---- */

static int handle_open(struct q1n1_glink *g, uint32_t rcid, uint32_t name_length)
{
    char name[Q1N1_GLINK_NAME_BYTES];
    struct q1n1_glink_channel *match = 0;
    if (name_length <= Q1N1_GLINK_NAME_BYTES) {
        rx_peek(g, 8, (uint8_t *)name, name_length);
        for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS && !match; c++) {
            struct q1n1_glink_channel *ch = &g->s.ch[c];
            uint32_t mine = 0;
            while (mine < Q1N1_GLINK_NAME_BYTES && ch->name[mine]) mine++;
            int same = mine && name_length == mine + 1;
            for (uint32_t i = 0; same && i < name_length; i++) same = name[i] == (i < mine ? ch->name[i] : 0);
            if (same) match = ch;
        }
    }
    if (!match) {
        /* PMIC_LOGS_SOCCP_APPS and anything else: Linux never acknowledges a
         * channel no driver claims, and neither do we. */
        g->s.foreign_opens++;
        return Q1N1_GLINK_OK;
    }
    unsigned c = index_of(g, match);
    if (match->remote_opened) {
        /* The remote opened it again without closing: whatever it offered or
         * half-sent under the old open is gone, whatever the id. */
        for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++) match->remote[i].valid = 0;
        for (unsigned i = 0; i < Q1N1_GLINK_LOCAL_INTENTS; i++) g->filled[c][i] = 0;
        match->ack_sent = 0;
    }
    match->rcid = rcid;
    match->remote_opened = 1;
    /* Linux records the open and acknowledges it only when a client wants
     * the channel (qcom_glink_rx_open / qcom_glink_create_remote). */
    if (!match->wanted) return Q1N1_GLINK_OK;
    int status = control(g, Q1N1_GLINK_OPEN_ACK, rcid, 0);
    if (status) return status;
    match->ack_sent = 1;
    if (!match->open_sent && !match->close_sent) status = send_open(g, match);
    return status;
}

static int handle_close(struct q1n1_glink *g, uint32_t rcid)
{
    struct q1n1_glink_channel *ch = by_rcid(g, rcid);
    if (!ch) return Q1N1_GLINK_OK;
    unsigned c = index_of(g, ch);
    g->s.remote_closes++;
    int status = control(g, Q1N1_GLINK_CLOSE_ACK, rcid, 0);
    if (status) return status;
    int ours = ch->open_sent;
    uint32_t wanted = ch->wanted;
    reset_channel(g, c);
    ch->wanted = wanted;
    /* Close our side too, as Linux does when the remote's close removes the
     * device (qcom_glink_destroy_ept -> close request); reopen only after the
     * remote acknowledges it. */
    if (ours) {
        status = control(g, Q1N1_GLINK_CLOSE, ch->lcid, 0);
        if (!status) ch->close_sent = 1;
    }
    return status;
}

static int handle_intent(struct q1n1_glink *g, uint32_t rcid, uint32_t count)
{
    struct q1n1_glink_channel *ch = by_rcid(g, rcid);
    for (uint32_t n = 0; n < count; n++) {
        uint8_t pair[8];
        rx_peek(g, 8 + 8 * n, pair, 8);
        uint32_t size = get32(pair), id = get32(pair + 4);
        if (!ch) { g->s.dropped++; continue; }
        struct q1n1_glink_intent *slot = 0;
        for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++)
            if (ch->remote[i].valid && ch->remote[i].id == id) slot = &ch->remote[i];
        for (unsigned i = 0; !slot && i < Q1N1_GLINK_REMOTE_INTENTS; i++)
            if (!ch->remote[i].valid) slot = &ch->remote[i];
        if (!slot) { g->s.dropped++; continue; }
        *slot = (struct q1n1_glink_intent){.id = id, .size = size, .valid = 1};
        ch->intent_req_pending = 0;          /* a fresh intent answers any request of ours */
    }
    return Q1N1_GLINK_OK;
}

static int handle_rx_done(struct q1n1_glink *g, uint32_t rcid, uint32_t id, int reuse)
{
    struct q1n1_glink_channel *ch = by_rcid(g, rcid);
    if (!ch) { g->s.dropped++; return Q1N1_GLINK_OK; }
    for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++) {
        struct q1n1_glink_intent *intent = &ch->remote[i];
        if (!intent->valid || intent->id != id) continue;
        intent->used = 0;
        if (!reuse) intent->valid = 0;
        return Q1N1_GLINK_OK;
    }
    g->s.dropped++;
    return Q1N1_GLINK_OK;
}

static int handle_intent_request(struct q1n1_glink *g, uint32_t rcid, uint32_t size)
{
    struct q1n1_glink_channel *ch = by_rcid(g, rcid);
    if (!ch || !ch->open_acked) { g->s.dropped++; return Q1N1_GLINK_OK; }
    /* Linux (handle_intent_req) allocates an intent of the requested size,
     * advertises it, then ACKs. Ours are fixed-size slots: grant when one is
     * free and big enough, decline otherwise -- but always answer, so the
     * remote is never left waiting on us. */
    unsigned slot = ch->local_count;
    for (unsigned i = 0; i < ch->local_count; i++) if (!ch->local[i].valid) { slot = i; break; }
    int granted = size <= ch->local_size && slot < ch->local_count;
    if (granted) {
        int status = advertise(g, ch, slot);
        if (status) return status;
    }
    return control(g, Q1N1_GLINK_RX_INTENT_REQ_ACK, ch->lcid, granted ? 1 : 0);
}

/* A cleanup_done on glink_ssr (downstream glink_private.h:780-784). */
static void deliver_ssr(struct q1n1_glink *g, const uint8_t *data, uint32_t length)
{
    if (length >= 12 && get32(data) == 0 && get32(data + 4) == 1 && get32(data + 8) == 0) g->s.cleanup_done = 1;
    else g->s.dropped++;
}

static int handle_data(struct q1n1_glink *g, uint32_t rcid, uint32_t id, uint32_t cmd)
{
    uint8_t h[16];
    rx_peek(g, 0, h, 16);
    uint32_t chunk = get32(h + 8), left = get32(h + 12);
    struct q1n1_glink_channel *ch = by_rcid(g, rcid);
    if (!ch) { g->s.dropped++; return Q1N1_GLINK_OK; }
    unsigned c = index_of(g, ch), slot = ch->local_count;
    for (unsigned i = 0; i < ch->local_count; i++)
        if (ch->local[i].valid && ch->local[i].id == id) slot = i;
    if (slot == ch->local_count) { g->s.dropped++; return Q1N1_GLINK_OK; }   /* "no intent found" */
    struct q1n1_glink_intent *intent = &ch->local[slot];
    uint32_t capacity;
    uint8_t *buffer = local_buffer(g, c, slot, &capacity);
    if (cmd == Q1N1_GLINK_TX_DATA) g->filled[c][slot] = 0;     /* a new message starts */
    uint64_t total = (uint64_t)g->filled[c][slot] + chunk + left;
    if (!buffer || total > intent->size || total > capacity) {
        /* The header lied about its size (or a copied session claims more
         * than this build's buffer): drop the payload (Linux does). */
        g->filled[c][slot] = 0;
        g->s.dropped++;
        return Q1N1_GLINK_OK;
    }
    rx_peek(g, 16, buffer + g->filled[c][slot], chunk);
    g->filled[c][slot] += chunk;
    if (left) return Q1N1_GLINK_OK;

    uint32_t length = g->filled[c][slot];
    g->filled[c][slot] = 0;
    intent->used = 1;
    g->s.messages++;
    if (c == Q1N1_GLINK_CH_SSR) deliver_ssr(g, buffer, length);
    else if (g->ops->deliver) g->ops->deliver(g->ops->deliver_ctx, buffer, length);
    intent->used = 0;
    if (g->s.features & Q1N1_GLINK_FEATURE_INTENT_REUSE)
        return control(g, Q1N1_GLINK_RX_DONE_W_REUSE, ch->lcid, intent->id);
    int status = control(g, Q1N1_GLINK_RX_DONE, ch->lcid, intent->id);
    intent->valid = 0;
    return status ? status : advertise(g, ch, slot);           /* replace the consumed intent */
}

/* Bytes the command at the RX tail occupies (qcom_glink_native.c:1172-1241).
 * 0 = incomplete, wait; <0 = cannot be a valid frame. */
static int64_t frame_length(struct q1n1_glink *g, const uint8_t *h, uint32_t avail)
{
    uint32_t cmd = get16(h), p2 = get32(h + 4);
    uint64_t length;
    switch (cmd) {
    case Q1N1_GLINK_VERSION: case Q1N1_GLINK_VERSION_ACK: case Q1N1_GLINK_CLOSE: case Q1N1_GLINK_CLOSE_ACK:
    case Q1N1_GLINK_RX_INTENT_REQ: case Q1N1_GLINK_OPEN_ACK: case Q1N1_GLINK_READ_NOTIF:
    case Q1N1_GLINK_RX_DONE: case Q1N1_GLINK_RX_DONE_W_REUSE: case Q1N1_GLINK_RX_INTENT_REQ_ACK:
    case Q1N1_GLINK_SIGNALS:
        length = 8;
        break;
    case Q1N1_GLINK_OPEN:
        if (!(p2 & 0xffff)) return -1;
        length = 8 + (uint64_t)align8(p2 & 0xffff);
        break;
    case Q1N1_GLINK_INTENT:
        length = 8 + 8 * (uint64_t)p2;
        break;
    case Q1N1_GLINK_TX_DATA: case Q1N1_GLINK_TX_DATA_CONT:
        if (avail < 16) return 0;
        length = (16 + (uint64_t)get32(h + 8) + 7) & ~7ull;
        break;
    default:
        return -1;
    }
    /* A frame as long as the FIFO can never be complete (qcom_glink_native.c:350). */
    if (length >= g->s.rx_length) return -1;
    return avail < length ? 0 : (int64_t)length;
}

static int dispatch(struct q1n1_glink *g, const uint8_t *h)
{
    uint32_t cmd = get16(h), p1 = get16(h + 2), p2 = get32(h + 4);
    struct q1n1_glink_channel *ch;
    switch (cmd) {
    case Q1N1_GLINK_VERSION:
        if (!p1) return Q1N1_GLINK_OK;             /* version 0: ignored, as Linux does */
        g->s.features &= p2;
        g->s.remote_version = 1;
        return control(g, Q1N1_GLINK_VERSION_ACK, 1, g->s.features);
    case Q1N1_GLINK_VERSION_ACK:
        /* Linux ignores version 0 (qcom_glink_receive_version_ack, case 0).
         * A downstream-style core answers each 8-byte zero frame -- VERSION 0,
         * which a Linux-style restart makes it read -- with exactly that. */
        if (!p1) { g->s.version_zero_acks++; return Q1N1_GLINK_OK; }
        if (p2 == g->s.features) { g->s.version_acked = 1; return Q1N1_GLINK_OK; }
        if (++g->s.version_resends > VERSION_RESENDS) return fail(g, Q1N1_GLINK_EPROTO, cmd);
        g->s.features &= p2;
        return send_version(g);
    case Q1N1_GLINK_OPEN:
        return handle_open(g, p1, p2 & 0xffff);
    case Q1N1_GLINK_OPEN_ACK:
        ch = by_lcid(g, p1);
        if (ch && ch->open_sent) ch->open_acked = 1;
        else g->s.dropped++;
        return Q1N1_GLINK_OK;
    case Q1N1_GLINK_CLOSE:
        return handle_close(g, p1);
    case Q1N1_GLINK_CLOSE_ACK:
        ch = by_lcid(g, p1);
        if (ch && ch->close_sent) {
            ch->close_sent = 0;
            ch->open_sent = ch->open_acked = ch->intents_sent = 0;
        }
        return Q1N1_GLINK_OK;
    case Q1N1_GLINK_SIGNALS:
        return Q1N1_GLINK_OK;
    case Q1N1_GLINK_INTENT:
        return handle_intent(g, p1, p2);
    case Q1N1_GLINK_RX_DONE:
    case Q1N1_GLINK_RX_DONE_W_REUSE:
        return handle_rx_done(g, p1, p2, cmd == Q1N1_GLINK_RX_DONE_W_REUSE);
    case Q1N1_GLINK_RX_INTENT_REQ:
        return handle_intent_request(g, p1, p2);
    case Q1N1_GLINK_RX_INTENT_REQ_ACK:
        ch = by_rcid(g, p1);
        if (!ch) return Q1N1_GLINK_OK;
        ch->intent_req_pending = 0;
        ch->intent_req_granted = p2 ? 1 : 0;
        return Q1N1_GLINK_OK;
    case Q1N1_GLINK_TX_DATA:
    case Q1N1_GLINK_TX_DATA_CONT:
        return handle_data(g, p1, p2, cmd);
    case Q1N1_GLINK_READ_NOTIF:
        return Q1N1_GLINK_OK;                      /* the doorbell follows the advance */
    }
    return fail(g, Q1N1_GLINK_EPROTO, cmd);
}

/* ---- public ---- */

int q1n1_glink_view(const struct q1n1_smem *smem, const struct q1n1_smem_partition *part,
                    struct q1n1_glink_view *v)
{
    if (!smem || !part || !v) return Q1N1_GLINK_EINVAL;
    struct q1n1_smem_item desc, tx, rx;
    int status = q1n1_smem_get(smem, part, Q1N1_GLINK_ITEM_DESCRIPTOR, &desc);
    if (!status) status = q1n1_smem_get(smem, part, Q1N1_GLINK_ITEM_TX_FIFO, &tx);
    if (!status) status = q1n1_smem_get(smem, part, Q1N1_GLINK_ITEM_RX_FIFO, &rx);
    if (status == Q1N1_SMEM_ENOENT) return Q1N1_GLINK_ENOENT;
    if (status) return Q1N1_GLINK_ECORRUPT;
    /* Linux refuses any descriptor that is not exactly 32 bytes. */
    if (desc.size != 32 || (desc.offset & 3)) return Q1N1_GLINK_ECORRUPT;
    if (tx.size < 64 || (tx.size & 7) || tx.size > (1u << 20) || rx.size < 64 || (rx.size & 7) ||
        rx.size > (1u << 20))
        return Q1N1_GLINK_ECORRUPT;
    v->desc = (volatile uint32_t *)(uintptr_t)(smem->base + desc.offset);
    v->desc_offset = desc.offset;
    v->tx_offset = tx.offset;
    v->tx_length = tx.size;
    v->rx_offset = rx.offset;
    v->rx_length = rx.size;
    v->remote_host = part->host0 == Q1N1_SMEM_HOST_APPS ? part->host1 : part->host0;
    return Q1N1_GLINK_OK;
}

/* Two readings of the four words delay_us apart; loads only. */
static int sample_words(volatile uint32_t *desc, uint32_t tx_length, uint32_t rx_length,
                        const struct q1n1_glink_ops *ops, uint32_t words[4], uint64_t delay_us)
{
    uint32_t first[4];
    for (unsigned i = 0; i < 4; i++) first[i] = desc[i];
    uint64_t until = ops->now_us(ops->ctx) + delay_us;
    while (ops->now_us(ops->ctx) < until) if (ops->relax) ops->relax(ops->ctx);
    for (unsigned i = 0; i < 4; i++) words[i] = desc[i];
    if (!index_ok(words[0], tx_length) || !index_ok(words[1], tx_length) ||
        !index_ok(words[2], rx_length) || !index_ok(words[3], rx_length))
        return Q1N1_GLINK_ECORRUPT;
    for (unsigned i = 0; i < 4; i++) if (first[i] != words[i]) return Q1N1_GLINK_EBUSY;
    return Q1N1_GLINK_OK;
}

int q1n1_glink_view_sample(const struct q1n1_glink_view *v, const struct q1n1_glink_ops *ops, uint32_t words[4],
                           uint64_t delay_us)
{
    if (!v || !v->desc || !ops || !ops->now_us) return Q1N1_GLINK_EINVAL;
    return sample_words(v->desc, v->tx_length, v->rx_length, ops, words, delay_us);
}

int q1n1_glink_bind(struct q1n1_glink *g, const struct q1n1_smem *smem, const struct q1n1_smem_partition *part,
                    const struct q1n1_glink_ops *ops, uint64_t doorbell_address, uint32_t doorbell_value)
{
    if (!g || !smem || !part || !ops || !ops->now_us || !ops->doorbell) return Q1N1_GLINK_EINVAL;
    g->ops = 0;
    g->desc = 0;
    struct q1n1_glink_view v;
    int status = q1n1_glink_view(smem, part, &v);
    if (status) return status;

    struct q1n1_glink_session *s = &g->s;
    int same = s->magic == Q1N1_GLINK_SESSION_MAGIC && s->version == Q1N1_GLINK_SESSION_VERSION &&
               s->size == sizeof(*s) &&
               s->smem_phys == smem->phys && s->smem_size == smem->size && s->remote_host == v.remote_host &&
               s->desc_offset == v.desc_offset && s->tx_offset == v.tx_offset && s->tx_length == v.tx_length &&
               s->rx_offset == v.rx_offset && s->rx_length == v.rx_length &&
               s->doorbell_address == doorbell_address && s->doorbell_value == doorbell_value &&
               channel_table_ok(s);
    if (!same) {
        *s = (struct q1n1_glink_session){
            .magic = Q1N1_GLINK_SESSION_MAGIC, .version = Q1N1_GLINK_SESSION_VERSION, .size = sizeof(*s),
            .smem_phys = smem->phys, .smem_size = smem->size, .remote_host = v.remote_host,
            .desc_offset = v.desc_offset, .tx_offset = v.tx_offset, .tx_length = v.tx_length,
            .rx_offset = v.rx_offset, .rx_length = v.rx_length,
            .doorbell_address = doorbell_address, .doorbell_value = doorbell_value,
            .state = Q1N1_GLINK_DOWN,
        };
    }
    g->ops = ops;
    g->desc = v.desc;
    g->tx = (volatile uint8_t *)(uintptr_t)(smem->base + v.tx_offset);
    g->rx = (volatile uint8_t *)(uintptr_t)(smem->base + v.rx_offset);
    g->servicing = 0;
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++)
        for (unsigned i = 0; i < Q1N1_GLINK_LOCAL_INTENTS; i++) g->filled[c][i] = 0;
    return Q1N1_GLINK_OK;
}

int q1n1_glink_sample(struct q1n1_glink *g, uint32_t words[4], uint64_t delay_us)
{
    if (!g || !g->desc || !g->ops) return Q1N1_GLINK_EINVAL;
    return sample_words(g->desc, g->s.tx_length, g->s.rx_length, g->ops, words, delay_us);
}

static void define_channel(struct q1n1_glink_channel *ch, const char *name, uint32_t n, uint32_t lcid,
                           uint32_t wanted, uint32_t count, uint32_t size)
{
    *ch = (struct q1n1_glink_channel){.lcid = lcid, .wanted = wanted, .local_count = count, .local_size = size};
    for (uint32_t i = 0; i < Q1N1_GLINK_NAME_BYTES; i++) ch->name[i] = i < n ? name[i] : 0;
}

int q1n1_glink_restart(struct q1n1_glink *g, const char *channel, uint32_t lcid, enum q1n1_glink_restart_mode mode)
{
    if (!g || !g->desc || !channel || !lcid || lcid > 0xffff || lcid == Q1N1_GLINK_SSR_LCID ||
        mode > Q1N1_GLINK_RESTART_RESYNC)
        return Q1N1_GLINK_EINVAL;
    uint32_t n = 0;
    while (channel[n]) if (++n >= Q1N1_GLINK_NAME_BYTES) return Q1N1_GLINK_EINVAL;
    if (!n) return Q1N1_GLINK_EINVAL;
    uint32_t words[4];
    int status = q1n1_glink_sample(g, words, 2000);
    if (status) return status;          /* nothing written */

    struct q1n1_glink_session *s = &g->s;
    for (unsigned i = 0; i < 4; i++) s->start_words[i] = words[i];
    s->restarts++;
    s->state = Q1N1_GLINK_STARTED;
    s->last_error = 0;
    s->last_command = 0;
    s->features = Q1N1_GLINK_FEATURE_INTENT_REUSE;
    s->version_acked = s->remote_version = s->version_resends = 0;
    s->cleanup_sent = s->cleanup_done = s->release_words_zero = 0;
    s->next_liid = 1;
    s->mode = mode;
    define_channel(&s->ch[Q1N1_GLINK_CH_CLIENT], channel, n, lcid, 1, Q1N1_GLINK_LOCAL_INTENTS,
                   Q1N1_GLINK_INTENT_BYTES);
    define_channel(&s->ch[Q1N1_GLINK_CH_SSR], Q1N1_GLINK_SSR_NAME, sizeof(Q1N1_GLINK_SSR_NAME) - 1,
                   Q1N1_GLINK_SSR_LCID, 0, 1, Q1N1_GLINK_SSR_INTENT_BYTES);
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++)
        for (unsigned i = 0; i < Q1N1_GLINK_LOCAL_INTENTS; i++) g->filled[c][i] = 0;

    s->rx_tail = mode == Q1N1_GLINK_RESTART_LINUX ? 0 : words[DESC_RX_HEAD];
    s->tx_head = mode == Q1N1_GLINK_RESTART_RESYNC ? words[DESC_TX_TAIL] : 0;
    g->desc[DESC_RX_TAIL] = s->rx_tail;
    g->desc[DESC_TX_HEAD] = s->tx_head;
    MB();
    s->started_us = now(g);
    return send_version(g);
}

int q1n1_glink_check(struct q1n1_glink *g)
{
    if (!g || !g->desc) return Q1N1_GLINK_EINVAL;
    if (g->s.state == Q1N1_GLINK_FAILED) return Q1N1_GLINK_EFAILED;
    if (g->s.state != Q1N1_GLINK_STARTED) return Q1N1_GLINK_ESTATE;
    uint32_t head = g->desc[DESC_TX_HEAD], tail = g->desc[DESC_RX_TAIL];
    if (head != g->s.tx_head || tail != g->s.rx_tail) return Q1N1_GLINK_ESTATE;
    if (!index_ok(g->desc[DESC_TX_TAIL], g->s.tx_length) || !index_ok(g->desc[DESC_RX_HEAD], g->s.rx_length) ||
        !index_ok(head, g->s.tx_length) || !index_ok(tail, g->s.rx_length))
        return Q1N1_GLINK_ECORRUPT;
    return Q1N1_GLINK_OK;
}

int q1n1_glink_poll(struct q1n1_glink *g)
{
    if (!g || !g->desc) return Q1N1_GLINK_EINVAL;
    if (g->s.state == Q1N1_GLINK_FAILED) return Q1N1_GLINK_EFAILED;
    if (g->s.state != Q1N1_GLINK_STARTED) return Q1N1_GLINK_ESTATE;
    if (g->desc[DESC_RX_TAIL] != g->s.rx_tail) return fail(g, Q1N1_GLINK_ESTATE, 0xffff);
    int limit = g->servicing > 0 ? g->servicing : FRAMES_PER_POLL;
    int handled = 0, kick = 0;
    while (handled < limit) {
        int bad = 0;
        uint32_t avail = rx_avail(g, &bad);
        if (bad) return fail(g, Q1N1_GLINK_ECORRUPT, 0xffff);
        if (avail < 8) break;
        if (g->servicing) {
            uint32_t room = tx_avail(g, &bad);
            if (bad) return fail(g, Q1N1_GLINK_ECORRUPT, 0xffff);
            if (room < SERVICE_TX_ROOM) break;     /* the remote is behind on our FIFO: later */
        }
        uint8_t h[16] = {0};
        rx_peek(g, 0, h, avail < 16 ? 8 : 16);
        int64_t frame = frame_length(g, h, avail);
        if (frame < 0) return fail(g, Q1N1_GLINK_EPROTO, get16(h));
        if (!frame) break;
        int status = dispatch(g, h);
        if (status) return status;
        if (g->s.state == Q1N1_GLINK_FAILED) return g->s.last_error;
        kick |= get16(h) == Q1N1_GLINK_READ_NOTIF;
        rx_advance(g, (uint32_t)frame);
        g->s.rx_frames++;
        handled++;
    }
    if (kick) {
        int status = ring(g);
        if (status) return status;
    }
    return handled;
}

int q1n1_glink_service(struct q1n1_glink *g, unsigned max_frames)
{
    if (!g || !g->desc || !g->ops) return Q1N1_GLINK_EINVAL;
    if (g->s.state != Q1N1_GLINK_STARTED) return 0;
    g->servicing = max_frames ? (int)(max_frames < FRAMES_PER_POLL ? max_frames : FRAMES_PER_POLL) : 1;
    int handled = q1n1_glink_poll(g);
    g->servicing = 0;
    g->s.service_polls++;
    if (handled > 0) g->s.service_frames += (uint64_t)handled;
    return handled;
}

int q1n1_glink_channel_ready(const struct q1n1_glink *g, unsigned channel)
{
    if (!g || channel >= Q1N1_GLINK_CHANNELS || g->s.state != Q1N1_GLINK_STARTED) return 0;
    const struct q1n1_glink_channel *ch = &g->s.ch[channel];
    return ch->open_acked && ch->remote_opened && ch->intents_sent && !ch->close_sent;
}

static int wait_channel(struct q1n1_glink *g, unsigned c, uint64_t deadline_us)
{
    for (;;) {
        int status = q1n1_glink_poll(g);
        if (status < 0) return status;
        status = drive(g, c);
        if (status) return status;
        if (q1n1_glink_channel_ready(g, c)) return Q1N1_GLINK_OK;
        if (now(g) > deadline_us) return Q1N1_GLINK_ETIMEDOUT;
        relax(g);
    }
}

int q1n1_glink_wait_ready(struct q1n1_glink *g, uint64_t deadline_us)
{
    if (!g || !g->desc) return Q1N1_GLINK_EINVAL;
    return wait_channel(g, Q1N1_GLINK_CH_CLIENT, deadline_us);
}

int q1n1_glink_send(struct q1n1_glink *g, unsigned channel, const void *data, uint32_t length, uint64_t deadline_us)
{
    if (!g || !g->desc || !data || !length || channel >= Q1N1_GLINK_CHANNELS) return Q1N1_GLINK_EINVAL;
    if (!q1n1_glink_channel_ready(g, channel))
        return g->s.state == Q1N1_GLINK_FAILED ? Q1N1_GLINK_EFAILED : Q1N1_GLINK_ECLOSED;
    if ((uint64_t)length + 16 >= g->s.tx_length) return Q1N1_GLINK_ENOSPC;
    struct q1n1_glink_channel *ch = &g->s.ch[channel];
    struct q1n1_glink_intent *intent = 0;
    for (;;) {
        int status = q1n1_glink_poll(g);
        if (status < 0) return status;
        if (!q1n1_glink_channel_ready(g, channel)) return Q1N1_GLINK_ECLOSED;
        for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++) {
            struct q1n1_glink_intent *r = &ch->remote[i];
            if (r->valid && !r->used && r->size >= length && (!intent || r->size < intent->size)) intent = r;
        }
        if (intent) break;
        /* No intent fits: ask for one (qcom_glink_request_intent). */
        if (!ch->intent_req_pending) {
            ch->intent_req_pending = 1;
            status = control(g, Q1N1_GLINK_RX_INTENT_REQ, ch->lcid, length);
            if (status) return status;
        }
        if (now(g) > deadline_us) {
            ch->intent_req_pending = 0;      /* ask again next time */
            return Q1N1_GLINK_ETIMEDOUT;
        }
        relax(g);
    }
    uint8_t h[16];
    header(h, Q1N1_GLINK_TX_DATA, ch->lcid, intent->id);
    put32(h + 8, length);
    put32(h + 12, 0);
    intent->used = 1;
    int status = write_frame(g, h, 16, data, length, deadline_us, 1);
    if (status) intent->used = 0;
    return status;
}

int q1n1_glink_scrub(struct q1n1_glink *g)
{
    if (!g || !g->desc) return Q1N1_GLINK_EINVAL;
    uint32_t head = g->s.tx_head, tail = g->desc[DESC_TX_TAIL], length = g->s.tx_length;
    if (!index_ok(tail, length) || !index_ok(head, length) || g->desc[DESC_TX_HEAD] != head)
        return Q1N1_GLINK_ESTATE;
    /* Free space runs from our head up to the remote's tail; all of it when
     * they are equal. Both are 8-aligned, so whole words suffice. */
    uint32_t free = tail > head ? tail - head : length - head + tail;
    volatile uint32_t *fifo = (volatile uint32_t *)(uintptr_t)g->tx;
    for (uint32_t done = 0, at = head; done < free; done += 4) {
        fifo[at / 4] = 0;
        at += 4;
        if (at == length) at = 0;
    }
    WMB();
    g->s.scrubbed++;
    return Q1N1_GLINK_OK;
}

int q1n1_glink_release(struct q1n1_glink *g, uint64_t deadline_us)
{
    int status = q1n1_glink_check(g);
    if (status) return status;
    struct q1n1_glink_session *s = &g->s;
    s->cleanup_sent = s->cleanup_done = s->release_words_zero = 0;
    s->ch[Q1N1_GLINK_CH_SSR].wanted = 1;
    /* 1. glink_ssr, open both ways with our intent for the reply; a timeout
     *    here has sent nothing but OPEN/OPEN_ACK, and the session stays up. */
    status = wait_channel(g, Q1N1_GLINK_CH_SSR, deadline_us);
    if (status) return status;
    /* 2. The firmware's message. Its effect is seen as the remote's tail
     *    going to 0, so that tail must not already be 0 when we send: if it
     *    is, let the remote catch up to our head first, and if our head is 0
     *    too, move it with one 8-byte VERSION 0 frame (ignored by Linux and
     *    by a downstream core on a negotiated link). */
    if (g->desc[DESC_TX_TAIL] == 0) {
        if (s->tx_head == 0) {
            status = control(g, Q1N1_GLINK_VERSION, 0, 0);
            if (status) return status;
        }
        while (g->desc[DESC_TX_TAIL] != s->tx_head) {
            status = q1n1_glink_poll(g);
            if (status < 0) return status;
            if (now(g) > deadline_us) return Q1N1_GLINK_ETIMEDOUT;
            relax(g);
        }
    }
    uint8_t message[Q1N1_GLINK_CLEANUP_BYTES];
    q1n1_glink_cleanup_message(message);
    uint32_t tail_at_send = g->desc[DESC_TX_TAIL];
    status = q1n1_glink_send(g, Q1N1_GLINK_CH_SSR, message, sizeof(message), deadline_us);
    if (status) return status;
    s->cleanup_sent = 1;
    /* 3. Wait for the SoCCP's half: words 0 and 3 to 0 (downstream ssr()). The
     *    zero tail counts only after the tail has moved since we sent (the
     *    remote read something) or the remote answered cleanup_done, so an
     *    idle FIFO at offset 0 is not mistaken for a finished cleanup.
     *    It also counts only while our head is not 0: a remote that simply
     *    read everything up to a head of 0 shows the same tail of 0 as one
     *    that reset (and cleanup_done does not settle it -- the reset may
     *    come later). When the head is 0 there, one VERSION 0 frame moves it
     *    to 8: a live remote reads it (tail 8, keep waiting), a remote that
     *    has reset is waiting for all four words to be 0 and reads nothing. */
    int consumed = 0, padded = 0;
    releasing = 1;
    for (;;) {
        uint32_t w0 = g->desc[DESC_TX_TAIL], w3 = g->desc[DESC_RX_HEAD];
        consumed |= w0 != tail_at_send || s->cleanup_done;
        if (w0) padded = 0;                  /* the remote read past it: it is alive */
        if (consumed && w0 == 0 && w3 == 0 && s->tx_head == 0) {
            status = control(g, Q1N1_GLINK_VERSION, 0, 0);
            if (status) { releasing = 0; return status; }
            s->release_pads++;
            padded = 1;
        } else if (consumed && w0 == 0 && w3 == 0) {
            /* Right after a pad, give a live remote time to read it. */
            uint64_t until = now(g) + (padded ? RELEASE_PAD_CONFIRM_US : RELEASE_CONFIRM_US);
            while (now(g) < until) relax(g);
            if (g->desc[DESC_TX_TAIL] == 0 && g->desc[DESC_RX_HEAD] == 0) break;
        } else {
            status = q1n1_glink_poll(g);
            if (status < 0) { releasing = 0; return status; }
        }
        if (now(g) > deadline_us) { releasing = 0; return fail(g, Q1N1_GLINK_ETIMEDOUT, 0xfffe); }
        relax(g);
    }
    releasing = 0;
    /* 4. Our half, as downstream ssr() does locally: nothing of ours left to
     *    replay, then our two words to 0. */
    volatile uint32_t *fifo = (volatile uint32_t *)(uintptr_t)g->tx;
    for (uint32_t at = 0; at < s->tx_length; at += 4) fifo[at / 4] = 0;
    WMB();
    g->desc[DESC_TX_HEAD] = 0;
    g->desc[DESC_RX_TAIL] = 0;
    MB();
    s->tx_head = s->rx_tail = 0;
    for (unsigned c = 0; c < Q1N1_GLINK_CHANNELS; c++) {
        uint32_t wanted = s->ch[c].wanted;
        reset_channel(g, c);
        s->ch[c].wanted = wanted;
    }
    s->ch[Q1N1_GLINK_CH_SSR].wanted = 0;
    s->release_words_zero = 1;
    s->releases++;
    s->state = Q1N1_GLINK_RELEASED;
    return Q1N1_GLINK_OK;
}
