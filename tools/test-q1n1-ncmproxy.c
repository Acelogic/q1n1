/* SPDX-License-Identifier: MIT */
/* Native harness for platform/uefi/ncm-proxy.c: the packets it puts on the
 * wire, checked byte by byte against an independent implementation.
 *
 * The link layer underneath is stubbed out, so this is purely about whether
 * the neighbour advertisements and UDP datagrams q1n1 builds are ones a real
 * IPv6 stack will accept -- which is exactly where a first attempt goes wrong
 * and exactly what is painful to diagnose across a USB cable.
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "ncm-proxy.h"

static int checks, failures;

static void check(int condition, const char *label)
{
    if (condition) {
        checks++;
    } else {
        failures++;
        printf("FAIL: %s\n", label);
    }
}

/* ---- link layer stubs ------------------------------------------------ */

static uint8_t queued[4][NCM_MAX_FRAME];
static uint32_t queued_length[4], queued_count, queued_taken;
static uint8_t sent[8][NCM_MAX_FRAME];
static uint32_t sent_length[8], sent_count;
static uint64_t fake_time;

int ncm_send(struct ncm *n, const uint8_t *frame, uint32_t length)
{
    (void)n;
    if (sent_count < 8) {
        memcpy(sent[sent_count], frame, length);
        sent_length[sent_count] = length;
        sent_count++;
    }
    return (int)length;
}

int ncm_receive(struct ncm *n, uint8_t *frame, uint32_t max)
{
    (void)n;
    if (queued_taken >= queued_count) return 0;
    uint32_t length = queued_length[queued_taken];
    if (length > max) length = max;
    memcpy(frame, queued[queued_taken], length);
    queued_taken++;
    return (int)length;
}

int ncm_open(struct ncm *n, struct xhci *x, uint32_t which, uint32_t wait_ms)
{ (void)n; (void)x; (void)which; (void)wait_ms; return 0; }
void ncm_close(struct ncm *n) { (void)n; }
uint64_t xhci_now_us(void) { return fake_time += 10; }

static void queue(const uint8_t *frame, uint32_t length)
{
    memcpy(queued[queued_count], frame, length);
    queued_length[queued_count++] = length;
}

/* ---- an independent checksum, written from the RFC rather than copied --- */

static uint16_t reference_checksum(const uint8_t *source, const uint8_t *destination,
                                   uint8_t next, const uint8_t *payload, uint32_t length)
{
    uint32_t total = 0;
    for (uint32_t n = 0; n < 16; n += 2) total += ((uint32_t)source[n] << 8) | source[n + 1];
    for (uint32_t n = 0; n < 16; n += 2) total += ((uint32_t)destination[n] << 8) | destination[n + 1];
    total += length >> 16;
    total += length & 0xffff;
    total += next;
    uint32_t n = 0;
    for (; n + 1 < length; n += 2) total += ((uint32_t)payload[n] << 8) | payload[n + 1];
    if (n < length) total += (uint32_t)payload[n] << 8;
    while (total >> 16) total = (total & 0xffff) + (total >> 16);
    return (uint16_t)(~total & 0xffff);
}

static uint32_t be16(const uint8_t *p) { return ((uint32_t)p[0] << 8) | p[1]; }

/* ---- tests ------------------------------------------------------------- */

static const uint8_t HOST_MAC[6] = {0x12, 0x77, 0x60, 0xeb, 0x84, 0xa7};
static const uint8_t PEER_MAC[6] = {0x12, 0x77, 0x60, 0xeb, 0x84, 0x58};
static uint8_t peer_address[16] = {0xfe, 0x80, 0, 0, 0, 0, 0, 0,
                                   0x10, 0x7d, 0xf1, 0x4d, 0x6e, 0x8d, 0xbe, 0xbe};

