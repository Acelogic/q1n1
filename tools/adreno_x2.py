# SPDX-License-Identifier: MIT
#
# The GPU and GMU OPP tables, memory carve-outs, BCMs and register regions cited "glymur.dtsi" are
# transcribed from Linux arch/arm64/boot/dts/qcom/glymur.dtsi (BSD-3-Clause,
# 3rdparty_licenses/LICENSE.BSD-3.qcom):
#   Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# The X2-85 protect and non-context tables are in adreno_x2_protect.py (GPL-2.0-only).
"""Adreno X2-90 (glymur, catalog chip 0x44070001) registers for the A16 GPU bring-up.

The one source of truth for the Stage-1 host tools (tools/a16gpu.py). It merges
the two research drafts -- gpu-bringup-q1n1/adreno_x2_map.py and
adreno-hw/adreno_x2e_regs.h (generated from the upstream freedreno XML) --
with the skeptic reviews' corrections applied:

  * GPU_CC_AHB is BRANCH_HALT_DELAY: nothing waits on its CLK_OFF bit.
  * GBIF (0x03d0f000..) is a CX block and may be written before GX is on.
  * CX GDSC completion is POWER_UP_COMPLETE, bit 16 of CFG_GDSCR 0x03d99084.
  * SPTPRAC and the legacy GMU DTCM defines are gone (see DROPPED).
  * The first GPU-block read is CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS 0x03d9f014.
  * CX_MISC_TCM_RET_CNTL 0x03d9e0e4 must read 0 (else the GMU takes the
    warm-boot path).
  * Runtime chip id = 0x44070001 | popcount(SLICE_ENABLE_FINAL & 0xf) << 4.

Every register carries its block, its power domain and the evidence for both:

  domain  AO  always on while the SoC runs: GCC, the GIC distributor, DRAM.
          CX  the GPU's CX side: GPUCC itself, and behind the CX GDSC the GMU,
              RSCC, CX_MISC, GBIF and the Adreno SMMU.
          GX  the GPU core and everything the GMU powers. Stage 1 never
              touches GX; tools/a16gpu.py refuses it before a byte is sent.

  gate    what must already be proven on before the register may be touched:
          always       AO.
          gpu_cfg_ahb  GCC_GPU_CFG_AHB_CBCR running and GCC_GPU_BCR released:
                       the GPUCC register file is reachable.
          gpucc_critical  gpu_cfg_ahb, and the three critical GPUCC CBCRs (CB,
                       CXO_AON, RSCC_XO_AON) read CLK_ENABLE set: the state
                       gpucc probe leaves before a6xx_gpu_init reads CX_MISC
                       FREQ_LIMIT (fw_devlink: gpu iommus -> adreno_smmu ->
                       gpucc), so the only state with a Linux precedent.
          cx_fuse      gpucc_critical, and the CX GDSC on (CFG_GDSCR bit 16) and
                       not under HW control. REVIEW-2: CX_MISC (A16 HWIO
                       GPU_CX_MISC_BLK_DEC0/1, "CX", not one of the GPU_GMU_AO
                       blocks) is behind the CX GDSC as far as anything shows,
                       and Linux's FREQ_LIMIT read happens while genpd still
                       holds a boot-on CX GDSC on (sync_state), so the Linux
                       precedent does not cover a collapsed GDSC.
          cx_gmu       CX GDSC on (CFG_GDSCR bit 16) and not under HW control
                       (GDSCR bit 1: the GMU/RSC could collapse it between the
                       status read and the access), with the clock set Linux
                       holds while it touches these blocks: GPU_CC_AHB enabled,
                       CX_GMU, CXO, HUB_CX_INT, RSCC_HUB_AON and GCC GEMNOC_GFX
                       running (glymur.dtsi gmu clocks).
          cx_smmu      CX GDSC on and not under HW control, GPU_CC_AHB enabled and GPU_CC_GPU_SMMU_VOTE
                       running (glymur.dtsi adreno_smmu clocks/power-domains).
          gx           the GMU reports GX on (Stage 3 OOB GPU_SET). Never here.
          never        a reset (BCR) or a register Stage 1 must not touch.

Evidence tags: LNX = upstream Linux 7.3-rc4 (drivers/clk/qcom, drivers/gpu/drm/msm),
plus master's glymur.dtsi, a6xx_gmu.c and gdsc.c where marked (master)), XML = the
freedreno register XML behind adreno_x2e_regs.h, ACPI = the A16's own DSDT/IORT,
A16 = observed on this laptop through q1n1, REVIEW = a skeptic-review correction.
INFERRED marks what nobody has shown on this unit yet.
"""
from __future__ import annotations

from dataclasses import dataclass

DOMAINS = ('AO', 'CX', 'GX')
GATES = ('always', 'gpu_cfg_ahb', 'gpucc_critical', 'cx_fuse', 'cx_misc', 'cx_gmu', 'cx_smmu', 'gmu_reset', 'gx',
         'never')


@dataclass(frozen=True)
class Reg:
    name: str
    addr: int
    block: str
    domain: str
    gate: str
    access: str          # '' (listed only), 'r', 'rw'
    evidence: str
    notes: str = ''
    count: int = 1       # register arrays: count entries at addr + stride * i
    stride: int = 4
    halt: str = ''       # branch clocks: HALT, HALT_VOTED, HALT_DELAY (LNX clk_branch.halt_check)
    inner: int = 1       # two-level arrays (RSC TCS n, command k): index = n * inner + k
    inner_stride: int = 0

    def at(self, index=0):
        if not 0 <= index < self.count:
            raise IndexError(f'{self.name}[{index}] outside 0..{self.count - 1}')
        outer, inner = divmod(index, self.inner)
        return self.addr + self.stride * outer + self.inner_stride * inner


@dataclass(frozen=True)
class Block:
    name: str
    base: int
    size: int
    domain: str
    gate: str
    evidence: str


# --- address windows -----------------------------------------------------------
# Used to classify an address that is not a named register (it is then refused
# anyway) and to check that every register sits in the block it claims.
BLOCKS = (
    Block('GCC', 0x00100000, 0x200000, 'AO', 'always', 'LNX glymur.dtsi gcc: clock-controller@100000'),
    Block('GICD', 0x17000000, 0x10000, 'AO', 'always',
          'A16 docs/A16-Q1N1-PROXY.md "GICD / GICR 0x17000000 / 0x17080000" from the MADT'),
    Block('CMD_DB', 0x81C60000, 0x20000, 'AO', 'always', 'LNX glymur.dtsi aop-cmd-db@81c60000 (reserved, no-map)'),
    Block('GPU', 0x03D00000, 0x0F000, 'GX', 'gx', 'LNX glymur.dtsi gpu@3d00000 reg[0]; XML A8XX RBBM/CP/...'),
    Block('GBIF', 0x03D0F000, 0x01000, 'CX', 'cx_gmu',
          'REVIEW: GBIF 0x03d0f000 is CX, writable before GX (a6xx_gmu.c writes GBIF_CX_CONFIG in fw_start)'),
    Block('GPU', 0x03D10000, 0x40000, 'GX', 'gx', 'LNX glymur.dtsi gpu reg[0] (0x6c000); XML A8XX'),
    Block('RSCC', 0x03D50000, 0x10000, 'CX', 'cx_gmu', 'LNX a6xx_gmu.c "RSCC lives at GPU base + 0x50000" (a8xx)'),
    Block('GXCLKCTL', 0x03D64000, 0x06000, 'GX', 'gx',
          'LNX glymur.dtsi gxclkctl@3d64000 power-domains GFX+GMXC+CX GDSC'),
    Block('GMU_GX', 0x03D6A000, 0x02000, 'GX', 'gx', 'XML GMU_GX_SPTPRAC_* 0x1a880.. (dropped; plan stage 4 keeps it trapped)'),
    Block('GMU', 0x03D6C000, 0x24000, 'CX', 'cx_gmu',
          'LNX glymur.dtsi gmu@3d6c000 power-domains cx=GPU_CC_CX_GDSC; clocks ahb,gmu,cxo,memnoc,hub,rscc'),
    Block('GPUCC', 0x03D90000, 0x9800, 'CX', 'gpu_cfg_ahb',
          'LNX glymur.dtsi gpucc@3d90000 power-domains MX+CX; register interface on GCC_GPU_CFG_AHB_CLK (critical)'),
    Block('CX_MISC', 0x03D9E000, 0x2000, 'CX', 'cx_gmu', 'LNX glymur.dtsi gpu reg[1] "cx_mem" 0x03d9e000/0x2000'),
    Block('SMMU', 0x03DA0000, 0x40000, 'CX', 'cx_smmu',
          'LNX glymur.dtsi adreno_smmu@3da0000 clocks GPU_CC_GPU_SMMU_VOTE_CLK, power-domains GPU_CC_CX_GDSC; ACPI IORT'),
    Block('APPS_RSC', 0x18900000, 0x30000, 'AO', 'always',
          'LNX glymur.dtsi apps_rsc rsc@18900000 drv-0/1/2, qcom,drv-id=2, qcom,tcs-offset=0xd00 (Stage 3 host votes)'),
    Block('IMEM', 0x14680000, 0x1000, 'AO', 'always',
          'LNX glymur.dtsi sram@14680000 "qcom,glymur-imem" (Linux writes pil-reloc-info at +0x94c in this page, '
          'so the page is NS-accessible); read-only here (reset-reason)'),
    # ddr-survey (a16gpu_ddr.py), read-only and opt-in: the LLCC slice status words Linux's llcc-qcom.c reads
    # through the broadcast bases (v6 offsets), and the rpmh-stats DDR block qcom_stats.c reads.
    Block('LLCC_BCAST', 0x20400000, 0x400000, 'AO', 'always',
          'LNX glymur.dtsi system-cache-controller@21800000 llcc_broadcast_base 0x20400000, '
          'llcc_broadcast_and_base 0x20600000 (read-only here: ddr-survey --llcc)'),
    Block('RPMH_STATS', 0x0C30F000, 0x400, 'AO', 'always',
          'LNX glymur.dtsi sram@c30f000 "qcom,rpmh-stats" (read-only here: ddr-survey --ddr-stats)'),
    # llcc-* (a16gpu_llcc.py): bank 0 of the LLCC, read for its bank count only (llcc-qcom.c qcom_llcc_probe).
    Block('LLCC0', 0x21800000, 0x100000, 'AO', 'always',
          'LNX glymur.dtsi system-cache-controller@21800000 reg "llcc0_base" 0x21800000/0x100000 (read-only here)'),
)


def block_of(addr):
    for block in BLOCKS:
        if block.base <= addr < block.base + block.size:
            return block
    return None


def _gcc(name, off, access, evidence, notes='', halt='', gate='always'):
    return Reg(name, 0x00100000 + off, 'GCC', 'AO', gate, access, evidence, notes, halt=halt)


def _gpucc(name, off, access, evidence, notes='', halt='', gate='gpu_cfg_ahb', domain='CX'):
    return Reg(name, 0x03D90000 + off, 'GPUCC', domain, gate, access, evidence, notes, halt=halt)


def _gicd(name, off, evidence, count=1, stride=4):
    return Reg(name, 0x17000000 + off, 'GICD', 'AO', 'always', 'r', evidence, count=count, stride=stride)


GIC_EVIDENCE = 'A16 GICv3 distributor at 0x17000000 (MADT, docs/A16-Q1N1-PROXY.md); Arm GICv3 GICD layout'

