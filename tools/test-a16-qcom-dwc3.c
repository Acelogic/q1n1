/* SPDX-License-Identifier: MIT */
/* Native simulation of the Qualcomm DWC3 takeover driver. Models only the
 * register behavior the driver relies on; no hardware or firmware is used. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../platform/uefi/qcom-dwc3.h"

#define BASE 0x0a600000u
#define QS 0x0a6f8800u
static uint32_t regs[0x10000 / 4], qs[0x100 / 4];
static uint64_t trb_addr[10];
static unsigned started[10], stalls_set, csftrst, depcfg[10];
static uint32_t write_pointer;
static int reset_clobbers = 1, block_run = 0;

uint32_t qdwc3_rd(uintptr_t a)
{
    if (a >= QS && a < QS + 0x100) return qs[(a - QS) / 4];
    assert(a >= BASE && a < BASE + 0x10000 && !(a & 3));
    return regs[(a - BASE) / 4];
}
static uint32_t *reg(uint32_t off) { return &regs[off / 4]; }
void qdwc3_wr(uintptr_t a, uint32_t v)
{
    if (a >= QS && a < QS + 0x100) { qs[(a - QS) / 4] = v; return; }
    assert(a >= BASE && a < BASE + 0x10000 && !(a & 3));
    uint32_t off = (uint32_t)(a - BASE);
    if (off == 0xc40c) { assert(v <= *reg(0xc40c)); *reg(0xc40c) -= v; return; } /* GEVNTCOUNT ack */
    *reg(off) = v;
    if (off == 0xc704) {                                                 /* DCTL */
        if (v & (1u << 30)) {
            csftrst++;
            *reg(0xc704) &= ~(1u << 30);
            if (reset_clobbers) { *reg(0xc200) = 0x40; *reg(0xc110) = 0x2000; *reg(0xc100) = 0; }
        }
        if (!block_run) *reg(0xc70c) = (*reg(0xc70c) & ~(1u << 22)) | ((v >> 31) ? 0 : (1u << 22));
    }
    if (off >= 0xc80c && off < 0xc80c + 16 * 10 && !((off - 0xc80c) % 16)) {  /* DEPCMD(n) */
        unsigned ep = (off - 0xc80c) / 16, cmd = v & 0xff;
        if (cmd == 0x01) depcfg[ep]++;
        if (cmd == 0x04) stalls_set++;
        if (cmd == 0x06) {
            trb_addr[ep] = (uint64_t)*reg(0xc808 + ep * 16) << 32 | *reg(0xc804 + ep * 16);
            started[ep]++;
        }
        *reg(off) = (v & ~(1u << 10)) | (cmd == 0x06 ? (ep + 1) << 16 : 0);
    }
}
void qdwc3_delay_us(uint64_t us) { (void)us; }

static uint8_t *trb(unsigned ep) { assert(trb_addr[ep]); return (uint8_t *)(uintptr_t)trb_addr[ep]; }
static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint8_t *buffer(unsigned ep) { const uint8_t *t = trb(ep); return (uint8_t *)(uintptr_t)(le32(t) | (uint64_t)le32(t + 4) << 32); }
static uint32_t trb_len(unsigned ep) { return le32(trb(ep) + 8) & 0xffffff; }
static uint32_t trb_type(unsigned ep) { return le32(trb(ep) + 12) >> 4 & 0x3f; }
static void inject(struct qdwc3 *d, uint32_t event)
{
    uint8_t *events = (uint8_t *)(uintptr_t)(*reg(0xc400) | (uint64_t)*reg(0xc404) << 32);
    assert(events == d->events);
    memcpy(events + write_pointer, &event, 4);
    write_pointer = (write_pointer + 4) % 4096;
    *reg(0xc40c) += 4;
}
static uint32_t ep_event(unsigned ep, unsigned kind) { return ep << 1 | kind << 6; }
static uint32_t dev_event(unsigned type) { return 1 | type << 8; }
/* Complete the active TRB on ep with `bytes` transferred and deliver XferComplete. */
static void complete(struct qdwc3 *d, unsigned ep, uint32_t bytes)
{
    uint8_t *t = trb(ep);
    uint32_t len = trb_len(ep);
    assert(bytes <= len);
    uint32_t remaining = len - bytes, ctrl = le32(t + 12) & ~1u;
    memcpy(t + 8, &remaining, 4);
    memcpy(t + 12, &ctrl, 4);
    inject(d, ep_event(ep, 1));
    qdwc3_poll(d);
}
static void not_ready(struct qdwc3 *d, unsigned ep) { inject(d, ep_event(ep, 3)); qdwc3_poll(d); }

