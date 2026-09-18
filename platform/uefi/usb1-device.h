/* SPDX-License-Identifier: MIT */
#pragma once
#include "qcom-dwc3.h"

/* USB1 device-mode fallback for BIOS312. The PD role is left untouched. Only
 * a powered USB1 host with no connected USB device may be borrowed. */
struct usb1_device {
    struct qdwc3 usb;
    struct qdwc3_saved saved;
    uint32_t qs_hs, qs_ss;
    int active;
};
int usb1_device_start(struct usb1_device *d, uintptr_t base, uint8_t *arena, uint64_t size);
void usb1_device_stop(struct usb1_device *d);
