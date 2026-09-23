/* SPDX-License-Identifier: MIT */
#pragma once
#include "efi.h"
#define Q1N1_PRELOAD_MAGIC UINT64_C(0x31444c50314e3151) /* Q1N1PLD1 */
#define Q1N1_CACHE_MAGIC UINT64_C(0x31484343314e3151) /* Q1N1CCH1 */
#define Q1N1_PRELOAD_MAX 16
#define Q1N1_PRELOAD_LIMIT UINT64_C(0x100000000)
#define Q1N1_PRELOAD_PATH "\\Snapintosh\\q1n1-preload.bin"
struct q1n1_preload_file { uint64_t size; uint8_t sha256[32]; char path[256]; };
struct q1n1_preload_manifest {
    uint64_t magic, version, count, entry_size;
    struct q1n1_preload_file files[Q1N1_PRELOAD_MAX];
};
struct q1n1_cached_file { uint64_t base, allocation; struct q1n1_preload_file file; };
struct q1n1_preload_cache {
    uint64_t magic, version, count, entry_size;
    struct q1n1_cached_file files[Q1N1_PRELOAD_MAX];
};
/* Retained even when no cache can be published. Phases: 1 storage connect,
 * 2 manifest discovery, 3 manifest read, 4 asset open/info, 5 read, 6 hash,
 * 7 completed. Status is the exact EFI result, not a generic "missing". */
struct q1n1_preload_diagnostics {
    uint64_t version, size, phase, status;
    uint64_t block_devices, connect_successes, filesystems, roots, manifests;
    uint64_t asset_index, read_bytes, expected_bytes;
};
extern struct q1n1_preload_diagnostics q1n1_preload_diagnostics;
_Static_assert(sizeof(struct q1n1_preload_file)==296,"preload file ABI");
_Static_assert(sizeof(struct q1n1_cached_file)==312,"cached file ABI");
/* Missing manifest is EFI_NOT_FOUND. Every other error leaves no cache. */
efi_status q1n1_preload(struct efi_boot_services *bs, uint64_t *address, uint64_t *size);
int q1n1_preload_manifest_valid(const struct q1n1_preload_manifest *m, uint64_t size);
int q1n1_preload_verify(const struct q1n1_preload_cache *cache, uint64_t index);
