/* SPDX-License-Identifier: MIT */
/* Model test for platform/uefi/gpt-survey.c against fake firmware Block I/O.
 *
 *   clang -std=gnu11 -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all \
 *     -Wall -Wextra -Werror -Iplatform/uefi tools/test-gpt-survey.c \
 *     platform/uefi/gpt-survey.c -o /tmp/test-gpt-survey && /tmp/test-gpt-survey
 *
 * With `--fixture FIRST LAST DISK_BYTES BLOCK OUT` it instead models one NVMe
 * disk whose first and last MiB come from files, runs the survey once and
 * writes the raw record to OUT for tools/test-gpt-survey.py to decode. Any read
 * outside those two windows fails the test.
 *
 * Every device's Reset, WriteBlocks and FlushBlocks slot points at a function
 * that aborts, so reaching one is a test failure, not a silent pass. */
#include "gpt-survey.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void poison(void) { fprintf(stderr, "FAIL: a non-read Block I/O slot was called\n"); abort(); }

#define MAX_DEVICES 320
struct device {
    struct q1n1_block_io io;
    struct q1n1_block_io_media media;
    uint8_t *first, *last;      /* images of the first/last window */
    uint64_t window;            /* bytes in each window */
    uint8_t path[300];
    int has_path, fail_lba, fail_protocol;
    uint64_t reads;
};
static struct device devices[MAX_DEVICES];
static unsigned device_count, total_reads, locate_fail, alloc_fail_at, alloc_calls, live_pages_allocs;
static uint64_t read_log[256][2];

static struct device *owner(struct q1n1_block_io *io)
{
    for (unsigned i = 0; i < device_count; i++) if (&devices[i].io == io) return &devices[i];
    abort();
}

static efi_status fake_read(struct q1n1_block_io *io, uint32_t media_id, uint64_t lba, uint64_t bytes, void *buffer)
{
    struct device *d = owner(io);
    uint64_t bs = d->media.block_size;
    assert(media_id == d->media.media_id);
    assert(buffer && bytes && bytes % bs == 0 && bytes <= Q1N1_GPT_SURVEY_ARRAY);
    assert(d->media.io_align <= 1 || (uintptr_t)buffer % d->media.io_align == 0);
    assert(lba <= d->media.last_block && bytes / bs - 1 <= d->media.last_block - lba);
    assert(total_reads < 256);
    read_log[total_reads][0] = lba; read_log[total_reads][1] = bytes;
    total_reads++; d->reads++;
    assert(d->reads <= Q1N1_GPT_SURVEY_READS_PER_DISK);
    if (d->fail_lba >= 0 && (uint64_t)d->fail_lba == lba) return EFI_ERROR(7);
    uint64_t disk = (d->media.last_block + 1) * bs, at = lba * bs;
    if (at + bytes <= d->window) memcpy(buffer, d->first + at, bytes);
    else if (at >= disk - d->window) memcpy(buffer, d->last + (at - (disk - d->window)), bytes);
    else { fprintf(stderr, "FAIL: read outside the GPT windows at LBA %llu\n", (unsigned long long)lba); abort(); }
    return 0;
}

