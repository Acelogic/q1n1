/* SPDX-License-Identifier: MIT */
/* Battery contract: decoding and formatting (battery.c) plus the allow-listed
 * PMIC GLINK read in ucsi-console.c, against a mocked firmware callback.
 * Uses the BIOS312 PmicGlinkDxe fixture for the driver bytes (ready flag,
 * cached remote status), like test-a16-ucsi-status.c. */
#define main register_fixture_main
#define efi_main ucsi_register_preflight
#include "test-a16-ucsi-reg.c"
#undef main
#undef efi_main
#define Q1N1_UCSI_REG_EMBEDDED
#define Q1N1_UCSI_STATUS_HOST_TEST
#include "../platform/uefi/ucsi-console.c"
#include "../platform/uefi/battery.c"

static unsigned battery_checks;
#define CHECK(x) do { battery_checks++; if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); exit(1); } } while (0)

efi_status mock_control_write(uint64_t cmd) { (void)cmd; return 0; }

/* A 73% pack, charging at 45 W on AC: mWh units, 70 Wh design. */
static uint8_t bst_reply[16], bix_reply[0x44], psr_reply[4];
static unsigned reads, mode, sample;
enum { NORMAL, NO_COPY, GUARD_OVERRUN, REMOTE_ERROR, LINK_DOWN, FLAPPING, STALE_PREFIX, RATE_JITTER, FAIL };
static void put32(uint8_t *p, unsigned index, uint32_t v)
{ p += 4 * index; p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }

efi_status mock_property_read(uint32_t reg, uint8_t *data, uint32_t size)
{
    reads++;
    sample++;
    if (mode == FAIL) return EFI_ERROR(7);
    if (mode == NO_COPY) return 0;
    const uint8_t *src = reg == Q1N1_PMGK_BST ? bst_reply : reg == Q1N1_PMGK_BIX ? bix_reply : psr_reply;
    for (unsigned i = 0; i < size; i++) data[i] = src[i];
    if (mode == FLAPPING && (sample & 1)) data[8] ^= 1;          /* remaining capacity moves */
    if (mode == RATE_JITTER && (sample & 1)) data[4] ^= 1;       /* rate moves: tolerated for BST */
    if (mode == GUARD_OVERRUN) data[size] = 0;
    if (mode == REMOTE_ERROR) bios[0x4950] = 1;
    if (mode == LINK_DOWN) bios[0x43e1] = 0;
    return 0;
}

static void fixture(void)
{
    memset(bst_reply, 0, sizeof(bst_reply));
    memset(bix_reply, 0, sizeof(bix_reply));
    memset(psr_reply, 0, sizeof(psr_reply));
    put32(bst_reply, 0, Q1N1_BATTERY_CHARGING);
    put32(bst_reply, 1, 45000);
    put32(bst_reply, 2, 51100);
    put32(bst_reply, 3, 17120);
    put32(bix_reply, 0, 1);         /* revision */
    put32(bix_reply, 1, 0);         /* mW/mWh */
    put32(bix_reply, 2, 75000);     /* design */
    put32(bix_reply, 3, 70000);     /* last full charge */
    put32(bix_reply, 4, 1);         /* rechargeable */
    put32(bix_reply, 5, 15480);     /* design voltage */
    put32(bix_reply, 8, 12);        /* cycles */
    put32(psr_reply, 0, 1);
    memset(bios, 0, sizeof(bios));
    bios[0x43e1] = 1;
    console_driver = bios;
    previous_length = 0;
    mode = NORMAL;
    sample = 0;
}

static efi_status read_all(uint8_t *bst, uint8_t *bix, uint8_t *psr)
{
    efi_status s = q1n1_pmgk_read(0, Q1N1_PMGK_PSR, psr, Q1N1_PMGK_PSR_BYTES);
    if (!s) s = q1n1_pmgk_read(0, Q1N1_PMGK_BIX, bix, Q1N1_PMGK_BIX_BYTES);
    if (!s) s = q1n1_pmgk_read(0, Q1N1_PMGK_BST, bst, Q1N1_PMGK_BST_BYTES);
    return s;
}

