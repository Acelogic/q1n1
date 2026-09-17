/* SPDX-License-Identifier: MIT */
/* Step 2: leave firmware and serve CDC ACM on USB0 with q1n1's own DWC3 driver.
 * Requires the volatile snapshot from q1n1-usb-ebs-prep.efi in this boot.
 * Runs a four-minute echo test, shows live counters on the framebuffer, then
 * requests a cold reset (firmware ResetSystem, then PSCI SYSTEM_RESET). */
#include "usb-ebs.h"
#include "fbcon.h"

#define TEST_SECONDS 240

void *memset(void *p, int v, size_t n) { uint8_t *b = p; while (n--) *b++ = (uint8_t)v; return p; }
void *memcpy(void *d, const void *s, size_t n) { uint8_t *a = d; const uint8_t *b = s; while (n--) *a++ = *b++; return d; }

#ifndef USB_EBS_QEMU_FIXTURE
static struct usb_ebs_snapshot snap;
#endif
static struct qdwc3_saved saved;
static struct qdwc3 usb;
static struct efi_runtime_services *rt;
static uint8_t *arena;
static uint64_t hz;
static uintptr_t dwc3_base = USB_EBS_DWC3_BASE, qscratch_base = USB_EBS_QSCRATCH_BASE;
extern void q1n1_enter(void) __attribute__((noreturn));

static uint64_t ticks(void) { uint64_t v; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(v)); return v; }
uint32_t qdwc3_rd(uintptr_t address) { return *(volatile uint32_t *)address; }
void qdwc3_wr(uintptr_t address, uint32_t value) { *(volatile uint32_t *)address = value; }
void qdwc3_delay_us(uint64_t us)
{
    uint64_t start = ticks(), wait = hz * us / 1000000 + 1;
    while (ticks() - start < wait) __asm__ volatile("yield");
}

static void reset_now(void) __attribute__((noreturn));
static void reset_now(void)
{
    fbcon_puts("REQUESTING COLD RESET\n");
    if (rt && rt->reset_system) rt->reset_system(0, 0, 0, NULL);
    fbcon_puts("RESETSYSTEM RETURNED - TRYING PSCI SYSTEM_RESET\n");
    __asm__ volatile("mov x0, #0x0009\n movk x0, #0x8400, lsl #16\n smc #0" ::: "x0", "x1", "x2", "x3", "memory");
    fbcon_puts("RESET FAILED - HOLD POWER TO RESTART\n");
    for (;;) __asm__ volatile("wfe");
}
static void wait_seconds(unsigned seconds)
{
    uint64_t start = ticks();
    while (ticks() - start < hz * seconds) __asm__ volatile("yield");
}

void q1n1_fault(uint64_t esr, uint64_t elr, uint64_t far) __attribute__((noreturn));
void q1n1_fault(uint64_t esr, uint64_t elr, uint64_t far)
{
    fbcon_bar(0xff3040);
    fbcon_at(31, 0);
    fbcon_puts("Q1N1 SYNCHRONOUS EXCEPTION\n");
    fbcon_field("ESR ", esr); fbcon_field("ELR ", elr); fbcon_field("FAR ", far);
    fbcon_field("INIT STEP ", usb.stats.init_step);
    fbcon_puts("RESET IN 60 S\n");
    wait_seconds(60);
    reset_now();
}