static uint8_t last_in[512];
static uint32_t last_in_len;
/* Drive one full control transfer as the host would. Returns IN data length. */
static int control(struct qdwc3 *d, const uint8_t setup[8], const uint8_t *out_data, uint32_t out_len)
{
    assert(trb_type(0) == 2 && trb_len(0) == 8);
    memcpy(buffer(0), setup, 8);
    unsigned stalls_before = stalls_set, setups_before = started[0];
    complete(d, 0, 8);
    if (stalls_set != stalls_before) { assert(started[0] == setups_before + 1 && trb_type(0) == 2); return -1; }
    uint16_t length = setup[6] | setup[7] << 8;
    if (length && (setup[0] & 0x80)) {
        not_ready(d, 1);
        assert(trb_type(1) == 5);
        last_in_len = trb_len(1);
        memcpy(last_in, buffer(1), last_in_len);
        complete(d, 1, last_in_len);
        not_ready(d, 0);
        assert(trb_type(0) == 4); /* STATUS3 after an IN data stage */
        complete(d, 0, 0);
    } else {
        if (length) {
            not_ready(d, 0);
            assert(trb_type(0) == 5 && trb_len(0) == 64);
            memcpy(buffer(0), out_data, out_len);
            complete(d, 0, out_len);
        }
        not_ready(d, 1);
        assert(trb_type(1) == (length ? 4 : 3)); /* STATUS3 after OUT data, STATUS2 for no-data */
        complete(d, 1, 0);
        last_in_len = 0;
    }
    assert(trb_type(0) == 2); /* The next SETUP is armed. */
    return (int)last_in_len;
}