_REGS = (
    # --- GIC distributor (AO; q1n1 already programs it for the EL2 tick) -----------
    _gicd('GICD_CTLR', 0x0000, GIC_EVIDENCE),
    _gicd('GICD_TYPER', 0x0004, GIC_EVIDENCE),
    _gicd('GICD_IGROUPR', 0x0080, GIC_EVIDENCE, count=32),
    _gicd('GICD_ISENABLER', 0x0100, GIC_EVIDENCE, count=32),
    _gicd('GICD_ISPENDR', 0x0200, GIC_EVIDENCE, count=32),
    _gicd('GICD_ICFGR', 0x0C00, GIC_EVIDENCE, count=64),
    _gicd('GICD_IROUTER_LO', 0x6000, GIC_EVIDENCE + ' (low half of the 64-bit IROUTER)', count=1020, stride=8),

    # --- GCC: always on; the GPU's AHB interface clock and its GPLL0 feeds ---------
    _gcc('GCC_GPU_BCR', 0x71000, 'r', 'LNX gcc-glymur.c:8390 resets[GCC_GPU_BCR]={0x71000}',
         'bit 0 = block reset asserted. Read only; never written.'),
    _gcc('GCC_GPU_CFG_AHB_CBCR', 0x71004, 'r', 'LNX gcc-glymur.c:8514 critical cbcr (enabled at GCC probe)',
         'CLK_OFF (bit 31) clear proves the GPUCC register interface is clocked.'),
    _gcc('GCC_QMIP_GPU_AHB_CBCR', 0x71008, 'r', 'LNX gcc-glymur.c:5099 halt_reg 0x71008'),
    _gcc('GCC_GPU_GEMNOC_GFX_CBCR', 0x71010, 'rw',
         'LNX gcc-glymur.c:3755 BRANCH_HALT_VOTED, hwcg bit 1; glymur.dtsi gmu clock "memnoc"',
         'Stage 1 enables it (bit 0); CLK_OFF is not checked while HW_CTL (bit 1) is set.', halt='HALT_VOTED'),
    _gcc('GCC_GPU_GPLL0_CLK_SRC_CBCR', 0x71024, 'r',
         'LNX gcc-glymur.c:3770 halt_reg 0x71024, enable = 0x62038 bit 0, hwcg bit 1',
         'Status only; enabled through the vote register.', halt='HALT_VOTED'),
    _gcc('GCC_GPU_GPLL0_DIV_CLK_SRC_CBCR', 0x7102C, 'r',
         'LNX gcc-glymur.c:3790 halt_reg 0x7102c, enable = 0x62038 bit 1, hwcg bit 1',
         'Status only; enabled through the vote register.', halt='HALT_VOTED'),
    _gcc('GCC_APCS_CLOCK_BRANCH_ENA_VOTE', 0x62038, 'rw',
         'LNX gcc-glymur.c:3776/3796 enable_reg 0x62038 bits 0 (GPLL0) and 1 (GPLL0 div) for the GPU',
         'Shared vote register: Stage 1 only ever ORs in bits 0-1.'),

    # --- GPUCC: CX rail, reachable once GCC_GPU_CFG_AHB runs ------------------------
    _gpucc('GPU_CC_PLL0_MODE', 0x0000, 'rw', 'LNX gpucc-glymur.c:55 gpu_cc_pll0 offset 0x0 (Taycan EKO_T)',
           'Stage 1 only reads it (GMU on XO); Stage 3 configures and enables PLL0 (gmu-boot B2).'),
    _gpucc('GPU_CC_RSCC_XO_AON_CBCR', 0x9004, 'rw', 'LNX gpucc-glymur.c:565 critical cbcr',
           'Critical: Linux sets CLK_ENABLE at probe without a halt check.', halt='CRITICAL'),
    _gpucc('GPU_CC_CXO_AON_CBCR', 0x9008, 'rw', 'LNX gpucc-glymur.c:564 critical cbcr', halt='CRITICAL'),
    _gpucc('GPU_CC_CX_GDSCR', 0x9080, 'rw',
           'LNX gpucc-glymur.c:503 gpu_cc_cx_gdsc .gdscr=0x9080 flags POLL_CFG_GDSCR|RETAIN_FF_ENABLE, '
           'waits en_rest=2 en_few=2 clk_dis=0xf; gdsc.c bits SW_COLLAPSE 0, HW_CONTROL 1, SW_OVERRIDE 2, '
           'RETAIN_FF 11, PWR_ON 31'),
    _gpucc('GPU_CC_CX_CFG_GDSCR', 0x9084, 'r',
           'REVIEW + LNX gdsc.c:67 POLL_CFG_GDSCR reads gdscr+0x4: POWER_UP_COMPLETE bit 16, '
           'POWER_DOWN_COMPLETE bit 15; STATUS_POLL_TIMEOUT_US 2000',
           'The CX GDSC completion bit. Not GDSCR bit 31.'),
    _gpucc('GPU_CC_CX_GDS_HW_CTRL', 0x9094, 'r', 'LNX gpucc-glymur.c:505 .gds_hw_ctrl=0x9094'),
    _gpucc('GPU_CC_AHB_CBCR', 0x90BC, 'rw', 'LNX gpucc-glymur.c:207 BRANCH_HALT_DELAY; glymur.dtsi gmu clock "ahb"',
           'REVIEW: HALT_DELAY, so CLK_OFF is not waited on; enable, then udelay(10).', halt='HALT_DELAY'),
    _gpucc('GPU_CC_SLEEP_CBCR', 0x90CC, 'r', 'LNX gpucc-glymur.c:491 BRANCH_HALT', halt='HALT'),
    _gpucc('GPU_CC_CX_GMU_CBCR', 0x90D4, 'rw', 'LNX gpucc-glymur.c:256 BRANCH_HALT_VOTED; glymur.dtsi gmu clock "gmu"',
           halt='HALT_VOTED'),
    _gpucc('GPU_CC_CXO_CBCR', 0x90E4, 'rw', 'LNX gpucc-glymur.c:274 BRANCH_HALT; glymur.dtsi gmu clock "cxo"',
           halt='HALT'),
    _gpucc('GPU_CC_HUB_CX_INT_CBCR', 0x90E8, 'rw',
           'LNX gpucc-glymur.c:442 BRANCH_HALT_VOTED; glymur.dtsi gmu clock "hub"', halt='HALT_VOTED'),
    _gpucc('GPU_CC_MEMNOC_GFX_CBCR', 0x90F0, 'r', 'LNX gpucc-glymur.c:460 BRANCH_HALT_VOTED (no DT consumer)',
           halt='HALT_VOTED'),
    _gpucc('GPU_CC_GMU_CMD_RCGR', 0x9318, 'rw',
           'LNX gpucc-glymur.c:155 gpu_cc_gmu_clk_src cmd_rcgr, clk_rcg2_shared_ops; clk-rcg2.c CMD bits '
           'UPDATE 0, ROOT_EN 1, ROOT_OFF 31'),
    _gpucc('GPU_CC_GMU_CFG_RCGR', 0x931C, 'rw',
           'LNX clk-rcg2.c CFG = cmd_rcgr + 4: SRC_DIV[4:0], SRC_SEL[10:8]; gpucc-glymur.c parent_map_1 '
           'BI_TCXO = 0; ftbl 19.2 MHz = XO /1',
           'Stage 1 parks it on XO (CFG = 0), as clk_rcg2_shared_disable does.'),
    _gpucc('GPU_CC_CB_CBCR', 0x93A4, 'rw', 'LNX gpucc-glymur.c:563 critical cbcr', halt='CRITICAL'),
    _gpucc('GPU_CC_RSCC_HUB_AON_CBCR', 0x93E8, 'rw',
           'LNX gpucc-glymur.c:473 BRANCH_HALT; glymur.dtsi gmu clock "rscc"', halt='HALT'),
    _gpucc('GPU_CC_HUB_AON_CBCR', 0x93EC, 'r', 'LNX gpucc-glymur.c:424 BRANCH_HALT_VOTED (no DT consumer)',
           halt='HALT_VOTED'),
    _gpucc('GPU_CC_HUB_CMD_RCGR', 0x93F0, 'rw', 'LNX gpucc-glymur.c:177 gpu_cc_hub_clk_src (GPLL0 200/300/400 MHz), '
           'clk_rcg2_shared_ops',
           'REVIEW-2: parked on XO before the CX GDSC, as clk_rcg2_shared_init does at registration. '
           'No frequency-table value is written.'),
    _gpucc('GPU_CC_HUB_CFG_RCGR', 0x93F4, 'rw', 'LNX clk-rcg2.c CFG = cmd_rcgr + 4; parent_map_1 BI_TCXO = 0'),
    _gpucc('GPU_CC_FF_CMD_RCGR', 0x9474, 'rw', 'LNX gpucc-glymur.c:131 gpu_cc_ff_clk_src, clk_rcg2_shared_ops',
           'REVIEW-2: parked on XO before the CX GDSC, as clk_rcg2_shared_init does at registration.'),
    _gpucc('GPU_CC_FF_CFG_RCGR', 0x9478, 'rw', 'LNX clk-rcg2.c CFG = cmd_rcgr + 4; parent_map_0 BI_TCXO = 0'),
    _gpucc('GPU_CC_GPU_SMMU_VOTE_CBCR', 0x7000, 'rw',
           'LNX gpucc-glymur.c:326 BRANCH_HALT_VOTED; glymur.dtsi adreno_smmu clock "hlos"', halt='HALT_VOTED'),
    # Resets: listed so the tool can refuse them by name.
    _gpucc('GPU_CC_XO_BCR', 0x9000, '', 'LNX gpucc-glymur.c:555', 'Reset. Never touched.', gate='never'),
    _gpucc('GPU_CC_CX_BCR', 0x907C, '', 'LNX gpucc-glymur.c:550', 'Reset. Never touched.', gate='never'),
    _gpucc('GPU_CC_GMU_BCR', 0x9314, '', 'LNX gpucc-glymur.c:553', 'Reset. Never touched.', gate='never'),
    _gpucc('GPU_CC_CB_BCR', 0x93A0, '', 'LNX gpucc-glymur.c:549', 'Reset. Never touched.', gate='never'),
    _gpucc('GPU_CC_FAST_HUB_BCR', 0x93E4, '', 'LNX gpucc-glymur.c:551', 'Reset. Never touched.', gate='never'),
    _gpucc('GPU_CC_FF_BCR', 0x9470, '', 'LNX gpucc-glymur.c:552', 'Reset. Never touched.', gate='never'),
    # GX-side branches and resets that live in the GPUCC register file: they
    # clock or reset GX, so they are GX for this tool and refused.
    _gpucc('GPU_CC_GX_BCR', 0x905C, '', 'LNX gpucc-glymur.c:554', 'GX reset. Refused.', gate='never', domain='GX'),
    _gpucc('GPU_CC_GX_GMU_CBCR', 0x9060, '', 'LNX gpucc-glymur.c:388',
           'Clocks GX; register sits in GPUCC. Refused until the GMU owns GX.', gate='gx', domain='GX', halt='HALT'),
    _gpucc('GPU_CC_GX_GDSCR_XML', 0x100C, '',
           'XML a6xx.xml GPU_CC_GX_GDSCR 0x24403 (abs 0x03d9100c) -- a6xx-era address',
           'Not glymur\'s GX GDSC (that is gxclkctl 0x03d68024). Refused.', gate='never', domain='GX'),

    # --- CX_MISC (cx_mem) -------------------------------------------------------------
    Reg('CX_MISC_TCM_RET_CNTL', 0x03D9E0E4, 'CX_MISC', 'CX', 'cx_misc', 'r',
        'XML a6xx.xml:5096 0x0039 (A7XX-); LNX a6xx_gmu.c(master):1351 reads it after the GMU clocks are on: '
        '==1 -> GMU_WARM_BOOT', 'REVIEW: must read 0 before a cold GMU boot. a8xx never writes it.'),
    Reg('CX_MISC_SLICE_ENABLE_FINAL', 0x03D9E21C, 'CX_MISC', 'CX', 'cx_misc', 'r',
        'XML a6xx.xml:5097 0x0087 (A8XX); LNX a8xx_gpu.c:107 slice_mask &= read, chip_id |= hweight << 4',
        'Linux reads it after qcom_scm_gpu_init_regs (a6xx_gmu.c(master):1322 CX GDSC, 1330 clocks, 1334 SMC, 1339 slice read); Stage 1 reads it before '
        'that SMC, so a 0 here may only mean TZ has not run it yet (INFERRED).'),
    Reg('CX_MISC_SW_FUSE_VALUE', 0x03D9F000, 'CX_MISC', 'CX', 'cx_misc', 'r',
        'XML a6xx.xml:5098 0x0400; LNX a6xx_gmu.c(master):1223 fallback write RAYTRACING|FASTBLEND|LPAC when SCM is absent',
        'Stage 3 (after SMC 0x42002801). Stage 1 does not read it.'),
    Reg('CX_MISC_SW_FUSE_FREQ_LIMIT_STATUS', 0x03D9F014, 'CX_MISC', 'CX', 'cx_fuse', 'r',
        'XML a6xx.xml:5103 0x0405 FINALFREQLIMIT[8:0] (A8XX-); LNX a6xx_gpu.c:2613 read in a6xx_gpu_init '
        '(ADRENO_QUIRK_SOFTFUSE) before any GPU runtime-PM vote, but after gpucc probe set the critical '
        'CBCRs (gpucc-glymur.c:563-565; probe order forced by gpu iommus -> adreno_smmu -> gpucc)',
        'REVIEW: the first GPU-block read. Safe only by inference: Linux reads it with no GMU clock or '
        'GPU vote of its own, but with CB/CXO_AON/RSCC_XO_AON enabled and a boot-on CX GDSC still held '
        'on by genpd, so the gate requires both (INFERRED). survey skips it unless firmware left them '
        'on and the GDSC is not under HW control; cx-on reads it after P4 (REVIEW-2).'),

    # --- GBIF (CX; REVIEW) -- listed for Stage 3, not accessed in Stage 1 -------------
    Reg('GBIF_CX_CONFIG', 0x03D0F000, 'GBIF', 'CX', 'cx_gmu', 'rw',
        'XML a6xx.xml:1271 0x3c00; LNX a6xx_gmu.c(master):1036 written in fw_start (0x20023000)',
        'REVIEW: CX, may be written before GX. Stage 3.'),
    Reg('GBIF_HALT', 0x03D0F114, 'GBIF', 'CX', 'cx_gmu', 'rw', 'XML a6xx.xml:1278 0x3c45',
        'Stage 3 DMA quiesce.'),

    # --- GMU (CX) -- listed for Stage 3, not accessed in Stage 1 ----------------------
    Reg('GMU_CORE_FW_VERSION', 0x03D74FE0, 'GMU', 'CX', 'cx_gmu', 'r', 'XML a6xx_gmu.xml:55 0x1d3f8'),
    Reg('GMU_PWR_CLK_STATUS', 0x03D7DFA0, 'GMU', 'CX', 'cx_gmu', 'r', 'XML a6xx_gmu.xml:112 0x1f7e8 (A8XX-)',
        'bits 0/1 clear = GX on (Stage 3 gate).'),
    Reg('GMU_CX_FAL_INTF', 0x03D7DFB0, 'GMU', 'CX', 'cx_gmu', 'rw', 'XML a6xx_gmu.xml:136 0x1f7ec (A8XX-)'),
    Reg('GMU_CX_FALNEXT_INTF', 0x03D7DFB4, 'GMU', 'CX', 'cx_gmu', 'rw', 'XML a6xx_gmu.xml:138 0x1f7ed (A8XX-)'),
    Reg('GMU_GMU2HOST_INTR_INFO', 0x03D7E648, 'GMU', 'CX', 'cx_gmu', 'r', 'XML a6xx_gmu.xml:163 0x1f992',
        'OOB ack bit 31 (Stage 3).'),
    Reg('GMU_HOST2GMU_INTR_SET', 0x03D7E650, 'GMU', 'CX', 'cx_gmu', 'rw', 'XML a6xx_gmu.xml:168 0x1f994',
        'OOB GPU_SET bit 30 (Stage 3).'),

    # --- GX: Stage 1 refuses them by name; Stage 3 only once the GMU reports GX on --
    Reg('GX_GDSCR', 0x03D68024, 'GXCLKCTL', 'GX', 'gx', '',
        'LNX gxclkctl-kaanapali.c:22 .gdscr=0x4024, glymur.dtsi gxclkctl@3d64000', 'Owned by the GMU.'),
    Reg('RBBM_STATUS', 0x03D00048, 'GPU', 'GX', 'gx', 'r', 'XML A8XX RBBM_STATUS 0x00012'),
    Reg('RBBM_SECVID_TRUST_CNTL', 0x03D3D000, 'GPU', 'GX', 'gx', 'rw',
        'XML a6xx.xml:774 0xF400; PLAN: written 0 after CP_ME_INIT on the zap-less path (Stage 3)'),
    Reg('CP_SQE_INSTR_BASE', 0x03D02058, 'GPU', 'GX', 'gx', 'rw', 'XML A8XX CP_SQE_INSTR_BASE 0x00816'),

    # --- Adreno SMMU (MMU-500, SMMUv2 programming model), GR0 page ----------------------
    Reg('SMMU_sCR0', 0x03DA0000, 'SMMU', 'CX', 'cx_smmu', 'rw', 'Arm SMMUv2 GR0 sCR0 +0x0; LNX glymur.dtsi iommu@3da0000'),
    Reg('SMMU_IDR0', 0x03DA0020, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 IDR0 +0x20: NUMSMRG[7:0]'),
    Reg('SMMU_IDR1', 0x03DA0024, 'SMMU', 'CX', 'cx_smmu', 'r',
        'Arm SMMUv2 IDR1 +0x24: NUMCB[7:0], NUMS2CB[23:16], NUMPAGENDXB[30:28], PAGESIZE[31]'),
    Reg('SMMU_IDR2', 0x03DA0028, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 IDR2 +0x28'),
    Reg('SMMU_IDR7', 0x03DA003C, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 IDR7 +0x3c (MAJOR/MINOR)'),
    Reg('SMMU_SMR', 0x03DA0800, 'SMMU', 'CX', 'cx_smmu', 'rw', 'Arm SMMUv2 SMRn +0x800 + 4n (n < NUMSMRG)',
        count=128),
    Reg('SMMU_S2CR', 0x03DA0C00, 'SMMU', 'CX', 'cx_smmu', 'rw', 'Arm SMMUv2 S2CRn +0xc00 + 4n (n < NUMSMRG)',
        count=128),
)

# === Stage 3 (GMU SMMU, GMU boot, HFI/OOB, CP) ========================================
# Offsets on a8xx are GPU-base relative dwords (LNX a6xx_gmu.h GMU_BYTE_OFFSET: gmu_read
# subtracts mmio_offset, so every GMU XML offset is (0x03d00000 + 4 * dword)); PROVEN for
# CORE_FW_VERSION = 0x03d74fe0 against the XML and the plan. RSCC is GPU base + 0x50000
# (LNX a6xx_gmu.c a8xx branch), its XML offsets are RSCC-relative dwords.
GPU_BASE = 0x03D00000
XML_GMU = 'XML a6xx_gmu.xml.h REG_'
XML_GPU = 'XML a6xx.xml.h REG_'


def _gmu(name, dword, access, evidence, notes='', gate='cx_gmu', count=1):
    return Reg(name, GPU_BASE + 4 * dword, 'GMU', 'CX', gate, access, evidence, notes, count=count)


def _gx(name, dword, access, evidence, notes='', count=1):
    return Reg(name, GPU_BASE + 4 * dword, 'GPU', 'GX', 'gx', access, evidence, notes, count=count)


def _gbif(name, dword, access, evidence, notes=''):
    return Reg(name, GPU_BASE + 4 * dword, 'GBIF', 'CX', 'cx_gmu', access, evidence, notes)


def _rscc(name, dword, access, evidence, notes=''):
    return Reg(name, 0x03D50000 + 4 * dword, 'RSCC', 'CX', 'cx_gmu', access, evidence, notes)


def _pll(name, off, access, evidence, notes=''):
    return Reg(name, 0x03D90000 + off, 'GPUCC', 'CX', 'gpu_cfg_ahb', access, evidence, notes)


def _cb(name, off, access, evidence, notes=''):
    # IDR1 0x40000019 on the A16 (stage1-cx-on-4.json): PAGESIZE 4 KiB, NUMPAGENDXB 4 -> 32 global
    # pages, so context bank n sits at base + 32 * 4 KiB + n * 4 KiB (Arm SMMUv2 CB base).
    return Reg(name, SMMU_CB_BASE + off, 'SMMU', 'CX', 'cx_smmu', access, evidence, notes, count=SMMU_NUM_CB,
               stride=0x1000)


SMMU_BASE = 0x03DA0000
SMMU_CB_BASE = SMMU_BASE + 32 * 0x1000     # A16 IDR1: NUMPAGENDXB=4 (32 pages), PAGESIZE=0 (PROVEN)
SMMU_NUM_CB = 25                           # A16 IDR1 NUMCB (PROVEN)
RSC_DRV2 = 0x18920000                      # LNX glymur.dtsi apps_rsc reg "drv-2"
RSC_TCS = RSC_DRV2 + 0xD00                 # qcom,tcs-offset
RSC_NUM_TCS = 8                            # tcs-config ACTIVE 2 + SLEEP 3 + WAKE 3 (CONTROL 0)
LNX_RSC = 'LNX rpmh-rsc.c rpmh_rsc_reg_offset_ver_'


def _tcs(name, v3_off, v2_off, notes=''):
    """One apps-RSC TCS register for both register layouts (RSC_DRV_ID major >= 3 picks _V3)."""
    return (Reg(name + '_V3', RSC_TCS + v3_off, 'APPS_RSC', 'AO', 'always', 'rw', LNX_RSC + '3_0', notes,
                count=RSC_NUM_TCS, stride=672),
            Reg(name + '_V2', RSC_TCS + v2_off, 'APPS_RSC', 'AO', 'always', 'rw', LNX_RSC + '2_7', notes,
                count=RSC_NUM_TCS, stride=672))


def _tcs_cmd(name, v3_off, v2_off):
    return (Reg(name + '_V3', RSC_TCS + v3_off, 'APPS_RSC', 'AO', 'always', 'rw', LNX_RSC + '3_0 (cmd stride 24)',
                count=RSC_NUM_TCS * 16, stride=672, inner=16, inner_stride=24),
            Reg(name + '_V2', RSC_TCS + v2_off, 'APPS_RSC', 'AO', 'always', 'rw', LNX_RSC + '2_7 (cmd stride 20)',
                count=RSC_NUM_TCS * 16, stride=672, inner=16, inner_stride=20))


_STAGE3_REGS = (
    # --- GPUCC PLL0 (Taycan EKO_T = Taycan ELU = Lucid EVO ops) -------------------------------
    _pll('GPU_CC_PLL0_OPMODE', 0x04, 'rw', 'LNX clk-alpha-pll.c TAYCAN_ELU PLL_OFF_OPMODE 0x04', 'STANDBY 0, RUN 1'),
    _pll('GPU_CC_PLL0_STATE', 0x08, 'r', 'LNX clk-alpha-pll.c TAYCAN_ELU PLL_OFF_STATE 0x08'),
    _pll('GPU_CC_PLL0_STATUS', 0x0C, 'r', 'LNX clk-alpha-pll.c TAYCAN_ELU PLL_OFF_STATUS 0x0c'),
    _pll('GPU_CC_PLL0_L_VAL', 0x10, 'rw', 'LNX clk-alpha-pll.c TAYCAN_ELU PLL_OFF_L_VAL 0x10; '
         'gpucc-glymur.c:44 l=0x3b, clk_lucid_evo_pll_configure ORs TRION_PLL_CAL_VAL 0x44 << 16'),
    _pll('GPU_CC_PLL0_ALPHA_VAL', 0x14, 'rw', 'LNX TAYCAN_ELU PLL_OFF_ALPHA_VAL 0x14; gpucc-glymur.c:45 alpha=0xe555'),
    _pll('GPU_CC_PLL0_USER_CTL', 0x18, 'rw', 'LNX TAYCAN_ELU PLL_OFF_USER_CTL 0x18; gpucc-glymur.c:49 0x408 '
         '(post_div_shift 10: even /2)', 'bits[2:0] outputs, bit 25 ENABLE_VOTE_RUN (FSM mode)'),
    _pll('GPU_CC_PLL0_USER_CTL_U', 0x1C, 'rw', 'LNX TAYCAN_ELU PLL_OFF_USER_CTL_U 0x1c; gpucc-glymur.c:50 0x2'),
    _pll('GPU_CC_PLL0_CONFIG_CTL', 0x20, 'rw', 'LNX TAYCAN_ELU 0x20; gpucc-glymur.c:46 0x25c400e7'),
    _pll('GPU_CC_PLL0_CONFIG_CTL_U', 0x24, 'rw', 'LNX TAYCAN_ELU 0x24; gpucc-glymur.c:47 0x0a8060e0'),
    _pll('GPU_CC_PLL0_CONFIG_CTL_U1', 0x28, 'rw', 'LNX TAYCAN_ELU 0x28; gpucc-glymur.c:48 0xf51dea20'),

    # --- GMU CM3 core, TCMs and boot handshake (CX) ----------------------------------------------
    _gmu('GMU_CM3_ITCM', 0x1B400, 'rw', XML_GMU + 'A6XX_GMU_CM3_ITCM_START 0x1b400; LNX a6xx_gmu_fw_load '
         'itcm_base 0x0, SZ_16K, gmu_write_bulk', 'Written only while the CM3 is held in SYSRESET.',
         gate='gmu_reset', count=0x1000),
    _gmu('GMU_CM3_DTCM', 0x1C400, 'rw', XML_GMU + 'A6XX_GMU_CM3_DTCM_START 0x1c400; LNX a6xx_gmu_fw_load '
         'dtcm_base 0x10004000 (a8xx), SZ_16K', 'Written only while the CM3 is held in SYSRESET. Entry 0xff8 '
         'is GMU_CORE_FW_VERSION.', gate='gmu_reset', count=0x1000),
    _gmu('GMU_ICACHE_CONFIG', 0x1F400, 'rw', XML_GMU + 'A6XX_GMU_ICACHE_CONFIG; LNX a6xx_gmu_power_config = 1'),
    _gmu('GMU_DCACHE_CONFIG', 0x1F401, 'rw', XML_GMU + 'A6XX_GMU_DCACHE_CONFIG; LNX a6xx_gmu_power_config = 1'),
    _gmu('GMU_SYS_BUS_CONFIG', 0x1F40F, 'rw', XML_GMU + 'A6XX_GMU_SYS_BUS_CONFIG; LNX a6xx_gmu_power_config = 1'),
    _gmu('GMU_MRC_GBIF_QOS_CTRL', 0x1F50B, 'rw', XML_GMU + 'A6XX_GMU_MRC_GBIF_QOS_CTRL; LNX a6xx_gmu_fw_start '
         'a8xx = 0x33'),
    _gmu('GMU_GMU_PWR_COL_KEEPALIVE', 0x1F7E4, 'rw', XML_GMU + 'A8XX_GMU_GMU_PWR_COL_KEEPALIVE; LNX force_off = 0'),
    _gmu('GMU_RPMH_POWER_STATE', 0x1F7E9, 'r', XML_GMU + 'A8XX_GPU_GMU_CX_GMU_RPMH_POWER_STATE; LNX '
         'a6xx_gmu_check_idle_level (ACTIVE = 0)'),
    _gmu('GMU_CM3_SYSRESET', 0x1F800, 'rw', XML_GMU + 'A6XX_GMU_CM3_SYSRESET; LNX a6xx_gmu_start 1 then 0'),
    _gmu('GMU_CM3_BOOT_CONFIG', 0x1F801, 'rw', XML_GMU + 'A6XX_GMU_CM3_BOOT_CONFIG; LNX fw_start = 0x02'),
    _gmu('GMU_CM3_FW_INIT_RESULT', 0x1F81C, 'rw', XML_GMU + 'A6XX_GMU_CM3_FW_INIT_RESULT; LNX fw_start = 0, '
         'a6xx_gmu_start polls (val & 0x1ff) == 0x100 for version > 0x20010004, 100 ms'),
    _gmu('GMU_CM3_CFG', 0x1F82D, 'rw', XML_GMU + 'A6XX_GMU_CM3_CFG; LNX fw_start = 0x4052'),
    _gmu('GMU_POWER_COUNTER_ENABLE', 0x1FC10, 'rw', XML_GMU + 'A8XX_GMU_CX_GMU_POWER_COUNTER_ENABLE; LNX hw_init = 1'),
    _gmu('GMU_POWER_COUNTER_SELECT_XOCLK_0', 0x1FC40, 'rw', XML_GMU + 'A8XX_..._SELECT_XOCLK_0; LNX hw_init rmw BIT(5)'),
    _gmu('GMU_POWER_COUNTER_SELECT_XOCLK_1', 0x1FC41, 'rw', XML_GMU + 'A8XX_..._SELECT_XOCLK_1; LNX hw_init = 4'),
    _gmu('GMU_HFI_CTRL_STATUS', 0x1F980, 'r', XML_GMU + 'A6XX_GMU_HFI_CTRL_STATUS; LNX a6xx_gmu_hfi_start polls bit 0'),
    _gmu('GMU_HFI_QTBL_INFO', 0x1F984, 'rw', XML_GMU + 'A6XX_GMU_HFI_QTBL_INFO; LNX fw_start = 1'),
    _gmu('GMU_HFI_QTBL_ADDR', 0x1F985, 'rw', XML_GMU + 'A6XX_GMU_HFI_QTBL_ADDR; LNX fw_start = hfi iova'),
    _gmu('GMU_HFI_CTRL_INIT', 0x1F986, 'rw', XML_GMU + 'A6XX_GMU_HFI_CTRL_INIT; LNX a6xx_gmu_hfi_start = 1'),
    _gmu('GMU_GMU2HOST_INTR_CLR', 0x1F991, 'rw', XML_GMU + 'A6XX_GMU_GMU2HOST_INTR_CLR'),
    _gmu('GMU_GMU2HOST_INTR_MASK', 0x1F993, 'rw', XML_GMU + 'A6XX_GMU_GMU2HOST_INTR_MASK; LNX resume ~CM3_FAULT'),
    _gmu('GMU_GENERAL_8', 0x1F9C8, 'rw', XML_GMU + 'A8XX_GMU_GENERAL_8; LNX fw_start log iova[31:12] | (4K pages - 1)'),
    _gmu('GMU_GENERAL_9', 0x1F9C9, 'rw', XML_GMU + 'A8XX_GMU_GENERAL_9; LNX a6xx_gmu_start log wptr = 0'),
    _gmu('GMU_GENERAL_10', 0x1F9CA, 'rw', XML_GMU + 'A8XX_GMU_GENERAL_10; LNX fw_start gmu_chipid 0x8010100'),
    _gmu('GMU_AO_HOST_INTERRUPT_CLR', 0x23B04, 'rw', XML_GMU + 'A6XX_GMU_AO_HOST_INTERRUPT_CLR'),
    _gmu('GMU_AO_HOST_INTERRUPT_STATUS', 0x23B05, 'r', XML_GMU + 'A6XX_GMU_AO_HOST_INTERRUPT_STATUS: WDOG_BITE 0, '
         'FENCE_ERR 3, HOST_AHB_BUS_ERROR 5'),
    _gmu('GMU_AO_HOST_INTERRUPT_MASK', 0x23B06, 'rw', XML_GMU + 'A6XX_GMU_AO_HOST_INTERRUPT_MASK; LNX resume '
         '~(WDOG|AHB_ERR|FENCE_ERR)'),
    _gmu('GMU_RSCC_CONTROL_REQ', 0x23B07, 'rw', XML_GMU + 'A6XX_GMU_RSCC_CONTROL_REQ; LNX a6xx_rpmh_stop = 1 then 0'),
    _gmu('GMU_RSCC_CONTROL_ACK', 0x23B08, 'r', XML_GMU + 'A6XX_GMU_RSCC_CONTROL_ACK'),
    _gmu('GMU_AO_GPU_CX_BUSY_STATUS', 0x23B0C, 'r', XML_GMU + 'A6XX_GPU_GMU_AO_GPU_CX_BUSY_STATUS: '
         'GPUBUSYIGNAHB bit 23'),
    _gmu('GMU_AO_GPU_CX_BUSY_MASK', 0x23B0E, 'rw', XML_GMU + 'A6XX_GPU_GMU_AO_GPU_CX_BUSY_MASK; LNX hw_init '
         '0xff000000'),
    _gmu('GMU_AO_AHB_FENCE_CTRL', 0x23B10, 'rw', XML_GMU + 'A6XX_GMU_AO_AHB_FENCE_CTRL; LNX slumber/force_off = 0'),
    _gmu('GMU_AHB_FENCE_RANGE_0', 0x23B11, 'rw', XML_GMU + 'A6XX_GMU_AHB_FENCE_RANGE_0; LNX fw_start a8xx '
         'BIT(31) | upper 0x32 << 18 | lower 0x8c0'),
    _gmu('GMU_AHB_FENCE_STATUS', 0x23B13, 'r', XML_GMU + 'A6XX_GMU_AHB_FENCE_STATUS; LNX fenced_write polls '
         'WRITEDROPPED'),
    _gmu('GMU_AHB_FENCE_STATUS_CLR', 0x23B14, 'rw', XML_GMU + 'A6XX_GMU_AHB_FENCE_STATUS_CLR; LNX force_off = 7'),

    # --- RSCC (GPU base + 0x50000 on a8xx) -----------------------------------------------------------
    _rscc('RSCC_RSC_STATUS0_DRV0', 0x004, 'r', XML_GMU + 'A6XX_GPU_RSCC_RSC_STATUS0_DRV0; LNX a6xx_rpmh_stop '
          'polls BIT(16) (BIT(30) only on a840)'),
    _rscc('RSCC_SEQ_BUSY_DRV0', 0x101, 'r', XML_GMU + 'A6XX_RSCC_SEQ_BUSY_DRV0; LNX a6xx_rpmh_start'),
    Reg('RSCC_TCS_DRV0_STATUS', 0x03D50000 + 4 * (0x346 + 4), 'RSCC', 'CX', 'cx_gmu', 'r',
        XML_GMU + 'A6XX_RSCC_TCS0_DRV0_STATUS 0x346 + seqmem_off 4 (a8xx); TCS0..9 at +0xa8 dwords each '
        '(0x346, 0x3ee, 0x496, ...); LNX a6xx_gmu_rpmh_off polls bit 0', count=10, stride=4 * 0xA8),

    # --- GBIF (CX) ----------------------------------------------------------------------------------------
    _gbif('GBIF_QSB_SIDE0', 0x3C03, 'rw', XML_GPU + 'A6XX_GBIF_QSB_SIDE0; LNX a6xx_catalog.c a840_gbif 0x00071e20 '
          '(the X2-90 entry uses a840_gbif)'),
    _gbif('GBIF_QSB_SIDE1', 0x3C04, 'rw', XML_GPU + 'A6XX_GBIF_QSB_SIDE1; a840_gbif 0x00071e20'),
    _gbif('GBIF_QSB_SIDE2', 0x3C05, 'rw', XML_GPU + 'A6XX_GBIF_QSB_SIDE2; a840_gbif 0x00071e20'),
    _gbif('GBIF_QSB_SIDE3', 0x3C06, 'rw', XML_GPU + 'A6XX_GBIF_QSB_SIDE3; a840_gbif 0x00071e20'),
    _gbif('GBIF_HALT_ACK', 0x3C46, 'r', XML_GPU + 'A6XX_GBIF_HALT_ACK; LNX a8xx_bus_clear_pending_transactions'),

    # --- Adreno SMMU: global space, context-bank attributes (GR1), context banks -----------------------
    Reg('SMMU_sGFSR', SMMU_BASE + 0x48, 'SMMU', 'CX', 'cx_smmu', 'rw', 'LNX arm-smmu.h ARM_SMMU_GR0_sGFSR 0x48'),
    Reg('SMMU_sGFSYNR0', SMMU_BASE + 0x50, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 GR0 sGFSYNR0 +0x50'),
    Reg('SMMU_CBAR', SMMU_BASE + 0x1000, 'SMMU', 'CX', 'cx_smmu', 'rw', 'LNX arm-smmu.h ARM_SMMU_GR1_CBAR(n) '
        '0x0 + 4n in GR1 (base + 1 page)', count=SMMU_NUM_CB),
    Reg('SMMU_CBFRSYNRA', SMMU_BASE + 0x1400, 'SMMU', 'CX', 'cx_smmu', 'r', 'LNX ARM_SMMU_GR1_CBFRSYNRA(n) 0x400',
        count=SMMU_NUM_CB),
    Reg('SMMU_CBA2R', SMMU_BASE + 0x1800, 'SMMU', 'CX', 'cx_smmu', 'rw', 'LNX ARM_SMMU_GR1_CBA2R(n) 0x800: VA64',
        count=SMMU_NUM_CB),
    _cb('SMMU_CB_SCTLR', 0x000, 'rw', 'LNX ARM_SMMU_CB_SCTLR 0x0; arm_smmu_write_context_bank + '
        'qcom_adreno_smmu_write_sctlr (HUPCF)'),
    _cb('SMMU_CB_ACTLR', 0x004, 'rw', 'LNX ARM_SMMU_CB_ACTLR 0x4; arm-smmu-qcom.c client_match '
        'qcom,adreno / qcom,adreno-gmu = PREFETCH_DEEP|CPRE|CMTLB'),
    _cb('SMMU_CB_TCR2', 0x010, 'rw', 'LNX ARM_SMMU_CB_TCR2 0x10: PASIZE, SEP_UPSTREAM, AS'),
    _cb('SMMU_CB_TTBR0', 0x020, 'rw', 'LNX ARM_SMMU_CB_TTBR0 0x20 (64-bit, writeq)'),
    _cb('SMMU_CB_TTBR0_HI', 0x024, 'rw', 'LNX ARM_SMMU_CB_TTBR0 upper word (ASID[63:48])'),
    _cb('SMMU_CB_TTBR1', 0x028, 'rw', 'LNX ARM_SMMU_CB_TTBR1 0x28'),
    _cb('SMMU_CB_TTBR1_HI', 0x02C, 'rw', 'LNX ARM_SMMU_CB_TTBR1 upper word'),
    _cb('SMMU_CB_TCR', 0x030, 'rw', 'LNX ARM_SMMU_CB_TCR 0x30 (AArch64 TCR, T0SZ/IRGN/ORGN/SH/TG0/EPD1)'),
    _cb('SMMU_CB_MAIR0', 0x038, 'rw', 'LNX ARM_SMMU_CB_S1_MAIR0 0x38; io-pgtable-arm.c MAIR NC/WB/DEV/INC'),
    _cb('SMMU_CB_MAIR1', 0x03C, 'rw', 'LNX ARM_SMMU_CB_S1_MAIR1 0x3c'),
    _cb('SMMU_CB_FSR', 0x058, 'rw', 'LNX ARM_SMMU_CB_FSR 0x58 (write-1-to-clear)'),
    _cb('SMMU_CB_FAR', 0x060, 'r', 'LNX ARM_SMMU_CB_FAR 0x60 (low word)'),
    _cb('SMMU_CB_FAR_HI', 0x064, 'r', 'LNX ARM_SMMU_CB_FAR 0x60 (high word)'),
    _cb('SMMU_CB_FSYNR0', 0x068, 'r', 'LNX ARM_SMMU_CB_FSYNR0 0x68'),
    _cb('SMMU_CB_TLBIASID', 0x610, 'rw', 'LNX ARM_SMMU_CB_S1_TLBIASID 0x610 (arm_smmu_tlb_inv_context_s1)'),
    _cb('SMMU_CB_TLBSYNC', 0x7F0, 'rw', 'LNX ARM_SMMU_CB_TLBSYNC 0x7f0'),
    _cb('SMMU_CB_TLBSTATUS', 0x7F4, 'r', 'LNX ARM_SMMU_CB_TLBSTATUS 0x7f4: SACTIVE bit 0'),

    # --- apps RSC drv-2: host RPMh votes (MX/CX levels, optional DDR BCMs) -----------------------------
    Reg('RSC_DRV_ID', RSC_DRV2 + 0x0, 'APPS_RSC', 'AO', 'always', 'r', 'LNX rpmh-rsc.c RSC_DRV_ID 0: major [23:16]'),
    Reg('RSC_DRV_PRNT_CHLD_CONFIG', RSC_DRV2 + 0xC, 'APPS_RSC', 'AO', 'always', 'r',
        'LNX rpmh-rsc.c DRV_PRNT_CHLD_CONFIG 0x0c: NUM_TCS per drv (6 bits each), NCPT [31:27]'),
    Reg('RSC_IRQ_ENABLE', RSC_TCS + 0x0, 'APPS_RSC', 'AO', 'always', 'rw', 'LNX RSC_DRV_IRQ_ENABLE 0x00 (both layouts)'),
    Reg('RSC_IRQ_STATUS', RSC_TCS + 0x4, 'APPS_RSC', 'AO', 'always', 'r', 'LNX RSC_DRV_IRQ_STATUS 0x04'),
    Reg('RSC_IRQ_CLEAR', RSC_TCS + 0x8, 'APPS_RSC', 'AO', 'always', 'rw', 'LNX RSC_DRV_IRQ_CLEAR 0x08'),
) + _tcs('RSC_TCS_WAIT_FOR_CMPL', 0x20, 0x10) + _tcs('RSC_TCS_CONTROL', 0x24, 0x14, 'AMC_MODE_ENABLE 16, TRIGGER 24') \
  + _tcs('RSC_TCS_STATUS', 0x28, 0x18, 'non-zero = idle (tcs_is_free)') + _tcs('RSC_TCS_CMD_ENABLE', 0x2C, 0x1C) \
  + _tcs_cmd('RSC_TCS_CMD_MSGID', 0x34, 0x30) + _tcs_cmd('RSC_TCS_CMD_ADDR', 0x38, 0x34) \
  + _tcs_cmd('RSC_TCS_CMD_DATA', 0x3C, 0x38) + _tcs_cmd('RSC_TCS_CMD_STATUS', 0x40, 0x3C) + (
    # --- GX (GPU core): Stage 3 touches these only once the GMU reports GX on (gate gx) --------------
    _gx('RBBM_GBIF_HALT', 0x00A, 'rw', XML_GPU + 'A8XX_RBBM_GBIF_HALT; LNX hw_init = 0, bus halt gx side'),
    _gx('RBBM_GBIF_HALT_ACK', 0x00B, 'r', XML_GPU + 'A8XX_RBBM_GBIF_HALT_ACK'),
    _gx('RBBM_GBIF_CLIENT_QOS_CNTL', 0x008, 'rw', XML_GPU + 'A8XX_RBBM_GBIF_CLIENT_QOS_CNTL; x285_nonctxt 0x22122212'),
    _gx('RBBM_WAIT_IDLE_CLOCKS_CNTL', 0x010, 'rw', XML_GPU + 'A8XX_RBBM_WAIT_IDLE_CLOCKS_CNTL; x285_nonctxt 0x30'),
    _gx('RBBM_WAIT_IDLE_CLOCKS_CNTL2', 0x011, 'rw', XML_GPU + 'A8XX_RBBM_WAIT_IDLE_CLOCKS_CNTL2; x285_nonctxt 0x30'),
    _gx('RBBM_STATUS1', 0x013, 'r', XML_GPU + 'A8XX_RBBM_STATUS1'),
    _gx('RBBM_INTERFACE_HANG_INT_CNTL', 0x02F, 'rw', XML_GPU + 'A8XX_RBBM_INTERFACE_HANG_INT_CNTL'),
    _gx('RBBM_INT_0_MASK', 0x062, 'rw', XML_GPU + 'A8XX_RBBM_INT_0_MASK; LNX a8xx_gpu.c A8XX_INT_MASK'),
    _gx('RBBM_INT_0_STATUS', 0x06A, 'r', XML_GPU + 'A8XX_RBBM_INT_0_STATUS: HANG_DETECT bit 23'),
    _gx('RBBM_CGC_P2S_CNTL', 0x09D, 'rw', XML_GPU + 'A8XX_RBBM_CGC_P2S_CNTL; x285_nonctxt 0x40'),
    _gx('RBBM_PERFCTR_GPU_BUSY_MASKED', 0x19E, 'rw', XML_GPU + 'A8XX_RBBM_PERFCTR_GPU_BUSY_MASKED'),
    _gx('RBBM_NC_MODE_CNTL', 0x440, 'rw', XML_GPU + 'A8XX_RBBM_NC_MODE_CNTL; x285_nonctxt 1'),
    _gx('RBBM_PERFCTR_CNTL', 0x460, 'rw', XML_GPU + 'A8XX_RBBM_PERFCTR_CNTL'),
    _gx('RBBM_SLICE_PERFCTR_CNTL', 0x500, 'rw', XML_GPU + 'A8XX_RBBM_SLICE_PERFCTR_CNTL'),
    _gx('RBBM_SLICE_INTERFACE_HANG_INT_CNTL', 0x58F, 'rw', XML_GPU + 'A8XX_RBBM_SLICE_INTERFACE_HANG_INT_CNTL'),
    _gx('RBBM_SLICE_NC_MODE_CNTL', 0x5EC, 'rw', XML_GPU + 'A8XX_RBBM_SLICE_NC_MODE_CNTL; x285_nonctxt 1'),
    _gx('CP_RB_BASE', 0x800, 'rw', XML_GPU + 'A6XX_CP_RB_BASE (64-bit)'),
    _gx('CP_RB_BASE_HI', 0x801, 'rw', XML_GPU + 'A6XX_CP_RB_BASE upper word'),
    _gx('CP_RB_CNTL', 0x802, 'rw', XML_GPU + 'A6XX_CP_RB_CNTL; LNX MSM_GPU_RB_CNTL_DEFAULT BUFSZ 12 | BLKSZ 2 << 8'),
    _gx('CP_RB_RPTR_ADDR', 0x804, 'rw', XML_GPU + 'A6XX_CP_RB_RPTR_ADDR (64-bit): rptr shadow'),
    _gx('CP_RB_RPTR_ADDR_HI', 0x805, 'rw', XML_GPU + 'A6XX_CP_RB_RPTR_ADDR upper word'),
    _gx('CP_RB_RPTR', 0x806, 'r', XML_GPU + 'A6XX_CP_RB_RPTR'),
    _gx('CP_RB_WPTR', 0x807, 'rw', XML_GPU + 'A6XX_CP_RB_WPTR; LNX a8xx_flush a6xx_fenced_write mask BIT(0)'),
    _gx('CP_RB_RPTR_ADDR_BV', 0x808, 'rw', XML_GPU + 'A8XX_CP_RB_RPTR_ADDR_BV (64-bit)'),
    _gx('CP_RB_RPTR_ADDR_BV_HI', 0x809, 'rw', XML_GPU + 'A8XX_CP_RB_RPTR_ADDR_BV upper word'),
    _gx('CP_SMMU_STREAM_ID_LPAC', 0x814, 'rw', XML_GPU + 'A8XX_CP_SMMU_STREAM_ID_LPAC; x285_nonctxt 0x101'),
    _gx('CP_SQE_CNTL', 0x815, 'rw', XML_GPU + 'A8XX_CP_SQE_CNTL; LNX hw_init = 1 starts the SQE'),
    _gx('CP_SQE_INSTR_BASE_HI', 0x817, 'rw', XML_GPU + 'A8XX_CP_SQE_INSTR_BASE upper word'),
    _gx('CP_APERTURE_CNTL_HOST', 0x81C, 'rw', XML_GPU + 'A8XX_CP_APERTURE_CNTL_HOST: PIPEID [15:12], SLICEID [18:16]'),
    _gx('CP_APRIV_CNTL_PIPE', 0x84D, 'rw', XML_GPU + 'A8XX_CP_APRIV_CNTL_PIPE (per aperture pipe)'),
    _gx('CP_PROTECT_CNTL_PIPE', 0x84E, 'rw', XML_GPU + 'A8XX_CP_PROTECT_CNTL_PIPE (per aperture pipe)'),
    _gx('CP_PROTECT_GLOBAL', 0x850, 'rw', XML_GPU + 'A8XX_CP_PROTECT_GLOBAL(i) 0x850 + i', count=64),
    _gx('CP_PROTECT_PIPE', 0x8A0, 'rw', XML_GPU + 'A8XX_CP_PROTECT_PIPE(i) 0x8a0 + i', count=16),
    _gx('CP_HW_FAULT_STATUS_MASK_PIPE', 0x8B4, 'rw', XML_GPU + 'A8XX_CP_HW_FAULT_STATUS_MASK_PIPE'),
    _gx('CP_INTERRUPT_STATUS_MASK_GLOBAL', 0x8B6, 'rw', XML_GPU + 'A8XX_CP_INTERRUPT_STATUS_MASK_GLOBAL'),
    _gx('CP_INTERRUPT_STATUS_MASK_PIPE', 0x8B8, 'rw', XML_GPU + 'A8XX_CP_INTERRUPT_STATUS_MASK_PIPE'),
    _gx('CP_PERFCTR_CP_SEL', 0x8D0, 'rw', XML_GPU + 'A8XX_CP_PERFCTR_CP_SEL(i)', count=16),
    _gx('UCHE_MODE_CNTL', 0xE01, 'rw', XML_GPU + 'A6XX_UCHE_MODE_CNTL; x285_nonctxt 0x80080000'),
    _gx('UCHE_WRITE_THRU_BASE', 0xE06, 'rw', XML_GPU + 'A8XX_UCHE_WRITE_THRU_BASE (64-bit)', count=2),
    _gx('UCHE_TRAP_BASE', 0xE08, 'rw', XML_GPU + 'A8XX_UCHE_TRAP_BASE (64-bit)', count=2),
    _gx('UCHE_VARB_IDLE_TIMEOUT', 0xE10, 'rw', XML_GPU + 'A8XX_UCHE_VARB_IDLE_TIMEOUT; x285_nonctxt 0x20'),
    _gx('UCHE_CLIENT_PF', 0xE11, 'rw', XML_GPU + 'A8XX_UCHE_CLIENT_PF; LNX hw_init BIT(7) | 1'),
    _gx('UCHE_GBIF_GX_CONFIG', 0xE12, 'rw', XML_GPU + 'A8XX_UCHE_GBIF_GX_CONFIG; x285_nonctxt 0x010240e0'),
    _gx('UCHE_CCHE_MODE_CNTL', 0xF01, 'rw', XML_GPU + 'A8XX_UCHE_CCHE_MODE_CNTL; x285_nonctxt 0x1000'),
    _gx('UCHE_CCHE_CACHE_WAYS', 0xF02, 'rw', XML_GPU + 'A8XX_UCHE_CCHE_CACHE_WAYS; x285_nonctxt 0x800'),
    _gx('UCHE_CCHE_WRITE_THRU_BASE', 0xF04, 'rw', XML_GPU + 'A8XX_UCHE_CCHE_WRITE_THRU_BASE (64-bit)', count=2),
    _gx('UCHE_CCHE_TRAP_BASE', 0xF06, 'rw', XML_GPU + 'A8XX_UCHE_CCHE_TRAP_BASE (64-bit)', count=2),
    _gx('UCHE_CCHE_GC_GMEM_RANGE_MIN', 0xF08, 'rw', XML_GPU + 'A8XX_UCHE_CCHE_GC_GMEM_RANGE_MIN (64-bit)', count=2),
    _gx('GRAS_TSEFE_DBG_ECO_CNTL', 0x8600, 'rw', XML_GPU + 'A8XX_GRAS_TSEFE_DBG_ECO_CNTL; x285_nonctxt'),
    _gx('GRAS_DBG_ECO_CNTL', 0x8702, 'rw', XML_GPU + 'A8XX_GRAS_DBG_ECO_CNTL; x285_nonctxt'),
    _gx('RB_GC_GMEM_PROTECT', 0x8E09, 'rw', XML_GPU + 'A8XX_RB_GC_GMEM_PROTECT; x285_nonctxt 0x15000000 (BR), '
        'written again after SECVID (LNX hw_init)'),
    _gx('RB_CONTEXT_SWITCH_GMEM_SAVE_RESTORE_ENABLE', 0x8E50, 'rw',
        XML_GPU + 'A6XX_RB_CONTEXT_SWITCH_GMEM_SAVE_RESTORE_ENABLE; LNX hw_init BR = 1'),
    _gx('RB_RESOLVE_PREFETCH_CNTL', 0x8F01, 'rw', XML_GPU + 'A8XX_RB_RESOLVE_PREFETCH_CNTL; x285_nonctxt 7'),
    _gx('RB_CMP_DBG_ECO_CNTL', 0x8F02, 'rw', XML_GPU + 'A8XX_RB_CMP_DBG_ECO_CNTL; x285_nonctxt 0x4000'),
    _gx('VPC_FLATSHADE_MODE_CNTL', 0x9741, 'rw', XML_GPU + 'A8XX_VPC_FLATSHADE_MODE_CNTL; x285_nonctxt 1'),
    _gx('PC_AUTO_VERTEX_STRIDE', 0x9E0A, 'rw', XML_GPU + 'A6XX_PC_AUTO_VERTEX_STRIDE; x285_nonctxt 1'),
    _gx('PC_VIS_STREAM_CNTL', 0x9E0D, 'rw', XML_GPU + 'A8XX_PC_VIS_STREAM_CNTL; x285_nonctxt 0x10010000'),
    _gx('PC_CHICKEN_BITS_3', 0x9E22, 'rw', XML_GPU + 'A8XX_PC_CHICKEN_BITS_3; x285_nonctxt 0x00500000'),
    _gx('PC_CHICKEN_BITS_4', 0x9E23, 'rw', XML_GPU + 'A8XX_PC_CHICKEN_BITS_4; x285_nonctxt 0x00500050'),
    _gx('PC_CHICKEN_BITS_1', 0x9E50, 'rw', XML_GPU + 'A8XX_PC_CHICKEN_BITS_1; x285_nonctxt 3'),
    _gx('PC_CONTEXT_SWITCH_STABILIZE_CNTL_1', 0x9E64, 'rw',
        XML_GPU + 'A8XX_PC_CONTEXT_SWITCH_STABILIZE_CNTL_1; x285_nonctxt 2'),
    _gx('PC_CHICKEN_BITS_2', 0x9F20, 'rw', XML_GPU + 'A8XX_PC_CHICKEN_BITS_2; x285_nonctxt 0x200'),
    _gx('VFD_DBG_ECO_CNTL', 0xA600, 'rw', XML_GPU + 'A7XX_VFD_DBG_ECO_CNTL; x285_nonctxt 0x8000'),
    _gx('VFD_CB_BV_THRESHOLD', 0xA639, 'rw', XML_GPU + 'A8XX_VFD_CB_BV_THRESHOLD; x285_nonctxt 0x00500050'),
    _gx('VFD_CB_BR_THRESHOLD', 0xA63A, 'rw', XML_GPU + 'A8XX_VFD_CB_BR_THRESHOLD; x285_nonctxt 0x00600060'),
    _gx('VFD_CB_BUSY_REQ_CNT', 0xA63B, 'rw', XML_GPU + 'A8XX_VFD_CB_BUSY_REQ_CNT; x285_nonctxt 0x00200020'),
    _gx('VFD_CB_LP_REQ_CNT', 0xA63C, 'rw', XML_GPU + 'A8XX_VFD_CB_LP_REQ_CNT; x285_nonctxt 0x20'),
    _gx('SP_CHICKEN_BITS_2', 0xAE09, 'rw', XML_GPU + 'A7XX_SP_CHICKEN_BITS_2; x285_nonctxt 0x00820800'),
    _gx('SP_CHICKEN_BITS_3', 0xAE0A, 'rw', XML_GPU + 'A7XX_SP_CHICKEN_BITS_3; x285_nonctxt 0x00300000'),
    _gx('SP_PERFCTR_SHADER_MASK', 0xAE0F, 'rw', XML_GPU + 'A6XX_SP_PERFCTR_SHADER_MASK; x285_nonctxt 0x3f'),
    _gx('SP_HLSQ_TIMEOUT_THRESHOLD_DP', 0xAE6B, 'rw', XML_GPU + 'A7XX_SP_HLSQ_TIMEOUT_THRESHOLD_DP; x285_nonctxt 0x80'),
    _gx('SP_READ_SEL', 0xAE6D, 'rw', XML_GPU + 'A7XX_SP_READ_SEL; x285_nonctxt 0x0001ff00'),
    _gx('SP_HLSQ_DBG_ECO_CNTL_2', 0xAE74, 'rw', XML_GPU + 'A7XX_SP_HLSQ_DBG_ECO_CNTL_2; x285_nonctxt BIT(31)'),
    _gx('SP_HLSQ_GC_GMEM_RANGE_MIN', 0xAE10, 'rw', XML_GPU + 'A8XX_SP_HLSQ_GC_GMEM_RANGE_MIN (64-bit)', count=2),
    _gx('TPL1_DBG_ECO_CNTL', 0xB600, 'rw', XML_GPU + 'A6XX_TPL1_DBG_ECO_CNTL; x285_nonctxt 0x10000000'),
    _gx('TPL1_DBG_ECO_CNTL1', 0xB602, 'rw', XML_GPU + 'A6XX_TPL1_DBG_ECO_CNTL1; x285_nonctxt 0x04000720'),
    _gx('TPL1_BICUBIC_WEIGHTS_TABLE', 0xB606, 'rw', XML_GPU + 'A8XX_TPL1_BICUBIC_WEIGHTS_TABLE(i)', count=20),
    _gx('RBBM_SECVID_TSB_TRUSTED_BASE', 0xF800, 'rw', XML_GPU + 'A6XX_RBBM_SECVID_TSB_TRUSTED_BASE (64-bit)',
        count=2),
    _gx('RBBM_SECVID_TSB_TRUSTED_SIZE', 0xF802, 'rw', XML_GPU + 'A6XX_RBBM_SECVID_TSB_TRUSTED_SIZE'),
    _gx('RBBM_SECVID_TSB_CNTL', 0xF803, 'rw', XML_GPU + 'A6XX_RBBM_SECVID_TSB_CNTL'),

    # --- SQE-reset diagnostics (sqe-reset/DIAGNOSIS.md): read before and after CP_SQE_CNTL ----------
    _gx('CP_AQE_INSTR_BASE_0', 0x818, 'r', XML_GPU + 'A8XX_CP_AQE_INSTR_BASE_0 (64-bit); LNX hw_init writes it only '
        'when the catalog names an AQE image (the X2-90 entry does not): read to see what the AQE would fetch',
        count=2),
    _gx('CP_CP2GMU_STATUS', 0x822, 'r', XML_GPU + 'A8XX_CP_CP2GMU_STATUS'),
    _gx('CP_INTERRUPT_STATUS_GLOBAL', 0x8B5, 'r', XML_GPU + 'A8XX_CP_INTERRUPT_STATUS_GLOBAL: HWFAULT/SWFAULT per pipe'),
    _gx('RBBM_SW_FUSE_INT_STATUS', 0x071, 'r', 'KGSL gen8_reg.h GEN8_RBBM_SW_FUSE_INT_STATUS 0x071 '
        '(upstream a6xx.xml has it for A7XX only, at 0x2c0)'),
    Reg('SMMU_sGFAR', SMMU_BASE + 0x40, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 GR0 sGFAR +0x40 (low word)'),
    Reg('SMMU_sGFAR_HI', SMMU_BASE + 0x44, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 GR0 sGFAR +0x44 (high word)'),
    Reg('SMMU_sGFSYNR1', SMMU_BASE + 0x54, 'SMMU', 'CX', 'cx_smmu', 'r',
        'Arm SMMUv2 GR0 sGFSYNR1 +0x54: stream ID of the global fault'),
    Reg('SMMU_sGFSYNR2', SMMU_BASE + 0x58, 'SMMU', 'CX', 'cx_smmu', 'r', 'Arm SMMUv2 GR0 sGFSYNR2 +0x58'),
    _cb('SMMU_CB_FSYNR1', 0x06C, 'r', 'LNX arm-smmu.h ARM_SMMU_CB_FSYNR1 0x6c'),
    # KGSL gen8_enable_ahb_timeout_detection (adreno_gen8.c): the five CX AHB ports get a timeout below
    # the GPU config-NoC timeout, so a stuck GPU register access ends as an AHB error (an SError the
    # q1n1 guard reports) instead of a NoC timeout. Offsets gen8_reg.h 0x27810..0x27814 (dwords).
    Reg('CX_MISC_CX_AHB_CNTL', 0x03D9E040, 'CX_MISC', 'CX', 'cx_misc', 'rw',
        'KGSL gen8_reg.h GEN8_GPU_CX_MISC_CX_AHB_{AON,GMU,CP,VBIF_SMMU,HOST}_CNTL 0x27810..0x27814; '
        'adreno.h ADRENO_AHB_CNTL_DEFAULT BIT(12)|BIT(11)|BIT(9)|BIT(8), timeout [4:0]', count=5),
    # IMEM (AO, read-only). Offsets are the msm-imem layout of the SM8550-class downstream trees
    # (qcom,msm-imem-download_mode @0, dload_type @0x1c, restart_reason @0x65c, boot_stats @0x6b0): INFERRED
    # for glymur, where only pil-reloc @0x94c is public.
    Reg('IMEM_DLOAD_MAGIC', 0x14680000, 'IMEM', 'AO', 'always', 'r', 'INFERRED msm-imem download_mode @0x0 (2 words)',
        count=2),
    Reg('IMEM_DLOAD_TYPE', 0x1468001C, 'IMEM', 'AO', 'always', 'r', 'INFERRED msm-imem dload_type @0x1c'),
    Reg('IMEM_RESTART_REASON', 0x1468065C, 'IMEM', 'AO', 'always', 'r', 'INFERRED msm-imem restart_reason @0x65c'),
    Reg('IMEM_BOOT_STATS', 0x146806B0, 'IMEM', 'AO', 'always', 'r', 'INFERRED msm-imem boot_stats @0x6b0 (8 words)',
        count=8),
    Reg('IMEM_PIL_RELOC', 0x1468094C, 'IMEM', 'AO', 'always', 'r', 'LNX glymur.dtsi pil-sram@94c (first word)'),
    # ddr-survey --llcc: LLCC_TRP_STATUSn (llcc-qcom.c v6, 4 + n * 4 KiB) for slice n, OR over the banks through
    # the broadcast base and AND through the broadcast-and base; bit 0 ACTIVE, bit 1 DEACTIVE, bit 4 ACT_COMPLETE.
    Reg('LLCC_BCAST_TRP_STATUS', 0x20400004, 'LLCC_BCAST', 'AO', 'always', 'r',
        'LNX llcc-qcom.c LLCC_TRP_STATUSn 4 + n*4K via llcc_broadcast_base', count=32, stride=0x1000),
    Reg('LLCC_BCAST_AND_TRP_STATUS', 0x20600004, 'LLCC_BCAST', 'AO', 'always', 'r',
        'LNX llcc-qcom.c LLCC_TRP_STATUSn via llcc_broadcast_and_base', count=32, stride=0x1000),
    # ddr-survey --ddr-stats: qcom_stats.c DDR block (magic 0xa1157a75, entry count, 16-byte entries).
    Reg('RPMH_DDR_STATS', 0x0C30F0B8, 'RPMH_STATS', 'AO', 'always', 'r',
        'LNX qcom_stats.c ddr stats at rpmh-stats + 0xb8: magic, count, {name, count, duration u64}', count=130),
    # llcc-survey / llcc-activate / llcc-deactivate (a16gpu_llcc.py): the v6 slice registers llcc-qcom.c programs through
    # llcc_broadcast_base (0x20400000, writes reach every bank) and reads back through llcc_broadcast_and_base
    # (0x20600000, the AND over the banks). Offsets: LLCC_TRP_ACT_CTRLn/ACT_CLEARn (llcc-qcom.c:49-51) and
    # llcc_v6_reg_offset (llcc-qcom.c:4124-4142): ATTR0..3 at 0x41000/08/10/14 + 64 n, the per-slice bit words at
    # 0x42008.. (+4 per 32 slices), COMMON_HW_INFO 0x64000, COMMON_STATUS0 0x6400c (bank count in bits 31:28).
    Reg('LLCC_BCAST_TRP_ACT_CTRL', 0x20400000, 'LLCC_BCAST', 'AO', 'always', 'rw',
        'LNX llcc-qcom.c:49 LLCC_TRP_ACT_CTRLn n*4K; llcc_update_act_ctrl (:4567)', count=32, stride=0x1000),
    Reg('LLCC_BCAST_TRP_ACT_CLEAR', 0x20400008, 'LLCC_BCAST', 'AO', 'always', 'rw',
        'LNX llcc-qcom.c:50 LLCC_TRP_ACT_CLEARn 8 + n*4K', count=32, stride=0x1000),
    Reg('LLCC_BCAST_COMMON_HW_INFO', 0x20464000, 'LLCC_BCAST', 'AO', 'always', 'r',
        'LNX llcc-qcom.c llcc_v6_reg_offset[LLCC_COMMON_HW_INFO] 0x64000 (the version, probe :5161)'),
    Reg('LLCC0_COMMON_STATUS0', 0x2186400C, 'LLCC0', 'AO', 'always', 'r',
        'LNX llcc-qcom.c llcc_v6_reg_offset[LLCC_COMMON_STATUS0] 0x6400c; LLCC_LB_CNT_MASK [31:28] (probe :5125)'),
) + tuple(
    Reg(f'LLCC_{base}_TRP_ATTR{n}', addr + off, 'LLCC_BCAST', 'AO', 'always', access,
        f'LNX llcc-qcom.c LLCC_V6_TRP_ATTR{n}_CFGn = llcc_v6_reg_offset + 64 n ({base.lower()})', count=32, stride=64)
    for base, addr, access in (('BCAST', 0x20400000, 'rw'), ('BCAST_AND', 0x20600000, 'r'))
    for n, off in ((0, 0x41000), (1, 0x41008), (2, 0x41010), (3, 0x41014))
) + tuple(
    Reg(f'LLCC_{base}_{name}', addr + off, 'LLCC_BCAST', 'AO', 'always', access,
        f'LNX llcc-qcom.c llcc_v6_reg_offset[LLCC_TRP_{name}] {off:#x} (+4 per 32 slices; {base.lower()})', count=2)
    for base, addr, access in (('BCAST', 0x20400000, 'rw'), ('BCAST_AND', 0x20600000, 'r'))
    for name, off in (('ALGO_STALE_EN', 0x42008), ('ALGO_STALE_CAP_EN', 0x42010), ('ALGO_MRU0', 0x42018),
                      ('ALGO_MRU1', 0x42020), ('ALGO_ALLOC0', 0x42028), ('ALGO_ALLOC1', 0x42030),
                      ('ALGO_ALLOC2', 0x42038), ('ALGO_ALLOC3', 0x42040), ('WRS_EN', 0x42080),
                      ('WRS_CACHEABLE_EN', 0x42088))
) + (
    # llcc-gpu: the GPU's own SCID for its traffic classes (a8xx_llc_activate, a8xx_gpu.c:1269-1294).
    _gbif('GBIF_SCACHE_CNTL0', 0x3C01, 'rw', XML_GPU + 'A6XX_GBIF_SCACHE_CNTL0 (a6xx.xml:1267); LNX a8xx_gpu.c:1286'),
    _gbif('GBIF_SCACHE_CNTL1', 0x3C02, 'rw', XML_GPU + 'A6XX_GBIF_SCACHE_CNTL1 (a6xx.xml:1268); LNX a8xx_gpu.c:1279'),
)

# Snapintosh agents/gpu-x290 (a16gpu_x290.py: x290-survey, x290-slices, cp-fence --x290). Offsets: Mesa a6xx.xml A8XX
# variants (line numbers of the vendored Mesa 26.3 copy). The NC_MODE registers are what a8xx_set_ubwc_config writes
# (linux-next a8xx_gpu.c:277-365), which q1n1's hw_init skips by default; the clock-gating registers are what
# a8xx_set_hwcg writes (a8xx_gpu.c:199-237), which q1n1 never writes; the survey only reads those.
_X290_REGS = (
    _gx('RBBM_CLOCK_CNTL_GLOBAL', 0x09A, 'r', XML_GPU + 'A8XX_RBBM_CLOCK_CNTL_GLOBAL (a6xx.xml:812); LNX a8xx_set_hwcg'),
    _gx('RBBM_CGC_GLOBAL_LOAD_CMD', 0x09B, 'r', XML_GPU + 'A8XX_RBBM_CGC_GLOBAL_LOAD_CMD (a6xx.xml:923); a8xx_set_hwcg'),
    _gx('RBBM_CGC_P2S_STATUS', 0x09F, 'r', XML_GPU + 'A8XX_RBBM_CGC_P2S_STATUS (a6xx.xml:934): TXDONE bit 0'),
    _gx('RBBM_CGC_0_PC', 0x10B, 'r', XML_GPU + 'A8XX_RBBM_CGC_0_PC (a6xx.xml:948); a8xx_set_hwcg x285 family 0x702'),
    _gx('UCHE_CCHE_LPAC_GMEM_RANGE_MIN', 0xF0A, 'r', XML_GPU + 'A8XX_UCHE_CCHE_LPAC_GMEM_RANGE_MIN (64-bit, a6xx.xml:1227)',
        count=2),
    _gx('RB_CCU_CNTL', 0x8E07, 'r', XML_GPU + 'A7XX- RB_CCU_CNTL (a6xx.xml:2871), BR pipe, context (cmd) register'),
    _gx('RB_CCU_NC_MODE_CNTL', 0x8E08, 'rw', XML_GPU + 'A8XX_RB_CCU_NC_MODE_CNTL (a6xx.xml:2886); BR, a8xx_set_ubwc_config'),
    _gx('RB_LPAC_GMEM_PROTECT', 0x8E0A, 'r', XML_GPU + 'A8XX_RB_LPAC_GMEM_PROTECT (a6xx.xml:2890); BR'),
    _gx('RB_CCU_CACHE_CNTL', 0x88E5, 'r', XML_GPU + 'A8XX_RB_CCU_CACHE_CNTL (a6xx.xml:2676), BR context register: '
        'COLOR_OFFSET [13:0] >> 12, COLOR_CACHE_SIZE [15:14], DEPTH_OFFSET [29:16] >> 12, DEPTH_CACHE_SIZE [31:30]'),
    _gx('GRAS_NC_MODE_CNTL', 0x8700, 'rw', XML_GPU + 'A8XX_GRAS_NC_MODE_CNTL (a6xx.xml:2219); BV and BR'),
    _gx('RB_CMP_NC_MODE_CNTL', 0x8F00, 'rw', XML_GPU + 'A8XX_RB_CMP_NC_MODE_CNTL (a6xx.xml:2898); BR'),
    _gx('RB_UNSLICE_STATUS', 0x8F03, 'r', XML_GPU + 'A8XX_RB_UNSLICE_STATUS (a6xx.xml:2902)'),
    _gx('SP_NC_MODE_CNTL', 0xAE02, 'rw', XML_GPU + 'SP_NC_MODE_CNTL (a6xx.xml:4364); global, a8xx_set_ubwc_config'),
    _gx('SP_HLSQ_LPAC_GMEM_RANGE_MIN', 0xAE12, 'r', XML_GPU + 'A8XX_SP_HLSQ_LPAC_GMEM_RANGE_MIN (64-bit, a6xx.xml:4423)',
        count=2),
    _gx('TPL1_NC_MODE_CNTL', 0xB604, 'rw', XML_GPU + 'TPL1_NC_MODE_CNTL (a6xx.xml:4580); global, a8xx_set_ubwc_config'),
    _gmu('GMU_AO_GMU_CGC_MODE_CNTL', 0x23B09, 'r', XML_GMU + 'GPU_GMU_AO_GMU_CGC_MODE_CNTL (a6xx_gmu.xml:220); a8xx_set_hwcg'),
    _gmu('GMU_AO_GMU_CGC_DELAY_CNTL', 0x23B0A, 'r', XML_GMU + 'GPU_GMU_AO_GMU_CGC_DELAY_CNTL (a6xx_gmu.xml:221)'),
    _gmu('GMU_AO_GMU_CGC_HYST_CNTL', 0x23B0B, 'r', XML_GMU + 'GPU_GMU_AO_GMU_CGC_HYST_CNTL (a6xx_gmu.xml:222)'),
)

_REGS = _REGS + _STAGE3_REGS + _X290_REGS
REGS = {reg.name: reg for reg in _REGS}
assert len(REGS) == len(_REGS), 'duplicate register name'

# Names from the drafts that are deliberately absent (REVIEW corrections).
DROPPED = {
    'GMU_GX_SPTPRAC_POWER_CONTROL': 'SPTPRAC does not exist on a8xx (REVIEW)',
    'GPU_GMU_GX_SPTPRAC_CLOCK_CONTROL': 'SPTPRAC does not exist on a8xx (REVIEW)',
    'GMU_PWR_COL_SPTPRAC_HYST': 'SPTPRAC does not exist on a8xx (REVIEW)',
    'GMU_CM3_DTCM_START': 'legacy name; the GMU sees its DTCM at VA 0x10004000 (REVIEW). Stage 3 correction: the '
                          'host still writes a8xx DTCM blocks through this window (a6xx_gmu_fw_load), listed '
                          'as GMU_CM3_DTCM',
}

# --- regions (DRAM, not registers) -------------------------------------------------
CMD_DB = (0x81C60000, 0x20000)             # LNX glymur.dtsi aop-cmd-db (reserved, no-map)
GPU_ZAP_CARVEOUT = (0x919FE000, 0x2000)    # LNX glymur.dtsi gpu-microcode (unused: no zap node on UX3607OA)
CMD_DB_MAGIC = bytes((0xDB, 0x30, 0x03, 0x0C))  # LNX drivers/soc/qcom/cmd-db.c
CMD_DB_GPU_KEYS = ('gfx.lvl', 'gmxc.lvl', 'mx.lvl', 'cx.lvl', 'SH0', 'MC0', 'ACV', 'CN0')   # CN0: HFI BW table

# --- bits ---------------------------------------------------------------------------
BCR_BLK_ARES = 1 << 0
CBCR_CLK_ENABLE = 1 << 0
CBCR_HW_CTL = 1 << 1                        # clk_branch hwcg bit (GCC GPU branches: hwcg_bit 1)
CBCR_CLK_OFF = 1 << 31
GDSC_SW_COLLAPSE = 1 << 0
GDSC_HW_CONTROL = 1 << 1
GDSC_SW_OVERRIDE = 1 << 2
GDSC_CLK_DIS_WAIT = 0xF << 12
GDSC_EN_FEW_WAIT = 0xF << 16
GDSC_EN_REST_WAIT = 0xF << 20
GDSC_RETAIN_FF = 1 << 11
GDSC_PWR_ON = 1 << 31
# gpu_cc_cx_gdsc waits (gpucc-glymur.c:506-508) in gdsc_init's fields.
CX_GDSC_WAITS = (0x2 << 20) | (0x2 << 16) | (0xF << 12)
CFG_POWER_UP_COMPLETE = 1 << 16
CFG_POWER_DOWN_COMPLETE = 1 << 15
RCG_CMD_UPDATE = 1 << 0
RCG_CMD_ROOT_EN = 1 << 1
RCG_CMD_ROOT_OFF = 1 << 31
RCG_CFG_SRC_DIV = 0x1F
RCG_CFG_SRC_SEL = 0x7 << 8
GCC_VOTE_GPU_GPLL0 = 1 << 0
GCC_VOTE_GPU_GPLL0_DIV = 1 << 1

# --- identity ----------------------------------------------------------------------------
CATALOG_CHIP_ID = 0x44070001                # LNX a6xx_catalog.c a8xx_gpus[0]
MAX_SLICES = 4                              # LNX a6xx_catalog.c .max_slices = 4
SLICE_MASK = (1 << MAX_SLICES) - 1
FREQ_LIMIT_MASK = 0x1FF                     # XML FINALFREQLIMIT[8:0]
SPEEDBINS = {0: 0, 388: 1, 357: 2, 284: 3}  # LNX a6xx_catalog.c:2436 ADRENO_SPEEDBINS
GMU_CHIPID = 0x8010100


def runtime_chip_id(slice_enable_final):
    """a8xx_gpu_get_slice_info(): catalog id | popcount(mask & GENMASK(3,0)) << 4."""
    return CATALOG_CHIP_ID | (bin(slice_enable_final & SLICE_MASK).count('1') << 4)


def speed_bin(freq_limit_status):
    fuse = freq_limit_status & FREQ_LIMIT_MASK
    return fuse, SPEEDBINS.get(fuse)


# --- interrupts and stream IDs (reference for later stages) -------------------------------
INTID_GFX = 0x14C          # ACPI GFX_INTERRUPT, DT SPI 300
INTID_GFX_LPAC = 0x14D     # ACPI GFX_LPAC_INTERRUPT
INTID_GMU_HFI = 0x150      # ACPI GMU_TO_HOST_INTERRUPT, DT SPI 304 "hfi"
INTID_GMU = 0x151          # DT SPI 305 "gmu"
INTID_SMMU_GLOBAL = 0x2C2  # DT SPI 674, ACPI IORT
INTID_SMMU_CB0 = 0x2C6     # DT SPI 678
SURVEY_INTIDS = (INTID_GFX, INTID_GFX_LPAC, INTID_GMU_HFI, INTID_GMU, INTID_SMMU_GLOBAL, INTID_SMMU_CB0)
SID_GPU, SID_LPAC, SID_GMU = 0, 1, 5       # LNX glymur.dtsi iommus


def reg(name):
    return REGS[name]


# === Stage 3 constants ===================================================================
# Each value names its source. PROVEN = shown on this unit or byte-for-byte in the upstream
# source/firmware; INFERRED = reasoned, not yet shown on the A16.

# GPUCC PLL0, Taycan EKO_T at 1150 MHz (gpucc-glymur.c:43-51; clk_lucid_evo_pll_configure):
PLL0_L_VAL = 0x3B | (0x44 << 16)          # l | TRION_PLL_CAL_VAL << LUCID_EVO_PLL_CAL_L_VAL_SHIFT
PLL0_ALPHA = 0xE555
PLL0_CONFIG_CTL = 0x25C400E7
PLL0_CONFIG_CTL_U = 0x0A8060E0
PLL0_CONFIG_CTL_U1 = 0xF51DEA20
PLL0_USER_CTL = 0x00000408                 # bit 3 + post-div even (bits 13:10) = 1 -> /2
PLL0_USER_CTL_U = 0x00000002
PLL_MODE_OUTCTRL, PLL_MODE_RESET_N = 1 << 0, 1 << 2
PLL_MODE_PCAL_NOT_DONE = 1 << 8            # LUCID_EVO_PCAL_NOT_DONE (A16 stage1: MODE 0x100 = never calibrated)
PLL_MODE_LOCK_DET = 1 << 31
PLL_OPMODE_STANDBY, PLL_OPMODE_RUN = 0, 1
PLL_USER_OUT_MASK = 0x7
PLL_USER_VOTE_RUN = 1 << 25                # LUCID_EVO_ENABLE_VOTE_RUN (FSM mode: not ours)
PLL_POLL_US = 1500                         # wait_for_pll: 1500 x 1 us
PLL0_HZ = 1150000000
# RCGs (clk-rcg2.c CFG: SRC_DIV [4:0] = 2*h-1, SRC_SEL [10:8]; F() macro clk-rcg.h:10):
GMU_RCG_CFG_575 = (2 << 8) | 1             # ftbl_gpu_cc_gmu_clk_src F(575 MHz, PLL0_OUT_EVEN, 1): src 2, div 1
HUB_RCG_CFG_200 = (5 << 8) | 5             # clk_set_rate(hub, 150 MHz) -> ceil F(200 MHz, GPLL0, 3): src 5, 2*3-1
RCG_CFG_SRC_MASK = RCG_CFG_SRC_DIV | RCG_CFG_SRC_SEL

# GMU memory (GMU VA, LNX a6xx_gmu_init/a6xx_gmu_memory_alloc; iommus <&adreno_smmu 5 0>):
GMU_SID, GPU_SID = 5, 0
GMU_CB, GPU_CB = 1, 0                      # qcom_adreno_smmu_alloc_context_bank: GPU (SID 0) -> CB0, others from 1
GMU_SMR, GPU_SMR = 1, 0                    # our fixed stream-match indices (Linux takes the first free)
# glymur.dtsi gpu iommus = <&adreno_smmu 0 0x0>, <&adreno_smmu 1 0x0>: Linux puts both streams of the GPU
# device in one domain, so SID 1 also translates through CB0 (arm_smmu_master_alloc_smes, one SME per
# stream ID). x285_nonctxt CP_SMMU_STREAM_ID_LPAC = 0x101 names SID 1 as the LPAC stream (INFERRED).
GPU_SID_LPAC, GPU_SMR_LPAC = 1, 2
MUST_FAULT_SIDS = (2, 4, 7)                # stay unmatched; sCR0.USFCFG makes them fault
GPU_SIDS = (GPU_SID, GPU_SID_LPAC)
GMU_ICACHE_VA, GMU_ICACHE_SIZE = 0x00004000, 0x1000000 - 0x4000     # SZ_16M - SZ_16K, MSM_BO_MAP_PRIV
GMU_DUMMY_VA, GMU_DUMMY_SIZE = 0x60000000, 0x4000                   # SZ_16K (a8xx), priv
GMU_DEBUG_VA, GMU_DEBUG_SIZE = 0x60400000, 0x7000                   # SZ_4K * 7, priv
GMU_LOG_VA, GMU_LOG_SIZE = 0x60004000, 0x4000     # INFERRED: first drm_mm hole above the dummy page
GMU_HFI_VA, GMU_HFI_SIZE = 0x60008000, 0x4000     # INFERRED: next hole; told to the GMU through QTBL_ADDR
GMU_ITCM_BASE, GMU_DTCM_BASE, GMU_TCM_SIZE = 0x0, 0x10004000, 0x4000   # a6xx_gmu_fw_load (a8xx dtcm_base)
GMU_CHIPID_A8 = GMU_CHIPID                  # 0x8010100 -> GENERAL_10
GMU_CM3_CFG_VALUE = 0x4052
GMU_BOOT_CONFIG_VALUE = 0x2
GMU_FENCE_RANGE_0 = (1 << 31) | (0x32 << 18) | 0x8C0     # a8xx fence_range_upper/lower
GBIF_CX_CONFIG_VALUE = 0x20023000
# KGSL gen8_gmu_fw_start writes GBIF_HALT = BIT(3) after the GBIF list, with the CM3 in reset: "We may
# have asserted gbif halt as part of reset sequence which may not get cleared if the gdsc was not reset".
# Run 2 (stage3-run2-c3p.json): GBIF_HALT 0 but GBIF_HALT_ACK 0x3 after hw_init's GBIF_HALT = 0 write.
GBIF_HALT_CLEAR_GEN8 = 1 << 3
GBIF_QSB_SIDE_VALUE = 0x00071E20            # a840_gbif (the X2-90 catalog entry points at it)
MRC_GBIF_QOS_VALUE = 0x33
GMU_INIT_RESULT_MASK, GMU_INIT_RESULT_OK = 0x1FF, 0x100   # firmware version > 0x20010004
GMU_INIT_RESULT_LEGACY = (0xFFFFFFFF, 0xBABEFACE)
GMU_INIT_TIMEOUT_S = 0.100                  # gmu_poll_timeout(..., 100, 100000)
AO_HOST_IRQ_WDOG, AO_HOST_IRQ_FENCE_ERR, AO_HOST_IRQ_AHB_ERR = 1 << 0, 1 << 3, 1 << 5
GMU2HOST_MSGQ, GMU2HOST_CM3_FAULT = 1 << 0, 1 << 23
OOB_GPU_SET_REQUEST, OOB_GPU_SET_ACK, OOB_GPU_SET_CLEAR = 30, 31, 31    # a6xx_gmu_oob_bits set_new/ack_new/clear_new
PWR_CLK_STATUS_GX_OFF = 0x3                 # A8XX_GMU_PWR_CLK_STATUS GX_HM_GDSC_POWER_OFF | GX_HM_CLK_OFF
CX_BUSY_GPUBUSYIGNAHB = 1 << 23
GMU_IDLE_STATE_ACTIVE = 0

# Firmware (linux-firmware 0364daaa; sha256 PROVEN on the host, adreno_x2e_bringup.json):
FIRMWARE = {
    'gmu': ('gen80100_gmu.bin', 117596, 'dae725872a751eccb73aa563dc2d4aeee3ddcc53805a900783787c329cab4d20'),
    'sqe': ('gen80100_sqe.fw', 119828, 'bfcc5193269855bab8961a83425d7ef1f1188e76cd6aee70a5a82f0c1aa7b258'),
}
GMU_FW_VERSION = 0x50020026                 # the DTCM block's word at 0x10007fe0 (v5.2.38), PROVEN from the file

# HFI (LNX a6xx_hfi.h / a6xx_hfi.c):
HFI_MSG_TABLE, HFI_MSG_BW_TABLE, HFI_MSG_CORE_FW_START, HFI_MSG_START = 15, 3, 14, 10
HFI_MSG_GX_BW_PERF_VOTE, HFI_MSG_PREPARE_SLUMBER = 30, 33
HFI_F2H_MSG_ACK, HFI_F2H_MSG_ERROR = 126, 100
# Firmware 0x50020026 acknowledges with the command's own id and header type 1 (HFI_MSG_ACK), not with
# id 126 (PROVEN, stage3-run1-hfi.json: TABLE seq 1 answered 0x0001030f 0x0010110f 0x00000000).
# Linux a6xx_hfi_wait_for_ack matches only on the returned header's seqnum, so both forms are acks.
HFI_MSG_TYPE_ACK = 1
HFI_TABLE_GPU_PERF = 1
HFI_QUEUE_DWORDS = 4096 // 4
HFI_QUEUE_IDS = (0, 1, 2)                   # command, response, debug (non-legacy ids)
HFI_MSG_TIMEOUT_S = 1.0                     # a6xx_hfi_wait_for_msg_interrupt 1 s
HFI_START_TIMEOUT_S = 0.010                 # a6xx_gmu_hfi_start 10 ms
OOB_TIMEOUT_S = 0.010                       # a6xx_gmu_set_oob 10 ms

# glymur.dtsi gpu_opp_table (Hz, RPMh level, peak kBps, opp-supported-hw) and gmu_opp_table:
GPU_OPPS = ((310000000, 56, 2136719, 0xF), (410000000, 64, 6074219, 0xF), (572000000, 128, 12449219, 0xF),
            (760000000, 192, 12449219, 0xF), (820000000, 224, 16500000, 0xF), (915000000, 256, 16500000, 0xF),
            (1070000000, 320, 16500000, 0xF), (1185000000, 384, 16500000, 0xF), (1350000000, 416, 18597657, 0xF),
            (1550000000, 448, 18597657, 0x7), (1700000000, 452, 18597657, 0x7), (1850000000, 456, 18597657, 0x3))
GMU_OPPS = ((575000000, 64), (700000000, 128), (725000000, 192), (750000000, 256))
RPMH_LEVEL_LOW_SVS = 64                     # dt-bindings qcom,rpmhpd.h; gpucc required-opps low_svs for MX and CX
# B1 floors the operator may pick (--rail-level). NOM is at or above what firmware keeps for USB and
# the display, so a NOM vote replacing an unseen drv-2 vote does not lower the rail in practice.
RPMH_LEVELS = {'low_svs': RPMH_LEVEL_LOW_SVS, 'svs': 128, 'nom': 256}
# a6xx_catalog.c X2-90 bcms: SH0 buswidth 16, MC0 buswidth 4, ACV fixed perfmode BIT(2) (a8xx always votes it)
GPU_BCMS = (('SH0', 16, False, 0), ('MC0', 4, False, 0), ('ACV', 0, True, 1 << 2))
BCM_COMMIT, BCM_VALID = 1 << 30, 1 << 29

# RPMh TCS (rpmh-rsc.c):
TCS_AMC_MODE_ENABLE, TCS_AMC_MODE_TRIGGER = 1 << 16, 1 << 24
CMD_MSGID_LEN, CMD_MSGID_RESP_REQ, CMD_MSGID_WRITE = 8, 1 << 8, 1 << 16
CMD_STATUS_ISSUED, CMD_STATUS_COMPL = 1 << 8, 1 << 16
RSC_ACTIVE_TCS = (0, 1)                     # tcs-config order: ACTIVE 2, SLEEP 3, WAKE 3
RSC_SLEEP_TCS, RSC_WAKE_TCS = (2, 3, 4), (5, 6, 7)

# SMMU programming (arm-smmu.c/arm-smmu.h, io-pgtable-arm.c, arm-smmu-qcom.c):
SCR0_CLIENTPD, SCR0_USFCFG = 1 << 0, 1 << 10
SMR_VALID = 1 << 31
S2CR_TYPE_TRANS, S2CR_TYPE_BYPASS, S2CR_TYPE_FAULT = 0, 1, 2
CBAR_S1_BYPASS_S2 = (1 << 16) | (3 << 8) | (0xF << 12)   # TYPE S1_TRANS_S2_BYPASS, BPSHCFG NSH, MEMATTR WB
CBA2R_VA64 = 1
SCTLR_M, SCTLR_TRE, SCTLR_AFE, SCTLR_CFRE, SCTLR_CFIE = 1 << 0, 1 << 1, 1 << 2, 1 << 5, 1 << 6
SCTLR_HUPCF, SCTLR_ASIDPNE = 1 << 8, 1 << 12
CB_SCTLR = SCTLR_M | SCTLR_TRE | SCTLR_AFE | SCTLR_CFRE | SCTLR_CFIE | SCTLR_ASIDPNE | SCTLR_HUPCF
CB_ACTLR = (3 << 8) | (1 << 1) | (1 << 0)  # PREFETCH_DEEP | CPRE | CMTLB
# Non-coherent walk (IORT says non-coherent; PLAN: start there): SH0 OS, IRGN/ORGN NC, 4 KiB, T0SZ 32
# (the GMU and our CP VAs all sit below 4 GiB), EPD1 (no TTBR1).
CB_T0SZ = 32
CB_TCR = (2 << 12) | CB_T0SZ | (1 << 23)
# `cp-fence --va-wide` / `adopt --va-wide` (Snapintosh ADRENO_VA_WIDE, agents/gpu-va-wide): CB0 only takes T0SZ 30
# (34-bit input addresses, still start level 1, 16 root entries; the monitor mirrors GPU VA [1 GiB, 16 GiB) into
# entries 1..15). Every other field is CB_TCR's; CB1 (the GMU) keeps T0SZ 32. It needs SMMU_IDR2.UBS above 32 bits
# (every A16 record reads IDR2 0x00005522: UBS 5).
CB_T0SZ_WIDE = 30
CB_TCR_WIDE = CB_TCR - CB_T0SZ + CB_T0SZ_WIDE
IDR2_UBS_SHIFT, IDR2_UBS_MASK = 8, 0xF
CB_TCR2 = 2 | (7 << 15) | (1 << 4)         # PASIZE 40-bit (IDR2 OAS=2), SEP_UPSTREAM, AS (AArch64)
CB_MAIR0 = 0x44 | (0xFF << 8) | (0x04 << 16) | (0xF4 << 24)   # idx0 NC, 1 WBRWA, 2 DEVICE, 3 INC_OWBRWA
# Bits the A16's SMMU fixes whatever is written (PROVEN, stage3-run1-b1.json, 2026-09-25): CB1 read
# back SCTLR 0x167 for 0x1167 (ASIDPNE RAZ) and TCR2 0x38072 for 0x38012 (bits 5/6, TBI0/TBI1, read 1).
# Neither changes translation here: one bank per stream, and every GMU/CP VA is below 4 GiB (top byte 0).
CB_READBACK_FIXED = {'SMMU_CB_SCTLR': SCTLR_ASIDPNE, 'SMMU_CB_TCR2': (1 << 5) | (1 << 6)}
CB_FSR_FAULT = 0x800001FE | (1 << 30)      # MULTI|SS|UUT|ASF|TLBLKF|TLBMCF|EF|PF|AFF|TF
# LPAE stage-1 descriptors (io-pgtable-arm.c arm_lpae_prot_to_pte):
PTE_TYPE_TABLE = PTE_TYPE_PAGE = 3
PTE_AP_UNPRIV, PTE_AP_RDONLY = 1 << 6, 1 << 7
PTE_SH_OS, PTE_AF, PTE_NG = 2 << 8, 1 << 10, 1 << 11
PTE_ATTR_NC = 0 << 2
PTE_ADDR_MASK = ((1 << 48) - 1) & ~0xFFF

# The GPU arena: physically contiguous, reserved in q1n1 (bootinfo gpu_arena_base/size).
ARENA_SIZE = 32 << 20
ARENA_LAYOUT = {                            # offset, size
    'tables': (0x0000000, 0x0100000),      # LPAE tables for CB0 and CB1, 4 KiB each
    'state': (0x0100000, 0x0001000),       # the host tool's own record (idempotency across invocations)
    'gmu_dummy': (0x0104000, GMU_DUMMY_SIZE),
    'gmu_debug': (0x0108000, GMU_DEBUG_SIZE),
    'gmu_log': (0x0110000, GMU_LOG_SIZE),
    'gmu_hfi': (0x0114000, GMU_HFI_SIZE),
    'cp_sqe': (0x0120000, 0x0020000),
    'cp_rb': (0x0140000, 0x0008000),       # MSM_GPU_RINGBUFFER_SZ 32 KiB
    'cp_memptrs': (0x0148000, 0x0001000),  # rptr shadow, BV rptr, fence
    'gmu_icache': (0x0200000, GMU_ICACHE_SIZE),
    # P6/P7 (docs/A16-ADRENO-STAGE3.md "P6 and P7"): one render window above the icache, mapped on CB0 by
    # blit/triangle only (never by cp-fence), each region with its own permission (a16gpu_p6p7.RENDER_REGIONS).
    'p67_ib': (0x1200000, 0x0010000),      # command buffers (IB1 from the ring), one 8 KiB slot per submission kind
    'p67_misc': (0x1210000, 0x0001000),    # zeroed page: TPL1_*_BORDER_COLOR_BASE (fd6_emit_static_context_regs)
    'p6_src': (0x1220000, 0x0040000),      # P6 blit source: 256x256 BGRA8, pitch 1024
    'p6_dst': (0x1260000, 0x0048000),      # P6 offscreen target: 16 KiB guard, 256x256 BGRA8, 16 KiB guard
    'p7_code': (0x12B0000, 0x0002000),     # P7 ir3 code: VS at +0, FS at +0x1000
    'p7_data': (0x12B2000, 0x0001000),     # P7 vertex buffer +0, UBO 1 (mvp) +0x100, T#0 +0x200, S#0 +0x300
    'p7_tex': (0x12B4000, 0x0004000),      # P7 texture: 64x64 RGBA8, pitch 256
    'p7_rt': (0x12B8000, 0x0008000),       # P7 render target: 4 KiB guard, 96x64 BGRA8 (pitch 384), guard
    # Stage 4 (tools/a16gpu_adopt.py): Snapintosh's monitor mirrors its virtual DART into CB0 root entries 1..3
    # (GPU VA 1..4 GiB) from here: three L2 tables and one L3 table per 2 MiB (compat/adreno.h
    # ADRENO_POOL_BYTES). Only the monitor writes it, and only while it runs; quiesce leaves it unreachable.
    'snap_smmu': (0x1400000, 0x0603000),
    # Stage 6 `render` (tools/a16gpu_render.py): the 28 wscts pipelines. One window above the Stage 4 pool, mapped on
    # CB0 by `render` only, each region with its own permission (a16gpu_render.RENDER_REGIONS; only r_rt writable).
    'r_ib': (0x1A10000, 0x00A0000),        # 40 command buffers, 16 KiB each (one per function)
    'r_misc': (0x1AB0000, 0x0001000),      # zeroed page: border colour table, empty bindless descriptor sets
    'r_code': (0x1AC0000, 0x0050000),      # ir3 code, one 4 KiB slot per stage (VS 2k, FS 2k+1)
    'r_data': (0x1B10000, 0x00A0000),      # per function 16 KiB: vertex buffer, UBOs, driver tables, T#/S#, bindless set
    'r_tex': (0x1BB0000, 0x0100000),       # textures at Mesa's linear pitch and layer stride
    'r_rt': (0x1CB0000, 0x0300000),        # render targets, each after a 4 KiB canary guard
}
P67_ARENA_WINDOW = (0x1200000, 0x00C0000)  # the render window's arena span (every p67/p6/p7 region above)
P67_VA = 0x11000000                        # CB0 VA of the render window's first byte (below 4 GiB, clear of GMEM)
RENDER_ARENA_WINDOW = (0x1A10000, 0x05A0000)  # the render window's arena span (every r_* region above)
RENDER_VA = 0x12000000                     # CB0 VA of its first byte (below the Stage 4 mirror at 1 GiB, clear of GMEM)
FB_VA = 0x20000000                         # CB0 VA window for framebuffer pages (only inside `blit --to-framebuffer`)
FB_VA_LIMIT = 0x08000000                   # at most 128 MiB of framebuffer pages are ever mapped
# GPU (CB0) VAs for the CP's buffers: below 4 GiB, clear of the GMEM range at 64 MiB.
CP_SQE_VA, CP_RB_VA, CP_MEMPTRS_VA = 0x10000000, 0x10100000, 0x10200000
CP_RPTR_SHADOW_OFF, CP_BV_RPTR_OFF, CP_FENCE_OFF = 0x0, 0x8, 0x40
MONITOR_FENCE_OFF = 0x80                    # Stage 4: the Snapintosh monitor's fence, on its own 64-byte line
RB_CNTL_DEFAULT = 12 | (2 << 8)             # BUFSZ ilog2(32K/8), BLKSZ ilog2(32/8)
UCHE_TRAP_BASE = 0x1FFFFFFFFF000             # a6xx_gpu.c uche_trap_base (outside our 32-bit VA: always traps)
GMEM_RANGE_MIN = 64 << 20

# PM4 (adreno_pm4.xml): opcodes and type-7 packet header with odd parity
CP_NOP, CP_WAIT_MEM_WRITES, CP_THREAD_CONTROL, CP_MEM_WRITE, CP_ME_INIT = 16, 18, 23, 61, 72
# P6/P7 packets (adreno_pm4.xml adreno_pm4_type3_packets; a8xx_regs.ENUMS has the same numbers)
CP_WAIT_TIMESTAMP, CP_SKIP_IB2_ENABLE_GLOBAL, CP_SKIP_IB2_ENABLE_LOCAL, CP_WAIT_FOR_IDLE = 20, 29, 35, 38
CP_BLIT, CP_LOAD_STATE6_GEOM, CP_LOAD_STATE6_FRAG, CP_DRAW_INDX_OFFSET, CP_CCHE_INVALIDATE = 44, 50, 52, 56, 58
CP_INDIRECT_BUFFER, CP_SET_DRAW_STATE, CP_EVENT_WRITE7, CP_CONTEXT_REG_BUNCH, CP_NON_CONTEXT_REG_BUNCH = 63, 67, 70, 92, 93
CP_SET_MODE, CP_SET_VISIBILITY_OVERRIDE, CP_SET_MARKER = 99, 100, 101
CP_BV_FENCE_OFF, CP_IB_FENCE_OFF = 0x50, 0x60   # memptrs: the BV-thread fence and the IB's own CACHE_FLUSH_TS
CP_ME_INIT_MASK = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 6)   # a8xx_cp_init without BIT(8) (no pwrup list)
PIPE_NONE, PIPE_BR, PIPE_BV, PIPE_LPAC, PIPE_DDE_BV = 0, 1, 2, 3, 7


def pm4_parity(value):
    return (0x9669 >> (0xF & (value ^ (value >> 4) ^ (value >> 8) ^ (value >> 12) ^ (value >> 16) ^
                              (value >> 20) ^ (value >> 24) ^ (value >> 28)))) & 1


def pkt7(opcode, count):
    return 0x70000000 | count | (pm4_parity(count) << 15) | ((opcode & 0x7F) << 16) | (pm4_parity(opcode) << 23)


def aperture(pipe, slice_id=0):
    return (pipe << 12) | (slice_id << 16)


def _gpl_tables():
    """tools/adreno_x2_protect.py (GPL-2.0-only), loaded by path so this module works however it was
    imported (sys.path, or importlib from another repository)."""
    import importlib.util
    import os
    import sys
    name = 'adreno_x2_protect'
    if name not in sys.modules:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + '.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def protect(reg, length, rdonly):
    """A6XX_PROTECT_NORDWR / A6XX_PROTECT_RDONLY (a6xx_gpu.h)."""
    return (0 if rdonly else 1 << 31) | ((length & 0x3FFF) << 18) | (reg & 0x3FFFF)


# a6xx_catalog.c x285_protect_regs (52 entries; the last one also goes into CP_PROTECT_PIPE(15)):
# The table itself is in adreno_x2_protect.py (GPL-2.0-only, translated from Linux); encoded here.
_X285 = _gpl_tables()
X285_PROTECT = tuple(protect(r, n, ro) for r, n, ro in _X285.X285_PROTECT_REGS)
_NONE, _BR, _BV = 1 << PIPE_NONE, 1 << PIPE_BR, 1 << PIPE_BV
# a6xx_catalog.c x285_nonctxt_regs, in order (register, value, pipe mask): adreno_x2_protect.py
assert (_X285._NONE, _X285._BR, _X285._BV) == (_NONE, _BR, _BV)
X285_NONCTXT = _X285.X285_NONCTXT

# --- agents/gpu-x290: the X2-90 (4-slice) part of the 0x44070001 family ------------------------------------------
# Linux has one catalog entry for the family (a8xx_catalog.c:1580-1621: chip id 0x44070001, max_slices 4, gmem 21 MiB,
# the x285_* lists) and folds the active slice count into the chip id at runtime (a8xx_gpu.c:107-113). Mesa names the
# results: 0x44070041 "Adreno (TM) X2-90" (freedreno_devices.py:1651-1667: 8 CCUs, 4 slices) and 0x44070031
# "Adreno (TM) X2-85" (:1632-1648: 6 CCUs, 3 slices). The A16's Windows driver installs the X2-90 configuration
# (qcdx8480.inf:164 generic DEV_0F36 -> qcdxkmbase8480_185.bin: GmemSize 0x1500000, GmemSizePerSlice 0x540000; the
# X2-85 file qcdxkmbase8480_ma_185.bin has GmemSize 0xfc0000), and every A16 record reads SLICE_ENABLE_FINAL 0xf.
X290_CHIP_ID = 0x44070041
X285_CHIP_ID = 0x44070031
X2_GMEM_BYTES = 21 << 20                     # LNX a8xx_catalog.c:1586 .gmem (max_slices 4)
X2_GMEM_PER_SLICE = X2_GMEM_BYTES // 4       # 5.25 MiB: Windows GmemSizePerSlice; linux-next a8xx_gpu.c:115-116
X2_CCU_PER_SLICE = 2                         # INFERRED from Mesa (X2-85 6 / 3, X2-90 8 / 4); no register reports it
CP_APERTURE_USESLICEID = 1 << 23             # Mesa a6xx.xml:471 a8xx_aperture_cntl USESLICEID
# soc/qcom/ubwc_config.c glymur_data (both Linux copies): UBWC 5.0, level 2 and 3 swizzle disabled, highest bank bit 16.
UBWC_GLYMUR = {'version': 0x50000000, 'disable_swizzle_lvl2': True, 'disable_swizzle_lvl3': True, 'hbb': 16}


def x2_gmem_bytes(slice_enable_final):
    """linux-next a8xx_gpu_get_slice_info: GMEM split evenly between the max_slices, the active share reported."""
    return X2_GMEM_PER_SLICE * bin(slice_enable_final & SLICE_MASK).count('1')


def gmem_protect_for(gmem_bytes):
    """RB_GC_GMEM_PROTECT as every a8xx catalog entry sets it: gmem << 4 (a830 12 MiB 0x0c000000, a840 18 MiB
    0x12000000, x285 family 21 MiB 0x15000000, a810 576 KiB 0x00900000; a8xx_catalog.c nonctxt lists)."""
    return gmem_bytes << 4


def aperture_slice(pipe, slice_id):
    """CP_APERTURE_CNTL_HOST selecting one slice of a pipe: PIPEID, SLICEID and USESLICEID (a8xx_aperture_cntl)."""
    return aperture(pipe, slice_id) | CP_APERTURE_USESLICEID


def ubwc_nc_mode(cfg=None):
    """The five values a8xx_set_ubwc_config (linux-next a8xx_gpu.c:277-365) writes for one UBWC configuration."""
    cfg = cfg or UBWC_GLYMUR
    version = cfg['version']
    l2, l3 = int(cfg['disable_swizzle_lvl2']), int(cfg['disable_swizzle_lvl3'])
    yuvnotcomptofc = int(version in (0x60000000, 0x70000000))
    rgba8888_lossless = int(0x40000000 <= version < 0x50000000)
    fp16compoptdis = int(version < 0x40030000)
    rgb565_predicator = int(version >= 0x40000000)
    amsbc = int(version >= 0x30000000)                       # qcom_ubwc_enable_amsbc
    ubwc_mode = int(version == 0x10000000)                   # qcom_ubwc_get_ubwc_mode
    min_acc_len_64b = 0                                      # a8xx_set_ubwc_config: always false
    mode = 5 if version >= 0x60000000 else 4 if version >= 0x50000000 else 3 if version >= 0x40030000 else \
        2 if version >= 0x40000000 else 1 if version >= 0x30000000 else 0   # qcom_ubwc_version_tag
    hbb = cfg['hbb'] - 13
    hbb_hi, hbb_lo = hbb >> 2, hbb & 3
    return {
        'GRAS_NC_MODE_CNTL': hbb << 5 | l3 << 4 | l2 << 3,
        'RB_CCU_NC_MODE_CNTL': yuvnotcomptofc << 6 | l3 << 5 | l2 << 4 | hbb_hi << 3 | hbb_lo << 1,
        'RB_CMP_NC_MODE_CNTL': (mode << 15 | yuvnotcomptofc << 6 | rgba8888_lossless << 4 | fp16compoptdis << 3 |
                                rgb565_predicator << 2 | amsbc << 1 | min_acc_len_64b),
        'SP_NC_MODE_CNTL': l3 << 13 | l2 << 12 | hbb_hi << 10 | 2 << 4 | min_acc_len_64b << 3 | hbb_lo << 1 | ubwc_mode,
        'TPL1_NC_MODE_CNTL': l3 << 7 | l2 << 6 | hbb_hi << 4 | min_acc_len_64b << 3 | hbb_lo << 1 | ubwc_mode,
    }


BICUBIC_WEIGHTS =(0, 0x3fe05ff4, 0x3fa0ebee, 0x3f5193ed, 0x3f0243f0, 0x00000000, 0x3fd093e8, 0x3f4133dc,
                   0x3ea1dfdb, 0x3e0283e0, 0x0000ac2b, 0x0000f01d, 0x00114412, 0x0021980a, 0x0051ec05,
                   0x0000380e, 0x3ff09001, 0x3fc10bfa, 0x3f9193f7, 0x3f7227f7)       # a8xx_gpu.c hw_init
A8XX_INT_MASK = ((1 << 1) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 15) | (1 << 17) | (1 << 20) |
                 (1 << 22) | (1 << 23) | (1 << 24) | (1 << 25) | (1 << 28) | (1 << 29))
A8XX_APRIV_MASK = 0x1 | 0x2 | 0x4 | 0x8     # ICACHE | RBFETCH | RBPRIVLEVEL | RBRPWB
A8XX_BR_APRIV_MASK = A8XX_APRIV_MASK | 0x20 | 0x40   # + CDREAD | CDWRITE
A8XX_CP_GLOBAL_INT_MASK = 0x7F | (0x7F << 16)        # HWFAULT{BR..DDEBV} | SWFAULT{BR..DDEBV}
A8XX_CP_INT_MASK_PIPE = (0x7F | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12) | (1 << 13) |
                         (1 << 19) | (1 << 21) | (1 << 22) | (1 << 23) | (1 << 24) | (1 << 25) | (1 << 26))
A8XX_CP_HW_FAULT_MASK_PIPE = 0x7F | (1 << 8) | (1 << 9) | (1 << 10)
CP_PROTECT_CNTL = 0xFFFF0000 | (1 << 3) | (1 << 1) | (1 << 0)   # HALT_SQE_RANGE | LAST_SPAN_INF | FAULT | PROT
RBBM_STATUS_CX_MASTER = 1 << 0
# qcom_scm_set_gpu_smmu_aperture(0) (qcom_scm.c; called by adreno_hw_init before the SQE base is written,
# on every a6xx+ when the call is available): SVC_MP 0x0c, CMD 0x1b -> SMC64 SIP 0x42000c1b,
# QCOM_SCM_ARGS(4), args 0xffff0000 | (CP_APERTURE_REG 0 << 8) | context bank 0, then three 0xffffffff.
SMC_CP_APERTURE = (0x42000C1B, 4, 0xFFFF0000, 0xFFFFFFFF)
GPU_VERSION_APERTURE = 3                   # gpu-el2.h Q1N1_GPU_VERSION that forwards the aperture SMC
# CX AHB timeouts (KGSL adreno.c adreno_get_ahb_timeout_val, adreno_gen8.c gen8_enable_ahb_timeout_detection).
AHB_CNTL_DEFAULT = (1 << 12) | (1 << 11) | (1 << 9) | (1 << 8)   # ERRORSTATUSENABLE|RESPONDERROR|TIMEOUTENABLE|RELEASE
# Every bit KGSL documents in these registers (adreno.h: TXFRTIMEOUTRELEASE 8, TXFRTIMEOUTENABLE 9,
# RESPONDERROR 11, ERRORSTATUSENABLE 12, timeout [4:0]). A value found with any other bit set is not the
# register KGSL describes: the tool does not write it (review: the X2-90 layout is INFERRED).
AHB_CNTL_FIELDS = AHB_CNTL_DEFAULT | 0x1F
NOC_TIMEOUT_US = 3410                       # KGSL gen8_0_0/gen8_0_1/gen8_4_0 noc_timeout_us; INFERRED for X2-90
AHB_MIN_TIMEOUT_US = 1000                   # ADRENO_AHB_MIN_TIMEOUT_VAL_USEC


def ahb_timeout_cntl(hub_hz, noc_timeout_us=NOC_TIMEOUT_US):
    """adreno_get_ahb_timeout_val: the largest 2^(v+1) HUB cycles below the NoC timeout, as the CNTL word.
    None when KGSL would refuse (too long for 5 bits, or shorter than 1 ms)."""
    mhz = hub_hz // 1000000
    cycles = mhz * noc_timeout_us
    if cycles & (cycles - 1) == 0:
        cycles -= 1
    value = cycles.bit_length() - 1                  # ilog2
    if value > 0x1F or AHB_MIN_TIMEOUT_US * mhz > (1 << value):
        return None
    return AHB_CNTL_DEFAULT | (value - 1)


CX_AHB_PORTS = ('AON', 'GMU', 'CP', 'VBIF_SMMU', 'HOST')
HUB_HZ = {0x505: 200000000, 0x506: 171428571, 0x507: 150000000}   # GPLL0 600 MHz / (SRC_DIV+1)/2 (src 5)
# IMEM words (reset-reason; INFERRED meanings from the msm-imem/restart drivers of downstream trees).
IMEM_DLOAD_MAGIC = (0xE47B337D, 0xCE14091A)    # download mode armed: a crash enters the ramdump (USB 05c6:9008)
IMEM_RESTART_REASONS = {0x77665500: 'bootloader', 0x77665501: 'normal reboot', 0x77665502: 'recovery',
                        0x77665503: 'rtc', 0x77665577: 'rtc', 0x7766550A: 'dm-verity', 0x6F656D00: 'oem'}
RBBM_INT_HANG_DETECT = 1 << 23
SECVID_TRUST_CNTL_NONSECURE = 0
