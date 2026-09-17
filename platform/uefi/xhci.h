/* SPDX-License-Identifier: MIT */
/* Polled xHCI host controller driver for q1n1.
 *
 * Ported from the host-side driver in tools/a16xhci.py, which brought this
 * machine's USB1 controller up and enumerated a Mac over the proxy. The two are
 * deliberately the same shape -- same register order, same ring arithmetic,
 * same command sequence -- so a divergence in behaviour can be found by reading
 * them side by side.
 *
 * No interrupts: the event ring is polled, the way qcom-dwc3.c polls its event
 * buffer, so this can be serviced either from a loop or from the EL2 tick.
 *
 * DMA is identity-mapped and cache-coherent on this SoC, so a barrier is enough
 * and no cache maintenance is needed.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>

#define XHCI_CMD_TRBS 64          /* including the Link TRB that closes the ring */
#define XHCI_EVENT_TRBS 64        /* no Link TRB: the ERST segment size wraps it */
#define XHCI_EP_TRBS 64
#define XHCI_TRB_SIZE 16
#define XHCI_MAX_DCI 8            /* EP0 plus three endpoint pairs is plenty here */
#define XHCI_DEFERRED 32          /* events held aside while another is awaited */

/* Completion codes worth naming. */
#define XHCI_SUCCESS 1
#define XHCI_SHORT_PACKET 13

struct xhci_ring {
    uint8_t *base;
    uint32_t count, index, cycle;
};

struct xhci_device {
    uint32_t slot, packet0, port, speed;
    uint8_t *device_context, *input_context;
    struct xhci_ring ep[XHCI_MAX_DCI];
    uint64_t pending[XHCI_MAX_DCI];   /* outstanding TRB address, 0 = idle */
};

struct xhci_stats {
    uint64_t commands, command_failures, transfers, transfer_failures, events;
    uint64_t rx_bytes, tx_bytes, port_changes, stalls;
    uint32_t last_completion, last_port, init_step;
};

/* An event pulled off the ring that the current waiter did not ask for. */
struct xhci_event {
    uint64_t parameter;
    uint32_t status, control, type, code;
};

struct xhci {
    uintptr_t base, op, runtime, db, ir0;
    uint32_t caplength, hciversion, max_slots, max_ports, context_size;
    uint32_t page_size, scratchpads, ac64;
    uint8_t *dma;
    uint64_t dma_size, dma_used;
    uint64_t *dcbaa;
    uint8_t *scratch_array;
    struct xhci_ring cmd;
    uint8_t *event;
    uint32_t event_index, event_cycle;
    uint8_t *erst;
    /* Waiting on one completion must not discard the others: a bulk transfer
     * can finish while a command is outstanding. Unmatched events land here
     * and the next waiter checks the cache before it polls again. */
    struct xhci_event deferred[XHCI_DEFERRED];
    uint32_t deferred_count;
    struct xhci_stats stats;
};

/* Bring the controller up: reset, rings, scratchpad, run. The arena must be
 * identity-mapped DMA memory; 256 KiB is enough for one device. */
int xhci_init(struct xhci *x, uintptr_t base, uint8_t *dma, uint64_t dma_size);
void xhci_stop(struct xhci *x);

/* Poll the event ring. Returns 1 and fills `event` when one was waiting. */
int xhci_poll(struct xhci *x, struct xhci_event *event);

/* Wait for a connected port, reset it, and return its number (0 = none). */
uint32_t xhci_wait_port(struct xhci *x, uint32_t timeout_ms);
uint32_t xhci_portsc(struct xhci *x, uint32_t port);

/* Enable a slot, build the contexts, and address the device on `port`. */
int xhci_address_device(struct xhci *x, struct xhci_device *d, uint32_t port);
int xhci_configure_endpoints(struct xhci *x, struct xhci_device *d,
                             const uint32_t *dci, const uint32_t *type,
                             const uint32_t *packet, uint32_t count);
void xhci_release(struct xhci *x, struct xhci_device *d);

/* Control transfer on EP0. `length` bytes move through `buffer`, which must be
 * identity-mapped DMA memory. Returns the byte count, or negative on failure. */
int xhci_control(struct xhci *x, struct xhci_device *d, uint32_t request_type,
                 uint32_t request, uint32_t value, uint32_t index,
                 uint8_t *buffer, uint32_t length, uint32_t timeout_ms);

/* Bulk endpoints. `xhci_post` queues one TRB; `xhci_reap` returns the byte
 * count when it completes, 0 while it is still outstanding, negative on error.
 * Splitting them is what lets one bulk IN stay parked across poll cycles. */
int xhci_post(struct xhci *x, struct xhci_device *d, uint32_t dci,
              uint8_t *buffer, uint32_t length);
int xhci_reap(struct xhci *x, struct xhci_device *d, uint32_t dci, uint32_t length);
int xhci_send(struct xhci *x, struct xhci_device *d, uint32_t dci,
              uint8_t *buffer, uint32_t length, uint32_t timeout_ms);

/* Is the controller out of reset and running? Used to decide whether a stage
 * can adopt a link a previous one left up, instead of resetting it. */
int xhci_running(struct xhci *x);

/* Carve identity-mapped DMA out of the arena; returns NULL when it is full. */
uint8_t *xhci_alloc(struct xhci *x, uint64_t size, uint64_t alignment);

/* Platform hooks, supplied by the payload or by the native test harness. */
uint32_t xhci_rd32(uintptr_t address);
void xhci_wr32(uintptr_t address, uint32_t value);
uint64_t xhci_rd64(uintptr_t address);
void xhci_wr64(uintptr_t address, uint64_t value);
void xhci_delay_us(uint64_t microseconds);
uint64_t xhci_now_us(void);
