/* SPDX-License-Identifier: MIT */
/* Qualcomm DWC3 CDC ACM takeover after ExitBootServices. See qcom-dwc3.h.
 * EP0 state machine, descriptors, and CDC handling follow m1n1 src/usb_dwc3.c.
 * Events and setup packets are decoded with explicit shifts because this file
 * is also built for the Microsoft ABI, whose bitfield layout differs. */
#include "qcom-dwc3.h"

/* Register layout values match m1n1 src/usb_dwc3_regs.h and Linux dwc3/core.h. */
#define DWC3_GSBUSCFG0 0xc100
#define DWC3_GCTL 0xc110
#define DWC3_GCTL_CORESOFTRESET (1u << 11)
#define DWC3_GCTL_PRTCAPDIR(n) ((uint32_t)(n) << 12)
#define DWC3_GCTL_PRTCAP_DEVICE 2
#define DWC3_GSNPSID 0xc120
#define DWC3_GUCTL 0xc12c
#define DWC3_GUSB2PHYCFG(n) (0xc200 + (n) * 4)
#define DWC3_GUSB2PHYCFG_PHYSOFTRST (1u << 31)
#define DWC3_GUSB3PIPECTL(n) (0xc2c0 + (n) * 4)
#define DWC3_GUSB3PIPECTL_PHYSOFTRST (1u << 31)
#define DWC3_GEVNTADRLO(n) (0xc400 + (n) * 16)
#define DWC3_GEVNTADRHI(n) (0xc404 + (n) * 16)
#define DWC3_GEVNTSIZ(n) (0xc408 + (n) * 16)
#define DWC3_GEVNTSIZ_INTMASK (1u << 31)
#define DWC3_GEVNTCOUNT(n) (0xc40c + (n) * 16)
#define DWC3_GEVNTCOUNT_MASK 0xfffc
#define DWC3_DCFG 0xc700
#define DWC3_DCFG_DEVADDR(a) ((uint32_t)(a) << 3)
#define DWC3_DCFG_DEVADDR_MASK DWC3_DCFG_DEVADDR(0x7f)
#define DWC3_DCFG_SPEED_MASK 7u
#define DWC3_DCFG_HIGHSPEED 0u
#define DWC3_DCTL 0xc704
#define DWC3_DCTL_RUN_STOP (1u << 31)
#define DWC3_DCTL_CSFTRST (1u << 30)
#define DWC3_DEVTEN_DISCONNEVTEN (1u << 0)
#define DWC3_DEVTEN_USBRSTEN (1u << 1)
#define DWC3_DEVTEN_CONNECTDONEEN (1u << 2)
#define DWC3_DSTS 0xc70c
#define DWC3_DSTS_DEVCTRLHLT (1u << 22)
#define DWC3_DSTS_CONNECTSPD 7u
#define DWC3_DALEPENA 0xc720
#define DWC3_DALEPENA_EP(n) (1u << (n))
#define DWC3_DEPCMDPAR2(n) (0xc800 + (n) * 16)
#define DWC3_DEPCMDPAR1(n) (0xc804 + (n) * 16)
#define DWC3_DEPCMDPAR0(n) (0xc808 + (n) * 16)
#define DWC3_DEPCMD(n) (0xc80c + (n) * 16)
#define DWC3_DEPCMD_PARAM(x) ((uint32_t)(x) << 16)
#define DWC3_DEPCMD_STATUS(v) (((v) >> 15) & 1)
#define DWC3_DEPCMD_HIPRI_FORCERM (1u << 11)
#define DWC3_DEPCMD_CMDACT (1u << 10)
#define DWC3_DEPCMD_SETEPCONFIG 0x01
#define DWC3_DEPCMD_SETTRANSFRESOURCE 0x02
#define DWC3_DEPCMD_SETSTALL 0x04
#define DWC3_DEPCMD_CLEARSTALL 0x05
#define DWC3_DEPCMD_STARTTRANSFER 0x06
#define DWC3_DEPCMD_ENDTRANSFER 0x08
#define DWC3_DEPCMD_DEPSTARTCFG 0x09
#define DWC3_DEPCMD_TYPE_CONTROL 0
#define DWC3_DEPCMD_TYPE_BULK 2
#define DWC3_DEPCMD_TYPE_INTR 3
#define DWC3_DEPCFG_EP_TYPE(n) (((uint32_t)(n) & 3) << 1)
#define DWC3_DEPCFG_MAX_PACKET_SIZE(n) (((uint32_t)(n) & 0x7ff) << 3)
#define DWC3_DEPCFG_FIFO_NUMBER(n) (((uint32_t)(n) & 0xf) << 17)
#define DWC3_DEPCFG_XFER_COMPLETE_EN (1u << 8)
#define DWC3_DEPCFG_XFER_NOT_READY_EN (1u << 10)
#define DWC3_DEPCFG_EP_NUMBER(n) (((uint32_t)(n) & 0x1f) << 25)
#define DWC3_TRB_SIZE_MASK 0x00ffffffu
#define DWC3_TRB_SIZE_LENGTH(n) ((n) & DWC3_TRB_SIZE_MASK)
#define DWC3_TRB_CTRL_HWO (1u << 0)
#define DWC3_TRB_CTRL_LST (1u << 1)
#define DWC3_TRB_CTRL_ISP_IMI (1u << 10)
#define DWC3_TRBCTL(n) ((uint32_t)(n) << 4)
#define DWC3_TRBCTL_NORMAL DWC3_TRBCTL(1)
#define DWC3_TRBCTL_CONTROL_SETUP DWC3_TRBCTL(2)
#define DWC3_TRBCTL_CONTROL_STATUS2 DWC3_TRBCTL(3)
#define DWC3_TRBCTL_CONTROL_STATUS3 DWC3_TRBCTL(4)
#define DWC3_TRBCTL_CONTROL_DATA DWC3_TRBCTL(5)
#define DWC3_DEPEVT_XFERCOMPLETE 1
#define DWC3_DEPEVT_XFERNOTREADY 3
#define DWC3_DEVT_DISCONN 0
#define DWC3_DEVT_USBRST 1
#define DWC3_DEVT_CONNECTDONE 2

