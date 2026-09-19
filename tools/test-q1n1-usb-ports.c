/* SPDX-License-Identifier: MIT */
/* Deterministic lifecycle model: real usb-ports.c, mocked controller drivers.
 * Covers two independent cables, stale configured flags, role reversal,
 * disconnect recovery, DMA ownership, and compatible stage handoff. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "usb-ports.h"

struct model {
    unsigned partner, dtr, bad_id, halt_failure, writes, suspended;
    unsigned device_starts, host_starts, opens, rebinds;
    uint32_t gctl, dctl, cmd, status;
    uint8_t input[32];
    unsigned head, tail;
} model[2];
static uint64_t now;
static void *arena;
static unsigned checks;
#define CHECK(x) do { assert(x); checks++; } while (0)

static unsigned index_of(uintptr_t base)
{
    assert(base == 0x0a600000 || base == 0x0a800000);
    return base == 0x0a800000;
}
uint64_t xhci_now_us(void) { return now; }
void qdwc3_delay_us(uint64_t us) { now += us; }
uint32_t qdwc3_rd(uintptr_t address)
{
    uintptr_t base = address & ~0x1fffffull;
    struct model *m = &model[index_of(base)];
    switch (address - base) {
    case 0: return 0x1200030;
    case 4: return 0x2000340;
    case 0x30: return m->cmd;
    case 0x34: return m->status;
    case 0xc110: return m->gctl;
    case 0xc120: return m->bad_id ? 0 : 0x33313130;
    case 0xc704: return m->dctl;
    case 0xc70c: return (m->dctl & (1u << 31)) ? m->suspended << 18 : (1u << 22);
    default: return 0;
    }
}
void qdwc3_wr(uintptr_t address, uint32_t value)
{
    uintptr_t base = address & ~0x1fffffull;
    struct model *m = &model[index_of(base)];
    m->writes++;
    switch (address - base) {
    case 0xc110: m->gctl = value; break;
    case 0xc704: m->dctl = value; break;
    case 0x30: m->cmd = value; if (!m->halt_failure) m->status = (value & 1) ? 0 : 1; break;
    default: break;
    }
}
int qdwc3_takeover(struct qdwc3 *d, uintptr_t base, uintptr_t qs,
                   const struct qdwc3_saved *saved, uint8_t *dma, uint64_t size)
{
    struct model *m = &model[index_of(base)];
    assert(((m->gctl >> 12) & 3) == 2);
    assert(size == QDWC3_ARENA_SIZE);
    assert(dma >= (uint8_t *)arena && dma + size <= (uint8_t *)arena + Q1N1_USB_ARENA_SIZE);
    memset(d, 0, sizeof(*d));
    d->regs = base; d->qscratch = qs; d->saved = saved; d->arena = dma;
    d->stats.init_step = 8;
    m->dctl = 1u << 31;
    m->device_starts++;
    return 0;
}
void qdwc3_poll(struct qdwc3 *d)
{
    struct model *m = &model[index_of(d->regs)];
    if (m->partner == Q1N1_USB_HOST) d->stats.configured = 1;
    /* Deliberately leave configured latched when unplugged. */
}
int qdwc3_link_down(const struct qdwc3 *d)
{ return model[index_of(d->regs)].partner != Q1N1_USB_HOST; }
int qdwc3_ready(const struct qdwc3 *d, unsigned pipe)
{ return !pipe && d->stats.configured && model[index_of(d->regs)].dtr; }
void qdwc3_stop(struct qdwc3 *d) { model[index_of(d->regs)].dctl = 0; }
static size_t input(unsigned i, uint8_t *buffer, size_t count)
{
    struct model *m = &model[i];
    size_t got = 0;
    while (got < count && m->tail < m->head) buffer[got++] = m->input[m->tail++];
    return got;
}
size_t qdwc3_read(struct qdwc3 *d, unsigned pipe, uint8_t *b, size_t n)
{ return pipe ? 0 : input(index_of(d->regs), b, n); }
size_t qdwc3_write(struct qdwc3 *d, unsigned pipe, const uint8_t *b, size_t n)
{ (void)d; (void)pipe; (void)b; return n; }
int xhci_init(struct xhci *x, uintptr_t base, uint8_t *dma, uint64_t size)
{
    struct model *m = &model[index_of(base)];
    assert(((m->gctl >> 12) & 3) == 1);
    assert(dma >= (uint8_t *)arena && dma + size <= (uint8_t *)arena + Q1N1_USB_ARENA_SIZE);
    memset(x, 0, sizeof(*x));
    x->base = base; x->max_ports = 2; x->dma = dma; x->stats.init_step = 4;
    m->status = 0; m->cmd = 1; m->host_starts++;
    return 0;
}
uint32_t xhci_portsc(struct xhci *x, uint32_t port)
{ return port == 1 && model[index_of(x->base)].partner == Q1N1_USB_DEVICE ? 0xe03 : 0x2a0; }
int xhci_running(struct xhci *x) { return model[index_of(x->base)].cmd & 1; }
int ncm_open(struct ncm *n, struct xhci *x, uint32_t which, uint32_t wait)
{
    (void)which; (void)wait;
    memset(n, 0, sizeof(*n)); n->x = x; n->stats.init_step = 9;
    n->device.port = 1; n->device.slot = 1;
    model[index_of(x->base)].opens++;
    return 0;
}
void ncm_close(struct ncm *n) { n->x = NULL; }
static int net_ready(void *ctx) { return ((struct ncm_proxy *)ctx)->have_peer; }
static void net_poll(void *ctx) { (void)ctx; }
static size_t net_read(void *ctx, uint8_t *b, size_t n)
{ return input(index_of(((struct ncm_proxy *)ctx)->n->x->base), b, n); }
static size_t net_write(void *ctx, const uint8_t *b, size_t n)
{ (void)ctx; (void)b; return n; }
void ncm_proxy_rebind(struct ncm_proxy *p, struct ncm *n)
{
    p->n = n;
    p->io = (struct q1n1_io){.read = net_read, .write = net_write, .ready = net_ready,
                            .poll = net_poll, .ctx = p};
    model[index_of(n->x->base)].rebinds++;
}
void ncm_proxy_init(struct ncm_proxy *p, struct ncm *n, uint64_t timeout)
{ (void)timeout; memset(p, 0, sizeof(*p)); ncm_proxy_rebind(p, n); p->have_peer = 1; }
void ncm_proxy_announce(struct ncm_proxy *p) { (void)p; }

