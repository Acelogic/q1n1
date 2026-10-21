/* SPDX-License-Identifier: MIT */
/* See smem.h. Independently written from the layouts cited there. */
#include "smem.h"

#define HEADER_VERSION7 0x5cu          /* version[7]: heap version in bits 31:16 */
#define HEADER_INITIALIZED 0xc0u
#define HEADER_RESERVED 0xccu
#define HEADER_BYTES 0xd0u             /* up to the legacy toc[] */
#define PTABLE_HEADER 32u
#define PTABLE_ENTRY 48u
#define PARTITION_HEADER 32u
#define PRIVATE_ENTRY 16u
#define PRIVATE_CANARY 0xa5a5u
#define DEFAULT_ITEMS 512u

static uint32_t rd32(const struct q1n1_smem *s, uint32_t offset) { return q1n1_smem_rd32(s, offset); }
static uint32_t rd16(const struct q1n1_smem *s, uint32_t offset)
{
    return *(volatile const uint16_t *)(s->base + offset);
}
static int magic(const struct q1n1_smem *s, uint32_t offset, uint32_t value) { return rd32(s, offset) == value; }
#define FOURCC(a, b, c, d) ((uint32_t)(a) | (uint32_t)(b) << 8 | (uint32_t)(c) << 16 | (uint32_t)(d) << 24)

int q1n1_smem_parse_target(const uint8_t raw[Q1N1_SMEM_TARGET_INFO_BYTES], struct q1n1_smem_target *out)
{
    if (!raw || !out) return Q1N1_SMEM_EINVAL;
    uint32_t id = (uint32_t)raw[0] | (uint32_t)raw[1] << 8 | (uint32_t)raw[2] << 16 | (uint32_t)raw[3] << 24;
    if (id != Q1N1_SMEM_TARGET_INFO_MAGIC) return Q1N1_SMEM_ENOENT;
    out->size = (uint32_t)raw[4] | (uint32_t)raw[5] << 8 | (uint32_t)raw[6] << 16 | (uint32_t)raw[7] << 24;
    out->base = 0;
    for (unsigned i = 0; i < 8; i++) out->base |= (uint64_t)raw[8 + i] << (8 * i);
    out->max_items = (uint32_t)raw[16] | (uint32_t)raw[17] << 8;
    /* A region this code can reason about: page aligned, TOC-sized or larger,
     * below 4 GiB like every Qualcomm SMEM carve-out, and not absurdly big. */
    if (!out->base || (out->base & 0xfff) || out->size < 2 * Q1N1_SMEM_TOC_BYTES ||
        out->size > (64u << 20) || (out->size & 0xfff) || out->base + out->size > (1ull << 32))
        return Q1N1_SMEM_ECORRUPT;
    return Q1N1_SMEM_OK;
}

int q1n1_smem_init(struct q1n1_smem *s, volatile const uint8_t *base, uint64_t phys, uint32_t size)
{
    if (!s || !base || size < 2 * Q1N1_SMEM_TOC_BYTES || (size & 0xfff) || ((uintptr_t)base & 0xfff))
        return Q1N1_SMEM_EINVAL;
    *s = (struct q1n1_smem){.base = base, .phys = phys, .size = size};
    if (rd32(s, HEADER_INITIALIZED) != 1 || rd32(s, HEADER_RESERVED) != 0) return Q1N1_SMEM_ENOTREADY;
    s->version = rd32(s, HEADER_VERSION7) >> 16;
    if (s->version != 11 && s->version != 12) return Q1N1_SMEM_ENOTREADY;

    s->ptable_offset = size - Q1N1_SMEM_TOC_BYTES;
    if (!magic(s, s->ptable_offset, FOURCC('$', 'T', 'O', 'C'))) return Q1N1_SMEM_ENOENT;
    if (rd32(s, s->ptable_offset + 4) != 1) return Q1N1_SMEM_ECORRUPT;
    s->ptable_entries = rd32(s, s->ptable_offset + 8);
    if (s->ptable_entries > Q1N1_SMEM_MAX_PARTITIONS) return Q1N1_SMEM_ECORRUPT;

    /* Linux smem.c:902-916: "SIII" right after the entries carries the count. */
    s->item_count = DEFAULT_ITEMS;
    uint32_t info = s->ptable_offset + PTABLE_HEADER + s->ptable_entries * PTABLE_ENTRY;
    if (info + 20 <= size && magic(s, info, FOURCC('S', 'I', 'I', 'I'))) {
        uint32_t count = rd32(s, info + 16) & 0xffff;
        if (count) s->item_count = count;
    }
    return Q1N1_SMEM_OK;
}

