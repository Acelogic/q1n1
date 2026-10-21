/* SPDX-License-Identifier: MIT */
/* See gicv3.h. Observed on the A16 (UX3607OA, BIOS312) after ExitBootServices:
 * GICD 0x17000000 (PIDR2 rev 4), redistributors from 0x17080000 with a 0x40000
 * stride, all awake, nothing enabled and GICD_CTLR = ARE_NS only, because
 * firmware disables interrupts on the way out. */
#include "gicv3.h"

#define GICD_CTLR 0x0000
#define GICD_CTLR_ENABLE_G1 (1u << 0)
#define GICD_CTLR_ENABLE_G1A (1u << 1)
#define GICD_CTLR_ARE_NS (1u << 4)
#define GICD_CTLR_RWP (1u << 31)

#define GICR_CTLR 0x0000
#define GICR_CTLR_RWP (1u << 3)
#define GICR_TYPER 0x0008
#define GICR_WAKER 0x0014
#define GICR_WAKER_SLEEP (1u << 1)
#define GICR_WAKER_CHILDREN_ASLEEP (1u << 2)
#define GICR_IGROUPR0 0x0080
#define GICR_ISENABLER0 0x0100
#define GICR_ICENABLER0 0x0180
#define GICR_ICPENDR0 0x0280
#define GICR_IPRIORITYR 0x0400
#define GICR_TYPER_VLPIS (1u << 1)
#define SGI_OFFSET 0x10000

#define EL2_TIMER_INTID 26 /* PPI 10: the non-secure EL2 physical timer */
#define TICK_PRIORITY 0x80
#define PRIORITY_MASK 0xf0
#define SPURIOUS 1023

#define CNTx_ENABLE 1u
#define CNTx_IMASK 2u

static uint32_t rd32(uint64_t address) { return *(volatile uint32_t *)(uintptr_t)address; }
static void wr32(uint64_t address, uint32_t value) { *(volatile uint32_t *)(uintptr_t)address = value; }
static uint64_t rd64(uint64_t address) { return *(volatile uint64_t *)(uintptr_t)address; }
static void wr8(uint64_t address, uint8_t value) { *(volatile uint8_t *)(uintptr_t)address = value; }
static void sync(void) { __asm__ volatile("dsb sy\n isb" ::: "memory"); }

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static int signature(const uint8_t *p, const char *text)
{
    for (unsigned n = 0; n < 4; n++) if (p[n] != (uint8_t)text[n]) return 0;
    return 1;
}

/* MADT walk: GICD base from a type 0x0C entry, this CPU's redistributor from
 * the GICC entries (offset 60) or a type 0x0E discovery range. */
static const uint8_t *find_madt(uint64_t rsdp_address)
{
    const uint8_t *rsdp = (const void *)(uintptr_t)rsdp_address;
    if (!rsdp || !signature(rsdp, "RSD ")) return 0;  /* "RSD PTR " */
    const uint8_t *xsdt = (const void *)(uintptr_t)get64(rsdp + 24);
    if (!xsdt || !signature(xsdt, "XSDT")) return 0;
    uint32_t size = get32(xsdt + 4);
    if (size < 36 || size > (1u << 20)) return 0;
    for (uint32_t offset = 36; offset + 8 <= size; offset += 8) {
        const uint8_t *table = (const void *)(uintptr_t)get64(xsdt + offset);
        if (table && signature(table, "APIC")) return table;
    }
    return 0;
}

static uint64_t this_affinity(void)
{
    uint64_t mpidr;
    __asm__ volatile("mrs %0, mpidr_el1" : "=r"(mpidr));
    return (mpidr & 0xffffff) | ((mpidr >> 32) & 0xff) << 24;
}

/* GICv4 has VLPI frames, so its redistributors are 0x40000 apart; GICv3 is
 * 0x20000. Take it from the first one rather than assuming a revision. */
static uint64_t redistributor_stride(uint64_t base)
{
    return (rd64(base + GICR_TYPER) & GICR_TYPER_VLPIS) ? 0x40000 : 0x20000;
}
static uint64_t match_redistributor(uint64_t base, uint64_t affinity, uint64_t span)
{
    uint64_t stride = redistributor_stride(base);
    for (uint64_t offset = 0; offset + stride <= span; offset += stride) {
        uint64_t candidate = base + offset;
        uint64_t typer = rd64(candidate + GICR_TYPER);
        if ((typer >> 32) == affinity) return candidate;
        if ((typer >> 4) & 1) break; /* Last redistributor in this range */
    }
    return 0;
}

