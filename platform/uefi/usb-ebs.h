/* SPDX-License-Identifier: MIT */
/* Hand-off between q1n1-usb-ebs-prep.efi (firmware USB0 bring-up and register
 * snapshot, returns to the shell) and q1n1-usb-ebs.efi (ExitBootServices and
 * q1n1-owned DWC3 takeover). Stored only in a volatile boot-services variable. */
#pragma once
#include "efi.h"
#include "qcom-dwc3.h"

#define USB_EBS_DWC3_BASE UINT64_C(0x0a600000)
#define USB_EBS_QSCRATCH_BASE UINT64_C(0x0a6f8800)
#define USB_EBS_CONFIG_DWC3_TABLE_RVA 0xc5d0 /* UsbConfigDxe BIOS312: USB0 core base */
#define USB_EBS_CONFIG_QSCRATCH_TABLE_RVA 0xc5f8 /* UsbConfigDxe BIOS312: USB0 qscratch */
#define USB_EBS_MAGIC UINT64_C(0x3130534245314e31) /* "1N1EBS01" */
#define EFI_VARIABLE_BOOTSERVICE_ACCESS 2
#define EFI_ALLOCATE_MAX_ADDRESS 1

static const efi_guid usb_ebs_guid = {0x7d1f0a2c,0x55b3,0x4c1e,{0x9a,0x6f,0x31,0x4e,0x31,0x51,0x55,0x42}};
static const char16 usb_ebs_variable[] = u"Q1N1Usb0Takeover";

/* DWC3 offsets captured while firmware runs and again after it stops. */
static const uint16_t usb_ebs_regs[] = {
    0xc100, 0xc104, 0xc108, 0xc10c, 0xc110, 0xc114, 0xc118, 0xc11c, 0xc120, 0xc124, 0xc128,
    0xc12c, 0xc140, 0xc144, 0xc148, 0xc14c, 0xc150, 0xc154, 0xc158, 0xc15c, 0xc19c, 0xc1a0,
    0xc1a4, 0xc200, 0xc2c0, 0xc400, 0xc404, 0xc408, 0xc40c, 0xc600, 0xc630, 0xc700, 0xc704,
    0xc708, 0xc70c, 0xc720,
};
#define USB_EBS_REG_COUNT (sizeof(usb_ebs_regs) / sizeof(usb_ebs_regs[0]))
#define USB_EBS_QSCRATCH_COUNT 26 /* qscratch offsets 0x00..0x64 */

struct efi_runtime_services {
    efi_header header;
    void *get_time, *set_time, *get_wakeup_time, *set_wakeup_time, *set_virtual_address_map, *convert_pointer;
    efi_status (*get_variable)(const char16 *, const efi_guid *, uint32_t *, uint64_t *, void *);
    void *get_next_variable_name;
    efi_status (*set_variable)(const char16 *, const efi_guid *, uint32_t, uint64_t, void *);
    void *get_next_high_monotonic_count;
    void (*reset_system)(uint32_t, efi_status, uint64_t, void *);
};
_Static_assert(offsetof(struct efi_runtime_services, get_variable) == 0x48, "GetVariable ABI");
_Static_assert(offsetof(struct efi_runtime_services, set_variable) == 0x58, "SetVariable ABI");
_Static_assert(offsetof(struct efi_runtime_services, reset_system) == 0x68, "ResetSystem ABI");

#define USB_EBS_FIRMWARE_CONFIGURED 1u
#define USB_EBS_FIRMWARE_STOPPED 2u

struct usb_ebs_snapshot {
    uint64_t magic;
    uint32_t size, flags;
    uint64_t dwc3_base, qscratch_base;
    uint64_t firmware_setups, firmware_resets;
    uint32_t firmware_speed, reserved;
    uint32_t running[USB_EBS_REG_COUNT], stopped[USB_EBS_REG_COUNT];
    uint32_t qs_running[USB_EBS_QSCRATCH_COUNT], qs_stopped[USB_EBS_QSCRATCH_COUNT];
    uint64_t checksum; /* FNV-1a of every preceding byte; detects stale/foreign data. */
};

static inline uint64_t usb_ebs_fnv(const uint8_t *p, uint64_t n)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    while (n--) h = (h ^ *p++) * UINT64_C(0x100000001b3);
    return h;
}
static inline uint32_t usb_ebs_reg(const uint32_t values[], uint16_t offset)
{
    for (unsigned n = 0; n < USB_EBS_REG_COUNT; n++) if (usb_ebs_regs[n] == offset) return values[n];
    return 0;
}
static inline int usb_ebs_valid(const struct usb_ebs_snapshot *s, uint64_t size)
{
    return size == sizeof(*s) && s->magic == USB_EBS_MAGIC && s->size == sizeof(*s) &&
           s->dwc3_base == USB_EBS_DWC3_BASE && s->qscratch_base == USB_EBS_QSCRATCH_BASE &&
           s->checksum == usb_ebs_fnv((const uint8_t *)s, offsetof(struct usb_ebs_snapshot, checksum));
}
static inline void usb_ebs_saved(const struct usb_ebs_snapshot *s, struct qdwc3_saved *out)
{
    const uint32_t *r = s->running;
    *out = (struct qdwc3_saved){
        .gsbuscfg0 = usb_ebs_reg(r, 0xc100), .gsbuscfg1 = usb_ebs_reg(r, 0xc104), .gctl = usb_ebs_reg(r, 0xc110),
        .guctl = usb_ebs_reg(r, 0xc12c), .guctl1 = usb_ebs_reg(r, 0xc11c), .guctl2 = usb_ebs_reg(r, 0xc19c),
        .gusb2phycfg = usb_ebs_reg(r, 0xc200), .gusb3pipectl = usb_ebs_reg(r, 0xc2c0),
        .gfladj = usb_ebs_reg(r, 0xc630), .dcfg = usb_ebs_reg(r, 0xc700), .gsnpsid = usb_ebs_reg(r, 0xc120)};
}
