/* SPDX-License-Identifier: MIT */
/* q1n1 Qualcomm UEFI payload (ASUS Zenbook A16 UX3607OA, BIOS312).
 *   (no option)   preflight: report entry EL, GOP and SPCR, return to the shell.
 *   --el2         framebuffer foothold after ExitBootServices (optional --uart, --fault-test).
 *   --boot        A16 USB boot window (firmware USB0 CDC ACM), then the m1n1-protocol
 *                 proxy at EL2 over q1n1's own DWC3 driver (1209:316D A16-Q1N1-EL2).
 *   --uart-proxy  the same proxy over an SPCR PL011 (QEMU validation path).
 * No Apple boot_args, ADT, AIC, DART or CPU chicken bits are used. Firmware
 * mappings stay in place; after EBS only ResetSystem is called.
 */
#include "efi.h"
#include "fbcon.h"
#include "console.h"
#include "sysinfo.h"
#include "gicv3.h"
#include "xhci.h"
#include "ncm.h"
#include "ncm-proxy.h"
#include "usb-ports.h"
#include "q1n1-bootinfo.h"
#include "boot-window.h"
#include "q1n1-proxy.h"

static struct {
    volatile uint32_t *pixels;
    uint32_t width, height, stride, format, red, green, blue;
    uint32_t row, col, scale;
    uint64_t bytes;
} fb;
static struct {
    uint64_t base;
    uint32_t irq;
    uint8_t type, baud;
    int enabled;
} uart;
static uint64_t before_el, map_size, map_stride;
static int fault_test;
extern const uint8_t q1n1_logo[];
extern void q1n1_enter(void) __attribute__((noreturn));

#define MODE_FOOTHOLD 0
#define MODE_USB_PROXY 1
#define MODE_UART_PROXY 2
#define HEAP_BYTES (64u << 20)
#define MAP_BYTES (256u * 1024)
#define STAGE_BYTES (16u << 20)
#define EFI_ALLOCATE_ADDRESS 2

/* Written for an unhandled exception and passed as the REQ_BOOT info pointer. */
struct q1n1_exception {
    uint64_t regs[31], sp, spsr, elr, esr, far, el, vector;
};

static int mode;
static struct efi_runtime_services *rt;
static uint64_t hz, proxy_start;
static struct qdwc3_saved saved;
#ifndef Q1N1_STAGE
static struct q1n1_boot_result boot;
#endif
static struct q1n1_bootinfo info;
static struct q1n1_exception exception;
static const struct q1n1_io *proxy_io;
static uint8_t *dma_arena;
static struct q1n1_gic gic;
/* The tick can interrupt the polled loop, so the driver has one owner at a time. */
static int in_exception, resetting;
static uint32_t status_row = 16;
static struct q1n1_usb_ports *usb_ports;
static int no_cdc;

static uint64_t stage_config;       /* logo choice etc., patched into a stage image */
extern const uint8_t q1n1_asahi_logo[];
/* The low two bits of the config word. The dragon is the default: centred where
 * m1n1 puts its logo, or parked in a corner per generation so a chainload is
 * visible at a glance. The upstream Asahi mark remains selectable. */
#define LOGO_CENTRE 0
#define LOGO_CORNER 1
#define LOGO_NONE 2
#define LOGO_ASAHI 3
/* Refresh only idle ports; preserve the transport carrying this request. */
uint64_t q1n1_xhci_retry(void) { return q1n1_usb_ports_refresh(usb_ports); }
uint64_t q1n1_ncm_proxy_run(uint64_t seconds)
{
    (void)seconds;
    if (!usb_ports) return ~0ull;
    uint64_t frames = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) frames += usb_ports->port[i].proxy.stats.frames_in;
    return frames; /* Both links are already served continuously. */
}

void *memcpy(void *dest, const void *src, size_t n)
{
    uint8_t *d = dest; const uint8_t *s = src;
    while (n--) *d++ = *s++;
    return dest;
}
void *memset(void *dest, int value, size_t n)
{
    uint8_t *d = dest;
    while (n--) *d++ = (uint8_t)value;
    return dest;
}
static int same(const void *a, const void *b, size_t n)
{
    const uint8_t *x = a, *y = b;
    while (n--) if (*x++ != *y++) return 0;
    return 1;
}
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static uint64_t current_el(void) { uint64_t v; __asm__ volatile("mrs %0, CurrentEL" : "=r"(v)); return v >> 2; }
static uint64_t counter(void) { uint64_t v; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(v)); return v; }
static uint64_t frequency(void) { uint64_t v; __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(v)); return v; }
static uint32_t rd(uint32_t offset) { return *(volatile uint32_t *)(uintptr_t)(uart.base + offset); }
static void wr(uint32_t offset, uint32_t v)
{
    *(volatile uint32_t *)(uintptr_t)(uart.base + offset) = v;
    __asm__ volatile("dsb sy" ::: "memory");
}
static int poll(uint32_t offset, uint32_t mask, uint32_t expected)
{
    uint64_t start = counter(), timeout = frequency() / 100; /* 10 ms, plus iteration cap */
    for (unsigned n = 0; n < 1000000; n++) {
        if ((rd(offset) & mask) == expected) return 1;
        if (counter() - start > timeout) break;
    }
    return 0;
}

/* Only use an already configured UART. Do not guess clocks, pins or power domains.
 * SPCR 0x13 describes Qualcomm GENI. Register definitions: Linux geni-se.h and
 * qcom_geni_serial.c. This small polled sequence is independently implemented.
 */
static int uart_byte(uint8_t c)
{
    if (!uart.enabled) return 0;
    if (uart.type == 3) { /* ARM PL011, used by QEMU */
        if (!poll(0x18, 1u << 5, 0)) goto failed;
        wr(0, c);
        return 1;
    }
    if (uart.type == 0x13) {
        /* Require UART firmware, FIFO mode, and an idle main sequencer. */
        if (((rd(0x68) >> 8) & 0xff) != 2 || (rd(0x258) & 1) ||
            !poll(0x40, 1, 0)) goto failed;
        wr(0x618, 1);       /* Clear previous command completion. */
        wr(0x270, 1);       /* One byte for this transfer. */
        wr(0x600, 1u << 27);/* UART START_TX. */
        wr(0x700, c);       /* First packed byte in the FIFO word. */
        if (!poll(0x610, 1, 1)) {
            wr(0x604, 2);   /* Abort this timed out command. */
            poll(0x610, 1u << 5, 1u << 5);
            wr(0x618, 1u << 5);
            goto failed;
        }
        wr(0x618, 1);
        return 1;
    }
failed:
    uart.enabled = 0;
    return 0;
}