int q1n1_smem_table_entry(const struct q1n1_smem *s, uint32_t index, struct q1n1_smem_partition *out)
{
    if (!s || !out || index >= s->ptable_entries) return Q1N1_SMEM_EINVAL;
    uint32_t e = s->ptable_offset + PTABLE_HEADER + index * PTABLE_ENTRY;
    *out = (struct q1n1_smem_partition){
        .index = index, .offset = rd32(s, e), .size = rd32(s, e + 4),
        .host0 = rd16(s, e + 12), .host1 = rd16(s, e + 14), .cacheline = rd32(s, e + 16),
    };
    if (!out->offset || !out->size) return Q1N1_SMEM_ENOENT;          /* unused slot */
    /* SmemDxe asserts: partitions are page aligned and below the TOC. */
    if ((out->offset & 0xfff) || out->size < PARTITION_HEADER || (out->size & 7) ||
        out->offset >= s->ptable_offset || out->size > s->ptable_offset - out->offset)
        return Q1N1_SMEM_ECORRUPT;
    return Q1N1_SMEM_OK;
}

int q1n1_smem_find_entry(const struct q1n1_smem *s, uint32_t host, struct q1n1_smem_partition *out)
{
    if (!s || !out || host == Q1N1_SMEM_HOST_APPS || host >= Q1N1_SMEM_HOST_GLOBAL) return Q1N1_SMEM_EINVAL;
    int found = 0;
    for (uint32_t i = 0; i < s->ptable_entries; i++) {
        struct q1n1_smem_partition p;
        int status = q1n1_smem_table_entry(s, i, &p);
        if (status == Q1N1_SMEM_ENOENT) continue;   /* Linux skips offset 0 / size 0 first */
        if (!((p.host0 == Q1N1_SMEM_HOST_APPS && p.host1 == host) ||
              (p.host1 == Q1N1_SMEM_HOST_APPS && p.host0 == host)))
            continue;
        if (status) return status;
        if (found++) return Q1N1_SMEM_ECORRUPT;   /* two partitions for one pair */
        *out = p;
    }
    return found ? Q1N1_SMEM_OK : Q1N1_SMEM_ENOENT;
}

int q1n1_smem_partition_at(const struct q1n1_smem *s, uint32_t index, struct q1n1_smem_partition *out)
{
    int status = q1n1_smem_table_entry(s, index, out);
    if (status) return status;
    /* Never a header APPS is not party to (Linux smem.c:1046-1053). */
    if (out->host0 != Q1N1_SMEM_HOST_APPS && out->host1 != Q1N1_SMEM_HOST_APPS) return Q1N1_SMEM_EINVAL;
    uint32_t h = out->offset;
    if (!magic(s, h, FOURCC('$', 'P', 'R', 'T')) || rd16(s, h + 4) != out->host0 ||
        rd16(s, h + 6) != out->host1 || rd32(s, h + 8) != out->size)
        return Q1N1_SMEM_ECORRUPT;
    out->free_uncached = rd32(s, h + 12);
    out->free_cached = rd32(s, h + 16);
    /* The two heaps grow towards each other and must not have crossed. */
    if (out->free_uncached < PARTITION_HEADER || out->free_uncached > out->size ||
        out->free_cached > out->size || out->free_cached < out->free_uncached)
        return Q1N1_SMEM_ECORRUPT;
    return Q1N1_SMEM_OK;
}

