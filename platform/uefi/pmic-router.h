/* SPDX-License-Identifier: MIT */
/* The A16 firmware's PMIC GLINK register path, spoken over q1n1's own GLINK
 * channel (PMIC_RTR_SOCCP_APPS) instead of through PmicGlinkDxe.
 *
 * Request (PmicGlinkDxe read callback RVA 0x263c, header builder 0x381c; the
 * same frame qcpmicglink8480.sys builds at 0x98f0), six little-endian u32:
 *   owner 0x8011, type 1, opcode 0x90, 0, register id, length (1..1024)
 * Reply: exactly 1040 bytes = 12-byte header + 1024 data bytes + u32 remote
 * error (PmicGlinkDxe caches the data at RVA 0x4550, the error at 0x4950).
 * The header is taken to echo owner/type/opcode; anything else is refused
 * and recorded, so the first hardware run shows what it really carries.
 *
 * Owner 0x8011 appears in no public source; it is what this firmware and the
 * Windows driver use for the registers the DSDT's battery reads (battery.h).
 *
 * Replies carry no register id, so a reply that arrives after its request
 * timed out would be taken for the next one. Timed-out requests are counted
 * as orphans and the next matching replies are discarded against them (the
 * count crosses a chainload in the GLINK session's client_carry); a reply
 * is taken only once its request is in the FIFO; on top of that every value
 * is read twice and must agree, and battery.c rejects implausible
 * combinations. */
#pragma once
#include <stdint.h>
#include "glink-smem.h"
#include "battery.h"

#define Q1N1_PMIC_OWNER 0x8011u
#define Q1N1_PMIC_TYPE_REQUEST 1u
#define Q1N1_PMIC_OPCODE_READ 0x90u
#define Q1N1_PMIC_REQUEST_BYTES 24u
#define Q1N1_PMIC_REPLY_BYTES 1040u
#define Q1N1_PMIC_DATA_BYTES 1024u
#define Q1N1_PMIC_UCSI_VERSION 0x20100u   /* reads 0x0210 on this machine (A16-UCSI-REGISTERS.md) */

enum {
    Q1N1_PMIC_OK = 0,
    Q1N1_PMIC_EDENIED = -40,      /* register/length not on the allow-list */
    Q1N1_PMIC_ETIMEDOUT = -41,    /* no reply before the deadline */
    Q1N1_PMIC_EREPLY = -42,       /* reply length or header not as expected */
    Q1N1_PMIC_EREMOTE = -43,      /* remote error word non-zero */
    Q1N1_PMIC_EUNSTABLE = -44,    /* two samples disagreed */
    Q1N1_PMIC_ESTALE = -45,       /* identical to the previous reply's prefix */
    Q1N1_PMIC_EIMPLAUSIBLE = -46, /* decoded, but not a plausible battery */
    Q1N1_PMIC_EORPHAN = -47,      /* an earlier reply is still owed; not sending yet */
};

struct q1n1_pmic_router {
    struct q1n1_glink *glink;
    /* Reply mailbox, filled by q1n1_pmic_router_deliver(). */
    uint8_t reply[Q1N1_PMIC_REPLY_BYTES];
    uint32_t reply_length, have_reply, waiting;
    uint32_t orphans;
    uint64_t orphan_since_us;
    /* The last accepted value, for the stale-copy check (as ucsi-console.c). */
    uint8_t previous[Q1N1_PMGK_BIX_BYTES];
    uint32_t previous_length;
    /* Diagnostics. */
    uint64_t requests, replies, stale_discarded, unsolicited, foreign, bad_replies;
    uint32_t last_owner, last_type, last_opcode, last_error, last_length;
};

/* The glink deliver hook: pass a struct q1n1_pmic_router * as deliver_ctx. */
void q1n1_pmic_router_deliver(void *ctx, const uint8_t *data, uint32_t length);

/* One request/reply, no allow-list, no double read. */
int q1n1_pmic_router_sample(struct q1n1_pmic_router *r, uint32_t reg, uint8_t *data, uint32_t size,
                            uint64_t deadline_us);

/* Allow-listed (battery.h's _BST/_BIX/_PSR, and UCSI VERSION as a known
 * answer), read twice, compared, and checked against the previous reply. */
int q1n1_pmic_router_read(struct q1n1_pmic_router *r, uint32_t reg, uint8_t *data, uint32_t size,
                          uint64_t deadline_us);

/* _BST, _BIX and _PSR, decoded by battery.c. EIMPLAUSIBLE leaves out filled. */
int q1n1_pmic_router_battery(struct q1n1_pmic_router *r, struct q1n1_battery *out, uint64_t deadline_us);