/* A compact independently authored 5x7 console font, row-major. */
static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-./=?()_";
static const uint8_t glyphs[][7] = {
    {14,17,17,31,17,17,17},{30,17,17,30,17,17,30},{14,17,16,16,16,17,14},
    {30,17,17,17,17,17,30},{31,16,16,30,16,16,31},{31,16,16,30,16,16,16},
    {14,17,16,23,17,17,15},{17,17,17,31,17,17,17},{14,4,4,4,4,4,14},
    {7,2,2,2,2,18,12},{17,18,20,24,20,18,17},{16,16,16,16,16,16,31},
    {17,27,21,21,17,17,17},{17,25,21,19,17,17,17},{14,17,17,17,17,17,14},
    {30,17,17,30,16,16,16},{14,17,17,17,21,18,13},{30,17,17,30,20,18,17},
    {15,16,16,14,1,1,30},{31,4,4,4,4,4,4},{17,17,17,17,17,17,14},
    {17,17,17,17,17,10,4},{17,17,17,21,21,21,10},{17,17,10,4,10,17,17},
    {17,17,10,4,4,4,4},{31,1,2,4,8,16,31},
    {14,17,19,21,25,17,14},{4,12,4,4,4,4,14},{14,17,1,2,4,8,31},
    {30,1,1,14,1,1,30},{2,6,10,18,31,2,2},{31,16,16,30,1,1,30},
    {14,16,16,30,17,17,14},{31,1,2,4,8,8,8},{14,17,17,14,17,17,14},
    {14,17,17,15,1,1,14},{0,4,4,0,4,4,0},{0,0,0,31,0,0,0},
    {0,0,0,0,0,4,4},{1,2,2,4,8,8,16},{0,0,31,0,31,0,0},
    {14,17,1,2,4,0,4},{2,4,8,8,8,4,2},{8,4,2,2,2,4,8},
    {0,0,0,0,0,0,31}
};
_Static_assert(sizeof(glyphs) / sizeof(glyphs[0]) == sizeof(alphabet) - 1, "font size");

static uint32_t component(uint8_t value, uint32_t mask)
{
    if (!mask) return 0;
    unsigned shift = 0;
    while (!(mask & 1)) { mask >>= 1; shift++; }
    return (uint32_t)(((uint64_t)value * mask / 255) << shift);
}
static uint32_t color(uint32_t rgb)
{
    return component((uint8_t)(rgb >> 16), fb.red) |
           component((uint8_t)(rgb >> 8), fb.green) | component((uint8_t)rgb, fb.blue);
}
static void pixel(uint32_t x, uint32_t y, uint32_t value)
{
    if (fb.pixels && x < fb.width && y < fb.height) fb.pixels[(uint64_t)y * fb.stride + x] = value;
}
static void rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t rgb)
{
    uint32_t v = color(rgb);
    for (uint32_t j = 0; j < h && y + j < fb.height; j++)
        for (uint32_t i = 0; i < w && x + i < fb.width; i++) pixel(x + i, y + j, v);
    __asm__ volatile("dsb sy" ::: "memory");
}
static void screen_char(char c)
{
    if (!fb.pixels) return;
    if (c == '\r') return;
    if (c == '\n') { fb.col = 0; fb.row++; return; }
    if (c >= 'a' && c <= 'z') c -= 32;
    if (20 + (fb.col + 1) * 6 * fb.scale >= fb.width) { fb.col = 0; fb.row++; }
    uint32_t y = 20 + fb.row * 9 * fb.scale;
    if (y + 7 * fb.scale >= fb.height) return;
    for (unsigned g = 0; g < sizeof(alphabet) - 1; g++) if (alphabet[g] == c) {
        for (unsigned j = 0; j < 7; j++) for (unsigned i = 0; i < 5; i++)
            if (glyphs[g][j] & (1u << (4 - i)))
                rect(20 + fb.col * 6 * fb.scale + i * fb.scale, y + j * fb.scale,
                     fb.scale, fb.scale, 0xe8edf5);
        break;
    }
    fb.col++;
}
static void out(const char *s)
{
    while (*s) {
        if (*s == '\n') uart_byte('\r');
        uart_byte((uint8_t)*s);
        screen_char(*s++);
    }
}
static void hex(uint64_t v)
{
    char s[19] = "0X0000000000000000";
    for (unsigned i = 0; i < 16; i++) { s[17-i] = "0123456789ABCDEF"[v & 15]; v >>= 4; }
    out(s);
}
static void field(const char *label, uint64_t v) { out(label); hex(v); out("\n"); }
/* The inverse of component(), for compositing against what is already drawn. */
static uint8_t channel(uint32_t value, uint32_t mask)
{
    if (!mask) return 0;
    unsigned shift = 0;
    while (!(mask & 1)) { mask >>= 1; shift++; }
    return (uint8_t)((uint64_t)((value >> shift) & mask) * 255 / mask);
}
/* Premultiplied source-over, so the emblem lands on the firmware's screen as a
 * dragon rather than as a dragon in a black box. */
static void draw_logo_at(uint32_t left, uint32_t top)
{
    if (fb.width < 600 || fb.height < 280 || !fb.pixels) return;
    for (unsigned y = 0; y < 256; y++) for (unsigned x = 0; x < 256; x++) {
        const uint8_t *p = q1n1_logo + (y * 256 + x) * 4;
        uint32_t a = p[3];
        if (!a) continue;
        uint32_t r = p[0], g = p[1], b = p[2];
        if (a < 255 && left + x < fb.width && top + y < fb.height) {
            uint32_t under = fb.pixels[(uint64_t)(top + y) * fb.stride + left + x], inv = 255 - a;
            r += (uint32_t)channel(under, fb.red) * inv / 255;
            g += (uint32_t)channel(under, fb.green) * inv / 255;
            b += (uint32_t)channel(under, fb.blue) * inv / 255;
        }
        pixel(left + x, top + y, color(r << 16 | g << 8 | b));
    }
}
static void draw_logo(void) { draw_logo_at(fb.width - 276, 20); }
/* Each chainloaded generation parks the logo in a different corner, so the
 * screen says at a glance which one is running. All four avoid the text area. */
static void draw_logo_for_generation(uint64_t generation)
{
    uint32_t right = fb.width > 296 ? fb.width - 276 : 20;
    uint32_t bottom = fb.height > 296 ? fb.height - 276 : 20;
    uint32_t middle = fb.height > 296 ? fb.height / 2 - 128 : 20;
    switch (generation & 3) {
    case 0: draw_logo_at(right, 20); break;
    case 1: draw_logo_at(right, bottom); break;
    case 2: draw_logo_at(20, bottom); break;
    default: draw_logo_at(right, middle); break;
    }
}

