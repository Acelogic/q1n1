/* SPDX-License-Identifier: MIT */
/* Minimal AArch64 UEFI ABI subset. All UINTN and pointer fields are 64 bits. */
#pragma once
#include <stdint.h>
#include <stddef.h>

typedef uint64_t efi_status;
typedef void *efi_handle;
typedef uint16_t char16;
typedef struct { uint32_t a; uint16_t b, c; uint8_t d[8]; } efi_guid;
typedef struct { uint64_t signature; uint32_t revision, size, crc, reserved; } efi_header;
#define EFI_ERROR(n) ((1ull << 63) | (n))
#define EFI_BUFFER_TOO_SMALL EFI_ERROR(5)
#define EFI_INVALID_PARAMETER EFI_ERROR(2)
#define EFI_UNSUPPORTED EFI_ERROR(3)
#define EFI_NOT_FOUND EFI_ERROR(14)
#define EFI_LOADER_DATA 2
#define EFI_BY_PROTOCOL 2

struct efi_text {
    void *reset;
    efi_status (*output)(struct efi_text *, const char16 *);
};
struct efi_input {
    void *reset, *read_key, *wait_key;
};
struct efi_config { efi_guid guid; void *table; };
struct efi_boot_services {
    efi_header header;
    void *raise_tpl, *restore_tpl;
    efi_status (*allocate_pages)(uint32_t, uint32_t, uint64_t, uint64_t *);
    void *free_pages;
    efi_status (*get_memory_map)(uint64_t *, void *, uint64_t *, uint64_t *, uint32_t *);
    void *allocate_pool;
    efi_status (*free_pool)(void *);
    void *create_event, *set_timer, *wait_event;
    void *signal_event, *close_event, *check_event;
    void *install_protocol, *reinstall_protocol, *uninstall_protocol;
    efi_status (*handle_protocol)(efi_handle, efi_guid *, void **);
    void *reserved, *register_notify, *locate_handle, *locate_device_path;
    void *install_config, *load_image, *start_image, *exit, *unload_image;
    efi_status (*exit_boot_services)(efi_handle, uint64_t);
    void *get_next_monotonic_count, *stall;
    efi_status (*set_watchdog)(uint64_t, uint64_t, uint64_t, char16 *);
    void *connect_controller, *disconnect_controller;
    void *open_protocol, *close_protocol, *open_protocol_information;
    void *protocols_per_handle;
    efi_status (*locate_handle_buffer)(uint32_t, efi_guid *, void *, uint64_t *, efi_handle **);
    efi_status (*locate_protocol)(efi_guid *, void *, void **);
};
struct efi_system_table {
    efi_header header;
    char16 *vendor;
    uint32_t revision;
    efi_handle console_in_handle;
    struct efi_input *console_in;
    efi_handle console_out_handle;
    struct efi_text *console_out;
    efi_handle stderr_handle;
    struct efi_text *stderr;
    void *runtime_services;
    struct efi_boot_services *boot;
    uint64_t table_count;
    struct efi_config *tables;
};
struct efi_loaded_image {
    uint32_t revision;
    efi_handle parent;
    struct efi_system_table *system;
    efi_handle device;
    void *path, *reserved;
    uint32_t options_size;
    char16 *options;
    void *image_base;
    uint64_t image_size;
    uint32_t code_type, data_type;
    void *unload;
};
struct efi_gop_info {
    uint32_t version, width, height, format;
    uint32_t red, green, blue, reserved;
    uint32_t stride;
};
struct efi_gop_mode {
    uint32_t max_mode, mode;
    struct efi_gop_info *info;
    uint64_t info_size, framebuffer, framebuffer_size;
};
struct efi_gop { void *query, *set, *blt; struct efi_gop_mode *mode; };
_Static_assert(offsetof(struct efi_boot_services, exit_boot_services) == 232, "UEFI EBS ABI");
_Static_assert(offsetof(struct efi_boot_services, free_pool) == 72, "UEFI FreePool ABI");
_Static_assert(offsetof(struct efi_boot_services, locate_handle_buffer) == 312, "UEFI LocateHandleBuffer ABI");
_Static_assert(offsetof(struct efi_boot_services, locate_protocol) == 320, "UEFI LocateProtocol ABI");
_Static_assert(offsetof(struct efi_system_table, boot) == 96, "UEFI system ABI");
_Static_assert(offsetof(struct efi_loaded_image, options) == 56, "UEFI image ABI");
