/* SPDX-License-Identifier: MIT */
/* GOP framebuffer text console usable after ExitBootServices (same 5x7 font as main.c). */
#pragma once
#include "efi.h"

int fbcon_init(struct efi_system_table *st);
/* Same console from already-known geometry (a chainloaded stage has no GOP). */
int fbcon_init_fields(uint64_t base, uint32_t width, uint32_t height, uint32_t stride, uint32_t format);
void fbcon_clear(uint32_t rgb);
void fbcon_bar(uint32_t rgb);
void fbcon_at(uint32_t row, uint32_t col);
void fbcon_clear_row(uint32_t row);
void fbcon_puts(const char *s);
void fbcon_hex(uint64_t value);
void fbcon_dec(uint64_t value);
void fbcon_field(const char *label, uint64_t value);