#ifndef Q1N1_STAGE
static int checksum(const uint8_t *p, uint32_t n)
{
    uint8_t sum = 0;
    while (n--) sum += *p++;
    return !sum;
}
static int table_valid(const uint8_t *p)
{
    uint32_t n = get32(p + 4);
    return n >= 36 && n <= 1024 * 1024 && checksum(p, n);
}
static void find_uart(struct efi_system_table *st)
{
    const efi_guid acpi = {0x8868e871,0xe4f1,0x11d3,{0xbc,0x22,0,0x80,0xc7,0x3c,0x88,0x81}};
    for (uint64_t i = 0; i < st->table_count; i++) {
        if (!same(&st->tables[i].guid, &acpi, sizeof(acpi))) continue;
        const uint8_t *rsdp = st->tables[i].table;
        if (!same(rsdp,"RSD PTR ",8) || !checksum(rsdp,20) || rsdp[15] < 2) continue;
        uint32_t length = get32(rsdp + 20);
        if (length < 36 || length > 4096 || !checksum(rsdp,length)) continue;
        info.acpi_rsdp = (uintptr_t)rsdp;
        const uint8_t *xsdt = (const void *)(uintptr_t)get64(rsdp + 24);
        if (!xsdt || !same(xsdt,"XSDT",4) || !table_valid(xsdt)) continue;
        uint32_t size = get32(xsdt + 4);
        for (uint32_t off = 36; off + 8 <= size; off += 8) {
            const uint8_t *t = (const void *)(uintptr_t)get64(xsdt + off);
            if (!t || !same(t,"SPCR",4) || !table_valid(t) || get32(t+4) < 80) continue;
            /* GAS must describe 32-bit system-memory access with no bit offset. */
            if (t[40] != 0 || t[41] != 32 || t[42] != 0 || (t[43] != 0 && t[43] != 3 && t[43] != 32)) continue;
            uart.type = t[36]; uart.base = get64(t+44); uart.irq = get32(t+54); uart.baud = t[58];
            if (uart.base & 3) uart.base = 0;
            return;
        }
    }
}
static void find_smbios(struct efi_system_table *st)
{
    const efi_guid smbios3 = {0xf2fd1544,0x9794,0x4a2c,{0x99,0x2e,0xe5,0xbb,0xcf,0x20,0xe3,0x94}};
    for (uint64_t i = 0; i < st->table_count; i++)
        if (same(&st->tables[i].guid, &smbios3, sizeof(smbios3))) info.smbios3 = (uintptr_t)st->tables[i].table;
}
static void find_framebuffer(struct efi_system_table *st)
{
    efi_guid guid = {0x9042a9de,0x23dc,0x4a38,{0x96,0xfb,0x7a,0xde,0xd0,0x80,0x51,0x6a}};
    struct efi_gop *gop = NULL;
    if (st->boot->locate_protocol(&guid, NULL, (void **)&gop) || !gop || !gop->mode || !gop->mode->info) return;
    struct efi_gop_info *info = gop->mode->info;
    if (gop->mode->info_size < sizeof(*info) || info->format > 2 ||
        !info->width || !info->height || info->width > info->stride ||
        (uint64_t)info->stride * info->height > gop->mode->framebuffer_size / 4 ||
        !gop->mode->framebuffer) return;
    fb.pixels = (void *)(uintptr_t)gop->mode->framebuffer;
    fb.bytes = gop->mode->framebuffer_size;
    fb.width = info->width; fb.height = info->height; fb.stride = info->stride; fb.format = info->format;
    fb.red = info->format == 0 ? 0xff : 0xff0000;
    fb.green = 0xff00;
    fb.blue = info->format == 0 ? 0xff0000 : 0xff;
    if (info->format == 2) { fb.red = info->red; fb.green = info->green; fb.blue = info->blue; }
    fb.scale = fb.width >= 1600 ? 3 : 2;
}
static int option(struct efi_loaded_image *image, const char *word)
{
    if (!image || !image->options || image->options_size > 65536) return 0;
    unsigned n = image->options_size / 2;
    for (unsigned i = 0; i < n && image->options[i]; i++) {
        if (i && image->options[i-1] != ' ') continue;
        unsigned j = 0;
        while (word[j] && i+j < n && image->options[i+j] == (uint8_t)word[j]) j++;
        if (!word[j] && (i+j == n || !image->options[i+j] || image->options[i+j] == ' ')) return 1;
    }
    return 0;
}
static void firmware_out(struct efi_system_table *st, const char *s)
{
    char16 chunk[120];
    while (*s) {
        unsigned i = 0;
        while (*s && i < 117) { if (*s == '\n') chunk[i++] = '\r'; chunk[i++] = (uint8_t)*s++; }
        chunk[i] = 0;
        if (st->console_out) st->console_out->output(st->console_out, chunk);
    }
}
#endif /* !Q1N1_STAGE */

/* ---- Post-EBS services shared by the proxy modes ---- */

uint32_t qdwc3_rd(uintptr_t address) { return *(volatile uint32_t *)address; }
void qdwc3_wr(uintptr_t address, uint32_t value) { *(volatile uint32_t *)address = value; }
void qdwc3_delay_us(uint64_t us)
{
    uint64_t start = counter(), wait = hz * us / 1000000 + 1;
    while (counter() - start < wait) __asm__ volatile("yield");
}
static void wait_seconds(unsigned seconds)
{
    uint64_t start = counter();
    while (counter() - start < hz * seconds) __asm__ volatile("yield");
}

/* The xHCI host driver's platform hooks. Separate from the DWC3 gadget's so a
 * native harness can substitute a simulated controller for either one. */
uint32_t xhci_rd32(uintptr_t address) { return *(volatile uint32_t *)address; }
void xhci_wr32(uintptr_t address, uint32_t value) { *(volatile uint32_t *)address = value; }
uint64_t xhci_rd64(uintptr_t address) { return *(volatile uint64_t *)address; }
void xhci_wr64(uintptr_t address, uint64_t value) { *(volatile uint64_t *)address = value; }
void xhci_delay_us(uint64_t microseconds) { qdwc3_delay_us(microseconds); }
uint64_t xhci_now_us(void) { return hz ? counter() * 1000000u / hz : 0; }
static void reset_now(void) __attribute__((noreturn));
static void reset_now(void)
{
    if (!resetting) {
        resetting = 1;
        q1n1_gic_stop(&gic);
        /* Present a clean USB disconnect so the Mac notices the reboot at once. */
        q1n1_usb_ports_stop(usb_ports);
    }
    if (rt && rt->reset_system) rt->reset_system(0, 0, 0, NULL);
    __asm__ volatile("mov x0, #0x0009\n movk x0, #0x8400, lsl #16\n smc #0" ::: "x0", "x1", "x2", "x3", "memory");
    for (;;) __asm__ volatile("wfe");
}

