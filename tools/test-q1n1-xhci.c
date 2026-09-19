/* SPDX-License-Identifier: MIT */
/* Native harness for platform/uefi/xhci.c and ncm.c.
 *
 * A simulated xHCI controller and a simulated CDC-NCM device stand in for the
 * hardware: the simulator owns the MMIO window, walks the command and transfer
 * rings when a doorbell is rung, and writes real events back into the driver's
 * event ring. That exercises the parts that are expensive to get wrong on the
 * A16 -- ring wrap through the Link TRB, cycle-bit handling, the deferred event
 * cache, NTB framing in both directions -- with sanitizers watching.
 *
 * It is not a conformance model of xHCI. It answers the way the real controller
 * answered when tools/a16xhci.py drove it, which is the behaviour this driver
 * has to match.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "xhci.h"
#include "ncm.h"

static int checks;
static int failures;

static void check(int condition, const char *label)
{
    if (condition) {
        checks++;
    } else {
        failures++;
        printf("FAIL: %s\n", label);
    }
}

/* ---- simulated controller ------------------------------------------- */

#define SIM_MMIO 0x10000
#define SIM_SLOTS 8
#define SIM_PORTS 2
#define CAPLENGTH 0x30
#define DBOFF 0x2000
#define RTSOFF 0x1000

struct sim_slot {
    int used, addressed;
    uint64_t device_context;
    uint64_t ep_ring[XHCI_MAX_DCI];
    uint32_t ep_cycle[XHCI_MAX_DCI];
};

static struct {
    uint8_t mmio[SIM_MMIO];
    uintptr_t base;
    uint64_t now_us;

    uint64_t cmd_ring;
    uint32_t cmd_cycle;
    uint64_t event_ring;
    uint32_t event_size, event_index, event_cycle;

    struct sim_slot slot[SIM_SLOTS];
    int running;
    int reset_stuck;

    /* the simulated NCM device */
    uint32_t configuration, alternate[4];
    uint8_t pending_ntb[4096];
    uint32_t pending_ntb_length;
    uint8_t last_out[4096];
    uint32_t last_out_length;
    uint32_t control_stalls;
    uint32_t ntb_input_size;
    uint64_t transfers;
    /* An IN TRB the driver queued while nothing was available. Real hardware
     * completes it the moment the device sends, with no further doorbell, so
     * the simulator has to remember it and finish it when data is queued. */
    uint32_t waiting_slot, waiting_dci;
} sim;

static uint32_t op_offset(void) { return CAPLENGTH; }

static void mmio_w32(uint32_t offset, uint32_t value)
{
    memcpy(sim.mmio + offset, &value, 4);
}
static uint32_t mmio_r32(uint32_t offset)
{
    uint32_t value;
    memcpy(&value, sim.mmio + offset, 4);
    return value;
}
static uint64_t mmio_r64(uint32_t offset)
{
    uint64_t value;
    memcpy(&value, sim.mmio + offset, 8);
    return value;
}

static void sim_event(uint64_t parameter, uint32_t status, uint32_t control)
{
    uint8_t *slot = (uint8_t *)(uintptr_t)(sim.event_ring + sim.event_index * XHCI_TRB_SIZE);
    uint32_t words[4] = {(uint32_t)parameter, (uint32_t)(parameter >> 32), status,
                         (control & ~1u) | sim.event_cycle};
    memcpy(slot, words, sizeof(words));
    sim.event_index++;
    if (sim.event_index == sim.event_size) {
        sim.event_index = 0;
        sim.event_cycle ^= 1;
    }
}

/* ---- the simulated USB device ---------------------------------------- */

static const uint8_t device_descriptor[18] = {
    18, 1, 0x10, 0x02, 0, 0, 0, 64, 0xac, 0x05, 0x05, 0x19, 0x00, 0x0f, 1, 2, 3, 1,
};

