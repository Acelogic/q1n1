/* SPDX-License-Identifier: MIT */
/* Experimental UX3607OA.312 boot-services test. Default: preflight only.
 * --device0 asks the inspected UsbConfigDxe driver to switch controller 0.
 * No MMIO writes, protocol-field edits, NVRAM writes, or ExitBootServices here.
 * The firmware callback DOES disconnect/start drivers and configure hardware.
 * See docs/A16-USB-ROLE-TEST.md and tools/audit-a16-usb-binding.py.
 */
#include "efi.h"

#define CONFIG_IMAGE_SIZE 0x13000
#define CONFIG_CODE_RVA 0x1000
#define CONFIG_CODE_SIZE 0xb000
#define CONFIG_CODE_FNV UINT64_C(0x5a151951eae7fefb)
#define GLOBAL_RVA 0xe590
#define CONTROLLERS_RVA 0xdf00
#define CONTROLLER_STRIDE 0x148
#define CONFIG_IN_CONTROLLER 0x28
#define CHANGE_RVA 0x3ac8
#define GET_BASE_RVA 0x38b0
#define PHY_GET_BASE_RVA 0x9d50
#define EXPECTED_USB0_BASE UINT64_C(0x0a600000)

struct qcom_config {
    uint64_t revision;
    void *methods[16];
    uint32_t selector, mode;
};
_Static_assert(offsetof(struct qcom_config, methods[9]) == 0x50, "Role callback ABI");
_Static_assert(offsetof(struct qcom_config, selector) == 0x88, "Selector ABI");
_Static_assert(offsetof(struct qcom_config, mode) == 0x8c, "Mode ABI");
typedef efi_status (*change_role_fn)(struct qcom_config *, uint32_t, uint32_t);
static efi_guid config_guid = {0xe722b03f,0xb250,0x42ce,{0x8e,0xbd,0x5b,0xd5,0x18,0x12,0xd0,0x37}};
static efi_guid loaded_guid = {0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid function_guid = {0x32d2963a,0xfe5d,0x4f30,{0xb6,0x33,0x6e,0x5d,0xc5,0x58,0x03,0xcc}};
static uint8_t memory_map[65536] __attribute__((aligned(8)));
static uint64_t map_size, descriptor_size;

static void out(struct efi_system_table *st, const char *s)
{
    char16 b[120];
    while (*s) {
        unsigned n = 0;
        while (*s && n < 117) {
            if (*s == '\n') b[n++] = '\r';
            b[n++] = (uint8_t)*s++;
        }
        b[n] = 0;
        st->console_out->output(st->console_out, b);
    }
}
static void number(struct efi_system_table *st, const char *label, uint64_t value)
{
    char s[19] = "0x0000000000000000";
    for (unsigned n = 0; n < 16; n++) s[n + 2] = "0123456789ABCDEF"[(value >> (60 - 4 * n)) & 15];
    out(st, label); out(st, s); out(st, "\n");
}
static uint64_t fingerprint(const uint8_t *p, uint64_t length)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    for (uint64_t n = 0; n < length; n++) h = (h ^ p[n]) * UINT64_C(0x100000001b3);
    return h;
}
static efi_status refuse(struct efi_system_table *st, const char *reason)
{
    out(st, "REFUSED: "); out(st, reason);
    out(st, "\nNo role change called. Returning to shell.\n");
    return EFI_UNSUPPORTED;
}
/* Exact option tokens; tolerate the shell's executable-name prefix. */
static int device_option(struct efi_loaded_image *image)
{
    if (!image->options_size) return 0;
    if (!image->options || image->options_size % 2 || image->options_size > 4096) return -1;
    const char16 *s = image->options;
    unsigned count = image->options_size / 2;
    int active = 0;
    for (unsigned n = 0; n < count && s[n];) {
        while (n < count && (s[n] == ' ' || s[n] == '\t')) n++;
        unsigned start = n;
        while (n < count && s[n] && s[n] != ' ' && s[n] != '\t') n++;
        if (start == n) break;
        if (s[start] != '-') continue;
        const char option[] = "--device0";
        if (n - start != sizeof(option) - 1 || active) return -1;
        for (unsigned k = 0; k < sizeof(option) - 1; k++) if (s[start + k] != option[k]) return -1;
        active = 1;
    }
    return active;
}
/* Use LoadedImage bounds before inspecting executable memory. The checksum
 * covers all driver code; no relocations occur in this region in BIOS312.
 * FNV is an accidental-version-mismatch check, not an authentication scheme. */
