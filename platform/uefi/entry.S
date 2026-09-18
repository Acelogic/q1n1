/* SPDX-License-Identifier: MIT */
#ifdef Q1N1_STAGE
/* A chainloaded stage is a flat binary: entry at offset 0, x0 = the inherited
 * bootinfo. Firmware is gone, so this sets up its own stack, zeroes its own
 * .bss (which the flat image does not carry) and installs its own vectors. */
.section .text.start,"ax"
.globl _start
_start:
    b .Lstage_start
    .p2align 3
    .quad 0x3147545331314e51        /* "Q1N1STG1" */
    .quad __stage_base
.globl q1n1_stage_config
q1n1_stage_config:
    .quad 0                         /* patched in the image by the host */
.Lstage_start:
    msr daifset, #15
    mov x19, x0
    adrp x9, q1n1_stack_top
    add x9, x9, :lo12:q1n1_stack_top
    mov sp, x9
    adrp x10, __bss_start
    add x10, x10, :lo12:__bss_start
    adrp x11, __bss_end
    add x11, x11, :lo12:__bss_end
1:  cmp x10, x11
    b.hs 2f
    str xzr, [x10], #8
    b 1b
2:  adrp x9, q1n1_vectors
    add x9, x9, :lo12:q1n1_vectors
    mrs x10, CurrentEL
    cmp x10, #8
    b.ne 3f
    msr vbar_el2, x9
    b 4f
3:  msr vbar_el1, x9
4:  isb
    mov x0, x19
    bl q1n1_stage_main
5:  wfe
    b 5b
#endif

.text
.p2align 4
.globl q1n1_enter
q1n1_enter:
    msr daifset, #15
    adrp x9, q1n1_stack_top
    add x9, x9, :lo12:q1n1_stack_top
    mov sp, x9
    adrp x9, q1n1_vectors
    add x9, x9, :lo12:q1n1_vectors
    mrs x10, CurrentEL
    cmp x10, #8
    b.ne 1f
    msr vbar_el2, x9
    b 2f
1:  msr vbar_el1, x9
2:  isb
    bl q1n1_main
3:  wfe
    b 3b

#ifdef Q1N1_EXCEPTION_FRAME
/* Recoverable vectors for q1n1.efi: save x0-x30 and the interrupted SP in a
 * 272-byte frame, call q1n1_exception(frame, vector index), restore, eret.
 * The handler may change ELR/SPSR and the saved registers. */
.p2align 11
q1n1_vectors:
.irp index, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15
    .p2align 7
    sub sp, sp, #272
    stp x0, x1, [sp, #0]
    mov x1, #\index
    b q1n1_exception_entry
.endr

q1n1_exception_entry:
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x10, x11, [sp, #80]
    stp x12, x13, [sp, #96]
    stp x14, x15, [sp, #112]
    stp x16, x17, [sp, #128]
    stp x18, x19, [sp, #144]
    stp x20, x21, [sp, #160]
    stp x22, x23, [sp, #176]
    stp x24, x25, [sp, #192]
    stp x26, x27, [sp, #208]
    stp x28, x29, [sp, #224]
    add x2, sp, #272
    stp x30, x2, [sp, #240]
    mov x0, sp
    bl q1n1_exception
    ldr x30, [sp, #240]
    ldp x28, x29, [sp, #224]
    ldp x26, x27, [sp, #208]
    ldp x24, x25, [sp, #192]
    ldp x22, x23, [sp, #176]
    ldp x20, x21, [sp, #160]
    ldp x18, x19, [sp, #144]
    ldp x16, x17, [sp, #128]
    ldp x14, x15, [sp, #112]
    ldp x12, x13, [sp, #96]
    ldp x10, x11, [sp, #80]
    ldp x8, x9, [sp, #64]
    ldp x6, x7, [sp, #48]
    ldp x4, x5, [sp, #32]
    ldp x2, x3, [sp, #16]
    ldp x0, x1, [sp, #0]
    add sp, sp, #272
    eret

.bss
.p2align 4
q1n1_stack:
    .space 262144
q1n1_stack_top:
#else
.p2align 11
q1n1_vectors:
.rept 16
    b q1n1_exception
    .space 124
.endr
q1n1_exception:
    msr daifset, #15
    mrs x3, CurrentEL
    cmp x3, #8
    b.ne 4f
    mrs x0, esr_el2
    mrs x1, elr_el2
    mrs x2, far_el2
    b q1n1_fault
4:  mrs x0, esr_el1
    mrs x1, elr_el1
    mrs x2, far_el1
    b q1n1_fault

.bss
.p2align 4
q1n1_stack:
    .space 65536
q1n1_stack_top:
#endif

#ifdef Q1N1_STAGE
.section .rodata,"a"
#else
.section .rdata,"dr"
#endif
.p2align 4
.globl q1n1_logo
q1n1_logo:
    .incbin "data/bootlogo_256.bin"

/* m1n1's console font (Source Code Pro Bold, 16x32, one coverage byte per
 * pixel), so q1n1 lays out the same screen. The emblem above serves both
 * q1n1 placements: centred where m1n1 puts its logo, or parked in a corner.
 * The upstream Asahi mark stays embedded too, for --logo asahi. */
.p2align 4
.globl q1n1_font
q1n1_font:
    .incbin "font/font_retina.bin"
.p2align 4
.globl q1n1_asahi_logo
q1n1_asahi_logo:
    .incbin "data/asahi_bootlogo_256.bin"
