/* SPDX-License-Identifier: MIT */
/* Live battery path (smem.c, glink-smem.c, pmic-router.c, live-battery.c)
 * against a simulated SoC: an SMEM image in RAM with the APPS<->SoCCP
 * partition, GLINK FIFOs, an SMP2P record and target info, plus a simulated
 * SoCCP that speaks the remote half of GLINK (two channels: the PMIC router
 * and glink_ssr), answers PMIC register reads, sends notifications, and acts
 * on the firmware's DO_CLEANUP the way downstream's SMEM transport does.
 * Nothing here is evidence about the laptop; it proves the stage code does
 * what the wire formats say, wraps its FIFOs correctly, and refuses cleanly.
 *
 * clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror \
 *   -DQ1N1_GLINK_HOST_TEST -Iplatform/uefi tools/test-q1n1-live-battery.c \
 *   platform/uefi/smem.c platform/uefi/glink-smem.c platform/uefi/pmic-router.c \
 *   platform/uefi/battery.c -o /tmp/test-q1n1-live-battery && /tmp/test-q1n1-live-battery */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
/* The stage's statics (session, router, record) are reset between scenarios,
 * and a chainload is simulated by clearing them and adopting a saved copy. */
#include "../platform/uefi/live-battery.c"

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { printf("FAIL %s line %d: %s\n", scenario, __LINE__, #x); dump(); exit(1); } } while (0)
static const char *scenario = "";

/* ------------------------------------------------------------------ */
/* Simulated SoC                                                        */
/* ------------------------------------------------------------------ */

#define SMEM_PHYS 0xffe00000ull
#define SMEM_SIZE 0x200000u
#define SMEM_ALIGN 0x10000u             /* so any host page size can protect SMEM ranges */
#define TARGET_PHYS 0x146aa000ull
#define PART_OFFSET 0x10000u
#define PART_SIZE 0x10000u
#define FOREIGN_OFFSET 0x50000u         /* ADSP<->SoCCP: not APPS's; must never be read */
#define FOREIGN_SIZE 0x10000u
#define ALT_OFFSET 0x60000u             /* layout.alt_host: a second APPS partition with GLINK items */
#define ALT_SIZE 0x10000u
#define REMOTE_LCID 0x21u
#define LOGS_LCID 0x22u
#define SSR_LCID 0x23u

/* The firmware's DO_CLEANUP("apss"), written out from GlinkDxe 0x3358
 * (zeroed 48 bytes; +0x0c = 5; +0x10 = "apss"), independent of the code. */
static const uint8_t firmware_cleanup[48] = {
    0, 0, 0, 0,  0, 0, 0, 0,  0, 0, 0, 0,  5, 0, 0, 0,  'a', 'p', 's', 's',
};

struct frame_queue { uint8_t data[1 << 16]; uint32_t length; };
struct rintent { uint32_t id, size, used, valid; };

struct remote {
    int enabled;
    /* Behaviour knobs. */
    int ignore_version, send_version, remote_opens, no_intent, reuse, features;
    uint32_t chunk;                /* split replies into chunks of at most this */
    uint32_t delay_first_us;       /* hold the first reply this long */
    int drop_replies;              /* never answer this many requests */
    uint32_t reply_length;         /* 1040 normally */
    uint32_t reply_opcode, remote_error, reply_owner;
    int flap;                      /* alternate BST remaining capacity */
    int implausible;
    int close_after_reply;
    int unsolicited_before;        /* send one 0x8011 reply nobody asked for, before the answer */
    int stray_on_intent_req;       /* answer our RX_INTENT_REQ with a stray 0x8011 reply first */
    int bad_command_after_open;    /* send command 10 once the channel is open */
    uint32_t want_intent;          /* ask APPS for an intent of this size at open */
    int moving;                    /* keep advancing word 3 with nothing behind it */
    int stall_after_open;          /* stop reading our FIFO once the channel is open */
    int ack_zero_version;          /* answer each VERSION 0 with VERSION_ACK 0 (downstream core) */
    int reopen_once;               /* re-OPEN the PMIC channel under the same id, new intent */
    int ssr_open_early;            /* open glink_ssr as soon as the link is up */
    int ssr_never;                 /* never answer our glink_ssr OPEN */
    int ssr_no_intent;             /* no intent on glink_ssr until asked */
    int ssr_reply;                 /* send cleanup_done */
    int ignore_cleanup;            /* read DO_CLEANUP but never reset */
    uint32_t ssr_delay_us;         /* reset this long after DO_CLEANUP */
    /* State. */
    int version_seen, opened, open_ack_seen, apps_open_seen;
    uint32_t apps_lcid;
    struct rintent apps[16];
    uint32_t my_iid, my_size, my_used;
    uint32_t next_iid;
    int ssr_opened, ssr_open_seen, ssr_acked_by_apps;
    uint32_t ssr_apps_lcid, ssr_iid, ssr_used;
    struct rintent ssr_apps[4];
    int ssr_pending, down;
    uint64_t ssr_due;
    int awaiting_apps_close;
    struct frame_queue out;
    struct { uint8_t data[1040]; uint32_t length; uint64_t due; int active; } held;
    int held_pending_intent;
    uint32_t pending_notify, notified, notify_req_out, notify_reqs, req_acks;
    uint32_t requests, answered, violations, versions, zero_versions, apps_closes, apps_opens;
    uint32_t cleanups, cleanup_matches, resets, down_ignored;
    uint32_t last_reg, last_len;
    uint32_t first_frame_cmd, first_frame_offset, frames_in;
};

struct sim {
    uint8_t *smem;
    uint64_t smem_phys;
    uint32_t smem_size;
    uint64_t target_pointer;
    uint8_t target[24];
    uint32_t ipcc_version;
    uint64_t now;
    uint32_t doorbells, bad_doorbells;
    uint64_t desc, fifo0, fifo1;       /* offsets in smem */
    uint32_t fifo0_len, fifo1_len;
    int fault_target, fault_ipcc, fault_page_offset, attr_fail, fault_writes, fault_doorbell;
    uint32_t attr, ipcc_attr;
    int ipcc_attr_fail;
    int64_t attr_page;                 /* one SMEM page (offset) mapped differently, or -1 */
    uint32_t attr_page_value;
    uint32_t writes;                   /* platform write32 calls outside IPCC */
    uint32_t fenced[2];                /* words 1 and 2 as of the last store-ordering barrier */
    uint32_t unordered_doorbells;      /* doorbells with a store to word 1/2 not yet behind a barrier */
    uint64_t alt_desc;                 /* layout.alt_host: that partition's descriptor */
    uint32_t reads;                    /* platform read calls */
    struct remote r;
} sim;

static void dump(void)
{
    const struct q1n1_live_battery *r = &result;
    printf("  status %lld phase %llu text '%s'\n", (long long)r->status, (unsigned long long)r->phase, r->text);
    printf("  glink state %llu error %lld cmd %llu restarts %llu adoptions %llu tx %llu rx %llu drop %llu\n",
           (unsigned long long)r->glink_state, (long long)r->glink_error, (unsigned long long)r->glink_command,
           (unsigned long long)r->restarts, (unsigned long long)r->adoptions, (unsigned long long)r->tx_frames,
           (unsigned long long)r->rx_frames, (unsigned long long)r->dropped);
    printf("  remote requests %u answered %u violations %u versions %u cleanups %u/%u resets %u down %d\n",
           sim.r.requests, sim.r.answered, sim.r.violations, sim.r.versions, sim.r.cleanup_matches, sim.r.cleanups,
           sim.r.resets, sim.r.down);
}

static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static void wr32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static void wr16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static volatile uint32_t *word(unsigned i) { return (volatile uint32_t *)(sim.smem + sim.desc) + i; }
static int words_zero(void) { return !(*word(0) | *word(1) | *word(2) | *word(3)); }

/* ---- SMEM image builder ---- */

struct part_builder { uint32_t offset, size, free_uncached, free_cached, cacheline; };

static void part_header(struct part_builder *p, uint32_t host0, uint32_t host1)
{
    uint8_t *h = sim.smem + p->offset;
    memcpy(h, "$PRT", 4);
    wr16(h + 4, host0);
    wr16(h + 6, host1);
    wr32(h + 8, p->size);
    wr32(h + 12, p->free_uncached);
    wr32(h + 16, p->free_cached);
}

static uint32_t add_uncached(struct part_builder *p, uint32_t item, uint32_t size)
{
    uint32_t padded = (size + 7) & ~7u;
    uint8_t *e = sim.smem + p->offset + p->free_uncached;
    wr16(e, 0xa5a5);
    wr16(e + 2, item);
    wr32(e + 4, padded);
    wr16(e + 8, padded - size);
    wr16(e + 10, 0);
    uint32_t data = p->offset + p->free_uncached + 16;
    p->free_uncached += 16 + padded;
    return data;
}

static uint32_t add_cached(struct part_builder *p, uint32_t item, uint32_t size)
{
    uint32_t padded = (size + 7) & ~7u, slot = (16 + p->cacheline - 1) & ~(p->cacheline - 1);
    uint32_t header = p->free_cached - slot;
    uint8_t *e = sim.smem + p->offset + header;
    wr16(e, 0xa5a5);
    wr16(e + 2, item);
    wr32(e + 4, padded);
    wr16(e + 8, padded - size);
    p->free_cached = header - padded;
    return p->offset + p->free_cached;
}

struct layout {
    uint32_t version;              /* 11 or 12 */
    uint32_t host;                 /* SoCCP partition host (19) */
    uint32_t fifo0, fifo1;         /* FIFO sizes */
    int fifo0_uncached;            /* default: 479 on the cached list, like downstream */
    int no_rx_fifo, desc_size, no_smp2p, fatal, not_ready, duplicate, no_siii;
    uint32_t w[4];                 /* initial descriptor words */
    uint32_t alt_host;             /* also an APPS<->alt_host partition with 478/479/480 and SMP2P */
};

static void add_smp2p(struct part_builder *p, uint32_t host, uint32_t value)
{
    uint32_t item = add_uncached(p, 617, 20 + 16 * 20);
    uint8_t *s = sim.smem + item;
    wr32(s, 0x504d5324);
    s[4] = 1;
    wr16(s + 8, host); wr16(s + 10, 0);
    wr16(s + 12, 16); wr16(s + 14, 2);
    memcpy(s + 20, "master-kernel\0\0\0", 16);   /* not ours; skipped */
    memcpy(s + 40, "slave-kernel\0xyz", 16);     /* bytes after the NUL do not matter */
    wr32(s + 56, value);
}

static void build(const struct layout *l)
{
    if (!sim.smem) sim.smem = aligned_alloc(SMEM_ALIGN, SMEM_SIZE);
    memset(sim.smem, 0, SMEM_SIZE);
    uint8_t *save = sim.smem;
    memset(&sim, 0, sizeof(sim));
    sim.smem = save;
    sim.smem_phys = SMEM_PHYS;
    sim.smem_size = SMEM_SIZE;
    sim.ipcc_version = 0x00020000;
    sim.attr = 0x44;
    sim.ipcc_attr = 0x04;
    sim.attr_page = -1;
    sim.now = 1000000;
    sim.fault_page_offset = -1;

    wr32(sim.smem + 0xc0, 1);
    wr32(sim.smem + 0x5c, (l->version ? l->version : 11) << 16);
    uint8_t *toc = sim.smem + SMEM_SIZE - 0x1000;
    memcpy(toc, "$TOC", 4);
    wr32(toc + 4, 1);
    unsigned entries = 0;
    struct part_builder p = {.offset = PART_OFFSET, .size = PART_SIZE, .free_uncached = 32,
                             .free_cached = PART_SIZE, .cacheline = 64};
    uint32_t host = l->host ? l->host : 19;
    uint8_t *e = toc + 32 + 48 * entries++;
    wr32(e, p.offset); wr32(e + 4, p.size); wr16(e + 12, 0); wr16(e + 14, (uint16_t)host); wr32(e + 16, p.cacheline);
    /* An ADSP partition next to it, which must not be picked. */
    e = toc + 32 + 48 * entries++;
    wr32(e, 0x20000); wr32(e + 4, 0x8000); wr16(e + 12, 2); wr16(e + 14, 0); wr32(e + 16, 64);
    struct part_builder adsp = {.offset = 0x20000, .size = 0x8000, .free_uncached = 32, .free_cached = 0x8000, .cacheline = 64};
    add_uncached(&adsp, 478, 32);
    part_header(&adsp, 2, 0);
    /* Two processors that are not APPS share this one. Its header is left
     * zero: opening it would be a structure error, reading it an
     * access-control risk. */
    e = toc + 32 + 48 * entries++;
    wr32(e, FOREIGN_OFFSET); wr32(e + 4, FOREIGN_SIZE); wr16(e + 12, 2); wr16(e + 14, (uint16_t)host); wr32(e + 16, 64);
    if (l->duplicate) {
        e = toc + 32 + 48 * entries++;
        wr32(e, 0x30000); wr32(e + 4, 0x8000); wr16(e + 12, (uint16_t)host); wr16(e + 14, 0); wr32(e + 16, 64);
        struct part_builder dup = {.offset = 0x30000, .size = 0x8000, .free_uncached = 32, .free_cached = 0x8000};
        part_header(&dup, host, 0);
    }
    if ((l->version ? l->version : 11) == 12) {
        e = toc + 32 + 48 * entries++;
        wr32(e, 0x40000); wr32(e + 4, 0x8000); wr16(e + 12, 0xfffe); wr16(e + 14, 0xfffe); wr32(e + 16, 64);
        struct part_builder global = {.offset = 0x40000, .size = 0x8000, .free_uncached = 32, .free_cached = 0x8000};
        part_header(&global, 0xfffe, 0xfffe);
    }
    if (l->alt_host) {
        /* Another remote APPS shares a partition with, carrying its own GLINK
         * items: nobody answers there. */
        e = toc + 32 + 48 * entries++;
        wr32(e, ALT_OFFSET); wr32(e + 4, ALT_SIZE); wr16(e + 12, 0); wr16(e + 14, (uint16_t)l->alt_host);
        wr32(e + 16, 64);
        struct part_builder alt = {.offset = ALT_OFFSET, .size = ALT_SIZE, .free_uncached = 32,
                                   .free_cached = ALT_SIZE, .cacheline = 64};
        sim.alt_desc = add_uncached(&alt, 478, 32);
        add_uncached(&alt, 479, 0x1000);
        add_uncached(&alt, 480, 0x1000);
        add_smp2p(&alt, l->alt_host, 2);
        part_header(&alt, 0, l->alt_host);
    }
    wr32(toc + 8, entries);
    if (!l->no_siii) {
        uint8_t *info = toc + 32 + 48 * entries;
        memcpy(info, "SIII", 4);
        wr16(info + 16, 1024);
    }

    sim.fifo0_len = l->fifo0 ? l->fifo0 : 0x4000;
    sim.fifo1_len = l->fifo1 ? l->fifo1 : 0x2000;
    sim.desc = add_uncached(&p, 478, l->desc_size ? (uint32_t)l->desc_size : 32);
    if (!l->no_rx_fifo) sim.fifo1 = add_uncached(&p, 480, sim.fifo1_len);
    if (!l->no_smp2p) add_smp2p(&p, host, l->fatal ? 3 : l->not_ready ? 0 : 2);
    sim.fifo0 = l->fifo0_uncached ? add_uncached(&p, 479, sim.fifo0_len) : add_cached(&p, 479, sim.fifo0_len);
    part_header(&p, 0, host);
    if (!l->desc_size || l->desc_size == 32) for (unsigned i = 0; i < 4; i++) *word(i) = l->w[i];
    sim.fenced[0] = *word(1);
    sim.fenced[1] = *word(2);

    /* Target info, where SmemDxe finds it. */
    sim.target_pointer = TARGET_PHYS;
    wr32(sim.target, 0x49494953);
    wr32(sim.target + 4, SMEM_SIZE);
    wr32(sim.target + 8, (uint32_t)SMEM_PHYS);
    wr32(sim.target + 12, 0);
    wr16(sim.target + 16, 1024);

    struct remote *r = &sim.r;
    r->enabled = 1;
    r->remote_opens = 1;
    r->reuse = 1;
    r->features = 1;
    r->chunk = 4096;
    r->reply_length = 1040;
    r->reply_opcode = 0x90;
    r->reply_owner = 0x8011;
    r->my_iid = 0x33;
    r->my_size = 512;
    r->next_iid = 0x40;
    r->ssr_iid = 0x60;
    r->ssr_reply = 1;
}

/* ---- the simulated SoCCP ---- */

static uint32_t remote_tx_avail(void)
{
    uint32_t head = *word(3), tail = *word(2), len = sim.fifo1_len;
    uint32_t avail = tail <= head ? len - head + tail : tail - head;
    return avail < 16 ? 0 : avail - 16;
}

static void remote_flush(void)
{
    struct remote *r = &sim.r;
    uint32_t done = 0;
    while (done < r->out.length) {
        uint32_t len = rd32(r->out.data + done);        /* each queued frame has a length prefix */
        uint8_t *frame = r->out.data + done + 4;
        if (remote_tx_avail() < len) break;
        uint32_t head = *word(3);
        for (uint32_t i = 0; i < len; i++) {
            sim.smem[sim.fifo1 + head] = frame[i];
            if (++head == sim.fifo1_len) head = 0;
        }
        head = (head + 7) & ~7u;
        if (head >= sim.fifo1_len) head -= sim.fifo1_len;
        *word(3) = head;
        done += 4 + ((len + 7) & ~7u);
    }
    memmove(r->out.data, r->out.data + done, r->out.length - done);
    r->out.length -= done;
}

static void queue(const uint8_t *frame, uint32_t len)
{
    struct remote *r = &sim.r;
    uint32_t padded = (len + 7) & ~7u;
    if (r->out.length + 4 + padded > sizeof(r->out.data)) { r->violations++; return; }
    wr32(r->out.data + r->out.length, len);
    memset(r->out.data + r->out.length + 4, 0, padded);
    memcpy(r->out.data + r->out.length + 4, frame, len);
    r->out.length += 4 + padded;
}

static void control(uint32_t cmd, uint32_t p1, uint32_t p2)
{
    uint8_t f[8];
    wr16(f, cmd); wr16(f + 2, p1); wr32(f + 4, p2);
    queue(f, 8);
}

static void remote_open(uint32_t lcid, const char *name)
{
    uint8_t f[48] = {0};
    uint32_t n = (uint32_t)strlen(name) + 1;
    wr16(f, 2); wr16(f + 2, lcid); wr32(f + 4, n);
    memcpy(f + 8, name, n);
    queue(f, 8 + ((n + 7) & ~7u));
}

static void remote_intent(uint32_t lcid, uint32_t iid, uint32_t size)
{
    uint8_t f[16];
    wr16(f, 5); wr16(f + 2, lcid); wr32(f + 4, 1);
    wr32(f + 8, size); wr32(f + 12, iid);
    queue(f, 16);
}

static void put_le(uint8_t *p, unsigned index, uint32_t v) { wr32(p + 4 * index, v); }

static void register_value(uint32_t reg, uint8_t *data)
{
    struct remote *r = &sim.r;
    memset(data, 0, 1024);
    if (reg == 0x101a8) {
        put_le(data, 0, 2);
        put_le(data, 1, 15000);
        put_le(data, 2, 51230 + (r->flap ? (r->answered & 1) * 10 : 0));
        put_le(data, 3, r->implausible ? 1 : 17120);
    } else if (reg == 0x10110) {
        put_le(data, 0, 1); put_le(data, 1, 0); put_le(data, 2, 70000); put_le(data, 3, 68000);
        put_le(data, 4, 1); put_le(data, 5, 15480); put_le(data, 8, 14);
    } else if (reg == 0x101d8) {
        put_le(data, 0, 1);
    } else if (reg == 0x20100) {
        put_le(data, 0, 0x0210);
    }
}

/* One message in TX_DATA/TX_DATA_CONT chunks to a free APPS intent. */
static int send_to(struct rintent *table, unsigned count, uint32_t lcid, const uint8_t *msg, uint32_t length)
{
    struct remote *r = &sim.r;
    int slot = -1;
    for (unsigned i = 0; i < count; i++)
        if (table[i].valid && !table[i].used && table[i].size >= length) { slot = (int)i; break; }
    if (slot < 0) return 0;
    table[slot].used = 1;
    uint32_t done = 0;
    while (done < length) {
        uint32_t chunk = length - done < r->chunk ? length - done : r->chunk;
        uint8_t f[16 + 4096];
        wr16(f, done ? 12 : 9); wr16(f + 2, lcid); wr32(f + 4, table[slot].id);
        wr32(f + 8, chunk); wr32(f + 12, length - done - chunk);
        memcpy(f + 16, msg + done, chunk);
        queue(f, 16 + chunk);
        done += chunk;
    }
    return 1;
}

static int send_reply(const uint8_t *reply, uint32_t length)
{
    struct remote *r = &sim.r;
    if (!send_to(r->apps, 16, REMOTE_LCID, reply, length)) return 0;
    r->answered++;
    if (r->close_after_reply) {
        control(3, REMOTE_LCID, 0);
        r->opened = r->open_ack_seen = r->apps_open_seen = 0;
        r->awaiting_apps_close = 1;
        memset(r->apps, 0, sizeof(r->apps));
    }
    return 1;
}

static void build_reply(uint32_t reg, uint8_t *reply, uint32_t *length)
{
    struct remote *r = &sim.r;
    memset(reply, 0, 1040);
    wr32(reply, r->reply_owner);
    wr32(reply + 4, 1);
    wr32(reply + 8, r->reply_opcode);
    register_value(reg, reply + 12);
    wr32(reply + 1036, r->remote_error);
    *length = r->reply_length;
}

static struct rintent *apps_table(uint32_t lcid, unsigned *count)
{
    struct remote *r = &sim.r;
    if (r->apps_lcid && lcid == r->apps_lcid) { *count = 16; return r->apps; }
    if (r->ssr_apps_lcid && lcid == r->ssr_apps_lcid) { *count = 4; return r->ssr_apps; }
    return 0;
}

/* A new link: everything the SoCCP knew about APPS is gone. */
static void remote_link_reset(void)
{
    struct remote *r = &sim.r;
    r->opened = r->open_ack_seen = r->apps_open_seen = 0;
    memset(r->apps, 0, sizeof(r->apps));
    memset(r->ssr_apps, 0, sizeof(r->ssr_apps));
    r->my_used = r->ssr_used = 0;
    r->ssr_opened = r->ssr_open_seen = r->ssr_acked_by_apps = 0;
    r->notify_req_out = 0;
}

static void remote_frame(const uint8_t *f, uint32_t avail, uint32_t *consumed)
{
    struct remote *r = &sim.r;
    uint32_t cmd = f[0] | f[1] << 8, p1 = f[2] | f[3] << 8, p2 = rd32(f + 4), len = 8;
    if (!r->frames_in++) r->first_frame_cmd = cmd;
    switch (cmd) {
    case 0: /* VERSION */
        if (!p1) {                       /* version 0 (zeroed FIFO) */
            r->zero_versions++;
            if (r->ack_zero_version) control(1, 0, 0);
            break;
        }
        r->versions++;
        if (p1 != 1 || (p2 & ~1u)) r->violations++;
        if (r->ignore_version) break;
        /* Taken as a peer restart: our channel state goes. */
        remote_link_reset();
        control(1, 1, p2 & (uint32_t)r->features);
        if (r->send_version) control(0, 1, (uint32_t)r->features);
        r->version_seen = 1;
        if (r->remote_opens && !r->opened) {
            remote_open(REMOTE_LCID, "PMIC_RTR_SOCCP_APPS");
            remote_open(LOGS_LCID, "PMIC_LOGS_SOCCP_APPS");
            r->opened = 1;
        }
        if (r->ssr_open_early && !r->ssr_opened) { remote_open(SSR_LCID, "glink_ssr"); r->ssr_opened = 1; }
        break;
    case 1: break; /* VERSION_ACK to our VERSION */
    case 2: { /* OPEN */
        uint32_t n = p2 & 0xffff;
        len = 8 + ((n + 7) & ~7u);
        if (avail < len) { *consumed = 0; return; }
        for (uint32_t i = n; i < ((n + 7) & ~7u); i++) if (f[8 + i]) r->violations++;
        if (n == 20 && !memcmp(f + 8, "PMIC_RTR_SOCCP_APPS", 20)) {
            if (r->awaiting_apps_close) r->violations++;    /* reopened without closing our side */
            r->apps_lcid = p1;
            r->apps_open_seen = 1;
            r->apps_opens++;
            control(4, p1, 0);
            if (!r->opened) { remote_open(REMOTE_LCID, "PMIC_RTR_SOCCP_APPS"); r->opened = 1; }
        } else if (n == 10 && !memcmp(f + 8, "glink_ssr", 10)) {
            r->ssr_open_seen = 1;
            if (r->ssr_never) break;
            r->ssr_apps_lcid = p1;
            if (p1 == r->apps_lcid) r->violations++;
            control(4, p1, 0);
            if (!r->ssr_opened) { remote_open(SSR_LCID, "glink_ssr"); r->ssr_opened = 1; }
        } else {
            r->violations++;
        }
        break;
    }
    case 4: /* OPEN_ACK */
        if (p1 == REMOTE_LCID) {
            r->open_ack_seen = 1;
            if (r->want_intent) control(7, REMOTE_LCID, r->want_intent);
            if (!r->no_intent) { remote_intent(REMOTE_LCID, r->my_iid, r->my_size); r->my_used = 0; }
            if (r->bad_command_after_open) control(10, 0, 0);
            if (r->reopen_once) {
                /* Open it again under the same id without a CLOSE: the intent
                 * offered above is void; only the next one is valid. */
                r->reopen_once = 0;
                r->my_iid = r->next_iid++;
                remote_open(REMOTE_LCID, "PMIC_RTR_SOCCP_APPS");
            }
        } else if (p1 == SSR_LCID) {
            if (!r->ssr_opened) r->violations++;
            r->ssr_acked_by_apps = 1;
            if (!r->ssr_no_intent) { remote_intent(SSR_LCID, r->ssr_iid, 64); r->ssr_used = 0; }
        } else {
            r->violations++;   /* nobody claims the logs channel */
        }
        break;
    case 5: { /* INTENT */
        len = 8 + 8 * p2;
        if (avail < len) { *consumed = 0; return; }
        unsigned count;
        struct rintent *table = apps_table(p1, &count);
        if (!table) { r->violations++; break; }
        for (uint32_t i = 0; i < p2; i++) {
            uint32_t size = rd32(f + 8 + 8 * i), id = rd32(f + 12 + 8 * i);
            int placed = 0;
            for (unsigned k = 0; k < count && !placed; k++)
                if (!table[k].valid || table[k].id == id) {
                    table[k] = (struct rintent){.id = id, .size = size, .valid = 1}; placed = 1;
                }
        }
        if (r->held_pending_intent && send_reply(r->held.data, r->held.length)) r->held_pending_intent = 0;
        break;
    }
    case 6: case 14: { /* RX_DONE */
        unsigned count;
        struct rintent *table = apps_table(p1, &count);
        if (!table) { r->violations++; break; }
        for (unsigned k = 0; k < count; k++)
            if (table[k].valid && table[k].id == p2) { table[k].used = 0; if (cmd == 6) table[k].valid = 0; }
        break;
    }
    case 7: /* RX_INTENT_REQ */
        if (p1 == r->apps_lcid) {
            if (r->stray_on_intent_req) {
                uint8_t reply[1040];
                uint32_t length;
                build_reply(0x101d8, reply, &length);
                send_to(r->apps, 16, REMOTE_LCID, reply, length);
                r->stray_on_intent_req = 0;
            }
            r->my_iid = r->next_iid++;
            r->my_size = p2 < 64 ? 64 : p2;
            r->my_used = 0;
            remote_intent(REMOTE_LCID, r->my_iid, r->my_size);
            control(8, REMOTE_LCID, 1);
        } else if (p1 == r->ssr_apps_lcid) {
            r->ssr_iid = r->next_iid++;
            r->ssr_used = 0;
            remote_intent(SSR_LCID, r->ssr_iid, p2);
            control(8, SSR_LCID, 1);
        } else {
            r->violations++;
        }
        break;
    case 8: /* RX_INTENT_REQ_ACK */
        if (p1 != r->apps_lcid) r->violations++;
        r->notify_req_out = 0;
        r->req_acks++;
        break;
    case 9: { /* TX_DATA */
        uint32_t chunk = rd32(f + 8), left = rd32(f + 12);
        len = (16 + chunk + 7) & ~7u;
        if (avail < len) { *consumed = 0; return; }
        if (r->ssr_apps_lcid && p1 == r->ssr_apps_lcid) {
            if (p2 != r->ssr_iid || r->ssr_used || left || chunk != 48) { r->violations++; break; }
            r->cleanups++;
            if (!memcmp(f + 16, firmware_cleanup, 48)) r->cleanup_matches++;
            else r->violations++;
            control(14, SSR_LCID, r->ssr_iid);
            if (r->ssr_reply) {
                uint8_t done[12] = {0};
                wr32(done + 4, 1);               /* GLINK_SSR_CLEANUP_DONE, version 0, seq 0 */
                if (!send_to(r->ssr_apps, 4, SSR_LCID, done, 12)) r->violations++;
            }
            if (!r->ignore_cleanup) { r->ssr_pending = 1; r->ssr_due = sim.now + r->ssr_delay_us; }
            break;
        }
        if (p1 != r->apps_lcid || p2 != r->my_iid || r->my_used || left || chunk != 24 || chunk > r->my_size) {
            r->violations++;
            break;
        }
        r->my_used = 1;
        const uint8_t *q = f + 16;
        if (rd32(q) != 0x8011 || rd32(q + 4) != 1 || rd32(q + 8) != 0x90 || rd32(q + 12)) r->violations++;
        r->last_reg = rd32(q + 16);
        r->last_len = rd32(q + 20);
        r->requests++;
        control(r->reuse ? 14 : 6, REMOTE_LCID, r->my_iid);
        if (r->reuse) r->my_used = 0;
        else { r->my_iid = r->next_iid++; remote_intent(REMOTE_LCID, r->my_iid, r->my_size); r->my_used = 0; }
        if (r->drop_replies > 0) { r->drop_replies--; break; }
        uint8_t reply[1040];
        uint32_t length;
        if (r->unsolicited_before) {
            build_reply(0x101d8, reply, &length);
            r->unsolicited_before = 0;
            send_reply(reply, length);
            r->answered--;
        }
        build_reply(r->last_reg, reply, &length);
        if (r->delay_first_us) {
            memcpy(r->held.data, reply, length);
            r->held.length = length;
            r->held.due = sim.now + r->delay_first_us;
            r->held.active = 1;
            r->delay_first_us = 0;
            break;
        }
        if (!send_reply(reply, length)) {
            memcpy(r->held.data, reply, length);
            r->held.length = length;
            r->held_pending_intent = 1;
            control(7, REMOTE_LCID, length);
        }
        break;
    }
    case 11: break; /* CLOSE_ACK */
    case 3: /* CLOSE of our side */
        control(11, p1, 0);
        if (p1 == r->apps_lcid) { r->apps_open_seen = 0; r->apps_closes++; r->awaiting_apps_close = 0; }
        break;
    case 13: break;
    default:
        r->violations++;
        break;
    }
    *consumed = (len + 7) & ~7u;
}

/* Battery-manager / UCSI notifications into APPS intents; when none is free,
 * ask for one (once) the way a GLINK core does, and wait. */
static void remote_notify(void)
{
    struct remote *r = &sim.r;
    while (r->pending_notify && r->open_ack_seen) {
        uint8_t message[16] = {0};
        wr32(message, r->notified & 1 ? 0x800b : 0x800a);
        wr32(message + 4, 2);
        wr32(message + 8, 7);
        if (!send_to(r->apps, 16, REMOTE_LCID, message, sizeof(message))) {
            if (!r->notify_req_out) { control(7, REMOTE_LCID, 16); r->notify_req_out = 1; r->notify_reqs++; }
            break;
        }
        r->pending_notify--;
        r->notified++;
    }
}

static void remote_step(void)
{
    struct remote *r = &sim.r;
    if (!r->enabled) return;
    if (r->moving) { *word(3) = (*word(3) + 8) % sim.fifo1_len; return; }
    if (r->down) {
        /* Downstream wait_link_down(): nothing until all four words are 0. */
        if (!words_zero()) { r->down_ignored++; return; }
        r->down = 0;
        r->version_seen = 0;
        r->out.length = 0;
        r->held.active = 0;
        r->held_pending_intent = 0;
        remote_link_reset();
    }
    for (int guard = 0; guard < 1000 && !(r->stall_after_open && r->open_ack_seen); guard++) {
        uint32_t head = *word(1), tail = *word(0), len = sim.fifo0_len;
        if (head >= len || tail >= len) { r->violations++; return; }
        uint32_t avail = head >= tail ? head - tail : len - tail + head;
        if (avail < 8) break;
        uint8_t f[16 + 4096 + 64];
        uint32_t take = avail < sizeof(f) ? avail : sizeof(f);
        for (uint32_t i = 0, at = tail; i < take; i++) { f[i] = sim.smem[sim.fifo0 + at]; if (++at == len) at = 0; }
        uint32_t consumed = 8;
        if (!r->frames_in) r->first_frame_offset = tail;
        remote_frame(f, avail, &consumed);
        if (!consumed) break;
        tail += consumed;
        if (tail >= len) tail -= len;
        *word(0) = tail;
    }
    if (r->held.active && sim.now >= r->held.due) {
        if (send_reply(r->held.data, r->held.length)) r->held.active = 0;
    }
    remote_notify();
    remote_flush();
    if (r->ssr_pending && sim.now >= r->ssr_due) {
        /* downstream ssr(): tx write_index (our word 3) and rx read_index
         * (our word 0) to 0, link down. */
        r->ssr_pending = 0;
        *word(3) = 0;
        *word(0) = 0;
        r->down = 1;
        r->resets++;
        r->out.length = 0;
    }
}

/* ---- the platform the stage sees ---- */

static uint32_t unordered_total;       /* unordered doorbells over every scenario: checked at the end */

static int sim_read(void *ctx, void *dst, uint64_t phys, uint32_t size)
{
    (void)ctx;
    sim.reads++;
    if ((phys & 3) || (size & 3)) return Q1N1_LIVE_EFAULT;
    if (phys == Q1N1_SMEM_TARGET_INFO_POINTER && size == 8) {
        if (sim.fault_target) return Q1N1_LIVE_EFAULT;
        memcpy(dst, &sim.target_pointer, 8);
        return 0;
    }
    if (phys >= TARGET_PHYS && phys + size <= TARGET_PHYS + 24) { memcpy(dst, sim.target + (phys - TARGET_PHYS), size); return 0; }
    if (phys == Q1N1_IPCC_FRAME && size == 4) {
        if (sim.fault_ipcc) return Q1N1_LIVE_EFAULT;
        memcpy(dst, &sim.ipcc_version, 4);
        return 0;
    }
    if (phys >= sim.smem_phys && phys + size <= sim.smem_phys + sim.smem_size) {
        if (sim.fault_page_offset >= 0 && phys - sim.smem_phys == (uint64_t)sim.fault_page_offset) return Q1N1_LIVE_EFAULT;
        memcpy(dst, sim.smem + (phys - sim.smem_phys), size);
        return 0;
    }
    return Q1N1_LIVE_EFAULT;
}

static int sim_write32(void *ctx, uint64_t phys, uint32_t value)
{
    (void)ctx;
    if (phys == Q1N1_IPCC_FRAME + Q1N1_IPCC_SEND) {
        if (sim.fault_ipcc || sim.fault_doorbell) return Q1N1_LIVE_EFAULT;
        if (value != Q1N1_IPCC_SOCCP_GLINK) sim.bad_doorbells++;
        /* The SoCCP may take the interrupt before a store the CPU has not
         * ordered: an index word that changed since the last barrier is one
         * it might not see (glink-smem.c ring()). */
        if (*word(1) != sim.fenced[0] || *word(2) != sim.fenced[1]) sim.unordered_doorbells++, unordered_total++;
        sim.doorbells++;
        remote_step();
        return 0;
    }
    sim.writes++;
    if (sim.fault_writes) return Q1N1_LIVE_EFAULT;
    if (phys >= sim.smem_phys && phys + 4 <= sim.smem_phys + sim.smem_size && !(phys & 3)) {
        memcpy(sim.smem + (phys - sim.smem_phys), &value, 4);
        return 0;
    }
    return Q1N1_LIVE_EFAULT;
}

static volatile uint8_t *sim_map(void *ctx, uint64_t phys, uint64_t size)
{
    (void)ctx;
    if (phys != sim.smem_phys || size != sim.smem_size) return NULL;
    return sim.smem;
}

static int sim_attr(void *ctx, uint64_t phys, uint32_t *attr)
{
    (void)ctx;
    if (sim.attr_fail) return -2;
    if (phys == Q1N1_IPCC_FRAME) {
        if (sim.ipcc_attr_fail) return -2;
        *attr = sim.ipcc_attr;
        return 0;
    }
    uint64_t offset = phys - sim.smem_phys;
    *attr = sim.attr_page >= 0 && (offset & ~0xfffull) == (uint64_t)sim.attr_page ? sim.attr_page_value : sim.attr;
    return 0;
}

static int sim_in_map(void *ctx, uint64_t phys, uint64_t size)
{
    (void)ctx;
    return (phys >= TARGET_PHYS && phys + size <= TARGET_PHYS + 0x1000) ||
           (phys >= sim.smem_phys && phys + size <= sim.smem_phys + sim.smem_size);
}

static void sim_barrier(int orders_stores)
{
    if (!orders_stores || !sim.smem || !sim.desc) return;
    sim.fenced[0] = *word(1);
    sim.fenced[1] = *word(2);
}

static uint64_t sim_now(void *ctx) { (void)ctx; return sim.now; }
static void sim_relax(void *ctx) { (void)ctx; sim.now += 10; remote_step(); }

static const struct q1n1_live_platform pf = {
    .read = sim_read, .write32 = sim_write32, .map = sim_map, .attr = sim_attr,
    .in_memory_map = sim_in_map, .now_us = sim_now, .relax = sim_relax,
};

/* A fresh stage: no session, no router state, no record. */
static void new_stage(void)
{
    memset(&glink, 0, sizeof(glink));
    memset(&router, 0, sizeof(router));
    busy = refresh = resume_tried = adopt_refused = 0;
    last_refresh_us = last_service_us = 0;
    result.calls = result.successes = 0;
}

static int call(uint64_t flags, uint64_t timeout_ms)
{
    return q1n1_live_battery_run(&pf, flags, timeout_ms, 0, 0, 0);
}

/* The status tick, the way main.c calls it, 20 ms of counter apart. */
static void ticks(unsigned count)
{
    for (unsigned i = 0; i < count; i++) {
        sim.now += SERVICE_US;
        q1n1_live_battery_tick(&pf);
    }
}

/* ------------------------------------------------------------------ */
/* Tests                                                               */
/* ------------------------------------------------------------------ */

static void test_smem_parser(void)
{
    scenario = "smem parser";
    struct layout l = {0};
    build(&l);
    struct q1n1_smem s;
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == 0);
    CHECK(s.version == 11 && s.item_count == 1024 && s.ptable_entries == 3);
    struct q1n1_smem_partition p;
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == 0 && p.offset == PART_OFFSET && p.host1 == 19);
    CHECK(q1n1_smem_find_partition(&s, 12, &p) == Q1N1_SMEM_ENOENT);
    CHECK(q1n1_smem_find_partition(&s, 0, &p) == Q1N1_SMEM_EINVAL);
    CHECK(q1n1_smem_find_entry(&s, 19, &p) == 0 && p.index == 0 && p.free_uncached == 0);   /* table only */
    /* The ADSP<->SoCCP entry is listed, but its header is never opened. */
    CHECK(q1n1_smem_table_entry(&s, 2, &p) == 0 && p.host0 == 2 && p.host1 == 19 && p.offset == FOREIGN_OFFSET);
    CHECK(q1n1_smem_partition_at(&s, 2, &p) == Q1N1_SMEM_EINVAL);
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == 0);
    struct q1n1_smem_item it;
    CHECK(q1n1_smem_get(&s, &p, 478, &it) == 0 && it.offset == sim.desc && it.size == 32 && !it.cached);
    CHECK(q1n1_smem_get(&s, &p, 479, &it) == 0 && it.offset == sim.fifo0 && it.size == 0x4000 && it.cached);
    CHECK(q1n1_smem_get(&s, &p, 480, &it) == 0 && it.offset == sim.fifo1 && it.size == 0x2000);
    CHECK(q1n1_smem_get(&s, &p, 481, &it) == Q1N1_SMEM_ENOENT);
    CHECK(q1n1_smem_get(&s, &p, 5000, &it) == Q1N1_SMEM_EINVAL);

    /* Version 12 with a global partition; no SIII means 512 items. */
    l.version = 12; l.no_siii = 1;
    build(&l);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == 0 && s.version == 12 && s.item_count == 512);
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == 0);
    CHECK(q1n1_smem_get(&s, &p, 617, &it) == Q1N1_SMEM_EINVAL);   /* beyond the item count */
    CHECK(q1n1_smem_get(&s, &p, 479, &it) == 0);
    CHECK(q1n1_smem_partition_at(&s, 3, &p) == Q1N1_SMEM_EINVAL); /* global: not needed, not opened */

    /* Header refusals. */
    l = (struct layout){0};
    build(&l);
    wr32(sim.smem + 0xc0, 0);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ENOTREADY);
    build(&l);
    wr32(sim.smem + 0x5c, 13u << 16);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ENOTREADY);
    build(&l);
    wr32(sim.smem + 0xcc, 1);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ENOTREADY);
    build(&l);
    sim.smem[SMEM_SIZE - 0x1000] = 'X';
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ENOENT);
    build(&l);
    wr32(sim.smem + SMEM_SIZE - 0x1000 + 4, 2);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ECORRUPT);
    build(&l);
    wr32(sim.smem + SMEM_SIZE - 0x1000 + 8, 1000);
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_ECORRUPT);
    CHECK(q1n1_smem_init(&s, sim.smem + 8, SMEM_PHYS, SMEM_SIZE) == Q1N1_SMEM_EINVAL);

    /* Partition refusals. */
    uint8_t *entry = sim.smem + SMEM_SIZE - 0x1000 + 32;
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    wr16(sim.smem + PART_OFFSET + 6, 18);                       /* header disagrees with the TOC */
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    wr32(entry + 4, SMEM_SIZE);                                 /* runs past the TOC */
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    wr32(entry, PART_OFFSET + 8);                               /* not page aligned */
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    wr32(sim.smem + PART_OFFSET + 16, 0x20);                    /* cached heap below the uncached one */
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    wr32(entry + 48 * 2 + 4, SMEM_SIZE);                        /* a corrupt foreign entry is skipped */
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == 0);
    l.duplicate = 1;
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE);
    CHECK(q1n1_smem_find_partition(&s, 19, &p) == Q1N1_SMEM_ECORRUPT);
    l.duplicate = 0;

    /* Item refusals: canary, a size that runs off the heap, a cached entry
     * that claims more than there is, and a zero cacheline. */
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE); q1n1_smem_find_partition(&s, 19, &p);
    wr16(sim.smem + sim.desc - 16, 0x5a5a);
    CHECK(q1n1_smem_get(&s, &p, 480, &it) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE); q1n1_smem_find_partition(&s, 19, &p);
    wr32(sim.smem + sim.desc - 16 + 4, 0x7fffffff);
    CHECK(q1n1_smem_get(&s, &p, 480, &it) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE); q1n1_smem_find_partition(&s, 19, &p);
    wr32(sim.smem + sim.fifo0 + sim.fifo0_len + 4, 0x7fff0000);
    CHECK(q1n1_smem_get(&s, &p, 479, &it) == Q1N1_SMEM_ECORRUPT);
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE); q1n1_smem_find_partition(&s, 19, &p);
    p.cacheline = 0;
    CHECK(q1n1_smem_get(&s, &p, 479, &it) == Q1N1_SMEM_ENOENT);
    p.cacheline = 48;
    CHECK(q1n1_smem_get(&s, &p, 479, &it) == Q1N1_SMEM_ENOENT);
    /* A corrupt padding field cannot push the walk out of the partition. */
    build(&l); q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE); q1n1_smem_find_partition(&s, 19, &p);
    wr16(sim.smem + sim.desc - 16 + 10, 0xfff8);
    CHECK(q1n1_smem_get(&s, &p, 480, &it) == Q1N1_SMEM_ECORRUPT);

    /* Target info. */
    uint8_t raw[24] = {0};
    struct q1n1_smem_target t;
    wr32(raw, 0x49494953); wr32(raw + 4, 0x200000); wr32(raw + 8, 0xffe00000); wr16(raw + 16, 1024);
    CHECK(q1n1_smem_parse_target(raw, &t) == 0 && t.base == 0xffe00000 && t.size == 0x200000 && t.max_items == 1024);
    wr32(raw, 0x49494954);
    CHECK(q1n1_smem_parse_target(raw, &t) == Q1N1_SMEM_ENOENT);
    wr32(raw, 0x49494953); wr32(raw + 8, 0xffe00800);
    CHECK(q1n1_smem_parse_target(raw, &t) == Q1N1_SMEM_ECORRUPT);
    wr32(raw + 8, 0xfff00000); wr32(raw + 4, 0x200000);
    CHECK(q1n1_smem_parse_target(raw, &t) == Q1N1_SMEM_ECORRUPT);   /* crosses 4 GiB */
}

