/* SPDX-License-Identifier: MIT */
/* Each powered A16 controller discovers its own USB data direction. GCTL is
 * local controller configuration, not a Type-C contract. No PD commands or
 * clock/PHY power guesses are needed: alternate bounded host/device attempts
 * while idle, and never disturb another port or a configured connection. */
#include "usb-ports.h"
void *memset(void *dest, int value, size_t n);

#define PORTS_MAGIC UINT64_C(0x325354524f505355)
#define PORTS_VERSION 1
#define GCTL 0xc110
#define GSNPSID 0xc120
#define DCTL 0xc704
#define DSTS 0xc70c
#define RUN_STOP (1u << 31)
#define ROLE_MASK (3u << 12)
#define HOST_DWELL_US 1500000u
#define DEVICE_DWELL_US 3000000u
#define DISCONNECT_US 500000u
#define SUSPEND_RECOVERY_US 8000000u
#define HOST_SLOT_BYTES (2u << 20)

/* Referenced by the stage header so the reader can reject an update that
 * would reset a live NCM link instead of adopting its shared state. */
const uint64_t q1n1_usb_ports_contract[3] = {
    PORTS_MAGIC, PORTS_VERSION, sizeof(struct q1n1_usb_ports)
};

_Static_assert(sizeof(struct q1n1_usb_ports) < 0x10000, "USB state fits before DMA");
_Static_assert(0x10000 + QDWC3_ARENA_SIZE + Q1N1_USB_HOST_ARENA <= HOST_SLOT_BYTES,
               "USB port DMA regions do not overlap");

static int host_present(const struct q1n1_usb_port *p)
{
    return p->ncm.x && p->ncm.stats.init_step == 9 && p->ncm.device.port &&
           (xhci_portsc((struct xhci *)&p->host, p->ncm.device.port) & 3u) == 3u;
}

static int port_ready(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    if (s->status[i].role == Q1N1_USB_DEVICE)
        return !qdwc3_link_down(&p->device) && qdwc3_ready(&p->device, 0);
    if (s->status[i].role == Q1N1_USB_HOST)
        return host_present(p) && p->proxy.io.ready(p->proxy.io.ctx);
    return 0;
}

static int halt_host(struct q1n1_usb_port *p)
{
    /* Do not change direction while xHCI might still own DMA. */
    uintptr_t op = p->base + (qdwc3_rd(p->base) & 0xff);
    qdwc3_wr(op, qdwc3_rd(op) & ~1u);
    for (unsigned n = 0; n < 1000; n++) {
        if (qdwc3_rd(op + 4) & 1u) return 0;
        qdwc3_delay_us(100);
    }
    return -1;
}

static int stop_port(struct q1n1_usb_ports *s, unsigned i, int graceful)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    if (st->role == Q1N1_USB_DEVICE) {
        qdwc3_stop(&p->device);
        if (qdwc3_rd(p->base + DCTL) & RUN_STOP) return -1;
        if (!(qdwc3_rd(p->base + DSTS) & (1u << 22))) return -1;
        qdwc3_wr(p->qscratch + 0x10, p->qs_hs);
        qdwc3_wr(p->qscratch + 0x30, p->qs_ss);
    } else if (st->role == Q1N1_USB_HOST) {
        if (graceful && host_present(p)) ncm_close(&p->ncm);
        if (halt_host(p)) return -1;
    }
    p->ncm.x = NULL;
    p->proxy.have_peer = 0;
    p->proxy.head = p->proxy.tail = 0;
    st->connected = 0;
    st->role = Q1N1_USB_OFF;
    p->down_since = 0;
    p->suspended_since = 0;
    p->console_ready = 0;
    p->checksum_state = 0;
    return 0;
}

