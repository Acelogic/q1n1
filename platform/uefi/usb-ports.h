/* SPDX-License-Identifier: MIT */
#pragma once
#include "qcom-dwc3.h"
#include "ncm-proxy.h"

#define Q1N1_USB_PORTS 2
#define Q1N1_USB_ARENA_SIZE (4u << 20)
#define Q1N1_USB_HOST_ARENA (256u << 10)

enum q1n1_usb_role { Q1N1_USB_OFF, Q1N1_USB_HOST, Q1N1_USB_DEVICE, Q1N1_USB_UNSUPPORTED };

/* Stable, all-u64 diagnostic records exposed through bootinfo. */
struct q1n1_usb_port_status {
    uint64_t base, role, connected, attempts, disconnects, last_error;
    uint64_t device_stats, host_stats, ncm_stats, proxy_stats;
};

struct q1n1_usb_port {
    struct qdwc3 device;
    struct qdwc3_saved saved;
    struct xhci host;
    struct ncm ncm;
    struct ncm_proxy proxy;
    uintptr_t base, qscratch;
    uint8_t *device_dma, *host_dma;
    uint64_t deadline, started, activity, down_since, retry_at, suspended_since;
    uint32_t qs_hs, qs_ss, failures, next_role, console_ready;
    int checksum_state;
};

struct q1n1_usb_ports {
    uint64_t magic, version, size;
    struct q1n1_usb_port_status status[Q1N1_USB_PORTS];
    struct q1n1_usb_port port[Q1N1_USB_PORTS];
    struct q1n1_io io;
    int selected;
    volatile int busy;
};

/* The arena is reserved at the top of the existing host-scratch heap. */
struct q1n1_usb_ports *q1n1_usb_ports_init(uint8_t *arena, uint64_t size, int adopt);
void q1n1_usb_ports_poll(struct q1n1_usb_ports *s);
void q1n1_usb_ports_tick(struct q1n1_usb_ports *s);
int q1n1_usb_ports_handoff(struct q1n1_usb_ports *s);
void q1n1_usb_ports_stop(struct q1n1_usb_ports *s);
uint64_t q1n1_usb_ports_refresh(struct q1n1_usb_ports *s);
