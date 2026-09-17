/* SPDX-License-Identifier: MIT */
/* Leaf memory helpers for the q1n1 proxy. None of them uses the stack or
 * changes x30, so an exception under Q1N1_GUARD_RETURN can resume at x30.
 * Sizes are in bytes and round down to the access width, as in m1n1. */
.text
.p2align 2

/* uint32_t q1n1_checksum_block(const uint8_t *data, uint64_t length, uint32_t sum) */
.globl q1n1_checksum_block
q1n1_checksum_block:
    mov w9, #31337
    mov w11, #0x5a
    cbz x1, 2f
1:  ldrb w10, [x0], #1
    mul w2, w2, w9
    eor w10, w10, w11
    add w2, w2, w10
    subs x1, x1, #1
    b.ne 1b
2:  mov w0, w2
    ret

/* void q1n1_copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t size) */
.globl q1n1_copy_bytes
q1n1_copy_bytes:
    cbz x2, 2f
1:  ldrb w3, [x1], #1
    strb w3, [x0], #1
    subs x2, x2, #1
    b.ne 1b
2:  ret

.globl q1n1_memcpy64
q1n1_memcpy64:
    ands x2, x2, #~7
    b.eq 2f
1:  ldr x3, [x1], #8
    str x3, [x0], #8
    subs x2, x2, #8
    b.ne 1b
2:  ret

.globl q1n1_memcpy32
q1n1_memcpy32:
    ands x2, x2, #~3
    b.eq 2f
1:  ldr w3, [x1], #4
    str w3, [x0], #4
    subs x2, x2, #4
    b.ne 1b
2:  ret

.globl q1n1_memcpy16
q1n1_memcpy16:
    ands x2, x2, #~1
    b.eq 2f
1:  ldrh w3, [x1], #2
    strh w3, [x0], #2
    subs x2, x2, #2
    b.ne 1b
2:  ret

.globl q1n1_memcpy8
q1n1_memcpy8:
    b q1n1_copy_bytes

.globl q1n1_memset64
q1n1_memset64:
    ands x2, x2, #~7
    b.eq 2f
1:  str x1, [x0], #8
    subs x2, x2, #8
    b.ne 1b
2:  ret

.globl q1n1_memset32
q1n1_memset32:
    ands x2, x2, #~3
    b.eq 2f
1:  str w1, [x0], #4
    subs x2, x2, #4
    b.ne 1b
2:  ret

.globl q1n1_memset16
q1n1_memset16:
    ands x2, x2, #~1
    b.eq 2f
1:  strh w1, [x0], #2
    subs x2, x2, #2
    b.ne 1b
2:  ret

.globl q1n1_memset8
q1n1_memset8:
    cbz x2, 2f
1:  strb w1, [x0], #1
    subs x2, x2, #1
    b.ne 1b
2:  ret