#define QSCRATCH_HS_PHY_CTRL 0x10
#define UTMI_OTG_VBUS_VALID (1u << 20)
#define SW_SESSVLD_SEL (1u << 28)
#define QSCRATCH_SS_PHY_CTRL 0x30
#define LANE0_PWR_PRESENT (1u << 24)

#define DWC3_GSBUSCFG1 0xc104
#define DWC3_GUCTL1 0xc11c
#define DWC3_GUCTL2 0xc19c
#define DWC3_GFLADJ 0xc630
#define DWC3_DEVTEN 0xc708

/* Logical endpoints: 0/1 control, 3 = 0x81 INT, 4/5 = 0x02/0x82 bulk,
 * 7 = 0x83 INT, 8/9 = 0x04/0x84 bulk (same numbering as m1n1). */
#define EP0_OUT 0
#define EP0_IN 1
static const uint8_t pipe_int[QDWC3_PIPES] = {3, 7};
static const uint8_t pipe_out[QDWC3_PIPES] = {4, 8};
static const uint8_t pipe_in[QDWC3_PIPES] = {5, 9};

#define EVENT_BYTES 4096u
#define TRB_BYTES 4096u
#define XFER_BYTES 16384u
#define RING_BYTES 65536u
#define HS_PACKET 512u

enum {
    EP0_IDLE, EP0_SETUP, EP0_DATA_SEND, EP0_DATA_RECV, EP0_DATA_SEND_DONE, EP0_DATA_RECV_DONE,
    EP0_RECV_STATUS, EP0_RECV_STATUS_DONE, EP0_SEND_STATUS, EP0_SEND_STATUS_DONE
};

