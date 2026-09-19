/* SPDX-License-Identifier: MIT */
/* See xhci.h. Ported from tools/a16xhci.py; the register order, ring
 * arithmetic and command sequence deliberately match it line for line. */
#include "xhci.h"

/* Operational registers, offsets from `op`. */
#define USBCMD 0x00
#define USBSTS 0x04
#define PAGESIZE 0x08
#define CRCR 0x18
#define DCBAAP 0x30
#define CONFIG 0x38
#define PORTSC(n) (0x400 + 0x10 * ((n) - 1))

#define CMD_RS (1u << 0)
#define CMD_HCRST (1u << 1)
#define STS_HCH (1u << 0)
#define STS_CNR (1u << 11)

/* Interrupter 0, offsets from `ir0`. */
#define IMOD 0x04
#define ERSTSZ 0x08
#define ERSTBA 0x10
#define ERDP 0x18

#define P_CCS (1u << 0)
#define P_PED (1u << 1)
#define P_PR (1u << 4)
#define P_PP (1u << 9)
#define P_CSC (1u << 17)
#define P_PEC (1u << 18)
#define P_WRC (1u << 19)
#define P_OCC (1u << 20)
#define P_PRC (1u << 21)
#define P_PLC (1u << 22)
#define P_CEC (1u << 23)
/* PED is write-1-to-disable and the change bits are write-1-to-clear, so every
 * read-modify-write of PORTSC has to mask all of them out. */
#define PORTSC_RW (~(P_PED | P_CSC | P_PEC | P_WRC | P_OCC | P_PRC | P_PLC | P_CEC))

#define TRB_NORMAL 1
#define TRB_SETUP 2
#define TRB_DATA 3
#define TRB_STATUS 4
#define TRB_LINK 6
#define TRB_ENABLE_SLOT 9
#define TRB_DISABLE_SLOT 10
#define TRB_ADDRESS_DEVICE 11
#define TRB_CONFIGURE_ENDPOINT 12
#define TRB_TRANSFER_EVENT 32
#define TRB_COMMAND_COMPLETION 33
#define TRB_PORT_STATUS_CHANGE 34

static void barrier(void) { __asm__ volatile("dsb sy" ::: "memory"); }

void *memcpy(void *dest, const void *src, size_t n);
void *memset(void *dest, int value, size_t n);

static void write_trb(uint8_t *slot, uint64_t parameter, uint32_t status, uint32_t control)
{
    volatile uint32_t *word = (volatile uint32_t *)slot;
    word[0] = (uint32_t)parameter;
    word[1] = (uint32_t)(parameter >> 32);
    word[2] = status;
    barrier();
    /* The cycle bit lives in the control word, so it must land last: the
     * controller may be looking at this TRB the instant it flips. */
    word[3] = control;
    barrier();
}

uint8_t *xhci_alloc(struct xhci *x, uint64_t size, uint64_t alignment)
{
    uint64_t offset = (x->dma_used + alignment - 1) & ~(alignment - 1);
    if (offset + size > x->dma_size) return NULL;
    x->dma_used = offset + size;
    memset(x->dma + offset, 0, (size_t)size);
    return x->dma + offset;
}

static void ring_init(struct xhci_ring *ring, uint8_t *base, uint32_t count)
{
    ring->base = base;
    ring->count = count;
    ring->index = 0;
    ring->cycle = 1;
    memset(base, 0, count * XHCI_TRB_SIZE);
    /* Close the ring with a Link TRB that toggles the cycle each lap. */
    write_trb(base + (count - 1) * XHCI_TRB_SIZE, (uint64_t)(uintptr_t)base, 0,
              (TRB_LINK << 10) | (1u << 1) | 1u);
}

static uint8_t *ring_push(struct xhci_ring *ring, uint64_t parameter, uint32_t status,
                          uint32_t control)
{
    uint8_t *slot = ring->base + ring->index * XHCI_TRB_SIZE;
    write_trb(slot, parameter, status, (control & ~1u) | ring->cycle);
    ring->index++;
    if (ring->index == ring->count - 1) {
        write_trb(ring->base + (ring->count - 1) * XHCI_TRB_SIZE,
                  (uint64_t)(uintptr_t)ring->base, 0,
                  (TRB_LINK << 10) | (1u << 1) | ring->cycle);
        ring->index = 0;
        ring->cycle ^= 1;
    }
    return slot;
}

