/* SPDX-License-Identifier: MIT */
/* The m1n1 proxy byte stream carried over the NCM link as UDP over IPv6.
 *
 * The obvious encoding would be raw Ethernet with a private ethertype, and the
 * proxy protocol -- which has its own framing, checksums and resynchronisation
 * -- needs nothing more than a datagram pipe. But a raw ethertype can only be
 * read on the host through BPF, which needs root, and a transport that demands
 * root every session is a transport nobody uses. Speaking UDP costs q1n1 about
 * a hundred lines and makes the host side an ordinary unprivileged socket.
 *
 * So this answers neighbour solicitations for its own address, and carries the
 * proxy stream in UDP datagrams on a link-local address derived from the MAC
 * the NCM function assigned us. No routing, no fragmentation, no DHCP.
 *
 * This is a second transport, not a replacement. USB0's CDC console stays the
 * lifeline, and `idle_timeout_us` lets the proxy loop hand control back to it
 * when this link goes quiet instead of stranding the machine.
 */
#pragma once
#include <stdint.h>
#include "ncm.h"
#include "q1n1-proxy.h"

#define NCM_PROXY_PORT 4919
#define NCM_PROXY_RING 8192
#define NCM_PROXY_PAYLOAD 1400

/* A second, fixed address this answers to: fe80::4919, the port number.
 *
 * The derived EUI-64 address below needs the MAC from the function's Ethernet
 * descriptor, and the host cannot see that value -- it is not the MAC the host
 * gives its own interface. Assuming the two matched cost an entire debugging
 * session: they happened to be equal once, and when they diverged the host
 * addressed a machine that was not there, which is indistinguishable from a
 * dead target. So q1n1 also answers on an address the host can hardcode. */
#define NCM_PROXY_FIXED_HI 0x49
#define NCM_PROXY_FIXED_LO 0x19

struct ncm_proxy_stats {
    uint64_t frames_in, frames_out, bytes_in, bytes_out;
    uint64_t dropped, foreign, solicitations, advertisements, bad_checksum;
};

struct ncm_proxy {
    struct ncm *n;
    struct q1n1_io io;

    uint8_t address[16];             /* ours: fe80:: + EUI-64 of the NCM MAC */
    /* Whichever of our two addresses the peer is actually using, so replies go
     * out with the source it addressed rather than the one it never saw. */
    uint8_t local[16];
    uint8_t peer_mac[6];
    uint8_t peer_address[16];
    uint16_t peer_port;
    int have_peer;

    uint8_t ring[NCM_PROXY_RING];
    uint32_t head, tail;
    uint8_t frame[NCM_MAX_FRAME];
    uint64_t idle_timeout_us, last_frame_us;
    struct ncm_proxy_stats stats;
};

/* Prepare a transport over `link`. `idle_timeout_us` of 0 disables the
 * watchdog; otherwise the proxy loop returns once nothing has arrived for that
 * long, which is what makes running it over this link recoverable. */
void ncm_proxy_init(struct ncm_proxy *p, struct ncm *link, uint64_t idle_timeout_us);

/* Announce ourselves so the host can find us without waiting to be asked. */
void ncm_proxy_announce(struct ncm_proxy *p);

/* Re-point a transport that a previous stage set up: everything it holds lives
 * in shared memory and stays valid, but its function pointers refer to the old
 * stage's text, which the next chainload may overwrite. */
void ncm_proxy_rebind(struct ncm_proxy *p, struct ncm *link);

/* A second UDP port on the same addresses, for out-of-band requests
 * (call-service.h). The handler fills `reply` (at most NCM_SIDE_REPLY_MAX
 * bytes) and returns its length, 0 for none; the reply goes back to the
 * sender from `port`, and the proxy peer is never touched. Per stage, not per
 * link: struct ncm_proxy is shared across a chainload and its layout frozen.
 * 1440 is the most one 1500-byte IPv6 packet carries after its UDP header. */
#define NCM_SIDE_REPLY_MAX 1440
typedef uint32_t (*ncm_side_handler)(void *ctx, const uint8_t *data, uint32_t length,
                                     uint8_t *reply);
void ncm_proxy_side_port(uint16_t port, ncm_side_handler handler, void *ctx);

/* Service one link while a P_CALL holds the proxy loop: neighbour discovery
 * and the side port are answered as usual; proxy datagrams are dropped and
 * counted, so they can neither queue behind nor re-point the pending reply. */
void ncm_proxy_call_poll(struct ncm_proxy *p);
/* Leave call mode after a call poll that never returned (the payload faulted
 * inside it and unwound the P_CALL), so ordinary polling resumes. */
void ncm_proxy_call_reset(void);

/* This stage's side-port counters: proxy datagrams dropped during calls,
 * side replies sent, side replies the link refused. */
struct ncm_side_stats { uint64_t call_dropped, replies, reply_failures; };
void ncm_proxy_side_stats(struct ncm_side_stats *out);