static int table_read(void *ctx, uint64_t phys, uint64_t *value)
{
    uint64_t *tables = ctx;
    if (phys < 0x1000 || phys >= 0x5000 || (phys & 7)) return -1;
    *value = tables[(phys - 0x1000) / 8];
    return 0;
}

static void test_mmu_walk(void)
{
    scenario = "mmu walk";
    static uint64_t tables[4 * 512];
    memset(tables, 0, sizeof(tables));
    /* 48-bit VA, 4 KiB: L0 @0x1000 -> L1 @0x2000 -> L2 @0x3000 -> L3 @0x4000. */
    uint64_t va = 0xffe01000ull;
    tables[0 + ((va >> 39) & 511)] = 0x2000 | 3;
    tables[512 + ((va >> 30) & 511)] = 0x3000 | 3;
    tables[1024 + ((va >> 21) & 511)] = 0x4000 | 3;
    tables[1536 + ((va >> 12) & 511)] = 0xffe01000ull | (2u << 2) | 3;         /* AttrIndx 2 */
    tables[1024 + ((0xffc00000ull >> 21) & 511)] = 0xffc00000ull | (1u << 2) | 1; /* 2 MiB block, AttrIndx 1 */
    uint64_t mair = 0x0000000000ff4400ull | (0x04ull << 24);
    uint64_t tcr = 16;                                                        /* T0SZ 16, TG0 4K */
    uint32_t attr = 0;
    CHECK(q1n1_mmu_attr(0x1000, tcr, mair, va, table_read, tables, &attr) == 0 && attr == 0xff);
    CHECK(!q1n1_mmu_attr_uncached(attr));
    CHECK(q1n1_mmu_attr(0x1000, tcr, mair, 0xffc12340ull, table_read, tables, &attr) == 0 && attr == 0x44);
    CHECK(q1n1_mmu_attr_uncached(attr) && !q1n1_mmu_attr_device(attr));
    CHECK(q1n1_mmu_attr(0x1000, tcr, mair, 0xffe02000ull, table_read, tables, &attr) == -3);  /* unmapped page */
    CHECK(q1n1_mmu_attr(0x1000, tcr | (1u << 14), mair, va, table_read, tables, &attr) == -1); /* 64K granule */
    CHECK(q1n1_mmu_attr(0x1000, 8, mair, va, table_read, tables, &attr) == -1);            /* T0SZ < 16 */
    CHECK(q1n1_mmu_attr(0x9000, tcr, mair, va, table_read, tables, &attr) == -2);          /* table faults */
    CHECK(q1n1_mmu_attr_uncached(0x00) && q1n1_mmu_attr_uncached(0x04) && q1n1_mmu_attr_uncached(0x0c));
    CHECK(q1n1_mmu_attr_device(0x00) && q1n1_mmu_attr_device(0x04) && q1n1_mmu_attr_device(0x0c));
    CHECK(!q1n1_mmu_attr_uncached(0xbb) && !q1n1_mmu_attr_uncached(0x4f) && !q1n1_mmu_attr_device(0xff));
    /* 39-bit VA starts at level 1: L1 @0x2000 directly. */
    CHECK(q1n1_mmu_attr(0x2000, 25, mair, va, table_read, tables, &attr) == 0 && attr == 0xff);
}