int xhci_poll(struct xhci *x, struct xhci_event *event)
{
    volatile uint32_t *trb = (volatile uint32_t *)(x->event + x->event_index * XHCI_TRB_SIZE);
    uint32_t control = trb[3];
    if ((control & 1u) != x->event_cycle) return 0;
    event->parameter = (uint64_t)trb[0] | ((uint64_t)trb[1] << 32);
    event->status = trb[2];
    event->control = control;
    event->type = (control >> 10) & 0x3f;
    event->code = (event->status >> 24) & 0xff;

    x->event_index++;
    if (x->event_index == XHCI_EVENT_TRBS) {
        x->event_index = 0;
        x->event_cycle ^= 1;
    }
    /* EHB (bit 3) clears the Event Handler Busy flag as the pointer advances. */
    xhci_wr64(x->ir0 + ERDP,
              (uint64_t)(uintptr_t)(x->event + x->event_index * XHCI_TRB_SIZE) | (1u << 3));
    x->stats.events++;
    x->stats.last_completion = event->code;
    if (event->type == TRB_PORT_STATUS_CHANGE) {
        x->stats.port_changes++;
        x->stats.last_port = (uint32_t)((event->parameter >> 24) & 0xff);
    }
    return 1;
}

/* Waiting for one specific completion must not throw away the others: a bulk
 * transfer can finish while a command is outstanding. Unmatched events go to a
 * small cache that the next waiter checks before it polls. */
static void defer(struct xhci *x, const struct xhci_event *event)
{
    if (x->deferred_count < XHCI_DEFERRED) {
        x->deferred[x->deferred_count++] = *event;
        return;
    }
    for (uint32_t n = 1; n < XHCI_DEFERRED; n++) x->deferred[n - 1] = x->deferred[n];
    x->deferred[XHCI_DEFERRED - 1] = *event;
}

static int take_deferred(struct xhci *x, uint32_t type, uint64_t trb, struct xhci_event *out)
{
    for (uint32_t n = 0; n < x->deferred_count; n++) {
        if (x->deferred[n].type != type || x->deferred[n].parameter != trb) continue;
        *out = x->deferred[n];
        for (uint32_t k = n + 1; k < x->deferred_count; k++) x->deferred[k - 1] = x->deferred[k];
        x->deferred_count--;
        return 1;
    }
    return 0;
}

/* Wait for the event that belongs to `trb`. 0 = it never arrived. */
static int await_event(struct xhci *x, uint32_t type, uint64_t trb,
                       struct xhci_event *out, uint32_t timeout_ms)
{
    if (take_deferred(x, type, trb, out)) return 1;
    uint64_t deadline = xhci_now_us() + (uint64_t)timeout_ms * 1000;
    struct xhci_event event;
    do {
        while (xhci_poll(x, &event)) {
            if (event.type == type && event.parameter == trb) {
                *out = event;
                return 1;
            }
            defer(x, &event);
        }
        xhci_delay_us(50);
    } while (xhci_now_us() < deadline);
    return 0;
}

static int command(struct xhci *x, uint64_t parameter, uint32_t control,
                   struct xhci_event *out, uint32_t timeout_ms)
{
    uint8_t *slot = ring_push(&x->cmd, parameter, 0, control);
    barrier();
    xhci_wr32(x->db, 0);
    x->stats.commands++;
    if (!await_event(x, TRB_COMMAND_COMPLETION, (uint64_t)(uintptr_t)slot, out, timeout_ms)) {
        x->stats.command_failures++;
        return -1;
    }
    if (out->code != XHCI_SUCCESS) {
        x->stats.command_failures++;
        return -2;
    }
    return 0;
}

static int wait_bits(struct xhci *x, uint32_t offset, uint32_t mask, uint32_t want,
                     uint32_t timeout_ms)
{
    uint64_t deadline = xhci_now_us() + (uint64_t)timeout_ms * 1000;
    do {
        if ((xhci_rd32(x->op + offset) & mask) == want) return 0;
        xhci_delay_us(100);
    } while (xhci_now_us() < deadline);
    return -1;
}

int xhci_running(struct xhci *x)
{
    if (!x->base || !x->op) return 0;
    return !(xhci_rd32(x->op + USBSTS) & STS_HCH);
}

