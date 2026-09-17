/* SPDX-License-Identifier: MIT */
/* BIOS312 Type-C cached-state snapshot. No vendor callback, MMIO, role change,
 * PD request, NVRAM write, or ExitBootServices. See A16-USB-TYPEC.md. */
#include "efi.h"

#define POWER_IMAGE_SIZE 0x5000
#define POWER_CODE_FNV UINT64_C(0xa599916bef31a4c9)
#define POWER_INTERFACE 0x30c0
#define PORT_COUNT_RVA 0x3138
#define CACHE_READY_RVA 0x35fc
#define PORT_TABLE_RVA 0x3640
#define CACHE_RVA 0x3698
#define PORT_STRIDE 0xc8

static efi_guid loaded_guid = {0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid power_guid = {0xe07df17e,0xe79e,0x4150,{0x93,0x78,0x50,0x62,0x3a,0x14,0x99,0x4a}};
static const uint16_t callbacks[] = {
    0x1bac,0x1be4,0x1be8,0x1d08,0x1df8,0x1e18,0x1e1c,
    0x1ef4,0x1f84,0x2004,0x2008,0x209c,0x20a4,0x20ac
};
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
static efi_status refuse(struct efi_system_table *st, const char *s)
{
    out(st, "REFUSED: "); out(st, s);
    out(st, "\nNo vendor callbacks called. Returning to shell.\n");
    return EFI_UNSUPPORTED;
}
static uint64_t fingerprint(const uint8_t *p, uint64_t size)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    for (uint64_t n = 0; n < size; n++) h = (h ^ p[n]) * UINT64_C(0x100000001b3);
    return h;
}
static uint8_t *find_driver(struct efi_system_table *st)
{
    efi_handle *handles = NULL;
    uint64_t count = 0;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &loaded_guid, NULL, &count, &handles)) return NULL;
    uint8_t *found = NULL;
    unsigned matches = 0;
    for (uint64_t n = 0; n < count; n++) {
        struct efi_loaded_image *li = NULL;
        if (st->boot->handle_protocol(handles[n], &loaded_guid, (void **)&li) || !li ||
            !li->image_base || li->image_size != POWER_IMAGE_SIZE) continue;
        uint8_t *base = li->image_base;
        if (fingerprint(base + 0x1000, 0x2000) == POWER_CODE_FNV) {
            found = base;
            matches++;
        }
    }
    st->boot->free_pool(handles);
    return matches == 1 ? found : NULL;
}
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

efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    (void)image;
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    out(st, "q1n1 A16 BIOS312 Type-C cached-state probe v1\n"
            "Read-only RAM snapshot; no vendor callbacks or hardware writes.\n");
    uint8_t *driver = find_driver(st);
    if (!driver) return refuse(st, "Matching BIOS312 UsbPwrCtrlDxe code not found.");
    efi_handle *handles = NULL;
    uint64_t count = 0, matches = 0;
    if (st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &power_guid, NULL, &count, &handles))
        return refuse(st, "USB power-control protocol absent.");
    for (uint64_t n = 0; n < count; n++) {
        void *p = NULL;
        if (!st->boot->handle_protocol(handles[n], &power_guid, &p) && p == driver + POWER_INTERFACE) matches++;
    }
    st->boot->free_pool(handles);
    if (!matches) return refuse(st, "Installed power interface is not the inspected static object.");
    const uintptr_t *protocol = (const uintptr_t *)(driver + POWER_INTERFACE);
    if (protocol[0] != 0x10004) return refuse(st, "Power protocol revision mismatch.");
    for (unsigned n = 0; n < sizeof(callbacks) / sizeof(callbacks[0]); n++)
        if (protocol[n + 1] != (uintptr_t)(driver + callbacks[n]))
            return refuse(st, "Power protocol callback layout mismatch.");
    uint64_t key = 0;
    uint32_t version = 0;
    map_size = sizeof(memory_map);
    if (st->boot->get_memory_map(&map_size, memory_map, &key, &descriptor_size, &version) ||
        descriptor_size < 40 || descriptor_size > sizeof(memory_map) ||
        map_size > sizeof(memory_map) || map_size % descriptor_size)
        return refuse(st, "Could not validate firmware RAM bounds.");
    unsigned ports = *(volatile uint8_t *)(driver + PORT_COUNT_RVA);
    uintptr_t table = *(volatile uintptr_t *)(driver + PORT_TABLE_RVA);
    number(st, "Firmware port count: ", ports);
    if (!ports || ports > 6 || !readable_ram(table, 7))
        return refuse(st, "Port count or port-list allocation is not valid.");
    unsigned ids[6], seen = 0;
    for (unsigned n = 0; n < ports; n++) {
        unsigned id = *(volatile uint8_t *)(table + 1 + n);
        if (id > 5 || (seen & (1u << id)) || !readable_ram(table, (uint64_t)id * PORT_STRIDE + 0x40))
            return refuse(st, "Port ID or configuration allocation is not valid.");
        seen |= 1u << id;
        ids[n] = id;
    }
    unsigned ready = *(volatile uint8_t *)(driver + CACHE_READY_RVA);
    number(st, "Firmware cached-status ready flag: ", ready);
    out(st, "Port IDs below are power-driver IDs; connector mapping is unconfirmed.\n");
    for (unsigned n = 0; n < ports; n++) {
        unsigned id = ids[n];
        const volatile uint8_t *record = (const volatile uint8_t *)(table + id * PORT_STRIDE);
        uint32_t backend = *(const volatile uint32_t *)(record + 0x30);
        uint32_t alternate = *(const volatile uint32_t *)(record + 0x34);
        uint32_t a = *(volatile uint32_t *)(driver + CACHE_RVA + id * 16);
        uint32_t b = *(volatile uint32_t *)(driver + CACHE_RVA + id * 16 + 4);
        number(st, "POWER PORT: ", id);
        number(st, "  Type-C status backend: ", backend);
        number(st, "  Alternate backend: ", alternate);
        number(st, "  Raw cached status: ", a | ((uint64_t)b << 32));
        number(st, "  Raw cached extra: ", *(volatile uint64_t *)(driver + CACHE_RVA + id * 16 + 8));
        number(st, "  Raw orientation byte: ", *(volatile uint8_t *)(driver + 0x3608 + id));
        /* Decode only the backend-6 cache path observed at 0x247c/0x24dc.
         * Backend 2 would query another protocol; this app never does so.
         * The firmware may refresh this RAM asynchronously; not an atomic
         * snapshot and not proof of physical PD negotiation or USB transfers. */
        if (!ready || (backend != 6 && alternate != 6)) {
            out(st, "  Cache decode unavailable for this backend/state.\n");
        } else if (!(a & (1u << 24))) {
            out(st, "  Cached connection: disconnected\n");
        } else {
            out(st, a & (1u << 25) ? "  Cached power role: source\n" : "  Cached power role: sink\n");
            out(st, "  USB data role is not independently established by this snapshot.\n");
        }
    }
    out(st, "Cached state can be stale; this does not prove cable enumeration.\n"
            "Snapshot complete. Returning to shell.\n");
    return 0;
}
