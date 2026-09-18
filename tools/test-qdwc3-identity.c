/* SPDX-License-Identifier: MIT */
/* Simultaneous EP0 replies must retain distinct identities and buffers. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../platform/uefi/qcom-dwc3.c"
uint32_t qdwc3_rd(uintptr_t p) { (void)p; assert(0); return 0; }
void qdwc3_wr(uintptr_t p, uint32_t v) { (void)p; (void)v; assert(0); }
void qdwc3_delay_us(uint64_t us) { (void)us; assert(0); }
static void serial_is(const struct qdwc3 *d, const char *serial)
{
    assert(d->ep0_length == 2 + 2 * strlen(serial));
    assert(d->ep0_buffer[0] == d->ep0_length && d->ep0_buffer[1] == 3);
    for (unsigned n = 0; serial[n]; n++) {
        assert(d->ep0_buffer[2 + 2*n] == (uint8_t)serial[n]);
        assert(!d->ep0_buffer[3 + 2*n]);
    }
}
int main(void)
{
    struct qdwc3 dock = {.regs = 0xa600000}, direct = {.regs = 0xa800000};
    assert(!get_descriptor(&dock, 0x303, 64));
    assert(!get_descriptor(&direct, 0x303, 64));
    assert(dock.ep0_buffer != direct.ep0_buffer);
    serial_is(&dock, "A16-Q1N1-EL2");
    serial_is(&direct, "A16-Q1N1-EL2B");
    assert(!get_descriptor(&direct, 0x301, 64));
    serial_is(&dock, "A16-Q1N1-EL2");
    puts("PASS: independent USB0/USB1 identities and pending EP0 string replies");
}
