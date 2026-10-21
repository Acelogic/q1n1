/* SPDX-License-Identifier: MIT */
/* Boot-log detail parallel to what m1n1 prints on a Mac, from tables the
 * firmware already left behind. Each function writes through console.h. */
#pragma once
#include <stdint.h>

void sysinfo_cpu(uint64_t timer_hz, uint64_t cores);
void sysinfo_smbios(uint64_t entry_point);
/* Does this machine's panel retain a static image?
 *
 * Nothing in UEFI says "this is an OLED", so there is no probing this: it is a
 * per-machine fact, matched on the SMBIOS product name. The A16's panel is
 * OLED and shows a mostly-unchanging boot log for as long as the proxy is up,
 * which is exactly what burns in. Other machines this is ported to are not
 * assumed to be the same -- add them to the table in sysinfo.c, or they get no
 * console motion, which is the safe default for a panel that does not need it. */
int sysinfo_panel_retains(uint64_t smbios_entry_point);
void sysinfo_acpi(uint64_t rsdp_address, uint64_t *cores);
void sysinfo_memory(uint64_t map, uint64_t size, uint64_t stride);
void sysinfo_uefi(uint64_t system_table);
