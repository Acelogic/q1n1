/* SPDX-License-Identifier: MIT */
#define Q1N1_ALLOW_INACTIVE_USB0
#define main role_fixture_main
#include "test-a16-usb-role-change.c"
#undef main

static void inactive_reset(void)
{
    reset(); config_handles_count = 1; usb0->mode = 0x10000;
    *(uint32_t *)(bios + CONTROLLERS_RVA + 0x12c) = 3;
}
static void check(efi_status expected, const char *message)
{
    assert(prepare_usb0((void *)1, &system_table, 1) == expected);
    assert(!calls && !watchdog_arms && !watchdog_disarms);
    assert(strstr(log_buffer, message)); tests++;
}
int main(int argc, char **argv)
{
    assert(!role_fixture_main(argc, argv));
    FILE *f = fopen(argv[1], "rb"); assert(f);
    original = calloc(1, CONFIG_IMAGE_SIZE); bios = malloc(CONFIG_IMAGE_SIZE);
    assert(original && bios);
    assert(fread(original, 1, CONFIG_IMAGE_SIZE, f) >= CONFIG_CODE_RVA + CONFIG_CODE_SIZE);
    fclose(f);
    inactive_reset(); check(0, "initialized USB0 is inactive");
    inactive_reset(); usb0->mode = 1;
    check(EFI_UNSUPPORTED, "Global/USB0 interfaces");
    inactive_reset(); usb0->mode = 4; function_handles_count = 1;
    check(EFI_UNSUPPORTED, "Global/USB0 interfaces");
    inactive_reset(); usb0->revision++;
    check(EFI_UNSUPPORTED, "Unexpected protocol revision");
    inactive_reset(); usb0->selector = 1;
    check(EFI_UNSUPPORTED, "Unexpected protocol revision");
    inactive_reset(); *(uintptr_t *)(bios + CONTROLLERS_RVA + 0x18) = 0;
    check(EFI_UNSUPPORTED, "outside described RAM");
    inactive_reset(); *(uintptr_t *)(phy_data + 0x50) = 0;
    check(EFI_UNSUPPORTED, "base getter differs");
    inactive_reset(); *(uint64_t *)(phy_data + 0x88) = 0xa800000;
    check(EFI_UNSUPPORTED, "does not map");
    inactive_reset(); *(uint32_t *)(bios + CONTROLLERS_RVA + 0x12c) = 2;
    check(EFI_UNSUPPORTED, "captured dock policy/gate");
    inactive_reset(); *(uint32_t *)(bios + CONTROLLERS_RVA + 0x140) = 3;
    check(EFI_UNSUPPORTED, "captured dock policy/gate");
    inactive_reset(); function_handles_count = 1;
    check(EFI_UNSUPPORTED, "Unexpected existing USB Function state");
    printf("PASS: %u combined role and inactive-controller cases.\n", tests);
    free(original); free(bios); return 0;
}
