/* SPDX-License-Identifier: MIT */
/* Read-only GPT survey; see gpt-survey.h for the contract. The only storage
 * operation in this file is ReadBlocks. Keep it that way: the source and the
 * compiled object are audited by tools/test-gpt-survey.py. */
#include "gpt-survey.h"

static efi_guid block_io_guid = {0x964e5b21,0x6459,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid device_path_guid = {0x09576e91,0x6d3f,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};

static void fill_zero(void *p, uint64_t n) { uint8_t *b = p; while (n--) *b++ = 0; }
static void copy_bytes(void *d, const void *s, uint64_t n)
{ uint8_t *a = d; const uint8_t *b = s; while (n--) *a++ = *b++; }
static uint32_t le32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t le64(const uint8_t *p) { return (uint64_t)le32(p) | (uint64_t)le32(p + 4) << 32; }
static int power_of_two(uint64_t v) { return v && !(v & (v - 1)); }

/* Copy whole device path nodes, stopping at the end node or the buffer limit.
 * Flags an NVMe namespace node (Messaging, subtype 0x17). */
static uint64_t copy_path(const uint8_t *node, uint8_t *out, uint64_t *flags)
{
    uint64_t used = 0;
    for (unsigned n = 0; node && n < 64; n++) {
        uint64_t length = (uint64_t)node[2] | (uint64_t)node[3] << 8;
        if (length < 4 || used + length > Q1N1_GPT_SURVEY_PATH) break;
        if (out) copy_bytes(out + used, node, length);
        used += length;
        if (node[0] == 3 && node[1] == 0x17) *flags |= Q1N1_GPT_NVME_PATH;
        if (node[0] == 0x7f && node[1] == 0xff) return used;
        node += length;
    }
    *flags |= Q1N1_GPT_PATH_TRUNCATED;
    return used;
}

static int geometry_ok(const struct q1n1_block_io_media *m)
{
    return m->block_size >= 512 && m->block_size <= Q1N1_GPT_SURVEY_BLOCK && power_of_two(m->block_size) &&
           (m->io_align <= 1 || (power_of_two(m->io_align) && m->io_align <= 4096)) &&
           m->last_block >= 67;
}

/* The single storage access in this file. Bounded per disk and in range. */
static efi_status read_range(struct q1n1_block_io *io, struct q1n1_gpt_survey *s, struct q1n1_gpt_disk *d,
                             uint64_t lba, uint64_t bytes, uint8_t *scratch, uint8_t *dst, uint64_t capacity)
{
    if (d->reads >= Q1N1_GPT_SURVEY_READS_PER_DISK || !bytes || bytes > capacity ||
        bytes > Q1N1_GPT_SURVEY_ARRAY || bytes % d->block_size)
        return EFI_INVALID_PARAMETER;
    uint64_t blocks = bytes / d->block_size;
    if (lba > d->last_block || blocks - 1 > d->last_block - lba) return EFI_INVALID_PARAMETER;
    d->reads++; s->reads++;
    efi_status status = io->read_blocks(io, (uint32_t)d->media_id, lba, bytes, scratch);
    if (!status) copy_bytes(dst, scratch, bytes);
    return status;
}

/* Where a header says its entry array is. 0: no array to read. */
static int array_extent(const uint8_t *h, const struct q1n1_gpt_disk *d, uint64_t *lba, uint64_t *bytes,
                        uint64_t *flags, uint64_t not_gpt)
{
    static const uint8_t signature[8] = {'E','F','I',' ','P','A','R','T'};
    for (unsigned i = 0; i < 8; i++) if (h[i] != signature[i]) { *flags |= not_gpt; return 0; }
    uint32_t header_size = le32(h + 12), count = le32(h + 80), entry_size = le32(h + 84);
    uint64_t entries = le64(h + 72);
    if (header_size < 92 || header_size > d->block_size || entry_size < 128 || entry_size > 1024 ||
        !power_of_two(entry_size) || !count) { *flags |= not_gpt; return 0; }
    uint64_t total = (uint64_t)count * entry_size;
    if (total > Q1N1_GPT_SURVEY_ARRAY) { *flags |= Q1N1_GPT_ARRAY_TOO_LARGE; return 0; }
    uint64_t rounded = (total + d->block_size - 1) / d->block_size * d->block_size;
    if (entries < 2 || entries > d->last_block || rounded / d->block_size - 1 > d->last_block - entries) {
        *flags |= Q1N1_GPT_ARRAY_OUT_OF_RANGE; return 0;
    }
    *lba = entries; *bytes = rounded;
    return 1;
}

static void survey_disk(struct efi_boot_services *bs, efi_handle handle, uint64_t index,
                        struct q1n1_gpt_survey *s, struct q1n1_gpt_disk *d, uint8_t *scratch)
{
    struct q1n1_block_io *io = 0;
    const uint8_t *path = 0;
    if (bs->handle_protocol(handle, &block_io_guid, (void **)&io) || !io || !io->media || !io->read_blocks) {
        d->status = EFI_NOT_FOUND;
        return;
    }
    const struct q1n1_block_io_media *m = io->media;
    d->handle_index = index; d->revision = io->revision;
    d->media_id = m->media_id; d->block_size = m->block_size; d->io_align = m->io_align;
    d->last_block = m->last_block; d->removable = m->removable; d->present = m->present;
    d->logical_partition = m->logical_partition; d->read_only = m->read_only;
    if (!bs->handle_protocol(handle, &device_path_guid, (void **)&path) && path)
        d->path_bytes = copy_path(path, d->path, &d->flags);

    efi_status status = read_range(io, s, d, 0, d->block_size, scratch, d->lba0, sizeof(d->lba0));
    if (status) { d->status = status; return; }
    d->flags |= Q1N1_GPT_HAVE_LBA0;

    status = read_range(io, s, d, 1, d->block_size, scratch, d->primary, sizeof(d->primary));
    if (status) { d->status = status; return; }
    d->flags |= Q1N1_GPT_HAVE_PRIMARY;
    uint64_t lba = 0, bytes = 0;
    if (array_extent(d->primary, d, &lba, &bytes, &d->flags, Q1N1_GPT_PRIMARY_NOT_GPT)) {
        d->primary_array_lba = lba; d->primary_array_bytes = bytes;
        status = read_range(io, s, d, lba, bytes, scratch, d->primary_array, sizeof(d->primary_array));
        if (status) { d->status = status; return; }
        d->flags |= Q1N1_GPT_HAVE_PRIMARY_ARRAY;
    }

    /* The backup header is at the last LBA (UEFI 2.x 5.3.2); the primary's
     * AlternateLBA is compared on the host, not trusted for addressing. */
    d->backup_lba = d->last_block;
    status = read_range(io, s, d, d->backup_lba, d->block_size, scratch, d->backup, sizeof(d->backup));
    if (status) { d->status = status; return; }
    d->flags |= Q1N1_GPT_HAVE_BACKUP;
    if (array_extent(d->backup, d, &lba, &bytes, &d->flags, Q1N1_GPT_BACKUP_NOT_GPT)) {
        d->backup_array_lba = lba; d->backup_array_bytes = bytes;
        status = read_range(io, s, d, lba, bytes, scratch, d->backup_array, sizeof(d->backup_array));
        if (status) { d->status = status; return; }
        d->flags |= Q1N1_GPT_HAVE_BACKUP_ARRAY;
    }
}

uint64_t q1n1_gpt_survey(struct efi_boot_services *bs, uint64_t *size)
{
    *size = 0;
    uint64_t address = 0, scratch = 0;
    const uint64_t pages = (sizeof(struct q1n1_gpt_survey) + 4095) / 4096;
    if (bs->allocate_pages(0, EFI_LOADER_DATA, pages, &address) || !address || (address & 4095)) return 0;
    struct q1n1_gpt_survey *s = (void *)(uintptr_t)address;
    fill_zero(s, sizeof(*s));
    s->magic = Q1N1_GPT_SURVEY_MAGIC; s->version = Q1N1_GPT_SURVEY_VERSION;
    s->size = sizeof(*s); s->disk_size = sizeof(s->disks[0]); s->max_disks = Q1N1_GPT_SURVEY_DISKS;
    s->off_lba0 = offsetof(struct q1n1_gpt_disk, lba0);
    s->off_primary = offsetof(struct q1n1_gpt_disk, primary);
    s->off_backup = offsetof(struct q1n1_gpt_disk, backup);
    s->off_primary_array = offsetof(struct q1n1_gpt_disk, primary_array);
    s->off_backup_array = offsetof(struct q1n1_gpt_disk, backup_array);
    s->off_path = offsetof(struct q1n1_gpt_disk, path);
    s->off_scalars = offsetof(struct q1n1_gpt_disk, status);
    s->off_disks = offsetof(struct q1n1_gpt_survey, disks);
    *size = sizeof(*s);

    s->phase = 1;
    efi_handle *handles = 0; uint64_t count = 0;
    efi_status status = bs->locate_handle_buffer(EFI_BY_PROTOCOL, &block_io_guid, 0, &count, &handles);
    if (status || !handles) { s->status = status ? status : EFI_NOT_FOUND; return address; }
    s->handles = count;
    if (count > Q1N1_GPT_SURVEY_HANDLES) count = Q1N1_GPT_SURVEY_HANDLES;

    /* Pass 1: whole, present, fixed disks with sane geometry. NVMe first. */
    s->phase = 2;
    static uint16_t chosen[Q1N1_GPT_SURVEY_DISKS];
    unsigned picked = 0;
    s->examined = count;
    for (int pass = 0; pass < 2; pass++) {
        const int want_nvme = pass == 0;
        for (uint64_t i = 0; i < count; i++) {
            struct q1n1_block_io *io = 0;
            const uint8_t *path = 0;
            uint64_t flags = 0;
            if (bs->handle_protocol(handles[i], &block_io_guid, (void **)&io) || !io || !io->media || !io->read_blocks) {
                if (want_nvme) s->skipped_no_media++;
                continue;
            }
            const struct q1n1_block_io_media *m = io->media;
            int eligible = 0;
            if (!m->present) { if (want_nvme) s->skipped_absent++; }
            else if (m->logical_partition) { if (want_nvme) s->skipped_logical++; }
            else if (m->removable) { if (want_nvme) s->skipped_removable++; }
            else if (!geometry_ok(m)) { if (want_nvme) s->skipped_geometry++; }
            else eligible = 1;
            if (!eligible) continue;
            if (want_nvme) s->candidates++;
            if (!bs->handle_protocol(handles[i], &device_path_guid, (void **)&path) && path)
                copy_path(path, 0, &flags);
            int nvme = (flags & Q1N1_GPT_NVME_PATH) != 0;
            if (nvme != want_nvme || picked >= Q1N1_GPT_SURVEY_DISKS) continue;
            chosen[picked++] = (uint16_t)i;
        }
    }

    s->phase = 3;
    if (picked) {
        const uint64_t scratch_pages = Q1N1_GPT_SURVEY_ARRAY / 4096;
        if (bs->allocate_pages(0, EFI_LOADER_DATA, scratch_pages, &scratch) || !scratch || (scratch & 4095)) {
            s->status = EFI_ERROR(9); /* EFI_OUT_OF_RESOURCES */
        } else {
            for (unsigned n = 0; n < picked; n++)
                survey_disk(bs, handles[chosen[n]], chosen[n], s, &s->disks[n], (uint8_t *)(uintptr_t)scratch);
            s->recorded = picked;
            bs->free_pages(scratch, scratch_pages);
        }
    }
    bs->free_pool(handles);
    if (!s->status) s->phase = 4;
    return address;
}