static void happy_checks(uint32_t expect_restarts)
{
    CHECK(result.status == 0);
    CHECK(result.plausible == 1 && result.state == 2 && result.remaining == 51230 && result.full == 68000);
    CHECK(result.voltage == 17120 && result.cycles == 14 && result.ac == 1 && result.percent == 75);
    CHECK(strstr(result.text, "75% charging on AC") != NULL);
    CHECK(result.restarts == expect_restarts);
    CHECK(sim.r.violations == 0 && sim.bad_doorbells == 0 && sim.unordered_doorbells == 0);
    CHECK(result.doorbell_address == 0x03e0400c && result.doorbell_value == 0x002e0000);
    CHECK(result.smem_base == SMEM_PHYS && result.remote_host == 19 && result.partition_offset == PART_OFFSET);
}

static void test_happy_path(void)
{
    scenario = "happy path";
    struct layout l = {0};
    build(&l);
    new_stage();
    uint8_t *before = malloc(SMEM_SIZE);
    memcpy(before, sim.smem, SMEM_SIZE);

    /* Survey first: reports everything, writes nothing, rings nothing. */
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0);
    CHECK(memcmp(before, sim.smem, SMEM_SIZE) == 0 && sim.doorbells == 0 && sim.writes == 0);
    CHECK(result.target_status == 0 && result.target_pointer == TARGET_PHYS && result.target_base == SMEM_PHYS);
    CHECK(result.smem_version == 11 && result.partitions == 3 && result.partition[0][0] == (19u << 16));
    CHECK(result.partition[2][0] == (2u | 19u << 16) && result.partition[2][1] == FOREIGN_OFFSET);
    CHECK(result.smp2p_status == 0 && result.smp2p_value == 2 && result.ipcc_version == 0x20000);
    CHECK(result.sample_status == 0 && result.smem_attr == 0x44 && result.ipcc_attr == 0x04);
    CHECK(result.desc_attr == 0x44 && result.tx_attr == 0x44 && result.rx_attr == 0x44);
    CHECK(strcmp(result.text, "survey complete") == 0);
    free(before);

    /* All four words 0: the one state q1n1 restarts from by itself. */
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_LINUX);
    CHECK(sim.r.first_frame_cmd == 0 && sim.r.first_frame_offset == 0);   /* VERSION at FIFO offset 0 */
    CHECK(sim.r.versions == 1 && sim.r.requests == 6);                    /* 3 registers, twice */
    CHECK(result.foreign_opens == 1);                                     /* PMIC_LOGS ignored */
    CHECK(result.lcid == 1 && result.rcid == REMOTE_LCID && result.features == 1);
    CHECK(result.reply_owner == 0x8011 && result.reply_opcode == 0x90 && result.reply_length == 1040);
    CHECK(result.elapsed_us < 1500000);
    CHECK(result.tx_cached == 1 && result.rx_cached == 0);   /* 479 found on the cached list */
    CHECK(result.desc_end1 == glink.s.tx_head && result.desc_end2 == glink.s.rx_tail);

    /* The next call continues the session: no VERSION, no OPEN. */
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.adoptions == 1 && sim.r.versions == 1 && sim.r.requests == 12);

    /* UCSI VERSION as a known answer. */
    CHECK(call(Q1N1_LIVE_UCSI_VERSION, 0) == 0 && result.ucsi_version == 0x0210);
    CHECK(sim.r.last_reg == 0x20100 && sim.r.last_len == 4);

    /* A chainload: the new stage copies the old session and continues it. */
    scenario = "chainload adoption";
    struct q1n1_glink_session saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    CHECK(glink.s.magic == Q1N1_GLINK_SESSION_MAGIC && glink.s.state == Q1N1_GLINK_STARTED);
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(sim.r.versions == 1 && result.adoptions == 3);
    /* A session from a differently built stage is ignored; our words are not
     * all 0 any more, so nothing restarts without an explicit mode. */
    new_stage();
    saved.size -= 8;
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    CHECK(glink.s.magic == 0 && adopt_refused == 1);
    uint32_t writes = sim.writes, doorbells = sim.doorbells;
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE && result.adopt_refused == 1 && result.phase == Q1N1_LIVE_PHASE_SESSION);
    CHECK(sim.writes == writes && sim.doorbells == doorbells && sim.r.versions == 1);
    CHECK(call(Q1N1_LIVE_RESTART, 0) == Q1N1_LIVE_ENEEDMODE && sim.writes == writes);
    CHECK(call(Q1N1_LIVE_RESYNC, 0) == 0 && sim.r.versions == 2 && result.glink_mode == Q1N1_GLINK_RESTART_RESYNC);

    /* Our index words changed behind our back (another agent took the
     * edge): the session is not continued, and not restarted by itself. */
    scenario = "foreign writer";
    uint32_t before_versions = sim.r.versions;
    *word(1) = (*word(1) + 8) % sim.fifo0_len;
    *word(0) = *word(1);
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE && sim.r.versions == before_versions);
    CHECK(call(Q1N1_LIVE_RESYNC, 0) == 0 && sim.r.versions == before_versions + 1);
}

