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

/* The panel on this machine is OLED, and this console is the longest-lived
 * thing on it: the proxy status block redraws the same labels at the same
 * coordinates for as long as the machine is up, which at the time of writing
 * was 19,728 seconds in one sitting. Static lit sub-pixels for hours is exactly
 * how burn-in happens.
 *
 * Two things are done about it.
 *
 * The glyph coverage value is scaled down before it becomes a grey level. The
 * font is antialiased and its peaks hit 255; a status panel does not need the
 * brightest white the panel can make, and dimming costs nothing but a multiply.
 *
 * And the whole image moves. Everything the console draws is offset by
 * (shift_x, shift_y), and con_shift_next() walks that offset around a grid --
 * carrying the pixels already on screen with it, so the boot log is not lost to
 * save the panel. The step is larger than a glyph stroke so a lit pixel in one
 * position is dark in the next. */
#define CON_MAX_LEVEL 0xc0
#define SHIFT_STEP 8
#define SHIFT_GRID 5
/* How far the grid can carry the image from its origin, in either axis. */
#define SHIFT_TRAVEL ((SHIFT_GRID - 1) * SHIFT_STEP)

extern const uint8_t q1n1_font[];

static struct {
    volatile uint32_t *pixels;
    uint32_t width, height, stride, red, green, blue;
    uint32_t rows, cols, row, col;
    uint32_t shift_x, shift_y, shift_n;
    uint32_t text_x;                    /* which side of the panel the text sits on */
    const uint8_t *emblem;              /* redrawn on the far side from the text */
    uint32_t emblem_size, emblem_x, emblem_y, swap_n;
    int retains;                        /* panel holds a static image: keep moving */
    int ready;
} con;

static uint8_t dim(uint8_t value)
{
    return (uint8_t)(((uint32_t)value * CON_MAX_LEVEL) / 0xff);
}
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
/* Every pixel the console draws goes through here, so the shift is applied here
 * and nowhere else -- one place to be right rather than one per drawing call. */