static struct q1n1_usb_ports *reset(unsigned a, unsigned b)
{
    memset(arena, 0, Q1N1_USB_ARENA_SIZE); memset(model, 0, sizeof(model)); now = 1;
    for (unsigned i = 0; i < 2; i++) {
        model[i].gctl = 0x102001; model[i].status = 1; model[i].dtr = 1;
    }
    model[0].partner = a; model[1].partner = b;
    return q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 0);
}
static void advance(struct q1n1_usb_ports *s, uint64_t us)
{ now += us; q1n1_usb_ports_poll(s); }
static void settle(struct q1n1_usb_ports *s)
{ for (unsigned n = 0; n < 50; n++) advance(s, 100000); }

int main(void)
{
    assert(!posix_memalign(&arena, 0x10000, Q1N1_USB_ARENA_SIZE));
    CHECK(q1n1_usb_ports_init((uint8_t *)arena + 1, Q1N1_USB_ARENA_SIZE, 0) == NULL);
    struct q1n1_usb_ports *s = reset(Q1N1_USB_DEVICE, Q1N1_USB_HOST);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_HOST && s->status[0].connected);
    CHECK(s->status[1].role == Q1N1_USB_DEVICE && s->status[1].connected);
    CHECK(model[1].device_starts == 1);
    CHECK(s->port[0].host_dma + Q1N1_USB_HOST_ARENA <= s->port[1].device_dma);
    model[0].input[0] = 0x11; model[0].input[1] = 0x22; model[0].head = 2;
    model[1].input[0] = 0x33; model[1].input[1] = 0x44; model[1].head = 2;
    uint8_t byte;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x11);
    *s->io.checksum_state(s) = 1;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x22);
    CHECK(s->io.read(s, &byte, 1) == 0); /* Cannot splice the other request. */
    s->io.end(s);
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x33);
    CHECK(*s->io.checksum_state(s) == 0 && s->port[0].checksum_state == 1);
    s->io.end(s);
    unsigned starts = model[1].device_starts;
    model[1].dtr = 0;
    advance(s, 31000000);
    CHECK(s->status[1].role == Q1N1_USB_DEVICE && model[1].device_starts == starts);
    model[1].dtr = 1;
    model[0].partner = 0;
    advance(s, 1); advance(s, 600000);
    CHECK(s->status[0].role == Q1N1_USB_OFF && s->port[0].ncm.x == NULL);
    CHECK(s->status[0].disconnects == 1 && model[1].device_starts == starts);
    model[0].partner = Q1N1_USB_DEVICE;
    settle(s);
    CHECK(s->status[0].connected && model[0].opens == 2);
    unsigned hosts = model[0].host_starts;
    q1n1_usb_ports_handoff(s);
    s = q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 1);
    CHECK(model[0].host_starts == hosts && s->status[0].connected);
    CHECK(s->port[0].proxy.io.ctx == &s->port[0].proxy);
    settle(s);
    CHECK(s->status[1].connected);
    model[1].partner = 0;
    advance(s, 1); advance(s, 600000);
    CHECK(s->status[1].role == Q1N1_USB_OFF && s->status[1].disconnects == 1);
    model[1].partner = Q1N1_USB_DEVICE;
    settle(s);
    CHECK(s->status[1].role == Q1N1_USB_HOST && s->status[1].connected);
    CHECK(model[0].host_starts == hosts);
    s = reset(Q1N1_USB_HOST, Q1N1_USB_DEVICE);
    settle(s);
    starts = model[0].device_starts;
    model[0].suspended = 3;
    advance(s, 1); advance(s, 1000000);
    CHECK(s->status[0].connected && model[0].device_starts == starts);
    advance(s, 8000000); advance(s, 600000);
    CHECK(s->status[0].role == Q1N1_USB_OFF && s->status[0].disconnects == 1);
    CHECK(s->status[1].connected && model[1].host_starts == 1);
    s = reset(Q1N1_USB_HOST, Q1N1_USB_DEVICE);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_DEVICE && s->status[0].connected);
    CHECK(s->status[1].role == Q1N1_USB_HOST && s->status[1].connected);
    s = reset(0, Q1N1_USB_HOST);
    model[0].bad_id = 1;
    s = q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 0);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_UNSUPPORTED && !model[0].writes);
    CHECK(s->status[1].connected);
    s = reset(0, Q1N1_USB_HOST);
    model[0].halt_failure = 1; model[0].status = 0;
    settle(s);
    CHECK(model[0].gctl == 0x102001 && model[0].device_starts == 0);
    CHECK(s->status[1].connected);
    free(arena);
    printf("PASS %u independent USB-port lifecycle checks\n", checks);
    return 0;
}
