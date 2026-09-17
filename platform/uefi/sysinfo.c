/* SPDX-License-Identifier: MIT */
/* The boot-log detail m1n1 prints on a Mac, in the A16's terms. Everything
 * here comes from tables the firmware already left in memory, so it costs no
 * drivers: SMBIOS for the machine identity (m1n1's board/chip id), ACPI for
 * the interrupt, timer, IOMMU and PCIe layout (m1n1's aic/wdt/dart/pcie lines),
 * the UEFI memory map for the RAM summary, and ID registers for the CPU.
 * Every walk is bounded and length-checked; the proxy's exception guard is a
 * backstop, not the plan. */
#include "sysinfo.h"
#include "console.h"

#define ACPI_HEADER 36

static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static int match(const uint8_t *p, const char *text, unsigned n)
{
    for (unsigned k = 0; k < n; k++) if (p[k] != (uint8_t)text[k]) return 0;
    return 1;
}
/* Print at most `limit` printable characters; firmware strings are untrusted. */
static void put_text(const uint8_t *text, unsigned limit)
{
    char buffer[65];
    unsigned n = 0;
    while (n < limit && n < sizeof(buffer) - 1 && text[n] && text[n] != 0xff) {
        buffer[n] = (text[n] >= 32 && text[n] < 127) ? (char)text[n] : '.';
        n++;
    }
    while (n && buffer[n - 1] == ' ') n--; /* firmware pads with spaces */
    buffer[n] = 0;
    con_puts(buffer);
}

void sysinfo_cpu(uint64_t timer_hz, uint64_t cores)
{
    uint64_t midr, ctr, pfr0, mmfr0, mpidr;
    __asm__ volatile("mrs %0, midr_el1" : "=r"(midr));
    __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    __asm__ volatile("mrs %0, id_aa64pfr0_el1" : "=r"(pfr0));
    __asm__ volatile("mrs %0, id_aa64mmfr0_el1" : "=r"(mmfr0));
    __asm__ volatile("mrs %0, mpidr_el1" : "=r"(mpidr));
    unsigned implementer = (unsigned)(midr >> 24) & 0xff;
    const char *vendor = implementer == 0x51 ? "Qualcomm" : implementer == 0x41 ? "ARM"
                       : implementer == 0x61 ? "Apple" : "unknown";
    con_puts("cpu: midr "); con_hexn(midr, 8); con_puts(" ("); con_puts(vendor);
    con_puts(" part "); con_hexn((midr >> 4) & 0xfff, 3);
    con_puts(" r"); con_dec((midr >> 20) & 0xf); con_puts("p"); con_dec(midr & 0xf);
    con_puts("), mpidr "); con_hexn(mpidr, 8);
    if (cores) { con_puts(", "); con_dec(cores); con_puts(" cores"); }
    con_puts("\n");
    static const unsigned bits[] = {32, 36, 40, 42, 44, 48, 52, 56};
    con_puts("cpu: cntfrq "); con_dec(timer_hz / 1000000); con_puts(" MHz");
    con_puts(", pa "); con_dec(bits[mmfr0 & 7]); con_puts(" bit");
    con_puts(", icache line "); con_dec(4u << (ctr & 15));
    con_puts(" B, dcache line "); con_dec(4u << ((ctr >> 16) & 15)); con_puts(" B\n");
    con_puts("cpu: el3 "); con_puts(((pfr0 >> 12) & 15) ? "yes" : "no");
    con_puts(", el2 "); con_puts(((pfr0 >> 8) & 15) ? "yes" : "no");
    con_puts(", fp "); con_puts(((pfr0 >> 16) & 15) == 15 ? "no" : "yes");
    con_puts(", simd "); con_puts(((pfr0 >> 20) & 15) == 15 ? "no" : "yes");
    con_puts(", sve "); con_puts(((pfr0 >> 32) & 15) ? "yes" : "no");
    con_puts(", gic sysreg v"); con_dec((pfr0 >> 24) & 15); con_puts("\n");
}

/* SMBIOS 3 entry point -> type 0 (BIOS) and type 1 (system): the closest thing
 * this machine has to m1n1's "Devicetree compatible value: apple,j313". */