static void test_wrap_around(void)
{
    scenario = "wrap-around";
    struct layout l = {.fifo0 = 256, .fifo1 = 512, .fifo0_uncached = 1};
    build(&l);
    sim.r.chunk = 200;          /* 1040-byte replies arrive as TX_DATA + 5 x TX_DATA_CONT */
    new_stage();
    for (int i = 0; i < 40; i++) {
        CHECK(call(0, 0) == 0);
        happy_checks(1);
    }
    CHECK(sim.r.requests == 240 && result.messages >= 240);
    CHECK(result.tx_frames > 250 && result.rx_frames > 1000);   /* both FIFOs wrapped many times */
    CHECK(glink.s.tx_head < 256 && glink.s.rx_tail < 512);
    /* The FIFO has wrapped many times, yet after each call it is zeros: a
     * Linux-style restart (our head to 0 under the remote's non-zero tail)
     * makes the remote read VERSION 0 no-ops, not our old requests. */
    for (uint32_t i = 0; i < sim.fifo0_len; i++) CHECK(sim.smem[sim.fifo0 + i] == 0);
    CHECK(*word(0) == *word(1) && *word(0) != 0 && glink.s.scrubbed >= 40);
    uint32_t requests = sim.r.requests;
    /* The SoCCP's tail is not 0: LINUX is refused unless forced. */
    CHECK(call(Q1N1_LIVE_RESTART | Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EWORDS && sim.r.requests == requests);
    CHECK(call(Q1N1_LIVE_RESTART | Q1N1_LIVE_LINUX | Q1N1_LIVE_FORCE, 0) == 0);
    happy_checks(2);
    CHECK(sim.r.requests == requests + 6 && result.glink_mode == Q1N1_GLINK_RESTART_LINUX);
}

static void test_restart_modes(void)
{
    /* The firmware left the link idle with non-zero indices. RESYNC keeps
     * the remote's view consistent without its cooperation. */
    scenario = "resync mode";
    struct layout l = {.w = {0x1a8, 0x1a8, 0x40, 0x40}};
    build(&l);
    new_stage();
    CHECK(call(Q1N1_LIVE_RESYNC, 0) == 0);
    happy_checks(1);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_RESYNC && sim.r.first_frame_offset == 0x1a8);

    /* Not all four words 0: no restart without a mode, whatever the flags. */
    scenario = "zero-index gate";
    build(&l);
    new_stage();
    uint8_t *before = malloc(SMEM_SIZE);
    memcpy(before, sim.smem, SMEM_SIZE);
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE && result.phase == Q1N1_LIVE_PHASE_SESSION);
    CHECK(call(Q1N1_LIVE_RESTART, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(call(Q1N1_LIVE_UCSI_VERSION, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(memcmp(before, sim.smem, SMEM_SIZE) == 0 && sim.writes == 0 && sim.doorbells == 0);
    CHECK(strstr(result.text, "explicit mode") != NULL);
    /* One non-zero word is enough to refuse. With word 0 or 1 set the
     * simulated SoCCP sees pending bytes and reads them while we sample:
     * moving words refuse a restart too (EBUSY), and the gate is never
     * reached from a state that only became zero during this call. */
    for (unsigned w = 0; w < 4; w++) {
        struct layout one = {0};
        one.w[w] = 0x40;
        build(&one);
        new_stage();
        CHECK(call(0, 0) == (w < 2 ? Q1N1_GLINK_EBUSY : Q1N1_LIVE_ENEEDMODE) && sim.writes == 0 && sim.doorbells == 0);
    }
    /* Two modes at once are refused before anything is read. */
    build(&l);
    new_stage();
    uint32_t reads = sim.reads;
    CHECK(call(Q1N1_LIVE_LINUX | Q1N1_LIVE_RESYNC, 0) == Q1N1_LIVE_EFLAGS && sim.reads == reads);

    /* An explicit LINUX over idle firmware indices: the remote reads its
     * stale tail to the end of our FIFO first; a FIFO the firmware never
     * wrapped is zeros there, VERSION 0 frames it ignores. */
    scenario = "explicit linux over idle firmware indices";
    /* Words 0 and 3 are the SoCCP's and not 0: it never cleaned up. LINUX
     * and DRAIN are refused before any write unless forced; RESYNC is not
     * affected (it never moves our head under the SoCCP's tail). */
    CHECK(call(Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EWORDS && result.phase == Q1N1_LIVE_PHASE_SESSION);
    CHECK(call(Q1N1_LIVE_DRAIN, 0) == Q1N1_LIVE_EWORDS && strstr(result.text, "force") != NULL);
    CHECK(call(Q1N1_LIVE_RESTART | Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EWORDS);
    CHECK(memcmp(before, sim.smem, SMEM_SIZE) == 0 && sim.writes == 0 && sim.doorbells == 0);
    /* FORCE means nothing without LINUX or DRAIN. */
    CHECK(call(Q1N1_LIVE_FORCE, 0) == Q1N1_LIVE_EFLAGS && call(Q1N1_LIVE_FORCE | Q1N1_LIVE_RESYNC, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(call(Q1N1_LIVE_FORCE | Q1N1_LIVE_RELEASE, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(sim.writes == 0 && sim.doorbells == 0);
    CHECK(call(Q1N1_LIVE_LINUX | Q1N1_LIVE_FORCE, 0) == 0);
    happy_checks(1);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_LINUX && result.desc1 == 0x1a8 && glink.s.start_words[0] == 0x1a8);
    /* A restart of our own session also needs a mode now. */
    CHECK(call(Q1N1_LIVE_RESTART, 0) == Q1N1_LIVE_ENEEDMODE && result.restarts == 1);
    CHECK(call(Q1N1_LIVE_RESTART | Q1N1_LIVE_RESYNC, 0) == 0);
    happy_checks(2);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_RESYNC && sim.r.versions == 2);

    scenario = "drain mode";
    l = (struct layout){.w = {0, 0, 0x40, 0x88}};      /* unread history the remote left for UEFI */
    build(&l);
    memset(sim.smem + sim.fifo1 + 0x40, 0xee, 0x48);   /* garbage: parsing it would be fatal */
    new_stage();
    CHECK(call(Q1N1_LIVE_DRAIN, 0) == Q1N1_LIVE_EWORDS && sim.writes == 0);   /* word 3 is not 0 */
    CHECK(call(Q1N1_LIVE_DRAIN | Q1N1_LIVE_FORCE, 0) == 0);
    happy_checks(1);

    scenario = "linux mode parses history";
    build(&l);
    memset(sim.smem + sim.fifo1, 0xee, 0x88);
    new_stage();
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(call(Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EWORDS);
    CHECK(call(Q1N1_LIVE_LINUX | Q1N1_LIVE_FORCE, 0) == Q1N1_GLINK_EPROTO);
    CHECK(result.phase == Q1N1_LIVE_PHASE_OPEN && result.glink_state == Q1N1_GLINK_FAILED);
    /* The failed session is not continued, and not restarted by itself. */
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(call(Q1N1_LIVE_RESYNC, 0) == 0);
    happy_checks(2);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_RESYNC);
    free(before);

    /* A downstream-style core answers each VERSION 0 it reads (the zeros a
     * LINUX restart makes it walk over) with VERSION_ACK 0. Linux ignores
     * those; so must we. */
    scenario = "version-ack 0 ignored";
    l = (struct layout){.fifo0 = 0x400, .fifo0_uncached = 1, .w = {0x1a8, 0x1a8, 0x40, 0x40}};
    build(&l);
    sim.r.ack_zero_version = 1;
    new_stage();
    CHECK(call(Q1N1_LIVE_LINUX | Q1N1_LIVE_FORCE, 0) == 0);
    happy_checks(1);
    CHECK(sim.r.zero_versions == (0x400 - 0x1a8) / 8 && result.version_zero_acks == sim.r.zero_versions);
    CHECK(result.version_acked == 1);
}

static void test_timeouts_and_stale(void)
{
    scenario = "silent remote";
    struct layout l = {0};
    build(&l);
    sim.r.enabled = 0;
    new_stage();
    uint64_t start = sim.now;
    CHECK(call(0, 300) == Q1N1_GLINK_ETIMEDOUT);
    CHECK(result.phase == Q1N1_LIVE_PHASE_OPEN && sim.now - start >= 300000 && sim.now - start < 400000);
    CHECK(result.open_sent == 1 && result.version_acked == 0);   /* OPEN sent on the fallback timer */
    /* Nothing was stuck: once the remote wakes up, the same session opens. */
    sim.r.enabled = 1;
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    scenario = "ignored version";
    build(&l);
    sim.r.ignore_version = 1;
    sim.r.remote_opens = 0;
    new_stage();
    CHECK(call(0, 0) == 0);                   /* an already-negotiated remote: OPEN still goes out */
    happy_checks(1);
    CHECK(result.version_acked == 0);

    scenario = "late reply";
    build(&l);
    sim.r.delay_first_us = 250000;
    new_stage();
    CHECK(call(0, 150) == Q1N1_PMIC_ETIMEDOUT && result.phase == Q1N1_LIVE_PHASE_READ);
    CHECK(router.orphans == 1 && glink.s.client_carry[0] == 1);
    sim.now += 200000;                        /* the late reply arrives in the FIFO */
    remote_step();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.pmic_stale == 1 && router.orphans == 0);

    /* The same, but a chainload happens between the timeout and the late
     * reply: the owed reply crosses in the session. */
    scenario = "late reply across a chainload";
    build(&l);
    sim.r.delay_first_us = 250000;
    new_stage();
    CHECK(call(0, 150) == Q1N1_PMIC_ETIMEDOUT && router.orphans == 1);
    struct q1n1_glink_session saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    sim.now += 200000;
    remote_step();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.pmic_stale == 1 && result.adoptions == 1 && router.orphans == 0);

    scenario = "lost reply";
    build(&l);
    sim.r.drop_replies = 1;
    new_stage();
    CHECK(call(0, 100) == Q1N1_PMIC_ETIMEDOUT && router.orphans == 1);
    sim.now += 2100000;                       /* past the orphan give-up */
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    scenario = "remote stops reading";
    l = (struct layout){.fifo0 = 64, .fifo0_uncached = 1};
    build(&l);
    sim.r.stall_after_open = 1;
    new_stage();
    start = sim.now;
    CHECK(call(0, 300) == Q1N1_GLINK_ETIMEDOUT && result.phase == Q1N1_LIVE_PHASE_OPEN);
    CHECK(sim.now - start >= 300000 && sim.now - start < 400000);   /* bounded by the call timeout */
    l = (struct layout){0};

    scenario = "unsolicited reply";
    build(&l);
    sim.r.unsolicited_before = 1;
    new_stage();
    /* A stray 0x8011 reply lands while the first BST sample waits: it carries
     * no register id, so it is taken, and the second sample disagrees. */
    CHECK(call(0, 0) == Q1N1_PMIC_EUNSTABLE && result.pmic_unsolicited == 1);
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    /* A stray reply that arrives while our request is still looking for a
     * remote intent answers something older: it must not be taken. */
    scenario = "reply before the request is sent";
    build(&l);
    sim.r.no_intent = 1;
    sim.r.stray_on_intent_req = 1;
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.pmic_unsolicited == 1 && result.pmic_replies == 6);
}

static void test_bad_replies(void)
{
    struct layout l = {0};
    scenario = "short reply";
    build(&l); sim.r.reply_length = 1036; new_stage();
    CHECK(call(0, 0) == Q1N1_PMIC_EREPLY && result.reply_length == 1036);
    scenario = "wrong opcode";
    build(&l); sim.r.reply_opcode = 0x91; new_stage();
    CHECK(call(0, 0) == Q1N1_PMIC_EREPLY && result.reply_opcode == 0x91);
    scenario = "foreign owner";
    build(&l); sim.r.reply_owner = 0x800a; new_stage();
    CHECK(call(0, 200) == Q1N1_PMIC_ETIMEDOUT && result.pmic_foreign >= 1);
    scenario = "remote error";
    build(&l); sim.r.remote_error = 5; new_stage();
    CHECK(call(0, 0) == Q1N1_PMIC_EREMOTE && result.reply_error == 5);
    scenario = "flapping";
    build(&l); sim.r.flap = 1; new_stage();
    CHECK(call(0, 0) == Q1N1_PMIC_EUNSTABLE);
    scenario = "implausible";
    build(&l); sim.r.implausible = 1; new_stage();
    CHECK(call(0, 0) == Q1N1_PMIC_EIMPLAUSIBLE && result.voltage == 1 && result.plausible == 0);
    CHECK(strstr(result.text, "implausible") != NULL);
    scenario = "unknown command";
    build(&l); sim.r.bad_command_after_open = 1; new_stage();
    CHECK(call(0, 0) == Q1N1_GLINK_EPROTO && result.glink_command == 10 && result.protocol_errors == 1);
}

/* A TX_DATA straight into the RX FIFO, as a remote that ignores intent sizes would. */
static void inject_data(uint32_t lcid, uint32_t iid, uint32_t length)
{
    static uint8_t message[4096];
    memset(message, 0x5a, sizeof(message));
    uint8_t f[16 + 4096];
    uint32_t chunk = length < 1024 ? length : 1024, done = 0;
    while (done < length) {
        uint32_t n = length - done < chunk ? length - done : chunk;
        wr16(f, done ? 12 : 9); wr16(f + 2, lcid); wr32(f + 4, iid);
        wr32(f + 8, n); wr32(f + 12, length - done - n);
        memcpy(f + 16, message + done, n);
        queue(f, 16 + n);
        done += n;
    }
    remote_flush();
}

static void test_protocol_variants(void)
{
    struct layout l = {0};
    scenario = "no remote intent";
    build(&l); sim.r.no_intent = 1; new_stage();
    CHECK(call(0, 0) == 0);                   /* RX_INTENT_REQ -> INTENT + ACK */
    happy_checks(1);

    scenario = "remote wants a big intent";
    build(&l); sim.r.want_intent = 4096; new_stage();
    CHECK(call(0, 0) == 0);                   /* declined (ACK 0), then business as usual */
    happy_checks(1);

    scenario = "no reuse";
    build(&l); sim.r.features = 0; sim.r.reuse = 0; new_stage();
    for (int i = 0; i < 5; i++) CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.features == 0 && glink.s.next_liid > 20);   /* consumed intents replaced */

    /* The remote closes: we acknowledge, close our side too, and reopen
     * only after that close is acknowledged (the sim flags an OPEN that
     * arrives while our side is still open). */
    scenario = "remote closes";
    build(&l); sim.r.close_after_reply = 1; new_stage();
    CHECK(call(Q1N1_LIVE_UCSI_VERSION, 0) == Q1N1_GLINK_ECLOSED);
    CHECK(result.remote_closes == 1 && sim.r.apps_closes == 1);
    CHECK(call(Q1N1_LIVE_UCSI_VERSION, 0) == Q1N1_GLINK_ECLOSED);
    sim.r.close_after_reply = 0;
    CHECK(call(0, 0) == 0);                   /* reopened by our OPEN, same link */
    happy_checks(1);
    CHECK(sim.r.apps_closes == 2 && sim.r.apps_opens == 3 && result.remote_closes == 2);

    /* The remote opens the channel again under the same id, without a
     * CLOSE: the intent it offered before is void and must not be used. */
    scenario = "reopen under the same id";
    build(&l); sim.r.reopen_once = 1; new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(sim.r.my_iid == 0x40 && result.remote_intents == 1);

    scenario = "remote sends version too";
    build(&l); sim.r.send_version = 1; new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.remote_version == 1);

    /* A copied (or corrupted) session that claims a local intent larger than
     * this build's 2048-byte buffer: the payload is dropped, not copied past
     * the buffer (ASan would see it). */
    scenario = "intent size above the buffer";
    build(&l); new_stage();
    CHECK(call(0, 0) == 0);
    struct q1n1_glink_intent *mine = &glink.s.ch[Q1N1_GLINK_CH_CLIENT].local[0];
    mine->size = 100000;
    uint64_t dropped = glink.s.dropped;
    inject_data(REMOTE_LCID, mine->id, 3000);
    CHECK(q1n1_glink_poll(&glink) >= 0 && glink.s.dropped == dropped + 1);
    /* ... and a stage refuses to adopt such a session at all. */
    struct q1n1_glink_session saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE && glink.s.state == Q1N1_GLINK_DOWN);
}

static void test_refusals(void)
{
    struct layout l = {0};
    uint8_t *before = malloc(SMEM_SIZE);
#define UNTOUCHED() (memcmp(before, sim.smem, SMEM_SIZE) == 0 && sim.doorbells == 0 && sim.writes == 0)
#define FRESH() do { new_stage(); memcpy(before, sim.smem, SMEM_SIZE); } while (0)

    scenario = "target info faults";
    build(&l); sim.fault_target = 1; FRESH();
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && result.target_pointer == ~0ull && result.smem_base == SMEM_PHYS);
    scenario = "target info outside the memory map";
    build(&l); sim.target_pointer = 0x500000; FRESH();
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && result.target_status == (uint64_t)(int64_t)Q1N1_SMEM_ENOENT);
    scenario = "target info names another base";
    build(&l); wr32(sim.target + 8, 0x80000000); FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.smem_base == 0x80000000 && UNTOUCHED());
    scenario = "explicit base";
    CHECK(q1n1_live_battery_run(&pf, 0, 0, 0, SMEM_PHYS, SMEM_SIZE) == 0);
    scenario = "header page faults";
    build(&l); sim.fault_page_offset = 0; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.phase == Q1N1_LIVE_PHASE_SMEM && UNTOUCHED());
    scenario = "table page faults";
    build(&l); sim.fault_page_offset = SMEM_SIZE - 0x1000; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.phase == Q1N1_LIVE_PHASE_SMEM && UNTOUCHED());
    scenario = "partition page faults";
    build(&l); sim.fault_page_offset = PART_OFFSET + 0x7000; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.phase == Q1N1_LIVE_PHASE_PARTITION && UNTOUCHED());
    CHECK(result.partition_offset == PART_OFFSET && result.partitions == 3);
    /* A page nobody needs: no longer probed, so no longer a refusal. */
    scenario = "unused page faults";
    build(&l); sim.fault_page_offset = 0x7000; FRESH();
    CHECK(call(0, 0) == 0);
    scenario = "SMEM not initialised";
    build(&l); wr32(sim.smem + 0xc0, 0); FRESH();
    CHECK(call(0, 0) == Q1N1_SMEM_ENOTREADY && UNTOUCHED());
    scenario = "SoCCP under another host id";
    l.host = 12; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_SMEM_ENOENT && result.phase == Q1N1_LIVE_PHASE_PARTITION && UNTOUCHED());
    CHECK(q1n1_live_battery_run(&pf, 0, 0, 12, 0, 0) == 0);
    l.host = 0;
    scenario = "no RX FIFO";
    l.no_rx_fifo = 1; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_SMEM_ENOENT && result.phase == Q1N1_LIVE_PHASE_ITEMS && UNTOUCHED());
    l.no_rx_fifo = 0;
    scenario = "descriptor size";
    l.desc_size = 16; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_GLINK_ECORRUPT && result.phase == Q1N1_LIVE_PHASE_BIND && UNTOUCHED());
    l.desc_size = 0;
    scenario = "SoCCP fatal";
    l.fatal = 1; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EREMOTE && result.smp2p_value == 3 && UNTOUCHED());
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0);
    l.fatal = 0;
    scenario = "SoCCP not ready";
    l.not_ready = 1; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EREMOTE && result.smp2p_value == 0 && result.phase == Q1N1_LIVE_PHASE_SMP2P);
    CHECK(UNTOUCHED() && call(Q1N1_LIVE_SURVEY, 0) == 0);
    l.not_ready = 0;
    scenario = "no SMP2P record";
    l.no_smp2p = 1; build(&l); FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EREMOTE && result.smp2p_status == (uint64_t)(int64_t)Q1N1_SMEM_ENOENT);
    CHECK(UNTOUCHED() && call(Q1N1_LIVE_SURVEY, 0) == 0);
    l.no_smp2p = 0;
    scenario = "IPCC version";
    build(&l); sim.ipcc_version = 0x00010000; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EIPCC && UNTOUCHED());
    scenario = "IPCC unmapped";
    build(&l); sim.fault_ipcc = 1; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.phase == Q1N1_LIVE_PHASE_IPCC && UNTOUCHED());
    scenario = "remote words moving";
    build(&l); sim.r.moving = 1; FRESH();
    CHECK(call(0, 0) == Q1N1_GLINK_EBUSY && result.phase == Q1N1_LIVE_PHASE_SESSION);
    CHECK(*word(1) == 0 && *word(2) == 0 && sim.writes == 0 && sim.doorbells == 0);
    scenario = "index out of range";
    l.w[3] = 0x2004; build(&l); FRESH();
    CHECK(call(Q1N1_LIVE_LINUX, 0) == Q1N1_GLINK_ECORRUPT && result.sample_status == (uint64_t)(int64_t)Q1N1_GLINK_ECORRUPT);
    CHECK(UNTOUCHED());
    l.w[3] = 0;
    scenario = "read-only SMEM";
    build(&l); sim.fault_writes = 1; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EFAULT && result.phase == Q1N1_LIVE_PHASE_SESSION && sim.doorbells == 0);

    /* The doorbell faults on our VERSION: the session fails with our head
     * past that frame, so the words are no longer all 0 and the next call
     * needs a mode. The rest of our FIFO is scrubbed even so. */
    scenario = "doorbell faults";
    build(&l); sim.fault_doorbell = 1; FRESH();
    memset(sim.smem + sim.fifo0 + 0x100, 0xee, 0x40);          /* old frames of an earlier session */
    CHECK(call(0, 0) == Q1N1_GLINK_EDOORBELL && result.phase == Q1N1_LIVE_PHASE_SESSION);
    CHECK(result.glink_state == Q1N1_GLINK_FAILED && *word(1) == 8 && glink.s.scrubbed == 1);
    for (uint32_t i = 0x100; i < 0x140; i++) CHECK(sim.smem[sim.fifo0 + i] == 0);
    sim.fault_doorbell = 0;
    /* The SoCCP finds our VERSION on its own (the sim reads on any step):
     * words moving, then settled but not zero. Neither restarts. */
    CHECK(call(0, 0) == Q1N1_GLINK_EBUSY);
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(call(Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EWORDS);
    CHECK(call(Q1N1_LIVE_LINUX | Q1N1_LIVE_FORCE, 0) == 0);
    scenario = "re-entry";
    build(&l); FRESH();
    busy = 1;
    CHECK(call(0, 0) == Q1N1_LIVE_EBUSY);
    busy = 0;
    scenario = "bad host";
    CHECK(q1n1_live_battery_run(&pf, 0, 0, 0xfffe, 0, 0) == Q1N1_SMEM_EINVAL);
    scenario = "no clock";
    build(&l); FRESH();
    sim.now = 0;
    uint32_t reads = sim.reads;
    CHECK(call(0, 0) == Q1N1_LIVE_ECLOCK && UNTOUCHED() && sim.reads == reads);
    sim.now = 1000000;
    scenario = "conflicting flags";
    CHECK(call(Q1N1_LIVE_RELEASE | Q1N1_LIVE_RESTART, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(call(Q1N1_LIVE_RELEASE | Q1N1_LIVE_LINUX, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(call(Q1N1_LIVE_RELEASE | Q1N1_LIVE_SURVEY, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(call(Q1N1_LIVE_NO_RESTART | Q1N1_LIVE_RESYNC, 0) == Q1N1_LIVE_EFLAGS);
    CHECK(UNTOUCHED() && sim.reads == reads);
    free(before);
}

/* Item 4: every page we touch is checked; a known-cacheable one is refused
 * whatever the flags; the doorbell page must be Device. */
static void test_attributes(void)
{
    struct layout l = {0};
    uint8_t *before = malloc(SMEM_SIZE);

    scenario = "cacheable SMEM";
    build(&l); sim.attr = 0xff; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_ECACHED && UNTOUCHED());
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == Q1N1_LIVE_ECACHED && UNTOUCHED());

    scenario = "cacheable descriptor page";
    build(&l); sim.attr_page = (int64_t)(sim.desc & ~0xfffull); sim.attr_page_value = 0xff; FRESH();
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == Q1N1_LIVE_ECACHED && UNTOUCHED());
    CHECK(result.smem_attr == 0x44 && result.desc_attr == 0xff && result.phase == Q1N1_LIVE_PHASE_MAPPING);
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && result.desc_attr == 0xff);      /* the survey reports it */

    scenario = "cacheable TX FIFO page";
    build(&l); sim.attr_page = (int64_t)((sim.fifo0 + sim.fifo0_len - 1) & ~0xfffull); sim.attr_page_value = 0xbb;
    FRESH();
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == Q1N1_LIVE_ECACHED && UNTOUCHED());
    CHECK(result.smem_attr == 0x44 && result.desc_attr == 0x44 && result.tx_attr == 0xbb);

    scenario = "unreadable tables";
    build(&l); sim.attr_fail = 1; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EATTR && result.smem_attr == ~0ull && result.ipcc_attr == ~0ull && UNTOUCHED());
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == 0);                        /* unknown, and told so */

    scenario = "doorbell mapped Normal non-cacheable";
    build(&l); sim.ipcc_attr = 0x44; FRESH();
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == Q1N1_LIVE_EDEVICE && UNTOUCHED() && result.ipcc_attr == 0x44);
    scenario = "doorbell mapped cacheable";
    build(&l); sim.ipcc_attr = 0xff; FRESH();
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == Q1N1_LIVE_EDEVICE && UNTOUCHED());
    scenario = "doorbell attribute unknown";
    build(&l); sim.ipcc_attr_fail = 1; FRESH();
    CHECK(call(0, 0) == Q1N1_LIVE_EATTR && UNTOUCHED());
    CHECK(call(Q1N1_LIVE_ATTR_UNKNOWN_OK, 0) == 0);
    scenario = "doorbell Device-nGnRE";
    build(&l); sim.ipcc_attr = 0x04; sim.attr = 0x00; FRESH();
    CHECK(call(0, 0) == 0);
    free(before);
}