static int snapshot(struct q1n1_usb_port *p)
{
    if (qdwc3_rd(p->base + GSNPSID) != 0x33313130 ||
        (qdwc3_rd(p->base) & 0xff) != 0x30 ||
        (qdwc3_rd(p->base + 4) >> 24) != 2) return -1;
    p->saved = (struct qdwc3_saved){
        .gsbuscfg0 = qdwc3_rd(p->base + 0xc100), .gsbuscfg1 = qdwc3_rd(p->base + 0xc104),
        .gctl = qdwc3_rd(p->base + GCTL), .guctl = qdwc3_rd(p->base + 0xc12c),
        .guctl1 = qdwc3_rd(p->base + 0xc11c), .guctl2 = qdwc3_rd(p->base + 0xc19c),
        .gusb2phycfg = qdwc3_rd(p->base + 0xc200), .gusb3pipectl = qdwc3_rd(p->base + 0xc2c0),
        .gfladj = qdwc3_rd(p->base + 0xc630), .dcfg = qdwc3_rd(p->base + 0xc700),
        .gsnpsid = qdwc3_rd(p->base + GSNPSID)};
    p->qs_hs = qdwc3_rd(p->qscratch + 0x10);
    p->qs_ss = qdwc3_rd(p->qscratch + 0x30);
    return 0;
}

static int start_port(struct q1n1_usb_ports *s, unsigned i, unsigned role)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    st->attempts++;
    /* Firmware/previous stages have stopped their engines before this point.
     * Still halt the host explicitly before changing direction. Device takeover
     * also quiesces and resets its engine before publishing new DMA pointers. */
    if (halt_host(p)) return -10;
    if (role == Q1N1_USB_HOST && (qdwc3_rd(p->base + DCTL) & RUN_STOP)) return -11;
    qdwc3_wr(p->base + GCTL, (p->saved.gctl & ~ROLE_MASK) | (role << 12));
    qdwc3_delay_us(50000);
    st->role = role;
    int result;
    if (role == Q1N1_USB_HOST) {
        result = xhci_init(&p->host, p->base, p->host_dma, Q1N1_USB_HOST_ARENA);
        p->deadline = xhci_now_us() + HOST_DWELL_US;
    } else {
        result = qdwc3_takeover(&p->device, p->base, p->qscratch, &p->saved,
                               p->device_dma, QDWC3_ARENA_SIZE);
        p->deadline = xhci_now_us() + DEVICE_DWELL_US;
    }
    p->started = xhci_now_us();
    p->activity = 0;
    p->down_since = 0;
    return result;
}

static void failed(struct q1n1_usb_ports *s, unsigned i, int result)
{
    struct q1n1_usb_port *p = &s->port[i];
    s->status[i].last_error = (uint64_t)(int64_t)result;
    if (stop_port(s, i, 0)) {
        s->status[i].role = Q1N1_USB_UNSUPPORTED; /* Cannot safely reuse DMA. */
        return;
    }
    if (p->failures < 4) p->failures++;
    p->retry_at = xhci_now_us() + (250000u << p->failures);
}

static void service_port(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    if (s->status[i].role == Q1N1_USB_DEVICE) {
        qdwc3_poll(&p->device);
        uint8_t discard[64];
        qdwc3_read(&p->device, 1, discard, sizeof(discard));
        int ready = qdwc3_ready(&p->device, 1);
        if (ready && !p->console_ready) {
            static const uint8_t banner[] = "q1n1 EL2: proxy is on the first CDC port.\r\n";
            qdwc3_write(&p->device, 1, banner, sizeof(banner) - 1);
        }
        p->console_ready = (uint32_t)ready;
    } else if (s->status[i].role == Q1N1_USB_HOST && host_present(p)) {
        p->proxy.io.poll(p->proxy.io.ctx);
    }
}