/* An NS from the peer asking for the target's address. */
static uint32_t build_solicitation(uint8_t *frame, const uint8_t *target)
{
    memset(frame, 0, 128);
    frame[0] = 0x33; frame[1] = 0x33; frame[2] = 0xff;
    frame[3] = target[13]; frame[4] = target[14]; frame[5] = target[15];
    memcpy(frame + 6, PEER_MAC, 6);
    frame[12] = 0x86; frame[13] = 0xdd;
    uint8_t *ip = frame + 14;
    ip[0] = 0x60;
    ip[4] = 0; ip[5] = 32;
    ip[6] = 58;
    ip[7] = 255;
    memcpy(ip + 8, peer_address, 16);
    ip[24] = 0xff; ip[25] = 0x02;
    ip[24 + 11] = 0x01; ip[24 + 12] = 0xff;
    ip[24 + 13] = target[13]; ip[24 + 14] = target[14]; ip[24 + 15] = target[15];
    uint8_t *icmp = ip + 40;
    icmp[0] = 135;
    memcpy(icmp + 8, target, 16);
    icmp[24] = 1; icmp[25] = 1;
    memcpy(icmp + 26, PEER_MAC, 6);
    uint16_t sum = reference_checksum(ip + 8, ip + 24, 58, icmp, 32);
    icmp[2] = (uint8_t)(sum >> 8); icmp[3] = (uint8_t)sum;
    return 14 + 40 + 32;
}

static uint32_t build_udp(uint8_t *frame, const uint8_t *target, const uint8_t *payload,
                          uint32_t length, uint32_t source_port)
{
    memset(frame, 0, 256);
    memcpy(frame, HOST_MAC, 6);
    memcpy(frame + 6, PEER_MAC, 6);
    frame[12] = 0x86; frame[13] = 0xdd;
    uint8_t *ip = frame + 14;
    ip[0] = 0x60;
    ip[4] = (uint8_t)((8 + length) >> 8); ip[5] = (uint8_t)(8 + length);
    ip[6] = 17;
    ip[7] = 255;
    memcpy(ip + 8, peer_address, 16);
    memcpy(ip + 24, target, 16);
    uint8_t *udp = ip + 40;
    udp[0] = (uint8_t)(source_port >> 8); udp[1] = (uint8_t)source_port;
    udp[2] = (uint8_t)(NCM_PROXY_PORT >> 8); udp[3] = (uint8_t)NCM_PROXY_PORT;
    udp[4] = (uint8_t)((8 + length) >> 8); udp[5] = (uint8_t)(8 + length);
    memcpy(udp + 8, payload, length);
    uint16_t sum = reference_checksum(ip + 8, ip + 24, 17, udp, 8 + length);
    udp[6] = (uint8_t)(sum >> 8); udp[7] = (uint8_t)sum;
    return 14 + 40 + 8 + length;
}