static void status(uint64_t elapsed, int result)
{
    const struct qdwc3_stats *s = &usb.stats;
    for (uint32_t row = 14; row < 27; row++) fbcon_clear_row(row);
    fbcon_at(14, 0);
    fbcon_puts("TAKEOVER RESULT "); fbcon_dec((uint64_t)(-(int64_t)result)); fbcon_puts("  INIT STEP "); fbcon_dec(s->init_step);
    fbcon_puts("  RESTORED "); fbcon_hex(s->restored); fbcon_puts("\n");
    fbcon_puts("ELAPSED S "); fbcon_dec(elapsed); fbcon_puts(" / "); fbcon_dec(TEST_SECONDS); fbcon_puts("\n");
    fbcon_puts("EVENTS "); fbcon_dec(s->events); fbcon_puts("  DEV "); fbcon_dec(s->device_events);
    fbcon_puts("  EP "); fbcon_dec(s->ep_events); fbcon_puts("  DMA MISMATCH "); fbcon_dec(s->dma_mismatch); fbcon_puts("\n");
    fbcon_puts("RESETS "); fbcon_dec(s->resets); fbcon_puts("  CONNECTS "); fbcon_dec(s->connects);
    fbcon_puts("  DISCONNECTS "); fbcon_dec(s->disconnects); fbcon_puts("  SPEED "); fbcon_dec(s->speed); fbcon_puts("\n");
    fbcon_puts("SETUPS "); fbcon_dec(s->setups); fbcon_puts("  ADDRESS "); fbcon_dec(s->address);
    fbcon_puts("  CONFIGURED "); fbcon_dec(s->configured); fbcon_puts("  STALLS "); fbcon_dec(s->stalls); fbcon_puts("\n");
    fbcon_puts("LAST SETUP "); fbcon_hex(s->last_setup); fbcon_puts("\n");
    fbcon_puts("PORT0 READY "); fbcon_dec((uint64_t)qdwc3_ready(&usb, 0)); fbcon_puts("  PORT1 READY ");
    fbcon_dec((uint64_t)qdwc3_ready(&usb, 1)); fbcon_puts("\n");
    fbcon_puts("RX BYTES "); fbcon_dec(s->rx_bytes); fbcon_puts("  TX BYTES "); fbcon_dec(s->tx_bytes);
    fbcon_puts("  DROPPED "); fbcon_dec(s->dropped_rx); fbcon_puts("\n");
    fbcon_puts("COMMAND FAILURES "); fbcon_dec(s->command_failures); fbcon_puts("  LAST "); fbcon_hex(s->last_command_status);
    fbcon_puts("  OVERFLOW "); fbcon_dec(s->overflow); fbcon_puts("\n");
    fbcon_puts("DSTS "); fbcon_hex(qdwc3_rd(dwc3_base + 0xc70c)); fbcon_puts("  DCTL ");
    fbcon_hex(qdwc3_rd(dwc3_base + 0xc704)); fbcon_puts("\n");
    fbcon_puts("GSTS "); fbcon_hex(qdwc3_rd(dwc3_base + 0xc118)); fbcon_puts("  GEVNTCOUNT ");
    fbcon_hex(qdwc3_rd(dwc3_base + 0xc40c)); fbcon_puts("\n");
}

