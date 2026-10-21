/* SPDX-License-Identifier: MIT */
/* A polled GLINK client over SMEM, for one remote processor.
 *
 * GLINK is Qualcomm's inter-processor message protocol. Over SMEM it is two
 * ring buffers (FIFOs) and four index words:
 *   item 478: 32-byte descriptor; word 0 = TX tail (remote writes), word 1 =
 *             TX head (we write), word 2 = RX tail (we write), word 3 = RX
 *             head (remote writes)       Linux qcom_glink_smem.c:34-36,294-297
 *   item 479: our FIFO to the remote; item 480: the remote's FIFO to us.
 * Every command starts with {u16 cmd, u16 param1, u32 param2} and occupies a
 * multiple of 8 bytes (qcom_glink_native.c:35-45, 1172-1241). After writing a
 * frame we publish the new head and ring the remote through IPCC.
 *
 * Two channels exist on a session: the client's (PMIC_RTR_SOCCP_APPS, opened
 * at once) and "glink_ssr" (opened only to release the link). Commands name a
 * channel by the id of the side that sent them, as Linux looks them up: OPEN,
 * CLOSE, INTENT, RX_DONE, RX_INTENT_REQ(_ACK) and TX_DATA carry the remote's
 * id (rcids); OPEN_ACK and CLOSE_ACK carry ours (lcids).
 *
 * Session model. The firmware's own GLINK session cannot be adopted: its
 * channel ids and receive intents lived in GlinkDxe's boot-services heap. So
 * the first session after ExitBootServices restarts the link the way Linux's
 * qcom_glink_smem_register() does (zero our two index words, send VERSION).
 * After that, the session state is plain data (struct q1n1_glink_session) that
 * a chainloaded stage copies and continues, after checking that nobody moved
 * our index words.
 *
 * Releasing. At ExitBootServices the firmware's GlinkDxe (BIOS 312) runs one
 * routine per edge, RVA 0x3358 (called as 0x3358(edge, "SMEM") from the EBS
 * callback at 0x2a08-0x2a6c, the soccp edge among them): it opens the
 * "glink_ssr" channel (name built at 0x3384-0x33ac: 8 bytes "glink_ss" from
 * .rdata 0x7bd8 plus "r\0"), waits for the connected state (0x3470-0x3488;
 * the state callback at 0x334c stores the event), and sends one 48-byte
 * message (0x34e4-0x34f8, glink_tx(handle, 0, msg, 0x30, 0xb)) built at
 * 0x3394-0x3424: {u32 version 0, u32 command 0, u32 seq 0, u32 name_len 5,
 * char name[32] "apss"} -- downstream's struct do_cleanup_msg with command
 * GLINK_SSR_DO_CLEANUP (glink_private.h:680-682, 766-772). (It sets byte
 * [xprt+0xc0] = 1 just before the send, 0x34dc; what that byte does is not
 * known.) It then calls the core's link_down entry on each transport of the
 * edge (0x352c-0x3578: [xprt+0xa8]+8, ops table .data 0x9140/0x91c8 slot 1
 * = 0x4da4), and then loops with no timeout (0x357c-0x35e8) until the
 * transport's op +0x98 returns 1. That op is 0x6570 (stored at 0x1914),
 * downstream's wait_link_down(): it returns 1 only when word 0 ([tx_desc+0])
 * and word 3 ([rx_desc+4]) are both 0, and then calls 0x64dc, which stores 0
 * to word 2 ([rx_desc], 0x6544) and word 1 ([tx_desc+4], 0x6550). The word
 * mapping is fixed at 0x1a08-0x1a28: the flag that picks TX FIFO 479 also
 * picks tx_desc = desc+0 and rx_desc = desc+8. So a receiver that follows
 * downstream's SMEM transport zeroes its own two words (our 0 and 3) on the
 * message (glink_smem_native_xprt.c:1456-1486), the firmware then zeroes
 * ours, and ExitBootServices returns only after all four read 0. If the
 * callback ran for the soccp edge (it is skipped by the gates at
 * 0x3428-0x345c), q1n1 therefore starts from four zero words; words 0 or 3
 * not zero mean it did not run there, and {0, x, y, 0} cannot be what it
 * leaves. q1n1_glink_release() does the same: the same bytes on the same
 * channel, a wait for the SoCCP to zero its words, then ours -- the state
 * wait_link_down() (:1495-1508) treats as down, and the one a Linux-style
 * restart expects.
 *
 * Ordering. Every store to an index word is followed by a store barrier
 * before the IPCC doorbell (ring()), as Linux's writel() gives it.
 *
 * Polled only: no interrupt is needed; every wait has a deadline; any
 * malformed frame or inconsistent index stops the session instead of guessing. */
