/* SPDX-License-Identifier: MIT */
/* m1n1-style framebuffer console: the same Source Code Pro Bold glyphs m1n1
 * uses (font/font_retina.bin, 16x32, one antialiasing byte per pixel, chars
 * 0x20..0x7e), the same centred logo placement, and a scrolling log.
 * The old 5x7 console in fbcon.c stays for the foothold payload. */
#pragma once
#include <stdint.h>

int con_init(uint64_t base, uint32_t width, uint32_t height, uint32_t stride, uint32_t format);
void con_clear(void);
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
/* Narrow the text column so a centred image has clear space beside it, which
 * is why m1n1 reports far fewer columns than the panel could hold. */
void con_reserve_centre(uint32_t size);
void con_at(uint32_t row, uint32_t col);
void con_clear_row(uint32_t row);
uint32_t con_row(void);
uint32_t con_rows(void);
uint32_t con_cols(void);
uint32_t con_width(void);
uint32_t con_height(void);