uint64_t q1n1_platform_ticks(void) { return counter(); }
uint64_t q1n1_platform_hz(void) { return hz; }
uint64_t q1n1_platform_bootargs(void) { return (uintptr_t)&info; }
uint64_t q1n1_platform_base(void) { return info.image_base; }
void q1n1_platform_reboot(void) { reset_now(); }

static uint64_t last_status;
static uint64_t last_shift;
static uint64_t last_swap;

/* How often the console steps around its anti-burn-in grid. Two minutes is well
 * inside the hours it takes an OLED to retain an image, and far enough apart
 * that the framebuffer copy it costs is not worth measuring. */
#define CON_SHIFT_SECONDS 120
/* And how often the text column changes sides. The grid handles a lit sub-pixel
 * holding one glyph stroke; it does nothing about half the panel being lit and
 * the other half black for the whole session, which with a centred emblem is
 * the actual shape on screen. Five minutes gives each side a real rest without
 * the screen looking like it has a fault. */
#define CON_SWAP_SECONDS 300

static void status_panel(void)
{
    const struct q1n1_proxy_stats *p = &q1n1_proxy_stats;
    if (hz && counter() - last_shift > (uint64_t)CON_SHIFT_SECONDS * hz) {
        last_shift = counter();
        con_shift_next();
    }
    /* Only the column moves; rows are untouched, so status_row still points at
     * the same lines and the status block redraws itself on the new side. */
    if (hz && counter() - last_swap > (uint64_t)CON_SWAP_SECONDS * hz) {
        last_swap = counter();
        con_swap_side();
    }
    for (uint32_t row = status_row; row < status_row + 6; row++) con_clear_row(row);
    con_at(status_row, 0);
    con_puts("proxy: up "); con_dec((counter() - proxy_start) / hz);
    con_puts(" s, "); con_dec(p->requests); con_puts(" requests, "); con_dec(p->proxy_calls);
    con_puts(" ops, last "); con_hexn(p->last_opcode, 3); con_puts("\n");
    con_puts("proxy: read "); con_dec(p->read_bytes); con_puts(" B, wrote "); con_dec(p->write_bytes);
    con_puts(" B, csum err "); con_dec(p->checksum_errors); con_puts(", timeouts "); con_dec(p->timeouts);
    con_puts(", bad cmd "); con_dec(p->bad_commands); con_puts(", exc "); con_dec(p->exceptions); con_puts("\n");
    con_puts("gic: "); con_dec(gic.ticks); con_puts(" ticks at "); con_dec(gic.rate);
    con_puts(" Hz, serviced "); con_dec(gic.serviced); con_puts(", skipped "); con_dec(gic.skipped);
    con_puts(", spurious "); con_dec(gic.spurious);
    if (mode != MODE_USB_PROXY || !usb_ports) return;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        const struct q1n1_usb_port_status *st = &usb_ports->status[i];
        const struct q1n1_usb_port *port = &usb_ports->port[i];
        con_puts("\nusb"); con_dec(i); con_puts(": ");
        if (st->role == Q1N1_USB_HOST) {
            con_puts("host NCM "); con_puts(st->connected ? "up" : "discovering");
            con_puts(", frames "); con_dec(port->ncm.stats.frames_in);
            con_puts("/"); con_dec(port->ncm.stats.frames_out);
        } else if (st->role == Q1N1_USB_DEVICE) {
            con_puts("CDC cfg "); con_dec(port->device.stats.configured);
            con_puts(", setups "); con_dec(port->device.stats.setups);
            con_puts(", rx "); con_dec(port->device.stats.rx_bytes);
            con_puts(", tx "); con_dec(port->device.stats.tx_bytes);
        } else con_puts(st->role == Q1N1_USB_UNSUPPORTED ? "unavailable" : "retrying");
        con_puts(", attempts "); con_dec(st->attempts);
        con_puts(", disconnects "); con_dec(st->disconnects);
    }
}
static void status_tick(void)
{
    uint64_t now = counter();
    if (now - last_status >= hz) { last_status = now; status_panel(); }
}

static void managed_usb_poll(void *ctx)
{
    q1n1_usb_ports_poll(ctx);
    status_tick();
}

static size_t pl011_read(void *ctx, uint8_t *b, size_t n)
{
    (void)ctx;
    size_t k = 0;
    while (k < n && !(rd(0x18) & (1u << 4))) b[k++] = (uint8_t)rd(0);
    return k;
}
static size_t pl011_write(void *ctx, const uint8_t *b, size_t n)
{
    (void)ctx;
    size_t k = 0;
    while (k < n && !(rd(0x18) & (1u << 5))) *(volatile uint32_t *)(uintptr_t)uart.base = b[k++];
    return k;
}
static void pl011_poll(void *ctx) { (void)ctx; status_tick(); }
static int pl011_ready(void *ctx) { (void)ctx; return 1; }
static const struct q1n1_io pl011_io = {.read = pl011_read, .write = pl011_write,
                                        .poll = pl011_poll, .ready = pl011_ready};

void q1n1_fault(uint64_t esr, uint64_t elr, uint64_t far) __attribute__((noreturn));
void q1n1_fault(uint64_t esr, uint64_t elr, uint64_t far)
{
    uart.enabled = 0; /* The fault may itself be an inaccessible UART. */
    if (mode != MODE_FOOTHOLD) {
        for (uint32_t row = status_row + 7; row < status_row + 12; row++) con_clear_row(row);
        con_at(status_row + 7, 0);
        con_puts("q1n1: unhandled exception\n");
        con_puts("  esr "); con_hex(esr); con_puts("  elr "); con_hex(elr);
        con_puts("  far "); con_hex(far); con_puts("\n");
        if (no_cdc) {
            /* Resetting here loops: the boot window already re-armed BootNext,
             * so the next boot lands straight back in the same fault. Halt and
             * let the screen be the diagnostic; a power cycle spends the
             * one-shot BootNext and returns to Windows. */
            con_puts("q1n1: halted - no console to report to; power cycle for Windows\n");
            for (;;) __asm__ volatile("wfe");
        }
        con_puts("q1n1: resetting in 30 s\n");
        wait_seconds(30);
        reset_now();
    }
    rect(0,0,fb.width,8,0xff3040);
    out("\nQ1N1 SYNCHRONOUS EXCEPTION\n");
    field("ESR ",esr); field("ELR ",elr); field("FAR ",far);
    for (;;) __asm__ volatile("wfe");
}

