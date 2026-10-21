/* SPDX-License-Identifier: MIT */
/* Step 1 of the post-ExitBootServices USB serial test (boot services only).
 * Uses the physically proven serial-v3 path to start USB0 in device mode with
 * firmware's USB Function driver, waits for host enumeration, snapshots DWC3
 * and qscratch registers read-only, stops the firmware driver, and stores the
 * snapshot in a volatile variable for q1n1-usb-ebs.efi. Returns to the shell. */
#define Q1N1_ALLOW_INACTIVE_USB0
#define Q1N1_CDC_LIBRARY
#include "usb-serial.c"
#include "usb-ebs.h"

static uint32_t mmio(uint64_t address) { return *(volatile uint32_t *)(uintptr_t)address; }
static void capture(struct efi_system_table *st, const char *label, uint32_t regs[], uint32_t qs[])
{
    for (unsigned n = 0; n < USB_EBS_REG_COUNT; n++) regs[n] = mmio(USB_EBS_DWC3_BASE + usb_ebs_regs[n]);
    for (unsigned n = 0; n < USB_EBS_QSCRATCH_COUNT; n++) qs[n] = mmio(USB_EBS_QSCRATCH_BASE + 4 * n);
    out(st, label);
    for (unsigned n = 0; n < USB_EBS_REG_COUNT; n++) { number(st, "  DWC3 offset: ", usb_ebs_regs[n]); number(st, "    value: ", regs[n]); }
    for (unsigned n = 0; n < USB_EBS_QSCRATCH_COUNT; n++) { number(st, "  QSCRATCH offset: ", 4 * n); number(st, "    value: ", qs[n]); }
}

