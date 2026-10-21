/* SPDX-License-Identifier: MIT */
/* GICv3/v4 and the EL2 physical timer, enough for a periodic tick at EL2.
 *
 * This is what lets q1n1 keep servicing its link when it is not the thing
 * running: m1n1's hypervisor does the same with a 1 kHz timer interrupt
 * (src/hv.c hv_tick), except Apple runs with VHE, so CNTP_* at EL2 is already
 * the EL2 timer there. This machine has HCR_EL2.E2H = 0, so the EL2 timer is
 * CNTHP_*_EL2 and its PPI is INTID 26.
 */
#pragma once
#include "efi.h"

struct q1n1_gic {
    uint64_t distributor, redistributor, sgi;  /* MMIO bases; sgi = redistributor + 0x10000 */
    uint64_t interval;                         /* counter ticks between interrupts */
    uint64_t ticks, spurious, other, serviced, skipped;
    uint32_t intid, last_intid, rate, ready;
    int32_t status;   /* q1n1_gic_init() result, kept for diagnosis */
};

/* Parse the MADT for the distributor and this CPU's redistributor, enable the
 * timer PPI and the CPU interface. Interrupts stay masked in PSTATE until
 * q1n1_gic_start(). Returns 0 on success. */
int q1n1_gic_init(struct q1n1_gic *gic, uint64_t rsdp, uint64_t timer_hz, uint32_t rate);
/* Arm the next tick and unmask IRQs. */
void q1n1_gic_start(struct q1n1_gic *gic);
/* Handle one IRQ exception: returns the INTID, already acknowledged and ended.
 * The timer tick is re-armed here; the caller decides what work to run. */
uint32_t q1n1_gic_handle(struct q1n1_gic *gic);
/* Mask IRQs, disable the timer and the PPI. Safe to call when not ready. */
void q1n1_gic_stop(struct q1n1_gic *gic);
