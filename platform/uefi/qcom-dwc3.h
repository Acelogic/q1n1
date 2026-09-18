/* SPDX-License-Identifier: MIT */
/* Polled DWC3 CDC ACM device for a Qualcomm controller that firmware already
 * powered, clocked, and configured. Ported from m1n1 src/usb_dwc3.c (Asahi
 * Linux contributors): identity DMA replaces DART IOVAs, the Qualcomm
 * qscratch VBUS override replaces Apple PHY/HPM setup, and only the device
 * soft reset is used so firmware PHY configuration is preserved. */
#pragma once
#include <stdint.h>
#include <stddef.h>

#define QDWC3_PIPES 2
#define QDWC3_ARENA_SIZE (1u << 20)

/* Firmware-running register values used to restore global configuration. */
struct qdwc3_saved {
    uint32_t gsbuscfg0, gsbuscfg1, gctl, guctl, guctl1, guctl2;
    uint32_t gusb2phycfg, gusb3pipectl, gfladj, dcfg, gsnpsid;
};

struct qdwc3_ring { uint8_t *data; uint32_t size, head, tail; };

struct qdwc3_stats {
    uint64_t events, device_events, ep_events, setups, stalls, resets, connects, disconnects;
    uint64_t rx_bytes, tx_bytes, dropped_rx, command_failures, dma_mismatch, overflow;
    uint32_t speed, address, configured, last_command_status, last_devt;
    uint32_t restored, init_step, last_setup;
};

struct qdwc3 {
    uintptr_t regs, qscratch;
    const struct qdwc3_saved *saved;
    uint8_t *arena;
    uint64_t arena_size;
    uint8_t *events;
    uint32_t event_offset;
    uint32_t ep0_state, ep0_three_stage;
    const uint8_t *ep0_buffer;
    uint32_t ep0_length;
    uint8_t *ep0_read_buffer;
    uint32_t ep0_read_length;
    struct {
        uint8_t in_progress, zlp_pending;
        uint8_t *buffer;
        uint8_t *trb;
    } ep[10];
    struct {
        struct qdwc3_ring host2device, device2host;
        uint8_t ep_in, ep_out, ready;
        uint8_t line_coding[7];
    } pipe[QDWC3_PIPES];
    uint16_t status_word;
    uint8_t string_buffer[64]; /* Each controller may have an EP0 reply pending. */
    struct qdwc3_stats stats;
};

/* Arena must be identity-mapped DMA memory of QDWC3_ARENA_SIZE bytes. */
int qdwc3_takeover(struct qdwc3 *d, uintptr_t regs, uintptr_t qscratch,
                   const struct qdwc3_saved *saved, uint8_t *arena, uint64_t arena_size);
void qdwc3_poll(struct qdwc3 *d);
size_t qdwc3_read(struct qdwc3 *d, unsigned pipe, uint8_t *buffer, size_t count);
size_t qdwc3_write(struct qdwc3 *d, unsigned pipe, const uint8_t *buffer, size_t count);
int qdwc3_ready(const struct qdwc3 *d, unsigned pipe);
/* True while the link state machine reports Disconnected. `stats.configured`
 * cannot see a physical unplug, because device mode forces session-valid; see
 * the comment on the definition. Debounce before acting on it. */
int qdwc3_link_down(const struct qdwc3 *d);
void qdwc3_stop(struct qdwc3 *d);

/* Platform hooks supplied by the EFI app or the host simulation. */
uint32_t qdwc3_rd(uintptr_t address);
void qdwc3_wr(uintptr_t address, uint32_t value);
void qdwc3_delay_us(uint64_t microseconds);
