/* SPDX-License-Identifier: MIT */
/* See ncm.h. The control sequence mirrors tools/a16xhci.py exactly, including
 * the ordering that matters: the xHCI Configure Endpoint command has to run
 * before SET_INTERFACE, because the bulk endpoints only exist in alternate
 * setting 1 and the controller needs their contexts first. */
#include "ncm.h"

void *memcpy(void *dest, const void *src, size_t n);
void *memset(void *dest, int value, size_t n);

#define DESC_DEVICE 1
#define DESC_CONFIGURATION 2
#define DESC_STRING 3
#define DESC_INTERFACE 4
#define DESC_ENDPOINT 5
#define DESC_CS_INTERFACE 0x24

#define CDC_UNION 0x06
#define CDC_ETHERNET 0x0f

#define CLASS_CDC_CONTROL 0x02
#define SUBCLASS_NCM 0x0d

#define EP_BULK 2

static uint32_t le16(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8); }
static uint32_t le32(const uint8_t *p) { return le16(p) | (le16(p + 2) << 16); }
static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static int get_descriptor(struct ncm *n, uint32_t kind, uint32_t index,
                          uint8_t *buffer, uint32_t length, uint32_t language)
{
    return xhci_control(n->x, &n->device, 0x80, 6, (kind << 8) | index, language,
                        buffer, length, 1000);
}