int main(void)
{
    uint8_t bst[16], bix[0x44], psr[4];
    struct q1n1_battery b;
    char line[160];

    /* Transport: the three registers, each read twice, and decoded. */
    fixture();
    CHECK(!read_all(bst, bix, psr) && reads == 6);
    CHECK(q1n1_battery_decode(bst, bix, psr, &b));
    CHECK(b.percent == 73 && b.ac == 1 && b.state == Q1N1_BATTERY_CHARGING && b.full == 70000);
    q1n1_battery_format(&b, line, sizeof(line));
    CHECK(!strcmp(line, "73% charging on AC, 45000 mW in, ~0h25 to full, 51100/70000 mWh, 17120 mV, 12 cycles"));

    /* Allow list: nothing else, and only the exact sizes. */
    fixture();
    CHECK(q1n1_pmgk_read(0, 0x20104, bst, 4) == EFI_UNSUPPORTED);
    CHECK(q1n1_pmgk_read(0, Q1N1_PMGK_BST, bst, 8) == EFI_UNSUPPORTED);
    CHECK(q1n1_pmgk_read(0, Q1N1_PMGK_BIX, bix, 64) == EFI_UNSUPPORTED);
    CHECK(reads == 6);
    console_driver = 0;
    CHECK(q1n1_pmgk_read(0, Q1N1_PMGK_PSR, psr, 4) == EFI_NOT_FOUND);

    /* Refusals: no copy, guard overrun, remote error, link down, failed read,
     * a sample pair that disagrees, and a stale copy of the previous reply. */
    const unsigned failing[] = { NO_COPY, GUARD_OVERRUN, REMOTE_ERROR, LINK_DOWN, FAIL };
    for (unsigned n = 0; n < sizeof(failing) / sizeof(failing[0]); n++) {
        fixture();
        mode = failing[n];
        CHECK(read_all(bst, bix, psr) != 0);
    }
    fixture();
    CHECK(!q1n1_pmgk_read(0, Q1N1_PMGK_PSR, psr, 4));
    mode = FLAPPING;
    CHECK(q1n1_pmgk_read(0, Q1N1_PMGK_BST, bst, 16) == EFI_ERROR(7));
    fixture();
    mode = RATE_JITTER;
    CHECK(!q1n1_pmgk_read(0, Q1N1_PMGK_BST, bst, 16));          /* rate may move */
    fixture();
    /* BST whose first 16 bytes equal the previous BIX reply: a stale cache copy. */
    memcpy(bst_reply, bix_reply, 16);
    CHECK(!q1n1_pmgk_read(0, Q1N1_PMGK_BIX, bix, 0x44));
    CHECK(q1n1_pmgk_read(0, Q1N1_PMGK_BST, bst, 16) == EFI_ERROR(7));

    /* A 4-byte _PSR equal to the previous reply's first dword is not refused
     * by the transport (coincidence is likely); the decoder bounds it. */
    fixture();
    CHECK(!q1n1_pmgk_read(0, Q1N1_PMGK_BIX, bix, 0x44) && !q1n1_pmgk_read(0, Q1N1_PMGK_PSR, psr, 4));

    /* Decoder plausibility. */
    fixture();
    CHECK(!read_all(bst, bix, psr));
    struct { unsigned index; uint32_t value; int which; } bad[] = {
        {0, 0x13, 0},       /* state bits above the low nibble */
        {0, 3, 0},          /* charging and discharging at once */
        {2, 90000, 0},      /* remaining far above full */
        {3, 1000, 0},       /* 1 V pack */
        {1, 2, 1},          /* unknown power unit */
        {2, 0, 1},          /* no design capacity */
        {3, 200000, 1},     /* full above twice design */
        {0, 2, 2},          /* _PSR other than 0/1 */
    };
    for (unsigned n = 0; n < sizeof(bad) / sizeof(bad[0]); n++) {
        uint8_t s[16], x[0x44], p[4];
        memcpy(s, bst, 16); memcpy(x, bix, 0x44); memcpy(p, psr, 4);
        put32(bad[n].which == 0 ? s : bad[n].which == 1 ? x : p, bad[n].index, bad[n].value);
        CHECK(!q1n1_battery_decode(s, x, p, &b));
    }
    /* Discharging on battery with a time-left estimate; unknown rate/voltage. */
    put32(bst, 0, Q1N1_BATTERY_DISCHARGING); put32(psr, 0, 0); put32(bst, 1, 12000);
    CHECK(q1n1_battery_decode(bst, bix, psr, &b) && b.percent == 73);
    q1n1_battery_format(&b, line, sizeof(line));
    CHECK(!strcmp(line, "73% discharging on battery, 12000 mW out, ~4h15 left, 51100/70000 mWh, 17120 mV, 12 cycles"));
    put32(bst, 1, 0xffffffff); put32(bst, 3, 0xffffffff);
    CHECK(q1n1_battery_decode(bst, bix, psr, &b));
    q1n1_battery_format(&b, line, sizeof(line));
    CHECK(!strcmp(line, "73% discharging on battery, 51100/70000 mWh, 12 cycles"));
    /* mAh units, full charge, critical flag, and a truncated buffer. */
    put32(bix, 1, 1); put32(bix, 2, 5000); put32(bix, 3, 4800); put32(bst, 2, 4800);
    put32(bst, 0, Q1N1_BATTERY_CRITICAL); put32(psr, 0, 1); put32(bst, 3, 17000);
    CHECK(q1n1_battery_decode(bst, bix, psr, &b) && b.percent == 100);
    q1n1_battery_format(&b, line, sizeof(line));
    CHECK(!strcmp(line, "100% idle CRITICAL on AC, 4800/4800 mAh, 17000 mV, 12 cycles"));
    char tiny[8];
    CHECK(q1n1_battery_format(&b, tiny, sizeof(tiny)) == 7 && !strcmp(tiny, "100% id"));

    printf("PASS: %u battery contract checks (decode, format, allow-listed PMIC GLINK reads)\n", battery_checks);
    return 0;
}