#pragma once
#include <stdint.h>
#include "smem.h"

#define Q1N1_GLINK_ITEM_DESCRIPTOR 478u
#define Q1N1_GLINK_ITEM_TX_FIFO 479u
#define Q1N1_GLINK_ITEM_RX_FIFO 480u

enum q1n1_glink_cmd {
    Q1N1_GLINK_VERSION = 0, Q1N1_GLINK_VERSION_ACK = 1, Q1N1_GLINK_OPEN = 2, Q1N1_GLINK_CLOSE = 3,
    Q1N1_GLINK_OPEN_ACK = 4, Q1N1_GLINK_INTENT = 5, Q1N1_GLINK_RX_DONE = 6, Q1N1_GLINK_RX_INTENT_REQ = 7,
    Q1N1_GLINK_RX_INTENT_REQ_ACK = 8, Q1N1_GLINK_TX_DATA = 9, Q1N1_GLINK_CLOSE_ACK = 11,
    Q1N1_GLINK_TX_DATA_CONT = 12, Q1N1_GLINK_READ_NOTIF = 13, Q1N1_GLINK_RX_DONE_W_REUSE = 14,
    Q1N1_GLINK_SIGNALS = 15,
};
#define Q1N1_GLINK_FEATURE_INTENT_REUSE 1u   /* the only feature the SMEM transport offers */

enum {
    Q1N1_GLINK_OK = 0,
    Q1N1_GLINK_EINVAL = -20,     /* bad argument or not bound */
    Q1N1_GLINK_ENOENT = -21,     /* descriptor or FIFO item missing */
    Q1N1_GLINK_ECORRUPT = -22,   /* item sizes or index words out of range */
    Q1N1_GLINK_EBUSY = -23,      /* remote index words moved while sampling */
    Q1N1_GLINK_ESTATE = -24,     /* our own index words changed behind our back, or no session */
    Q1N1_GLINK_EPROTO = -25,     /* unknown command or a frame that cannot be right */
    Q1N1_GLINK_ETIMEDOUT = -26,
    Q1N1_GLINK_ENOSPC = -27,     /* message larger than any intent or the FIFO */
    Q1N1_GLINK_EDOORBELL = -28,  /* the IPCC write faulted */
    Q1N1_GLINK_ECLOSED = -29,    /* the remote closed the channel */
    Q1N1_GLINK_EFAILED = -30,    /* session already failed; restart required */
};

enum q1n1_glink_state {
    Q1N1_GLINK_DOWN = 0,         /* nothing sent */
    Q1N1_GLINK_STARTED = 1,      /* VERSION sent; handshake in progress or done */
    Q1N1_GLINK_FAILED = 2,       /* a check failed; only a restart leaves this */
    Q1N1_GLINK_RELEASED = 3,     /* DO_CLEANUP sent, all four words 0: as the firmware leaves it */
};

#define Q1N1_GLINK_CHANNELS 2u
#define Q1N1_GLINK_CH_CLIENT 0u          /* PMIC_RTR_SOCCP_APPS */
#define Q1N1_GLINK_CH_SSR 1u             /* glink_ssr */
#define Q1N1_GLINK_SSR_NAME "glink_ssr"
#define Q1N1_GLINK_SSR_LCID 2u
#define Q1N1_GLINK_LOCAL_INTENTS 4u      /* per channel, at most */
#define Q1N1_GLINK_REMOTE_INTENTS 8u
#define Q1N1_GLINK_INTENT_BYTES 2048u    /* client: holds the 1040-byte PMIC register reply */
#define Q1N1_GLINK_SSR_INTENT_BYTES 64u  /* glink_ssr: a 12-byte cleanup_done */
#define Q1N1_GLINK_NAME_BYTES 32u
#define Q1N1_GLINK_CLEANUP_BYTES 48u

/* The firmware's DO_CLEANUP("apss"), byte for byte (see the top comment). */
void q1n1_glink_cleanup_message(uint8_t out[Q1N1_GLINK_CLEANUP_BYTES]);

struct q1n1_glink_intent {
    uint32_t id, size, used, valid;
};

