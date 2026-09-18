/* SPDX-License-Identifier: MIT */
/* Register-model guard/cleanup tests. No firmware or physical MMIO. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../platform/uefi/usb1-device.h"
#define BASE 0xa800000u
#define QS 0xa8f8800u
static uint32_t regs[0x10000 / 4], qs[0x100 / 4];
static unsigned reads, writes, takeovers, stops;
static int stuck, failure;
uint32_t qdwc3_rd(uintptr_t a)
{
    reads++;
    if (a >= QS && a < QS + sizeof(qs)) return qs[(a - QS) / 4];
    assert(a >= BASE && a < BASE + sizeof(regs) && !(a & 3));
    return regs[(a - BASE) / 4];
}
void qdwc3_wr(uintptr_t a, uint32_t v)
{
    writes++;
    if (a >= QS && a < QS + sizeof(qs)) { qs[(a - QS) / 4] = v; return; }
    assert(a >= BASE && a < BASE + sizeof(regs) && !(a & 3));
    regs[(a - BASE) / 4] = v;
    if (a == BASE + 0x30 && !(v & 1) && !stuck) regs[0x34 / 4] |= 1;
}
void qdwc3_delay_us(uint64_t us) { (void)us; }
int qdwc3_takeover(struct qdwc3 *d, uintptr_t r, uintptr_t q, const struct qdwc3_saved *s,
                  uint8_t *arena, uint64_t size)
{
    assert(r == BASE && q == QS && arena && size == QDWC3_ARENA_SIZE);
    assert((regs[0xc110 / 4] & 0x3000) == 0x2000);
    assert(s->gctl == 0x101005 && s->gsnpsid == 0x33313130);
    assert(s->gusb2phycfg == 0x102400 && s->gusb3pipectl == 0xb081402);
    d->saved = s;
    qs[0x10 / 4] |= 0x10100000; qs[0x30 / 4] |= 0x1000000;
    takeovers++;
    return failure;
}
void qdwc3_stop(struct qdwc3 *d) { assert(d->saved); stops++; }
static void reset(void)
{
    memset(regs, 0, sizeof(regs)); memset(qs, 0, sizeof(qs));
    reads = writes = takeovers = stops = 0; stuck = failure = 0;
    regs[0] = 0x1200030; regs[1] = 0x2000340; regs[0x30 / 4] = 1;
    regs[0xc110 / 4] = 0x101005; regs[0xc120 / 4] = 0x33313130;
    regs[0xc200 / 4] = 0x102400; regs[0xc2c0 / 4] = 0xb081402;
    qs[0x10 / 4] = 0x10000000;
}
int main(void)
{
    struct usb1_device d = {0};
    uint8_t *arena = aligned_alloc(4096, QDWC3_ARENA_SIZE);
    assert(arena); reset();
    assert(usb1_device_start(&d, 0xa600000, arena, QDWC3_ARENA_SIZE) == -1 && !reads && !writes);
    assert(usb1_device_start(&d, BASE, arena + 1, QDWC3_ARENA_SIZE) == -1 && !reads);
    assert(usb1_device_start(&d, BASE, arena, 4096) == -1 && !reads);
    regs[0xc120 / 4] = 0;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -2 && !writes);
    reset(); regs[0xc110 / 4] = 0x102005;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -2 && !writes);
    for (unsigned port = 0; port < 2; port++) {
        reset(); regs[(0x430 + 16 * port) / 4] = 1;
        assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -3 && !writes);
    }
    reset(); regs[1] = 0x1000340;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -3 && !writes);
    reset(); stuck = 1;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -4);
    assert(!takeovers && !d.active && regs[0xc110 / 4] == 0x101005);
    reset(); failure = -4;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -4);
    assert(takeovers == 1 && stops == 1 && !d.active && regs[0xc110 / 4] == 0x101005);
    assert(qs[0x10 / 4] == 0x10000000 && !qs[0x30 / 4]);
    reset();
    assert(!usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) && d.active);
    unsigned previous_writes = writes;
    assert(usb1_device_start(&d, BASE, arena, QDWC3_ARENA_SIZE) == -1 && writes == previous_writes);
    usb1_device_stop(&d);
    assert(!d.active && stops == 1 && regs[0xc110 / 4] == 0x101005);
    assert(qs[0x10 / 4] == 0x10000000 && !qs[0x30 / 4]);
    previous_writes = writes; usb1_device_stop(&d); assert(writes == previous_writes);
    free(arena);
    puts("PASS: USB1 device fallback guards, halt timeout, failed takeover cleanup, and exact restore");
}
