/* SPDX-License-Identifier: MIT */
/* Read-only Qualcomm SMEM parser for the post-ExitBootServices stage.
 *
 * Shared memory (SMEM) is the region the application processor and the
 * coprocessors use to talk. It starts with a header, ends with a partition
 * table ("$TOC"), and each pair of processors gets a private partition
 * ("$PRT") holding numbered items. The GLINK transport to the SoCCP lives in
 * items 478/479/480 of the APPS<->SoCCP partition.
 *
 * Layouts: Linux drivers/soc/qcom/smem.c (structs at :67-250, lookup at
 * :294-357 and :590-702, probe checks at :1199-1244), and the A16's own
 * SmemDxe (BIOS 312, FFS f541d663..., strings smem_partition.c/smem_target.c):
 *   - target info: a u64 pointer at physical 0x01FD4000 (SmemDxe RVA
 *     0x2830-0x2844) to {u32 "SIII" 0x49494953, u32 size, u64 base, u16
 *     max_items} (magic compare RVA 0x2854-0x2860, fields 0x2858/0x2878/0x2898);
 *   - initialized word at +0xC0 must be 1 (RVA 0x28a8), heap version in the
 *     top half of +0x5C (version[7], RVA 0x28b4-0x28bc compares 11);
 *   - 0x01F00000 in SmemDxe is NOT SMEM: it is the TCSR mutex block, used as
 *     0x01F00000 + {0x42000, 0x43000} (table at .data RVA 0x4fbc).
 *
 * Nothing here writes, allocates or takes the hardware mutex. Every access is
 * a naturally aligned 16- or 32-bit load, so a Device mapping cannot fault on
 * alignment, and every offset is bounds-checked before it is used.
 *
 * What is read is a subset of what Linux maps (smem.c qcom_smem_map_toc
 * :1083-1097, qcom_smem_enumerate_partitions :1046-1053): the first 4 KiB
 * (header), the last 4 KiB (partition table), and partitions one of whose
 * hosts is APPS (0). q1n1_smem_init touches only the first two pages;
 * q1n1_smem_table_entry and q1n1_smem_find_entry read only the table, so
 * other processors' partitions are listed without touching their headers;
 * q1n1_smem_partition_at refuses any entry APPS is not a host of. Other
 * partitions can be protected by an access-control unit (the firmware ships
 * SmemAcDxe), so a speculative read there is not assumed to be harmless. */
#pragma once
#include <stdint.h>

#define Q1N1_SMEM_TARGET_INFO_POINTER 0x01fd4000ull  /* SmemDxe RVA 0x2830 */
#define Q1N1_SMEM_TARGET_INFO_MAGIC 0x49494953u      /* "SIII", SmemDxe RVA 0x2854 */
#define Q1N1_SMEM_TARGET_INFO_BYTES 24u
/* The fallback when no target info is readable: Linux glymur.dtsi:684-689. */
#define Q1N1_SMEM_DEFAULT_BASE 0xffe00000ull
#define Q1N1_SMEM_DEFAULT_SIZE 0x00200000u

#define Q1N1_SMEM_HOST_APPS 0u
#define Q1N1_SMEM_HOST_GLOBAL 0xfffeu
#define Q1N1_SMEM_TOC_BYTES 0x1000u
#define Q1N1_SMEM_MAX_PARTITIONS 84u   /* what a 4 KiB TOC can hold: (4096 - 32) / 48 */

enum {
    Q1N1_SMEM_OK = 0,
    Q1N1_SMEM_EINVAL = -1,     /* bad argument */
    Q1N1_SMEM_ENOTREADY = -2,  /* header says SMEM is not initialised / unknown version */
    Q1N1_SMEM_ECORRUPT = -3,   /* a bounds, magic or canary check failed */
    Q1N1_SMEM_ENOENT = -4,     /* partition or item absent */
};

struct q1n1_smem_target {
    uint64_t base;
    uint32_t size, max_items;
};

struct q1n1_smem {
    volatile const uint8_t *base;   /* where the CPU reads it (identity at EL2) */
    uint64_t phys;
    uint32_t size;
    uint32_t version;               /* heap version: 11 or 12 */
    uint32_t item_count;            /* from the "SIII" block after the TOC, else 512 */
    uint32_t ptable_offset, ptable_entries;
};

struct q1n1_smem_partition {
    uint32_t index;                 /* TOC entry */
    uint32_t offset, size, cacheline;
    uint32_t host0, host1;
    uint32_t free_uncached, free_cached;
};

struct q1n1_smem_item {
    uint32_t offset;                /* data, from the SMEM base */
    uint32_t size;                  /* usable bytes (size - padding_data) */
    uint32_t cached;                /* found on the downward (cached) list */
};

/* Decode the 24 bytes the target-info pointer names. */
int q1n1_smem_parse_target(const uint8_t raw[Q1N1_SMEM_TARGET_INFO_BYTES], struct q1n1_smem_target *out);

/* Validate the header and the partition table of a mapped SMEM region. */
int q1n1_smem_init(struct q1n1_smem *s, volatile const uint8_t *base, uint64_t phys, uint32_t size);

/* TOC entry index as the table describes it (index, offset, size, hosts,
 * cacheline; the free_* fields are left 0). Reads the table page only.
 * ENOENT for an unused slot, ECORRUPT for an entry that does not fit below
 * the table. */
int q1n1_smem_table_entry(const struct q1n1_smem *s, uint32_t index, struct q1n1_smem_partition *out);

/* The table entry of the private APPS<->host partition, from the table only.
 * Refuses (ECORRUPT) when two entries claim the same pair, rather than
 * picking one. */
int q1n1_smem_find_entry(const struct q1n1_smem *s, uint32_t host, struct q1n1_smem_partition *out);

/* TOC entry index with its partition header validated. The header is read
 * only when APPS is one of the entry's two hosts (EINVAL otherwise). */
int q1n1_smem_partition_at(const struct q1n1_smem *s, uint32_t index, struct q1n1_smem_partition *out);

/* q1n1_smem_find_entry, then q1n1_smem_partition_at on it. */
int q1n1_smem_find_partition(const struct q1n1_smem *s, uint32_t host, struct q1n1_smem_partition *out);

/* Look an item up on both lists of a partition. Walks are bounded by the
 * partition size and stop at the first inconsistent entry. */
int q1n1_smem_get(const struct q1n1_smem *s, const struct q1n1_smem_partition *p, uint32_t item,
                  struct q1n1_smem_item *out);

/* Aligned little-endian loads relative to the SMEM base; the caller has
 * already bounds-checked offset. Exposed for the GLINK layer. */
static inline uint32_t q1n1_smem_rd32(const struct q1n1_smem *s, uint32_t offset)
{
    return *(volatile const uint32_t *)(s->base + offset);
}