/* Called from the vectors in entry.S (Q1N1_EXCEPTION_FRAME) with x0-x30 and SP. */
void q1n1_exception(uint64_t *frame, uint64_t vector);
void q1n1_exception(uint64_t *frame, uint64_t vector)
{
    int el2 = current_el() == 2;
    uint64_t esr, elr, far, spsr;
    if (el2) {
        __asm__ volatile("mrs %0, esr_el2" : "=r"(esr)); __asm__ volatile("mrs %0, elr_el2" : "=r"(elr));
        __asm__ volatile("mrs %0, far_el2" : "=r"(far)); __asm__ volatile("mrs %0, spsr_el2" : "=r"(spsr));
    } else {
        __asm__ volatile("mrs %0, esr_el1" : "=r"(esr)); __asm__ volatile("mrs %0, elr_el1" : "=r"(elr));
        __asm__ volatile("mrs %0, far_el1" : "=r"(far)); __asm__ volatile("mrs %0, spsr_el1" : "=r"(spsr));
    }
    if (resetting) reset_now();
    if ((vector & 3) == 1) { /* IRQ: the EL2 timer tick keeps the link alive */
        uint32_t intid = q1n1_gic_handle(&gic);
        if (intid == gic.intid) q1n1_usb_ports_tick(usb_ports);
        return;
    }
    uint64_t guard = q1n1_exc_guard & Q1N1_GUARD_TYPE_MASK;
    if ((vector & 3) == 0 && guard != Q1N1_GUARD_OFF) {
        if (guard == Q1N1_GUARD_SKIP) {
            elr += 4;
        } else if (guard == Q1N1_GUARD_MARK) {
            /* A load or store: the destination register is Rt in bits 4:0. */
            uint32_t insn = *(const uint32_t *)(uintptr_t)elr;
            if ((insn & 0x1f) < 31) frame[insn & 0x1f] = Q1N1_GUARD_MARKER;
            elr += 4;
        } else {
            /* Leaf helpers in proxy-asm.S: return to the caller with a marker. */
            frame[0] = Q1N1_GUARD_MARKER;
            elr = frame[30];
            q1n1_exc_guard = Q1N1_GUARD_OFF;
        }
        q1n1_exc_count++;
        if (el2) __asm__ volatile("msr elr_el2, %0" : : "r"(elr) : "memory");
        else __asm__ volatile("msr elr_el1, %0" : : "r"(elr) : "memory");
        return;
    }
    q1n1_proxy_stats.exceptions++;
    if (mode != MODE_FOOTHOLD && proxy_io && !in_exception) {
        in_exception = 1;
        for (unsigned n = 0; n < 31; n++) exception.regs[n] = frame[n];
        exception.sp = frame[31]; exception.spsr = spsr; exception.elr = elr;
        exception.esr = esr; exception.far = far; exception.el = el2 ? 2 : 1; exception.vector = vector;
        uint64_t result = q1n1_proxy_run(proxy_io, Q1N1_START_EXCEPTION, (uint32_t)(vector & 3), (uintptr_t)&exception);
        in_exception = 0;
        if (result == Q1N1_EXC_RET_HANDLED) {
            for (unsigned n = 0; n < 31; n++) frame[n] = exception.regs[n];
            elr = exception.elr; spsr = exception.spsr;
            if (el2) {
                __asm__ volatile("msr elr_el2, %0" : : "r"(elr) : "memory");
                __asm__ volatile("msr spsr_el2, %0" : : "r"(spsr) : "memory");
            } else {
                __asm__ volatile("msr elr_el1, %0" : : "r"(elr) : "memory");
                __asm__ volatile("msr spsr_el1, %0" : : "r"(spsr) : "memory");
            }
            return;
        }
    }
    q1n1_fault(esr, elr, far);
}