/* Two NCM functions, like the Mac's: interfaces 0/1 and 2/3. */
static const uint8_t configuration_descriptor[] = {
    9, 2, 0, 0, 4, 1, 0, 0x00, 0xc0,               /* total length patched below */
    /* function A */
    9, 4, 0, 0, 0, 0x02, 0x0d, 0x00, 0,
    5, 0x24, 0x00, 0x10, 0x01,
    5, 0x24, 0x06, 0x00, 0x01,                     /* union: control 0, data 1 */
    13, 0x24, 0x0f, 4, 0, 0, 0, 0, 0x8e, 0x3e, 0, 0, 0,   /* iMACAddress = 4 */
    6, 0x24, 0x1a, 0x00, 0x01, 0xbb,
    9, 4, 1, 0, 0, 0x0a, 0x00, 0x01, 0,
    9, 4, 1, 1, 2, 0x0a, 0x00, 0x01, 0,
    7, 5, 0x81, 0x02, 0x00, 0x02, 0,               /* bulk IN 1, 512 */
    7, 5, 0x01, 0x02, 0x00, 0x02, 0,               /* bulk OUT 1, 512 */
    /* function B */
    9, 4, 2, 0, 0, 0x02, 0x0d, 0x00, 0,
    5, 0x24, 0x00, 0x10, 0x01,
    5, 0x24, 0x06, 0x02, 0x03,                     /* union: control 2, data 3 */
    13, 0x24, 0x0f, 5, 0, 0, 0, 0, 0x8e, 0x3e, 0, 0, 0,   /* iMACAddress = 5 */
    6, 0x24, 0x1a, 0x00, 0x01, 0xbb,
    9, 4, 3, 0, 0, 0x0a, 0x00, 0x01, 0,
    9, 4, 3, 1, 2, 0x0a, 0x00, 0x01, 0,
    7, 5, 0x82, 0x02, 0x00, 0x02, 0,               /* bulk IN 2, 512 */
    7, 5, 0x02, 0x02, 0x00, 0x02, 0,               /* bulk OUT 2, 512 */
};

static const char *mac_strings[] = {"127760EB84A7", "127760EB8487"};

static const uint8_t ntb_parameters[28] = {
    28, 0, 0x03, 0,                                 /* wLength, formats */
    0xfc, 0x7f, 0, 0,                               /* dwNtbInMaxSize 32764 */
    4, 0, 0, 0, 4, 0, 0, 0,                         /* in divisor/remainder/align */
    0xfc, 0x7f, 0, 0,                               /* dwNtbOutMaxSize 32764 */
    4, 0, 0, 0, 4, 0, 0, 2,                         /* out divisor/remainder/align, 512 */
};

/* Fill `buffer` for a control IN request; returns the byte count, -1 to stall. */
static int device_control(uint32_t request_type, uint32_t request, uint32_t value,
                          uint32_t index, uint8_t *buffer, uint32_t length)
{
    if (request_type == 0x80 && request == 6) {         /* GET_DESCRIPTOR */
        uint32_t kind = value >> 8, which = value & 0xff;
        if (kind == 1) {
            uint32_t n = length < sizeof(device_descriptor) ? length : sizeof(device_descriptor);
            memcpy(buffer, device_descriptor, n);
            return (int)n;
        }
        if (kind == 2) {
            static uint8_t full[sizeof(configuration_descriptor)];
            memcpy(full, configuration_descriptor, sizeof(full));
            full[2] = (uint8_t)sizeof(full);
            full[3] = (uint8_t)(sizeof(full) >> 8);
            uint32_t n = length < sizeof(full) ? length : sizeof(full);
            memcpy(buffer, full, n);
            return (int)n;
        }
        if (kind == 3 && (which == 4 || which == 5)) {
            const char *text = mac_strings[which - 4];
            uint32_t characters = (uint32_t)strlen(text);
            uint32_t total = 2 + characters * 2;
            uint8_t built[64];
            built[0] = (uint8_t)total;
            built[1] = 3;
            for (uint32_t k = 0; k < characters; k++) {
                built[2 + k * 2] = (uint8_t)text[k];
                built[3 + k * 2] = 0;
            }
            uint32_t n = length < total ? length : total;
            memcpy(buffer, built, n);
            return (int)n;
        }
        return -1;
    }
    if (request_type == 0x00 && request == 9) { sim.configuration = value; return 0; }
    if (request_type == 0x01 && request == 11) {
        if (index < 4) sim.alternate[index] = value;
        return 0;
    }
    if (request_type == 0xa1 && request == 0x80) {      /* GET_NTB_PARAMETERS */
        uint32_t n = length < sizeof(ntb_parameters) ? length : sizeof(ntb_parameters);
        memcpy(buffer, ntb_parameters, n);
        return (int)n;
    }
    if (request_type == 0x21 && request == 0x86 && length >= 4) {   /* SET_NTB_INPUT_SIZE */
        sim.ntb_input_size = (uint32_t)buffer[0] | ((uint32_t)buffer[1] << 8) |
                             ((uint32_t)buffer[2] << 16) | ((uint32_t)buffer[3] << 24);
        return 0;
    }
    if (request_type == 0x21 && request == 0x43) return 0;   /* packet filter */
    return -1;
}

