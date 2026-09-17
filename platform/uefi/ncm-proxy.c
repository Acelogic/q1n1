/* SPDX-License-Identifier: MIT */
/* See ncm-proxy.h. */
#include "ncm-proxy.h"

void *memcpy(void *dest, const void *src, size_t n);
void *memset(void *dest, int value, size_t n);

#define ETH_HEADER 14
#define IP6_HEADER 40
#define UDP_HEADER 8
#define HEADERS (ETH_HEADER + IP6_HEADER + UDP_HEADER)

#define ETHERTYPE_IPV6 0x86DD
#define NEXT_UDP 17
#define NEXT_ICMPV6 58
#define ICMPV6_NEIGHBOUR_SOLICIT 135
#define ICMPV6_NEIGHBOUR_ADVERT 136

static uint32_t be16(const uint8_t *p) { return ((uint32_t)p[0] << 8) | p[1]; }
static void put_be16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }

/* One's-complement sum, left folded for the caller to finish. */
static uint32_t sum16(const uint8_t *data, uint32_t length, uint32_t total)
{
    uint32_t n = 0;
    for (; n + 1 < length; n += 2) total += be16(data + n);
    if (n < length) total += (uint32_t)data[n] << 8;
    return total;
}

static uint16_t fold(uint32_t total)
{
    while (total >> 16) total = (total & 0xffff) + (total >> 16);
    return (uint16_t)(~total & 0xffff);
}

/* IPv6 pseudo-header plus the payload, as RFC 8200 requires for UDP/ICMPv6. */
static uint16_t transport_checksum(const uint8_t *source, const uint8_t *destination,
                                   uint32_t next, const uint8_t *payload, uint32_t length)
{
    uint32_t total = sum16(source, 16, 0);
    total = sum16(destination, 16, total);
    total += (length >> 16) & 0xffff;
    total += length & 0xffff;
    total += next;
    total = sum16(payload, length, total);
    return fold(total);
}

static uint32_t ring_used(const struct ncm_proxy *p)
{
    return (p->head - p->tail) & (NCM_PROXY_RING - 1);
}

static void ring_push(struct ncm_proxy *p, const uint8_t *data, uint32_t length)
{
    for (uint32_t n = 0; n < length; n++) {
        p->ring[p->head] = data[n];
        p->head = (p->head + 1) & (NCM_PROXY_RING - 1);
    }
}

/* Fill in Ethernet and IPv6 headers; returns the offset of the payload. */
static uint32_t build_headers_from(struct ncm_proxy *p, uint8_t *frame, const uint8_t *source,
                                   const uint8_t *destination_mac, const uint8_t *destination,
                                   uint32_t next, uint32_t payload_length)
{
    memcpy(frame, destination_mac, 6);
    memcpy(frame + 6, p->n->mac, 6);
    put_be16(frame + 12, ETHERTYPE_IPV6);
    uint8_t *ip = frame + ETH_HEADER;
    memset(ip, 0, IP6_HEADER);
    ip[0] = 0x60;                                   /* version 6 */
    put_be16(ip + 4, payload_length);
    ip[6] = (uint8_t)next;
    ip[7] = 255;                                    /* hop limit */
    memcpy(ip + 8, source, 16);
    memcpy(ip + 24, destination, 16);
    return ETH_HEADER + IP6_HEADER;
}

/* Sourced from whichever of our addresses the peer is using. */
static uint32_t build_headers(struct ncm_proxy *p, uint8_t *frame, const uint8_t *destination_mac,
                              const uint8_t *destination, uint32_t next, uint32_t payload_length)
{
    return build_headers_from(p, frame, p->local, destination_mac, destination,
                              next, payload_length);
}

static void send_frame(struct ncm_proxy *p, uint8_t *frame, uint32_t length)
{
    if (length < 60) {                              /* minimum Ethernet frame */
        memset(frame + length, 0, 60 - length);
        length = 60;
    }
    if (ncm_send(p->n, frame, length) >= 0) p->stats.frames_out++;
}

/* The fixed address, materialised: fe80::4919. */
static void fixed_address(uint8_t *out)
{
    for (unsigned n = 0; n < 16; n++) out[n] = 0;
    out[0] = 0xfe;
    out[1] = 0x80;
    out[14] = NCM_PROXY_FIXED_HI;
    out[15] = NCM_PROXY_FIXED_LO;
}

