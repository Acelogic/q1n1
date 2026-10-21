/* SPDX-License-Identifier: MIT */
/* ELF64 ABI subset needed by GNU-EFI's AArch64 relocator on macOS. */
#pragma once
#include <stdint.h>
typedef struct { int64_t d_tag; union { uint64_t d_val, d_ptr; } d_un; } Elf64_Dyn;
typedef struct { uint64_t r_offset, r_info; int64_t r_addend; } Elf64_Rela;
#define DT_NULL 0
#define DT_RELA 7
#define DT_RELASZ 8
#define DT_RELAENT 9
#define ELF64_R_TYPE(info) ((uint32_t)(info))
#define R_AARCH64_NONE 0
#define R_AARCH64_RELATIVE 1027