int q1n1_smem_find_partition(const struct q1n1_smem *s, uint32_t host, struct q1n1_smem_partition *out)
{
    struct q1n1_smem_partition entry;
    int status = q1n1_smem_find_entry(s, host, &entry);
    return status ? status : q1n1_smem_partition_at(s, entry.index, out);
}

int q1n1_smem_get(const struct q1n1_smem *s, const struct q1n1_smem_partition *p, uint32_t item,
                  struct q1n1_smem_item *out)
{
    if (!s || !p || !out || item >= s->item_count || item > 0xffff) return Q1N1_SMEM_EINVAL;
    const uint32_t base = p->offset, size = p->size;
    /* Every entry is at least 16 bytes, so this caps both walks. */
    uint32_t budget = size / PRIVATE_ENTRY + 1;

    /* Uncached: upward from the header. Data at e+16+padding_hdr, next entry
     * after the data (Linux smem.c:294-330). */
    uint32_t e = PARTITION_HEADER;
    while (e < p->free_uncached) {
        if (!budget--) return Q1N1_SMEM_ECORRUPT;
        if (e > size - PRIVATE_ENTRY || (e & 3)) return Q1N1_SMEM_ECORRUPT;
        uint32_t word0 = rd32(s, base + e);
        if ((word0 & 0xffff) != PRIVATE_CANARY) return Q1N1_SMEM_ECORRUPT;
        uint32_t length = rd32(s, base + e + 4), pads = rd32(s, base + e + 8);
        uint32_t pad_data = pads & 0xffff, pad_hdr = pads >> 16;
        uint64_t data = (uint64_t)e + PRIVATE_ENTRY + pad_hdr;
        if (pad_data > length || data + length > p->free_uncached) return Q1N1_SMEM_ECORRUPT;
        if ((word0 >> 16) == item) {
            if (data & 3) return Q1N1_SMEM_ECORRUPT;
            *out = (struct q1n1_smem_item){.offset = base + (uint32_t)data, .size = length - pad_data};
            return Q1N1_SMEM_OK;
        }
        e = (uint32_t)(data + length);
    }
    if (e != p->free_uncached) return Q1N1_SMEM_ECORRUPT;

    /* Cached: downward from the end. The header sits above its data, in a
     * cacheline-sized slot (Linux smem.c:302-357). A zero or odd cacheline
     * cannot describe a layout; treat that list as absent. */
    uint32_t line = p->cacheline;
    if (!line || (line & (line - 1)) || line > 4096) return Q1N1_SMEM_ENOENT;
    uint32_t slot = (PRIVATE_ENTRY + line - 1) & ~(line - 1);
    if (slot > size) return Q1N1_SMEM_ECORRUPT;
    int64_t c = (int64_t)size - slot;
    while (c > (int64_t)p->free_cached) {
        if (!budget--) return Q1N1_SMEM_ECORRUPT;
        if (c & 3) return Q1N1_SMEM_ECORRUPT;
        uint32_t word0 = rd32(s, base + (uint32_t)c);
        if ((word0 & 0xffff) != PRIVATE_CANARY) return Q1N1_SMEM_ECORRUPT;
        uint32_t length = rd32(s, base + (uint32_t)c + 4), pad_data = rd32(s, base + (uint32_t)c + 8) & 0xffff;
        if (pad_data > length || (int64_t)length > c - (int64_t)p->free_cached) return Q1N1_SMEM_ECORRUPT;
        if ((word0 >> 16) == item) {
            uint32_t data = (uint32_t)(c - length);
            if (data & 3) return Q1N1_SMEM_ECORRUPT;
            *out = (struct q1n1_smem_item){.offset = base + data, .size = length - pad_data, .cached = 1};
            return Q1N1_SMEM_OK;
        }
        c = c - length - slot;
    }
    /* A complete walk ends one slot below the last entry's data, which is
     * where the free pointer says the cached heap stops. */
    if (c + slot != (int64_t)p->free_cached) return Q1N1_SMEM_ECORRUPT;
    return Q1N1_SMEM_ENOENT;
}