static void maintain_port(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    uint64_t now = xhci_now_us();
    if (st->role == Q1N1_USB_UNSUPPORTED) return;
    if (st->role == Q1N1_USB_OFF) {
        if (now < p->retry_at) return;
        unsigned role = p->next_role;
        p->next_role = role == Q1N1_USB_HOST ? Q1N1_USB_DEVICE : Q1N1_USB_HOST;
        int result = start_port(s, i, role);
        if (result) failed(s, i, result);
        return;
    }
    if (st->role == Q1N1_USB_DEVICE) {
        uint64_t activity = p->device.stats.setups + p->device.stats.resets;
        int down = qdwc3_link_down(&p->device);
        /* The A16 forces session-valid. A real cable removal can therefore
         * leave USB2 in U3/Suspend, with configured and DTR still latched,
         * instead of producing Disconnected. Allow ordinary short suspends;
         * after sustained bus silence release the stale role and rediscover.
         * A genuinely sleeping host may re-enumerate after this timeout. */
        unsigned link_state = (qdwc3_rd(p->base + DSTS) >> 18) & 15u;
        if (link_state == 3 && p->device.stats.configured) {
            if (!p->suspended_since) p->suspended_since = now;
            if (now - p->suspended_since >= SUSPEND_RECOVERY_US) down = 1;
        } else {
            p->suspended_since = 0;
        }
        st->connected = p->device.stats.configured && !down;
        if (p->device.stats.configured) {
            if (!down) { p->down_since = 0; p->failures = 0; return; }
            if (!p->down_since) p->down_since = now;
            if (now - p->down_since < DISCONNECT_US) return;
            st->disconnects++;
        } else {
            /* Do not kill a first enumeration merely because the link starts
             * disconnected. Setup/reset activity earns time, bounded overall. */
            if (activity != p->activity) {
                p->deadline = now + 8000000u;
                p->activity = activity;
            }
            if (now < p->deadline && now - p->started < 30000000u) return;
        }
        if (stop_port(s, i, 0)) { failed(s, i, -12); return; }
        p->next_role = Q1N1_USB_HOST;
        p->retry_at = now + 100000u;
        return;
    }
    if (p->ncm.stats.init_step == 9 && p->ncm.x) {
        st->connected = host_present(p);
        if (st->connected) { p->down_since = 0; p->failures = 0; return; }
        if (!p->down_since) p->down_since = now;
        if (now - p->down_since < DISCONNECT_US) return;
        st->disconnects++;
        if (stop_port(s, i, 0)) { failed(s, i, -13); return; }
        p->next_role = Q1N1_USB_HOST;
        p->retry_at = now + 100000u;
        return;
    }
    unsigned connected = 0;
    for (unsigned n = 1; n <= p->host.max_ports; n++)
        if (xhci_portsc(&p->host, n) & 1u) { connected = n; break; }
    if (connected) {
        int result = ncm_open(&p->ncm, &p->host, 0, 1);
        if (result) { failed(s, i, result); return; }
        ncm_proxy_init(&p->proxy, &p->ncm, 0);
        ncm_proxy_announce(&p->proxy);
        st->connected = 1;
        p->failures = 0;
    } else if (now >= p->deadline) {
        if (stop_port(s, i, 0)) { failed(s, i, -14); return; }
        p->next_role = Q1N1_USB_DEVICE;
        p->retry_at = now + 100000u;
    }
}

void q1n1_usb_ports_tick(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) service_port(s, i);
    s->busy = 0;
}

void q1n1_usb_ports_poll(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        service_port(s, i);
        maintain_port(s, i);
    }
    s->busy = 0;
}

static void io_poll(void *ctx) { q1n1_usb_ports_poll(ctx); }
static int io_ready(void *ctx)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->selected >= 0) return port_ready(s, (unsigned)s->selected);
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) if (port_ready(s, i)) return 1;
    return 0;
}
static size_t io_read(void *ctx, uint8_t *buffer, size_t size)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->busy) return 0;
    s->busy = 1;
    size_t got = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if ((s->selected >= 0 && s->selected != (int)i) || !port_ready(s, i)) continue;
        struct q1n1_usb_port *p = &s->port[i];
        got = s->status[i].role == Q1N1_USB_DEVICE
            ? qdwc3_read(&p->device, 0, buffer, size)
            : p->proxy.io.read(p->proxy.io.ctx, buffer, size);
        if (got) { s->selected = (int)i; break; }
    }
    s->busy = 0;
    return got;
}
static size_t io_write(void *ctx, const uint8_t *buffer, size_t size)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->busy) return 0;
    s->busy = 1;
    size_t sent = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if ((s->selected >= 0 && s->selected != (int)i) || !port_ready(s, i)) continue;
        struct q1n1_usb_port *p = &s->port[i];
        sent = s->status[i].role == Q1N1_USB_DEVICE
            ? qdwc3_write(&p->device, 0, buffer, size)
            : p->proxy.io.write(p->proxy.io.ctx, buffer, size);
        break;
    }
    s->busy = 0;
    return sent;
}
static void io_end(void *ctx) { ((struct q1n1_usb_ports *)ctx)->selected = -1; }
static int *io_checksum_state(void *ctx)
{
    struct q1n1_usb_ports *s = ctx;
    return &s->port[s->selected >= 0 ? (unsigned)s->selected : 0].checksum_state;
}

