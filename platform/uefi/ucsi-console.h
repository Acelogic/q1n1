/* SPDX-License-Identifier: MIT */
/* Interactive UCSI access for the q1n1 boot window. The firmware's PmicGlinkDxe
 * register transport (the physically tested path) is driven one command at a
 * time from the Mac, so a data-role experiment costs a line of text instead of
 * a rebuild, an install and a reboot. */
#pragma once
#include "efi.h"

struct q1n1_ucsi_result {
    uint32_t cci;        /* final CCI, including error/not-supported bits */
    uint32_t length;     /* MESSAGE_IN bytes reported by CCI */
    uint32_t stable;     /* the two MESSAGE_IN samples agreed */
    efi_status status;   /* transport status, not the UCSI result */
    uint8_t data[64];
};

/* Fingerprint PmicGlinkDxe and latch its transport. Safe to call repeatedly. */
efi_status q1n1_ucsi_init(efi_handle image, struct efi_system_table *st);
int q1n1_ucsi_ready(void);
/* Send one UCSI command and collect CCI plus any MESSAGE_IN payload. The
 * completion is acknowledged so the PPM returns to idle; connector-change
 * indications are left pending for the platform's own driver. */
efi_status q1n1_ucsi_command(struct efi_system_table *st, uint64_t command, struct q1n1_ucsi_result *result);