/* ---- ring walking ---------------------------------------------------- */

static void read_trb(uint64_t address, uint64_t *parameter, uint32_t *status, uint32_t *control)
{
    uint32_t words[4];
    memcpy(words, (void *)(uintptr_t)address, sizeof(words));
    *parameter = (uint64_t)words[0] | ((uint64_t)words[1] << 32);
    *status = words[2];
    *control = words[3];
}

static void process_command_ring(void)
{
    for (;;) {
        uint64_t parameter;
        uint32_t status, control;
        read_trb(sim.cmd_ring, &parameter, &status, &control);
        if ((control & 1u) != sim.cmd_cycle) return;
        uint32_t type = (control >> 10) & 0x3f;
        uint64_t address = sim.cmd_ring;

        if (type == 6) {                                  /* Link */
            sim.cmd_ring = parameter;
            if (control & (1u << 1)) sim.cmd_cycle ^= 1;
            continue;
        }
        sim.cmd_ring += XHCI_TRB_SIZE;

        if (type == 9) {                                  /* Enable Slot */
            uint32_t chosen = 0;
            for (uint32_t n = 1; n < SIM_SLOTS; n++)
                if (!sim.slot[n].used) { chosen = n; break; }
            if (!chosen) { sim_event(address, 9u << 24, (33u << 10)); continue; }
            memset(&sim.slot[chosen], 0, sizeof(sim.slot[chosen]));
            sim.slot[chosen].used = 1;
            sim_event(address, 1u << 24, (33u << 10) | (chosen << 24));
        } else if (type == 10) {                          /* Disable Slot */
            uint32_t s = (control >> 24) & 0xff;
            if (s < SIM_SLOTS) memset(&sim.slot[s], 0, sizeof(sim.slot[s]));
            sim_event(address, 1u << 24, (33u << 10) | (s << 24));
        } else if (type == 11 || type == 12) {            /* Address / Configure */
            uint32_t s = (control >> 24) & 0xff;
            if (s >= SIM_SLOTS || !sim.slot[s].used) {
                sim_event(address, 11u << 24, (33u << 10) | (s << 24));
                continue;
            }
            uint8_t *input = (uint8_t *)(uintptr_t)parameter;
            uint32_t add;
            memcpy(&add, input + 4, 4);
            /* 64-byte contexts: input control at 0, slot at 64, DCI n at 64*(n+1) */
            for (uint32_t dci = 1; dci < XHCI_MAX_DCI; dci++) {
                if (!(add & (1u << dci))) continue;
                uint64_t dequeue;
                memcpy(&dequeue, input + 64 * (dci + 1) + 8, 8);
                sim.slot[s].ep_ring[dci] = dequeue & ~0xfULL;
                sim.slot[s].ep_cycle[dci] = (uint32_t)(dequeue & 1);
            }
            if (type == 11) {
                uint8_t context[64];
                memcpy(context, input + 64, 64);
                context[12] = 1;                          /* USB device address */
                context[15] = (uint8_t)(2 << 3);          /* slot state: addressed */
                uint64_t device_context;
                memcpy(&device_context, sim.mmio + op_offset() + 0x30, 8);
                memcpy(&device_context, (void *)(uintptr_t)(device_context + 8 * s), 8);
                if (device_context) memcpy((void *)(uintptr_t)device_context, context, 64);
                sim.slot[s].device_context = device_context;
                sim.slot[s].addressed = 1;
            }
            sim_event(address, 1u << 24, (33u << 10) | (s << 24));
        } else {                                          /* No Op and anything else */
            sim_event(address, 1u << 24, (33u << 10));
        }
    }
}