struct q1n1_usb_ports *q1n1_usb_ports_init(uint8_t *arena, uint64_t size, int adopt)
{
    if (!arena || ((uintptr_t)arena & 0xffff) || size < Q1N1_USB_ARENA_SIZE) return NULL;
    struct q1n1_usb_ports *s = (void *)arena;
    int valid = adopt && s->magic == PORTS_MAGIC && s->version == PORTS_VERSION && s->size == sizeof(*s);
    if (!valid) memset(s, 0, sizeof(*s));
    s->busy = 1;
    s->selected = -1;
    s->io = (struct q1n1_io){.read = io_read, .write = io_write, .poll = io_poll,
                           .ready = io_ready, .end = io_end, .ctx = s,
                           .checksum_state = io_checksum_state};
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        struct q1n1_usb_port *p = &s->port[i];
        struct q1n1_usb_port_status *st = &s->status[i];
        uintptr_t base = i ? 0x0a800000u : 0x0a600000u;
        /* Shared pointers are valid only for this exact arena and layout. */
        int keep = valid && p->base == base && p->host.base == base &&
                   p->ncm.x == &p->host && st->role == Q1N1_USB_HOST &&
                   p->host.dma == arena + i * HOST_SLOT_BYTES + 0x10000 + QDWC3_ARENA_SIZE &&
                   ((qdwc3_rd(base + GCTL) >> 12) & 3) == 1 &&
                   xhci_running(&p->host) && host_present(p);
        if (!keep) {
            if (valid && p->base == base && st->role != Q1N1_USB_UNSUPPORTED && stop_port(s, i, 0)) {
                st->role = Q1N1_USB_UNSUPPORTED;
                continue;
            }
            memset(p, 0, sizeof(*p));
            memset(st, 0, sizeof(*st));
            p->base = base;
            p->qscratch = base + 0xf8800;
            p->device_dma = arena + i * HOST_SLOT_BYTES + 0x10000;
            p->host_dma = p->device_dma + QDWC3_ARENA_SIZE;
            if (snapshot(p)) { st->role = Q1N1_USB_UNSUPPORTED; continue; }
            p->next_role = ((p->saved.gctl >> 12) & 3) == 1 ? Q1N1_USB_HOST : Q1N1_USB_DEVICE;
        } else {
            ncm_proxy_rebind(&p->proxy, &p->ncm);
            p->proxy.head = p->proxy.tail = 0; /* Drop incomplete old requests. */
        }
        st->base = base;
        st->device_stats = (uintptr_t)&p->device.stats;
        st->host_stats = (uintptr_t)&p->host.stats;
        st->ncm_stats = (uintptr_t)&p->ncm.stats;
        st->proxy_stats = (uintptr_t)&p->proxy.stats;
    }
    s->magic = PORTS_MAGIC;
    s->version = PORTS_VERSION;
    s->size = sizeof(*s);
    s->busy = 0;
    return s;
}

int q1n1_usb_ports_handoff(struct q1n1_usb_ports *s)
{
    if (!s) return 0;
    s->busy = 1;
    int result = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++)
        if (s->status[i].role == Q1N1_USB_DEVICE && stop_port(s, i, 0)) result = -1;
    s->selected = -1;
    s->busy = 0;
    return result;
}
void q1n1_usb_ports_stop(struct q1n1_usb_ports *s)
{
    if (!s) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) stop_port(s, i, 1);
    s->magic = 0;
    s->busy = 0;
}
uint64_t q1n1_usb_ports_refresh(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return 1;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if (s->selected == (int)i || s->status[i].connected) continue;
        if (s->status[i].role == Q1N1_USB_UNSUPPORTED) continue;
        if (stop_port(s, i, 0)) { s->busy = 0; return 2; }
        s->port[i].retry_at = 0;
    }
    s->busy = 0;
    return 0;
}
