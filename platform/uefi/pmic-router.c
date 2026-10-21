/* SPDX-License-Identifier: MIT */
/* See pmic-router.h. */
#include "pmic-router.h"

#define ORPHAN_GIVE_UP_US 2000000u   /* a reply this late is not coming */

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static void put32(uint8_t *p, uint32_t v)
{
    for (unsigned i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8 * i));
}
static uint64_t now(struct q1n1_pmic_router *r) { return r->glink->ops->now_us(r->glink->ops->ctx); }
static void relax(struct q1n1_pmic_router *r)
{
    if (r->glink->ops->relax) r->glink->ops->relax(r->glink->ops->ctx);
}

void q1n1_pmic_router_deliver(void *ctx, const uint8_t *data, uint32_t length)
{
    struct q1n1_pmic_router *r = ctx;
    if (length < 12 || get32(data) != Q1N1_PMIC_OWNER) {
        /* Battery-manager or UCSI notifications (owners 0x800a/0x800b) share
         * the channel; nothing here asked for them. */
        r->foreign++;
        return;
    }
    if (r->orphans) {
        /* Replies come back in request order, so the oldest timed-out
         * request is the one this answers. */
        r->orphans--;
        r->stale_discarded++;
        return;
    }
    if (!r->waiting || r->have_reply) {
        r->unsolicited++;
        return;
    }
    uint32_t n = length < Q1N1_PMIC_REPLY_BYTES ? length : Q1N1_PMIC_REPLY_BYTES;
    for (uint32_t i = 0; i < n; i++) r->reply[i] = data[i];
    r->reply_length = length;
    r->have_reply = 1;
    r->replies++;
    r->last_owner = get32(data);
    r->last_type = get32(data + 4);
    r->last_opcode = get32(data + 8);
    r->last_length = length;
    r->last_error = length >= Q1N1_PMIC_REPLY_BYTES ? get32(data + Q1N1_PMIC_REPLY_BYTES - 4) : 0xffffffffu;
}

int q1n1_pmic_router_sample(struct q1n1_pmic_router *r, uint32_t reg, uint8_t *data, uint32_t size,
                            uint64_t deadline_us)
{
    if (!r || !r->glink || !data || !size || size > Q1N1_PMIC_DATA_BYTES) return Q1N1_PMIC_EDENIED;
    int status = q1n1_glink_poll(r->glink);
    if (status < 0) return status;
    /* Let owed replies arrive before asking again, but not forever. */
    while (r->orphans) {
        if (now(r) - r->orphan_since_us > ORPHAN_GIVE_UP_US) { r->orphans = 0; break; }
        if (now(r) > deadline_us) return Q1N1_PMIC_EORPHAN;
        relax(r);
        status = q1n1_glink_poll(r->glink);
        if (status < 0) return status;
    }

    uint8_t request[Q1N1_PMIC_REQUEST_BYTES];
    put32(request, Q1N1_PMIC_OWNER);
    put32(request + 4, Q1N1_PMIC_TYPE_REQUEST);
    put32(request + 8, Q1N1_PMIC_OPCODE_READ);
    put32(request + 12, 0);
    put32(request + 16, reg);
    put32(request + 20, size);
    r->have_reply = 0;
    r->reply_length = 0;
    /* Not waiting until the request is in the FIFO: q1n1_glink_send polls
     * while it looks for a remote intent, and any 0x8011 reply that arrives
     * then answers something older (counted unsolicited), never this. */
    r->waiting = 0;
    status = q1n1_glink_send(r->glink, Q1N1_GLINK_CH_CLIENT, request, sizeof(request), deadline_us);
    if (status) return status;
    r->waiting = 1;
    r->requests++;
    while (!r->have_reply) {
        status = q1n1_glink_poll(r->glink);
        if (status < 0) { r->waiting = 0; return status; }
        if (r->have_reply) break;
        if (now(r) > deadline_us) {
            r->waiting = 0;
            r->orphans++;
            r->orphan_since_us = now(r);
            return Q1N1_PMIC_ETIMEDOUT;
        }
        relax(r);
    }
    r->waiting = 0;
    if (r->reply_length != Q1N1_PMIC_REPLY_BYTES || get32(r->reply + 4) != Q1N1_PMIC_TYPE_REQUEST ||
        get32(r->reply + 8) != Q1N1_PMIC_OPCODE_READ) {
        r->bad_replies++;
        return Q1N1_PMIC_EREPLY;
    }
    if (get32(r->reply + Q1N1_PMIC_REPLY_BYTES - 4)) return Q1N1_PMIC_EREMOTE;
    for (uint32_t i = 0; i < size; i++) data[i] = r->reply[12 + i];
    return Q1N1_PMIC_OK;
}

static int allowed(uint32_t reg, uint32_t size)
{
    return (reg == Q1N1_PMGK_BST && size == Q1N1_PMGK_BST_BYTES) ||
           (reg == Q1N1_PMGK_BIX && size == Q1N1_PMGK_BIX_BYTES) ||
           (reg == Q1N1_PMGK_PSR && size == Q1N1_PMGK_PSR_BYTES) ||
           (reg == Q1N1_PMIC_UCSI_VERSION && size == 4);
}

int q1n1_pmic_router_read(struct q1n1_pmic_router *r, uint32_t reg, uint8_t *data, uint32_t size,
                          uint64_t deadline_us)
{
    if (!r || !data || !allowed(reg, size)) return Q1N1_PMIC_EDENIED;
    uint8_t first[Q1N1_PMGK_BIX_BYTES], second[Q1N1_PMGK_BIX_BYTES];
    int status = q1n1_pmic_router_sample(r, reg, first, size, deadline_us);
    if (!status) status = q1n1_pmic_router_sample(r, reg, second, size, deadline_us);
    if (status) return status;
    /* The same rules as the boot-window reader (ucsi-console.c): both samples
     * agree, except that _BST's rate and voltage may move between them; and a
     * reply of 8+ bytes that repeats the previous reply's prefix is refused. */
    unsigned same = 1, stale = size >= 8 && r->previous_length >= size;
    for (uint32_t i = 0; i < size; i++) {
        same &= first[i] == second[i];
        if (i < r->previous_length) stale &= first[i] == r->previous[i];
    }
    if (!same && reg == Q1N1_PMGK_BST) {
        same = 1;
        for (unsigned i = 0; i < 4; i++) same &= first[i] == second[i] && first[8 + i] == second[8 + i];
    }
    if (!same) return Q1N1_PMIC_EUNSTABLE;
    if (stale) return Q1N1_PMIC_ESTALE;
    for (uint32_t i = 0; i < size; i++) data[i] = r->previous[i] = first[i];
    r->previous_length = size;
    return Q1N1_PMIC_OK;
}

int q1n1_pmic_router_battery(struct q1n1_pmic_router *r, struct q1n1_battery *out, uint64_t deadline_us)
{
    uint8_t bst[Q1N1_PMGK_BST_BYTES], bix[Q1N1_PMGK_BIX_BYTES], psr[Q1N1_PMGK_PSR_BYTES];
    int status = q1n1_pmic_router_read(r, Q1N1_PMGK_BST, bst, sizeof(bst), deadline_us);
    if (!status) status = q1n1_pmic_router_read(r, Q1N1_PMGK_BIX, bix, sizeof(bix), deadline_us);
    if (!status) status = q1n1_pmic_router_read(r, Q1N1_PMGK_PSR, psr, sizeof(psr), deadline_us);
    if (status) return status;
    return q1n1_battery_decode(bst, bix, psr, out) ? Q1N1_PMIC_OK : Q1N1_PMIC_EIMPLAUSIBLE;
}
