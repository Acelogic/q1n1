/* SPDX-License-Identifier: MIT */
/* See live-battery.h. The order of checks is the order of the hardware
 * questions: where SMEM is, whether the SoCCP partition and its GLINK items
 * are there, whether the SoCCP is alive, whether the doorbell is the one the
 * firmware used, how the memory is mapped -- and only then any write. */
#include "live-battery.h"

#define DEFAULT_TIMEOUT_MS 1500u
#define MAX_TIMEOUT_MS 10000u
#define REFRESH_US 30000000u
#define REFRESH_TIMEOUT_MS 400u
#define SERVICE_US 20000u          /* the tick services a STARTED session this often */
#define SERVICE_FRAMES 16u         /* ... handling at most this many frames each time */
#define RESUME_TIMEOUT_MS 300u
#define RELEASE_TIMEOUT_MS 1500u
#define SMP2P_MAGIC 0x504d5324u   /* "$SMP", Linux smp2p.c:52 */
#define SMP2P_HEADER 20u
#define SMP2P_ENTRY 20u

#define RECORD_VERSION 2u
static struct q1n1_live_battery result = {
    .magic = Q1N1_LIVE_MAGIC, .version = RECORD_VERSION, .size = sizeof(struct q1n1_live_battery),
};
static struct q1n1_glink glink;
static struct q1n1_pmic_router router;
static int busy, refresh, resume_tried, adopt_refused, released_now;
static uint32_t survey_words[4];   /* a survey's second sample, for desc_end */
static uint64_t last_refresh_us, last_service_us;

struct q1n1_live_battery *q1n1_live_battery_result(void) { return &result; }
struct q1n1_glink_session *q1n1_live_battery_session(void) { return &glink.s; }

void q1n1_live_battery_adopt(const void *session, uint64_t size)
{
    const struct q1n1_glink_session *from = session;
    if (!from || from->magic != Q1N1_GLINK_SESSION_MAGIC) return;
    if (size != sizeof(*from) || from->version != Q1N1_GLINK_SESSION_VERSION || from->size != sizeof(*from)) {
        /* Not ours to continue. The next call sees the words it left and
         * applies the restart policy (live-battery.h). */
        adopt_refused = 1;
        return;
    }
    const uint8_t *src = session;
    uint8_t *dst = (uint8_t *)&glink.s;
    for (unsigned i = 0; i < sizeof(glink.s); i++) dst[i] = src[i];
    resume_tried = 0;
}

int q1n1_mmu_attr(uint64_t ttbr, uint64_t tcr, uint64_t mair, uint64_t va,
                  int (*read64)(void *ctx, uint64_t phys, uint64_t *value), void *ctx, uint32_t *attr)
{
    const uint64_t address = 0x0000fffffffff000ull;
    uint32_t t0sz = (uint32_t)(tcr & 63);
    if (((tcr >> 14) & 3) != 0 || t0sz < 16 || t0sz > 39) return -1;   /* 4 KiB granule only */
    uint32_t bits = 64 - t0sz;
    if (va >> bits) return -1;
    int level = 4 - (int)((bits - 12 + 8) / 9);
    uint64_t table = ttbr & address, desc = 0;
    for (;; level++) {
        uint32_t shift = 12 + 9 * (uint32_t)(3 - level);
        if (read64(ctx, table + ((va >> shift) & 0x1ff) * 8, &desc)) return -2;
        if (!(desc & 1)) return -3;                                     /* not mapped */
        if (level == 3) {
            if ((desc & 3) != 3) return -3;
            break;
        }
        if ((desc & 3) == 1) {                                          /* block */
            if (level == 0) return -4;
            break;
        }
        table = desc & address;
    }
    *attr = (uint32_t)(mair >> (8 * ((desc >> 2) & 7))) & 0xff;
    return 0;
}

int q1n1_mmu_attr_uncached(uint32_t attr)
{
    return (attr & 0xf0) == 0 || attr == 0x44;
}

int q1n1_mmu_attr_device(uint32_t attr)
{
    return (attr & 0xf0) == 0;
}