void xhci_stop(struct xhci *x)
{
    if (!(xhci_rd32(x->op + USBSTS) & STS_HCH)) {
        xhci_wr32(x->op + USBCMD, xhci_rd32(x->op + USBCMD) & ~CMD_RS);
        wait_bits(x, USBSTS, STS_HCH, STS_HCH, 2000);
    }
}

int xhci_init(struct xhci *x, uintptr_t base, uint8_t *dma, uint64_t dma_size)
{
    memset(x, 0, sizeof(*x));
    x->base = base;
    x->dma = dma;
    x->dma_size = dma_size;

    uint32_t cap = xhci_rd32(base);
    x->caplength = cap & 0xff;
    x->hciversion = (cap >> 16) & 0xffff;
    uint32_t hcs1 = xhci_rd32(base + 0x04);
    uint32_t hcs2 = xhci_rd32(base + 0x08);
    uint32_t hcc1 = xhci_rd32(base + 0x10);
    x->op = base + x->caplength;
    x->runtime = base + (xhci_rd32(base + 0x18) & ~0x1fu);
    x->db = base + (xhci_rd32(base + 0x14) & ~0x3u);
    x->ir0 = x->runtime + 0x20;
    x->max_slots = hcs1 & 0xff;
    x->max_ports = (hcs1 >> 24) & 0xff;
    x->context_size = (hcc1 >> 2) & 1 ? 64 : 32;
    x->ac64 = hcc1 & 1;
    x->scratchpads = (((hcs2 >> 21) & 0x1f) << 5) | ((hcs2 >> 27) & 0x1f);
    if (!x->caplength || !x->max_slots || !x->max_ports) return -1;
    x->stats.init_step = 1;

    xhci_stop(x);
    xhci_wr32(x->op + USBCMD, CMD_HCRST);
    if (wait_bits(x, USBCMD, CMD_HCRST, 0, 2000)) return -2;
    if (wait_bits(x, USBSTS, STS_CNR, 0, 2000)) return -3;
    x->page_size = (xhci_rd32(x->op + PAGESIZE) & 0xffff) << 12;
    if (!x->page_size) x->page_size = 4096;
    x->stats.init_step = 2;

    /* The reset cleared the controller's view of memory, so the arena starts
     * over too and every structure below is freshly zeroed. */
    x->dma_used = 0;
    x->dcbaa = (uint64_t *)xhci_alloc(x, (x->max_slots + 1) * 8, 64);
    uint8_t *cmd_ring = xhci_alloc(x, XHCI_CMD_TRBS * XHCI_TRB_SIZE, 64);
    x->event = xhci_alloc(x, XHCI_EVENT_TRBS * XHCI_TRB_SIZE, 64);
    x->erst = xhci_alloc(x, 16, 64);
    if (!x->dcbaa || !cmd_ring || !x->event || !x->erst) return -4;

    if (x->scratchpads) {
        x->scratch_array = xhci_alloc(x, x->scratchpads * 8, 64);
        if (!x->scratch_array) return -4;
        for (uint32_t n = 0; n < x->scratchpads; n++) {
            uint8_t *page = xhci_alloc(x, x->page_size, x->page_size);
            if (!page) return -4;
            ((uint64_t *)x->scratch_array)[n] = (uint64_t)(uintptr_t)page;
        }
        x->dcbaa[0] = (uint64_t)(uintptr_t)x->scratch_array;
    }

    ring_init(&x->cmd, cmd_ring, XHCI_CMD_TRBS);
    x->event_index = 0;
    x->event_cycle = 1;
    x->deferred_count = 0;

    /* ERST entry: segment base, then the segment size in TRBs. */
    ((volatile uint64_t *)x->erst)[0] = (uint64_t)(uintptr_t)x->event;
    ((volatile uint32_t *)x->erst)[2] = XHCI_EVENT_TRBS;
    ((volatile uint32_t *)x->erst)[3] = 0;
    barrier();

    xhci_wr64(x->op + DCBAAP, (uint64_t)(uintptr_t)x->dcbaa);
    xhci_wr32(x->op + CONFIG, x->max_slots);
    xhci_wr64(x->op + CRCR, (uint64_t)(uintptr_t)x->cmd.base | 1u);

    /* ERSTSZ and ERDP before ERSTBA: writing ERSTBA is what makes the
     * controller load the table. */
    xhci_wr32(x->ir0 + ERSTSZ, 1);
    xhci_wr64(x->ir0 + ERDP, (uint64_t)(uintptr_t)x->event);
    xhci_wr64(x->ir0 + ERSTBA, (uint64_t)(uintptr_t)x->erst);
    xhci_wr32(x->ir0 + IMOD, 0);
    barrier();
    x->stats.init_step = 3;

    xhci_wr32(x->op + USBCMD, xhci_rd32(x->op + USBCMD) | CMD_RS);
    if (wait_bits(x, USBSTS, STS_HCH, 0, 2000)) return -5;
    x->stats.init_step = 4;
    return 0;
}