static const uint8_t *smbios_string(const uint8_t *structure, unsigned index)
{
    if (!index) return 0;
    const uint8_t *cursor = structure + structure[1];
    for (unsigned n = 1; n < 32; n++) {
        if (!cursor[0]) return 0; /* end of the string set */
        if (n == index) return cursor;
        while (*cursor) cursor++;
        cursor++;
    }
    return 0;
}
void sysinfo_smbios(uint64_t entry_point)
{
    const uint8_t *entry = (const void *)(uintptr_t)entry_point;
    if (!entry || !match(entry, "_SM3_", 5)) return;
    const uint8_t *table = (const void *)(uintptr_t)get64(entry + 16);
    uint32_t span = get32(entry + 12);
    if (!table || !span || span > (1u << 20)) return;
    con_puts("smbios: "); con_dec(entry[7]); con_puts("."); con_dec(entry[8]);
    con_puts(" at "); con_addr((uintptr_t)table); con_puts("\n");
    const uint8_t *cursor = table;
    unsigned seen = 0;
    while (cursor + 4 < table + span && seen++ < 256) {
        uint8_t type = cursor[0], length = cursor[1];
        if (length < 4) break;
        if (type == 1) { /* System Information */
            con_puts("smbios: system ");
            const uint8_t *maker = smbios_string(cursor, cursor[4]);
            const uint8_t *product = smbios_string(cursor, cursor[5]);
            if (maker) { put_text(maker, 48); con_puts(" "); }
            if (product) put_text(product, 48);
            con_puts("\n");
        } else if (type == 0) { /* BIOS Information */
            con_puts("smbios: firmware ");
            const uint8_t *vendor = smbios_string(cursor, cursor[4]);
            const uint8_t *version = smbios_string(cursor, cursor[5]);
            const uint8_t *date = smbios_string(cursor, cursor[8]);
            if (vendor) { put_text(vendor, 32); con_puts(" "); }
            if (version) put_text(version, 32);
            if (date) { con_puts(" ("); put_text(date, 16); con_puts(")"); }
            con_puts("\n");
        }
        /* Skip the formatted area and the double-NUL terminated string set. */
        const uint8_t *next = cursor + length;
        while (next + 1 < table + span && (next[0] || next[1])) next++;
        cursor = next + 2;
        if (type == 127) break; /* end of table */
    }
}

/* The ACPI tables that describe what m1n1 reports from the Apple device tree. */
void sysinfo_acpi(uint64_t rsdp_address, uint64_t *cores)
{
    const uint8_t *rsdp = (const void *)(uintptr_t)rsdp_address;
    if (cores) *cores = 0;
    if (!rsdp || !match(rsdp, "RSD PTR ", 8)) return;
    const uint8_t *xsdt = (const void *)(uintptr_t)get64(rsdp + 24);
    if (!xsdt || !match(xsdt, "XSDT", 4)) return;
    uint32_t span = get32(xsdt + 4);
    if (span < ACPI_HEADER || span > (1u << 20)) return;

    con_puts("acpi: rev "); con_dec(rsdp[15]); con_puts(", oem ");
    put_text(xsdt + 10, 6); con_puts("/"); put_text(xsdt + 16, 8);
    con_puts(", xsdt @ "); con_addr((uintptr_t)xsdt); con_puts("\n");
    con_puts("acpi: tables");
    for (uint32_t offset = ACPI_HEADER; offset + 8 <= span; offset += 8) {
        const uint8_t *table = (const void *)(uintptr_t)get64(xsdt + offset);
        if (!table) continue;
        con_puts(" "); put_text(table, 4);
    }
    con_puts("\n");

    for (uint32_t offset = ACPI_HEADER; offset + 8 <= span; offset += 8) {
        const uint8_t *table = (const void *)(uintptr_t)get64(xsdt + offset);
        if (!table) continue;
        uint32_t length = get32(table + 4);
        if (length < ACPI_HEADER || length > (1u << 20)) continue;

        if (match(table, "APIC", 4)) { /* count enabled CPUs, like m1n1's die/core report */
            uint64_t enabled = 0, total = 0;
            for (uint32_t n = 44; n + 2 <= length;) {
                uint8_t kind = table[n], size = table[n + 1];
                if (size < 2 || n + size > length) break;
                if (kind == 0x0b) { total++; if (get32(table + n + 12) & 1) enabled++; }
                n += size;
            }
            if (cores) *cores = enabled;
            con_puts("madt: "); con_dec(enabled); con_puts(" of "); con_dec(total);
            con_puts(" cpu interfaces enabled\n");
        } else if (match(table, "GTDT", 4) && length >= 96) {
            uint32_t count = get32(table + 88), start = get32(table + 92);
            con_puts("gtdt: cnt control @ ");
            uint64_t control = get64(table + 36);
            if (control + 1 == 0) con_puts("not provided"); else con_addr(control);
            con_puts(", el2 timer gsiv "); con_dec(get32(table + 72));
            con_puts(", "); con_dec(count); con_puts(" platform timers\n");
            for (uint32_t n = 0, at = start; n < count && at + 4 <= length; n++) {
                uint8_t kind = table[at];
                uint16_t size = get16(table + at + 1);
                if (size < 4 || at + size > length) break;
                if (kind == 1 && size >= 28) { /* SBSA watchdog: m1n1's "WDT registers @" */
                    con_puts("wdt: sbsa watchdog refresh @ "); con_addr(get64(table + at + 4));
                    con_puts(" control @ "); con_addr(get64(table + at + 12));
                    con_puts(" gsiv "); con_dec(get32(table + at + 20)); con_puts("\n");
                }
                at += size;
            }
        } else if (match(table, "IORT", 4) && length >= 48) { /* the dart equivalent */
            uint32_t count = get32(table + 36), at = get32(table + 40);
            con_puts("iort: "); con_dec(count); con_puts(" nodes\n");
            for (uint32_t n = 0; n < count && at + 16 <= length; n++) {
                uint8_t kind = table[at];
                uint16_t size = get16(table + at + 1);
                if (size < 16 || at + size > length) break;
                if (kind == 3 && size >= 28) {
                    con_puts("smmu: SMMUv1/v2 @ "); con_addr(get64(table + at + 16));
                    con_puts(" span "); con_addr(get64(table + at + 24));
                    con_puts(", "); con_dec(get32(table + at + 8)); con_puts(" id mappings\n");
                } else if (kind == 4 && size >= 24) {
                    con_puts("smmu: SMMUv3 @ "); con_addr(get64(table + at + 16));
                    con_puts(", "); con_dec(get32(table + at + 8)); con_puts(" id mappings\n");
                }
                at += size;
            }
        } else if (match(table, "MCFG", 4) && length >= 60) { /* m1n1's pcie init */
            con_puts("mcfg: "); con_dec((length - 44) / 16); con_puts(" ecam segments\n");
            for (uint32_t at = 44; at + 16 <= length; at += 16) {
                con_puts("mcfg: segment "); con_dec(get16(table + at + 8));
                con_puts(" ecam @ "); con_addr(get64(table + at));
                con_puts(", buses "); con_dec(table[at + 10]); con_puts("-"); con_dec(table[at + 11]);
                con_puts("\n");
            }
        } else if (match(table, "SPCR", 4) && length >= 80) {
            con_puts("spcr: console type "); con_hexn(table[36], 2);
            con_puts(" @ "); con_addr(get64(table + 44));
            con_puts(", gsiv "); con_dec(get32(table + 54)); con_puts("\n");
        }
    }
}