static void process_transfer_ring(uint32_t slot_index, uint32_t dci)
{
    struct sim_slot *slot = &sim.slot[slot_index];
    if (slot_index >= SIM_SLOTS || !slot->used || !slot->ep_ring[dci]) return;

    uint64_t setup = 0;
    uint8_t *data_buffer = NULL;
    uint32_t data_length = 0, data_in = 0;

    for (;;) {
        uint64_t parameter;
        uint32_t status, control;
        read_trb(slot->ep_ring[dci], &parameter, &status, &control);
        if ((control & 1u) != slot->ep_cycle[dci]) return;
        uint32_t type = (control >> 10) & 0x3f;
        uint64_t address = slot->ep_ring[dci];

        if (type == 6) {
            slot->ep_ring[dci] = parameter;
            if (control & (1u << 1)) slot->ep_cycle[dci] ^= 1;
            continue;
        }
        slot->ep_ring[dci] += XHCI_TRB_SIZE;
        sim.transfers++;

        if (type == 2) {                                  /* Setup */
            setup = parameter;
            data_buffer = NULL;
            data_length = 0;
        } else if (type == 3) {                           /* Data */
            data_buffer = (uint8_t *)(uintptr_t)parameter;
            data_length = status & 0x1ffff;
            data_in = (control >> 16) & 1;
        } else if (type == 4) {                           /* Status: run the request */
            uint32_t request_type = setup & 0xff;
            uint32_t request = (setup >> 8) & 0xff;
            uint32_t value = (setup >> 16) & 0xffff;
            uint32_t index = (setup >> 32) & 0xffff;
            int moved = device_control(request_type, request, value, index,
                                       data_buffer, data_length);
            if (moved < 0) {
                sim.control_stalls++;
                sim_event(address, 6u << 24, (32u << 10) | (slot_index << 24) | (dci << 16));
            } else {
                uint32_t residual = data_in && (uint32_t)moved < data_length
                                        ? data_length - (uint32_t)moved : 0;
                sim_event(address, (1u << 24) | residual,
                          (32u << 10) | (slot_index << 24) | (dci << 16));
            }
        } else if (type == 1) {                           /* Normal: bulk */
            uint8_t *buffer = (uint8_t *)(uintptr_t)parameter;
            uint32_t length = status & 0x1ffff;
            if (dci & 1) {                                /* IN */
                if (!sim.pending_ntb_length) {
                    /* Nothing to deliver: leave the TRB outstanding, which is
                     * exactly what the hardware does, and remember it so that
                     * queueing data later completes it. */
                    slot->ep_ring[dci] = address;
                    sim.waiting_slot = slot_index;
                    sim.waiting_dci = dci;
                    return;
                }
                uint32_t n = length < sim.pending_ntb_length ? length : sim.pending_ntb_length;
                memcpy(buffer, sim.pending_ntb, n);
                sim.pending_ntb_length = 0;
                sim_event(address, (1u << 24) | (length - n),
                          (32u << 10) | (slot_index << 24) | (dci << 16));
            } else {                                      /* OUT */
                uint32_t n = length < sizeof(sim.last_out) ? length : sizeof(sim.last_out);
                if (n) memcpy(sim.last_out, buffer, n);
                sim.last_out_length = n;
                sim_event(address, 1u << 24,
                          (32u << 10) | (slot_index << 24) | (dci << 16));
            }
        }
    }
}

/* ---- platform hooks -------------------------------------------------- */

uint32_t xhci_rd32(uintptr_t address)
{
    return mmio_r32((uint32_t)(address - sim.base));
}

uint64_t xhci_rd64(uintptr_t address)
{
    return mmio_r64((uint32_t)(address - sim.base));
}