static uint8_t *find_firmware(struct efi_system_table *st, uint64_t image_size,
                              uint64_t code_rva, uint64_t code_size, uint64_t expected)
{
    efi_handle *handles = NULL;
    uint64_t count = 0;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &loaded_guid, NULL, &count, &handles)) return NULL;
    uint8_t *found = NULL;
    unsigned matches = 0;
    for (uint64_t n = 0; n < count; n++) {
        struct efi_loaded_image *li = NULL;
        if (st->boot->handle_protocol(handles[n], &loaded_guid, (void **)&li) || !li) continue;
        if (!li->image_base || li->image_size != image_size || code_rva > image_size ||
            code_size > image_size - code_rva) continue;
        uint8_t *base = li->image_base;
        if (fingerprint(base + code_rva, code_size) == expected) {
            found = base;
            matches++;
        }
    }
    st->boot->free_pool(handles);
    return matches == 1 ? found : NULL;
}
/* Private PHY data is only read inside a firmware-described RAM allocation. */
static int readable_ram(uintptr_t address, uint64_t length)
{
    if (!address || address % 8 || address + length < address) return 0;
    for (uint64_t off = 0; off + descriptor_size <= map_size; off += descriptor_size) {
        const uint8_t *d = memory_map + off;
        uint32_t type = *(const uint32_t *)d;
        uint64_t start = *(const uint64_t *)(d + 8), pages = *(const uint64_t *)(d + 24);
        if (type < 1 || type > 7 || pages > (UINT64_MAX - start) / 4096) continue;
        uint64_t end = start + pages * 4096;
        if (address >= start && address <= end && length <= end - address) return 1;
    }
    return 0;
}
static efi_status function_count(struct efi_system_table *st, uint64_t *count)
{
    efi_handle *handles = NULL;
    *count = 0;
    efi_status status = st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &function_guid, NULL, count, &handles);
    if (status == EFI_NOT_FOUND) return 0;
    if (handles) st->boot->free_pool(handles);
    return status;
}
static efi_status change_device0(struct efi_system_table *, struct qcom_config *, struct qcom_config *);
static efi_status prepare_usb0(efi_handle image, struct efi_system_table *st, int allow_device)
{
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st, "q1n1 A16 BIOS312 USB role-change test v1\n");
    out(st, "Default is preflight. --device0 requests USB0 function mode.\n");
    struct efi_loaded_image *self = NULL;
    if (st->boot->handle_protocol(image, &loaded_guid, (void **)&self) || !self) return refuse(st, "LoadedImage unavailable.");
#ifdef Q1N1_ROLE_FORCE_ACTIVE
    /* The linking payload parsed its own options and requests device mode. */
    int active = 1;
#else
    int active = device_option(self);
