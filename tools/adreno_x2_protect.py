# SPDX-License-Identifier: GPL-2.0-only
#
# Translated from Linux drivers/gpu/drm/msm/adreno/a6xx_catalog.c, which carries:
#
# Copyright (C) 2013-2014 Red Hat
# Author: Rob Clark <robdclark@gmail.com>
#
# Copyright (c) 2014,2017 The Linux Foundation. All rights reserved.
"""X2-85 family (chip id 0x44070001) CP protect ranges and non-context register values.

x285_protect_regs and x285_nonctxt_regs from Linux drivers/gpu/drm/msm/adreno/a6xx_catalog.c, translated
entry by entry and in the same order. This file is GPL-2.0-only, the licence of its source
(3rdparty_licenses/LICENSE.GPL-2); the rest of q1n1 is MIT. tools/adreno_x2.py imports it: its X285_PROTECT
is X285_PROTECT_REGS encoded with protect(), and it re-exports X285_NONCTXT. Data only, no imports.
"""

# Pipe bits as Linux writes them: BIT(PIPE_NONE), BIT(PIPE_BR), BIT(PIPE_BV) (adreno_x2.PIPE_* = 0, 1, 2).
_NONE, _BR, _BV = 1 << 0, 1 << 1, 1 << 2

# x285_protect_regs (52 entries; the last one also goes into CP_PROTECT_PIPE(15)):
# Each entry is (register, count, read-only): A6XX_PROTECT_RDONLY(reg, count) when read-only is 1,
# A6XX_PROTECT_NORDWR(reg, count) when it is 0.
X285_PROTECT_REGS = (
    (0x00008, 0x039b, 1), (0x003b4, 0x008b, 1), (0x00440, 0x001f, 0), (0x00580, 0x005f, 1), (0x005e0, 0x011f, 0),
    (0x0074a, 0x0005, 1), (0x00759, 0x0026, 1), (0x00789, 0x0000, 1), (0x0078c, 0x0013, 1), (0x00800, 0x0029, 0),
    (0x0082c, 0x0000, 0), (0x00837, 0x00af, 0), (0x008e7, 0x00c9, 1), (0x008ec, 0x00c3, 0), (0x009b1, 0x0250, 0),
    (0x00ce0, 0x0001, 1), (0x00df0, 0x0000, 1), (0x00df1, 0x0000, 0), (0x00e01, 0x0000, 0), (0x00e03, 0x1fff, 0),
    (0x03c00, 0x00c5, 0), (0x03cc6, 0x0039, 1), (0x03d00, 0x1fff, 0), (0x08600, 0x01ff, 0), (0x08e00, 0x00ff, 0),
    (0x08f00, 0x0000, 1), (0x08f01, 0x01be, 0), (0x09600, 0x01ff, 0), (0x0981a, 0x02e5, 1), (0x09e00, 0x01ff, 0),
    (0x0a600, 0x01ff, 0), (0x0a82e, 0x0000, 0), (0x0ae00, 0x0006, 0), (0x0ae08, 0x0006, 0), (0x0ae10, 0x00bf, 0),
    (0x0aed0, 0x002f, 1), (0x0af00, 0x027f, 0), (0x0b600, 0x1fff, 0), (0x0dc00, 0x1fff, 0), (0x0fc00, 0x1fff, 1),
    (0x18400, 0x003f, 0), (0x18440, 0x013f, 1), (0x18580, 0x1fff, 0), (0x1b400, 0x1fff, 0), (0x1f400, 0x0477, 0),
    (0x1f878, 0x0507, 1), (0x1f930, 0x0329, 0), (0x1fd80, 0x1fff, 0), (0x27800, 0x007f, 0), (0x27880, 0x0385, 1),
    (0x27882, 0x000a, 0), (0x27c06, 0x0000, 0))

# x285_nonctxt_regs, in order (register, value, pipe mask):
X285_NONCTXT = (
    ('CP_SMMU_STREAM_ID_LPAC', 0x00000101, _NONE), ('GRAS_DBG_ECO_CNTL', 0x00000800, _BV | _BR),
    ('GRAS_TSEFE_DBG_ECO_CNTL', 0x00200000, _BV | _BR), ('PC_AUTO_VERTEX_STRIDE', 0x00000001, _BV | _BR),
    ('PC_VIS_STREAM_CNTL', 0x10010000, _BV | _BR), ('PC_CONTEXT_SWITCH_STABILIZE_CNTL_1', 0x00000002, _BV | _BR),
    ('PC_CHICKEN_BITS_1', 0x00000003, _BV | _BR), ('PC_CHICKEN_BITS_2', 0x00000200, _BV | _BR),
    ('PC_CHICKEN_BITS_3', 0x00500000, _BV | _BR), ('PC_CHICKEN_BITS_4', 0x00500050, _BV | _BR),
    ('RB_GC_GMEM_PROTECT', 0x15000000, _BR), ('RB_RESOLVE_PREFETCH_CNTL', 0x00000007, _BR),
    ('RB_CMP_DBG_ECO_CNTL', 0x00004000, _BR), ('RBBM_NC_MODE_CNTL', 0x00000001, _NONE),
    ('RBBM_SLICE_NC_MODE_CNTL', 0x00000001, _NONE), ('RBBM_WAIT_IDLE_CLOCKS_CNTL', 0x00000030, _NONE),
    ('RBBM_WAIT_IDLE_CLOCKS_CNTL2', 0x00000030, _NONE), ('RBBM_INTERFACE_HANG_INT_CNTL', 0x0FFFFFFF, _NONE),
    ('RBBM_GBIF_CLIENT_QOS_CNTL', 0x22122212, _NONE), ('RBBM_CGC_P2S_CNTL', 0x00000040, _NONE),
    ('SP_CHICKEN_BITS_2', 0x00820800, _NONE), ('SP_CHICKEN_BITS_3', 0x00300000, _NONE),
    ('SP_PERFCTR_SHADER_MASK', 0x0000003F, _NONE), ('SP_HLSQ_DBG_ECO_CNTL_2', 1 << 31, _NONE),
    ('SP_HLSQ_TIMEOUT_THRESHOLD_DP', 0x00000080, _NONE), ('SP_READ_SEL', 0x0001FF00, _NONE),
    ('TPL1_DBG_ECO_CNTL', 0x10000000, _NONE), ('TPL1_DBG_ECO_CNTL1', 0x04000720, _NONE),
    ('UCHE_MODE_CNTL', 0x80080000, _NONE), ('UCHE_CCHE_MODE_CNTL', 0x00001000, _NONE),
    ('UCHE_CCHE_CACHE_WAYS', 0x00000800, _NONE), ('UCHE_GBIF_GX_CONFIG', 0x010240E0, _NONE),
    ('UCHE_VARB_IDLE_TIMEOUT', 0x00000020, _NONE), ('VFD_DBG_ECO_CNTL', 0x00008000, _BV | _BR),
    ('VFD_CB_BV_THRESHOLD', 0x00500050, _BV | _BR), ('VFD_CB_BR_THRESHOLD', 0x00600060, _BV | _BR),
    ('VFD_CB_BUSY_REQ_CNT', 0x00200020, _BV | _BR), ('VFD_CB_LP_REQ_CNT', 0x00000020, _BV | _BR),
    ('VPC_FLATSHADE_MODE_CNTL', 0x00000001, _BV | _BR),
)
