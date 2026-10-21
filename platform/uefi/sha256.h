/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdint.h>
#include <stddef.h>
struct q1n1_sha256 { uint32_t h[8]; uint64_t bytes; uint8_t block[64]; };
void q1n1_sha256_init(struct q1n1_sha256 *s);
void q1n1_sha256_update(struct q1n1_sha256 *s, const void *data, size_t size);
void q1n1_sha256_final(struct q1n1_sha256 *s, uint8_t digest[32]);
void q1n1_sha256(const void *data, size_t size, uint8_t digest[32]);