/* Item 1: nothing outside the header page, the table page and the SoCCP
 * partition is touched -- every other page of SMEM is made inaccessible, so
 * one stray load (the old 512-page sweep, a foreign partition header, the
 * global or ADSP partitions) would crash the harness. */
static void test_foreign_partitions_untouched(void)
{
    scenario = "foreign partitions untouched";
    struct layout l = {.version = 12};
    build(&l);
    new_stage();
    long page = sysconf(_SC_PAGESIZE);
    CHECK(page > 0 && page <= 0x4000 && SMEM_ALIGN % (uint32_t)page == 0);
    const uint32_t keep_low = 0x4000, keep_high = SMEM_SIZE - 0x4000;   /* 16 KiB covers any host page */
    CHECK(mprotect(sim.smem + keep_low, PART_OFFSET - keep_low, PROT_NONE) == 0);
    CHECK(mprotect(sim.smem + PART_OFFSET + PART_SIZE, keep_high - PART_OFFSET - PART_SIZE, PROT_NONE) == 0);
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && result.partitions == 4);
    CHECK(result.partition[3][0] == (0xfffeu | 0xfffeu << 16) && result.partition[2][1] == FOREIGN_OFFSET);
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && glink.s.state == Q1N1_GLINK_RELEASED);
    CHECK(call(0, 0) == 0);
    happy_checks(2);
    struct q1n1_smem s;
    struct q1n1_smem_partition p;
    CHECK(q1n1_smem_init(&s, sim.smem, SMEM_PHYS, SMEM_SIZE) == 0);
    CHECK(q1n1_smem_partition_at(&s, 2, &p) == Q1N1_SMEM_EINVAL);   /* foreign: refused unread */
    CHECK(q1n1_smem_partition_at(&s, 3, &p) == Q1N1_SMEM_EINVAL);   /* global: refused unread */
    CHECK(mprotect(sim.smem, SMEM_SIZE, PROT_READ | PROT_WRITE) == 0);
}

