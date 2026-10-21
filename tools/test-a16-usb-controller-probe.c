/* SPDX-License-Identifier: MIT */
/* Reuse native EFI mocks. Captured firmware is data and is never executed. */
#define efi_main role_test_efi_main
#define main role_test_main
#include "test-a16-usb-role-change.c"
#undef main
#undef efi_main
#define Q1N1_CONFIG_HELPERS_PRESENT
#include "../platform/uefi/usb-controller-probe.c"

static void snapshot(efi_status expected, const char *message)
{
    assert(efi_main((void *)1, &system_table) == expected);
    assert(!calls && !watchdog_arms && !watchdog_disarms);
    assert(strstr(log_buffer, message));
    tests++;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    FILE *f = fopen(argv[1], "rb"); assert(f);
    original = calloc(1, CONFIG_IMAGE_SIZE); bios = malloc(CONFIG_IMAGE_SIZE);
    assert(original && bios);
    size_t n = fread(original, 1, CONFIG_IMAGE_SIZE, f); fclose(f);
    assert(n >= CONFIG_CODE_RVA + CONFIG_CODE_SIZE);
    console.output = output;
    boot.locate_handle_buffer = locate; boot.handle_protocol = protocol;
    boot.free_pool = free_pool; boot.get_memory_map = get_map;
    boot.set_watchdog = watchdog; boot.stall = (void *)stall_mock;
    system_table.boot = &boot; system_table.console_out = &console;

    reset(); snapshot(0, "Verified PHY controller base: 0x000000000A600000");
    reset(); config_handles_count = 1; usb0->mode = 0x10000;
    snapshot(0, "Installed interface references: 0x0000000000000000");
    assert(strstr(log_buffer, "Mode: 0x0000000000010000"));
    reset(); *(uintptr_t *)(bios + CONTROLLERS_RVA + 0x18) = 0;
    snapshot(0, "PHY object not initialized.");
    reset(); *(uintptr_t *)(bios + CONTROLLERS_RVA + 0x18) = 1;
    snapshot(0, "outside described RAM; not dereferenced.");
    reset(); *(uintptr_t *)(phy_data + 0x50) = 0;
    snapshot(0, "PHY getter differs; base not decoded.");
    reset(); bios[0x4ca8] ^= 1;
    snapshot(EFI_UNSUPPORTED, "not the inspected BIOS312 image");
    reset(); global_config->revision++;
    snapshot(EFI_UNSUPPORTED, "Global configuration ABI differs");
    reset(); map_fails = 1;
    snapshot(EFI_UNSUPPORTED, "Could not validate firmware RAM bounds");
    reset(); global_config = (struct qcom_config *)1;
    snapshot(EFI_UNSUPPORTED, "verified global interface is not installed");
    printf("PASS: %u controller snapshot cases; no vendor callbacks or watchdog calls.\n", tests);
    free(original); free(bios);
    return 0;
}
