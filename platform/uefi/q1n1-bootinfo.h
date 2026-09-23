/* SPDX-License-Identifier: MIT */
/* What P_GET_BOOTARGS returns, and the hand-off a chainloaded stage inherits.
 * Every field is 64 bits; tools/q1n1proxy.py parses them positionally. */
#pragma once
#include <stdint.h>

#define Q1N1_BOOTINFO_MAGIC UINT64_C(0x4f464e49314e3151) /* "Q1N1INFO" */

struct q1n1_bootinfo {
    uint64_t magic, version, size;
    uint64_t image_base, image_size, entry_el, current_el, mode;
    uint64_t system_table, runtime_services, config_tables, config_count, acpi_rsdp, smbios3;
    uint64_t fb_base, fb_size, fb_width, fb_height, fb_stride, fb_format;
    uint64_t memory_map, memory_map_size, memory_map_stride, memory_map_version;
    uint64_t heap_base, heap_size, dma_base, dma_size;
    uint64_t usb_dwc3, usb_qscratch, usb_snapshot, usb_stats, proxy_stats, exception;
    uint64_t boot_current, return_armed, arm_status, entry_status, timer_hz;
    uint64_t gic_distributor, gic_redistributor, gic_stats;
    /* Chainloading: a region reserved before ExitBootServices at an address the
     * stage binary is linked for, and a counter every stage increments so the
     * host can prove the new code is the one answering. */
    uint64_t stage_base, stage_size, stage_generation;
    /* The xHCI host on USB1 and the NCM link over it, when a stage was asked to
     * bring them up. Zero when it was not. */
    uint64_t xhci_base, xhci_stats, ncm_stats;
    /* The transport's own counters. Published rather than left for the host to
     * find, because locating it by adding up sizeof() on the host is guesswork
     * that silently returns neighbouring fields when a struct changes -- three
     * times in one session, each time as convincing nonsense. */
    uint64_t ncm_proxy_stats;
    /* Optional extension; readers must honor size when talking to older images. */
    uint64_t usb_ports, usb_port_count;
    /* Optional SSD cache. Data/table are EFI LoaderData, excluded from guests.
     * verify(index) returns 1 only after freshly hashing the resident bytes. */
    uint64_t preload_table, preload_size, preload_status, preload_verify;
    uint64_t preload_diagnostics;
};
_Static_assert(sizeof(struct q1n1_bootinfo) == 56 * 8, "bootinfo layout");

/* A chainloaded stage starts here with x0 = the inherited bootinfo. */
void q1n1_stage_main(struct q1n1_bootinfo *inherited);
