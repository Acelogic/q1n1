/* SPDX-License-Identifier: MIT */
/* CDC-NCM host driver: enumerate a USB network device, configure its data
 * interface, and move Ethernet frames over the bulk pair.
 *
 * Written against what this machine's Mac actually presents over a bare
 * USB-C cable (two NCM 1.0 functions, NTB16 and NTB32, 32764-byte blocks,
 * 4-byte alignment) and ported from the proven host-side implementation in
 * tools/a16xhci.py.
 *
 * Only NTB16 is produced. The device advertises both, and NTB32 buys nothing
 * at these sizes.
 */
#pragma once
#include <stdint.h>
#include "xhci.h"

#define NCM_MAX_FRAME 1536
#define NCM_IN_BUFFER 16384
#define NCM_OUT_BUFFER 4096

struct ncm_stats {
    uint64_t blocks_in, blocks_out, frames_in, frames_out, bytes_in, bytes_out;
    uint64_t malformed, oversize, send_failures;
    /* Which check rejected a block, so a burst of `malformed` says why. */
    uint64_t bad_signature, bad_ndp, bad_entry;
    uint32_t init_step;
};

struct ncm {
    struct xhci *x;
    struct xhci_device device;
    uint32_t control_interface, data_interface, configuration;
    uint32_t dci_in, dci_out, packet_in, packet_out;
    uint32_t ntb_in_max, ntb_out_max, out_divisor, out_remainder, out_alignment;
    uint16_t vendor, product;
    uint8_t mac[6];                  /* the address the device assigns this host */
    uint8_t *in_buffer, *out_buffer, *scratch;
    uint32_t sequence;

    /* One received NTB is handed back a datagram at a time, so the walk has to
     * survive across calls. */
    uint32_t rx_length, rx_ndp, rx_entry;
    int rx_posted;

    struct ncm_stats stats;
};

/* Bring up the first NCM function on the attached device. `which` picks the
 * function when the device offers more than one (0 = the first). */
int ncm_open(struct ncm *n, struct xhci *x, uint32_t which, uint32_t wait_ms);
void ncm_close(struct ncm *n);

/* Queue an Ethernet frame. Returns the byte count or negative on failure. */
int ncm_send(struct ncm *n, const uint8_t *frame, uint32_t length);

/* Next received Ethernet frame, or 0 when none is ready. Never blocks. */
int ncm_receive(struct ncm *n, uint8_t *frame, uint32_t max);