static efi_guid block_guid = {0x964e5b21,0x6459,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid path_guid = {0x09576e91,0x6d3f,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static int same_guid(const efi_guid *a, const efi_guid *b) { return !memcmp(a, b, sizeof(*a)); }

static efi_status handle_protocol(efi_handle h, efi_guid *guid, void **out)
{
    struct device *d = h;
    if (d->fail_protocol) return EFI_UNSUPPORTED;
    if (same_guid(guid, &block_guid)) { *out = &d->io; return 0; }
    if (same_guid(guid, &path_guid)) { if (!d->has_path) return EFI_UNSUPPORTED; *out = d->path; return 0; }
    fprintf(stderr, "FAIL: unexpected protocol %08x\n", guid->a); abort();
}
static efi_status locate(uint32_t type, efi_guid *guid, void *key, uint64_t *count, efi_handle **out)
{
    assert(type == EFI_BY_PROTOCOL && same_guid(guid, &block_guid) && !key);
    if (locate_fail) return EFI_NOT_FOUND;
    efi_handle *h = calloc(device_count ? device_count : 1, sizeof(*h));
    for (unsigned i = 0; i < device_count; i++) h[i] = &devices[i];
    *count = device_count; *out = h; return 0;
}
static efi_status free_pool(void *p) { free(p); return 0; }
static efi_status allocate_pages(uint32_t type, uint32_t kind, uint64_t pages, uint64_t *address)
{
    assert(type == 0 && kind == EFI_LOADER_DATA && pages && pages < 4096);
    if (++alloc_calls == alloc_fail_at) return EFI_ERROR(9);
    void *p = aligned_alloc(4096, pages * 4096);
    memset(p, 0x5a, pages * 4096);   /* the survey must clear what it publishes */
    live_pages_allocs++;
    *address = (uintptr_t)p; return 0;
}
static efi_status free_pages(uint64_t address, uint64_t pages)
{ (void)pages; live_pages_allocs--; free((void *)(uintptr_t)address); return 0; }
static void *forbidden_slot = (void *)poison;

static struct efi_boot_services boot(void)
{
    struct efi_boot_services bs;
    /* Every slot poisoned; only the five the survey is allowed to use are real. */
    void **slot = (void **)&bs;
    for (size_t i = 0; i < sizeof(bs) / sizeof(void *); i++) slot[i] = forbidden_slot;
    bs.allocate_pages = allocate_pages; bs.free_pages = free_pages; bs.free_pool = free_pool;
    bs.handle_protocol = handle_protocol; bs.locate_handle_buffer = locate;
    return bs;
}

/* ---- GPT construction (mirrors UEFI 2.x 5.3) ---- */
static uint32_t crc32(const uint8_t *p, size_t n)
{
    uint32_t c = ~0u;
    while (n--) { c ^= *p++; for (int k = 0; k < 8; k++) c = c >> 1 ^ (0xedb88320u & -(c & 1)); }
    return ~c;
}
static void put32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> 8 * i); }
static void put64(uint8_t *p, uint64_t v) { for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> 8 * i); }

struct layout { uint64_t bs, blocks; uint32_t count, esize; uint64_t array_lba; int nparts; uint64_t parts[8][2]; };
/* Builds a disk image as two windows; returns window size. */
static uint64_t build_gpt(const struct layout *l, uint8_t **first, uint8_t **last)
{
    uint64_t window = 1 << 20;
    uint64_t array_bytes = (uint64_t)l->count * l->esize;
    uint64_t array_blocks = (array_bytes + l->bs - 1) / l->bs;
    *first = calloc(1, window); *last = calloc(1, window);
    uint8_t *array = calloc(1, array_blocks * l->bs + 1);
    for (int i = 0; i < l->nparts; i++) {
        uint8_t *e = array + (size_t)i * l->esize;
        memset(e, 0x11 + i, 16); memset(e + 16, 0x61 + i, 16);
        put64(e + 32, l->parts[i][0]); put64(e + 40, l->parts[i][1]);
        e[56] = 'P'; e[58] = (uint8_t)('0' + i);
    }
    uint64_t last_lba = l->blocks - 1;
    for (int backup = 0; backup < 2; backup++) {
        uint8_t h[92] = {0};
        memcpy(h, "EFI PART", 8); put32(h + 8, 0x10000); put32(h + 12, 92);
        uint64_t mine = backup ? last_lba : 1, other = backup ? 1 : last_lba;
        uint64_t entries = backup ? last_lba - array_blocks : l->array_lba;
        put64(h + 24, mine); put64(h + 32, other);
        put64(h + 40, l->array_lba + array_blocks); put64(h + 48, last_lba - array_blocks - 1);
        memset(h + 56, 0x42, 16); put64(h + 72, entries); put32(h + 80, l->count); put32(h + 84, l->esize);
        put32(h + 88, crc32(array, array_bytes)); put32(h + 16, crc32(h, 92));
        uint64_t hat = mine * l->bs, aat = entries * l->bs, disk = l->blocks * l->bs;
        if (!backup) { memcpy(*first + hat, h, 92); memcpy(*first + aat, array, array_blocks * l->bs); }
        else { memcpy(*last + hat - (disk - window), h, 92); memcpy(*last + aat - (disk - window), array, array_blocks * l->bs); }
    }
    (*first)[510] = 0x55; (*first)[511] = 0xaa; (*first)[450] = 0xee;
    free(array);
    return window;
}