void q1n1_main(void) __attribute__((noreturn));
void q1n1_main(void)
{
    uint64_t el; __asm__ volatile("mrs %0, CurrentEL" : "=r"(el));
    fbcon_clear(0x080b10);
    fbcon_bar((el >> 2) == 2 ? 0x35e38b : 0xffb347);
    fbcon_at(0, 0);
    fbcon_puts("Q1N1 / A16 USB0 CDC ACM AFTER EXITBOOTSERVICES\n\n");
    fbcon_field("CURRENT EL ", el >> 2);
    fbcon_field("DMA ARENA ", (uintptr_t)arena);
    fbcon_field("SNAPSHOT GSNPSID ", saved.gsnpsid);
    uint32_t id = qdwc3_rd(dwc3_base + 0xc120);
    fbcon_field("LIVE GSNPSID ", id);
    fbcon_field("FIRMWARE GUSB2PHYCFG ", saved.gusb2phycfg);
    fbcon_field("QSCRATCH HS_PHY_CTRL ", qdwc3_rd(qscratch_base + 0x10));
    fbcon_field("QSCRATCH SS_PHY_CTRL ", qdwc3_rd(qscratch_base + 0x30));
    fbcon_puts("MAC: EXPECT 1209:316D SERIAL A16-Q1N1-EL2 - TWO ACM PORTS - ECHO ON BOTH\n");
    int result = qdwc3_takeover(&usb, dwc3_base, qscratch_base, &saved, arena, QDWC3_ARENA_SIZE);
    status(0, result);
    uint64_t start = ticks(), last = 0;
    static uint8_t pending[QDWC3_PIPES][4096];
    size_t length[QDWC3_PIPES] = {0}, offset[QDWC3_PIPES] = {0};
    int was_ready = 0;
    static const char greeting[] = "hello from q1n1 at EL2 after ExitBootServices\r\n";
    while (!result && ticks() - start < hz * TEST_SECONDS) {
        qdwc3_poll(&usb);
        int ready = qdwc3_ready(&usb, 0);
        if (ready && !was_ready) qdwc3_write(&usb, 0, (const uint8_t *)greeting, sizeof(greeting) - 1);
        was_ready = ready;
        for (unsigned p = 0; p < QDWC3_PIPES; p++) {
            if (!length[p]) { length[p] = qdwc3_read(&usb, p, pending[p], sizeof(pending[p])); offset[p] = 0; }
            if (length[p]) {
                offset[p] += qdwc3_write(&usb, p, pending[p] + offset[p], length[p] - offset[p]);
                if (offset[p] == length[p]) length[p] = 0;
            }
        }
        uint64_t now = ticks();
        if (now - last >= hz / 2) { last = now; status((now - start) / hz, result); }
    }
    if (usb.stats.init_step >= 2) qdwc3_stop(&usb);
    status((ticks() - start) / hz, result);
    fbcon_at(28, 0);
    fbcon_puts(result ? "TAKEOVER FAILED - SEE RESULT AND INIT STEP\n" : "TEST WINDOW ENDED - CONTROLLER STOPPED\n");
    fbcon_puts("RESET TO WINDOWS IN 20 S\n");
    wait_seconds(20);
    reset_now();
}

static int takeover_option(struct efi_loaded_image *image)
{
    if (!image || !image->options || image->options_size % 2 || image->options_size > 4096) return 0;
    const char16 *s = image->options;
    unsigned count = image->options_size / 2, found = 0;
    for (unsigned n = 0; n < count && s[n];) {
        while (n < count && s[n] == ' ') n++;
        unsigned start = n;
        while (n < count && s[n] && s[n] != ' ') n++;
        if (start == n || s[start] != '-') continue;
        static const char want[] = "--takeover";
        if (n - start != sizeof(want) - 1 || found) return 0;
        for (unsigned k = 0; k < sizeof(want) - 1; k++) if (s[start + k] != (char16)want[k]) return 0;
        found = 1;
    }
    return found;
}
static void say(struct efi_system_table *st, const char *text)
{
    char16 chunk[120];
    while (*text) {
        unsigned i = 0;
        while (*text && i < 117) { if (*text == '\n') chunk[i++] = '\r'; chunk[i++] = (uint8_t)*text++; }
        chunk[i] = 0;
        st->console_out->output(st->console_out, chunk);
    }
}

#ifndef USB_EBS_QEMU_FIXTURE
static efi_status load_snapshot(struct efi_system_table *st)
{
    uint64_t size = sizeof(snap);
    uint32_t attributes = 0;
    efi_status status = rt->get_variable(usb_ebs_variable, &usb_ebs_guid, &attributes, &size, &snap);
    if (status || attributes != EFI_VARIABLE_BOOTSERVICE_ACCESS || !usb_ebs_valid(&snap, size) ||
        (snap.flags & (USB_EBS_FIRMWARE_CONFIGURED | USB_EBS_FIRMWARE_STOPPED)) !=
            (USB_EBS_FIRMWARE_CONFIGURED | USB_EBS_FIRMWARE_STOPPED)) {
        say(st, "REFUSED: no valid prep snapshot from this boot. Firmware not exited.\n");
        return EFI_NOT_FOUND;
    }
    usb_ebs_saved(&snap, &saved);
    if (qdwc3_rd(dwc3_base + 0xc120) != saved.gsnpsid) {
        say(st, "REFUSED: live DWC3 ID differs from the prep snapshot. Firmware not exited.\n");
        return EFI_UNSUPPORTED;
    }
    return 0;
}
#else
/* QEMU-only build: RAM stands in for the controller so ExitBootServices, the
 * post-EBS console, the driver's soft-reset timeout path, stop, and
 * ResetSystem are exercised. Never deployed to hardware. */
