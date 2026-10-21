# A16 NVMe from q1n1, read-only (full-install M3)

**Finding (A16, 2026-09-25):** after ExitBootServices, q1n1 can take the internal Samsung SSD over
from the host through the proxy. It reads the disk through its own queues, and gets back exactly
the GPT that firmware showed before ExitBootServices. The tool is `tools/nvme_m3.py`. It is
read-only by construction: only Identify, Get Features, Get Log Page, Number of Queues and I/O
queue create/delete can be built on the admin queue, and only Read on the I/O queue. Anything else
is refused before a byte reaches a queue or a doorbell.

```sh
python3 tools/nvme_m3.py --device udp://en5 --survey build/full-install/survey-20260925.json \
    --json build/full-install/nvme-m3-<date>.json [--timed-mib 64]
```

This is the host-driven reference sequence. Serving a running macOS guest (full-install M6) needs
the same sequence in EL2 code, and this is the version that code will be ported from.

## What the SSD and its path look like after ExitBootServices

Read-only probes before any change: `tools/nvme_h3h4_probe.py` → `build/full-install/h3h4-20260925.json`
and `tools/nvme_s2walk.py` → `build/full-install/nvme-s2walk-20260925.json`.

- **Where it is:** PCIe segment 5 (the survey's `Acpi(PNP0A08, 5)`), ECAM 0x7A0000000 from
  MCFG. Root port 0:0.0 is 17cb:0120, secondary bus 1, link Gen4 x4 with the data link active.
  The endpoint is 1:0.0, 144d:a80d, class 010802. Its 64-bit BAR0 is 0x7A000000 (the CPU address
  equals the bus address), and Memory Space and Bus Master are both already on.
- **Controller state firmware leaves behind:** enabled (CC 0x460001, CSTS 0x1). The admin queues
  are at 0xc6413000/0xc6414000 in firmware memory. CAP: MQES 16384, DSTRD 0, TO 20 s, MPSMIN
  4 KiB, NVM command set.
- **DMA goes through the PCIe SMMUv3** at 0x15480000. It is enabled with GBPA abort and a
  two-level stream table at 0xcb050000. The SSD's StreamID 0x50100 (segment << 16 | RID) has a
  valid STE with Config 6: stage 1 bypassed, stage 2 translating (4 KiB granule, 40-bit IPA,
  start level 1, S2TTB 0x85396000, VMID 3, stall and record faults on).
  - Firmware's stage-2 table is an identity map over most of DRAM: 321 L1 slots valid, 316 of
    them fully block-mapped.
  - It does **not** map q1n1's own image (0xb0000000 at the time) or heap (0xb83d8000). Anything
    the SSD must read or write therefore lives outside q1n1's regions.
  - The tool checks every page of its DMA window against this table (identity, S2AP read/write)
    before the controller is told about any of it. It writes nothing to the SMMU.

## What the tool does

1. Arms the SError strict guard.
2. Finds and pins the SSD through ACPI. It checks the vendor and class, that BAR0 sits inside the
   root port's window, and that Memory Space and Bus Master are on. It never changes config space.
3. Finds the DMA window: the highest 4 MiB, 2 MiB-aligned window that is Conventional in the final
   EFI map, clear of every region q1n1 publishes, and identity-mapped read/write by the stage-2
   table. On this boot that was 0x8b90c00000, at the top of the 0x8800000000..0x8b91000000
   region, found on the first try. The window is zeroed and cleaned to the point of coherency.
4. Takes the controller over:
   - a normal shutdown (CC.SHN, SHST complete: CSTS 0x9);
   - CC.EN 0 (RDY 0);
   - its own admin queue (32 entries);
   - CC.EN 1 with 64-byte SQ and 16-byte CQ entries and 4 KiB pages (CSTS 0x1).
5. Identify controller and namespace 1, pinning EUI64 002538F261C3C291 and the size against the
   survey. Then APST, SMART, and one I/O queue pair (64 entries, polled, no interrupts).
6. Reads LBA 0-33, the backup header, and the backup array. `gpt_survey.decode_gpt` decodes
   them, and the result is compared field by field with the survey's pinned disk.
7. Optionally times sequential reads from the largest partition, eight 256 KiB commands in flight.
8. Deletes the I/O queues, then does a normal shutdown and CC.EN 0.

Cache maintenance: SQ entries and PRP lists are cleaned (DC CIVAC) before a doorbell. CQ entries
and data are cleaned and invalidated before they are read. The CPU writes nothing the device writes
after the window is zeroed, so no dirty line can overwrite a completion.

## Evidence: `build/full-install/nvme-m3-20260925.json` (exit 0, 2,620 proxy operations, 0 SErrors)

- **GPT:** equal to `survey-20260925.json`, all 18 partitions, no errors. The GPT read took 19 ms.
- **Controller:** SAMSUNG MZVL81T0HFLB-00BTW, serial <serial>, firmware KXF72W1Q, NVMe 2.0,
  NN 1.
  - MDTS 7, so 512 KiB per command.
  - ONCS 0xdf; VWC 0x7 (volatile write cache present); APSTA 1, but APST is off after our
    reset.
  - RTD3E 10 s.
  - Five power states; PS3 and PS4 are non-operational.
- **Namespace 1:** 2,000,409,264 LBAs of 512 bytes. A 4 KiB format exists and is never switched
  to, since that would be a Format. NUSE is 374,637,144.
  - **DLFEAT 0x1: deallocated blocks read as zeros.** So full-install M5 can deallocate the
    window once and copy only the allocated extents, and holes still read back as zeros (H5).
- **SMART:** no critical warning, 42 °C, spare 100 %, 0 % used, 0 media errors, 0 error-log
  entries, 101 power-on hours, 184 power cycles, **80 unsafe shutdowns**.
- **Rates (H7):**
  - 82.0 MB/s host-driven: 64 MiB with eight commands in flight. The limit is the proxy round
    trips, not the SSD.
  - 19.5 MB/s end to end when the data also crosses NCM (16 MiB).

## What this means for writes (M5 and later)

- **80 unsafe shutdowns in 184 power cycles.** Every reset from q1n1 cuts the SSD's power with
  no shutdown notification. That is harmless while q1n1 only reads, but the SSD has a volatile
  write cache. So any session that writes must Flush and do a normal shutdown (CC.SHN) before
  anything resets the A16. The tool already ends with a normal shutdown.
- **Throughput:** with host-driven submission, NCM sets the pace of a copy, not the SSD. At
  about 20–29 MB/s, M5's 20–25 GB of allocated extents takes 15–20 minutes.

## Not checked

H6, Windows booting normally after q1n1 has reset the controller, was not run: the operator skipped it.
Windows reinitialises the controller from reset on every boot, and this session only read.