void xhci_wr32(uintptr_t address, uint32_t value)
{
    uint32_t offset = (uint32_t)(address - sim.base);

    /* PORTSC has to be modelled before the store, not after: its change bits
     * are write-1-to-clear and PED is write-1-to-*disable*, so a driver leaves
     * zeroes in all of them and the old value is what survives. Storing the
     * written value first would silently disable the port. */
    if (offset >= op_offset() + 0x400 && offset < op_offset() + 0x400 + 0x10 * SIM_PORTS) {
        uint32_t previous = mmio_r32(offset);
        uint32_t port = (offset - op_offset() - 0x400) / 0x10 + 1;
        if (value & (1u << 4)) {                      /* PR: reset completes at once */
            if (sim.reset_stuck) {
                mmio_w32(offset, previous | (1u << 4));
                return;
            }
            mmio_w32(offset, 0xe03 | (1u << 21));     /* enabled, U0, high speed, PRC */
            sim_event((uint64_t)port << 24, 1u << 24, 34u << 10);
        } else {
            uint32_t current = previous & ~(value & 0x00fe0000u);
            if (value & (1u << 1)) current &= ~(1u << 1);
            mmio_w32(offset, current);
        }
        return;
    }

    mmio_w32(offset, value);

    if (offset == op_offset() + 0x00) {                   /* USBCMD */
        if (value & 2u) {                                 /* HCRST */
            uint8_t saved[CAPLENGTH];
            memcpy(saved, sim.mmio, CAPLENGTH);
            memset(sim.mmio, 0, sizeof(sim.mmio));
            memcpy(sim.mmio, saved, CAPLENGTH);
            memset(sim.slot, 0, sizeof(sim.slot));
            sim.running = 0;
            sim.configuration = 0;
            mmio_w32(op_offset() + 0x04, 1);              /* USBSTS: halted */
            mmio_w32(op_offset() + 0x08, 1);              /* PAGESIZE */
            for (uint32_t n = 1; n <= SIM_PORTS; n++)
                mmio_w32(op_offset() + 0x400 + 0x10 * (n - 1), 0x2a0);
            /* Port 1 has a high-speed device attached. */
            mmio_w32(op_offset() + 0x400, 0x2a1 | (1u << 17));
        }
        if (value & 1u) {                                 /* Run/Stop */
            sim.running = 1;
            mmio_w32(op_offset() + 0x04, 0);
            sim.cmd_ring = mmio_r64(op_offset() + 0x18) & ~0xfULL;
            sim.cmd_cycle = (uint32_t)(mmio_r64(op_offset() + 0x18) & 1);
        } else if (!(value & 2u)) {
            sim.running = 0;
            mmio_w32(op_offset() + 0x04, 1);
        }
    } else if (offset == RTSOFF + 0x20 + 0x10) {          /* ERSTBA low word */
        uint64_t erst = mmio_r64(RTSOFF + 0x20 + 0x10);
        if (erst) {
            memcpy(&sim.event_ring, (void *)(uintptr_t)erst, 8);
            memcpy(&sim.event_size, (void *)(uintptr_t)(erst + 8), 4);
            sim.event_index = 0;
            sim.event_cycle = 1;
        }
    } else if (offset >= DBOFF && offset < DBOFF + 0x100) {
        uint32_t slot_index = (offset - DBOFF) / 4;
        if (!sim.running) return;
        if (slot_index == 0) process_command_ring();
        else process_transfer_ring(slot_index, value & 0xff);
    }
}

void xhci_wr64(uintptr_t address, uint64_t value)
{
    uint32_t offset = (uint32_t)(address - sim.base);
    memcpy(sim.mmio + offset, &value, 8);
    if (offset == RTSOFF + 0x20 + 0x10) xhci_wr32(address, (uint32_t)value);
}

void xhci_delay_us(uint64_t microseconds) { sim.now_us += microseconds ? microseconds : 1; }
uint64_t xhci_now_us(void) { return sim.now_us += 1; }

/* ---- the tests -------------------------------------------------------- */

static void sim_reset(void)
{
    memset(&sim, 0, sizeof(sim));
    sim.base = (uintptr_t)sim.mmio;
    mmio_w32(0x00, (0x0120u << 16) | CAPLENGTH);
    mmio_w32(0x04, (SIM_PORTS << 24) | (1u << 8) | (SIM_SLOTS - 1));   /* HCSPARAMS1 */
    mmio_w32(0x08, (2u << 27));                                        /* 2 scratchpads */
    mmio_w32(0x10, 0x0000000du);                                       /* AC64, CSZ */
    mmio_w32(0x14, DBOFF);
    mmio_w32(0x18, RTSOFF);
    mmio_w32(op_offset() + 0x04, 1);
    mmio_w32(op_offset() + 0x08, 1);
}

