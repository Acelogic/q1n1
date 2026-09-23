# SSD assets before ExitBootServices

q1n1 can load selected files through firmware before entering the EL2 proxy.
This lets a host reuse a local SSD image instead of transferring the same
large RAM disk over USB on every boot attempt. It does not provide a disk
driver after ExitBootServices or expose an APFS block device to XNU.

## Manifest and reservation

The EFI payload first asks existing Block I/O handles to connect their
partition/filesystem children with recursive `ConnectController`. It then
searches all exposed Simple File System volumes for
`\Snapintosh\q1n1-preload.bin`. The matching volume also supplies the files;
drive letters, shell `fsN:` mappings, and a specific guest profile are not
compiled into the loader. More than one manifest-bearing volume is refused.
All file access is read-only through the [UEFI File Protocol](https://uefi.org/specs/UEFI/2.11/13_Protocols_Media_Access.html).

Create a manifest from the exact local image that was staged on FAT:

```sh
python3 tools/preload.py \
  --file '\Snapintosh\j700ap-26A428\basesystem-26A428.apfs' \
         ../Snapintosh/downloads/extracted/j700ap/basesystem-26A428.apfs \
  --output build/q1n1-preload.bin
```

Copy the manifest to `Snapintosh/q1n1-preload.bin` on that volume. It contains
no host paths: a versioned header followed by up to 16 records with file size,
SHA256, and a canonical absolute EFI path. Total content is bounded to 4 GiB.
The loader rejects invalid paths, duplicates, size/EOF/hash mismatches, and
allocation/read failures. A failed load releases every cache allocation and
leaves the ordinary host-upload proxy available. Absence is optional.

The table and data use EFI LoaderData allocations made before the final memory
map capture. The host accepts them only inside those reservations and rejects
overlap. `q1n1_bootinfo` appends table address/size, EFI status, and a callable
resident verifier. Older bootinfo readers remain compatible through its size
field. A new chainloaded stage preserves the cache and publishes its own
verifier address, avoiding dependence on a previous stage's code slot.

Bootinfo also publishes optional diagnostics: storage handles and successful
connections, discovered filesystems and opened roots, matching manifests,
asset index, bytes read/expected, and the exact EFI status. Phases identify
storage connection (1), manifest discovery (2), manifest reading (3), asset
open/info (4), asset reading (5), hash verification (6), and completion (7).
This distinguishes a missing manifest from an image-open failure, even when
both return `EFI_NOT_FOUND`. Diagnostics survive stage replacement too.

## Snapintosh integration

The existing profile checks still bind the actual Apple kernel and all selected
firmware. The host builds its normal RAMDisk layout and matches the complete
local image's SHA256 and length to an advertised cache entry. The resident
verifier hashes all cached bytes again before any copy. The host copies in
bounded requests into conventional guest memory and omits that upload chunk.
The cached source remains outside the guest and survives subsequent attempts.
An explicit guest placement overlapping a cache allocation is rejected too.

`SNAP_REQUIRE_PRELOAD=1` refuses to boot unless the matching RAMDisk cache is
available; use it for qualification so a USB fallback cannot masquerade as SSD
loading. `SNAP_NO_PRELOAD=1` exercises the original upload path. This first
integration reuses the raw RAMDisk; transformed kernel segments, tables, and
handoff structures are still uploaded by the host.

## Validation and limits

- `python3 tools/test-preload.py`: ASan/UBSan coverage of SHA256 known vectors
  and streaming, lazy filesystem connection, partial reads, EOF/size/hash errors, duplicate manifests and
  paths, allocation failures/cleanup, plus host reservation/content guards.
- `python3 tools/test-uefi.py --proxy --preload`: real EFI file loading in
  QEMU, byte readback after ExitBootServices, corruption rejection, host local
  copy, and cache/verifier survival across two stage replacements.
- `python3 tools/test-uefi.py --proxy`: absent-manifest compatibility path.
- Existing proxy, xHCI/NCM, NCM proxy, and USB-port lifecycle suites.

On 2026-09-21 the A16 loaded a 1862270976-byte BaseSystem from its SSD and
passed a fresh full-cache target hash in 9.40 seconds. The cache-required
Snapintosh run verified/copied it in 10.6 seconds and completed its timed
preparation/upload phase in 55.1 seconds, versus 506.0 seconds previously.
It reached the existing native APFS root-hash panic; the independent runtime
audit passed. This boot required a direct-cable replug after restart, so it
does not qualify automatic USB re-enumeration. Full evidence is recorded in
the companion Snapintosh `docs/EFI-SSD-PRELOAD-20260921.md`.

The implementation does not establish native APFS mount, launchd,
WindowServer, or responsive input, and the timing excludes EFI cold-boot
loading and image construction on the host.