/* Read a string descriptor as ASCII. Returns the character count. */
static int get_string(struct ncm *n, uint32_t index, char *out, uint32_t max)
{
    if (!index) return 0;
    int got = get_descriptor(n, DESC_STRING, index, n->scratch, 255, 0x0409);
    if (got < 4 || n->scratch[1] != DESC_STRING) return 0;
    uint32_t characters = ((uint32_t)n->scratch[0] - 2) / 2;
    if (characters > max) characters = max;
    for (uint32_t k = 0; k < characters; k++) out[k] = (char)n->scratch[2 + k * 2];
    return (int)characters;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* The Ethernet functional descriptor names a string holding 12 hex digits. */
static int parse_mac(struct ncm *n, uint32_t index, uint8_t *mac)
{
    char text[16];
    if (get_string(n, index, text, sizeof(text)) < 12) return -1;
    for (uint32_t k = 0; k < 6; k++) {
        int high = hex_nibble(text[k * 2]), low = hex_nibble(text[k * 2 + 1]);
        if (high < 0 || low < 0) return -1;
        mac[k] = (uint8_t)((high << 4) | low);
    }
    return 0;
}

/* Walk a configuration descriptor for the `which`th NCM function, recording its
 * control and data interfaces and the bulk endpoints in alternate setting 1. */
static int find_function(struct ncm *n, const uint8_t *config, uint32_t total, uint32_t which)
{
    uint32_t offset = 0, found = 0;
    int control = -1, data = -1, mac_index = 0;
    uint32_t in_ep = 0, out_ep = 0, in_packet = 0, out_packet = 0;
    int in_data_alt = 0;

    while (offset + 2 <= total) {
        uint32_t length = config[offset];
        if (length < 2 || offset + length > total) break;
        uint32_t kind = config[offset + 1];
        const uint8_t *item = config + offset;

        if (kind == DESC_INTERFACE && length >= 9) {
            uint32_t number = item[2], alternate = item[3];
            uint32_t klass = item[5], subclass = item[6];
            in_data_alt = (data >= 0 && (int)number == data && alternate == 1);
            if (klass == CLASS_CDC_CONTROL && subclass == SUBCLASS_NCM && alternate == 0) {
                if (control >= 0 && in_ep && out_ep) break;   /* previous one is complete */
                if (found++ < which) {
                    control = -1; data = -1; in_ep = out_ep = 0;
                } else {
                    control = (int)number;
                }
            }
        } else if (kind == DESC_CS_INTERFACE && length >= 3 && control >= 0) {
            if (item[2] == CDC_UNION && length >= 5) data = item[4];
            else if (item[2] == CDC_ETHERNET && length >= 13) mac_index = item[3];
        } else if (kind == DESC_ENDPOINT && length >= 7 && in_data_alt) {
            if ((item[3] & 0x3) == EP_BULK) {
                uint32_t address = item[2];
                uint32_t packet = le16(item + 4) & 0x7ff;
                if (address & 0x80) { in_ep = address & 0xf; in_packet = packet; }
                else { out_ep = address & 0xf; out_packet = packet; }
            }
        }
        offset += length;
    }

    if (control < 0 || data < 0 || !in_ep || !out_ep) return -1;
    n->control_interface = (uint32_t)control;
    n->data_interface = (uint32_t)data;
    /* DCI = endpoint number * 2, plus one for an IN endpoint. */
    n->dci_in = in_ep * 2 + 1;
    n->dci_out = out_ep * 2;
    n->packet_in = in_packet;
    n->packet_out = out_packet;
    if (mac_index) parse_mac(n, (uint32_t)mac_index, n->mac);
    return 0;
}

int ncm_open(struct ncm *n, struct xhci *x, uint32_t which, uint32_t wait_ms)
{
    uint8_t *config;
    uint32_t port;

    memset(n, 0, sizeof(*n));
    n->x = x;

    n->scratch = xhci_alloc(x, 512, 64);
    n->in_buffer = xhci_alloc(x, NCM_IN_BUFFER, 64);
    n->out_buffer = xhci_alloc(x, NCM_OUT_BUFFER, 64);
    config = xhci_alloc(x, 512, 64);
    if (!n->scratch || !n->in_buffer || !n->out_buffer || !config) return -1;
    n->stats.init_step = 1;

    port = xhci_wait_port(x, wait_ms ? wait_ms : 15000);
    if (!port) return -2;
    n->stats.init_step = 2;

    if (xhci_address_device(x, &n->device, port)) return -3;
    n->stats.init_step = 3;

    if (get_descriptor(n, DESC_DEVICE, 0, n->scratch, 18, 0) < 18) return -4;
    n->vendor = (uint16_t)le16(n->scratch + 8);
    n->product = (uint16_t)le16(n->scratch + 10);
    /* Full and low speed devices report the real EP0 size here; if it differs
     * from the 8 we addressed with, the rest of enumeration still works because
     * every transfer below is short. */
    n->device.packet0 = n->scratch[7];
    n->stats.init_step = 4;

    if (get_descriptor(n, DESC_CONFIGURATION, 0, config, 9, 0) < 9) return -5;
    n->configuration = config[5];
    uint32_t total = le16(config + 2);
    if (total > 512) total = 512;
    if (get_descriptor(n, DESC_CONFIGURATION, 0, config, total, 0) < (int)total) return -5;
    if (find_function(n, config, total, which)) return -6;
    n->stats.init_step = 5;

    if (xhci_control(x, &n->device, 0x00, 9, n->configuration, 0, NULL, 0, 1000) < 0) return -7;
    n->stats.init_step = 6;

    const uint32_t dci[2] = {n->dci_out, n->dci_in};
    const uint32_t type[2] = {2, 6};                 /* bulk OUT, bulk IN */
    const uint32_t packet[2] = {n->packet_out, n->packet_in};
    if (xhci_configure_endpoints(x, &n->device, dci, type, packet, 2)) return -8;
    n->stats.init_step = 7;

    if (xhci_control(x, &n->device, 0x01, 11, 1, n->data_interface, NULL, 0, 1000) < 0) return -9;
    n->stats.init_step = 8;

    /* GET_NTB_PARAMETERS: class request on the control interface. */
    n->ntb_in_max = NCM_IN_BUFFER;
    n->ntb_out_max = NCM_OUT_BUFFER;
    n->out_divisor = 4;
    n->out_alignment = 4;
    if (xhci_control(x, &n->device, 0xa1, 0x80, 0, n->control_interface,
                     n->scratch, 28, 1000) >= 28) {
        uint32_t in_max = le32(n->scratch + 4);
        uint32_t out_max = le32(n->scratch + 16);
        if (in_max && in_max < n->ntb_in_max) n->ntb_in_max = in_max;
        if (out_max && out_max < n->ntb_out_max) n->ntb_out_max = out_max;
        n->out_divisor = le16(n->scratch + 20);
        n->out_remainder = le16(n->scratch + 22);
        n->out_alignment = le16(n->scratch + 24);
        if (!n->out_divisor) n->out_divisor = 4;
        if (!n->out_alignment) n->out_alignment = 4;
    }
    /* SET_NTB_INPUT_SIZE: the device otherwise uses its own dwNtbInMaxSize,
     * which here is twice the buffer this driver posts, so a busy moment would
     * hand back a block that does not fit. */
    n->scratch[0] = (uint8_t)n->ntb_in_max;
    n->scratch[1] = (uint8_t)(n->ntb_in_max >> 8);
    n->scratch[2] = (uint8_t)(n->ntb_in_max >> 16);
    n->scratch[3] = (uint8_t)(n->ntb_in_max >> 24);
    xhci_control(x, &n->device, 0x21, 0x86, 0, n->control_interface, n->scratch, 4, 1000);

    /* SET_ETHERNET_PACKET_FILTER: without it some devices stay silent. A
     * device that does not implement it is not a reason to stop. */
    xhci_control(x, &n->device, 0x21, 0x43, 0x0f, n->control_interface, NULL, 0, 1000);
    n->stats.init_step = 9;
    return 0;
}

void ncm_close(struct ncm *n)
{
    if (!n->x) return;
    /* A half-built device -- ncm_open failed before addressing it -- has no EP0
     * ring to talk over, so there is nothing to unwind. */
    if (n->device.slot && n->device.ep[1].base) {
        /* Drop back to the endpoint-less alternate setting before releasing. */
        xhci_control(n->x, &n->device, 0x01, 11, 0, n->data_interface, NULL, 0, 500);
        xhci_release(n->x, &n->device);
    }
    n->x = NULL;
}

int ncm_send(struct ncm *n, const uint8_t *frame, uint32_t length)
{
    uint8_t *out = n->out_buffer;
    uint32_t header = 12, index, ndp, total;

    if (!n->x || !length || length > NCM_MAX_FRAME) return -1;
    index = (header + n->out_alignment - 1) & ~(n->out_alignment - 1);
    ndp = (index + length + 3) & ~3u;
    total = ndp + 16;
    if (total > n->ntb_out_max || total > NCM_OUT_BUFFER) {
        n->stats.oversize++;
        return -2;
    }

    memset(out, 0, total);
    /* NTH16 */
    out[0] = 'N'; out[1] = 'C'; out[2] = 'M'; out[3] = 'H';
    put16(out + 4, header);
    put16(out + 6, n->sequence++);
    put16(out + 8, total);
    put16(out + 10, ndp);
    memcpy(out + index, frame, length);
    /* NDP16 with one datagram and the terminating zero entry. */
    out[ndp] = 'N'; out[ndp + 1] = 'C'; out[ndp + 2] = 'M'; out[ndp + 3] = '0';
    put16(out + ndp + 4, 16);
    put16(out + ndp + 6, 0);
    put16(out + ndp + 8, index);
    put16(out + ndp + 10, length);
    put16(out + ndp + 12, 0);
    put16(out + ndp + 14, 0);

    if (xhci_send(n->x, &n->device, n->dci_out, out, total, 1000) < 0) {
        n->stats.send_failures++;
        return -3;
    }
    n->stats.blocks_out++;
    n->stats.frames_out++;
    n->stats.bytes_out += length;
    return (int)length;
}

int ncm_receive(struct ncm *n, uint8_t *frame, uint32_t max)
{
    if (!n->x) return 0;

    /* Hand back the next datagram of the block already in the buffer. */
    while (n->rx_length && n->rx_ndp) {
        const uint8_t *block = n->in_buffer;
        uint32_t ndp = n->rx_ndp;
        uint32_t ndp_length = le16(block + ndp + 4);
        uint32_t entry = ndp + 8 + n->rx_entry * 4;

        if (ndp + ndp_length > n->rx_length || entry + 4 > ndp + ndp_length) {
            n->rx_ndp = le16(block + ndp + 6);   /* wNextNdpIndex */
            n->rx_entry = 0;
            if (n->rx_ndp >= n->rx_length) { n->rx_length = 0; n->rx_ndp = 0; }
            continue;
        }

        uint32_t index = le16(block + entry);
        uint32_t length = le16(block + entry + 2);
        n->rx_entry++;
        if (!index && !length) {                 /* terminating entry */
            n->rx_ndp = le16(block + ndp + 6);
            n->rx_entry = 0;
            if (n->rx_ndp >= n->rx_length) { n->rx_length = 0; n->rx_ndp = 0; }
            continue;
        }
        if (!length || index + length > n->rx_length) {
            n->stats.malformed++;
            n->stats.bad_entry++;
            continue;
        }
        if (length > max) {
            n->stats.oversize++;
            continue;
        }
        memcpy(frame, block + index, length);
        n->stats.frames_in++;
        n->stats.bytes_in += length;
        return (int)length;
    }

    /* The block is spent: queue a transfer and see whether one has landed. */
    n->rx_length = n->rx_ndp = n->rx_entry = 0;
    xhci_post(n->x, &n->device, n->dci_in, n->in_buffer, n->ntb_in_max);
    int got = xhci_reap(n->x, &n->device, n->dci_in, n->ntb_in_max);
    if (got <= 0) return 0;

    const uint8_t *block = n->in_buffer;
    if ((uint32_t)got < 12 || block[0] != 'N' || block[1] != 'C' ||
        block[2] != 'M' || block[3] != 'H') {
        n->stats.malformed++;
        n->stats.bad_signature++;
        return 0;
    }
    uint32_t declared = le16(block + 8);
    n->rx_length = declared && declared <= (uint32_t)got ? declared : (uint32_t)got;
    n->rx_ndp = le16(block + 10);
    n->rx_entry = 0;
    n->stats.blocks_in++;
    if (n->rx_ndp + 8 > n->rx_length) {
        n->rx_length = n->rx_ndp = 0;
        n->stats.malformed++;
        n->stats.bad_ndp++;
        return 0;
    }
    return ncm_receive(n, frame, max);
}
