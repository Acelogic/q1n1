/* SPDX-License-Identifier: MIT */
/* Battery and AC status from the A16's own ACPI contract.
 *
 * The DSDT's control-method battery (\_SB.CMBD, PNP0C0A) and power source
 * (\_SB.CMPS, ACPI0003) read everything from \_SB.PMGK, the PMIC GLINK
 * GenericSerialBus region. Its field offsets are register identifiers on the
 * same PmicGlinkDxe transport q1n1 already uses for UCSI (VERSION 0x20100):
 *
 *   BSTD 0x101A8, 16 bytes: _BST  state, present rate, remaining, voltage
 *   BIXD 0x10110, 68 bytes: _BIX  revision, unit, design, last full, ...
 *   PSRD 0x101D8,  4 bytes: _PSR  1 = on AC power
 *
 * Each read returns the ACPI package values as little-endian dwords, the
 * GenericSerialBus status/length prefix excluded (VERSION reads as 0x0210).
 * The decoder is transport-neutral. The boot window reads the registers
 * through the firmware (ucsi-console.c); the EL2 proxy, after
 * ExitBootServices, through its own GLINK channel (live-battery.h). */
#pragma once
#include <stdint.h>

#define Q1N1_PMGK_BST 0x101a8u
#define Q1N1_PMGK_BST_BYTES 16u
#define Q1N1_PMGK_BIX 0x10110u
#define Q1N1_PMGK_BIX_BYTES 0x44u
#define Q1N1_PMGK_PSR 0x101d8u
#define Q1N1_PMGK_PSR_BYTES 4u

/* _BST state bits (ACPI 6.5, 10.2.2.11). */
#define Q1N1_BATTERY_DISCHARGING 1u
#define Q1N1_BATTERY_CHARGING 2u
#define Q1N1_BATTERY_CRITICAL 4u
#define Q1N1_BATTERY_LIMITING 8u
#define Q1N1_BATTERY_UNKNOWN 0xffffffffu

struct q1n1_battery {
    uint32_t state, rate, remaining, voltage;        /* _BST */
    uint32_t unit, design, full, cycles;             /* _BIX: 0 = mW/mWh, 1 = mA/mAh */
    uint32_t ac;                                     /* _PSR */
    uint32_t percent;                                /* remaining/full, 0..100, or UNKNOWN */
};

/* Decode raw register bytes. Returns 1 when every value is plausible for a
 * laptop battery, 0 otherwise (out is still filled for diagnostics). */
int q1n1_battery_decode(const uint8_t bst[Q1N1_PMGK_BST_BYTES],
                        const uint8_t bix[Q1N1_PMGK_BIX_BYTES],
                        const uint8_t psr[Q1N1_PMGK_PSR_BYTES],
                        struct q1n1_battery *out);

/* One-line summary, e.g. "73% charging on AC, 45000 mW in, 51230/70000 mWh,
 * 17120 mV, 12 cycles". Returns the length written, always NUL-terminated. */
unsigned q1n1_battery_format(const struct q1n1_battery *b, char *out, unsigned size);
