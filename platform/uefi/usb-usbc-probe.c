/* SPDX-License-Identifier: MIT */
/* BIOS312's USBCReadBuffer fetches 132 bytes using GLINK owner 0x800c,
 * opcode 0x14. Share the exact driver guards with the UCSI diagnostic.
 * No role, reset, connect or write callback is used. */
#define Q1N1_USBC_BUFFER_PROBE
#include "usb-ucsi-probe.c"