int main(void)
{
    static struct ncm link;
    static struct ncm_proxy proxy;
    uint8_t frame[256];

    memcpy(link.mac, HOST_MAC, 6);
    ncm_proxy_init(&proxy, &link, 0);

    /* The address has to be the EUI-64 link-local of the assigned MAC, because
     * that is the one the host computes without being told. */
    static const uint8_t expected[16] = {0xfe, 0x80, 0, 0, 0, 0, 0, 0,
                                         0x10, 0x77, 0x60, 0xff, 0xfe, 0xeb, 0x84, 0xa7};
    check(memcmp(proxy.address, expected, 16) == 0, "link-local address is EUI-64 of the NCM MAC");

    /* --- unsolicited advertisement --- */
    sent_count = 0;
    ncm_proxy_announce(&proxy);
    /* One per address we answer to: the derived EUI-64 and the fixed
     * fe80::4919 the host can hardcode. */
    check(sent_count == 2, "announce emits one frame per address");
    if (sent_count > 1) {
        static const uint8_t fixed[16] = {0xfe, 0x80, 0, 0, 0, 0, 0, 0,
                                          0, 0, 0, 0, 0, 0, 0x49, 0x19};
        const uint8_t *ip = sent[1] + 14, *icmp = sent[1] + 54;
        check(memcmp(icmp + 8, fixed, 16) == 0, "second announce targets fe80::4919");
        check(memcmp(ip + 8, fixed, 16) == 0, "and is sourced from it");
        check(reference_checksum(ip + 8, ip + 24, 58, icmp, 32) == 0,
              "fixed-address advertisement checksum verifies");
    }
    if (sent_count) {
        const uint8_t *f = sent[0];
        const uint8_t *ip = f + 14, *icmp = f + 54;
        check(f[0] == 0x33 && f[1] == 0x33 && f[5] == 0x01, "announce goes to all-nodes multicast");
        check(be16(f + 12) == 0x86dd, "announce is IPv6");
        check(ip[0] == 0x60 && ip[6] == 58 && ip[7] == 255, "IPv6 header: version, ICMPv6, hop 255");
        check(be16(ip + 4) == 32, "IPv6 payload length covers the advertisement");
        check(icmp[0] == 136, "ICMPv6 type is neighbour advertisement");
        check(memcmp(icmp + 8, proxy.address, 16) == 0, "advertisement target is our address");
        check(icmp[24] == 2 && icmp[25] == 1, "target link-layer address option present");
        check(memcmp(icmp + 26, HOST_MAC, 6) == 0, "option carries our MAC");
        check(reference_checksum(ip + 8, ip + 24, 58, icmp, 32) == 0,
              "unsolicited advertisement checksum verifies");
    }

    /* --- solicited advertisement --- */
    sent_count = queued_count = queued_taken = 0;
    uint32_t length = build_solicitation(frame, proxy.address);
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.stats.solicitations == 1, "solicitation for our address is recognised");
    check(sent_count == 1, "solicitation is answered");
    if (sent_count) {
        const uint8_t *f = sent[0];
        const uint8_t *ip = f + 14, *icmp = f + 54;
        check(memcmp(f, PEER_MAC, 6) == 0, "answer is unicast to the asker");
        check(memcmp(ip + 24, peer_address, 16) == 0, "answer is addressed to the asker");
        check((icmp[4] & 0x40) != 0, "solicited flag is set");
        check((icmp[4] & 0x20) != 0, "override flag is set");
        check(reference_checksum(ip + 8, ip + 24, 58, icmp, 32) == 0,
              "solicited advertisement checksum verifies");
    }

    /* --- UDP in, and the reply out --- */
    sent_count = queued_count = queued_taken = 0;
    const uint8_t payload[] = {0xff, 0x55, 0xaa, 0x01, 0x02, 0x03, 0x04, 0x05};
    length = build_udp(frame, proxy.address, payload, sizeof(payload), 55555);
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.stats.frames_in == 1, "UDP datagram accepted");
    check(proxy.io.ready(proxy.io.ctx), "transport is ready once a peer is known");
    check(proxy.peer_port == 55555, "source port remembered for the reply");

    uint8_t got[16];
    size_t read = proxy.io.read(proxy.io.ctx, got, sizeof(payload));
    check(read == sizeof(payload) && memcmp(got, payload, sizeof(payload)) == 0,
          "payload reaches the proxy byte stream");

    sent_count = 0;
    const uint8_t reply[] = {0xde, 0xad, 0xbe, 0xef, 0x11, 0x22};
    size_t wrote = proxy.io.write(proxy.io.ctx, reply, sizeof(reply));
    check(wrote == sizeof(reply), "write accepts the reply");
    check(sent_count == 1, "reply is one datagram");
    if (sent_count) {
        const uint8_t *f = sent[0];
        const uint8_t *ip = f + 14, *udp = f + 54;
        check(memcmp(f, PEER_MAC, 6) == 0, "reply is unicast to the peer");
        check(ip[6] == 17, "reply is UDP");
        check(memcmp(ip + 8, proxy.address, 16) == 0, "reply source is our address");
        check(memcmp(ip + 24, peer_address, 16) == 0, "reply destination is the peer");
        check(be16(udp) == NCM_PROXY_PORT, "reply source port");
        check(be16(udp + 2) == 55555, "reply goes back to the sender's port");
        check(be16(udp + 4) == 8 + sizeof(reply), "UDP length");
        check(be16(ip + 4) == 8 + sizeof(reply), "IPv6 payload length matches UDP length");
        check(reference_checksum(ip + 8, ip + 24, 17, udp, 8 + (uint32_t)sizeof(reply)) == 0,
              "UDP checksum verifies");
        check(be16(udp + 6) != 0, "UDP checksum is never zero over IPv6");
    }

    /* --- traffic that is not ours must be stepped over, not consumed --- */
    sent_count = queued_count = queued_taken = 0;
    uint64_t before = proxy.stats.frames_in;
    length = build_udp(frame, proxy.address, payload, sizeof(payload), 55555);
    frame[14 + 40 + 2] = 0x00;                       /* a different destination port */
    frame[14 + 40 + 3] = 0x35;
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.stats.frames_in == before, "a datagram for another port is ignored");
    check(proxy.stats.foreign > 0, "and counted as foreign");

    /* --- the fixed address, end to end ---
     *
     * This is the one the host can reach without knowing the descriptor MAC,
     * so it has to work on its own: solicitation answered, datagram accepted,
     * and the reply sourced from the address the host actually addressed. A
     * reply from the derived address instead would arrive on an unconnected
     * socket and pass unnoticed here, while looking like a dead target to
     * anything stricter -- which is how this went undiagnosed for a session. */
    static const uint8_t fixed[16] = {0xfe, 0x80, 0, 0, 0, 0, 0, 0,
                                      0, 0, 0, 0, 0, 0, 0x49, 0x19};
    sent_count = queued_count = queued_taken = 0;
    before = proxy.stats.solicitations;
    length = build_solicitation(frame, fixed);
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.stats.solicitations == before + 1, "solicitation for fe80::4919 is recognised");
    check(sent_count == 1, "and answered");
    if (sent_count) {
        const uint8_t *ip = sent[0] + 14, *icmp = sent[0] + 54;
        check(memcmp(icmp + 8, fixed, 16) == 0, "advertised target is the solicited address");
        check(reference_checksum(ip + 8, ip + 24, 58, icmp, 32) == 0,
              "fixed-address solicited advertisement checksum verifies");
    }

    sent_count = queued_count = queued_taken = 0;
    length = build_udp(frame, fixed, payload, sizeof(payload), 4242);
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.io.read(proxy.io.ctx, got, sizeof(payload)) == sizeof(payload),
          "datagram to fe80::4919 reaches the byte stream");
    sent_count = 0;
    proxy.io.write(proxy.io.ctx, reply, sizeof(reply));
    check(sent_count == 1, "reply to the fixed address is one datagram");
    if (sent_count) {
        const uint8_t *ip = sent[0] + 14, *udp = sent[0] + 54;
        check(memcmp(ip + 8, fixed, 16) == 0, "reply is sourced from fe80::4919");
        check(reference_checksum(ip + 8, ip + 24, 17, udp, 8 + (uint32_t)sizeof(reply)) == 0,
              "reply checksum verifies against the fixed source");
    }

    /* A datagram on our port but addressed to a third party is not ours. */
    sent_count = queued_count = queued_taken = 0;
    before = proxy.stats.frames_in;
    static const uint8_t stranger[16] = {0xfe, 0x80, 0, 0, 0, 0, 0, 0,
                                         0, 0, 0, 0, 0, 0, 0, 0x99};
    length = build_udp(frame, stranger, payload, sizeof(payload), 55555);
    queue(frame, length);
    proxy.io.poll(proxy.io.ctx);
    check(proxy.stats.frames_in == before, "a datagram for another address is ignored");

    if (failures) {
        printf("FAILED: %d of %d checks\n", failures, checks + failures);
        return 1;
    }
    printf("PASS: %d q1n1 NCM proxy packet checks (native harness, sanitizers on)\n", checks);
    return 0;
}
