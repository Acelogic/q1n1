/* SPDX-License-Identifier: MIT */
/* See battery.h. Pure decoding and formatting; no firmware calls. */
#include "battery.h"

static uint32_t dword(const uint8_t *p, unsigned index)
{
    p += 4 * index;
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

int q1n1_battery_decode(const uint8_t bst[Q1N1_PMGK_BST_BYTES],
                        const uint8_t bix[Q1N1_PMGK_BIX_BYTES],
                        const uint8_t psr[Q1N1_PMGK_PSR_BYTES],
                        struct q1n1_battery *out)
{
    /* \_SB.CMBD._BST keeps only the low nibble of the state word. */
    out->state = dword(bst, 0) & 0x0f;
    out->rate = dword(bst, 1);
    out->remaining = dword(bst, 2);
    out->voltage = dword(bst, 3);
    /* _BIX order: revision, power unit, design capacity, last full charge
     * capacity, technology, design voltage, warning, low, cycle count, ... */
    out->unit = dword(bix, 1);
    out->design = dword(bix, 2);
    out->full = dword(bix, 3);
    out->cycles = dword(bix, 8);
    out->ac = dword(psr, 0);
    out->percent = Q1N1_BATTERY_UNKNOWN;

    const uint32_t unknown = Q1N1_BATTERY_UNKNOWN;
    if (out->full && out->full != unknown && out->remaining != unknown) {
        uint64_t p = ((uint64_t)out->remaining * 100 + out->full / 2) / out->full;
        out->percent = p > 100 ? 100 : (uint32_t)p;
    }
    /* Plausibility, not policy: reject what a stale or foreign reply would
     * look like. A laptop pack is 1..10 Ah (or Wh-scaled mWh), 5..30 V. */
    if (out->unit > 1 || out->ac > 1) return 0;
    if (dword(bst, 0) & ~0x0fu) return 0;
    if ((out->state & (Q1N1_BATTERY_CHARGING | Q1N1_BATTERY_DISCHARGING)) ==
        (Q1N1_BATTERY_CHARGING | Q1N1_BATTERY_DISCHARGING)) return 0;
    if (!out->design || out->design == unknown || out->design > 500000) return 0;
    if (!out->full || out->full == unknown || out->full > out->design * 2) return 0;
    if (out->remaining == unknown || out->remaining > out->full + out->full / 5) return 0;
    if (out->voltage != unknown && (out->voltage < 5000 || out->voltage > 30000)) return 0;
    if (out->rate != unknown && out->rate > 500000) return 0;
    if (out->cycles != unknown && out->cycles > 100000) return 0;
    return 1;
}

struct writer { char *out; unsigned size, used; };
static void put(struct writer *w, const char *s)
{
    while (*s) {
        if (w->used + 1 < w->size) w->out[w->used] = *s;
        w->used++;
        s++;
    }
}
static void put_number(struct writer *w, uint32_t v)
{
    char digits[11];
    unsigned n = 0;
    do { digits[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < sizeof(digits));
    char text[12];
    for (unsigned i = 0; i < n; i++) text[i] = digits[n - 1 - i];
    text[n] = 0;
    put(w, text);
}

unsigned q1n1_battery_format(const struct q1n1_battery *b, char *out, unsigned size)
{
    struct writer w = {out, size, 0};
    const uint32_t unknown = Q1N1_BATTERY_UNKNOWN;
    const char *power = b->unit ? " mA" : " mW", *energy = b->unit ? " mAh" : " mWh";
    if (b->percent == unknown) put(&w, "?%");
    else { put_number(&w, b->percent); put(&w, "%"); }
    put(&w, b->state & Q1N1_BATTERY_CHARGING ? " charging" :
            b->state & Q1N1_BATTERY_DISCHARGING ? " discharging" : " idle");
    if (b->state & Q1N1_BATTERY_CRITICAL) put(&w, " CRITICAL");
    if (b->state & Q1N1_BATTERY_LIMITING) put(&w, " (charge limited)");
    put(&w, b->ac ? " on AC" : " on battery");
    if (b->rate != unknown && b->rate) {
        put(&w, ", ");
        put_number(&w, b->rate);
        put(&w, power);
        put(&w, b->state & Q1N1_BATTERY_CHARGING ? " in" : " out");
        /* Time estimate from the ACPI rate: to empty or to full, in minutes. */
        uint64_t left = 0;
        if (b->state & Q1N1_BATTERY_DISCHARGING) left = (uint64_t)b->remaining * 60 / b->rate;
        else if ((b->state & Q1N1_BATTERY_CHARGING) && b->full > b->remaining)
            left = (uint64_t)(b->full - b->remaining) * 60 / b->rate;
        if (left && left < 100000) {
            put(&w, ", ~");
            put_number(&w, (uint32_t)(left / 60));
            put(&w, "h");
            put_number(&w, (uint32_t)(left % 60 / 10));
            put_number(&w, (uint32_t)(left % 10));
            put(&w, b->state & Q1N1_BATTERY_CHARGING ? " to full" : " left");
        }
    }
    put(&w, ", ");
    put_number(&w, b->remaining);
    put(&w, "/");
    put_number(&w, b->full);
    put(&w, energy);
    if (b->voltage != unknown) { put(&w, ", "); put_number(&w, b->voltage); put(&w, " mV"); }
    if (b->cycles != unknown) { put(&w, ", "); put_number(&w, b->cycles); put(&w, " cycles"); }
    if (size) out[w.used < size ? w.used : size - 1] = 0;
    return w.used < size ? w.used : size - 1;
}