uint32_t xhci_portsc(struct xhci *x, uint32_t port)
{
    return xhci_rd32(x->op + PORTSC(port));
}

static void portsc_write(struct xhci *x, uint32_t port, uint32_t bits)
{
    xhci_wr32(x->op + PORTSC(port), (xhci_portsc(x, port) & PORTSC_RW) | bits);
}

uint32_t xhci_wait_port(struct xhci *x, uint32_t timeout_ms)
{
    uint64_t deadline = xhci_now_us() + (uint64_t)timeout_ms * 1000;
    struct xhci_event event;
    uint32_t port = 0;
    do {
        while (xhci_poll(x, &event)) defer(x, &event);
        for (uint32_t n = 1; n <= x->max_ports && !port; n++)
            if (xhci_portsc(x, n) & P_CCS) port = n;
        if (port) break;
        xhci_delay_us(1000);
    } while (xhci_now_us() < deadline);
    if (!port) return 0;

    /* A previous controller instance can leave PRC/PED latched. Clear the old
     * completion before requesting a new reset; stale PED is not proof that
     * this reset completed or that the device is at address zero. */
    portsc_write(x, port, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC);
    portsc_write(x, port, P_PR);
    uint64_t reset_deadline = xhci_now_us() + 1000 * 1000;
    int reset_complete = 0;
    while (xhci_now_us() < reset_deadline) {
        uint32_t status = xhci_portsc(x, port);
        if (!(status & P_CCS)) return 0;
        if (!(status & P_PR) && (status & P_PED) && (status & (P_PRC | P_WRC))) {
            reset_complete = 1;
            break;
        }
        xhci_delay_us(1000);
    }
    if (!reset_complete) return 0;
    portsc_write(x, port, P_PRC | P_WRC | P_CSC | P_PEC | P_PLC);
    while (xhci_poll(x, &event)) defer(x, &event);
    if (!(xhci_portsc(x, port) & P_PED)) return 0;
    xhci_delay_us(100 * 1000);      /* USB reset recovery before SET_ADDRESS */
    return port;
}

/* Input contexts are shifted by one: the input control context sits at 0, so
 * the slot context is at 1 and endpoint DCI n is at n + 1. */
static uint8_t *input_slot(struct xhci *x, struct xhci_device *d, uint32_t dci)
{
    return d->input_context + x->context_size * (dci + 1);
}

int xhci_address_device(struct xhci *x, struct xhci_device *d, uint32_t port)
{
    struct xhci_event event;
    memset(d, 0, sizeof(*d));
    d->port = port;
    d->speed = (xhci_portsc(x, port) >> 10) & 0xf;

    if (command(x, 0, TRB_ENABLE_SLOT << 10, &event, 1000)) return -1;
    d->slot = (event.control >> 24) & 0xff;
    if (!d->slot || d->slot > x->max_slots) return -2;

    d->device_context = xhci_alloc(x, x->context_size * 32, 64);
    d->input_context = xhci_alloc(x, x->context_size * 33, 64);
    uint8_t *ep0 = xhci_alloc(x, XHCI_EP_TRBS * XHCI_TRB_SIZE, 64);
    if (!d->device_context || !d->input_context || !ep0) return -3;
    ring_init(&d->ep[1], ep0, XHCI_EP_TRBS);

    /* Low and full speed devices report their real EP0 size in the descriptor;
     * 8 is the only size guaranteed to work before that is known. */
    d->packet0 = d->speed == 4 || d->speed == 5 ? 512 : (d->speed == 3 ? 64 : 8);

    volatile uint32_t *control = (volatile uint32_t *)d->input_context;
    control[0] = 0;
    control[1] = 0x3;                          /* A0: slot context, A1: EP0 */

    volatile uint32_t *slot = (volatile uint32_t *)input_slot(x, d, 0);
    slot[0] = ((d->speed & 0xf) << 20) | (1u << 27);   /* speed, one context entry */
    slot[1] = (port & 0xff) << 16;                     /* root hub port number */
    slot[2] = 0;
    slot[3] = 0;

    volatile uint32_t *endpoint = (volatile uint32_t *)input_slot(x, d, 1);
    endpoint[0] = 0;
    endpoint[1] = (3u << 1) | (4u << 3) | (d->packet0 << 16);   /* CErr 3, control */
    endpoint[2] = (uint32_t)(uintptr_t)d->ep[1].base | 1u;      /* dequeue + DCS */
    endpoint[3] = (uint32_t)((uint64_t)(uintptr_t)d->ep[1].base >> 32);
    endpoint[4] = 8;                                            /* average TRB length */
    barrier();

    x->dcbaa[d->slot] = (uint64_t)(uintptr_t)d->device_context;
    barrier();

    if (command(x, (uint64_t)(uintptr_t)d->input_context,
                (TRB_ADDRESS_DEVICE << 10) | (d->slot << 24), &event, 2000)) {
        command(x, 0, (TRB_DISABLE_SLOT << 10) | (d->slot << 24), &event, 1000);
        d->slot = 0;
        return -4;
    }
    return 0;
}

