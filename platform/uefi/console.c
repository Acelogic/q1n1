/* SPDX-License-Identifier: MIT */
/* See console.h. Glyph lookup matches m1n1's font_get_pixel(): the byte at
 * font[(c - 0x20) * w * h + y * w + x] is the coverage value, written straight
 * as a grey level, which is why m1n1's text looks antialiased. */
#include "console.h"

#define FONT_WIDTH 16
#define FONT_HEIGHT 32
#define FONT_FIRST 0x20
#define FONT_LAST 0x7e
#define MARGIN_ROWS 1
#define MARGIN_COLS 1

extern const uint8_t q1n1_font[];

static struct {
    volatile uint32_t *pixels;
    uint32_t width, height, stride, red, green, blue;
    uint32_t rows, cols, row, col;
    int ready;
} con;

static uint32_t component(uint8_t value, uint32_t mask)
{
    if (!mask) return 0;
    unsigned shift = 0;
    while (!(mask & 1)) { mask >>= 1; shift++; }
    return (uint32_t)(((uint64_t)value * mask / 255) << shift);
}
static uint32_t colour(uint32_t rgb)
{
    return component((uint8_t)(rgb >> 16), con.red) | component((uint8_t)(rgb >> 8), con.green) |
           component((uint8_t)rgb, con.blue);
}
static void put_pixel(uint32_t x, uint32_t y, uint32_t value)
{
    if (con.pixels && x < con.width && y < con.height) con.pixels[(uint64_t)y * con.stride + x] = value;
}
static void fill(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t rgb)
{
    uint32_t value = colour(rgb);
    for (uint32_t j = 0; j < h && y + j < con.height; j++)
        for (uint32_t i = 0; i < w && x + i < con.width; i++) put_pixel(x + i, y + j, value);
    __asm__ volatile("dsb sy" ::: "memory");
}

int con_init(uint64_t base, uint32_t width, uint32_t height, uint32_t stride, uint32_t format)
{
    if (!base || !width || !height || width > stride || format > 2) return 0;
    con.pixels = (void *)(uintptr_t)base;
    con.width = width; con.height = height; con.stride = stride;
    con.red = format == 0 ? 0xff : 0xff0000;
    con.green = 0xff00;
    con.blue = format == 0 ? 0xff0000 : 0xff;
    con.cols = width / FONT_WIDTH;
    con.rows = height / FONT_HEIGHT;
    if (con.cols <= 2 * MARGIN_COLS || con.rows <= 2 * MARGIN_ROWS) return 0;
    con.cols -= 2 * MARGIN_COLS;
    con.rows -= 2 * MARGIN_ROWS;
    con.row = con.col = 0;
    con.ready = 1;
    return 1;
}

void con_reserve_centre(uint32_t size)
{
    if (!con.ready || con.width <= size) return;
    uint32_t free_px = (con.width - size) / 2;           /* space left of the image */
    uint32_t cols = free_px / FONT_WIDTH;
    if (cols > MARGIN_COLS + 1) cols -= MARGIN_COLS + 1; /* keep a gap */
    if (cols && cols < con.cols) con.cols = cols;
}
uint32_t con_rows(void) { return con.rows; }
uint32_t con_cols(void) { return con.cols; }
uint32_t con_row(void) { return con.row; }
uint32_t con_width(void) { return con.width; }
uint32_t con_height(void) { return con.height; }
void con_clear(void) { fill(0, 0, con.width, con.height, 0x000000); con.row = con.col = 0; }
void con_bar(uint32_t rgb) { fill(0, 0, con.width, 8, rgb); }
void con_at(uint32_t row, uint32_t col) { con.row = row; con.col = col; }
void con_clear_row(uint32_t row)
{
    fill(MARGIN_COLS * FONT_WIDTH, (MARGIN_ROWS + row) * FONT_HEIGHT,
         con.cols * FONT_WIDTH, FONT_HEIGHT, 0x000000);
}

