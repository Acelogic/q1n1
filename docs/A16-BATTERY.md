# Battery and charging status, 2026-09-24

**Status:** implemented and host-tested. **Not yet run on the A16:** the
boot-window payload has to be installed on the ESP first.

## Where the numbers come from

The A16's own DSDT (read live through q1n1, `build/a16-acpi/DSDT.dsl`)
describes the battery as `\_SB.CMBD` (`PNP0C0A`, control-method battery) and
the charger as `\_SB.CMPS` (`ACPI0003`). Both read everything from `\_SB.PMGK`,
the PMIC GLINK GenericSerialBus region. The field offsets there are register
identifiers on the same PmicGlinkDxe transport the boot window already uses
for UCSI. UCSI VERSION `0x20100` appears in the same field list.

| ACPI | Register | Bytes | Content |
|---|---|---|---|
| `_BST` (`BSTD`) | `0x101A8` | 16 | state (bit0 discharging, bit1 charging, bit2 critical, bit3 limited), rate, remaining capacity, voltage |
| `_BIX` (`BIXD`) | `0x10110` | 68 | revision, power unit (0 mW/mWh, 1 mA/mAh), design capacity, last full charge, technology, design voltage, ..., cycle count (dword 8) |
| `_PSR` (`PSRD`) | `0x101D8` | 4 | 1 on AC |

The replies are the ACPI package dwords. Percentage is remaining ÷ last full.
The time estimate is capacity ÷ rate.

## Use

In the boot window, send `battery` (or run `python3 tools/a16ctl.py battery`).
The answer also appears on the panel:

```text
BATTERY 73% charging on AC, 45000 mW in, ~0h25 to full, 51100/70000 mWh, 17120 mV, 12 cycles raw_state=02 unit=0
```

`BATTERY?` means the values arrived but failed the plausibility bounds. `ERR`
carries the transport status.

## Safety

`q1n1_pmgk_read()` (`platform/uefi/ucsi-console.c`) allows exactly these three
registers at exactly these sizes, and only reads. For each sample it checks:
- guard bytes on both sides of the caller's buffer;
- the ready flag;
- the firmware's cached remote status.

Each register is read twice, and the two samples must agree. For `_BST`, rate
and voltage may move between the samples; state and remaining capacity may
not. A reply of 8 bytes or more that equals the prefix of the previous reply
is refused as a stale cache copy: the known failure mode of this firmware
cache, see `A16-UCSI-REGISTERS.md`. The decoder (`platform/uefi/battery.c`)
rejects values no laptop pack produces.

## Limit: boot window only (for this path)

PmicGlinkDxe is a UEFI driver: it depends on boot services. Once q1n1 calls
ExitBootServices, this path is gone. The EL2 proxy reads the same registers
over q1n1's own GLINK channel to the SoCCP (`PMIC_RTR_SOCCP_APPS`; this chip's
power coprocessor is SoCCP, not the ADSP of earlier Snapdragon laptops): see
`A16-LIVE-BATTERY.md`. `a16ctl battery` picks whichever is running.

## Checks

```shell
clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror -Wno-unused-function \
  -Iplatform/uefi tools/test-q1n1-battery.c -o build/test-q1n1-battery && build/test-q1n1-battery
```

The test runs 36 checks:
- decoding and formatting (charging, discharging, mAh, critical, unknown
  rate/voltage, truncation);
- the allow list;
- guard overrun, remote error, link down, failed read, no copy;
- samples that disagree, a stale 16-byte copy, and a coincidental 4-byte value
  that is not treated as stale.

`make uefi` builds with `-Werror`. `tools/test-uefi.py --proxy` still passes
39 checks in QEMU; QEMU has no PMIC GLINK, so it does not exercise the read.