static unsigned add_device(uint64_t bs, uint64_t blocks, int present, int logical, int removable, int nvme)
{
    assert(device_count < MAX_DEVICES);
    unsigned i = device_count++;
    struct device *d = &devices[i];
    memset(d, 0, sizeof(*d));
    d->fail_lba = -1;
    d->io.revision = 0x20001; d->io.media = &d->media;
    d->io.reset_not_used = forbidden_slot; d->io.write_not_used = forbidden_slot; d->io.flush_not_used = forbidden_slot;
    d->io.read_blocks = fake_read;
    d->media.media_id = 7 + i; d->media.block_size = (uint32_t)bs; d->media.io_align = 4;
    d->media.last_block = blocks - 1; d->media.present = (uint8_t)present;
    d->media.logical_partition = (uint8_t)logical; d->media.removable = (uint8_t)removable;
    d->has_path = 1;
    uint8_t *p = d->path; size_t n = 0;
    const uint8_t acpi[12] = {2, 1, 12, 0, 0xd0, 0x41, 0x08, 0x0a, 4, 0, 0, 0};
    memcpy(p + n, acpi, 12); n += 12;
    const uint8_t pci[6] = {1, 1, 6, 0, 0, 0};
    memcpy(p + n, pci, 6); n += 6;
    if (nvme) {
        const uint8_t node[16] = {3, 0x17, 16, 0, 1, 0, 0, 0, 0x00, 0x25, 0x38, 0xf2, 0x61, 0xc3, 0xc2, 0x91};
        memcpy(p + n, node, 16); n += 16;
    } else if (removable) {
        const uint8_t usb[6] = {3, 5, 6, 0, 1, 0};
        memcpy(p + n, usb, 6); n += 6;
    }
    const uint8_t end[4] = {0x7f, 0xff, 4, 0};
    memcpy(p + n, end, 4);
    return i;
}
static void give_gpt(unsigned i, const struct layout *l)
{ devices[i].window = build_gpt(l, &devices[i].first, &devices[i].last); }

static void reset(void)
{
    for (unsigned i = 0; i < device_count; i++) { free(devices[i].first); free(devices[i].last); }
    device_count = 0; total_reads = 0; locate_fail = 0; alloc_fail_at = 0; alloc_calls = 0;
}
static struct q1n1_gpt_survey *run(uint64_t *size)
{
    struct efi_boot_services bs = boot();
    uint64_t address = q1n1_gpt_survey(&bs, size);
    assert(live_pages_allocs == (address ? 1u : 0u));   /* scratch always returned */
    return (void *)(uintptr_t)address;
}
static void release(struct q1n1_gpt_survey *s) { if (s) { live_pages_allocs--; free(s); } }

static const struct layout windows_like = {512, 2000409264, 128, 128, 2, 3,
    {{2048, 4095}, {1730560, 2652159}, {2684928, 1996188302}}};