struct q1n1_glink_channel {
    char name[Q1N1_GLINK_NAME_BYTES];
    uint32_t lcid, rcid;
    uint32_t wanted;                   /* we open it (else a remote OPEN is only recorded, as Linux does) */
    uint32_t ack_sent, open_sent, open_acked, remote_opened, intents_sent, close_sent;
    uint32_t intent_req_pending, intent_req_granted;
    uint32_t local_count, local_size;  /* how many receive intents we offer, and their size */
    struct q1n1_glink_intent local[Q1N1_GLINK_LOCAL_INTENTS];
    struct q1n1_glink_intent remote[Q1N1_GLINK_REMOTE_INTENTS];
};

/* Everything a later stage needs to continue a session. Plain data, all
 * fields 32 or 64 bits; a stage continues it only when magic, version and
 * size match its own exactly. */
#define Q1N1_GLINK_SESSION_MAGIC UINT64_C(0x4b4e4c47314e3151) /* "Q1N1GLNK" */
#define Q1N1_GLINK_SESSION_VERSION 2u
struct q1n1_glink_session {
    uint64_t magic, version, size;
    uint64_t smem_phys;
    uint32_t smem_size, remote_host;
    uint32_t desc_offset, tx_offset, tx_length, rx_offset, rx_length, reserved0;
    uint64_t doorbell_address;
    uint32_t doorbell_value;
    uint32_t state;
    uint32_t tx_head, rx_tail;         /* what we last stored in words 1 and 2 */
    uint32_t features, version_acked, remote_version, version_resends;
    uint32_t next_liid, mode;
    uint64_t started_us;
    struct q1n1_glink_channel ch[Q1N1_GLINK_CHANNELS];
    uint32_t start_words[4];           /* descriptor as found at the last restart */
    uint32_t cleanup_sent, cleanup_done, release_words_zero, release_pads;
    uint64_t client_carry[2];          /* the client's own state across a chainload (pmic-router orphans) */
    /* Diagnostics. */
    uint64_t restarts, adoptions, tx_frames, rx_frames, doorbells, messages;
    uint64_t dropped, foreign_opens, protocol_errors, scrubbed;
    uint64_t releases, service_polls, service_frames, version_zero_acks, remote_closes;
    int32_t last_error;
    uint32_t last_command;             /* command being parsed when last_error was set */
};

struct q1n1_glink_ops {
    uint64_t (*now_us)(void *ctx);
    void (*relax)(void *ctx);                 /* each wait iteration */
    int (*doorbell)(void *ctx, uint64_t address, uint32_t value);   /* 0 on success */
    void *ctx;
    /* A complete message on the client channel; data is valid for the call only. */
    void (*deliver)(void *ctx, const uint8_t *data, uint32_t length);
    void *deliver_ctx;
};

struct q1n1_glink {
    struct q1n1_glink_session s;
    const struct q1n1_glink_ops *ops;
    volatile uint32_t *desc;
    volatile uint8_t *tx, *rx;
    int servicing;                     /* q1n1_glink_service: never wait for FIFO space */
    uint32_t filled[Q1N1_GLINK_CHANNELS][Q1N1_GLINK_LOCAL_INTENTS];
    uint8_t buffer[Q1N1_GLINK_LOCAL_INTENTS][Q1N1_GLINK_INTENT_BYTES];
    uint8_t ssr_buffer[Q1N1_GLINK_SSR_INTENT_BYTES];
};

/* Where the three items are, checked the way bind checks them, without
 * touching any session: what a survey uses. */
struct q1n1_glink_view {
    volatile uint32_t *desc;
    uint32_t desc_offset, tx_offset, tx_length, rx_offset, rx_length, remote_host;
};
int q1n1_glink_view(const struct q1n1_smem *smem, const struct q1n1_smem_partition *part,
                    struct q1n1_glink_view *v);
/* Two samples of the four words through a view (loads only); as q1n1_glink_sample. */
int q1n1_glink_view_sample(const struct q1n1_glink_view *v, const struct q1n1_glink_ops *ops, uint32_t words[4],
                           uint64_t delay_us);

#ifdef Q1N1_GLINK_HOST_TEST
/* Called after each barrier: 1 for one that orders earlier stores (WMB, MB). */
extern void (*q1n1_glink_barrier_hook)(int orders_stores);
#endif

/* Locate 478/479/480 in the partition and point at them. A session already in
 * g->s (copied from a predecessor) is kept only if it names the same SMEM
 * region and items and its channel table is within this build's limits;
 * otherwise g->s is reset to DOWN. */