/* Is this one of the addresses we answer to? */
static int ours(const struct ncm_proxy *p, const uint8_t *address)
{
    uint8_t fixed[16];
    unsigned n, derived = 1, well_known = 1;
    fixed_address(fixed);
    for (n = 0; n < 16; n++) {
        if (address[n] != p->address[n]) derived = 0;
        if (address[n] != fixed[n]) well_known = 0;
    }
    return derived || well_known;
}

/* Answer a solicitation, or announce ourselves unprompted to seed the host's
 * neighbour cache so its first datagram does not wait on a resolution.
 *
 * `target` is the address being advertised, which must be the one that was
 * solicited -- we answer to two, so it is not always p->address. */
static void send_advertisement(struct ncm_proxy *p, const uint8_t *destination_mac,
                               const uint8_t *destination, const uint8_t *target_address,
                               uint32_t solicited)
{
    uint8_t *frame = p->frame;
    /* The reply is built in the very buffer the solicitation arrived in, so
     * the asker's address has to be copied out before the headers overwrite
     * it -- otherwise the answer is addressed to ourselves and is dropped. */
    uint8_t mac[6], destination_copy[16], target[16];
    for (unsigned n = 0; n < 6; n++) mac[n] = destination_mac[n];
    for (unsigned n = 0; n < 16; n++) destination_copy[n] = destination[n];
    for (unsigned n = 0; n < 16; n++) target[n] = target_address[n];
    destination_mac = mac;
    destination = destination_copy;
    uint32_t offset = build_headers_from(p, frame, target, destination_mac, destination,
                                         NEXT_ICMPV6, 32);
    uint8_t *icmp = frame + offset;
    memset(icmp, 0, 32);
    icmp[0] = ICMPV6_NEIGHBOUR_ADVERT;
    icmp[4] = (uint8_t)(solicited ? 0x60 : 0x20);   /* solicited+override, or override */
    memcpy(icmp + 8, target, 16);
    icmp[24] = 2;                                   /* target link-layer address option */
    icmp[25] = 1;
    memcpy(icmp + 26, p->n->mac, 6);
    put_be16(icmp + 2, transport_checksum(target, destination, NEXT_ICMPV6, icmp, 32));
    send_frame(p, frame, offset + 32);
    p->stats.advertisements++;
}