static void queue_ntb(const uint8_t *const *frames, const uint32_t *lengths, uint32_t count)
{
    uint8_t *block = sim.pending_ntb;
    uint32_t offset = 12, indices[4], n;
    memset(block, 0, sizeof(sim.pending_ntb));
    for (n = 0; n < count; n++) {
        offset = (offset + 3) & ~3u;
        indices[n] = offset;
        memcpy(block + offset, frames[n], lengths[n]);
        offset += lengths[n];
    }
    uint32_t ndp = (offset + 3) & ~3u;
    uint32_t ndp_length = 8 + 4 * (count + 1);
    memcpy(block, "NCMH", 4);
    block[4] = 12; block[5] = 0;
    block[6] = 1; block[7] = 0;
    uint32_t total = ndp + ndp_length;
    block[8] = (uint8_t)total; block[9] = (uint8_t)(total >> 8);
    block[10] = (uint8_t)ndp; block[11] = (uint8_t)(ndp >> 8);
    memcpy(block + ndp, "NCM0", 4);
    block[ndp + 4] = (uint8_t)ndp_length; block[ndp + 5] = (uint8_t)(ndp_length >> 8);
    for (n = 0; n < count; n++) {
        uint8_t *entry = block + ndp + 8 + n * 4;
        entry[0] = (uint8_t)indices[n]; entry[1] = (uint8_t)(indices[n] >> 8);
        entry[2] = (uint8_t)lengths[n]; entry[3] = (uint8_t)(lengths[n] >> 8);
    }
    sim.pending_ntb_length = total;

    /* Finish an IN transfer the driver already queued, the way the controller
     * would as soon as the device had something to send. */
    if (sim.waiting_dci) {
        uint32_t slot_index = sim.waiting_slot, dci = sim.waiting_dci;
        sim.waiting_slot = sim.waiting_dci = 0;
        process_transfer_ring(slot_index, dci);
    }
}