/* Scroll the whole text area up one row, as m1n1's fb_move_font_row chain does. */
static void scroll(void)
{
    uint32_t top = MARGIN_ROWS * FONT_HEIGHT;
    uint32_t left = MARGIN_COLS * FONT_WIDTH;
    uint32_t span = con.cols * FONT_WIDTH;
    for (uint32_t y = 0; y + FONT_HEIGHT < con.rows * FONT_HEIGHT; y++) {
        volatile uint32_t *destination = con.pixels + (uint64_t)(top + y) * con.stride + left;
        volatile uint32_t *source = destination + (uint64_t)FONT_HEIGHT * con.stride;
        for (uint32_t x = 0; x < span; x++) destination[x] = source[x];
    }
    con_clear_row(con.rows - 1);
    __asm__ volatile("dsb sy" ::: "memory");
}

static void put(char c)
{
    if (!con.ready) return;
    if (c == '\r') { con.col = 0; return; }
    if (c == '\n') { con.col = 0; con.row++; }
    if (con.col >= con.cols) { con.col = 0; con.row++; }
    while (con.row >= con.rows) { scroll(); con.row--; }
    if (c == '\n') return;
    uint8_t glyph = (uint8_t)c;
    if (glyph < FONT_FIRST || glyph > FONT_LAST) glyph = '?';
    const uint8_t *bitmap = q1n1_font + (uint32_t)(glyph - FONT_FIRST) * FONT_WIDTH * FONT_HEIGHT;
    uint32_t x = (MARGIN_COLS + con.col) * FONT_WIDTH;
    uint32_t y = (MARGIN_ROWS + con.row) * FONT_HEIGHT;
    for (uint32_t j = 0; j < FONT_HEIGHT; j++)
        for (uint32_t i = 0; i < FONT_WIDTH; i++) {
            uint8_t value = bitmap[j * FONT_WIDTH + i];
            if (value) put_pixel(x + i, y + j, colour((uint32_t)value << 16 | (uint32_t)value << 8 | value));
        }
    con.col++;
}

void con_puts(const char *text)
{
    while (*text) put(*text++);
    __asm__ volatile("dsb sy" ::: "memory");
}
void con_hexn(uint64_t value, unsigned digits)
{
    char text[19] = "0x";
    if (digits > 16) digits = 16;
    for (unsigned n = 0; n < digits; n++) text[2 + n] = "0123456789abcdef"[(value >> (4 * (digits - 1 - n))) & 15];
    text[2 + digits] = 0;
    con_puts(text);
}
void con_hex(uint64_t value) { con_hexn(value, 16); }
void con_addr(uint64_t value) { con_hexn(value, (value >> 32) ? 16 : 8); }
void con_dec(uint64_t value)
{
    char text[21];
    unsigned n = 0;
    if (!value) { con_puts("0"); return; }
    while (value && n < sizeof(text) - 1) { text[n++] = (char)('0' + value % 10); value /= 10; }
    char out[21];
    for (unsigned k = 0; k < n; k++) out[k] = text[n - 1 - k];
    out[n] = 0;
    con_puts(out);
}
void con_field(const char *label, uint64_t value) { con_puts(label); con_hex(value); con_puts("\n"); }

void con_logo_at(const uint8_t *image, uint32_t size, uint32_t left, uint32_t top)
{
    if (!con.ready || !image || !size) return;
    for (uint32_t y = 0; y < size; y++)
        for (uint32_t x = 0; x < size; x++) {
            const uint8_t *pixel = image + ((uint64_t)y * size + x) * 4;
            put_pixel(left + x, top + y,
                      colour((uint32_t)pixel[0] << 16 | (uint32_t)pixel[1] << 8 | pixel[2]));
        }
    __asm__ volatile("dsb sy" ::: "memory");
}
void con_logo(const uint8_t *image, uint32_t size)
{
    if (!con.ready || con.width < size || con.height < size) return;
    con_logo_at(image, size, (con.width - size) / 2, (con.height - size) / 2);
}