static void test_selection_and_bounds(void)
{
    reset();
    unsigned other = add_device(512, 1 << 20, 1, 0, 0, 0);         /* non-NVMe fixed disk, first */
    struct layout small = {512, 1 << 20, 128, 128, 2, 1, {{2048, 4095}}};
    give_gpt(other, &small);
    for (int i = 0; i < 60; i++) add_device(512, 1 << 20, 1, 1, 0, 1); /* partitions: skipped */
    add_device(512, 1 << 22, 1, 0, 1, 0);                          /* USB stick: skipped */
    add_device(512, 1 << 20, 0, 0, 0, 1);                          /* no media: skipped */
    add_device(520, 1 << 20, 1, 0, 0, 1);                          /* bad block size */
    unsigned odd = add_device(512, 1 << 20, 1, 0, 0, 1); devices[odd].media.io_align = 8192;
    unsigned broken = add_device(512, 1 << 20, 1, 0, 0, 1); devices[broken].fail_protocol = 1;
    unsigned nvme = add_device(512, 2000409264, 1, 0, 0, 1);
    give_gpt(nvme, &windows_like);
    uint64_t size;
    struct q1n1_gpt_survey *s = run(&size);
    assert(s && size == sizeof(*s) && s->magic == Q1N1_GPT_SURVEY_MAGIC && s->phase == 4 && !s->status);
    assert(s->handles == device_count && s->recorded == 2 && s->candidates == 2);
    assert(s->skipped_logical == 60 && s->skipped_removable == 1 && s->skipped_absent == 1);
    assert(s->skipped_geometry == 2 && s->skipped_no_media == 1);
    const struct q1n1_gpt_disk *d = &s->disks[0];
    assert(d->handle_index == nvme && (d->flags & Q1N1_GPT_NVME_PATH));       /* NVMe first */
    assert(d->flags == (Q1N1_GPT_HAVE_LBA0 | Q1N1_GPT_HAVE_PRIMARY | Q1N1_GPT_HAVE_PRIMARY_ARRAY |
                        Q1N1_GPT_HAVE_BACKUP | Q1N1_GPT_HAVE_BACKUP_ARRAY | Q1N1_GPT_NVME_PATH));
    assert(d->reads == 5 && d->primary_array_lba == 2 && d->primary_array_bytes == 16384);
    assert(d->backup_lba == 2000409263 && d->backup_array_lba == 2000409231);
    assert(d->last_block == 2000409263 && d->block_size == 512 && !memcmp(d->primary, "EFI PART", 8));
    assert(d->path_bytes == 12 + 6 + 16 + 4 && d->path[18] == 3 && d->path[19] == 0x17);
    assert(s->disks[1].handle_index == other && !(s->disks[1].flags & Q1N1_GPT_NVME_PATH));
    assert(s->reads == 10 && total_reads == 10);
    for (unsigned i = 0; i < total_reads; i++) assert(read_log[i][1] <= 16384);
    for (unsigned n = 2; n < Q1N1_GPT_SURVEY_DISKS; n++) {
        const uint8_t *raw = (const uint8_t *)&s->disks[n];
        for (size_t k = 0; k < sizeof(s->disks[n]); k++) assert(!raw[k]);   /* no stale bytes */
    }
    release(s);
}

static void test_hostile_headers(void)
{
    struct { uint32_t count, esize; uint64_t array_lba; uint32_t header_size; uint64_t flag; } cases[] = {
        {0xffffffffu, 128, 2, 92, Q1N1_GPT_ARRAY_TOO_LARGE},
        {1024, 128, 2, 92, Q1N1_GPT_ARRAY_TOO_LARGE},            /* 128 KiB > 64 KiB */
        {128, 128, UINT64_C(1) << 40, 92, Q1N1_GPT_ARRAY_OUT_OF_RANGE},
        {128, 128, 1, 92, Q1N1_GPT_ARRAY_OUT_OF_RANGE},
        {128, 96, 2, 92, Q1N1_GPT_PRIMARY_NOT_GPT},
        {128, 128, 2, 2000, Q1N1_GPT_PRIMARY_NOT_GPT},
        {0, 128, 2, 92, Q1N1_GPT_PRIMARY_NOT_GPT},
    };
    for (unsigned c = 0; c < sizeof(cases) / sizeof(cases[0]); c++) {
        reset();
        unsigned i = add_device(512, 1 << 20, 1, 0, 0, 1);
        struct layout l = {512, 1 << 20, 128, 128, 2, 1, {{2048, 4095}}};
        give_gpt(i, &l);
        uint8_t *h = devices[i].first + 512;
        put32(h + 80, cases[c].count); put32(h + 84, cases[c].esize);
        put64(h + 72, cases[c].array_lba); put32(h + 12, cases[c].header_size);
        uint64_t size;
        struct q1n1_gpt_survey *s = run(&size);
        const struct q1n1_gpt_disk *d = &s->disks[0];
        assert(s->recorded == 1 && (d->flags & cases[c].flag) && !(d->flags & Q1N1_GPT_HAVE_PRIMARY_ARRAY));
        assert(d->reads == 4 && (d->flags & Q1N1_GPT_HAVE_BACKUP_ARRAY));   /* backup still read, bounded */
        release(s);
    }
}