int main(void)
{
    static uint8_t arena[512 * 1024] __attribute__((aligned(4096)));
    struct xhci x;
    struct ncm n;

    sim_reset();
    check(xhci_init(&x, sim.base, arena, sizeof(arena)) == 0, "xhci_init succeeds");
    check(x.stats.init_step == 4, "init reached the running step");
    check(x.max_ports == SIM_PORTS, "port count read from HCSPARAMS1");
    check(x.context_size == 64, "64-byte contexts from HCCPARAMS1.CSZ");
    check(x.scratchpads == 2, "scratchpad count from HCSPARAMS2");
    check(sim.running, "controller was started");

    /* Function A. */
    int opened = ncm_open(&n, &x, 0, 0);
    if (opened) printf("  ncm_open returned %d at init_step %u\n", opened, n.stats.init_step);
    check(opened == 0, "ncm_open finds the first function");
    check(n.control_interface == 0 && n.data_interface == 1, "function A interfaces");
    check(n.dci_out == 2 && n.dci_in == 3, "function A endpoint DCIs");
    check(n.packet_in == 512 && n.packet_out == 512, "bulk max packet parsed");
    check(n.vendor == 0x05ac && n.product == 0x1905, "device ids parsed");
    check(memcmp(n.mac, "\x12\x77\x60\xeb\x84\xa7", 6) == 0, "MAC parsed from the string");
    check(sim.configuration == 1, "SET_CONFIGURATION reached the device");
    check(sim.alternate[1] == 1, "SET_INTERFACE selected alternate 1");
    check(n.ntb_in_max <= 32764 && n.out_alignment == 4, "NTB parameters applied");
    check(sim.ntb_input_size == n.ntb_in_max,
          "SET_NTB_INPUT_SIZE told the device the buffer size we actually post");

    /* Receive: one block carrying two datagrams. */
    const uint8_t first[] = {0x33, 0x33, 0, 0, 0, 0xfb, 0x12, 0x77, 0x60, 0xeb, 0x84, 0x58,
                             0x86, 0xdd, 0x60, 0x00, 0x00, 0x00};
    const uint8_t second[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x12, 0x77, 0x60, 0xeb,
                              0x84, 0x58, 0x08, 0x00, 0x45, 0x00};
    const uint8_t *frames[2] = {first, second};
    const uint32_t lengths[2] = {sizeof(first), sizeof(second)};
    queue_ntb(frames, lengths, 2);

    uint8_t got[NCM_MAX_FRAME];
    int length = ncm_receive(&n, got, sizeof(got));
    check(length == (int)sizeof(first) && memcmp(got, first, sizeof(first)) == 0,
          "first datagram of the block");
    length = ncm_receive(&n, got, sizeof(got));
    check(length == (int)sizeof(second) && memcmp(got, second, sizeof(second)) == 0,
          "second datagram of the same block");
    check(ncm_receive(&n, got, sizeof(got)) == 0, "no third datagram");
    check(n.stats.blocks_in == 1 && n.stats.frames_in == 2, "receive counters");

    /* Transmit: the block the device sees has to be a well-formed NTB16. */
    const uint8_t outgoing[64] = {0x12, 0x77, 0x60, 0xeb, 0x84, 0x58, 0x12, 0x77,
                                  0x60, 0xeb, 0x84, 0xa7, 0x86, 0xdd};
    check(ncm_send(&n, outgoing, sizeof(outgoing)) == (int)sizeof(outgoing), "ncm_send accepts");
    check(memcmp(sim.last_out, "NCMH", 4) == 0, "transmitted block has the NTH16 signature");
    {
        const uint8_t *block = sim.last_out;
        uint32_t total = block[8] | (block[9] << 8);
        uint32_t ndp = block[10] | (block[11] << 8);
        uint32_t index = block[ndp + 8] | (block[ndp + 9] << 8);
        uint32_t size = block[ndp + 10] | (block[ndp + 11] << 8);
        check(total == sim.last_out_length, "wBlockLength matches the transfer");
        check(memcmp(block + ndp, "NCM0", 4) == 0, "NDP16 signature");
        check((ndp & 3) == 0 && (index & 3) == 0, "NDP and datagram are 4-byte aligned");
        check(size == sizeof(outgoing) && memcmp(block + index, outgoing, size) == 0,
              "datagram survives the round trip");
        check(block[ndp + 12] == 0 && block[ndp + 13] == 0, "NDP has its terminating entry");
    }

    /* Ring wrap: push more transfers than a ring holds, through the Link TRB. */
    uint64_t before = sim.transfers;
    for (uint32_t k = 0; k < XHCI_EP_TRBS * 3; k++) {
        if (ncm_send(&n, outgoing, sizeof(outgoing)) != (int)sizeof(outgoing)) {
            check(0, "ncm_send survives ring wrap");
            break;
        }
    }
    check(sim.transfers > before + XHCI_EP_TRBS, "transfers continued past the ring size");
    check(n.stats.send_failures == 0, "no send failures across the wrap");

    /* Receiving keeps working after the wrap, which means the cycle bit tracked. */
    queue_ntb(frames, lengths, 1);
    length = ncm_receive(&n, got, sizeof(got));
    check(length == (int)sizeof(first), "receive still works after the wrap");

    /* Oversize frames are refused rather than corrupting the block. */
    check(ncm_send(&n, outgoing, NCM_MAX_FRAME + 1) < 0, "oversize frame refused");

    ncm_close(&n);
    check(n.x == NULL, "ncm_close releases the device");

    /* Function B, on a fresh controller. */
    sim_reset();
    check(xhci_init(&x, sim.base, arena, sizeof(arena)) == 0, "re-init after reset");
    check(ncm_open(&n, &x, 1, 0) == 0, "ncm_open finds the second function");
    check(n.control_interface == 2 && n.data_interface == 3, "function B interfaces");
    check(n.dci_out == 4 && n.dci_in == 5, "function B endpoint DCIs");
    check(memcmp(n.mac, "\x12\x77\x60\xeb\x84\x87", 6) == 0, "function B MAC");
    ncm_close(&n);

    /* A controller with nothing attached must fail cleanly, not hang. */
    sim_reset();
    check(xhci_init(&x, sim.base, arena, sizeof(arena)) == 0, "init with no device");
    mmio_w32(op_offset() + 0x400, 0x2a0);          /* clear the connect */
    check(xhci_wait_port(&x, 5) == 0, "wait_port reports nothing attached");
    mmio_w32(op_offset() + 0x400, 0xe03 | (1u << 21));
    sim.reset_stuck = 1;
    check(xhci_wait_port(&x, 5) == 0, "stale PRC/PED cannot make an unfinished reset succeed");
    check(!(mmio_r32(op_offset() + 0x400) & (1u << 21)), "old reset completion was acknowledged");
    sim.reset_stuck = 0;
    check(xhci_wait_port(&x, 5) == 1, "fresh reset recovers the same connected port");

    if (failures) {
        printf("FAILED: %d of %d checks\n", failures, checks + failures);
        return 1;
    }
    printf("PASS: %d q1n1 xHCI and NCM checks (native harness, sanitizers on)\n", checks);
    return 0;
}