void ncm_proxy_announce(struct ncm_proxy *p)
{
    static const uint8_t all_nodes[16] = {0xff, 0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
    static const uint8_t all_nodes_mac[6] = {0x33, 0x33, 0, 0, 0, 1};
    uint8_t fixed[16];
    send_advertisement(p, all_nodes_mac, all_nodes, p->address, 0);
    fixed_address(fixed);
    send_advertisement(p, all_nodes_mac, all_nodes, fixed, 0);
}

static void handle_icmpv6(struct ncm_proxy *p, const uint8_t *frame, uint32_t length)
{
    const uint8_t *ip = frame + ETH_HEADER;
    const uint8_t *icmp = ip + IP6_HEADER;
    if (length < ETH_HEADER + IP6_HEADER + 24) return;
    if (icmp[0] != ICMPV6_NEIGHBOUR_SOLICIT) return;
    /* Only ours to answer -- either the derived address or the fixed one. */
    if (!ours(p, icmp + 8)) return;
    p->stats.solicitations++;
    send_advertisement(p, frame + 6, ip + 8, icmp + 8, 1);
}

static void handle_udp(struct ncm_proxy *p, const uint8_t *frame, uint32_t length)
{
    const uint8_t *ip = frame + ETH_HEADER;
    const uint8_t *udp = ip + IP6_HEADER;
    if (length < HEADERS) return;
    if (be16(udp + 2) != NCM_PROXY_PORT) {
        p->stats.foreign++;
        return;
    }
    uint32_t udp_length = be16(udp + 4);
    if (udp_length < UDP_HEADER || ETH_HEADER + IP6_HEADER + udp_length > length) {
        p->stats.dropped++;
        return;
    }
    uint32_t payload = udp_length - UDP_HEADER;
    if (payload > NCM_PROXY_RING - 1 - ring_used(p)) {
        p->stats.dropped++;
        return;
    }

    /* Addressed to one of ours, not just forwarded past us. */
    if (!ours(p, ip + 24)) {
        p->stats.foreign++;
        return;
    }

    /* Learn the peer from whoever talks to us: no configuration either side.
     * `local` comes from the datagram too, so replies carry the source the
     * host is expecting rather than the address it could not have known. */
    memcpy(p->peer_mac, frame + 6, 6);
    memcpy(p->peer_address, ip + 8, 16);
    memcpy(p->local, ip + 24, 16);
    p->peer_port = (uint16_t)be16(udp);
    p->have_peer = 1;

    ring_push(p, udp + UDP_HEADER, payload);
    p->stats.frames_in++;
    p->stats.bytes_in += payload;
    p->last_frame_us = xhci_now_us();
}

static void proxy_poll(void *ctx)
{
    struct ncm_proxy *p = ctx;
    for (unsigned budget = 0; budget < 8; budget++) {
        int length = ncm_receive(p->n, p->frame, sizeof(p->frame));
        if (length <= 0) return;
        if ((uint32_t)length < ETH_HEADER + IP6_HEADER) continue;
        if (be16(p->frame + 12) != ETHERTYPE_IPV6) {
            p->stats.foreign++;
            continue;
        }
        uint32_t next = p->frame[ETH_HEADER + 6];
        if (next == NEXT_ICMPV6) handle_icmpv6(p, p->frame, (uint32_t)length);
        else if (next == NEXT_UDP) handle_udp(p, p->frame, (uint32_t)length);
        else p->stats.foreign++;
    }
}

static size_t proxy_read(void *ctx, uint8_t *buffer, size_t count)
{
    struct ncm_proxy *p = ctx;
    size_t got = 0;
    while (got < count && p->tail != p->head) {
        buffer[got++] = p->ring[p->tail];
        p->tail = (p->tail + 1) & (NCM_PROXY_RING - 1);
    }
    return got;
}

/* Sent straight out rather than buffered: the proxy writes a reply and then
 * waits for the next request, so anything held back here would only add a
 * round trip of latency. */
static size_t proxy_write(void *ctx, const uint8_t *buffer, size_t count)
{
    struct ncm_proxy *p = ctx;
    size_t sent = 0;
    if (!p->have_peer) return 0;
    while (sent < count) {
        uint32_t chunk = (uint32_t)(count - sent);
        if (chunk > NCM_PROXY_PAYLOAD) chunk = NCM_PROXY_PAYLOAD;
        uint32_t udp_length = UDP_HEADER + chunk;
        uint32_t offset = build_headers(p, p->frame, p->peer_mac, p->peer_address,
                                        NEXT_UDP, udp_length);
        uint8_t *udp = p->frame + offset;
        put_be16(udp, NCM_PROXY_PORT);
        put_be16(udp + 2, p->peer_port);
        put_be16(udp + 4, udp_length);
        put_be16(udp + 6, 0);
        memcpy(udp + UDP_HEADER, buffer + sent, chunk);
        uint16_t check = transport_checksum(p->local, p->peer_address, NEXT_UDP, udp, udp_length);
        /* A zero checksum means "none" in IPv4 and is illegal over IPv6. */
        put_be16(udp + 6, check ? check : 0xffff);
        send_frame(p, p->frame, offset + udp_length);
        p->stats.bytes_out += chunk;
        sent += chunk;
    }
    return sent;
}

static int proxy_ready(void *ctx)
{
    struct ncm_proxy *p = ctx;
    return p->have_peer;
}

static int proxy_abort(void *ctx)
{
    struct ncm_proxy *p = ctx;
    if (!p->idle_timeout_us) return 0;
    return xhci_now_us() - p->last_frame_us > p->idle_timeout_us;
}

void ncm_proxy_rebind(struct ncm_proxy *p, struct ncm *link)
{
    p->n = link;
    p->io.read = proxy_read;
    p->io.write = proxy_write;
    p->io.poll = proxy_poll;
    p->io.ready = proxy_ready;
    p->io.abort = proxy_abort;
    p->io.ctx = p;
}

void ncm_proxy_init(struct ncm_proxy *p, struct ncm *link, uint64_t idle_timeout_us)
{
    memset(p, 0, sizeof(*p));
    p->n = link;
    p->idle_timeout_us = idle_timeout_us;
    p->last_frame_us = xhci_now_us();

    /* fe80::/64 with the EUI-64 of the MAC the NCM function gave this host. */
    p->address[0] = 0xfe;
    p->address[1] = 0x80;
    p->address[8] = link->mac[0] ^ 0x02;
    p->address[9] = link->mac[1];
    p->address[10] = link->mac[2];
    p->address[11] = 0xff;
    p->address[12] = 0xfe;
    p->address[13] = link->mac[3];
    p->address[14] = link->mac[4];
    p->address[15] = link->mac[5];
    /* Until a peer picks one, replies use the derived address. */
    memcpy(p->local, p->address, 16);

    p->io.read = proxy_read;
    p->io.write = proxy_write;
    p->io.poll = proxy_poll;
    p->io.ready = proxy_ready;
    p->io.abort = proxy_abort;
    p->io.ctx = p;
}
