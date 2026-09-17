/* SPDX-License-Identifier: MIT */
/* QEMU TEST FIXTURE ONLY. Never install on a physical machine.
 * Deliberately null vendor methods: the passive probe must only read data.
 * This boot-service driver remains resident so the published data stays valid.
 */
#include "efi.h"
static efi_guid guid = {0xe722b03f,0xb250,0x42ce,{0x8e,0xbd,0x5b,0xd5,0x18,0x12,0xd0,0x37}};
static struct config { uint8_t reserved[0x88]; uint32_t selector, mode; } configs[] = {
    {{0}, 0, 1}, {{0}, 1, 4}, {{0}, 6, 4},
};
_Static_assert(offsetof(struct config, selector) == 0x88, "Selector offset");
_Static_assert(offsetof(struct config, mode) == 0x8c, "Mode offset");
efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    (void)image;
    efi_status (*install)(efi_handle *, efi_guid *, uint32_t, void *) =
        (efi_status (*)(efi_handle *, efi_guid *, uint32_t, void *))st->boot->install_protocol;
    for (unsigned i = 0; i < sizeof(configs) / sizeof(configs[0]); i++) {
        efi_handle handle = NULL;
        efi_status status = install(&handle, &guid, 0, &configs[i]);
        if (status) return status;
    }
    return 0;
}