int q1n1_gic_init(struct q1n1_gic *gic, uint64_t rsdp, uint64_t timer_hz, uint32_t rate)
{
    for (unsigned n = 0; n < sizeof(*gic); n++) ((uint8_t *)gic)[n] = 0;
    gic->intid = EL2_TIMER_INTID;
    gic->rate = rate;
    if (!timer_hz || !rate) return gic->status = -1;
    gic->interval = timer_hz / rate;
    if (!gic->interval) return gic->status = -1;

    const uint8_t *madt = find_madt(rsdp);
    if (!madt) return gic->status = -2;
    uint32_t size = get32(madt + 4);
    if (size < 44 || size > (1u << 20)) return gic->status = -2;
    uint64_t affinity = this_affinity();
    for (uint32_t offset = 44; offset + 2 <= size;) {
        const uint8_t *entry = madt + offset;
        uint8_t kind = entry[0], length = entry[1];
        if (length < 2 || offset + length > size) break;
        if (kind == 0x0c && length >= 24) {
            gic->distributor = get64(entry + 8);
        } else if (kind == 0x0b && length >= 76 && !gic->redistributor) {
            uint64_t candidate = get64(entry + 60); /* GICR Base Address */
            if (candidate && rd64(candidate + GICR_TYPER) >> 32 == affinity) gic->redistributor = candidate;
        } else if (kind == 0x0e && length >= 16 && !gic->redistributor) {
            uint64_t base = get64(entry + 4), span = get32(entry + 12);
            uint64_t found = match_redistributor(base, affinity, span);
            if (found) gic->redistributor = found;
        }
        offset += length;
    }
    if (!gic->distributor || !gic->redistributor) return gic->status = -3;
    gic->sgi = gic->redistributor + SGI_OFFSET;

    /* The redistributor must be awake before its registers mean anything. */
    uint32_t waker = rd32(gic->redistributor + GICR_WAKER);
    if (waker & GICR_WAKER_SLEEP) {
        wr32(gic->redistributor + GICR_WAKER, waker & ~GICR_WAKER_SLEEP);
        for (unsigned n = 0; n < 100000; n++)
            if (!(rd32(gic->redistributor + GICR_WAKER) & GICR_WAKER_CHILDREN_ASLEEP)) break;
    }
    if (rd32(gic->redistributor + GICR_WAKER) & GICR_WAKER_CHILDREN_ASLEEP) return gic->status = -4;

    /* Affinity routing with Group 1 enabled, as Linux's gic-v3 does. */
    uint32_t control = rd32(gic->distributor + GICD_CTLR);
    wr32(gic->distributor + GICD_CTLR, control | GICD_CTLR_ARE_NS | GICD_CTLR_ENABLE_G1A | GICD_CTLR_ENABLE_G1);
    for (unsigned n = 0; n < 100000; n++)
        if (!(rd32(gic->distributor + GICD_CTLR) & GICD_CTLR_RWP)) break;

    /* The timer PPI: Group 1 non-secure, level triggered (ICFGR default), one
     * priority below the mask, no stale pending state. */
    wr32(gic->sgi + GICR_IGROUPR0, rd32(gic->sgi + GICR_IGROUPR0) | (1u << EL2_TIMER_INTID));
    wr8(gic->sgi + GICR_IPRIORITYR + EL2_TIMER_INTID, TICK_PRIORITY);
    wr32(gic->sgi + GICR_ICPENDR0, 1u << EL2_TIMER_INTID);
    wr32(gic->sgi + GICR_ISENABLER0, 1u << EL2_TIMER_INTID);
    for (unsigned n = 0; n < 100000; n++)
        if (!(rd32(gic->redistributor + GICR_CTLR) & GICR_CTLR_RWP)) break;
    sync();

    /* CPU interface. ICC_SRE_EL2.SRE is already set by firmware on this
     * machine; set it anyway so the sequence does not depend on that. */
    uint64_t sre;
    __asm__ volatile("mrs %0, icc_sre_el2" : "=r"(sre));
    __asm__ volatile("msr icc_sre_el2, %0" : : "r"(sre | 1));
    __asm__ volatile("isb");
    __asm__ volatile("msr icc_pmr_el1, %0" : : "r"((uint64_t)PRIORITY_MASK));
    __asm__ volatile("msr icc_bpr1_el1, %0" : : "r"((uint64_t)0));
    __asm__ volatile("msr icc_igrpen1_el1, %0" : : "r"((uint64_t)1));
    __asm__ volatile("isb");
    gic->ready = 1;
    return 0;
}

void q1n1_gic_start(struct q1n1_gic *gic)
{
    if (!gic->ready) return;
    __asm__ volatile("msr cnthp_tval_el2, %0" : : "r"(gic->interval));
    __asm__ volatile("msr cnthp_ctl_el2, %0" : : "r"((uint64_t)CNTx_ENABLE));
    __asm__ volatile("isb");
    __asm__ volatile("msr daifclr, #2" ::: "memory"); /* unmask IRQ */
}

uint32_t q1n1_gic_handle(struct q1n1_gic *gic)
{
    uint64_t intid;
    __asm__ volatile("mrs %0, icc_iar1_el1" : "=r"(intid));
    __asm__ volatile("dsb sy" ::: "memory");
    gic->last_intid = (uint32_t)intid;
    if ((intid & 0xffffff) == SPURIOUS) {
        gic->spurious++;
        return SPURIOUS;
    }
    if ((uint32_t)intid == gic->intid) {
        gic->ticks++;
        /* Re-arm before the end-of-interrupt so a level-triggered timer does
         * not immediately re-assert. */
        __asm__ volatile("msr cnthp_tval_el2, %0" : : "r"(gic->interval));
        __asm__ volatile("isb");
    } else {
        gic->other++;
    }
    __asm__ volatile("msr icc_eoir1_el1, %0" : : "r"(intid));
    __asm__ volatile("isb");
    return (uint32_t)intid;
}

void q1n1_gic_stop(struct q1n1_gic *gic)
{
    __asm__ volatile("msr daifset, #2" ::: "memory");
    __asm__ volatile("msr cnthp_ctl_el2, %0" : : "r"((uint64_t)CNTx_IMASK));
    __asm__ volatile("isb");
    if (!gic->ready) return;
    wr32(gic->sgi + GICR_ICENABLER0, 1u << gic->intid);
    sync();
    gic->ready = 0;
}