static void put_pixel(uint32_t x, uint32_t y, uint32_t value)
{
    x += con.shift_x;
    y += con.shift_y;
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

void con_reserve_emblem(uint32_t size)
{
    if (!con.ready || con.width <= size) return;
    uint32_t free_px = (con.width - size) / 2;           /* space beside the image */
    /* Leave the grid its travel. Without this the text is sized to fill the
     * space exactly, and then the first shift pushes its right edge into the
     * image -- and there is no room to park the same column on the far side. */
    if (free_px <= SHIFT_TRAVEL) return;
    free_px -= SHIFT_TRAVEL;
    uint32_t cols = free_px / FONT_WIDTH;
    if (cols > MARGIN_COLS + 1) cols -= MARGIN_COLS + 1; /* keep a gap */
    if (cols && cols < con.cols) con.cols = cols;
}
void con_retains(int panel_retains) { con.retains = panel_retains; }

/* The width the text column occupies, including its margins and the room the
 * grid needs to step into. */
static uint32_t text_block(void)
{
    return (con.cols + 2 * MARGIN_COLS) * FONT_WIDTH + SHIFT_TRAVEL;
}
/* Where the emblem goes: centred in the half the text is not using, so the two
 * trade places on a swap. A solid mark is the worst thing on this panel -- it
 * is saturated, it is dense, and it never changes -- so it has to travel
 * further than the 32 pixels the grid gives it. If there is no room to take
 * sides, it stays centred and only the grid moves it. */
static void emblem_place(uint32_t *x, uint32_t *y)
{
    uint32_t block = text_block();
    /* Height moves on a three-step cycle while the side alternates on a two-step
     * one, so the mark works through six places before it repeats rather than
     * two -- at five minutes a swap that is half an hour of travel. Step zero is
     * centred, so the screen looks deliberate the moment it is drawn. */
    uint32_t base = (con.height - con.emblem_size) / 2;
    uint32_t quarter = (con.height - con.emblem_size) / 4;
    switch (con.swap_n % 3) {
    case 1:  *y = base + quarter; break;
    case 2:  *y = base - quarter; break;
    default: *y = base; break;
    }
    if (block >= con.width || con.emblem_size >= con.width - block) {
        *x = (con.width - con.emblem_size) / 2;
        return;
    }
    uint32_t free_start = con.text_x ? 0 : block;        /* the half without text */
    *x = free_start + ((con.width - block) - con.emblem_size) / 2;
}
void con_emblem(const uint8_t *image, uint32_t size)
{
    if (!con.ready || !image || !size || size > con.width || size > con.height) return;
    con.emblem = image;
    con.emblem_size = size;
    emblem_place(&con.emblem_x, &con.emblem_y);
    con_logo_at(image, size, con.emblem_x, con.emblem_y);
}
uint32_t con_rows(void) { return con.rows; }
uint32_t con_cols(void) { return con.cols; }
uint32_t con_row(void) { return con.row; }
uint32_t con_width(void) { return con.width; }
uint32_t con_height(void) { return con.height; }
void con_clear(void)
{
    /* Ignore the shift: a clear means the whole panel, including whatever the
     * previous offset left outside the shifted area. */
    uint32_t sx = con.shift_x, sy = con.shift_y;
    con.shift_x = con.shift_y = 0;
    fill(0, 0, con.width, con.height, 0x000000);
    con.shift_x = sx; con.shift_y = sy;
    con.row = con.col = 0;
    con.text_x = 0;
}

/* Move the console one step around the anti-burn-in grid, taking the pixels
 * already on screen with it.
 *
 * Carrying the image matters. The alternative is to shift the origin and clear,
 * which is three lines instead of thirty -- and throws away the boot log every
 * time it fires. The log is the reason anyone is looking at this screen, so the
 * pixels move with the text.
 *
 * The copy has to run in the direction that does not overwrite what it has not
 * read yet, which for a downward or rightward move means starting at the far
 * end. The strips the image vacates are blanked, or the old edge stays lit and
 * the whole exercise is pointless. */
void con_shift_next(void)
{
    if (!con.ready || !con.pixels || !con.retains) return;
    con.shift_n++;
    uint32_t nx = (con.shift_n % SHIFT_GRID) * SHIFT_STEP;
    uint32_t ny = ((con.shift_n / SHIFT_GRID) % SHIFT_GRID) * SHIFT_STEP;
    int dx = (int)nx - (int)con.shift_x;
    int dy = (int)ny - (int)con.shift_y;
    if (!dx && !dy) return;

    uint32_t limit = SHIFT_TRAVEL;
    if (con.width <= limit || con.height <= limit) return;
    uint32_t w = con.width - limit;              /* the area that always exists */
    uint32_t h = con.height - limit;

    for (uint32_t n = 0; n < h; n++) {
        uint32_t y = (dy > 0) ? (h - 1 - n) : n;
        volatile uint32_t *src = con.pixels + (uint64_t)(y + con.shift_y) * con.stride + con.shift_x;
        volatile uint32_t *dst = con.pixels + (uint64_t)(y + ny) * con.stride + nx;
        if (dx > 0)
            for (uint32_t x = w; x-- > 0;) dst[x] = src[x];
        else
            for (uint32_t x = 0; x < w; x++) dst[x] = src[x];
    }
    con.shift_x = nx;
    con.shift_y = ny;

    /* Blank everything outside the new position, in panel coordinates. */
    uint32_t sx = con.shift_x, sy = con.shift_y;
    con.shift_x = con.shift_y = 0;
    if (sy) fill(0, 0, con.width, sy, 0x000000);
    if (sy + h < con.height) fill(0, sy + h, con.width, con.height - (sy + h), 0x000000);
    if (sx) fill(0, sy, sx, h, 0x000000);
    if (sx + w < con.width) fill(sx + w, sy, con.width - (sx + w), h, 0x000000);
    con.shift_x = sx; con.shift_y = sy;
    __asm__ volatile("dsb sy" ::: "memory");
}
/* Park the text column on the other side of the panel, carrying the log across.
 *
 * The grid above moves the image 32 pixels. That is enough to stop one
 * sub-pixel holding one glyph stroke for hours, and it does nothing at all
 * about the larger pattern: with a centred emblem the text occupies one half of
 * the panel and the other half is black for as long as the machine is up. This
 * swaps those halves, so each side gets its turn being dark.
 *
 * The emblem does not move. It is drawn once, centred, through put_pixel, and
 * the swap deliberately touches only the text block on one side of it -- the
 * mirrored position is the same size by construction, because con_reserve_centre
 * sized the column to fit beside a centred image.
 *
 * No-op when the text already spans the panel, as it does with the corner
 * emblem: there is nowhere to put it. */
void con_swap_side(void)
{
    if (!con.ready || !con.pixels || !con.retains) return;
    uint32_t block = (con.cols + 2 * MARGIN_COLS) * FONT_WIDTH;
    if (block + SHIFT_TRAVEL >= con.width) return;
    uint32_t to = con.text_x ? 0 : con.width - block - SHIFT_TRAVEL;
    if (to == con.text_x) return;

    /* Clear the emblem before the text moves. The text is about to be copied
     * over where it stood, but only where the two happen to overlap, and a
     * saturated mark left burning in the background is the thing being fixed. */
    if (con.emblem) {
        uint32_t sx = con.shift_x, sy = con.shift_y;
        con.shift_x = con.shift_y = 0;
        fill(con.emblem_x + sx, con.emblem_y + sy, con.emblem_size, con.emblem_size, 0x000000);
        con.shift_x = sx; con.shift_y = sy;
    }

    uint32_t h = (con.rows + 2 * MARGIN_ROWS) * FONT_HEIGHT;
    if (con.shift_y + h > con.height) h = con.height - con.shift_y;
    uint32_t from_x = con.text_x + con.shift_x;
    uint32_t to_x = to + con.shift_x;

    /* Same direction rule as the grid copy: for a rightward move, start at the
     * far end, in case the two positions overlap on a narrower panel. */
    for (uint32_t y = 0; y < h; y++) {
        volatile uint32_t *row = con.pixels + (uint64_t)(y + con.shift_y) * con.stride;
        if (to_x > from_x)
            for (uint32_t x = block; x-- > 0;) row[to_x + x] = row[from_x + x];
        else
            for (uint32_t x = 0; x < block; x++) row[to_x + x] = row[from_x + x];
    }

    /* Blank what the text vacated -- the whole point is that the side it left
     * goes dark. Only the part that the new position does not already cover,
     * so an overlapping move does not erase what was just written. */
    uint32_t dead = from_x, dead_end = from_x + block;
    if (to_x > from_x) { if (dead_end > to_x) dead_end = to_x; }
    else               { if (dead < to_x + block) dead = to_x + block; }
    if (dead < dead_end) {
        uint32_t sx = con.shift_x, sy = con.shift_y;
        con.shift_x = con.shift_y = 0;
        fill(dead, sy, dead_end - dead, h, 0x000000);
        con.shift_x = sx; con.shift_y = sy;
    }
    con.text_x = to;
    /* Redraw rather than copy: the source bytes are still in the image, so the
     * mark lands exactly right on the side the text just left. */
    if (con.emblem) {
        con.swap_n++;
        emblem_place(&con.emblem_x, &con.emblem_y);
        con_logo_at(con.emblem, con.emblem_size, con.emblem_x, con.emblem_y);
    }
    __asm__ volatile("dsb sy" ::: "memory");
}
void con_bar(uint32_t rgb) { fill(0, 0, con.width, 8, rgb); }
void con_at(uint32_t row, uint32_t col) { con.row = row; con.col = col; }
void con_clear_row(uint32_t row)
{
    fill(con.text_x + MARGIN_COLS * FONT_WIDTH, (MARGIN_ROWS + row) * FONT_HEIGHT,
         con.cols * FONT_WIDTH, FONT_HEIGHT, 0x000000);
}

/* Scroll the whole text area up one row, as m1n1's fb_move_font_row chain does. */
static void scroll(void)
{
    /* This is the one drawing path that does not go through put_pixel -- it
     * moves whole rows -- so it carries the shift itself. */
    uint32_t top = MARGIN_ROWS * FONT_HEIGHT + con.shift_y;
    uint32_t left = con.text_x + MARGIN_COLS * FONT_WIDTH + con.shift_x;
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
    uint32_t x = con.text_x + (MARGIN_COLS + con.col) * FONT_WIDTH;
    uint32_t y = (MARGIN_ROWS + con.row) * FONT_HEIGHT;
    for (uint32_t j = 0; j < FONT_HEIGHT; j++)
        for (uint32_t i = 0; i < FONT_WIDTH; i++) {
            uint8_t value = dim(bitmap[j * FONT_WIDTH + i]);
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

/* The inverse of component(): recover a 0..255 channel from a packed pixel. */
static uint8_t channel(uint32_t value, uint32_t mask)
{
    if (!mask) return 0;
    unsigned shift = 0;
    while (!(mask & 1)) { mask >>= 1; shift++; }
    return (uint8_t)((uint64_t)((value >> shift) & mask) * 255 / mask);
}
static uint32_t peek_pixel(uint32_t x, uint32_t y)
{
    x += con.shift_x;
    y += con.shift_y;
    if (!con.pixels || x >= con.width || y >= con.height) return 0;
    return con.pixels[(uint64_t)y * con.stride + x];
}
/* Premultiplied source-over: the emblem composites onto whatever is already on
 * screen instead of stamping its own background over it. Premultiplied keeps
 * this to out = src + dst * (1 - a), and bounds each channel by 255 for free. */
void con_logo_at(const uint8_t *image, uint32_t size, uint32_t left, uint32_t top)
{
    if (!con.ready || !image || !size) return;
    for (uint32_t y = 0; y < size; y++)
        for (uint32_t x = 0; x < size; x++) {
            const uint8_t *pixel = image + ((uint64_t)y * size + x) * 4;
            uint32_t a = pixel[3];
            if (!a) continue;           /* fully transparent: leave the screen alone */
            uint32_t r = pixel[0], g = pixel[1], b = pixel[2];
            if (a < 255) {              /* an antialiased edge, so blend with the screen */
                uint32_t under = peek_pixel(left + x, top + y), inv = 255 - a;
                r += (uint32_t)channel(under, con.red) * inv / 255;
                g += (uint32_t)channel(under, con.green) * inv / 255;
                b += (uint32_t)channel(under, con.blue) * inv / 255;
            }
            put_pixel(left + x, top + y, colour(r << 16 | g << 8 | b));
        }
    __asm__ volatile("dsb sy" ::: "memory");
}
void con_logo(const uint8_t *image, uint32_t size)
{
    if (!con.ready || con.width < size || con.height < size) return;
    con_logo_at(image, size, (con.width - size) / 2, (con.height - size) / 2);
}
