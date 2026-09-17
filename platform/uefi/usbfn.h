/* SPDX-License-Identifier: MIT */
/* Minimal AArch64 ABI declarations derived from the public UEFI protocol.
 * https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Protocol/UsbFunctionIo.h
 * Qualcomm revision 0x10003 adds ConfigureEnableEndpointsEx at offset 0x90:
 * https://github.com/Project-Silicium/Mu-Silicium/blob/main/Silicon/Qualcomm/QcomPkg/Include/Protocol/EFIUsbfnIo.h
 */
#pragma once
#include "efi.h"
struct usb_setup { uint8_t type, request; uint16_t value, index, length; } __attribute__((packed));
struct usb_interface_info { void *descriptor; void **endpoints; };
struct usb_config_info { void *descriptor; struct usb_interface_info **interfaces; };
struct usb_device_info { void *descriptor; struct usb_config_info **configs; };
struct usb_ss_endpoint { void *descriptor, *companion; };
struct usb_ss_interface { void *descriptor; struct usb_ss_endpoint **endpoints; };
struct usb_ss_config { void *descriptor; struct usb_ss_interface **interfaces; };
struct usb_ss_device { void *descriptor; struct usb_ss_config **configs; void *bos; };
struct usb_transfer_result {
    uint64_t bytes;
    uint32_t status;
    uint8_t endpoint;
    uint32_t direction;
    void *buffer;
};
union usb_payload { struct usb_setup setup; struct usb_transfer_result transfer; uint32_t speed; };
enum { USB_NONE, USB_SETUP, USB_RX, USB_TX, USB_DETACH, USB_ATTACH, USB_RESET, USB_SUSPEND, USB_RESUME, USB_SPEED };
enum { USB_OUT, USB_IN };
enum { USB_COMPLETE = 1, USB_ABORTED = 2 };
enum { USB_FULL = 2, USB_HIGH = 3, USB_SUPER = 4 };
struct usbfn {
    uint32_t revision;
    void *detect_port;
    efi_status (*configure)(struct usbfn *, struct usb_device_info *);
    efi_status (*max_packet)(struct usbfn *, uint32_t, uint32_t, uint16_t *);
    void *device_info, *vendor_product;
    efi_status (*abort)(struct usbfn *, uint8_t, uint32_t);
    efi_status (*get_stall)(struct usbfn *, uint8_t, uint32_t, uint8_t *);
    efi_status (*set_stall)(struct usbfn *, uint8_t, uint32_t, uint8_t);
    efi_status (*event)(struct usbfn *, uint32_t *, uint64_t *, union usb_payload *);
    efi_status (*transfer)(struct usbfn *, uint8_t, uint32_t, uint64_t *, void *);
    efi_status (*max_transfer)(struct usbfn *, uint64_t *);
    efi_status (*allocate)(struct usbfn *, uint64_t, void **);
    efi_status (*free)(struct usbfn *, void *);
    efi_status (*start)(struct usbfn *);
    efi_status (*stop)(struct usbfn *);
    efi_status (*set_policy)(struct usbfn *, uint8_t, uint32_t, uint32_t, uint64_t, void *);
    void *get_policy;
    efi_status (*configure_ex)(struct usbfn *, struct usb_device_info *, struct usb_ss_device *);
    void *update_ss;
};
_Static_assert(sizeof(struct usb_setup) == 8, "USB request ABI");
_Static_assert(sizeof(struct usb_transfer_result) == 32, "USBFn event ABI");
_Static_assert(offsetof(struct usb_transfer_result, direction) == 16, "USBFn direction ABI");
_Static_assert(offsetof(struct usbfn, event) == 0x48, "USBFn EventHandler ABI");
_Static_assert(offsetof(struct usbfn, transfer) == 0x50, "USBFn Transfer ABI");
_Static_assert(offsetof(struct usbfn, configure_ex) == 0x90, "Qualcomm USBFn extension ABI");