efi_status efi_main(efi_handle image, struct efi_system_table *st)
{
    if (!st || !st->boot || !st->console_out || !st->runtime_services) return EFI_UNSUPPORTED;
    out(st, "q1n1 A16 USB0 EL2 takeover prep v1 (firmware enumeration + snapshot)\n");
    struct efi_loaded_image *self = NULL;
    if (st->boot->handle_protocol(image, &loaded_guid, (void **)&self) || !self) return EFI_UNSUPPORTED;
    if (device_option(self) != 1) { out(st, "Use --device0 to run the firmware USB0 prep.\n"); return EFI_INVALID_PARAMETER; }
    struct efi_runtime_services *rt = st->runtime_services;
    uint64_t existing = 0;
    if (rt->get_variable(usb_ebs_variable, &usb_ebs_guid, NULL, &existing, NULL) != EFI_NOT_FOUND) {
        out(st, "REFUSED: a takeover snapshot variable already exists in this boot.\n");
        return EFI_UNSUPPORTED;
    }
    uint8_t *config = find_firmware(st, CONFIG_IMAGE_SIZE, CONFIG_CODE_RVA, CONFIG_CODE_SIZE, CONFIG_CODE_FNV);
    if (!config || *(const uint64_t *)(config + USB_EBS_CONFIG_DWC3_TABLE_RVA) != USB_EBS_DWC3_BASE ||
        *(const uint64_t *)(config + USB_EBS_CONFIG_QSCRATCH_TABLE_RVA) != USB_EBS_QSCRATCH_BASE) {
        out(st, "REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.\n");
        return EFI_UNSUPPORTED;
    }
    out(st, "UsbConfigDxe table: USB0 core 0x0A600000, qscratch 0x0A6F8800.\n");
    efi_status status = prepare_usb0(image, st, 1);
    if (status) return status;
    struct cdc c = {0};
    c.fn = checked_usbfn(st);
    if (!c.fn) { out(st, "REFUSED: USB Function driver code or ABI differs from BIOS312.\n"); return EFI_UNSUPPORTED; }
    c.speed = USB_HIGH; c.greeting = 1;
    const uint8_t line[] = {0, 0xc2, 1, 0, 0, 0, 8}; copy_bytes(c.line_coding, line, 7);
    uint8_t **buffers[] = {&c.control, &c.rx, &c.tx, &c.notification};
    unsigned allocated = 0;
    status = st->boot->set_watchdog(180, 0, 0, NULL);
    int watchdog = !status;
    for (; !status && allocated < 4; allocated++) {
        status = c.fn->allocate(c.fn, DMA_SIZE, (void **)buffers[allocated]);
        if (status) break;
    }
    int started = 0;
    if (!status) { started = 1; status = c.fn->start(c.fn); }
    if (!status) status = c.fn->configure_ex(c.fn, &device_info, &ss_device);
    number(st, "USBFn initialization status: ", status);
    static struct usb_ebs_snapshot snap;
    snap.magic = USB_EBS_MAGIC; snap.size = sizeof(snap);
    snap.dwc3_base = USB_EBS_DWC3_BASE; snap.qscratch_base = USB_EBS_QSCRATCH_BASE;
    if (!status) {
        out(st, "Waiting up to 60 s for the Mac to configure 1209:316D / A16-Q1N1-UEFI.\n");
        efi_status (*stall)(uint64_t) = (efi_status (*)(uint64_t))st->boot->stall;
        uint64_t frequency; __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(frequency));
        uint64_t start = 0; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(start));
        uint64_t configured_at = 0;
        for (;;) {
            uint64_t now; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(now));
            if (!frequency || now - start > frequency * 60) break;
            if (configured_at && now - configured_at > frequency * 3) break; /* let the host driver settle */
            union usb_payload payload = {0}; uint64_t size = sizeof(payload); uint32_t event = 0;
            status = c.fn->event(c.fn, &event, &size, &payload);
            if (status == NOT_READY) { status = 0; event = USB_NONE; }
            if (status) break;
            if (event == USB_SPEED) snap.firmware_speed = payload.speed;
            status = event_step(&c, event, &payload, size); if (status) break;
            if (c.configured && !configured_at) { configured_at = now; out(st, "Firmware USB configuration: 1\n"); }
            status = service(&c); if (status) break;
            if (event == USB_NONE) stall(1000);
        }
        if (c.configured) snap.flags |= USB_EBS_FIRMWARE_CONFIGURED;
    }
    number(st, "Firmware enumeration loop status: ", status);
    number(st, "Firmware setup requests: ", c.setups);
    number(st, "Firmware bus speed: ", snap.firmware_speed);
    snap.firmware_setups = c.setups; snap.firmware_resets = c.resets;
    if (!status) capture(st, "Snapshot while firmware USB0 runs:\n", snap.running, snap.qs_running);
    if (started) {
        efi_status stopped = c.fn->stop(c.fn);
        number(st, "USBFn stop status: ", stopped);
        if (stopped) {
            out(st, "STOP FAILED. Keeping DMA buffers and this app resident. Power-cycle the A16.\n");
            for (;;) __asm__ volatile("wfe");
        }
        snap.flags |= USB_EBS_FIRMWARE_STOPPED;
    }
    for (unsigned n = 0; n < allocated; n++) c.fn->free(c.fn, *buffers[n]);
    if (!status) capture(st, "Snapshot after firmware USB0 stop:\n", snap.stopped, snap.qs_stopped);
    if (watchdog) st->boot->set_watchdog(0, 0, 0, NULL);
    if (status) { out(st, "Prep failed; no snapshot stored. Do not run the takeover.\n"); return status; }

    uint32_t id = usb_ebs_reg(snap.running, 0xc120), gctl = usb_ebs_reg(snap.running, 0xc110);
    if (!id || id == 0xffffffff || id != usb_ebs_reg(snap.stopped, 0xc120) || ((gctl >> 12) & 3) != 2 ||
        !(snap.qs_running[0x10 / 4] & (1u << 20)) || !(snap.flags & USB_EBS_FIRMWARE_CONFIGURED)) {
        number(st, "GSNPSID running: ", id);
        number(st, "GCTL running: ", gctl);
        number(st, "HS_PHY_CTRL running: ", snap.qs_running[0x10 / 4]);
        out(st, "REFUSED: firmware state is not a configured device-mode DWC3; no snapshot stored.\n");
        return EFI_UNSUPPORTED;
    }
    snap.checksum = usb_ebs_fnv((const uint8_t *)&snap, offsetof(struct usb_ebs_snapshot, checksum));
    status = rt->set_variable(usb_ebs_variable, &usb_ebs_guid, EFI_VARIABLE_BOOTSERVICE_ACCESS, sizeof(snap), &snap);
    number(st, "Volatile snapshot variable status: ", status);
    number(st, "Snapshot checksum: ", snap.checksum);
    out(st, status ? "Prep failed to store the snapshot.\n" :
                     "Prep complete. Next: q1n1-usb-ebs.efi --takeover (leaves firmware).\n");
    return status;
}