/* Item 2: the firmware's DO_CLEANUP, and the state it leaves. */
static void test_release(void)
{
    struct layout l = {0};

    scenario = "release";
    build(&l);
    sim.r.ssr_delay_us = 5000;               /* the SoCCP resets a little later: cleanup_done arrives first */
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(sim.r.ssr_open_seen == 0 && result.ssr_opened == 0);   /* glink_ssr is not opened for reads */
    uint32_t tail = *word(0);
    CHECK(tail != 0);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0);
    CHECK(sim.r.cleanups == 1 && sim.r.cleanup_matches == 1 && sim.r.violations == 0);   /* the firmware's bytes */
    CHECK(sim.r.resets == 1 && words_zero() && glink.s.state == Q1N1_GLINK_RELEASED);
    CHECK(result.releases == 1 && result.cleanup_sent == 1 && result.cleanup_done == 1 && result.release_status == 0);
    CHECK(result.desc_end0 == 0 && result.desc_end1 == 0 && result.desc_end2 == 0 && result.desc_end3 == 0);
    CHECK(strstr(result.text, "released") != NULL);
    for (uint32_t i = 0; i < sim.fifo0_len; i++) CHECK(sim.smem[sim.fifo0 + i] == 0);   /* nothing to replay */
    /* The OPEN for glink_ssr: name "glink_ssr\0", 10 bytes, lcid 2 (the sim
     * checks the padding and that the id differs from the client's). */
    CHECK(sim.r.ssr_open_seen == 1 && sim.r.ssr_apps_lcid == Q1N1_GLINK_SSR_LCID && sim.r.ssr_acked_by_apps == 1);
    /* Released is the cleaned-up state: the next call restarts by itself,
     * Linux's way, and the SoCCP (back up once all four words are 0) answers. */
    CHECK(call(0, 0) == 0);
    happy_checks(2);
    CHECK(result.glink_mode == Q1N1_GLINK_RESTART_LINUX && sim.r.versions == 2 && sim.r.down == 0);
    /* Releasing again right away, then with nothing up. */
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && sim.r.resets == 2 && words_zero());
    uint32_t doorbells = sim.doorbells;
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && sim.doorbells == doorbells && strstr(result.text, "nothing") != NULL);

    scenario = "release, immediate reset";
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && words_zero() && sim.r.resets == 1 && result.cleanup_done == 0);
    CHECK(sim.r.violations == 0);

    /* The SoCCP opened glink_ssr at link-up: recorded, not acknowledged, until
     * the release wants it (as Linux does for a channel no driver claims). */
    scenario = "release, glink_ssr opened early";
    build(&l);
    sim.r.ssr_open_early = 1;
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.ssr_opened == 1 && sim.r.ssr_acked_by_apps == 0 && result.foreign_opens == 1);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && sim.r.ssr_acked_by_apps == 1 && sim.r.cleanup_matches == 1);
    CHECK(words_zero() && sim.r.violations == 0);

    scenario = "release, no intent on glink_ssr";
    build(&l);
    sim.r.ssr_no_intent = 1;
    new_stage();
    CHECK(call(0, 0) == 0);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && sim.r.cleanup_matches == 1 && words_zero());

    /* The SoCCP never answers our glink_ssr OPEN: nothing was sent but the
     * OPEN, the session stays up and keeps working. */
    scenario = "release, glink_ssr never opens";
    build(&l);
    sim.r.ssr_never = 1;
    new_stage();
    CHECK(call(0, 0) == 0);
    CHECK(call(Q1N1_LIVE_RELEASE, 200) == Q1N1_GLINK_ETIMEDOUT && result.phase == Q1N1_LIVE_PHASE_RELEASE);
    CHECK(glink.s.state == Q1N1_GLINK_STARTED && sim.r.cleanups == 0 && !words_zero());
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    /* The SoCCP reads the message but never resets: our words stay as they
     * were (zeroing them under a live remote tail would make it replay), the
     * session is failed, and nothing restarts without a mode. */
    scenario = "release, SoCCP never resets";
    build(&l);
    sim.r.ignore_cleanup = 1;
    new_stage();
    CHECK(call(0, 0) == 0);
    uint32_t head = *word(1);
    CHECK(call(Q1N1_LIVE_RELEASE, 200) == Q1N1_GLINK_ETIMEDOUT && sim.r.cleanup_matches == 1);
    CHECK(glink.s.state == Q1N1_GLINK_FAILED && *word(1) != 0 && *word(1) >= head && *word(0) == *word(1));
    CHECK(call(0, 0) == Q1N1_LIVE_ENEEDMODE);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == Q1N1_LIVE_ENOSESSION);

    /* The handoff path main.c takes before jumping to a non-q1n1 payload. */
    scenario = "release at handoff";
    build(&l);
    new_stage();
    CHECK(q1n1_live_battery_release(&pf) == 0 && result.calls == 0);    /* nothing up: nothing done */
    CHECK(call(0, 0) == 0);
    CHECK(q1n1_live_battery_release(&pf) == 0 && words_zero() && sim.r.cleanup_matches == 1);
    CHECK(result.calls == 2 && glink.s.state == Q1N1_GLINK_RELEASED);
    /* A chainloaded stage that never made a call still releases the
     * session it adopted. */
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    struct q1n1_glink_session saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    CHECK(q1n1_live_battery_release(&pf) == 0 && words_zero() && sim.r.cleanup_matches == 1);
}

