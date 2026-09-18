/* SPDX-License-Identifier: MIT */
/* m1n1-style framebuffer console: the same Source Code Pro Bold glyphs m1n1
 * uses (font/font_retina.bin, 16x32, one antialiasing byte per pixel, chars
 * 0x20..0x7e), the same centred logo placement, and a scrolling log.
 * The old 5x7 console in fbcon.c stays for the foothold payload. */
#pragma once
#include <stdint.h>

int con_init(uint64_t base, uint32_t width, uint32_t height, uint32_t stride, uint32_t format);
void con_clear(void);
/* Both of these are for panels that retain a static image, and both are no-ops
 * until con_retains() says this machine has one. Safe to call from the proxy
 * loop; each costs one framebuffer copy.
 *
 * Step the whole console around a small grid, carrying the image with it, so a
 * panel left showing one page for hours does not keep the same sub-pixels lit. */
void con_shift_next(void);
/* Move the text column to the other side of the panel, carrying the log across,
 * so the half that has been lit goes dark. A centred emblem stays centred. */
void con_swap_side(void);
/* Whether this machine's panel retains a static image. Off by default: a panel
 * that does not need this should not have its console wandering about. */
void con_retains(int panel_retains);
void con_bar(uint32_t rgb);
void con_puts(const char *text);
void con_hex(uint64_t value);          /* 0x0123456789abcdef */
void con_hexn(uint64_t value, unsigned digits);
/* 8 digits below 4 GiB, 16 above: addresses never come out truncated. */
void con_addr(uint64_t value);
void con_dec(uint64_t value);
void con_field(const char *label, uint64_t value);
/* Blit a 256x256 RGBA image centred, as m1n1's fb_blit_logo does. */
void con_logo(const uint8_t *image, uint32_t size);
void con_logo_at(const uint8_t *image, uint32_t size, uint32_t left, uint32_t top);
/* Adopt an image as the console's emblem: drawn now, and moved to the far side
 * from the text on every con_swap_side(). A solid mark is the worst thing to
 * leave on a panel that retains, so it travels with the text rather than
 * sitting in one place for the session. Call con_reserve_emblem() first. */
void con_emblem(const uint8_t *image, uint32_t size);
/* Narrow the text column to one side of the panel, leaving the other side for
 * the emblem, which is why far fewer columns are reported than the panel holds. */
void con_reserve_emblem(uint32_t size);
void con_at(uint32_t row, uint32_t col);
void con_clear_row(uint32_t row);
uint32_t con_row(void);
uint32_t con_rows(void);
uint32_t con_cols(void);
uint32_t con_width(void);
uint32_t con_height(void);