#endif
    if (active < 0) return refuse(st, "Unknown, duplicate, or malformed option.");
    uint8_t *driver = find_firmware(st, CONFIG_IMAGE_SIZE, CONFIG_CODE_RVA, CONFIG_CODE_SIZE, CONFIG_CODE_FNV);
    if (!driver) return refuse(st, "Matching BIOS312 UsbConfigDxe code not found.");
    out(st, "Loaded UsbConfigDxe code fingerprint matched BIOS312.\n");

    efi_handle *handles = NULL;
    uint64_t count = 0;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &config_guid, NULL, &count, &handles))
        return refuse(st, "USB configuration protocol absent.");
    struct qcom_config *global = NULL, *controller = NULL;
    unsigned globals = 0, controllers = 0;
    uintptr_t observed[16] = {0};
    /* Compare interface addresses before reading their fields. Both instances
     * are in the matched image's static data, not unbounded vendor allocations. */
    for (uint64_t n = 0; n < count; n++) {
        void *p = NULL;
        if (st->boot->handle_protocol(handles[n], &config_guid, &p)) continue;
        if (n < sizeof(observed) / sizeof(observed[0])) observed[n] = (uintptr_t)p;
        if (p == driver + GLOBAL_RVA) { global = p; globals++; }
        if (p == driver + CONTROLLERS_RVA + CONFIG_IN_CONTROLLER) { controller = p; controllers++; }
    }
    st->boot->free_pool(handles);
    int inactive = 0;
#ifdef Q1N1_ALLOW_INACTIVE_USB0
    /* BIOS312's selector at 0x4ca8 accepts its initialized static USB0 object
     * in mode 0x10000. At 0x4dcc it skips disconnecting a nonexistent driver.
     * The dock RAM capture qualified this exact unregistered state. Retain all
     * ABI, PHY allocation/base, policy, gate, and Function checks below. */
    if (allow_device && global && !controller) {
        struct qcom_config *candidate = (void *)(driver + CONTROLLERS_RVA + CONFIG_IN_CONTROLLER);
        if (candidate->mode == 0x10000) { controller = candidate; inactive = 1; }
    }
