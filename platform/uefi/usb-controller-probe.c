/* SPDX-License-Identifier: MIT */
/* BIOS312 RAM inventory, including controller objects with no installed handle.
 * Never invokes vendor callbacks, starts drivers, or accesses controller MMIO. */
#ifndef Q1N1_CONFIG_HELPERS_PRESENT
#define Q1N1_ROLE_LIBRARY
#include "usb-role-change.c"
#endif

efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    (void)image;
    (void)prepare_usb0; /* Shared helpers; the active role path is never called. */
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st, "q1n1 A16 BIOS312 USB controller RAM inventory v1\n"
            "Read-only; no vendor callbacks, driver starts, or MMIO.\n");
    uint8_t *driver = find_firmware(st, CONFIG_IMAGE_SIZE, CONFIG_CODE_RVA,
                                   CONFIG_CODE_SIZE, CONFIG_CODE_FNV);
    if (!driver) return refuse(st, "Loaded UsbConfigDxe is not the inspected BIOS312 image.");
    struct qcom_config *global = (void *)(driver + GLOBAL_RVA);
    if (global->revision != 0x20001 || global->selector != 5 ||
        global->mode != 0x10000 || global->methods[9] != driver + CHANGE_RVA ||
        global->methods[0] != driver + GET_BASE_RVA)
        return refuse(st, "Global configuration ABI differs from BIOS312.");

    uint64_t key = 0;
    uint32_t version = 0;
    map_size = sizeof(memory_map);
    efi_status status = st->boot->get_memory_map(&map_size, memory_map, &key,
                                                &descriptor_size, &version);
    if (status || descriptor_size < 40 || descriptor_size > sizeof(memory_map) ||
        map_size > sizeof(memory_map) || map_size % descriptor_size)
        return refuse(st, "Could not validate firmware RAM bounds.");

    efi_handle *handles = NULL;
    uint64_t count = 0;
    status = st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &config_guid, NULL,
                                           &count, &handles);
    if (status) return refuse(st, "Configuration inventory unavailable.");
    unsigned refs[5] = {0}, global_refs = 0;
    for (uint64_t n = 0; n < count; n++) {
        void *interface = NULL;
        if (st->boot->handle_protocol(handles[n], &config_guid, &interface)) continue;
        if (interface == global) global_refs++;
        for (unsigned c = 0; c < 5; c++)
            if (interface == driver + CONTROLLERS_RVA + c * CONTROLLER_STRIDE + CONFIG_IN_CONTROLLER)
                refs[c]++;
    }
    st->boot->free_pool(handles);
    if (!global_refs) return refuse(st, "The verified global interface is not installed.");
    number(st, "UsbConfigDxe image base: ", (uintptr_t)driver);
    number(st, "Global interface references: ", global_refs);
    for (unsigned c = 0; c < 5; c++) {
        const uint8_t *port = driver + CONTROLLERS_RVA + c * CONTROLLER_STRIDE;
        const struct qcom_config *config = (const void *)(port + CONFIG_IN_CONTROLLER);
        uintptr_t phy = *(const uintptr_t *)(port + 0x18);
        number(st, "CONTROLLER: ", c);
        number(st, "  Installed interface references: ", refs[c]);
        number(st, "  Revision: ", config->revision);
        number(st, "  Selector: ", config->selector);
        number(st, "  Mode: ", config->mode);
        number(st, "  Policy: ", *(const uint32_t *)(port + 0x12c));
        number(st, "  Transition gate: ", *(const uint32_t *)(port + 0x140));
        number(st, "  PHY pointer: ", phy);
        if (!phy) { out(st, "  PHY object not initialized.\n"); continue; }
        if (!readable_ram(phy, 0x98)) {
            out(st, "  PHY object outside described RAM; not dereferenced.\n");
            continue;
        }
        if (*(const uintptr_t *)(phy + 0x50) != (uintptr_t)(driver + PHY_GET_BASE_RVA)) {
            out(st, "  PHY getter differs; base not decoded.\n");
            continue;
        }
        number(st, "  Verified PHY controller base: ", *(const uint64_t *)(phy + 0x88));
    }
    out(st, "RAM inventory complete. No USB role or controller state was changed.\n");
    return 0;
}