static uint32_t rd(struct qdwc3 *d, uint32_t off) { return qdwc3_rd(d->regs + off); }
static void wr(struct qdwc3 *d, uint32_t off, uint32_t v) { qdwc3_wr(d->regs + off, v); }
static void mask(struct qdwc3 *d, uint32_t off, uint32_t clear, uint32_t set)
{ wr(d, off, (rd(d, off) & ~clear) | set); }
static int poll_bits(struct qdwc3 *d, uint32_t off, uint32_t m, uint32_t want, uint32_t ms)
{
    for (uint32_t n = 0; n <= ms * 10; n++) {
        if ((rd(d, off) & m) == want) return 0;
        qdwc3_delay_us(100);
    }
    return -1;
}
static void bytes_copy(uint8_t *dst, const uint8_t *src, size_t n) { while (n--) *dst++ = *src++; }
static void bytes_fill(uint8_t *dst, uint8_t value, size_t n) { while (n--) *dst++ = value; }
static uint32_t get32le(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static void put32le(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }

static size_t ring_used(const struct qdwc3_ring *r) { return r->head - r->tail; }
static size_t ring_write(struct qdwc3_ring *r, const uint8_t *p, size_t n)
{
    size_t done = 0;
    while (done < n && ring_used(r) < r->size) { r->data[r->head++ & (r->size - 1)] = p[done++]; }
    return done;
}
static size_t ring_peek(const struct qdwc3_ring *r, uint8_t *p, size_t n)
{
    size_t done = 0, used = ring_used(r);
    while (done < n && done < used) { p[done] = r->data[(r->tail + done) & (r->size - 1)]; done++; }
    return done;
}
static size_t ring_read(struct qdwc3_ring *r, uint8_t *p, size_t n)
{
    size_t done = ring_peek(r, p, n);
    r->tail += (uint32_t)done;
    return done;
}

static const uint8_t device_descriptor[18] = {
    18, 1, 0x00, 0x02, 2, 0, 0, 64, 0x09, 0x12, 0x6d, 0x31, 0x00, 0x01, 1, 2, 3, 1};
static const uint8_t qualifier_descriptor[10] = {10, 6, 0x00, 0x02, 2, 0, 0, 64, 0, 0};
/* Two m1n1-style ACM functions: interfaces 0/1 (EP 0x81, 0x02/0x82), 2/3 (0x83, 0x04/0x84). */
static const uint8_t config_descriptor[] = {
    9, 2, 97, 0, 4, 1, 0, 0xc0, 250,
    9, 4, 0, 0, 1, 2, 2, 0, 0,  5, 0x24, 6, 0, 1,  7, 5, 0x81, 3, 64, 0, 10,
    9, 4, 1, 0, 2, 0x0a, 0, 0, 0,  7, 5, 0x02, 2, 0x00, 0x02, 10,  7, 5, 0x82, 2, 0x00, 0x02, 10,
    9, 4, 2, 0, 1, 2, 2, 0, 0,  5, 0x24, 6, 2, 3,  7, 5, 0x83, 3, 64, 0, 10,
    9, 4, 3, 0, 2, 0x0a, 0, 0, 0,  7, 5, 0x04, 2, 0x00, 0x02, 10,  7, 5, 0x84, 2, 0x00, 0x02, 10,
};
_Static_assert(sizeof(config_descriptor) == 97, "CDC configuration length");
static const uint8_t languages[4] = {4, 3, 0x09, 0x04};
static const char *const strings[] = {"q1n1", "q1n1 A16 EL2 serial", "A16-Q1N1-EL2"};
static uint8_t string_buffer[64];

static int ep_command(struct qdwc3 *d, uint8_t ep, uint32_t cmd, uint32_t p0, uint32_t p1, uint32_t p2)
{
    wr(d, DWC3_DEPCMDPAR0(ep), p0);
    wr(d, DWC3_DEPCMDPAR1(ep), p1);
    wr(d, DWC3_DEPCMDPAR2(ep), p2);
    wr(d, DWC3_DEPCMD(ep), cmd | DWC3_DEPCMD_CMDACT);
    int status = poll_bits(d, DWC3_DEPCMD(ep), DWC3_DEPCMD_CMDACT, 0, 500) ? -1 :
                 (int)DWC3_DEPCMD_STATUS(rd(d, DWC3_DEPCMD(ep)));
    if (status) { d->stats.command_failures++; d->stats.last_command_status = (uint32_t)status | ep << 8 | cmd << 16; }
    return status;
}
static int ep_configure(struct qdwc3 *d, uint8_t ep, uint8_t type, uint32_t packet)
{
    uint32_t p0 = DWC3_DEPCFG_EP_TYPE(type) | DWC3_DEPCFG_MAX_PACKET_SIZE(packet);
    if (type != DWC3_DEPCMD_TYPE_CONTROL) p0 |= DWC3_DEPCFG_FIFO_NUMBER(ep);
    uint32_t p1 = DWC3_DEPCFG_XFER_COMPLETE_EN | DWC3_DEPCFG_XFER_NOT_READY_EN | DWC3_DEPCFG_EP_NUMBER(ep);
    if (ep_command(d, ep, DWC3_DEPCMD_SETEPCONFIG, p0, p1, 0)) return -1;
    return ep_command(d, ep, DWC3_DEPCMD_SETTRANSFRESOURCE, 1, 0, 0) ? -1 : 0;
}
static void set_stall(struct qdwc3 *d, uint8_t ep, int stall)
{
    if (stall) d->stats.stalls++;
    ep_command(d, ep, stall ? DWC3_DEPCMD_SETSTALL : DWC3_DEPCMD_CLEARSTALL, 0, 0, 0);
}
static int start_setup(struct qdwc3 *d);
/* As Linux dwc3_ep0_stall_and_restart: stall, then re-arm the next SETUP. */
static void stall_ep0(struct qdwc3 *d)
{
    set_stall(d, EP0_OUT, 1);
    d->ep0_state = EP0_IDLE;
    if (!d->ep[EP0_OUT].in_progress) start_setup(d);
}

/* Identity DMA: the TRB and buffer addresses are their physical addresses. */
static int start_trb(struct qdwc3 *d, uint8_t ep, uint32_t trbctl, uint32_t length)
{
    if (d->ep[ep].in_progress) return -1;
    uint8_t *trb = d->ep[ep].trb;
    uint64_t buffer = (uintptr_t)d->ep[ep].buffer, address = (uintptr_t)trb;
    put32le(trb, (uint32_t)buffer);
    put32le(trb + 4, (uint32_t)(buffer >> 32));
    put32le(trb + 8, DWC3_TRB_SIZE_LENGTH(length));
    put32le(trb + 12, DWC3_TRB_CTRL_HWO | DWC3_TRB_CTRL_ISP_IMI | DWC3_TRB_CTRL_LST | trbctl);
    if (ep_command(d, ep, DWC3_DEPCMD_STARTTRANSFER, (uint32_t)(address >> 32), (uint32_t)address, 0)) return -1;
    d->ep[ep].in_progress = 1;
    return 0;
}
static int start_setup(struct qdwc3 *d)
{
    d->ep0_state = EP0_SETUP;
    return start_trb(d, EP0_OUT, DWC3_TRBCTL_CONTROL_SETUP, 8);
}
/* As Linux dwc3_ep0_do_control_status: STATUS3 after a data stage, else STATUS2. */
static int start_status(struct qdwc3 *d, uint8_t ep)
{ return start_trb(d, ep, d->ep0_three_stage ? DWC3_TRBCTL_CONTROL_STATUS3 : DWC3_TRBCTL_CONTROL_STATUS2, 0); }

static int get_descriptor(struct qdwc3 *d, uint16_t value, uint16_t length)
{
    const uint8_t *p = NULL;
    uint32_t n = 0;
    uint8_t type = value >> 8, index = value & 0xff;
    if (type == 1) { p = device_descriptor; n = sizeof(device_descriptor); }
    else if (type == 2) { p = config_descriptor; n = sizeof(config_descriptor); }
    else if (type == 6) { p = qualifier_descriptor; n = sizeof(qualifier_descriptor); }
    else if (type == 3 && index == 0) { p = languages; n = sizeof(languages); }
    else if (type == 3 && index <= 3) {
        const char *s = strings[index - 1];
        n = 2;
        while (*s && n + 2 <= sizeof(string_buffer)) { string_buffer[n++] = (uint8_t)*s++; string_buffer[n++] = 0; }
        string_buffer[0] = (uint8_t)n; string_buffer[1] = 3;
        p = string_buffer;
    }
    if (!p) return -1;
    d->ep0_buffer = p;
    d->ep0_length = n < length ? n : length;
    return 0;
}
static void set_configuration(struct qdwc3 *d, uint16_t value)
{
    uint32_t bits = 0;
    for (unsigned i = 0; i < QDWC3_PIPES; i++)
        bits |= DWC3_DALEPENA_EP(pipe_int[i]) | DWC3_DALEPENA_EP(pipe_in[i]) | DWC3_DALEPENA_EP(pipe_out[i]);
    if (value == 0) {
        mask(d, DWC3_DALEPENA, bits, 0);
        for (unsigned i = 0; i < QDWC3_PIPES; i++) d->pipe[i].ready = 0;
        d->stats.configured = 0;
    } else {
        mask(d, DWC3_DALEPENA, 0, bits);
        d->stats.configured = 1;
    }
    d->ep0_state = EP0_SEND_STATUS;
}
static void handle_setup(struct qdwc3 *d)
{
    const uint8_t *s = d->ep[EP0_OUT].buffer;
    uint8_t type = s[0], request = s[1];
    uint16_t value = s[2] | s[3] << 8, index = s[4] | s[5] << 8, length = s[6] | s[7] << 8;
    d->stats.setups++;
    d->ep0_three_stage = length != 0;
    d->stats.last_setup = (uint32_t)type << 24 | (uint32_t)request << 16 | value;
    if ((type & 0x60) == 0) {
        uint8_t recipient = type & 0x1f;
        if (recipient == 0 && request == 5) {           /* SET_ADDRESS */
            mask(d, DWC3_DCFG, DWC3_DCFG_DEVADDR_MASK, DWC3_DCFG_DEVADDR(value & 0x7f));
            d->stats.address = value & 0x7f;
            d->ep0_state = EP0_SEND_STATUS;
        } else if (recipient == 0 && request == 9 && value <= 1) {
            set_configuration(d, value);
        } else if (recipient == 0 && request == 6) {
            if (get_descriptor(d, value, length)) stall_ep0(d);
            else d->ep0_state = EP0_DATA_SEND;
        } else if (request == 0 && (type & 0x80)) {     /* GET_STATUS: self-powered device */
            d->status_word = recipient == 0 ? 1 : 0;
            d->ep0_buffer = (const uint8_t *)&d->status_word;
            d->ep0_length = length < 2 ? length : 2;
            d->ep0_state = EP0_DATA_SEND;
        } else if (recipient == 2 && request == 1 && value == 0) { /* CLEAR_FEATURE(HALT) */
            uint8_t ep = (uint8_t)(((index & 0x7f) << 1) | (index >> 7 & 1));
            if (ep < 10) set_stall(d, ep, 0);
            d->ep0_state = EP0_SEND_STATUS;
        } else if (recipient == 1 && request == 11) {    /* SET_INTERFACE alt 0 */
            if (value) stall_ep0(d); else d->ep0_state = EP0_SEND_STATUS;
        } else {
            stall_ep0(d);
        }
        return;
    }
    if ((type & 0x60) == 0x20) {
        unsigned pipe = (index & 0xff) / 2;
        if (pipe >= QDWC3_PIPES) { stall_ep0(d); return; }
        if (request == 0x21) {                          /* GET_LINE_CODING */
            d->ep0_buffer = d->pipe[pipe].line_coding;
            d->ep0_length = length < 7 ? length : 7;
            d->ep0_state = EP0_DATA_SEND;
        } else if (request == 0x22) {                   /* SET_CONTROL_LINE_STATE */
            d->pipe[pipe].ready = value & 1;
            d->ep0_state = EP0_SEND_STATUS;
        } else if (request == 0x20) {                   /* SET_LINE_CODING */
            d->ep0_read_buffer = d->pipe[pipe].line_coding;
            d->ep0_read_length = length < 7 ? length : 7;
            d->ep0_state = EP0_DATA_RECV;
        } else {
            stall_ep0(d);
        }
        return;
    }
    stall_ep0(d);
}
static void ep0_xfer_done(struct qdwc3 *d, uint8_t ep)
{
    switch (d->ep0_state) {
    case EP0_SETUP: handle_setup(d); break;
    case EP0_RECV_STATUS_DONE: case EP0_SEND_STATUS_DONE: start_setup(d); break;
    case EP0_DATA_SEND_DONE: d->ep0_state = EP0_RECV_STATUS; break;
    case EP0_DATA_RECV_DONE:
        bytes_copy(d->ep0_read_buffer, d->ep[ep].buffer, d->ep0_read_length);
        d->ep0_state = EP0_SEND_STATUS;
        break;
    default: stall_ep0(d); break;
    }
}
static void ep0_not_ready(struct qdwc3 *d)
{
    switch (d->ep0_state) {
    case EP0_IDLE: start_setup(d); break;
    case EP0_DATA_SEND:
        bytes_fill(d->ep[EP0_IN].buffer, 0, 64);
        bytes_copy(d->ep[EP0_IN].buffer, d->ep0_buffer, d->ep0_length);
        start_trb(d, EP0_IN, DWC3_TRBCTL_CONTROL_DATA, d->ep0_length);
        d->ep0_state = EP0_DATA_SEND_DONE;
        break;
    case EP0_DATA_RECV:
        bytes_fill(d->ep[EP0_OUT].buffer, 0, 64);
        start_trb(d, EP0_OUT, DWC3_TRBCTL_CONTROL_DATA, 64);
        d->ep0_state = EP0_DATA_RECV_DONE;
        break;
    case EP0_RECV_STATUS: start_status(d, EP0_OUT); d->ep0_state = EP0_RECV_STATUS_DONE; break;
    case EP0_SEND_STATUS: start_status(d, EP0_IN); d->ep0_state = EP0_SEND_STATUS_DONE; break;
    default: stall_ep0(d); break;
    }
}
static int pipe_for_ep(uint8_t ep, int in)
{
    for (unsigned i = 0; i < QDWC3_PIPES; i++) if ((in ? pipe_in[i] : pipe_out[i]) == ep) return (int)i;
    return -1;
}
/* One HS packet per OUT transfer: host writes of exact packet multiples must
 * complete even when the host sends no zero-length packet. */
static void start_bulk_out(struct qdwc3 *d, unsigned pipe)
{
    uint8_t ep = pipe_out[pipe];
    struct qdwc3_ring *r = &d->pipe[pipe].host2device;
    if (!d->stats.configured || d->ep[ep].in_progress || r->size - ring_used(r) < HS_PACKET) return;
    start_trb(d, ep, DWC3_TRBCTL_NORMAL, HS_PACKET);
}
static void start_bulk_in(struct qdwc3 *d, unsigned pipe)
{
    uint8_t ep = pipe_in[pipe];
    if (!d->stats.configured || d->ep[ep].in_progress) return;
    struct qdwc3_ring *r = &d->pipe[pipe].device2host;
    size_t n = ring_peek(r, d->ep[ep].buffer, XFER_BYTES);
    if (!n && !d->ep[ep].zlp_pending) return;
    if (start_trb(d, ep, DWC3_TRBCTL_NORMAL, (uint32_t)n)) return; /* Bytes stay queued. */
    r->tail += (uint32_t)n;
    d->stats.tx_bytes += n;
    d->ep[ep].zlp_pending = n && !(n % HS_PACKET);
}
static void bulk_out_done(struct qdwc3 *d, uint8_t ep)
{
    int pipe = pipe_for_ep(ep, 0);
    if (pipe < 0) return;
    uint32_t remaining = get32le(d->ep[ep].trb + 8) & DWC3_TRB_SIZE_MASK;
    uint32_t received = remaining <= HS_PACKET ? HS_PACKET - remaining : 0;
    size_t kept = ring_write(&d->pipe[pipe].host2device, d->ep[ep].buffer, received);
    d->stats.rx_bytes += kept;
    d->stats.dropped_rx += received - kept;
}
static void handle_usb_reset(struct qdwc3 *d)
{
    d->stats.resets++;
    for (unsigned ep = 0; ep < 10; ep++) {
        if (ep > 1 && d->ep[ep].in_progress)
            ep_command(d, ep, DWC3_DEPCMD_ENDTRANSFER | DWC3_DEPCMD_HIPRI_FORCERM |
                       DWC3_DEPCMD_PARAM((rd(d, DWC3_DEPCMD(ep)) >> 16) & 0x7f), 0, 0, 0);
        if (ep > 1) { d->ep[ep].in_progress = 0; d->ep[ep].zlp_pending = 0; set_stall(d, ep, 0); }
    }
    d->ep[EP0_OUT].in_progress = d->ep[EP0_IN].in_progress = 0;
    for (unsigned i = 0; i < QDWC3_PIPES; i++) d->pipe[i].ready = 0;
    d->stats.configured = 0;
    d->stats.address = 0;
    mask(d, DWC3_DCFG, DWC3_DCFG_DEVADDR_MASK, 0);
    wr(d, DWC3_DALEPENA, DWC3_DALEPENA_EP(EP0_OUT) | DWC3_DALEPENA_EP(EP0_IN));
    d->ep0_state = EP0_IDLE;
}
static void handle_event(struct qdwc3 *d, uint32_t e)
{
    d->stats.events++;
    if (e & 1) {
        if ((e >> 1 & 0x7f) != 0) return; /* Not a device event (e.g. carkit/I2C). */
        uint32_t type = e >> 8 & 0xf;
        d->stats.device_events++;
        d->stats.last_devt = type;
        if (type == DWC3_DEVT_USBRST) handle_usb_reset(d);
        else if (type == DWC3_DEVT_CONNECTDONE) {
            d->stats.connects++;
            d->stats.speed = rd(d, DWC3_DSTS) & DWC3_DSTS_CONNECTSPD;
            d->ep[EP0_OUT].in_progress = 0;
            start_setup(d);
        } else if (type == DWC3_DEVT_DISCONN) {
            d->stats.disconnects++;
            for (unsigned i = 0; i < QDWC3_PIPES; i++) d->pipe[i].ready = 0;
            d->stats.configured = 0;
        }
        return;
    }
    uint8_t ep = e >> 1 & 0x1f, kind = e >> 6 & 0xf;
    d->stats.ep_events++;
    if (ep >= 10) return;
    if (kind == DWC3_DEPEVT_XFERCOMPLETE) {
        d->ep[ep].in_progress = 0;
        if (ep <= EP0_IN) ep0_xfer_done(d, ep);
        else if (pipe_for_ep(ep, 0) >= 0) bulk_out_done(d, ep);
    } else if (kind == DWC3_DEPEVT_XFERNOTREADY) {
        if (d->ep[ep].in_progress) return; /* m1n1: spurious while active; ignore. */
        if (ep <= EP0_IN) ep0_not_ready(d);
        else if (pipe_for_ep(ep, 1) >= 0) start_bulk_in(d, (unsigned)pipe_for_ep(ep, 1));
        else if (pipe_for_ep(ep, 0) >= 0) start_bulk_out(d, (unsigned)pipe_for_ep(ep, 0));
    }
}

void qdwc3_poll(struct qdwc3 *d)
{
    uint32_t count = rd(d, DWC3_GEVNTCOUNT(0)) & DWC3_GEVNTCOUNT_MASK;
    if (count > EVENT_BYTES) { d->stats.overflow++; count = EVENT_BYTES; }
    __asm__ volatile("dmb oshld" ::: "memory");
    for (uint32_t n = 0; n < count; n += 4) {
        uint32_t e = get32le(d->events + d->event_offset);
        /* The buffer is prefilled with 0xaa; an unwritten slot means DMA did not land. */
        if (e == 0xaaaaaaaa) d->stats.dma_mismatch++;
        else handle_event(d, e);
        d->event_offset = (d->event_offset + 4) % EVENT_BYTES;
    }
    if (count) wr(d, DWC3_GEVNTCOUNT(0), count);
    for (unsigned i = 0; i < QDWC3_PIPES; i++) {
        start_bulk_out(d, i);
        start_bulk_in(d, i);
    }
}
size_t qdwc3_read(struct qdwc3 *d, unsigned pipe, uint8_t *buffer, size_t count)
{
    return pipe < QDWC3_PIPES ? ring_read(&d->pipe[pipe].host2device, buffer, count) : 0;
}
size_t qdwc3_write(struct qdwc3 *d, unsigned pipe, const uint8_t *buffer, size_t count)
{
    if (pipe >= QDWC3_PIPES || !d->pipe[pipe].ready) return 0;
    size_t n = ring_write(&d->pipe[pipe].device2host, buffer, count);
    start_bulk_in(d, pipe);
    return n;
}
int qdwc3_ready(const struct qdwc3 *d, unsigned pipe) { return pipe < QDWC3_PIPES && d->pipe[pipe].ready; }

static void restore(struct qdwc3 *d, uint32_t off, uint32_t value, uint32_t bit)
{
    if (rd(d, off) != value) { wr(d, off, value); d->stats.restored |= bit; }
}
int qdwc3_takeover(struct qdwc3 *d, uintptr_t regs, uintptr_t qscratch,
                   const struct qdwc3_saved *saved, uint8_t *arena, uint64_t arena_size)
{
    bytes_fill((uint8_t *)d, 0, sizeof(*d));
    d->regs = regs; d->qscratch = qscratch; d->saved = saved; d->arena = arena; d->arena_size = arena_size;
    d->stats.init_step = 1;
    if (!arena || arena_size < QDWC3_ARENA_SIZE || ((uintptr_t)arena & 0xfff)) return -1;
    uint32_t id = rd(d, DWC3_GSNPSID);
    if (!id || id != saved->gsnpsid) return -2;

    /* Carve the arena: events, TRBs (16 bytes each), per-endpoint buffers, rings. */
    d->events = arena;
    bytes_fill(d->events, 0xaa, EVENT_BYTES);
    uint8_t *trbs = arena + EVENT_BYTES;
    bytes_fill(trbs, 0, TRB_BYTES);
    uint8_t *cursor = trbs + TRB_BYTES;
    for (unsigned ep = 0; ep < 10; ep++) {
        d->ep[ep].trb = trbs + ep * 16;
        d->ep[ep].buffer = cursor;
        cursor += (ep <= EP0_IN || pipe_for_ep(ep, 0) >= 0 || pipe_for_ep(ep, 1) >= 0) ? XFER_BYTES : 0;
    }
    for (unsigned i = 0; i < QDWC3_PIPES; i++) {
        d->pipe[i].host2device = (struct qdwc3_ring){cursor, RING_BYTES, 0, 0}; cursor += RING_BYTES;
        d->pipe[i].device2host = (struct qdwc3_ring){cursor, RING_BYTES, 0, 0}; cursor += RING_BYTES;
        static const uint8_t line[7] = {0x00, 0xc2, 0x01, 0, 0, 0, 8};
        bytes_copy(d->pipe[i].line_coding, line, 7);
    }
    if ((uint64_t)(cursor - arena) > arena_size) return -3;

    /* Stop whatever firmware left, then restore VBUS override for device mode. */
    d->stats.init_step = 2;
    mask(d, DWC3_DCTL, DWC3_DCTL_RUN_STOP, 0);
    poll_bits(d, DWC3_DSTS, DWC3_DSTS_DEVCTRLHLT, DWC3_DSTS_DEVCTRLHLT, 500);
    qdwc3_wr(qscratch + QSCRATCH_SS_PHY_CTRL, qdwc3_rd(qscratch + QSCRATCH_SS_PHY_CTRL) | LANE0_PWR_PRESENT);
    qdwc3_wr(qscratch + QSCRATCH_HS_PHY_CTRL,
             qdwc3_rd(qscratch + QSCRATCH_HS_PHY_CTRL) | UTMI_OTG_VBUS_VALID | SW_SESSVLD_SEL);

    /* Device soft reset only; firmware owns PHY power, clocks, and PHY tuning. */
    d->stats.init_step = 3;
    mask(d, DWC3_DCTL, 0, DWC3_DCTL_CSFTRST);
    if (poll_bits(d, DWC3_DCTL, DWC3_DCTL_CSFTRST, 0, 1000)) return -4;
    qdwc3_delay_us(50000);

    d->stats.init_step = 4;
    restore(d, DWC3_GSBUSCFG0, saved->gsbuscfg0, 1 << 0);
    restore(d, DWC3_GSBUSCFG1, saved->gsbuscfg1, 1 << 1);
    restore(d, DWC3_GUCTL, saved->guctl, 1 << 2);
    restore(d, DWC3_GUCTL1, saved->guctl1, 1 << 3);
    restore(d, DWC3_GUCTL2, saved->guctl2, 1 << 4);
    restore(d, DWC3_GUSB2PHYCFG(0), saved->gusb2phycfg & ~DWC3_GUSB2PHYCFG_PHYSOFTRST, 1 << 5);
    restore(d, DWC3_GUSB3PIPECTL(0), saved->gusb3pipectl & ~DWC3_GUSB3PIPECTL_PHYSOFTRST, 1 << 6);
    restore(d, DWC3_GFLADJ, saved->gfladj, 1 << 7);
    uint32_t gctl = (saved->gctl & ~(DWC3_GCTL_CORESOFTRESET | DWC3_GCTL_PRTCAPDIR(3))) |
                    DWC3_GCTL_PRTCAPDIR(DWC3_GCTL_PRTCAP_DEVICE);
    restore(d, DWC3_GCTL, gctl, 1 << 8);
    wr(d, DWC3_DCFG, (saved->dcfg & ~(DWC3_DCFG_DEVADDR_MASK | DWC3_DCFG_SPEED_MASK)) | DWC3_DCFG_HIGHSPEED);

    d->stats.init_step = 5;
    uint64_t events = (uintptr_t)d->events;
    wr(d, DWC3_GEVNTADRLO(0), (uint32_t)events);
    wr(d, DWC3_GEVNTADRHI(0), (uint32_t)(events >> 32));
    wr(d, DWC3_GEVNTSIZ(0), DWC3_GEVNTSIZ_INTMASK | EVENT_BYTES); /* Polled; events still queue. */
    uint32_t stale = rd(d, DWC3_GEVNTCOUNT(0)) & DWC3_GEVNTCOUNT_MASK;
    if (stale) wr(d, DWC3_GEVNTCOUNT(0), stale);
    wr(d, DWC3_DEVTEN, DWC3_DEVTEN_DISCONNEVTEN | DWC3_DEVTEN_USBRSTEN | DWC3_DEVTEN_CONNECTDONEEN);

    d->stats.init_step = 6;
    if (ep_command(d, 0, DWC3_DEPCMD_DEPSTARTCFG, 0, 0, 0)) return -6;
    if (ep_configure(d, EP0_OUT, DWC3_DEPCMD_TYPE_CONTROL, 64) ||
        ep_configure(d, EP0_IN, DWC3_DEPCMD_TYPE_CONTROL, 64)) return -7;
    for (unsigned i = 0; i < QDWC3_PIPES; i++)
        if (ep_configure(d, pipe_int[i], DWC3_DEPCMD_TYPE_INTR, 64) ||
            ep_configure(d, pipe_in[i], DWC3_DEPCMD_TYPE_BULK, HS_PACKET) ||
            ep_configure(d, pipe_out[i], DWC3_DEPCMD_TYPE_BULK, HS_PACKET)) return -8;
    wr(d, DWC3_DALEPENA, DWC3_DALEPENA_EP(EP0_OUT) | DWC3_DALEPENA_EP(EP0_IN));

    d->stats.init_step = 7;
    d->ep0_state = EP0_IDLE;
    mask(d, DWC3_DCTL, 0, DWC3_DCTL_RUN_STOP);
    if (poll_bits(d, DWC3_DSTS, DWC3_DSTS_DEVCTRLHLT, 0, 500)) return -9;
    d->stats.init_step = 8;
    return 0;
}

void qdwc3_stop(struct qdwc3 *d)
{
    for (unsigned ep = 2; ep < 10; ep++)
        if (d->ep[ep].in_progress)
            ep_command(d, ep, DWC3_DEPCMD_ENDTRANSFER | DWC3_DEPCMD_HIPRI_FORCERM |
                       DWC3_DEPCMD_PARAM((rd(d, DWC3_DEPCMD(ep)) >> 16) & 0x7f), 0, 0, 0);
    wr(d, DWC3_DEVTEN, 0);
    mask(d, DWC3_DCTL, DWC3_DCTL_RUN_STOP, 0);
    poll_bits(d, DWC3_DSTS, DWC3_DSTS_DEVCTRLHLT, DWC3_DSTS_DEVCTRLHLT, 500);
    /* Present a clean disconnect to the host before the reset. */
    qdwc3_wr(d->qscratch + QSCRATCH_HS_PHY_CTRL,
             qdwc3_rd(d->qscratch + QSCRATCH_HS_PHY_CTRL) & ~(UTMI_OTG_VBUS_VALID | SW_SESSVLD_SEL));
    qdwc3_wr(d->qscratch + QSCRATCH_SS_PHY_CTRL, qdwc3_rd(d->qscratch + QSCRATCH_SS_PHY_CTRL) & ~LANE0_PWR_PRESENT);
}
