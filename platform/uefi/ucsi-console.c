/* SPDX-License-Identifier: MIT */
/* See ucsi-console.h. This reuses the physically tested BIOS312 UCSI transport
 * from usb-ucsi-status.c (PmicGlinkDxe fingerprint, guarded property reads,
 * CONTROL writes) and adds a command-at-a-time interface for the boot window.
 * Unlike the one-shot apps it observes UCSI errors instead of refusing on them,
 * because reading the rejection is the point of the exercise. */
#define Q1N1_UCSI_CONSOLE
#define UCSI_STATUS_FULL_BLOCK
#define UCSI_STATUS_VERSION "console"
#define UCSI_STATUS_ENTRY q1n1_ucsi_query_main
#include "usb-ucsi-status.c"
#include "ucsi-console.h"
#include "battery.h"
#define Q1N1_PMGK_MAX Q1N1_PMGK_BIX_BYTES

#define CCI_NOT_SUPPORTED (1u << 25)
#define CCI_CANCEL_COMPLETE (1u << 26)
#define CCI_RESET_COMPLETE (1u << 27)
#define CCI_ERROR (1u << 30)
#define UCSI_PPM_RESET 0x01
#define UCSI_ACK_CC_CI 0x04

static uint8_t *console_driver;

int q1n1_ucsi_ready(void) { return console_driver != 0; }

efi_status q1n1_ucsi_init(efi_handle image, struct efi_system_table *st)
{
    if (console_driver) return 0;
    if (!st->boot->stall) return EFI_UNSUPPORTED;
    efi_status status = ucsi_register_preflight(image, st);
    if (status) return status;
    uint8_t *driver = find_driver(st);
    if (!driver) return EFI_UNSUPPORTED;
    console_driver = driver;
    return 0;
}

/* Wait for any terminal CCI state. Errors are results here, not failures. */
static efi_status wait_settled(struct efi_system_table *st, uint32_t wanted, uint32_t *cci)
{
    const uint32_t terminal = CCI_COMPLETE | CCI_ACK | CCI_ERROR | CCI_NOT_SUPPORTED |
                              CCI_CANCEL_COMPLETE | CCI_RESET_COMPLETE;
    for (unsigned attempt = 0; attempt < 100; attempt++) {
        efi_status status = get_cci(st, console_driver, cci);
        if (status) return status;
        if (!(*cci & CCI_BUSY) && (*cci & (wanted ? wanted : terminal))) return 0;
        status = ((efi_status (*)(uint64_t))st->boot->stall)(20000);
        if (status) return status;
    }
    return EFI_ERROR(18); /* timeout */
}

efi_status q1n1_ucsi_command(struct efi_system_table *st, uint64_t command, struct q1n1_ucsi_result *result)
{
    for (unsigned n = 0; n < sizeof(result->data); n++) result->data[n] = 0;
    result->cci = result->length = result->stable = 0;
    result->status = 0;
    if (!console_driver) return result->status = EFI_NOT_FOUND;

    uint8_t opcode = (uint8_t)command;
    uint32_t cci = 0;
    efi_status status = get_cci(st, console_driver, &cci);
    if (status) return result->status = status;
    /* Clear a completion left by an earlier command so the PPM accepts a new one. */
    if ((cci & (CCI_COMPLETE | CCI_ERROR | CCI_NOT_SUPPORTED)) && opcode != UCSI_ACK_CC_CI) {
        if (!send_control(st, console_driver, 0x20004)) wait_settled(st, CCI_ACK, &cci);
    }
    status = send_control(st, console_driver, command);
    if (status) return result->status = status;

    uint32_t wanted = opcode == UCSI_PPM_RESET ? CCI_RESET_COMPLETE :
                      opcode == UCSI_ACK_CC_CI ? CCI_ACK : 0;
    status = wait_settled(st, wanted, &cci);
    result->cci = cci;
    if (status) return result->status = status;

    unsigned bytes = (cci >> 8) & 255;
    if (bytes > sizeof(result->data)) bytes = sizeof(result->data);
    if (bytes && (cci & CCI_COMPLETE)) {
        uint8_t first[64], second[64];
        status = guarded_read(st, console_driver, 0x20200, first, 64);
        if (!status) status = guarded_read(st, console_driver, 0x20200, second, 64);
        if (!status) {
            result->stable = 1;
            for (unsigned n = 0; n < bytes; n++) {
                result->data[n] = first[n];
                if (first[n] != second[n]) result->stable = 0;
            }
            result->length = bytes;
        }
    }
    /* Acknowledge command completion only; connector-change stays pending. */
    if ((cci & (CCI_COMPLETE | CCI_ERROR | CCI_NOT_SUPPORTED)) && opcode != UCSI_ACK_CC_CI) {
        uint32_t ack = 0;
        if (!send_control(st, console_driver, 0x20004)) wait_settled(st, CCI_ACK, &ack);
    }
    return result->status = status;
}

