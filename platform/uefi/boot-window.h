/* SPDX-License-Identifier: MIT */
/* q1n1 A16 boot window: firmware USB0 CDC ACM command channel before EBS. */
#pragma once
#include "usb-ebs.h"

#define Q1N1_BOOT_WINDOWS 0u /* return 0: startup.nsh continues to Windows */
#define Q1N1_BOOT_PROXY 1u   /* snapshot valid, firmware USB stopped: leave firmware */
#define Q1N1_BOOT_SHELL 2u   /* return nonzero: startup.nsh stops at the shell */
/* No USB0 host answered, so there is no console to take over: leave firmware
 * without touching the device-mode controller and serve the proxy over the NCM
 * link instead. Only reachable with --auto. */
#define Q1N1_BOOT_PROXY_NCM 3u

/* In automatic mode, a configured USB0 link must not fall through to Windows
 * just because no host process claimed the boot window in 25 seconds. */
static inline uint32_t q1n1_timeout_choice(int automatic, int usb0_configured)
{
    if (!automatic) return Q1N1_BOOT_WINDOWS;
    return usb0_configured ? Q1N1_BOOT_PROXY : Q1N1_BOOT_PROXY_NCM;
}

struct q1n1_boot_result {
    uint32_t choice, return_armed;
    uint16_t boot_current, reserved16;
    uint32_t configured;
    efi_status entry_status, arm_status;
    struct usb_ebs_snapshot snapshot;
};

/* Returns 0 with result->choice set, or an error (USB stopped, nothing armed). */
/* `automatic` picks q1n1 on timeout with a configured USB0 host or through
 * direct-link discovery when USB0 has no host. */
efi_status q1n1_boot_window(efi_handle image, struct efi_system_table *st,
                            struct q1n1_boot_result *result, int automatic);
