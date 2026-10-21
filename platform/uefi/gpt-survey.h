/* SPDX-License-Identifier: MIT */
/* Read-only GPT survey of whole-disk firmware Block I/O devices.
 *
 * Runs before ExitBootServices, next to the SSD preload, on the same firmware
 * storage stack the preload reads SNAPBOOT through. It copies the raw GPT
 * blocks (LBA 0, both headers, both entry arrays) and each disk's device path
 * into one EfiLoaderData record that the host reads over the proxy after
 * ExitBootServices (bootinfo gpt_survey / gpt_survey_size). All decoding and
 * CRC checking happens on the host (tools/gpt_survey.py).
 *
 * Read-only by construction: the Block I/O protocol below gives only
 * ReadBlocks a callable type. Reset, WriteBlocks and FlushBlocks are opaque
 * `const void *` slots; calling one would need a cast of a *_not_used field,
 * and tools/test-gpt-survey.py rejects any reference to those fields, any
 * write-capable protocol GUID in the object, and any Boot Services slot other
 * than the five this file needs. The model test poisons the write, flush and
 * reset slots and fails if one is ever reached.
 *
 * Bounded: at most Q1N1_GPT_SURVEY_HANDLES handles examined, at most
 * Q1N1_GPT_SURVEY_DISKS disks recorded, at most five ReadBlocks calls per
 * recorded disk, each at most Q1N1_GPT_SURVEY_ARRAY bytes. */
#pragma once
#include "efi.h"

#define Q1N1_GPT_SURVEY_MAGIC UINT64_C(0x31545047314e3151) /* "Q1N1GPT1" */
#define Q1N1_GPT_SURVEY_VERSION 1
#define Q1N1_GPT_SURVEY_DISKS 4
#define Q1N1_GPT_SURVEY_HANDLES 256
#define Q1N1_GPT_SURVEY_BLOCK 4096   /* largest block size recorded */
#define Q1N1_GPT_SURVEY_ARRAY 65536  /* largest entry array recorded */
#define Q1N1_GPT_SURVEY_PATH 256     /* device path bytes kept */
#define Q1N1_GPT_SURVEY_READS_PER_DISK 5

/* UEFI 2.x EFI_BLOCK_IO_MEDIA, revision 1 fields only. */
struct q1n1_block_io_media {
    uint32_t media_id;
    uint8_t removable, present, logical_partition, read_only, write_caching;
    uint8_t pad[3];
    uint32_t block_size, io_align;
    uint64_t last_block;
};
/* UEFI 2.x EFI_BLOCK_IO_PROTOCOL. Only read_blocks is callable here. */
struct q1n1_block_io {
    uint64_t revision;
    struct q1n1_block_io_media *media;
    const void *reset_not_used;
    efi_status (*read_blocks)(struct q1n1_block_io *, uint32_t media_id, uint64_t lba,
                              uint64_t bytes, void *buffer);
    const void *write_not_used;
    const void *flush_not_used;
};
_Static_assert(offsetof(struct q1n1_block_io, media) == 8, "Block I/O Media");
_Static_assert(offsetof(struct q1n1_block_io, read_blocks) == 24, "Block I/O ReadBlocks");
_Static_assert(offsetof(struct q1n1_block_io, write_not_used) == 32, "Block I/O WriteBlocks");
_Static_assert(offsetof(struct q1n1_block_io_media, block_size) == 12, "Media BlockSize");
_Static_assert(offsetof(struct q1n1_block_io_media, io_align) == 16, "Media IoAlign");
_Static_assert(offsetof(struct q1n1_block_io_media, last_block) == 24, "Media LastBlock");

/* flags */
#define Q1N1_GPT_HAVE_LBA0            (1u << 0)
#define Q1N1_GPT_HAVE_PRIMARY         (1u << 1)
#define Q1N1_GPT_HAVE_PRIMARY_ARRAY   (1u << 2)
#define Q1N1_GPT_HAVE_BACKUP          (1u << 3)
#define Q1N1_GPT_HAVE_BACKUP_ARRAY    (1u << 4)
#define Q1N1_GPT_ARRAY_TOO_LARGE      (1u << 5)
#define Q1N1_GPT_PATH_TRUNCATED       (1u << 6)
#define Q1N1_GPT_NVME_PATH            (1u << 7)
#define Q1N1_GPT_PRIMARY_NOT_GPT      (1u << 8)
#define Q1N1_GPT_BACKUP_NOT_GPT       (1u << 9)
#define Q1N1_GPT_ARRAY_OUT_OF_RANGE   (1u << 10)

/* One recorded disk. Raw blocks first (8-byte aligned), then scalars. The
 * host checks every offset against the layout words in the survey header. */
struct q1n1_gpt_disk {
    uint8_t lba0[Q1N1_GPT_SURVEY_BLOCK];
    uint8_t primary[Q1N1_GPT_SURVEY_BLOCK];
    uint8_t backup[Q1N1_GPT_SURVEY_BLOCK];
    uint8_t primary_array[Q1N1_GPT_SURVEY_ARRAY];
    uint8_t backup_array[Q1N1_GPT_SURVEY_ARRAY];
    uint8_t path[Q1N1_GPT_SURVEY_PATH];
    uint64_t status, flags, handle_index, revision;
    uint64_t media_id, block_size, io_align, last_block;
    uint64_t removable, present, logical_partition, read_only;
    uint64_t reads, path_bytes;
    uint64_t primary_array_lba, primary_array_bytes;
    uint64_t backup_lba, backup_array_lba, backup_array_bytes;
};
/* Phases: 1 locate, 2 examine, 3 read, 4 complete. */
struct q1n1_gpt_survey {
    uint64_t magic, version, size, disk_size, max_disks;
    /* Layout words: offsets inside struct q1n1_gpt_disk, and first disk. */
    uint64_t off_lba0, off_primary, off_backup, off_primary_array, off_backup_array;
    uint64_t off_path, off_scalars, off_disks;
    uint64_t status, phase, handles, examined, candidates, recorded, reads;
    uint64_t skipped_absent, skipped_logical, skipped_removable, skipped_geometry;
    uint64_t skipped_no_media;
    struct q1n1_gpt_disk disks[Q1N1_GPT_SURVEY_DISKS];
};

/* Returns the record (EfiLoaderData, zero-initialized, published even when a
 * disk read fails) or 0 when no record could be allocated. Never writes
 * storage, never connects or disconnects controllers. */
uint64_t q1n1_gpt_survey(struct efi_boot_services *bs, uint64_t *size);
