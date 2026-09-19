/* SPDX-License-Identifier: MIT */
/* A vanished host must not leave device DMA live across a stage handoff. */
#include <assert.h>
#include <stdio.h>
#include "../platform/uefi/qcom-dwc3.c"
static uint32_t control, status, hs, ss, resets, stuck;
static uint64_t elapsed;
uint32_t qdwc3_rd(uintptr_t address)
{
    switch (address) {
    case 0xa600000 + DWC3_DCTL: return control;
    case 0xa600000 + DWC3_DSTS: return status;
    case 0xa6f8800 + QSCRATCH_HS_PHY_CTRL: return hs;
    case 0xa6f8800 + QSCRATCH_SS_PHY_CTRL: return ss;
    default: assert(0); return 0;
    }
}
void qdwc3_wr(uintptr_t address, uint32_t value)
{
    switch (address) {
    case 0xa600000 + DWC3_DEVTEN: assert(value == 0); break;
    case 0xa600000 + DWC3_DCTL:
        control = value;
        if (value & DWC3_DCTL_CSFTRST) {
            resets++;
            if (!stuck) { control &= ~DWC3_DCTL_CSFTRST; status = DWC3_DSTS_DEVCTRLHLT; }
        }
        break;
    case 0xa6f8800 + QSCRATCH_HS_PHY_CTRL: hs = value; break;
    case 0xa6f8800 + QSCRATCH_SS_PHY_CTRL: ss = value; break;
    default: assert(0);
    }
}
void qdwc3_delay_us(uint64_t us) { elapsed += us; }
int main(void)
{
    struct qdwc3 d = {.regs = 0xa600000, .qscratch = 0xa6f8800};
    control = DWC3_DCTL_RUN_STOP;
    hs = UTMI_OTG_VBUS_VALID | SW_SESSVLD_SEL; ss = LANE0_PWR_PRESENT;
    qdwc3_stop(&d);
    assert(resets == 1 && status == DWC3_DSTS_DEVCTRLHLT);
    assert(!hs && !ss && !(control & DWC3_DCTL_RUN_STOP));
    qdwc3_stop(&d);
    assert(resets == 1); /* A graceful halt needs no reset. */
    stuck = 1; status = 0; control = DWC3_DCTL_RUN_STOP; elapsed = 0;
    qdwc3_stop(&d);
    assert(resets == 2 && !status && elapsed <= 1600000);
    puts("PASS: graceful stop, stalled-halt reset, bounded failed reset");
    return 0;
}