static void proxy_main(void) __attribute__((noreturn));
static void proxy_main(void)
{
    uint64_t el = current_el(), midr;
    __asm__ volatile("mrs %0, midr_el1" : "=r"(midr));
    info.current_el = el;
    proxy_start = counter();
    /* Start both panel timers here. They are compared against a counter that
     * has been running since power-on, so leaving them at zero makes the first
     * comparison true and the console jumps as soon as it is drawn. */
    last_shift = last_swap = proxy_start;
    con_init(info.fb_base, (uint32_t)info.fb_width, (uint32_t)info.fb_height,
             (uint32_t)info.fb_stride, (uint32_t)info.fb_format);
    /* Before anything is drawn: this decides how the column is sized, and a
     * machine whose panel does not retain an image gets a console that stays
     * put. The line naming the decision is printed with the rest of the log. */
    con_retains(sysinfo_panel_retains(info.smbios3));
    con_clear();
    switch (stage_config & 3) {
    case LOGO_CORNER: draw_logo_for_generation(info.stage_generation); break;
    case LOGO_NONE: break;
    /* Reserve first: the emblem is placed relative to the text column, so the
     * column has to be sized before the mark knows which half it belongs in. */
    case LOGO_ASAHI:
        con_reserve_emblem(256);
        con_emblem(q1n1_asahi_logo, 256);
        break;
    default:
        con_reserve_emblem(256);
        con_emblem(q1n1_logo, 256);
        break;
    }

    /* An m1n1-shaped boot log: subsystem, colon, what it found. */
    con_at(0, 0);
    con_puts("q1n1 ");
    if (info.stage_generation) {
        con_puts("stage "); con_dec(info.stage_generation); con_puts(" @ ");
        con_hexn(info.image_base, 8); con_puts(" (chainloaded over the proxy)\n");
    } else {
        con_puts("payload @ "); con_hexn(info.image_base, 8); con_puts(" (loaded by firmware)\n");
    }
    con_puts(el == 2 ? "q1n1: EL2 confirmed after ExitBootServices (entry EL"
                     : "q1n1: WARNING - running at EL1, not EL2 (entry EL");
    con_dec(info.entry_el); con_puts(")\n");
    uint64_t cores = 0;
    sysinfo_acpi(info.acpi_rsdp, &cores);
    sysinfo_cpu(hz, cores);
    sysinfo_uefi(info.system_table);
    sysinfo_smbios(info.smbios3);
    sysinfo_memory(info.memory_map, info.memory_map_size, info.memory_map_stride);
    con_puts("fb init: "); con_dec(info.fb_width); con_puts("x"); con_dec(info.fb_height);
    con_puts(" [s="); con_dec(info.fb_stride); con_puts("] @"); con_hexn(info.fb_base, 8); con_puts("\n");
    con_puts("fb console: max rows "); con_dec(con_rows()); con_puts(", max cols "); con_dec(con_cols());
    con_puts("\n");
    /* Say which way this went. If the panel is OLED and this line reads "fixed",
     * the product match is wrong and the screen is unprotected -- which is not
     * something to discover from a burnt-in panel months later. */
    con_puts("fb panel: ");
    if (sysinfo_panel_retains(info.smbios3)) {
        con_puts("retains image, console moves (grid "); con_dec(CON_SHIFT_SECONDS);
        con_puts(" s, sides "); con_dec(CON_SWAP_SECONDS); con_puts(" s)\n");
    } else {
        con_puts("not known to retain, console fixed\n");
    }
    con_puts("fb: display logo\n");
    con_puts("q1n1 mem: heap "); con_dec(info.heap_size >> 20); con_puts(" MiB @ "); con_hexn(info.heap_base, 8);
    con_puts(", dma "); con_dec(info.dma_size >> 20); con_puts(" MiB @ "); con_hexn(info.dma_base, 8);
    con_puts(", stage "); con_dec(info.stage_size >> 20); con_puts(" MiB @ "); con_hexn(info.stage_base, 8);
    con_puts("\n");

    if (mode == MODE_USB_PROXY) {
        if (info.heap_size < Q1N1_USB_ARENA_SIZE + 0x10000) {
            con_puts("USB: insufficient DMA scratch; halted\n");
            for (;;) __asm__ volatile("wfe");
        }
        uint8_t *arena = (void *)(uintptr_t)((info.heap_base + info.heap_size - Q1N1_USB_ARENA_SIZE) & ~0xffffull);
        usb_ports = q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, info.stage_generation != 0);
        if (!usb_ports) {
            con_puts("USB: invalid arena; halted\n");
            for (;;) __asm__ volatile("wfe");
        }
        usb_ports->io.poll = managed_usb_poll;
        proxy_io = &usb_ports->io;
        info.usb_ports = (uintptr_t)usb_ports->status;
        info.usb_port_count = Q1N1_USB_PORTS;
        info.usb_stats = (uintptr_t)&usb_ports->port[0].device.stats;
        info.xhci_base = 0;
        info.xhci_stats = (uintptr_t)&usb_ports->port[0].host.stats;
        info.ncm_stats = (uintptr_t)&usb_ports->port[0].ncm.stats;
        info.ncm_proxy_stats = (uintptr_t)&usb_ports->port[0].proxy.stats;
        con_puts("usb: independent host/CDC discovery on both controllers\n");
    } else {
        con_puts("uart: pl011 @ "); con_hexn(uart.base, 8); con_puts("\n");
        proxy_io = &pl011_io;
    }

    int tick = q1n1_gic_init(&gic, info.acpi_rsdp, hz, 1000);
    info.gic_distributor = gic.distributor;
    info.gic_redistributor = gic.redistributor;
    info.gic_stats = (uintptr_t)&gic;
    if (tick) {
        con_puts("gic: unavailable, status "); con_dec((uint64_t)(-(int64_t)tick));
        con_puts(" - the link is serviced by polling only\n");
    } else {
        con_puts("gic: dist @ "); con_hexn(gic.distributor, 8);
        con_puts(" redist @ "); con_hexn(gic.redistributor, 8);
        con_puts(", EL2 timer ppi "); con_dec(gic.intid);
        con_puts(" at "); con_dec(gic.rate); con_puts(" Hz\n");
        q1n1_gic_start(&gic);
    }
    con_puts("Initialization complete.\n");
    con_puts("Running proxy...\n");
    /* Anchor the live panel under the log, but never past the last row: on a
     * small screen the panel replaces the tail of the log instead of scrolling
     * it on every redraw. */
    status_row = con_row() + 1;
    if (status_row + 7 > con_rows()) status_row = con_rows() > 7 ? con_rows() - 7 : 0;

    for (;;) {
        q1n1_proxy_run(proxy_io, Q1N1_START_BOOT, 0, (uintptr_t)&info);
        if (!q1n1_next_stage.entry) continue;
        /* Chainload: stop the tick first so the outgoing vectors and driver
         * state cannot be entered once the new stage owns the hardware. */
        q1n1_gic_stop(&gic);
        if (q1n1_usb_ports_handoff(usb_ports)) {
            q1n1_next_stage.entry = q1n1_next_stage.argument = 0;
            con_puts("q1n1: chainload refused: USB DMA did not halt\n");
            q1n1_gic_start(&gic);
            continue;
        }
        uint64_t entry = q1n1_next_stage.entry;
        uint64_t argument = q1n1_next_stage.argument ? q1n1_next_stage.argument : (uintptr_t)&info;
        q1n1_next_stage.entry = q1n1_next_stage.argument = 0;
        con_at(status_row + 7, 0);
        con_clear_row(status_row + 7);
        con_puts("q1n1: chainloading a stage at "); con_hexn(entry, 8); con_puts("\n");
        __asm__ volatile("dsb sy\n isb" ::: "memory");
        ((void (*)(uint64_t))(uintptr_t)entry)(argument);
        con_puts("q1n1: the stage returned; resetting\n");
        reset_now();
    }
}

void q1n1_main(void) __attribute__((noreturn));
void q1n1_main(void)
{
    if (mode != MODE_FOOTHOLD) proxy_main();
    uint64_t el = current_el(), midr;
    __asm__ volatile("mrs %0, midr_el1" : "=r"(midr));
    rect(0,0,fb.width,fb.height,0x080b10);
    rect(0,0,fb.width,8,el == 2 ? 0x35e38b : 0xffb347);
    draw_logo();
    out("Q1N1 / SNAPDRAGON BRINGUP\n\n");
    out(el == 2 ? "EL2 CONFIRMED AFTER EXITBOOTSERVICES\n" : "EL2 NOT REACHED - STILL IN EL1\n");
    field("ENTRY EL ",before_el); field("CURRENT EL ",el); field("MIDR ",midr);
    field("FRAMEBUFFER ",(uint64_t)(uintptr_t)fb.pixels);
    field("SPCR UART ",uart.base); field("SPCR TYPE ",uart.type);
    field("SPCR GSI ",uart.irq); field("SPCR BAUD CODE ",uart.baud);
    field("MEMORY DESCRIPTORS ",map_stride ? map_size / map_stride : 0);
    out(uart.enabled ? "POLLED UART TX ACTIVE\n" : "UART TX DISABLED OR UNAVAILABLE\n");
    out("USB PROXY: RUN Q1N1.EFI --BOOT\n\n");
    out("BOOT FOOTHOLD ONLY - NO GUEST OR SMP YET\n");
    out("POWER CYCLE TO RETURN TO FIRMWARE\n");
    if (fault_test) __asm__ volatile("brk #0");
    uint64_t last = counter(), ticks = frequency();
    unsigned beat = 0;
    for (;;) {
        if (counter() - last >= ticks) {
            last = counter(); beat++;
            if (fb.height >= 20) rect(20,fb.height-16,32,8,beat & 1 ? 0x35e38b : 0x173023);
        }
        __asm__ volatile("yield");
    }
}