/* The last accepted PMIC GLINK reply, to refuse a stale copy of it. */
static uint8_t previous_reply[Q1N1_PMGK_MAX];
static unsigned previous_length;

static efi_status pmgk_sample(struct efi_system_table *st, uint32_t reg, uint8_t *data, unsigned size)
{
    uint8_t buffer[8 + Q1N1_PMGK_MAX + 8];
    for (unsigned i = 0; i < sizeof(buffer); i++) buffer[i] = 0xa5;
    if (*(volatile uint8_t *)(console_driver + 0x43e1) != 1) return EFI_ERROR(6);
#ifdef Q1N1_UCSI_STATUS_HOST_TEST
    extern efi_status mock_property_read(uint32_t, uint8_t *, uint32_t);
    efi_status status = mock_property_read(reg, buffer + 8, size);
#else
    typedef efi_status (*read_fn)(uint32_t, void *, uint32_t);
    efi_status status = ((read_fn)(console_driver + 0x263c))(reg, buffer + 8, size);
#endif
    for (unsigned i = 0; i < 8; i++)
        if (buffer[i] != 0xa5 || buffer[8 + size + i] != 0xa5) return EFI_ERROR(7);
    if (status) return status;
    if (little(console_driver + 0x4950, 4)) return EFI_ERROR(7);   /* cached remote error */
    unsigned changed = 0;
    for (unsigned i = 0; i < size; i++) { data[i] = buffer[8 + i]; changed += data[i] != 0xa5; }
    (void)st;
    return changed ? 0 : EFI_ERROR(6);
}

efi_status q1n1_pmgk_read(struct efi_system_table *st, uint32_t reg, uint8_t *data, unsigned size)
{
    if (!console_driver) return EFI_NOT_FOUND;
    if (!((reg == Q1N1_PMGK_BST && size == Q1N1_PMGK_BST_BYTES) ||
          (reg == Q1N1_PMGK_BIX && size == Q1N1_PMGK_BIX_BYTES) ||
          (reg == Q1N1_PMGK_PSR && size == Q1N1_PMGK_PSR_BYTES))) return EFI_UNSUPPORTED;
    uint8_t first[Q1N1_PMGK_MAX], second[Q1N1_PMGK_MAX];
    efi_status status = pmgk_sample(st, reg, first, size);
    if (!status) status = pmgk_sample(st, reg, second, size);
    if (status) return status;
    /* A 4-byte value cannot be told from a coincidental prefix (_PSR 1 equals
     * _BIX's revision 1); the decoder's range checks cover those. */
    unsigned same = 1, stale = size >= 8 && previous_length >= size;
    for (unsigned i = 0; i < size; i++) {
        same &= first[i] == second[i];
        if (i < previous_length) stale &= first[i] == previous_reply[i];
    }
    /* _BST's rate and voltage may move between samples; state and remaining
     * capacity must not, within milliseconds. */
    if (!same && reg == Q1N1_PMGK_BST) {
        same = 1;
        for (unsigned i = 0; i < 4; i++) same &= first[i] == second[i] && first[8 + i] == second[8 + i];
    }
    if (!same || stale) return EFI_ERROR(7);
    for (unsigned i = 0; i < size; i++) data[i] = previous_reply[i] = first[i];
    previous_length = size;
    return 0;
}
