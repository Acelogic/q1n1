/* SPDX-License-Identifier: MIT */
/* Native mocks only. The BIOS image is data and is NEVER executed here.
 * Run against pefile's memory-mapped captured UsbConfigDxe image. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../platform/uefi/usb-role-change.c"

static uint8_t *original, *bios;
static uint8_t phy_data[0x100] __attribute__((aligned(8)));
static struct qcom_config *global_config, *usb0;
static struct efi_loaded_image app_image, firmware_image;
static struct efi_boot_services boot;
static struct efi_text console;
static struct efi_system_table system_table;
static efi_handle image_handles[] = {(void *)1, (void *)2};
static efi_handle config_handles[] = {(void *)3, (void *)4, (void *)6, (void *)7};
static efi_handle function_handles[] = {(void *)5};
static char log_buffer[8192];
static size_t log_used;
static unsigned calls, watchdog_arms, watchdog_disarms, tests;
static uint64_t function_handles_count;
static uint64_t config_handles_count;
static int arm_fails, method_changes, map_fails;
static efi_status method_result, inventory_result;

static int same(efi_guid *a, efi_guid *b) { return !memcmp(a, b, sizeof(*a)); }
static efi_status output(struct efi_text *c, const char16 *s)
{
    (void)c;
    while (*s && log_used + 1 < sizeof(log_buffer)) log_buffer[log_used++] = (char)*s++;
    log_buffer[log_used] = 0;
    return 0;
}
static efi_status locate(uint32_t kind, efi_guid *guid, void *key, uint64_t *count, efi_handle **handles)
{
    assert(kind == EFI_BY_PROTOCOL && !key);
    if (same(guid, &loaded_guid)) { *count = 2; *handles = image_handles; return 0; }
    if (same(guid, &config_guid)) { *count = config_handles_count; *handles = config_handles; return 0; }
    assert(same(guid, &function_guid));
    if (inventory_result) return inventory_result;
    if (!function_handles_count) return EFI_NOT_FOUND;
    *count = function_handles_count; *handles = function_handles; return 0;
}
static efi_status protocol(efi_handle h, efi_guid *guid, void **result)
{
    if (same(guid, &loaded_guid)) {
        *result = h == (void *)1 ? &app_image : &firmware_image;
        return 0;
    }
    assert(same(guid, &config_guid));
    *result = h == (void *)3 || h == (void *)6 ? global_config : usb0;
    return 0;
}
static efi_status free_pool(void *p) { (void)p; return 0; }
static efi_status get_map(uint64_t *size, void *data, uint64_t *key, uint64_t *stride, uint32_t *version)
{
    if (map_fails) return EFI_BUFFER_TOO_SMALL;
    assert(*size >= 40);
    memset(data, 0, 40);
    *(uint32_t *)data = 4;
    *(uint64_t *)((uint8_t *)data + 8) = (uintptr_t)phy_data & ~UINT64_C(4095);
    *(uint64_t *)((uint8_t *)data + 24) = 2;
    *size = 40; *key = 1; *stride = 40; *version = 1;
    return 0;
}
static efi_status watchdog(uint64_t seconds, uint64_t code, uint64_t size, char16 *data)
{
    assert(!code && !size && !data);
    if (seconds) { assert(seconds == 60); watchdog_arms++; return arm_fails ? EFI_UNSUPPORTED : 0; }
    watchdog_disarms++; return 0;
}
static efi_status stall_mock(uint64_t delay) { assert(delay == 200000); return 0; }
static efi_status change_mock(struct qcom_config *self, uint32_t selector, uint32_t mode)
{
    assert(self == global_config && selector == 0 && mode == 4);
    assert(watchdog_arms == 1 && !watchdog_disarms);
    calls++;
    if (method_changes) { usb0->mode = 4; function_handles_count = 1; }
    return method_result;
}
static void reset(void)
{
    memcpy(bios, original, CONFIG_IMAGE_SIZE);
    memset(phy_data, 0, sizeof(phy_data));
    global_config = (void *)(bios + GLOBAL_RVA);
    usb0 = (void *)(bios + CONTROLLERS_RVA + CONFIG_IN_CONTROLLER);
    global_config->revision = usb0->revision = 0x20001;
    global_config->selector = 5; global_config->mode = 0x10000;
    global_config->methods[0] = bios + GET_BASE_RVA;
    global_config->methods[9] = bios + CHANGE_RVA;
    usb0->selector = 0; usb0->mode = 1;
    *(uintptr_t *)(bios + CONTROLLERS_RVA + 0x18) = (uintptr_t)phy_data;
    *(uintptr_t *)(phy_data + 0x50) = (uintptr_t)(bios + PHY_GET_BASE_RVA);
    *(uint64_t *)(phy_data + 0x88) = EXPECTED_USB0_BASE;
    *(uint32_t *)(bios + CONTROLLERS_RVA + 0x12c) = 1;
    *(uint32_t *)(bios + CONTROLLERS_RVA + 0x140) = 0;
    firmware_image.image_base = bios; firmware_image.image_size = CONFIG_IMAGE_SIZE;
    app_image.options = NULL; app_image.options_size = 0;
    log_used = calls = watchdog_arms = watchdog_disarms = 0;
    log_buffer[0] = 0; function_handles_count = 0; config_handles_count = 2;
    arm_fails = map_fails = 0; method_changes = 1;
    method_result = inventory_result = 0;
}
static void rejected(const char *message)
{
    efi_status s = efi_main((void *)1, &system_table);
    if (s != EFI_UNSUPPORTED || calls || !strstr(log_buffer, message)) {
        fprintf(stderr, "%s\n", log_buffer); abort();
    }
    tests++;
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    FILE *f = fopen(argv[1], "rb"); assert(f);
    original = calloc(1, CONFIG_IMAGE_SIZE); bios = malloc(CONFIG_IMAGE_SIZE);
    assert(original && bios);
    size_t read = fread(original, 1, CONFIG_IMAGE_SIZE, f); fclose(f);
    assert(read >= CONFIG_CODE_RVA + CONFIG_CODE_SIZE);
    assert(fingerprint(original + CONFIG_CODE_RVA, CONFIG_CODE_SIZE) == CONFIG_CODE_FNV);
    console.output = output;
    boot.locate_handle_buffer = locate; boot.handle_protocol = protocol;
    boot.free_pool = free_pool; boot.get_memory_map = get_map;
    boot.set_watchdog = watchdog; boot.stall = (void *)stall_mock;
    system_table.boot = &boot; system_table.console_out = &console;

    reset(); assert(!efi_main((void *)1, &system_table));
    assert(!calls && !watchdog_arms && strstr(log_buffer, "Preflight passed")); tests++;
    reset(); config_handles_count=4; assert(!efi_main((void *)1, &system_table));
    assert(!calls && strstr(log_buffer, "Identical interface pointers share handles")); tests++;
    reset(); config_handles_count=4; global_config->revision++; rejected("Unexpected protocol revision");
    reset(); struct qcom_config other_usb0=*usb0; usb0=&other_usb0; config_handles_count=4;
    rejected("Global/USB0 interfaces"); /* Identical fields at another address are not accepted. */
    reset(); bios[0x4ca8] ^= 1; rejected("Matching BIOS312 UsbConfigDxe code not found");
    reset(); global_config->revision++; rejected("Unexpected protocol revision");
    reset(); global_config->methods[9] = NULL; rejected("Unexpected protocol revision");
    reset(); global_config = (struct qcom_config *)1; rejected("Global/USB0 interfaces");
    assert(strstr(log_buffer, "Global matches: 0x0000000000000000"));
    reset(); usb0 = (struct qcom_config *)1; rejected("Global/USB0 interfaces");
    assert(strstr(log_buffer, "USB0 matches: 0x0000000000000000"));
    reset(); usb0->mode = 4; rejected("Unexpected protocol revision");
    reset(); *(uintptr_t *)(bios + CONTROLLERS_RVA + 0x18) = 1; rejected("outside described RAM");
    reset(); *(uintptr_t *)(phy_data + 0x50) = 0; rejected("base getter differs");
    reset(); *(uint64_t *)(phy_data + 0x88) = 0xa800000; rejected("does not map");
    reset(); *(uint32_t *)(bios + CONTROLLERS_RVA + 0x12c) = 4; rejected("does not permit");
    reset(); *(uint32_t *)(bios + CONTROLLERS_RVA + 0x140) = 1; rejected("does not permit");
    reset(); map_fails = 1; rejected("RAM bounds");
    reset(); function_handles_count = 1; rejected("Unexpected existing USB Function");
    char16 unknown[] = {'-','-','a','l','l',0};
    reset(); app_image.options = unknown; app_image.options_size = sizeof(unknown); rejected("Unknown, duplicate");
    char16 active[] = {'f','s','2',':','x','.','e','f','i',' ','-','-','d','e','v','i','c','e','0',0};
    reset(); app_image.options = active; app_image.options_size = sizeof(active);
    usb0->mode = 4; function_handles_count = 1;
    assert(!prepare_usb0((void *)1, &system_table, 1));
    assert(!calls && !watchdog_arms && strstr(log_buffer, "already has its function driver")); tests++;
    reset(); app_image.options = active; app_image.options_size = sizeof(active);
    config_handles_count=4; usb0->mode=4; function_handles_count=1;
    assert(!prepare_usb0((void *)1,&system_table,1));
    assert(!calls && !watchdog_arms && strstr(log_buffer,"already has its function driver")); tests++;
    reset(); app_image.options = active; app_image.options_size = sizeof(active);
    arm_fails = 1; rejected("Could not arm"); assert(watchdog_arms == 1);

    /* Exercise the command transaction with a native mock callback, after
     * testing the real preflight separately. Never execute captured ARM code. */
    reset(); global_config->methods[9] = (void *)change_mock;
    assert(!change_device0(&system_table, global_config, usb0));
    assert(calls == 1 && watchdog_disarms == 1 && strstr(log_buffer, "FUNCTION DRIVER PRESENT")); tests++;
    reset(); global_config->methods[9] = (void *)change_mock; method_changes = 0;
    assert(change_device0(&system_table, global_config, usb0) == EFI_UNSUPPORTED);
    assert(calls == 1 && watchdog_disarms == 1 && strstr(log_buffer, "NOT CONFIRMED")); tests++;
    reset(); global_config->methods[9] = (void *)change_mock; method_result = EFI_ERROR(7);
    assert(change_device0(&system_table, global_config, usb0) == EFI_ERROR(7));
    assert(calls == 1 && watchdog_disarms == 1 && !strstr(log_buffer, "FUNCTION DRIVER PRESENT")); tests++;
    reset(); global_config->methods[9] = (void *)change_mock; inventory_result = EFI_ERROR(7);
    assert(change_device0(&system_table, global_config, usb0) == EFI_UNSUPPORTED);
    assert(calls == 1 && watchdog_disarms == 1 && !strstr(log_buffer, "FUNCTION DRIVER PRESENT")); tests++;
    printf("PASS: %u preflight and role-transaction cases; no BIOS code executed.\n", tests);
    free(original); free(bios);
    return 0;
}
