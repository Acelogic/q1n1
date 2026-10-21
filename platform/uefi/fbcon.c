/* SPDX-License-Identifier: MIT */
/* Framebuffer console for post-ExitBootServices apps. The 5x7 font and GOP
 * validation are the independently authored ones from main.c; main.c keeps
 * its own copy so the physically tested EL2 payload source is unchanged. */
#include "fbcon.h"

static struct {
    volatile uint32_t *pixels;
    uint32_t width, height, stride, red, green, blue, row, col, scale;
} fb;

static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-./=?()_+";
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
    {0,0,0,0,0,0,31},{0,4,4,31,4,4,0}
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
    return component((uint8_t)(rgb >> 16), fb.red) | component((uint8_t)(rgb >> 8), fb.green) |
           component((uint8_t)rgb, fb.blue);
}
static void rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t rgb)
{
    if (!fb.pixels) return;
    uint32_t v = color(rgb);
    for (uint32_t j = 0; j < h && y + j < fb.height; j++)
        for (uint32_t i = 0; i < w && x + i < fb.width; i++) fb.pixels[(uint64_t)(y + j) * fb.stride + x + i] = v;
}
int fbcon_init_fields(uint64_t base, uint32_t width, uint32_t height, uint32_t stride, uint32_t format)
{
    if (!base || !width || !height || width > stride || format > 2) return 0;
    fb.pixels = (void *)(uintptr_t)base;
    fb.width = width; fb.height = height; fb.stride = stride;
    fb.red = format == 0 ? 0xff : 0xff0000;
    fb.green = 0xff00;
    fb.blue = format == 0 ? 0xff0000 : 0xff;
    fb.scale = fb.width >= 1600 ? 3 : 2;
    fb.row = fb.col = 0;
    return 1;
}
int fbcon_init(struct efi_system_table *st)
{
    efi_guid guid = {0x9042a9de,0x23dc,0x4a38,{0x96,0xfb,0x7a,0xde,0xd0,0x80,0x51,0x6a}};
    struct efi_gop *gop = NULL;
    if (st->boot->locate_protocol(&guid, NULL, (void **)&gop) || !gop || !gop->mode || !gop->mode->info) return 0;
    struct efi_gop_info *info = gop->mode->info;
    if (gop->mode->info_size < sizeof(*info) || info->format > 2 || !info->width || !info->height ||
        info->width > info->stride || (uint64_t)info->stride * info->height > gop->mode->framebuffer_size / 4 ||
        !gop->mode->framebuffer) return 0;
    fb.pixels = (void *)(uintptr_t)gop->mode->framebuffer;
    fb.width = info->width; fb.height = info->height; fb.stride = info->stride;
    fb.red = info->format == 0 ? 0xff : 0xff0000;
    fb.green = 0xff00;
    fb.blue = info->format == 0 ? 0xff0000 : 0xff;
    if (info->format == 2) { fb.red = info->red; fb.green = info->green; fb.blue = info->blue; }
    fb.scale = fb.width >= 1600 ? 3 : 2;
    return 1;
}
void fbcon_clear(uint32_t rgb) { rect(0, 0, fb.width, fb.height, rgb); fb.row = fb.col = 0; }
void fbcon_bar(uint32_t rgb) { rect(0, 0, fb.width, 8, rgb); }
void fbcon_at(uint32_t row, uint32_t col) { fb.row = row; fb.col = col; }
void fbcon_clear_row(uint32_t row) { rect(0, 20 + row * 9 * fb.scale, fb.width, 9 * fb.scale, 0x080b10); }
static void put(char c)
{
    if (c == '\n') { fb.col = 0; fb.row++; return; }
    if (c >= 'a' && c <= 'z') c -= 32;
    if (20 + (fb.col + 1) * 6 * fb.scale >= fb.width) { fb.col = 0; fb.row++; }
    uint32_t y = 20 + fb.row * 9 * fb.scale;
    if (y + 7 * fb.scale >= fb.height) return;
    for (unsigned g = 0; g < sizeof(alphabet) - 1; g++) if (alphabet[g] == c) {
        for (unsigned j = 0; j < 7; j++) for (unsigned i = 0; i < 5; i++)
            if (glyphs[g][j] & (1u << (4 - i)))
                rect(20 + fb.col * 6 * fb.scale + i * fb.scale, y + j * fb.scale, fb.scale, fb.scale, 0xe8edf5);
        break;
    }
    fb.col++;
}
void fbcon_puts(const char *s) { while (*s) put(*s++); __asm__ volatile("dsb sy" ::: "memory"); }
void fbcon_hex(uint64_t v)
{
    char s[19] = "0X0000000000000000";
    for (unsigned i = 0; i < 16; i++) { s[17 - i] = "0123456789ABCDEF"[v & 15]; v >>= 4; }
    fbcon_puts(s);
}
void fbcon_dec(uint64_t v)
{
    char s[21]; unsigned n = sizeof(s) - 1; s[n] = 0;
    do { s[--n] = (char)('0' + v % 10); v /= 10; } while (v && n);
    fbcon_puts(s + n);
}
void fbcon_field(const char *label, uint64_t value) { fbcon_puts(label); fbcon_hex(value); fbcon_puts("\n"); }