/* Item 3: the tick services the session, so nothing waits on APPS. */
static void test_service(void)
{
    struct layout l = {0};

    scenario = "notifications drained by the tick";
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    /* The SoCCP has 10 notifications for 4 intents. Without a reader it
     * fills them, asks for more once, and waits. */
    sim.r.pending_notify = 10;
    remote_step();
    CHECK(sim.r.notified == 4 && sim.r.notify_reqs == 1 && sim.r.req_acks == 0);
    ticks(1);
    CHECK(result.service_polls == 1 && result.service_frames <= SERVICE_FRAMES);
    ticks(20);
    CHECK(sim.r.pending_notify == 0 && sim.r.notified == 10 && sim.r.req_acks >= 1);
    CHECK(router.foreign == 10 && sim.r.violations == 0 && glink.s.state == Q1N1_GLINK_STARTED);
    CHECK(result.calls == 1);                          /* servicing is not a call */
    /* Reads keep working afterwards. */
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    /* The tick never waits: with the SoCCP no longer reading our FIFO and no
     * room for a reply, frames stay where they are for a later tick. */
    scenario = "tick never blocks";
    l = (struct layout){.fifo0 = 128, .fifo0_uncached = 1};
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    sim.r.stall_after_open = 1;
    sim.r.pending_notify = 12;
    remote_step();
    for (int i = 0; i < 10; i++) {
        uint64_t before = sim.now + SERVICE_US;
        ticks(1);
        CHECK(sim.now == before);                      /* no relax, no wait */
    }
    CHECK(glink.s.state == Q1N1_GLINK_STARTED);
    sim.r.stall_after_open = 0;
    remote_step();
    ticks(40);
    CHECK(sim.r.pending_notify == 0 && sim.r.notified == 12 && glink.s.state == Q1N1_GLINK_STARTED);
    CHECK(sim.r.violations == 0);

    /* A chainloaded stage resumes the adopted session on its first tick,
     * without restarting, and then services it. */
    scenario = "resume after a chainload";
    l = (struct layout){0};
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    struct q1n1_glink_session saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    sim.r.pending_notify = 6;
    remote_step();
    ticks(1);
    CHECK(result.calls == 1 && result.status == 0 && strcmp(result.text, "session resumed") == 0);
    CHECK(result.restarts == 1 && result.adoptions == 1 && sim.r.versions == 1);
    ticks(10);
    CHECK(sim.r.notified == 6 && sim.r.pending_notify == 0 && router.foreign == 6);
    CHECK(call(0, 0) == 0);
    happy_checks(1);

    /* A resume that cannot continue (our words moved) does not restart,
     * and is not retried every tick. */
    scenario = "resume refuses";
    saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(&saved, sizeof(saved));
    *word(1) = (*word(1) + 8) % sim.fifo0_len;
    uint32_t writes = sim.writes, versions = sim.r.versions;
    ticks(5);
    CHECK(result.calls == 1 && result.status == (uint64_t)(int64_t)Q1N1_LIVE_ENORESTART);
    CHECK(sim.writes == writes && sim.r.versions == versions);

    /* Nothing is serviced unless it is STARTED (released, failed, down). */
    scenario = "tick leaves other states alone";
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0 && call(Q1N1_LIVE_RELEASE, 0) == 0);
    uint64_t polls = glink.s.service_polls;
    uint32_t doorbells = sim.doorbells;
    ticks(5);
    CHECK(glink.s.service_polls == polls && sim.doorbells == doorbells);
}