#ifndef Q1N1_STAGE
static efi_status leave_firmware(efi_handle handle, struct efi_system_table *st, const char *message)
{
    efi_status status = st->boot->set_watchdog(0,0,0,NULL);
    if (status) return status;
    /* Preallocate ample map room. After an EBS attempt, only retry GetMemoryMap
     * and EBS. No logging, allocation, protocol or firmware console calls. */
    uint64_t map = 0, key = 0;
    uint32_t version = 0;
    status = st->boot->allocate_pages(0,EFI_LOADER_DATA,MAP_BYTES / 4096,&map);
    if (status) return status;
    firmware_out(st,message);
    for (unsigned attempt = 0; attempt < 3; attempt++) {
        map_size = MAP_BYTES;
        status = st->boot->get_memory_map(&map_size,(void *)(uintptr_t)map,&key,&map_stride,&version);
        if (status) break;
        info.memory_map = map; info.memory_map_size = map_size;
        info.memory_map_stride = map_stride; info.memory_map_version = version;
        status = st->boot->exit_boot_services(handle,key);
        if (!status) q1n1_enter();
        if (status != EFI_INVALID_PARAMETER) break;
    }
    /* Firmware may be partly shut down. Never return to the UEFI caller now. */
    __asm__ volatile("msr daifset, #15" ::: "memory");
    uart.enabled = 0;
    rect(0,0,fb.width,fb.height,0x080b10);
    out("EXITBOOTSERVICES FAILED\n"); field("STATUS ",status);
    for (;;) __asm__ volatile("wfe");
}

/* --boot and --uart-proxy. For --boot every refusal returns 0 so the startup
 * script continues to Windows; only an explicit shell choice returns nonzero. */
static efi_status proxy_efi_main(efi_handle handle, struct efi_system_table *st, struct efi_loaded_image *image)
{
    int usb_mode = option(image, "--boot");
    efi_status refused = usb_mode ? 0 : EFI_UNSUPPORTED;
    rt = st->runtime_services;
    hz = frequency();
    if (!image || !rt || !hz || !fb.pixels || !fbcon_init(st) || (before_el != 1 && before_el != 2)) {
        firmware_out(st, "REFUSED: LoadedImage, runtime services, timer, or framebuffer unavailable.\n");
        return refused;
    }
    info.magic = Q1N1_BOOTINFO_MAGIC; info.version = 1; info.size = sizeof(info);
    info.image_base = (uintptr_t)image->image_base; info.image_size = image->image_size;
    info.entry_el = before_el; info.timer_hz = hz;
    info.system_table = (uintptr_t)st; info.runtime_services = (uintptr_t)rt;
    info.config_tables = (uintptr_t)st->tables; info.config_count = st->table_count;
    info.fb_base = (uintptr_t)fb.pixels; info.fb_size = fb.bytes; info.fb_width = fb.width;
    info.fb_height = fb.height; info.fb_stride = fb.stride; info.fb_format = fb.format;
    info.proxy_stats = (uintptr_t)&q1n1_proxy_stats; info.exception = (uintptr_t)&exception;
    find_smbios(st);
    if (usb_mode) {
        efi_status status = q1n1_boot_window(handle, st, &boot, option(image, "--auto"));
        info.boot_current = boot.boot_current; info.return_armed = boot.return_armed;
        info.arm_status = boot.arm_status; info.entry_status = boot.entry_status;
        if (status) { firmware_out(st, "Boot window did not complete; continuing to Windows.\n"); return 0; }
        if (boot.choice == Q1N1_BOOT_WINDOWS) { firmware_out(st, "Continuing to Windows.\n"); return 0; }
        if (boot.choice == Q1N1_BOOT_SHELL) { firmware_out(st, "Staying in the UEFI shell.\n"); return EFI_ERROR(21); }
        if (boot.choice == Q1N1_BOOT_PROXY_NCM) {
            /* No dock: the device-mode controller is left untouched and there is
             * no console to take over. Everything rides on the NCM link, which
             * proxy_main brings up after ExitBootServices. */
            firmware_out(st, "No USB0 host; the proxy will answer on the NCM link only.\n");
            no_cdc = 1;
            mode = MODE_USB_PROXY;
            info.mode = MODE_USB_PROXY;
            /* Published so host tools never read through a null pointer; the
             * counters simply stay zero because the gadget is never started. */
            info.usb_stats = 0;
        } else {
        usb_ebs_saved(&boot.snapshot, &saved);
        if (qdwc3_rd(USB_EBS_DWC3_BASE + 0xc120) != saved.gsnpsid) {
            firmware_out(st, "REFUSED: live DWC3 ID differs from the boot-window snapshot.\n");
            return 0;
        }
        uint64_t address = 0xffffffff;
        status = st->boot->allocate_pages(EFI_ALLOCATE_MAX_ADDRESS, EFI_LOADER_DATA, QDWC3_ARENA_SIZE / 4096, &address);
        if (status || (address & 0xfff)) { firmware_out(st, "REFUSED: no DMA arena below 4 GiB.\n"); return 0; }
        dma_arena = (uint8_t *)(uintptr_t)address;
        info.dma_base = address; info.dma_size = QDWC3_ARENA_SIZE;
        info.usb_dwc3 = USB_EBS_DWC3_BASE; info.usb_qscratch = USB_EBS_QSCRATCH_BASE;
        info.usb_snapshot = (uintptr_t)&boot.snapshot; info.usb_stats = 0;
        mode = MODE_USB_PROXY;
        info.mode = MODE_USB_PROXY;
        }
    } else {
        if (!uart.base || uart.type != 3) {
            firmware_out(st, "REFUSED: --uart-proxy needs an SPCR PL011 (QEMU); GENI is not qualified.\n");
            return refused;
        }
        mode = MODE_UART_PROXY;
        info.mode = MODE_UART_PROXY;
    }
    uint64_t heap = 0;
    if (st->boot->allocate_pages(0, 1 /* EfiLoaderCode */, HEAP_BYTES / 4096, &heap)) {
        firmware_out(st, "REFUSED: could not allocate the proxy heap.\n");
        mode = MODE_FOOTHOLD;
        return refused;
    }
    info.heap_base = heap; info.heap_size = HEAP_BYTES;
    /* A fixed region for chainloaded stages: they are linked for one of these
     * addresses, so no relocation is needed. The host picks the matching
     * binary by reading stage_base back out of bootinfo. */
    static const uint64_t stage_candidates[] = {0xb0000000, 0x4c000000, 0xa0000000};
    for (unsigned n = 0; n < sizeof(stage_candidates) / sizeof(stage_candidates[0]); n++) {
        uint64_t address = stage_candidates[n];
        if (!st->boot->allocate_pages(EFI_ALLOCATE_ADDRESS, 1 /* EfiLoaderCode */,
                                      STAGE_BYTES / 4096, &address)) {
            info.stage_base = address;
            info.stage_size = STAGE_BYTES;
            break;
        }
    }
    uart.enabled = 0; /* The proxy owns the UART byte stream. */
    efi_status status = leave_firmware(handle, st, "Leaving firmware for the q1n1 proxy.\n");
    mode = MODE_FOOTHOLD;
    return usb_mode ? 0 : status;
}

