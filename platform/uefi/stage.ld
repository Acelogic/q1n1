/* SPDX-License-Identifier: MIT */
/* Flat chainloadable stage. Linked for one fixed address (passed with
 * --defsym=STAGE_BASE=...) that the running payload reserved before
 * ExitBootServices, so no runtime relocation is needed. */
ENTRY(_start)
SECTIONS
{
    . = STAGE_BASE;
    __stage_base = STAGE_BASE;
    .text : {
        KEEP(*(.text.start))
        *(.text .text.*)
    }
    .rodata : { *(.rodata .rodata.*) }
    .data : { *(.data .data.*) }
    . = ALIGN(16);
    __bss_start = .;
    .bss : { *(.bss .bss.*) *(COMMON) }
    . = ALIGN(16);
    __bss_end = .;
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.interp) }
}
