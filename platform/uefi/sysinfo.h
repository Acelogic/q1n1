/* SPDX-License-Identifier: MIT */
/* Boot-log detail parallel to what m1n1 prints on a Mac, from tables the
 * firmware already left behind. Each function writes through console.h. */
#pragma once
#include <stdint.h>

void sysinfo_cpu(uint64_t timer_hz, uint64_t cores);
void sysinfo_smbios(uint64_t entry_point);
void sysinfo_acpi(uint64_t rsdp_address, uint64_t *cores);
void sysinfo_memory(uint64_t map, uint64_t size, uint64_t stride);
void sysinfo_uefi(uint64_t system_table);