int q1n1_glink_bind(struct q1n1_glink *g, const struct q1n1_smem *smem, const struct q1n1_smem_partition *part,
                    const struct q1n1_glink_ops *ops, uint64_t doorbell_address, uint32_t doorbell_value);

/* Two samples of the four descriptor words, delay_us apart. EBUSY when a
 * word moved, ECORRUPT when an index is out of range or unaligned. */
int q1n1_glink_sample(struct q1n1_glink *g, uint32_t words[4], uint64_t delay_us);

/* How a restart sets the two index words we own. The remote's words are
 * never written.
 *   LINUX:  word 1 = 0, word 2 = 0, exactly qcom_glink_smem.c:352-353. A third
 *           party ran this on an A16 after UEFI and the SoCCP answered. The
 *           only mode q1n1 chooses by itself, and only when all four words
 *           already read 0 (the cleaned-up state); see live-battery.h.
 *   DRAIN:  as LINUX, but word 2 = word 3: never parse what was left unread.
 *   RESYNC: word 1 = word 0, word 2 = word 3. Both FIFOs are empty from both
 *           sides' point of view, whether or not the remote resets; only the
 *           protocol restarts. Depends on the remote accepting a VERSION on a
 *           link it considered up (unproven on this SoCCP; a downstream core
 *           ignores a second VERSION, glink.c:4114). */
enum q1n1_glink_restart_mode { Q1N1_GLINK_RESTART_LINUX = 0, Q1N1_GLINK_RESTART_DRAIN = 1,
                               Q1N1_GLINK_RESTART_RESYNC = 2 };
/* Sample (refusing on EBUSY/ECORRUPT without writing), set our index words
 * per mode, reset the session, send VERSION and ring the doorbell. The
 * client channel opens as the remote answers (q1n1_glink_wait_ready). */
int q1n1_glink_restart(struct q1n1_glink *g, const char *channel, uint32_t lcid,
                       enum q1n1_glink_restart_mode mode);

/* A copied session is usable only if it is STARTED, words 1 and 2 still hold
 * what it wrote, and the indices are sane. */
int q1n1_glink_check(struct q1n1_glink *g);

/* Handle every complete frame in the RX FIFO. Returns frames handled, or <0. */
int q1n1_glink_poll(struct q1n1_glink *g);

/* The periodic tick's poll: at most max_frames frames, and never a wait. A
 * frame is handled only while our FIFO has room for every reply it can need,
 * so nothing blocks; frames beyond that stay for the next tick. Does nothing
 * unless the session is STARTED; never restarts. Returns frames or <0. */
int q1n1_glink_service(struct q1n1_glink *g, unsigned max_frames);

int q1n1_glink_channel_ready(const struct q1n1_glink *g, unsigned channel);
/* Poll until the client channel is open and our intents are advertised. */
int q1n1_glink_wait_ready(struct q1n1_glink *g, uint64_t deadline_us);
/* Zero the part of our FIFO the remote is not reading (everything outside
 * [word 0, our head)). Only we write that FIFO and the remote only reads the
 * pending range, so this cannot race. Refused (ESTATE) unless word 1 still
 * holds our head. It leaves the FIFO the way a firmware that never wrapped
 * leaves it: if a later client restarts the link Linux's way (our head to 0
 * under a non-zero remote tail), the remote reads zeros -- VERSION 0, which
 * GLINK ignores -- instead of replaying our old frames. */
int q1n1_glink_scrub(struct q1n1_glink *g);

/* Send one message in one TX_DATA on a channel; waits (bounded) for a
 * remote intent, asking for one if none fits. */
int q1n1_glink_send(struct q1n1_glink *g, unsigned channel, const void *data, uint32_t length,
                    uint64_t deadline_us);

/* Leave the SoCCP as the firmware leaves it at ExitBootServices: open
 * glink_ssr, send the firmware's DO_CLEANUP("apss"), wait until the remote
 * has zeroed words 0 and 3, then zero our FIFO and words 1 and 2 and mark the
 * session RELEASED. A zero remote tail is taken as the remote's reset only
 * while our head is not 0 (else one VERSION 0 frame moves it first, counted
 * in release_pads). Needs a session q1n1_glink_check accepts. On a timeout
 * our words are left as they were and the session is FAILED (nobody is
 * reading any more; the SoCCP may still act on the message). */
int q1n1_glink_release(struct q1n1_glink *g, uint64_t deadline_us);