#endif
    /* More than one handle can expose the identical static interface. Match
     * the object address, not handle cardinality; every field check below is
     * still applied to the same inspected global and USB0 objects. */
    if (!global || !controller) {
        number(st, "UsbConfigDxe image base: ", (uintptr_t)driver);
        number(st, "Expected global interface: ", (uintptr_t)(driver + GLOBAL_RVA));
        number(st, "Expected USB0 interface: ", (uintptr_t)(driver + CONTROLLERS_RVA + CONFIG_IN_CONTROLLER));
        number(st, "Configuration handles: ", count);
        number(st, "Global matches: ", globals);
        number(st, "USB0 matches: ", controllers);
        for (uint64_t n = 0; n < count && n < sizeof(observed) / sizeof(observed[0]); n++) {
            number(st, "  Interface index: ", n);
            number(st, "  Interface address (not dereferenced): ", observed[n]);
        }
        return refuse(st, "Global/USB0 interfaces do not match inspected layout.");
    }
    number(st, "Global interface handle references: ", globals);
    number(st, "USB0 interface handle references: ", controllers);
    if (globals > 1 || controllers > 1) {
        number(st, "Matched global object address: ", (uintptr_t)global);
        number(st, "Matched USB0 object address: ", (uintptr_t)controller);
        out(st, "Identical interface pointers share handles; ABI checks still required.\n");
    }
    if (global->revision != 0x20001 || global->selector != 5 || global->mode != 0x10000 ||
        global->methods[9] != driver + CHANGE_RVA || global->methods[0] != driver + GET_BASE_RVA ||
        controller->revision != 0x20001 || controller->selector != 0 ||
        (controller->mode != 1 && !(allow_device && controller->mode == 4) &&
         !(inactive && controller->mode == 0x10000)))
        return refuse(st, "Unexpected protocol revision, callbacks, or initial USB0 role.");

    uint64_t key = 0;
    uint32_t descriptor_version = 0;
    map_size = sizeof(memory_map);
    efi_status status = st->boot->get_memory_map(&map_size, memory_map, &key, &descriptor_size, &descriptor_version);
    if (status || descriptor_size < 40 || descriptor_size > sizeof(memory_map) || map_size > sizeof(memory_map) ||
        map_size % descriptor_size) return refuse(st, "Could not validate firmware RAM bounds.");
    const uint8_t *port = driver + CONTROLLERS_RVA;
    uintptr_t phy = *(const uintptr_t *)(port + 0x18);
    if (!readable_ram(phy, 0x98)) return refuse(st, "USB0 PHY object is outside described RAM.");
    if (*(const uintptr_t *)(phy + 0x50) != (uintptr_t)(driver + PHY_GET_BASE_RVA))
        return refuse(st, "USB0 base getter differs from inspected implementation.");
    uint64_t base = *(const uint64_t *)(phy + 0x88);
    number(st, "USB0 controller base: ", base);
    if (base != EXPECTED_USB0_BASE) return refuse(st, "USB0 does not map to the captured URS0 address.");
    uint32_t policy = *(const uint32_t *)(port + 0x12c);
    uint32_t gate = *(const uint32_t *)(port + 0x140);
    number(st, "Firmware policy state: ", policy);
    number(st, "Firmware transition gate: ", gate);
    uint64_t functions = 0;
    if (function_count(st, &functions)) return refuse(st, "USB Function enumeration failed.");
    number(st, "Existing USB Function handle count: ", functions);
    if (allow_device && controller->mode == 4 && functions == 1) {
        out(st, "USB0 already has its function driver; no additional role call.\n");
        return 0;
    }
    if (policy < 1 || policy > 3 || gate == 1 || gate == 2)
        return refuse(st, "Firmware state does not permit the inspected device transition.");
    if (functions) return refuse(st, "Unexpected existing USB Function state.");
    if (inactive && (policy != 3 || gate != 0))
        return refuse(st, "Inactive USB0 differs from the captured dock policy/gate.");
    out(st, inactive ? "Preflight passed: initialized USB0 is inactive at 0x0A600000.\n"
                     : "Preflight passed: USB0 is the host controller at 0x0A600000.\n");
    if (!active) {
        out(st, "No role change called. Run with --device0 for the active test.\n");
        return 0;
    }
    return change_device0(st, global, controller);
}
#ifndef Q1N1_ROLE_LIBRARY
efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    return prepare_usb0(image, st, 0);
}
#endif
static efi_status change_device0(struct efi_system_table *st, struct qcom_config *global,
                                 struct qcom_config *controller)
{
    /* The firmware method can block. Arm its watchdog before entering it.
     * This is a fallback, not a guarantee against a firmware/hardware hang. */
    efi_status status = st->boot->set_watchdog(60, 0, 0, NULL);
    if (status) return refuse(st, "Could not arm the firmware watchdog.");
    out(st, "REQUESTING USB0 DEVICE MODE; the port's host devices will disconnect.\n");
    status = ((change_role_fn)global->methods[9])(global, 0, 4);
    number(st, "Role-change EFI status: ", status);
    /* Allow firmware notifications to run before reading the resulting state. */
    efi_status (*stall)(uint64_t) = (efi_status (*)(uint64_t))st->boot->stall;
    efi_status inventory_status = 0;
    uint64_t functions = 0;
    for (unsigned n = 0; n < 5; n++) {
        if (stall(200000)) break;
        inventory_status = function_count(st, &functions);
        if (inventory_status) break;
    }
    uint32_t resulting_mode = *(volatile uint32_t *)&controller->mode;
    number(st, "USB0 resulting mode: ", resulting_mode);
    number(st, "USB FUNCTION handles: ", functions);
    if (inventory_status) number(st, "USB Function inventory error: ", inventory_status);
    efi_status watchdog_status = st->boot->set_watchdog(0, 0, 0, NULL);
    if (watchdog_status) number(st, "Watchdog disable error: ", watchdog_status);
    int success = !status && !inventory_status && resulting_mode == 4 && functions == 1;
    out(st, success ? "FUNCTION DRIVER PRESENT; cable enumeration is not tested yet.\n" :
                     "DEVICE TRANSITION NOT CONFIRMED.\n");
#ifndef Q1N1_ROLE_LIBRARY
    out(st, "No serial descriptors or transfers were started.\n"
            "Returning to shell. Reboot to restore normal firmware USB policy.\n");
#else
    out(st, "Role check finished; returning to the serial app.\n");
#endif
    return status ? status : success ? 0 : EFI_UNSUPPORTED;
}