int xhci_configure_endpoints(struct xhci *x, struct xhci_device *d, const uint32_t *dci,
                             const uint32_t *type, const uint32_t *packet, uint32_t count)
{
    struct xhci_event event;
    uint32_t add = 1;               /* A0: the slot context changes too */
    uint32_t highest = 1;

    memset(d->input_context, 0, x->context_size * 33);
    for (uint32_t n = 0; n < count; n++) {
        if (dci[n] >= XHCI_MAX_DCI) return -1;
        add |= 1u << dci[n];
        if (dci[n] > highest) highest = dci[n];
        uint8_t *ring = xhci_alloc(x, XHCI_EP_TRBS * XHCI_TRB_SIZE, 64);
        if (!ring) return -2;
        ring_init(&d->ep[dci[n]], ring, XHCI_EP_TRBS);
        d->pending[dci[n]] = 0;

        volatile uint32_t *endpoint = (volatile uint32_t *)input_slot(x, d, dci[n]);
        endpoint[0] = 0;
        endpoint[1] = (3u << 1) | (type[n] << 3) | (packet[n] << 16);
        endpoint[2] = (uint32_t)(uintptr_t)ring | 1u;
        endpoint[3] = (uint32_t)((uint64_t)(uintptr_t)ring >> 32);
        endpoint[4] = packet[n];
    }

    volatile uint32_t *control = (volatile uint32_t *)d->input_context;
    control[0] = 0;
    control[1] = add;

    /* Carry the addressed slot context forward, raising Context Entries. */
    memcpy(input_slot(x, d, 0), d->device_context, 32);
    volatile uint32_t *slot = (volatile uint32_t *)input_slot(x, d, 0);
    slot[0] = (slot[0] & 0x07ffffffu) | (highest << 27);
    barrier();

    return command(x, (uint64_t)(uintptr_t)d->input_context,
                   (TRB_CONFIGURE_ENDPOINT << 10) | (d->slot << 24), &event, 2000);
}

int xhci_control(struct xhci *x, struct xhci_device *d, uint32_t request_type,
                 uint32_t request, uint32_t value, uint32_t index,
                 uint8_t *buffer, uint32_t length, uint32_t timeout_ms)
{
    struct xhci_event event;
    uint32_t incoming = request_type & 0x80;
    uint32_t trt = length ? (incoming ? 3 : 2) : 0;
    uint64_t setup = (uint64_t)(request_type & 0xff) | ((uint64_t)(request & 0xff) << 8) |
                     ((uint64_t)(value & 0xffff) << 16) | ((uint64_t)(index & 0xffff) << 32) |
                     ((uint64_t)(length & 0xffff) << 48);

    ring_push(&d->ep[1], setup, 8, (TRB_SETUP << 10) | (trt << 16) | (1u << 6));
    if (length)
        ring_push(&d->ep[1], (uint64_t)(uintptr_t)buffer, length,
                  (TRB_DATA << 10) | (incoming ? (1u << 16) : 0));
    uint8_t *last = ring_push(&d->ep[1], 0, 0,
                              (TRB_STATUS << 10) | (incoming ? 0 : (1u << 16)) | (1u << 5));
    barrier();
    xhci_wr32(x->db + 4 * d->slot, 1);
    x->stats.transfers++;

    if (!await_event(x, TRB_TRANSFER_EVENT, (uint64_t)(uintptr_t)last, &event, timeout_ms)) {
        x->stats.transfer_failures++;
        return -1;
    }
    if (event.code != XHCI_SUCCESS && event.code != XHCI_SHORT_PACKET) {
        x->stats.transfer_failures++;
        if (event.code == 6) x->stats.stalls++;
        return -2 - (int)event.code;
    }
    return (int)(length - (event.status & 0xffffff));
}

