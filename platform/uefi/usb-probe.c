/* SPDX-License-Identifier: MIT */
/* Passive boot-services protocol inventory. No controller methods, MMIO,
 * device connection, NVRAM changes, or ExitBootServices. Always returns.
 * ABI references: EDK2 MdePkg/Include/Protocol/{UsbFunctionIo,SerialIo,
 * Usb2HostController,DevicePath,DevicePathToText}.h.
 */
#include "efi.h"

#ifndef A16_USB_ROLE_PROBE
#define A16_USB_ROLE_PROBE 0
#endif
enum interface_data { NO_DATA, REVISION, A16_CONFIG };

struct path_to_text {
    void *node;
    char16 *(*path)(const void *, uint8_t, uint8_t);
};
static efi_guid path_guid = {0x09576e91,0x6d3f,0x11d2,{0x8e,0x39,0,0xa0,0xc9,0x69,0x72,0x3b}};
static efi_guid text_guid = {0x8b843e20,0x8132,0x4852,{0x90,0xcc,0x55,0x1a,0x4e,0x4a,0x7f,0x1c}};
static struct {
    const char *name;
    efi_guid guid;
    enum interface_data data;
} protocols[] = {
    {"USB FUNCTION", {0x32d2963a,0xfe5d,0x4f30,{0xb6,0x33,0x6e,0x5d,0xc5,0x58,0x03,0xcc}}, 1},
    {"USB2 HOST", {0x3e745226,0x9818,0x45b6,{0xa2,0xac,0xd7,0xcd,0x0e,0x8b,0xa2,0xbc}}, 0},
    {"SERIAL IO", {0xbb25cf6f,0xf1d4,0x11d2,{0x9a,0x0c,0,0x90,0x27,0x3f,0xc1,0xfd}}, 1},
#if A16_USB_ROLE_PROBE
    /* UX3607OA.312: both UsbfnDwc3Dxe and XhciPciEmulation Supported()
     * open this protocol and read two UINT32s at offsets 0x88 and 0x8c.
     * See docs/A16-USB-BINDING.md for exact binary hashes and evidence. */
    {"QCOM USB CONFIG", {0xe722b03f,0xb250,0x42ce,{0x8e,0xbd,0x5b,0xd5,0x18,0x12,0xd0,0x37}}, A16_CONFIG},
#endif
};

static void out(struct efi_system_table *st, const char *s)
{
    char16 chunk[120];
    while (*s) {
        unsigned i = 0;
        while (*s && i < 117) {
            if (*s == '\n') chunk[i++] = '\r';
            chunk[i++] = (uint8_t)*s++;
        }
        chunk[i] = 0;
        st->console_out->output(st->console_out, chunk);
    }
}
static void hex(struct efi_system_table *st, uint64_t value)
{
    char buf[19];
    buf[0] = '0'; buf[1] = 'x';
    for (unsigned i = 0; i < 16; i++)
        buf[i + 2] = "0123456789ABCDEF"[(value >> (60 - i * 4)) & 15];
    buf[18] = 0;
    out(st, buf);
}
static void path(struct efi_system_table *st, struct path_to_text *convert, efi_handle handle)
{
    void *device_path = NULL;
    efi_status status = st->boot->handle_protocol(handle, &path_guid, &device_path);
    if (status || !device_path || !convert) {
        out(st, "    Device path unavailable (or no text converter).\n");
        return;
    }
    char16 *text = convert->path(device_path, 0, 0);
    if (!text) { out(st, "    Device path conversion failed.\n"); return; }
    out(st, "    Path: ");
    /* Bound console output for an unexpectedly long firmware path. */
    char16 chunk[120];
    unsigned total = 0;
    while (total < 2048 && text[total]) {
        unsigned n = 0;
        while (total < 2048 && n < 119 && text[total]) chunk[n++] = text[total++];
        chunk[n] = 0;
        st->console_out->output(st->console_out, chunk);
    }
    if (total == 2048) out(st, " [truncated]");
    out(st, "\n");
    st->boot->free_pool(text);
}
efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    (void)image;
    if (!st || !st->boot || !st->console_out) return EFI_UNSUPPORTED;
    struct path_to_text *convert = NULL;
    efi_status status = st->boot->locate_protocol(&text_guid, NULL, (void **)&convert);
    if (status) convert = NULL;
    out(st, A16_USB_ROLE_PROBE ? "q1n1 A16 BIOS312 USB role discovery v1\n" :
                               "q1n1 USB protocol discovery v1\n");
    uint64_t el;
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(el));
    out(st, "Entry EL: "); hex(st, el >> 2); out(st, "\n");
    out(st, "Passive inventory; controllers are not started or reconfigured.\n");
    efi_status result = 0;
    for (unsigned p = 0; p < sizeof(protocols) / sizeof(protocols[0]); p++) {
        uint64_t count = 0;
        efi_handle *handles = NULL;
        status = st->boot->locate_handle_buffer(EFI_BY_PROTOCOL, &protocols[p].guid,
                                               NULL, &count, &handles);
        out(st, protocols[p].name);
        if (status == EFI_NOT_FOUND) { out(st, ": not installed\n"); continue; }
        if (status || !handles) {
            out(st, ": enumeration failed "); hex(st, status); out(st, "\n");
            result = status ? status : EFI_INVALID_PARAMETER;
            continue;
        }
        out(st, " handles: "); hex(st, count); out(st, "\n");
        for (uint64_t i = 0; i < count && i < 16; i++) {
            out(st, "  Instance "); hex(st, i); out(st, "\n");
            if (protocols[p].data != NO_DATA) {
                void *interface = NULL;
                status = st->boot->handle_protocol(handles[i], &protocols[p].guid, &interface);
                if (!status && interface) {
                    if (protocols[p].data == REVISION) {
                        out(st, "    Revision: "); hex(st, *(const uint32_t *)interface); out(st, "\n");
                    } else {
                        /* Read data only. No vendor methods, DriverBinding.Supported,
                         * ConnectController, or register accesses are invoked. */
                        const uint8_t *config = interface;
                        uint32_t selector = *(const uint32_t *)(config + 0x88);
                        uint32_t mode = *(const uint32_t *)(config + 0x8c);
                        out(st, "    Config[88]: "); hex(st, selector); out(st, "\n");
                        out(st, "    Config[8C]: "); hex(st, mode); out(st, "\n");
                        out(st, selector > 5 ? "    Outside both drivers' selector range.\n" :
                                mode == 4 ? "    Matches USB function driver's data checks.\n" :
                                mode == 1 ? "    Matches XHCI host driver's data checks.\n" :
                                            "    Matches neither driver's mode check.\n");
                    }
                } else {
                    out(st, "    Interface query failed: "); hex(st, status); out(st, "\n");
                }
            }
            path(st, convert, handles[i]);
        }
        if (count > 16) out(st, "  Remaining instances omitted.\n");
        st->boot->free_pool(handles);
    }
    out(st, "Discovery complete. Returning to shell.\n"
            "Protocol presence does not prove a connected cable or post-EBS USB.\n");
    return result;
}