void sysinfo_memory(uint64_t map, uint64_t size, uint64_t stride)
{
    if (!map || !stride || stride < 40 || size % stride) return;
    uint64_t conventional = 0, reserved = 0, firmware = 0, biggest = 0, biggest_at = 0;
    uint64_t entries = size / stride;
    for (uint64_t n = 0; n < entries; n++) {
        const uint8_t *entry = (const uint8_t *)(uintptr_t)(map + n * stride);
        uint32_t type = get32(entry);
        uint64_t start = get64(entry + 8), bytes = get64(entry + 24) * 4096;
        if (type == 7) {
            conventional += bytes;
            if (bytes > biggest) { biggest = bytes; biggest_at = start; }
        } else if (type == 0 || type == 8) reserved += bytes;
        else if (type != 11 && type != 12) firmware += bytes; /* MMIO is not RAM */
    }
    con_puts("mem: "); con_dec((conventional + reserved + firmware) >> 20);
    con_puts(" MiB mapped, "); con_dec(conventional >> 20); con_puts(" MiB free, ");
    con_dec(reserved >> 20); con_puts(" MiB reserved, "); con_dec(firmware >> 20);
    con_puts(" MiB firmware, "); con_dec(entries); con_puts(" descriptors\n");
    con_puts("mem: largest free block "); con_dec(biggest >> 20);
    con_puts(" MiB @ "); con_addr(biggest_at); con_puts("\n");
}

void sysinfo_uefi(uint64_t system_table)
{
    const uint8_t *st = (const void *)(uintptr_t)system_table;
    if (!st) return;
    /* EFI_TABLE_HEADER: signature 0, revision 8, size 12, crc 16, reserved 20.
     * EFI_SYSTEM_TABLE then has FirmwareVendor at 24 and FirmwareRevision at 32. */
    uint32_t revision = get32(st + 8), firmware = get32(st + 32);
    const uint16_t *vendor = (const void *)(uintptr_t)get64(st + 24);
    con_puts("uefi: rev "); con_dec(revision >> 16); con_puts(".");
    con_dec((revision & 0xffff) / 10);
    con_puts(", firmware rev "); con_hexn(firmware, 8);
    if (vendor) {
        char text[49];
        unsigned n = 0;
        while (n < sizeof(text) - 1 && vendor[n]) {
            text[n] = (vendor[n] >= 32 && vendor[n] < 127) ? (char)vendor[n] : '.';
            n++;
        }
        text[n] = 0;
        con_puts(", vendor "); con_puts(text);
    }
    con_puts("\n");
}