int main(void)
{
    unsigned checks = 0;
    static struct qdwc3 d;
    static const struct qdwc3_saved saved = {
        .gsbuscfg0 = 0xe, .gsbuscfg1 = 0x300, .gctl = 0x30c12004 | (1u << 11), .guctl = 0x0d00a010,
        .guctl1 = 0x0908018a, .guctl2 = 0, .gusb2phycfg = 0x00102500 | (1u << 31), .gusb3pipectl = 0x010c0002,
        .gfladj = 0x0c80c000, .dcfg = 0x00080804 | (5 << 3), .gsnpsid = 0x33310000 | 0x200a};
    uint8_t *arena = aligned_alloc(4096, QDWC3_ARENA_SIZE);
    assert(arena);

    /* Takeover refuses a different or absent core before touching anything. */
    *reg(0xc120) = 0;
    assert(qdwc3_takeover(&d, BASE, QS, &saved, arena, QDWC3_ARENA_SIZE) == -2 && !csftrst && !qs[0x10 / 4]); checks++;
    assert(qdwc3_takeover(&d, BASE, QS, &saved, arena + 1, QDWC3_ARENA_SIZE) == -1); checks++;

    *reg(0xc120) = saved.gsnpsid;
    *reg(0xc704) = 1u << 31;            /* Firmware left run/stop set. */
    *reg(0xc40c) = 12;                  /* Stale events in firmware's freed buffer. */
    qs[0x10 / 4] = 0x0100;
    assert(qdwc3_takeover(&d, BASE, QS, &saved, arena, QDWC3_ARENA_SIZE) == 0 && d.stats.init_step == 8);
    assert(qs[0x10 / 4] == (0x0100 | (1u << 20) | (1u << 28)) && (qs[0x30 / 4] & (1u << 24)));
    assert(csftrst == 1 && *reg(0xc200) == 0x00102500 && *reg(0xc110) == ((0x30c12004u & ~(3u << 12)) | (2u << 12)));
    assert(*reg(0xc100) == 0xe && (d.stats.restored & 0x121) == 0x121);
    assert(*reg(0xc700) == (saved.dcfg & ~((0x7fu << 3) | 7u))); /* firmware DCFG, address 0, high speed */
    assert(*reg(0xc400) == (uint32_t)(uintptr_t)arena && *reg(0xc408) == (0x80000000u | 4096) && *reg(0xc40c) == 0);
    assert(*reg(0xc708) == 7 && *reg(0xc720) == 3 && (*reg(0xc704) >> 31) && !(*reg(0xc70c) & (1u << 22)));
    static const unsigned configured_eps[] = {0, 1, 3, 4, 5, 7, 8, 9};
    for (unsigned i = 0; i < 8; i++) assert(depcfg[configured_eps[i]] == 1);
    checks++;

    /* Connect: SETUP armed. Enumerate like macOS. */
    *reg(0xc70c) &= ~7u;
    inject(&d, dev_event(1)); qdwc3_poll(&d);        /* USB reset */
    inject(&d, dev_event(2)); qdwc3_poll(&d);        /* Connect done, high speed */
    assert(d.stats.connects == 1 && d.stats.resets == 1 && trb_type(0) == 2); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 1, 0, 0, 64, 0}, NULL, 0) == 18 && last_in[0] == 18 &&
           last_in[8] == 0x09 && last_in[9] == 0x12 && last_in[10] == 0x6d && last_in[11] == 0x31); checks++;
    assert(control(&d, (uint8_t[]){0, 5, 9, 0, 0, 0, 0, 0}, NULL, 0) == 0 && ((*reg(0xc700) >> 3) & 0x7f) == 9); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 2, 0, 0, 9, 0}, NULL, 0) == 9 && last_in[2] == 97); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 2, 0, 0, 255, 0}, NULL, 0) == 97 && last_in[9 + 5] == 2 && last_in[97 - 5] == 0x84); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 3, 3, 9, 4, 255, 0}, NULL, 0) == 26 && last_in[2] == 'A' && last_in[24] == '2'); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 6, 0, 0, 10, 0}, NULL, 0) == 10 && last_in[1] == 6); checks++;
    assert(control(&d, (uint8_t[]){0x80, 0, 0, 0, 0, 0, 2, 0}, NULL, 0) == 2 && last_in[0] == 1); checks++;
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 0x0f, 0, 0, 5, 0}, NULL, 0) == -1 && d.stats.stalls == 1); checks++; /* BOS stalls */
    assert(control(&d, (uint8_t[]){0, 9, 1, 0, 0, 0, 0, 0}, NULL, 0) == 0 && d.stats.configured);
    assert(*reg(0xc720) == 0x3bb); checks++; /* EP0/1 plus 0x81,0x02,0x82,0x83,0x04,0x84 */

    /* CDC requests on pipe 0 (interface 0). */
    const uint8_t coding[7] = {0x00, 0x10, 0x0e, 0x00, 0, 0, 8};
    assert(control(&d, (uint8_t[]){0x21, 0x20, 0, 0, 0, 0, 7, 0}, coding, 7) == 0 && !memcmp(d.pipe[0].line_coding, coding, 7));
    assert(control(&d, (uint8_t[]){0xa1, 0x21, 0, 0, 0, 0, 7, 0}, NULL, 0) == 7 && !memcmp(last_in, coding, 7)); checks++;
    assert(!qdwc3_ready(&d, 0));
    assert(control(&d, (uint8_t[]){0x21, 0x22, 3, 0, 0, 0, 0, 0}, NULL, 0) == 0 && qdwc3_ready(&d, 0) && !qdwc3_ready(&d, 1)); checks++;
    assert(control(&d, (uint8_t[]){0x21, 0x22, 1, 0, 2, 0, 0, 0}, NULL, 0) == 0 && qdwc3_ready(&d, 1)); checks++;
    assert(control(&d, (uint8_t[]){0x21, 0x23, 0, 0, 0, 0, 0, 0}, NULL, 0) == -1); checks++; /* SEND_BREAK stalls */

    /* Bulk OUT: one HS packet per transfer; exact-512 writes complete without a ZLP. */
    qdwc3_poll(&d);
    assert(trb_type(4) == 1 && trb_len(4) == 512);
    const uint8_t probe[] = {0, 1, 2, 0xff, 'Q', '\r', '\n'};
    memcpy(buffer(4), probe, sizeof(probe));
    complete(&d, 4, sizeof(probe));
    uint8_t in[2048];
    assert(qdwc3_read(&d, 0, in, sizeof(in)) == sizeof(probe) && !memcmp(in, probe, sizeof(probe)));
    assert(trb_len(4) == 512 && started[4] == 2); checks++;
    uint8_t packet[512];
    for (unsigned i = 0; i < 512; i++) packet[i] = (uint8_t)(i * 7);
    memcpy(buffer(4), packet, 512); complete(&d, 4, 512);
    memcpy(buffer(4), packet + 12, 500); complete(&d, 4, 500);
    assert(qdwc3_read(&d, 0, in, sizeof(in)) == 1012 && !memcmp(in, packet, 512) && !memcmp(in + 512, packet + 12, 500)); checks++;

    /* Bulk IN echo, with a ZLP after an exact-packet transfer and nothing lost on a busy endpoint. */
    assert(qdwc3_write(&d, 0, probe, sizeof(probe)) == sizeof(probe));
    assert(trb_len(5) == sizeof(probe) && !memcmp(buffer(5), probe, sizeof(probe)));
    assert(qdwc3_write(&d, 0, packet, 512) == 512 && started[5] == 1); /* queued while busy */
    complete(&d, 5, sizeof(probe));
    assert(started[5] == 2 && trb_len(5) == 512 && !memcmp(buffer(5), packet, 512));
    complete(&d, 5, 512);
    assert(started[5] == 3 && trb_len(5) == 0); /* ZLP */
    complete(&d, 5, 0);
    qdwc3_poll(&d);
    assert(started[5] == 3 && d.stats.tx_bytes == sizeof(probe) + 512); checks++;
    assert(qdwc3_write(&d, 1, probe, 3) == 3 && trb_len(9) == 3); checks++; /* second pipe independent */

    /* Unwritten event slot means DMA did not land in our buffer. */
    uint64_t events = d.stats.events;
    *reg(0xc40c) += 4; write_pointer = (write_pointer + 4) % 4096;
    memset(d.events + d.event_offset, 0xaa, 4);
    qdwc3_poll(&d);
    assert(d.stats.dma_mismatch == 1 && d.stats.events == events); checks++;

    /* Host reset clears configuration and endpoint enables; re-enumeration works. */
    inject(&d, dev_event(1)); qdwc3_poll(&d);
    assert(!d.stats.configured && !qdwc3_ready(&d, 0) && *reg(0xc720) == 3 && !((*reg(0xc700) >> 3) & 0x7f));
    assert(qdwc3_write(&d, 0, probe, 1) == 0);
    inject(&d, dev_event(2)); qdwc3_poll(&d);
    assert(control(&d, (uint8_t[]){0x80, 6, 0, 1, 0, 0, 18, 0}, NULL, 0) == 18); checks++;

    /* Stop halts the controller and drops the VBUS override so the host sees a detach. */
    qdwc3_stop(&d);
    assert(!(*reg(0xc704) >> 31) && (*reg(0xc70c) & (1u << 22)) && !*reg(0xc708));
    assert(!(qs[0x10 / 4] & ((1u << 20) | (1u << 28))) && !(qs[0x30 / 4] & (1u << 24)) && (qs[0x10 / 4] & 0x100)); checks++;

    /* A controller that never leaves halt is reported, not ignored. */
    static struct qdwc3 e;
    block_run = 1; *reg(0xc70c) = 1u << 22;
    assert(qdwc3_takeover(&e, BASE, QS, &saved, arena, QDWC3_ARENA_SIZE) == -9 && e.stats.init_step == 7); checks++;

    free(arena);
    printf("PASS: %u Qualcomm DWC3 takeover simulation checks (register model only)\n", checks);
    return 0;
}