int xhci_post(struct xhci *x, struct xhci_device *d, uint32_t dci,
              uint8_t *buffer, uint32_t length)
{
    if (dci >= XHCI_MAX_DCI || !d->ep[dci].base) return -1;
    if (d->pending[dci]) return 0;      /* one outstanding TRB per endpoint */
    uint8_t *slot = ring_push(&d->ep[dci], (uint64_t)(uintptr_t)buffer, length,
                              (TRB_NORMAL << 10) | (1u << 5) | (1u << 2));  /* IOC, ISP */
    barrier();
    xhci_wr32(x->db + 4 * d->slot, dci);
    d->pending[dci] = (uint64_t)(uintptr_t)slot;
    x->stats.transfers++;
    return 1;
}

int xhci_reap(struct xhci *x, struct xhci_device *d, uint32_t dci, uint32_t length)
{
    if (dci >= XHCI_MAX_DCI || !d->pending[dci]) return 0;
    struct xhci_event event;
    if (!await_event(x, TRB_TRANSFER_EVENT, d->pending[dci], &event, 0)) return 0;
    d->pending[dci] = 0;
    if (event.code != XHCI_SUCCESS && event.code != XHCI_SHORT_PACKET) {
        x->stats.transfer_failures++;
        if (event.code == 6) x->stats.stalls++;
        return -1;
    }
    uint32_t moved = length - (event.status & 0xffffff);
    x->stats.rx_bytes += moved;
    return (int)moved;
}

int xhci_send(struct xhci *x, struct xhci_device *d, uint32_t dci,
              uint8_t *buffer, uint32_t length, uint32_t timeout_ms)
{
    struct xhci_event event;
    if (dci >= XHCI_MAX_DCI || !d->ep[dci].base) return -1;
    uint8_t *slot = ring_push(&d->ep[dci], (uint64_t)(uintptr_t)buffer, length,
                              (TRB_NORMAL << 10) | (1u << 5) | (1u << 2));
    barrier();
    xhci_wr32(x->db + 4 * d->slot, dci);
    x->stats.transfers++;
    if (!await_event(x, TRB_TRANSFER_EVENT, (uint64_t)(uintptr_t)slot, &event, timeout_ms)) {
        x->stats.transfer_failures++;
        return -2;
    }
    if (event.code != XHCI_SUCCESS && event.code != XHCI_SHORT_PACKET) {
        x->stats.transfer_failures++;
        return -3;
    }
    x->stats.tx_bytes += length;

    /* A block that is an exact multiple of the packet size needs a zero-length
     * packet, or the far end keeps waiting for more of it. */
    if (length && (length % 512) == 0) {
        slot = ring_push(&d->ep[dci], 0, 0, (TRB_NORMAL << 10) | (1u << 5));
        barrier();
        xhci_wr32(x->db + 4 * d->slot, dci);
        if (!await_event(x, TRB_TRANSFER_EVENT, (uint64_t)(uintptr_t)slot, &event, timeout_ms))
            return -4;
    }
    return (int)length;
}

void xhci_release(struct xhci *x, struct xhci_device *d)
{
    struct xhci_event event;
    if (!d->slot || !d->ep[1].base) return;
    /* Unwind before letting go: a device left configured when the controller is
     * reset under it can refuse to enumerate again until it is replugged. */
    xhci_control(x, d, 0x00, 9, 0, 0, NULL, 0, 500);
    command(x, 0, (TRB_DISABLE_SLOT << 10) | (d->slot << 24), &event, 500);
    x->dcbaa[d->slot] = 0;
    d->slot = 0;
}