static uint32_t rd32le(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* The SoCCP's SMP2P record (item 617, Linux smp2p.c:70-84): local_pid must
 * be the remote host, remote_pid APPS. Returns the "slave-kernel" value. */
static int smp2p_value(const struct q1n1_smem *smem, const struct q1n1_smem_item *item, uint32_t host,
                       uint64_t *value)
{
    if (item->size < SMP2P_HEADER) return Q1N1_LIVE_EREMOTE;
    uint32_t at = item->offset;
    uint32_t pids = q1n1_smem_rd32(smem, at + 8), counts = q1n1_smem_rd32(smem, at + 12);
    uint32_t total = counts & 0xffff, valid = counts >> 16;
    if (q1n1_smem_rd32(smem, at) != SMP2P_MAGIC || (pids & 0xffff) != host || (pids >> 16) != 0 ||
        total > 16 || valid > total || item->size < SMP2P_HEADER + total * SMP2P_ENTRY)
        return Q1N1_LIVE_EREMOTE;
    static const char name[] = "slave-kernel";
    for (uint32_t e = 0; e < valid; e++) {
        uint32_t entry = at + SMP2P_HEADER + e * SMP2P_ENTRY;
        /* Linux compares with strcmp: bytes after the NUL do not matter. */
        int match = 1;
        for (unsigned i = 0; i < sizeof(name) && match; i++) {
            uint32_t word = q1n1_smem_rd32(smem, entry + (i & ~3u));
            match = (uint8_t)(word >> (8 * (i & 3))) == (uint8_t)name[i];
        }
        if (!match) continue;
        *value = q1n1_smem_rd32(smem, entry + 16);
        return Q1N1_LIVE_OK;
    }
    return Q1N1_SMEM_ENOENT;
}

static const struct q1n1_live_platform *platform;
static uint64_t glink_now(void *ctx) { (void)ctx; return platform->now_us(platform->ctx); }
static void glink_relax(void *ctx) { (void)ctx; if (platform->relax) platform->relax(platform->ctx); }
static int glink_doorbell(void *ctx, uint64_t address, uint32_t value)
{
    (void)ctx;
    return platform->write32(platform->ctx, address, value);
}
static struct q1n1_glink_ops ops = {
    .now_us = glink_now, .relax = glink_relax, .doorbell = glink_doorbell,
    .deliver = q1n1_pmic_router_deliver, .deliver_ctx = &router,
};

static void record_session(void)
{
    const struct q1n1_glink_session *s = &glink.s;
    struct q1n1_live_battery *r = &result;
    r->glink_state = s->state;
    r->glink_error = (uint64_t)(int64_t)s->last_error;
    r->glink_command = s->last_command;
    r->glink_mode = s->mode;
    r->features = s->features;
    r->version_acked = s->version_acked;
    r->remote_version = s->remote_version;
    const struct q1n1_glink_channel *ch = &s->ch[Q1N1_GLINK_CH_CLIENT];
    r->lcid = ch->lcid;
    r->rcid = ch->rcid;
    r->open_sent = ch->open_sent;
    r->open_acked = ch->open_acked;
    r->remote_opened = ch->remote_opened;
    r->remote_intents = 0;
    for (unsigned i = 0; i < Q1N1_GLINK_REMOTE_INTENTS; i++) r->remote_intents += ch->remote[i].valid;
    r->ssr_opened = s->ch[Q1N1_GLINK_CH_SSR].remote_opened;
    r->releases = s->releases;
    r->cleanup_sent = s->cleanup_sent;
    r->cleanup_done = s->cleanup_done;
    r->service_polls = s->service_polls;
    r->service_frames = s->service_frames;
    r->version_zero_acks = s->version_zero_acks;
    r->remote_closes = s->remote_closes;
    r->adopt_refused = (uint64_t)adopt_refused;
    if (glink.desc) {
        r->desc_end0 = glink.desc[0];
        r->desc_end1 = glink.desc[1];
        r->desc_end2 = glink.desc[2];
        r->desc_end3 = glink.desc[3];
    }
    r->restarts = s->restarts;
    r->adoptions = s->adoptions;
    r->tx_frames = s->tx_frames;
    r->rx_frames = s->rx_frames;
    r->doorbells = s->doorbells;
    r->messages = s->messages;
    r->dropped = s->dropped;
    r->foreign_opens = s->foreign_opens;
    r->protocol_errors = s->protocol_errors;
    r->pmic_requests = router.requests;
    r->pmic_replies = router.replies;
    r->pmic_stale = router.stale_discarded;
    r->pmic_unsolicited = router.unsolicited;
    r->pmic_foreign = router.foreign;
    r->pmic_bad = router.bad_replies;
    r->reply_owner = router.last_owner;
    r->reply_type = router.last_type;
    r->reply_opcode = router.last_opcode;
    r->reply_length = router.last_length;
    r->reply_error = router.last_error;
}

static void put_text(const char *a, int64_t status, uint64_t phase)
{
    char *t = result.text;
    unsigned n = 0;
    while (*a && n + 1 < Q1N1_LIVE_TEXT) t[n++] = *a++;
    if (status) {
        const char *tail = " status -";
        while (*tail && n + 1 < Q1N1_LIVE_TEXT) t[n++] = *tail++;
        char digits[20];
        unsigned d = 0;
        uint64_t v = (uint64_t)-status;
        do { digits[d++] = (char)('0' + v % 10); v /= 10; } while (v && d < sizeof(digits));
        while (d && n + 1 < Q1N1_LIVE_TEXT) t[n++] = digits[--d];
        tail = " phase ";
        while (*tail && n + 1 < Q1N1_LIVE_TEXT) t[n++] = *tail++;
        if (n + 3 < Q1N1_LIVE_TEXT) {
            if (phase >= 10) t[n++] = (char)('0' + phase / 10);
            t[n++] = (char)('0' + phase % 10);
        }
    }
    t[n] = 0;
}

/* Guarded probe of one word in each page of [at, at + size): the parser
 * below then reads those pages through plain loads, which cannot fault. */
static int probe_pages(const struct q1n1_live_platform *pf, uint64_t at, uint64_t size)
{
    for (uint64_t page = at & ~0xfffull; page < at + size; page += 4096) {
        uint32_t word;
        if (pf->read(pf->ctx, &word, page, 4)) return Q1N1_LIVE_EFAULT;
    }
    return Q1N1_LIVE_OK;
}

/* Worst MAIR attribute over the pages of [phys, phys + size): 0 when every
 * page is non-cacheable, 1 when one is known cacheable (*record = its
 * attribute), -1 when one could not be read (*record = ~0). */
static int range_attr(const struct q1n1_live_platform *pf, uint64_t phys, uint64_t size, uint64_t *record)
{
    int unknown = 0;
    uint64_t first = ~0ull;
    for (uint64_t page = phys & ~0xfffull; page < phys + size; page += 4096) {
        uint32_t attr = 0;
        if (pf->attr(pf->ctx, page, &attr)) { unknown = 1; continue; }
        if (!q1n1_mmu_attr_uncached(attr)) { *record = attr; return 1; }
        if (first == ~0ull) first = attr;
    }
    *record = unknown ? ~0ull : first;
    return unknown ? -1 : 0;
}

static int all_zero(const uint32_t words[4]) { return !(words[0] | words[1] | words[2] | words[3]); }

static int run(const struct q1n1_live_platform *pf, uint64_t flags, uint64_t deadline, uint32_t host,
               uint64_t base, uint64_t size)
{
    struct q1n1_live_battery *r = &result;
    const int survey = (flags & Q1N1_LIVE_SURVEY) != 0;
    int status;

    /* 1. Where SMEM is: explicit override, else the firmware's target info,
     *    else the glymur device tree. */
    r->phase = Q1N1_LIVE_PHASE_TARGET;
    r->target_status = (uint64_t)(int64_t)Q1N1_SMEM_ENOENT;
    if (!base) {
        uint64_t pointer = 0;
        status = pf->read(pf->ctx, &pointer, Q1N1_SMEM_TARGET_INFO_POINTER, 8);
        r->target_pointer = status ? ~0ull : pointer;
        if (!status && pointer && !(pointer & 7) && pf->in_memory_map(pf->ctx, pointer, Q1N1_SMEM_TARGET_INFO_BYTES)) {
            _Alignas(8) uint8_t raw[Q1N1_SMEM_TARGET_INFO_BYTES];
            struct q1n1_smem_target target;
            status = pf->read(pf->ctx, raw, pointer, sizeof(raw));
            if (!status) status = q1n1_smem_parse_target(raw, &target);
            r->target_status = (uint64_t)(int64_t)status;
            if (!status) {
                base = target.base;
                size = target.size;
                r->target_base = target.base;
                r->target_size = target.size;
            }
        }
        if (!base) base = Q1N1_SMEM_DEFAULT_BASE, size = Q1N1_SMEM_DEFAULT_SIZE;
    }
    if (!size) size = Q1N1_SMEM_DEFAULT_SIZE;
    r->smem_base = base;
    r->smem_size = size;
    if ((base & 0xfff) || (size & 0xfff) || size < 0x2000 || size > (64u << 20) || base + size > (1ull << 40))
        return Q1N1_SMEM_EINVAL;

    /* 2. The header page and the table page -- the two pages Linux maps
     *    before it knows the partitions (smem.c:1083-1097) -- and nothing
     *    else until the table says which partition is ours. */
    r->phase = Q1N1_LIVE_PHASE_SMEM;
    status = probe_pages(pf, base, 4);
    if (!status) status = probe_pages(pf, base + size - Q1N1_SMEM_TOC_BYTES, 4);
    if (status) return status;
    volatile const uint8_t *mapped = pf->map(pf->ctx, base, size);
    if (!mapped) return Q1N1_LIVE_EFAULT;
    struct q1n1_smem smem;
    status = q1n1_smem_init(&smem, mapped, base, (uint32_t)size);
    r->smem_status = (uint64_t)(int64_t)status;
    r->smem_version = smem.version;
    r->smem_items = smem.item_count;
    r->ptable_entries = smem.ptable_entries;
    if (status) return status;
    /* Every partition as the table lists it; no partition header is read. */
    r->partitions = 0;
    for (uint32_t i = 0; i < smem.ptable_entries && r->partitions < Q1N1_LIVE_PARTITIONS; i++) {
        struct q1n1_smem_partition p;
        if (q1n1_smem_table_entry(&smem, i, &p) == Q1N1_SMEM_ENOENT) continue;
        uint64_t *row = r->partition[r->partitions++];
        row[0] = p.host0 | p.host1 << 16;
        row[1] = p.offset;
        row[2] = p.size;
        row[3] = p.cacheline;
    }

    /* 3. The APPS<->SoCCP partition: chosen from the table, then its pages
     *    proved readable, then its header checked. */
    r->phase = Q1N1_LIVE_PHASE_PARTITION;
    r->remote_host = host;
    struct q1n1_smem_partition part;
    status = q1n1_smem_find_entry(&smem, host, &part);
    if (!status) {
        r->partition_offset = part.offset;
        r->partition_size = part.size;
        r->partition_cacheline = part.cacheline;
        status = probe_pages(pf, base + part.offset, part.size);
        if (status) return status;
        status = q1n1_smem_partition_at(&smem, part.index, &part);
    }
    r->partition_status = (uint64_t)(int64_t)status;
    if (status) return status;

    /* 4. The GLINK items. */
    r->phase = Q1N1_LIVE_PHASE_ITEMS;
    struct q1n1_smem_item desc, tx, rx;
    status = q1n1_smem_get(&smem, &part, Q1N1_GLINK_ITEM_DESCRIPTOR, &desc);
    if (!status) r->desc_offset = desc.offset;
    if (!status) status = q1n1_smem_get(&smem, &part, Q1N1_GLINK_ITEM_TX_FIFO, &tx);
    if (!status) r->tx_offset = tx.offset, r->tx_length = tx.size, r->tx_cached = tx.cached;
    if (!status) status = q1n1_smem_get(&smem, &part, Q1N1_GLINK_ITEM_RX_FIFO, &rx);
    if (!status) r->rx_offset = rx.offset, r->rx_length = rx.size, r->rx_cached = rx.cached;
    if (status) return status;
    if (desc.size == 32) {
        const uint32_t w = desc.offset;
        r->desc0 = q1n1_smem_rd32(&smem, w);
        r->desc1 = q1n1_smem_rd32(&smem, w + 4);
        r->desc2 = q1n1_smem_rd32(&smem, w + 8);
        r->desc3 = q1n1_smem_rd32(&smem, w + 12);
    }

    /* 5. Is the SoCCP alive? Writes need its record present, ready (bit 1)
     *    and not fatal (bit 0); the survey only reports. */
    r->phase = Q1N1_LIVE_PHASE_SMP2P;
    struct q1n1_smem_item smp2p;
    status = q1n1_smem_get(&smem, &part, Q1N1_SOCCP_SMP2P_IN, &smp2p);
    if (!status) {
        r->smp2p_offset = smp2p.offset;
        status = smp2p_value(&smem, &smp2p, host, &r->smp2p_value);
    }
    r->smp2p_status = (uint64_t)(int64_t)status;
    if (!survey && (status || (r->smp2p_value & 1) || !(r->smp2p_value & 2))) return Q1N1_LIVE_EREMOTE;

    /* 6. The doorbell the firmware used. */
    r->phase = Q1N1_LIVE_PHASE_IPCC;
    r->doorbell_address = Q1N1_IPCC_FRAME + Q1N1_IPCC_SEND;
    r->doorbell_value = Q1N1_IPCC_SOCCP_GLINK;
    uint32_t version = 0;
    status = pf->read(pf->ctx, &version, Q1N1_IPCC_FRAME, 4);
    r->ipcc_version = status ? ~0ull : version;
    if (status) return status;
    if ((version >> 16) != 2 && !survey) return Q1N1_LIVE_EIPCC;

    /* 7. How every page we write or read is mapped. The index words of both
     *    sides share a cache line and the SoCCP does not snoop our caches, so
     *    a cacheable page anywhere we touch is refused, override or not; a
     *    page whose attribute cannot be read is refused unless the caller
     *    says so. The doorbell must be Device: a Normal mapping may merge,
     *    delay or repeat the write. */
    r->phase = Q1N1_LIVE_PHASE_MAPPING;
    int cls[4];
    cls[0] = range_attr(pf, base, 4096, &r->smem_attr);
    cls[1] = range_attr(pf, base + desc.offset, 32, &r->desc_attr);
    cls[2] = range_attr(pf, base + tx.offset, tx.size, &r->tx_attr);
    cls[3] = range_attr(pf, base + rx.offset, rx.size, &r->rx_attr);
    uint32_t attr = 0;
    int ipcc_known = !pf->attr(pf->ctx, Q1N1_IPCC_FRAME, &attr);
    r->ipcc_attr = ipcc_known ? attr : ~0ull;
    if (!survey) {
        int unknown = !ipcc_known;
        for (unsigned i = 0; i < 4; i++) {
            if (cls[i] > 0) return Q1N1_LIVE_ECACHED;
            unknown |= cls[i] < 0;
        }
        if (ipcc_known && !q1n1_mmu_attr_device(attr)) return Q1N1_LIVE_EDEVICE;
        if (unknown && !(flags & Q1N1_LIVE_ATTR_UNKNOWN_OK)) return Q1N1_LIVE_EATTR;
    }

    /* 8. Bind. A session copied from the previous stage survives if it names
     *    the same items. Reading the descriptor twice shows whether it moves.
     *    A survey only looks: it reads through a view and never binds, so it
     *    cannot reset or re-point a live session (any SMEM, any host). */
    r->phase = Q1N1_LIVE_PHASE_BIND;
    uint32_t words[4];
    if (survey) {
        struct q1n1_glink_view view;
        status = q1n1_glink_view(&smem, &part, &view);
        if (status) return status;
        status = q1n1_glink_view_sample(&view, &ops, words, 1000);
        r->sample_status = (uint64_t)(int64_t)status;
        for (unsigned i = 0; i < 4; i++) survey_words[i] = words[i];
        return Q1N1_LIVE_OK;
    }
    const struct q1n1_glink_session *live = &glink.s;
    if (live->state == Q1N1_GLINK_STARTED && live->magic == Q1N1_GLINK_SESSION_MAGIC &&
        (live->smem_phys != base || live->smem_size != size || live->remote_host != host))
        return Q1N1_LIVE_EOTHER;
    status = q1n1_glink_bind(&glink, &smem, &part, &ops, r->doorbell_address, r->doorbell_value);
    if (status) return status;
    status = q1n1_glink_sample(&glink, words, 1000);
    r->sample_status = (uint64_t)(int64_t)status;

    /* 9. Continue the session, release it, or restart the link. */
    r->phase = Q1N1_LIVE_PHASE_SESSION;
    int first_in_stage = router.glink == 0;
    router.glink = &glink;
    int continued = !(flags & Q1N1_LIVE_RESTART) && q1n1_glink_check(&glink) == Q1N1_GLINK_OK;
    if (flags & Q1N1_LIVE_RELEASE) {
        r->phase = Q1N1_LIVE_PHASE_RELEASE;
        if (!continued) {
            /* Nothing of ours is up. Already all zero is what a release
             * leaves; anything else is not ours to clean up. */
            status = q1n1_glink_sample(&glink, words, 1000);
            if (status) return status;
            return all_zero(words) ? Q1N1_LIVE_OK : Q1N1_LIVE_ENOSESSION;
        }
        status = q1n1_glink_release(&glink, deadline);
        r->release_status = (uint64_t)(int64_t)status;
        released_now = !status;
        return status;
    }
    if (continued) {
        glink.s.adoptions++;
        if (first_in_stage) {
            /* Replies owed to the previous stage's timed-out requests. */
            router.orphans = (uint32_t)glink.s.client_carry[0];
            router.orphan_since_us = glink.s.client_carry[1];
        }
    } else {
        if (flags & Q1N1_LIVE_NO_RESTART) return Q1N1_LIVE_ENORESTART;
        /* A restart needs the remote quiet and every index in range, in both
         * samples of this call (the words moving at all means someone is
         * using the link); check that before the first write. */
        if (r->sample_status) return (int)(int64_t)r->sample_status;
        status = q1n1_glink_sample(&glink, words, 1000);
        if (status) return status;
        enum q1n1_glink_restart_mode mode;
        if (flags & Q1N1_LIVE_LINUX) mode = Q1N1_GLINK_RESTART_LINUX;
        else if (flags & Q1N1_LIVE_DRAIN) mode = Q1N1_GLINK_RESTART_DRAIN;
        else if (flags & Q1N1_LIVE_RESYNC) mode = Q1N1_GLINK_RESTART_RESYNC;
        else if (all_zero(words)) mode = Q1N1_GLINK_RESTART_LINUX;   /* the cleaned-up state */
        else return Q1N1_LIVE_ENEEDMODE;
        /* Our head to 0 under a SoCCP tail that is not 0 makes it read our
         * FIFO from there; LINUX with its head not 0 parses its old frames.
         * Neither is a state the firmware leaves: only on purpose. */
        if (mode != Q1N1_GLINK_RESTART_RESYNC && (words[0] | words[3]) && !(flags & Q1N1_LIVE_FORCE))
            return Q1N1_LIVE_EWORDS;
        /* Our first write: prove the page takes it with the value already there. */
        uint64_t word1 = base + desc.offset + 4;
        uint32_t current = q1n1_smem_rd32(&smem, desc.offset + 4);
        if (pf->write32(pf->ctx, word1, current)) return Q1N1_LIVE_EFAULT;
        router.orphans = 0;
        router.previous_length = 0;
        status = q1n1_glink_restart(&glink, Q1N1_PMIC_CHANNEL, Q1N1_PMIC_LCID, mode);
        if (status) return status;
    }

    /* 10. Resume (a chainloaded stage's first tick): consume what is waiting. */
    if (flags & Q1N1_LIVE_RESUME) {
        status = q1n1_glink_service(&glink, SERVICE_FRAMES);
        return status < 0 ? status : Q1N1_LIVE_OK;
    }

    /* 11. Channel open, intents exchanged. */
    r->phase = Q1N1_LIVE_PHASE_OPEN;
    status = q1n1_glink_wait_ready(&glink, deadline);
    if (status) return status;

    /* 12. The registers. */
    r->phase = Q1N1_LIVE_PHASE_READ;
    if (flags & Q1N1_LIVE_UCSI_VERSION) {
        uint8_t value[4];
        status = q1n1_pmic_router_read(&router, Q1N1_PMIC_UCSI_VERSION, value, 4, deadline);
        if (status) return status;
        r->ucsi_version = rd32le(value);
        return Q1N1_LIVE_OK;
    }
    struct q1n1_battery b;
    status = q1n1_pmic_router_battery(&router, &b, deadline);
    if (status && status != Q1N1_PMIC_EIMPLAUSIBLE) return status;
    r->plausible = status == Q1N1_PMIC_OK;
    r->state = b.state;
    r->rate = b.rate;
    r->remaining = b.remaining;
    r->voltage = b.voltage;
    r->unit = b.unit;
    r->design = b.design;
    r->full = b.full;
    r->cycles = b.cycles;
    r->ac = b.ac;
    r->percent = b.percent;
    if (status) return status;
    q1n1_battery_format(&b, r->text, Q1N1_LIVE_TEXT);
    return Q1N1_LIVE_OK;
}

static int flags_valid(uint64_t flags)
{
    uint64_t modes = flags & Q1N1_LIVE_MODES;
    if (modes & (modes - 1)) return 0;                                  /* two modes */
    if ((flags & Q1N1_LIVE_RELEASE) &&
        (modes || (flags & (Q1N1_LIVE_RESTART | Q1N1_LIVE_SURVEY | Q1N1_LIVE_UCSI_VERSION | Q1N1_LIVE_RESUME))))
        return 0;                                                       /* a release never restarts */
    if ((flags & (Q1N1_LIVE_NO_RESTART | Q1N1_LIVE_RESUME)) && (modes || (flags & Q1N1_LIVE_RESTART))) return 0;
    if ((flags & Q1N1_LIVE_FORCE) && !(flags & (Q1N1_LIVE_LINUX | Q1N1_LIVE_DRAIN))) return 0;  /* force what? */
    return 1;
}

int q1n1_live_battery_run(const struct q1n1_live_platform *pf, uint64_t flags, uint64_t timeout_ms,
                          uint64_t host, uint64_t smem_base, uint64_t smem_size)
{
    if (!pf || !pf->read || !pf->write32 || !pf->map || !pf->attr || !pf->in_memory_map || !pf->now_us)
        return Q1N1_SMEM_EINVAL;
    if (busy) return Q1N1_LIVE_EBUSY;
    busy = 1;
    platform = pf;
    if (flags & Q1N1_LIVE_REFRESH_ON) refresh = 1;
    if (flags & Q1N1_LIVE_REFRESH_OFF) refresh = 0;
    if (!timeout_ms) timeout_ms = DEFAULT_TIMEOUT_MS;
    if (timeout_ms > MAX_TIMEOUT_MS) timeout_ms = MAX_TIMEOUT_MS;
    if (!host) host = Q1N1_SOCCP_HOST;

    struct q1n1_live_battery *r = &result;
    uint64_t calls = r->calls + 1, successes = r->successes;
    uint8_t *clear = (uint8_t *)r;
    for (unsigned i = 0; i < sizeof(*r); i++) clear[i] = 0;
    r->magic = Q1N1_LIVE_MAGIC;
    r->version = RECORD_VERSION;
    r->size = sizeof(*r);
    r->calls = calls;
    r->successes = successes;
    r->flags = flags;

    released_now = 0;
    for (unsigned i = 0; i < 4; i++) survey_words[i] = 0;
    uint64_t start = pf->now_us(pf->ctx);
    int status = !start ? Q1N1_LIVE_ECLOCK :
                 !flags_valid(flags) ? Q1N1_LIVE_EFLAGS :
                 host >= Q1N1_SMEM_HOST_GLOBAL ? Q1N1_SMEM_EINVAL :
                 run(pf, flags, start + timeout_ms * 1000, (uint32_t)host, smem_base, smem_size);
    if (glink.desc && !(flags & Q1N1_LIVE_SURVEY)) {
        /* Leave our FIFO the way the firmware left it: zeros past the pending
         * data (q1n1_glink_scrub), so whoever restarts the link next finds no
         * old frames of ours to replay. Also after a failure, as long as
         * word 1 still holds our head (the scrub checks). */
        if (glink.s.state == Q1N1_GLINK_STARTED || glink.s.state == Q1N1_GLINK_FAILED) q1n1_glink_scrub(&glink);
        glink.s.client_carry[0] = router.orphans;
        glink.s.client_carry[1] = router.orphan_since_us;
    }
    r->elapsed_us = pf->now_us(pf->ctx) - start;
    r->status = (uint64_t)(int64_t)status;
    record_session();
    if (flags & Q1N1_LIVE_SURVEY) {
        /* The words this survey saw, not those of whatever session is bound. */
        r->desc_end0 = survey_words[0];
        r->desc_end1 = survey_words[1];
        r->desc_end2 = survey_words[2];
        r->desc_end3 = survey_words[3];
    }
    if (!status) {
        r->successes++;
        r->phase = Q1N1_LIVE_PHASE_DONE;
        if (flags & Q1N1_LIVE_SURVEY) put_text("survey complete", 0, 0);
        else if (flags & Q1N1_LIVE_RELEASE) put_text(released_now ?
                                                     "released: DO_CLEANUP sent, all four words 0" :
                                                     "nothing to release: words already 0", 0, 0);
        else if (flags & Q1N1_LIVE_RESUME) put_text("session resumed", 0, 0);
        else if (flags & Q1N1_LIVE_UCSI_VERSION) put_text("UCSI version read", 0, 0);
    } else {
        put_text(r->plausible || status == Q1N1_PMIC_EIMPLAUSIBLE ? "implausible battery values" :
                 status == Q1N1_LIVE_ENEEDMODE ? "restart needs an explicit mode" :
                 status == Q1N1_LIVE_EWORDS ? "SoCCP words 0/3 not 0: linux/drain refused without force" :
                 status == Q1N1_LIVE_EOTHER ? "a session on another SMEM/host is up: release it first" :
                 (flags & Q1N1_LIVE_RELEASE) ? "release failed" : "no reading",
                 status, r->phase);
    }
    busy = 0;
    return status;
}

void q1n1_live_battery_tick(const struct q1n1_live_platform *pf)
{
    if (!pf || !pf->now_us || busy) return;
    uint64_t now = pf->now_us(pf->ctx);
    if (!now) return;
    if (glink.s.state == Q1N1_GLINK_STARTED && now - last_service_us >= SERVICE_US) {
        last_service_us = now;
        if (!glink.desc) {
            /* A session adopted from the previous stage: bind it through the
             * full checks once, without restarting, then service it. */
            if (!resume_tried) {
                resume_tried = 1;
                q1n1_live_battery_run(pf, Q1N1_LIVE_RESUME | Q1N1_LIVE_NO_RESTART, RESUME_TIMEOUT_MS,
                                      glink.s.remote_host, glink.s.smem_phys, glink.s.smem_size);
            }
        } else {
            busy = 1;
            platform = pf;
            q1n1_glink_service(&glink, SERVICE_FRAMES);
            record_session();
            busy = 0;
        }
    }
    if (!refresh) return;
    if (last_refresh_us && now - last_refresh_us < REFRESH_US) return;
    last_refresh_us = now;
    q1n1_live_battery_run(pf, Q1N1_LIVE_NO_RESTART, REFRESH_TIMEOUT_MS, 0, 0, 0);
}

int q1n1_live_battery_release(const struct q1n1_live_platform *pf)
{
    if (glink.s.state != Q1N1_GLINK_STARTED) return Q1N1_LIVE_OK;
    return q1n1_live_battery_run(pf, Q1N1_LIVE_RELEASE, RELEASE_TIMEOUT_MS, glink.s.remote_host,
                                 glink.s.smem_phys, glink.s.smem_size);
}