efi_status efi_main(efi_handle handle, struct efi_system_table *st)
{
    efi_guid loaded_guid = {0x5b1b31a1,0x9562,0x11d2,{0x8e,0x3f,0,0xa0,0xc9,0x69,0x72,0x3b}};
    struct efi_loaded_image *image = NULL;
    st->boot->handle_protocol(handle, &loaded_guid, (void **)&image);
    before_el = current_el();
    find_framebuffer(st);
    find_uart(st);
    firmware_out(st,"q1n1 AArch64 UEFI entry.\n");
    firmware_out(st,before_el == 2 ? "Entry EL: 2\n" : "Entry EL: 1\n");
    firmware_out(st,fb.pixels ? "GOP framebuffer captured.\n" : "No usable GOP framebuffer.\n");
    firmware_out(st,uart.base ? "ACPI SPCR UART found.\n" : "No usable ACPI SPCR UART.\n");
    if (option(image,"--boot") || option(image,"--uart-proxy")) return proxy_efi_main(handle, st, image);
    if (!option(image,"--el2")) {
        firmware_out(st,"Preflight only. No ExitBootServices or UART MMIO.\n");
        if (before_el == 2)
            firmware_out(st,"Already at EL2. Run q1n1.efi --el2 directly, or --boot for the A16 USB proxy.\n"
                            "Do not load slbounce or run sltest at EL2.\n");
        else
            firmware_out(st,"At EL1: qualify sltest before using slbounce.\n"
                            "Use SLBOUNCE_ALWAYS_SWITCH=1 for this ACPI target,\n"
                            "then run q1n1.efi --el2 (optional --uart).\n");
        return 0;
    }
    if (!fb.pixels || (before_el != 1 && before_el != 2)) return EFI_UNSUPPORTED;
    uart.enabled = option(image,"--uart") && uart.base && (uart.type == 3 || uart.type == 0x13);
    fault_test = option(image,"--fault-test");
    return leave_firmware(handle, st, "Leaving firmware now. Green bar = EL2; amber = EL1.\n");
}
#else /* Q1N1_STAGE: a flat payload chainloaded over the proxy */

/* Entered from stage-entry in entry.S with the inherited bootinfo. Firmware is
 * long gone; everything this needs was recorded before ExitBootServices. */
extern char __stage_base[]; /* the address this image was linked for */

extern const uint64_t q1n1_stage_config;

void q1n1_stage_main(struct q1n1_bootinfo *inherited)
{
    stage_config = q1n1_stage_config;
    memset(&info, 0, sizeof(info));
    memcpy(&info, inherited, inherited->size < sizeof(info) ? inherited->size : sizeof(info));
    info.size = sizeof(info);
    info.stage_generation++;
    /* Where this stage actually runs, which is not necessarily the region base:
     * successive chainloads alternate slots, and the host picks the free one by
     * comparing image_base against each candidate's link address. */
    info.image_base = (uintptr_t)__stage_base;
    info.image_size = info.stage_size;
    info.proxy_stats = (uintptr_t)&q1n1_proxy_stats;
    info.exception = (uintptr_t)&exception;
    info.usb_stats = 0;
    info.gic_stats = (uintptr_t)&gic;
    hz = info.timer_hz;
    rt = (struct efi_runtime_services *)(uintptr_t)info.runtime_services;
    mode = (int)info.mode;
    dma_arena = (uint8_t *)(uintptr_t)info.dma_base;
    uart.base = 0;
    uart.enabled = 0;
    if (mode == MODE_UART_PROXY) {
        /* The PL011 base is not in bootinfo; rediscover it from ACPI. */
        const uint8_t *rsdp = (const void *)(uintptr_t)info.acpi_rsdp;
        if (rsdp) {
            const uint8_t *xsdt = (const void *)(uintptr_t)get64(rsdp + 24);
            uint32_t size = xsdt ? get32(xsdt + 4) : 0;
            for (uint32_t offset = 36; xsdt && offset + 8 <= size; offset += 8) {
                const uint8_t *table = (const void *)(uintptr_t)get64(xsdt + offset);
                if (!table || !same(table, "SPCR", 4)) continue;
                uart.type = table[36];
                uart.base = get64(table + 44);
                break;
            }
        }
    }
    /* No snapshot means the payload never took the device-mode controller
     * over, which is how a dock-free boot looks. That is a property of the
     * boot, so every stage after it inherits it. */
    no_cdc = mode == MODE_USB_PROXY && !info.usb_snapshot;
    if (info.usb_snapshot)
        usb_ebs_saved((const struct usb_ebs_snapshot *)(uintptr_t)info.usb_snapshot, &saved);
    fbcon_init_fields(info.fb_base, (uint32_t)info.fb_width, (uint32_t)info.fb_height,
                      (uint32_t)info.fb_stride, (uint32_t)info.fb_format);
    fb.pixels = (void *)(uintptr_t)info.fb_base;
    fb.width = (uint32_t)info.fb_width; fb.height = (uint32_t)info.fb_height;
    fb.stride = (uint32_t)info.fb_stride; fb.format = (uint32_t)info.fb_format;
    fb.red = info.fb_format == 0 ? 0xff : 0xff0000;
    fb.green = 0xff00;
    fb.blue = info.fb_format == 0 ? 0xff0000 : 0xff;
    fb.scale = fb.width >= 1600 ? 3 : 2;
    proxy_main();
}
#endif /* Q1N1_STAGE */