static void test_refresh(void)
{
    scenario = "periodic refresh";
    struct layout l = {0};
    build(&l);
    new_stage();
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 0);                  /* off by default */
    CHECK(call(Q1N1_LIVE_REFRESH_ON, 0) == 0 && result.calls == 1);
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 2 && result.status == 0);
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 2);                  /* not again within 30 s */
    sim.now += 31000000;
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 3 && result.status == 0);

    /* The refresh never restarts: a failed session stays failed. */
    scenario = "refresh never restarts";
    glink.s.state = Q1N1_GLINK_FAILED;
    uint32_t writes = sim.writes, doorbells = sim.doorbells, versions = sim.r.versions;
    sim.now += 31000000;
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 4 && result.status == (uint64_t)(int64_t)Q1N1_LIVE_ENORESTART);
    CHECK(sim.writes == writes && sim.doorbells == doorbells && sim.r.versions == versions);
    /* Not even from the all-zero state. */
    build(&l);
    new_stage();
    refresh = 1;
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 1 && result.status == (uint64_t)(int64_t)Q1N1_LIVE_ENORESTART && sim.doorbells == 0);
    CHECK(call(Q1N1_LIVE_REFRESH_OFF | Q1N1_LIVE_SURVEY, 0) == 0);
    sim.now += 31000000;
    q1n1_live_battery_tick(&pf);
    CHECK(result.calls == 2);
}

/* The direct GLINK API: frames the stage writes are well-formed for any
 * FIFO position, including a header that straddles the end. */
static void test_frame_straddle(void)
{
    scenario = "straddled frames";
    unsigned padded = 0;
    for (uint32_t start = 0; start < 256; start += 8) {
        struct layout l = {.fifo0 = 256, .fifo1 = 256, .fifo0_uncached = 1, .w = {start, start, start, start}};
        build(&l);
        sim.r.chunk = 64;
        new_stage();
        CHECK(call(Q1N1_LIVE_RESYNC | Q1N1_LIVE_UCSI_VERSION, 0) == 0);
        CHECK(result.ucsi_version == 0x0210 && sim.r.violations == 0);
        /* ... and the release, wherever the head is. With the remote's tail
         * at 0 when DO_CLEANUP goes out, a finished cleanup would look like
         * nothing happened, so the release first moves our head off 0 with
         * one VERSION 0 frame. Some start positions put the head there
         * right after glink_ssr opens; the loop must hit that case. */
        uint32_t zero_versions = sim.r.zero_versions;
        CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && words_zero() && sim.r.cleanup_matches == 1);
        CHECK(sim.r.violations == 0 && sim.r.zero_versions - zero_versions <= 1);
        padded += sim.r.zero_versions - zero_versions;
    }
    CHECK(padded > 0);
}

/* A survey only looks, whatever it names; a live session is never reset or
 * re-pointed by a call for another SMEM or host (REVIEW 2, should-fix 1). */
static void test_one_session(void)
{
    scenario = "survey of another host leaves the session alone";
    struct layout l = {.alt_host = 12};
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.partitions == 4);
    glink.filled[Q1N1_GLINK_CH_CLIENT][1] = 123;       /* as if mid-way through a multi-part message */
    struct q1n1_glink_session *saved = malloc(sizeof(*saved));
    *saved = glink.s;
    volatile uint32_t *bound = glink.desc;
    uint32_t writes = sim.writes, doorbells = sim.doorbells;
    volatile uint32_t *alt = (volatile uint32_t *)(sim.smem + sim.alt_desc);
    alt[0] = 0x40; alt[1] = 0x48; alt[2] = 0x10; alt[3] = 0x18;
    CHECK(q1n1_live_battery_run(&pf, Q1N1_LIVE_SURVEY, 0, 12, 0, 0) == 0);
    CHECK(result.remote_host == 12 && result.partition_offset == ALT_OFFSET && result.sample_status == 0);
    CHECK(result.desc0 == 0x40 && result.desc3 == 0x18);
    CHECK(result.desc_end0 == 0x40 && result.desc_end1 == 0x48 && result.desc_end2 == 0x10 && result.desc_end3 == 0x18);
    CHECK(memcmp(saved, &glink.s, sizeof(*saved)) == 0 && glink.desc == bound);
    CHECK(glink.filled[Q1N1_GLINK_CH_CLIENT][1] == 123);
    CHECK(sim.writes == writes && sim.doorbells == doorbells);
    /* The same with an explicit base, and on the session's own host. */
    CHECK(q1n1_live_battery_run(&pf, Q1N1_LIVE_SURVEY, 0, 12, SMEM_PHYS, SMEM_SIZE) == 0);
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && result.desc_end1 == saved->tx_head && result.desc_end2 == saved->rx_tail);
    CHECK(memcmp(saved, &glink.s, sizeof(*saved)) == 0 && glink.filled[Q1N1_GLINK_CH_CLIENT][1] == 123);
    glink.filled[Q1N1_GLINK_CH_CLIENT][1] = 0;
    /* The tick still services the session, and the next call continues it. */
    sim.r.pending_notify = 6;
    remote_step();
    ticks(10);
    CHECK(sim.r.notified == 6 && sim.r.pending_notify == 0 && router.foreign == 6);
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(result.adoptions == 1);

    /* A call that would bind elsewhere refuses before binding, while the
     * session is up; after a release the other host is free. */
    scenario = "no second session while one is up";
    writes = sim.writes, doorbells = sim.doorbells;
    CHECK(q1n1_live_battery_run(&pf, 0, 0, 12, 0, 0) == Q1N1_LIVE_EOTHER && result.phase == Q1N1_LIVE_PHASE_BIND);
    CHECK(strstr(result.text, "release it first") != NULL);
    CHECK(q1n1_live_battery_run(&pf, Q1N1_LIVE_RESYNC, 0, 12, 0, 0) == Q1N1_LIVE_EOTHER);
    CHECK(q1n1_live_battery_run(&pf, Q1N1_LIVE_RELEASE, 0, 12, 0, 0) == Q1N1_LIVE_EOTHER);
    CHECK(glink.s.state == Q1N1_GLINK_STARTED && glink.s.remote_host == 19 && glink.desc == bound);
    CHECK(sim.writes == writes && sim.doorbells == doorbells && alt[1] == 0x48 && alt[2] == 0x10);
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && words_zero());
    alt[0] = alt[1] = alt[2] = alt[3] = 0;
    CHECK(q1n1_live_battery_run(&pf, 0, 50, 12, 0, 0) == Q1N1_GLINK_ETIMEDOUT);   /* nobody there */
    CHECK(glink.s.remote_host == 12 && result.phase == Q1N1_LIVE_PHASE_OPEN);

    /* A session adopted after a chainload but not yet resumed: a survey
     * does not take it over, and the first tick still resumes it. */
    scenario = "survey before the resume";
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    *saved = glink.s;
    new_stage();
    q1n1_live_battery_adopt(saved, sizeof(*saved));
    CHECK(q1n1_live_battery_run(&pf, Q1N1_LIVE_SURVEY, 0, 12, 0, 0) == 0);
    CHECK(call(Q1N1_LIVE_SURVEY, 0) == 0 && glink.desc == NULL && glink.s.state == Q1N1_GLINK_STARTED);
    CHECK(q1n1_live_battery_run(&pf, 0, 0, 12, 0, 0) == Q1N1_LIVE_EOTHER && glink.desc == NULL);
    ticks(1);
    CHECK(strcmp(result.text, "session resumed") == 0 && result.adoptions == 1 && sim.r.versions == 1);
    free(saved);
}

/* Every store to our index words is ordered before the doorbell that
 * announces it (REVIEW 2, must-fix 2): the simulator compares words 1 and 2
 * at each doorbell with their values at the last store barrier. This covers
 * frames (head), and the READ_NOTIF kick right after consuming (tail). */
static void test_doorbell_order(void)
{
    scenario = "doorbell after a store barrier";
    struct layout l = {0};
    build(&l);
    new_stage();
    CHECK(call(0, 0) == 0);
    happy_checks(1);
    CHECK(sim.doorbells > 10 && sim.unordered_doorbells == 0);
    /* The SoCCP asks to be told when we read: q1n1 consumes the frame (tail
     * store) and rings at once. */
    uint32_t doorbells = sim.doorbells, tail = *word(2);
    control(13, 0, 0);
    remote_flush();
    ticks(1);
    CHECK(*word(2) != tail && sim.doorbells == doorbells + 1 && sim.unordered_doorbells == 0);
    CHECK(call(Q1N1_LIVE_RELEASE, 0) == 0 && sim.unordered_doorbells == 0);
}

/* The release's completion test (REVIEW 2, should-fix 4). A remote that read
 * DO_CLEANUP and everything after it, with our head wrapped to exactly 0 and
 * its own head at 0, shows words 0 and 3 at 0 without having reset. Every
 * start position of both FIFOs is tried with a remote that never resets: no
 * release may report success, and some must hit that state (and pad). The
 * same positions with a remote that resets all release. */
static void test_release_zero_head(void)
{
    scenario = "release: zero head is not a reset";
    unsigned padded = 0, released = 0;
    for (int resets = 0; resets < 2; resets++) {
        for (uint32_t a = 0; a < 256; a += 8) {
            for (uint32_t b = 0; b < 256; b += 8) {
                struct layout l = {.fifo0 = 256, .fifo1 = 256, .fifo0_uncached = 1, .w = {a, a, b, b}};
                build(&l);
                sim.r.chunk = 64;
                sim.r.ignore_cleanup = !resets;
                new_stage();
                CHECK(call(Q1N1_LIVE_RESYNC | Q1N1_LIVE_UCSI_VERSION, 0) == 0 && result.ucsi_version == 0x0210);
                uint64_t pads = glink.s.release_pads;
                int status = call(Q1N1_LIVE_RELEASE, 60);
                padded += (unsigned)(glink.s.release_pads - pads);
                if (resets) {
                    CHECK(status == 0 && words_zero() && sim.r.resets == 1);
                    released++;
                } else {
                    CHECK(status == Q1N1_GLINK_ETIMEDOUT && glink.s.state == Q1N1_GLINK_FAILED);
                    CHECK(sim.r.cleanup_matches == 1 && sim.r.resets == 0);
                }
                CHECK(sim.r.violations == 0 && sim.unordered_doorbells == 0);
            }
        }
    }
    CHECK(padded > 0 && released == 32 * 32);
}

int main(void)
{
    q1n1_glink_barrier_hook = sim_barrier;
    test_smem_parser();
    test_mmu_walk();
    test_happy_path();
    test_wrap_around();
    test_restart_modes();
    test_timeouts_and_stale();
    test_bad_replies();
    test_protocol_variants();
    test_refusals();
    test_attributes();
    test_foreign_partitions_untouched();
    test_release();
    test_service();
    test_refresh();
    test_frame_straddle();
    test_one_session();
    test_doorbell_order();
    test_release_zero_head();
    scenario = "every doorbell ordered after its index store";
    CHECK(unordered_total == 0);
    printf("live battery: %u checks passed\n", checks);
    free(sim.smem);
    return 0;
}