static efi_status load_snapshot(struct efi_system_table *st)
{
    uint64_t fake = 0xffffffff;
    if (st->boot->allocate_pages(EFI_ALLOCATE_MAX_ADDRESS, EFI_LOADER_DATA, 0x11000 / 4096, &fake)) return EFI_UNSUPPORTED;
    dwc3_base = (uintptr_t)fake;
    qscratch_base = (uintptr_t)fake + 0x10000;
    for (uint64_t n = 0; n < 0x11000; n++) ((uint8_t *)dwc3_base)[n] = 0;
    saved.gsnpsid = 0x5533330a;
    *(uint32_t *)(dwc3_base + 0xc120) = saved.gsnpsid;
    say(st, "QEMU FIXTURE: fake controller in RAM, not an A16 build.\n");
    return 0;
}
#endif

efi_status efi_main(efi_handle handle, struct efi_system_table *st)
{
    efi_guid loaded_guid = {0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
    if (!st || !st->boot || !st->console_out || !st->runtime_services) return EFI_UNSUPPORTED;
    say(st, "q1n1 A16 USB0 EL2 takeover v1\n");
    struct efi_loaded_image *image = NULL;
    st->boot->handle_protocol(handle, &loaded_guid, (void **)&image);
    if (!takeover_option(image)) { say(st, "Use --takeover after q1n1-usb-ebs-prep.efi --device0.\n"); return EFI_INVALID_PARAMETER; }
    rt = st->runtime_services;
    efi_status status = load_snapshot(st);
    if (status) return status;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(hz));
    if (!hz || !fbcon_init(st)) { say(st, "REFUSED: timer or GOP framebuffer unavailable.\n"); return EFI_UNSUPPORTED; }
    uint64_t address = 0xffffffff;
    status = st->boot->allocate_pages(EFI_ALLOCATE_MAX_ADDRESS, EFI_LOADER_DATA, QDWC3_ARENA_SIZE / 4096, &address);
    if (status || (address & 0xfff)) { say(st, "REFUSED: could not allocate DMA arena below 4 GiB.\n"); return status ? status : EFI_UNSUPPORTED; }
    arena = (uint8_t *)(uintptr_t)address;
    uint64_t map = 0, key = 0, map_size, stride;
    uint32_t version = 0;
    const uint64_t capacity = 256 * 1024;
    status = st->boot->allocate_pages(0, EFI_LOADER_DATA, capacity / 4096, &map);
    if (status) return status;
    status = st->boot->set_watchdog(0, 0, 0, NULL);
    if (status) return status;
    say(st, "Snapshot valid. Leaving firmware; watch the framebuffer and the Mac.\n");
    for (unsigned attempt = 0; attempt < 3; attempt++) {
        map_size = capacity;
        status = st->boot->get_memory_map(&map_size, (void *)(uintptr_t)map, &key, &stride, &version);
        if (status) break;
        status = st->boot->exit_boot_services(handle, key);
        if (!status) q1n1_enter();
        if (status != EFI_INVALID_PARAMETER) break;
    }
    /* Firmware may be partly shut down. Never return to the UEFI caller now. */
    __asm__ volatile("msr daifset, #15" ::: "memory");
    fbcon_clear(0x080b10);
    fbcon_puts("EXITBOOTSERVICES FAILED\n");
    fbcon_field("STATUS ", status);
    wait_seconds(30);
    reset_now();
}