static void test_4k_blocks_and_failures(void)
{
    reset();
    unsigned i = add_device(4096, 250051158, 1, 0, 0, 1);
    struct layout l = {4096, 250051158, 128, 128, 2, 2, {{256, 511}, {512, 250000000}}};
    give_gpt(i, &l);
    uint64_t size;
    struct q1n1_gpt_survey *s = run(&size);
    assert(s->disks[0].primary_array_bytes == 16384 && s->disks[0].reads == 5);
    assert(s->disks[0].backup_array_lba == 250051157 - 4);
    release(s);

    reset();
    i = add_device(512, 2000409264, 1, 0, 0, 1);
    give_gpt(i, &windows_like);
    devices[i].fail_lba = 2;                                   /* primary array read fails */
    s = run(&size);
    assert(s->disks[0].status == EFI_ERROR(7) && s->disks[0].reads == 3 && total_reads == 3);
    assert(!(s->disks[0].flags & Q1N1_GPT_HAVE_BACKUP));        /* stops at the first failure */
    release(s);

    reset();
    for (int n = 0; n < 6; n++) { unsigned k = add_device(512, 2000409264, 1, 0, 0, 1); give_gpt(k, &windows_like); }
    s = run(&size);
    assert(s->candidates == 6 && s->recorded == 4 && total_reads == 20);
    release(s);

    reset();
    i = add_device(512, 2000409264, 1, 0, 0, 1); give_gpt(i, &windows_like);
    memset(devices[i].path, 3, sizeof(devices[i].path));      /* no end node, 3-byte lengths */
    devices[i].path[2] = 200; devices[i].path[3] = 0;
    s = run(&size);
    assert((s->disks[0].flags & Q1N1_GPT_PATH_TRUNCATED) && s->disks[0].path_bytes <= Q1N1_GPT_SURVEY_PATH);
    release(s);

    reset();
    i = add_device(512, 2000409264, 1, 0, 0, 1); give_gpt(i, &windows_like);
    alloc_fail_at = 1;                                           /* no record at all */
    s = run(&size);
    assert(!s && !size && !total_reads);
    alloc_fail_at = 2; alloc_calls = 0;                          /* record, but no scratch */
    s = run(&size);
    assert(s && s->status == EFI_ERROR(9) && !s->recorded && !total_reads);
    release(s);

    reset();                                                     /* handle scan is bounded */
    for (int n = 0; n < 300; n++) add_device(512, 1 << 20, 1, 1, 0, 1);
    i = add_device(512, 2000409264, 1, 0, 0, 1); give_gpt(i, &windows_like);
    s = run(&size);
    assert(s->handles == 301 && s->examined == Q1N1_GPT_SURVEY_HANDLES && !s->recorded && !total_reads);
    release(s);

    reset();
    locate_fail = 1;
    s = run(&size);
    assert(s && s->status == EFI_NOT_FOUND && s->phase == 1 && !total_reads);
    release(s);
}

static uint8_t *load(const char *path, size_t want)
{
    FILE *f = fopen(path, "rb"); assert(f);
    uint8_t *p = malloc(want);
    assert(fread(p, 1, want, f) == want); fclose(f);
    return p;
}

int main(int argc, char **argv)
{
    if (argc == 7 && !strcmp(argv[1], "--fixture")) {
        uint64_t disk = strtoull(argv[4], 0, 0), bs = strtoull(argv[5], 0, 0);
        unsigned i = add_device(bs, disk / bs, 1, 0, 0, 1);
        devices[i].window = 1 << 20;
        devices[i].first = load(argv[2], 1 << 20); devices[i].last = load(argv[3], 1 << 20);
        uint64_t size;
        struct q1n1_gpt_survey *s = run(&size);
        FILE *f = fopen(argv[6], "wb"); assert(f);
        assert(fwrite(s, 1, size, f) == size); fclose(f);
        printf("fixture record %llu bytes, %llu reads\n", (unsigned long long)size, (unsigned long long)s->reads);
        release(s); reset();
        return 0;
    }
    test_selection_and_bounds();
    test_hostile_headers();
    test_4k_blocks_and_failures();
    reset();
    printf("gpt-survey model: all checks passed (record %zu bytes, disk %zu bytes)\n",
           sizeof(struct q1n1_gpt_survey), sizeof(struct q1n1_gpt_disk));
    return 0;
}
