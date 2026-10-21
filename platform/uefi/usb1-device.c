/* SPDX-License-Identifier: MIT */
#include "usb1-device.h"

#define CORE 0x0a800000u
#define QS 0x0a8f8800u /* UsbConfigDxe BIOS312 qscratch table, controller 1. */
static uint32_t rd(uint32_t off) { return qdwc3_rd(CORE + off); }
static void wr(uint32_t off, uint32_t value) { qdwc3_wr(CORE + off, value); }

void usb1_device_stop(struct usb1_device *d)
{
    if (!d->active) return;
    qdwc3_stop(&d->usb);
    qdwc3_wr(QS + 0x10, d->qs_hs);
    qdwc3_wr(QS + 0x30, d->qs_ss);
    wr(0xc110, d->saved.gctl);
    d->active = 0;
    /* The caller must initialize xHCI again before using its old rings. */
}

int usb1_device_start(struct usb1_device *d, uintptr_t base, uint8_t *arena, uint64_t size)
{
    if (d->active || base != CORE || !arena || size < QDWC3_ARENA_SIZE ||
        ((uintptr_t)arena & 0xfff)) return -1;
    if (rd(0xc120) != 0x33313130 || ((rd(0xc110) >> 12) & 3) != 1) return -2;
    uint32_t cap = rd(0) & 0xff;
    /* These are the two ports of the observed USB1 core. Never switch an
     * attached device away from its host controller. */
    if (cap != 0x30 || (rd(4) >> 24) != 2 ||
        ((rd(cap + 0x400) | rd(cap + 0x410)) & 1u)) return -3;
    d->saved = (struct qdwc3_saved){
        .gsbuscfg0 = rd(0xc100), .gsbuscfg1 = rd(0xc104), .gctl = rd(0xc110),
        .guctl = rd(0xc12c), .guctl1 = rd(0xc11c), .guctl2 = rd(0xc19c),
        .gusb2phycfg = rd(0xc200), .gusb3pipectl = rd(0xc2c0),
        .gfladj = rd(0xc630), .dcfg = rd(0xc700), .gsnpsid = rd(0xc120)};
    d->qs_hs = qdwc3_rd(QS + 0x10);
    d->qs_ss = qdwc3_rd(QS + 0x30);
    wr(cap, rd(cap) & ~1u); /* Stop xHCI before changing the DWC3 direction. */
    unsigned n;
    for (n = 0; n < 1000 && !(rd(cap + 4) & 1u); n++) qdwc3_delay_us(100);
    if (!(rd(cap + 4) & 1u)) return -4;
    wr(0xc110, (d->saved.gctl & ~(3u << 12)) | (2u << 12));
    qdwc3_delay_us(50000);
    d->active = 1;
    int rc = qdwc3_takeover(&d->usb, CORE, QS, &d->saved, arena, size);
    if (rc) usb1_device_stop(d);
    return rc;
}
