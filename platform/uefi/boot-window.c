/* SPDX-License-Identifier: MIT */
/* q1n1 A16 boot window (boot services only), linked into q1n1.efi --boot.
 * USB0 comes up in device mode through the physically proven serial-v3 path
 * with firmware's USB Function driver, as 1209:316D serial A16-Q1N1-BOOT.
 * The Mac (or the laptop keyboard) chooses:
 *   proxy       arm BootNext = BootCurrent (the q1n1 UEFI Shell entry), then EL2 proxy
 *   proxy once  EL2 proxy without arming a return
 *   windows     continue to Windows (also the non-auto timeout and every refusal)
 *   shell       stop in the UEFI shell
 * For proxy, DWC3/qscratch registers are captured while firmware runs and after
 * it stops, exactly as q1n1-usb-ebs-prep.efi did, for the post-EBS takeover. */
#define Q1N1_ALLOW_INACTIVE_USB0
#define Q1N1_CDC_LIBRARY
#define Q1N1_ROLE_FORCE_ACTIVE
#define Q1N1_EXTERNAL_MEM
#define Q1N1_CDC_PRODUCT "q1n1 A16 boot window"
#define Q1N1_CDC_SERIAL "A16-Q1N1-BOOT"
#include "usb-serial.c"
#include "boot-window.h"
#include "ucsi-console.h"

#define WINDOW_SECONDS 25       /* --auto selects q1n1 when no host answers */
#define HOST_SECONDS 20         /* extra time once a host opens the port */
#define TYPING_SECONDS 60       /* extra time after any received byte */
#define DRAIN_SECONDS 2
#define EFI_VARIABLE_NV_BS_RT 7

static const efi_guid global_guid = {0x8be4df61,0x93ca,0x11d2,{0xaa,0x0d,0x00,0xe0,0x98,0x03,0x2b,0x8c}};
static const char16 shell_description[] = u"q1n1 UEFI Shell";
static const char shell_path[] = "\\EFI\\q1n1\\shellaa64.efi";

static struct { char data[1024]; unsigned length; } queue;
static efi_handle window_image;
static int held;
static char line[64];
static unsigned line_length;

static void say(const char *s) { while (*s && queue.length < sizeof(queue.data)) queue.data[queue.length++] = *s++; }
static void say_hex(uint64_t value, unsigned digits)
{
    char s[17];
    for (unsigned n = 0; n < digits; n++) s[n] = "0123456789abcdef"[(value >> (4 * (digits - 1 - n))) & 15];
    s[digits] = 0;
    say(s);
}
static char lower(char c) { return c >= 'A' && c <= 'Z' ? (char)(c + 32) : c; }
static int same_text(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}
static int starts_with(const char *text, const char *prefix, const char **rest)
{
    while (*prefix) { if (*text != *prefix) return 0; text++; prefix++; }
    if (*text && *text != ' ') return 0;
    while (*text == ' ') text++;
    *rest = text;
    return 1;
}
static int parse_hex(const char *text, uint64_t *value)
{
    uint64_t result = 0;
    unsigned digits = 0;
    if (text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) text += 2;
    for (; *text; text++) {
        unsigned digit;
        if (*text >= '0' && *text <= '9') digit = (unsigned)(*text - '0');
        else if (*text >= 'a' && *text <= 'f') digit = (unsigned)(*text - 'a' + 10);
        else return 0;
        if (digits >= 16) return 0;
        result = result << 4 | digit;
        digits++;
    }
    *value = result;
    return digits != 0;
}

/* Concatenate File Path media nodes and compare, ignoring case. */
static int path_matches(const uint8_t *path, uint64_t length)
{
    char joined[128];
    unsigned n = 0;
    for (uint64_t offset = 0; offset + 4 <= length;) {
        uint8_t type = path[offset], subtype = path[offset + 1];
        uint16_t size = (uint16_t)(path[offset + 2] | path[offset + 3] << 8);
        if (size < 4 || offset + size > length) return 0;
        if (type == 0x7f && subtype == 0xff) break;
        if (type == 4 && subtype == 4) {
            for (uint64_t k = offset + 4; k + 1 < offset + size; k += 2) {
                uint16_t c = (uint16_t)(path[k] | path[k + 1] << 8);
                if (!c) break;
                if (c > 127 || n + 2 >= sizeof(joined)) return 0;
                if (k == offset + 4 && n && joined[n - 1] != '\\' && c != '\\') joined[n++] = '\\';
                joined[n++] = (char)c;
            }
        }
        offset += size;
    }
    if (n != sizeof(shell_path) - 1) return 0;
    for (unsigned k = 0; k < n; k++) if (lower(joined[k]) != lower(shell_path[k])) return 0;
    return 1;
}

/* BootCurrent must be the Windows-created "q1n1 UEFI Shell" load option that
 * runs \EFI\q1n1\shellaa64.efi; only that option is armed as BootNext. */
static efi_status shell_entry(struct efi_runtime_services *rt, uint16_t *number)
{
    uint16_t current = 0;
    uint64_t size = sizeof(current);
    uint32_t attributes = 0;
    efi_status status = rt->get_variable(u"BootCurrent", &global_guid, &attributes, &size, &current);
    if (status) return status;
    if (size != sizeof(current)) return EFI_UNSUPPORTED;
    char16 name[9] = u"Boot0000";
    for (unsigned k = 0; k < 4; k++) name[4 + k] = (char16)"0123456789ABCDEF"[(current >> (12 - 4 * k)) & 15];
    static uint8_t option[4096];
    size = sizeof(option);
    status = rt->get_variable(name, &global_guid, &attributes, &size, option);
    if (status) return status;
    uint64_t path_offset = 6 + sizeof(shell_description);
    if (size < path_offset || !(option[0] & 1)) return EFI_NOT_FOUND;
    for (unsigned k = 0; k < sizeof(shell_description) / 2; k++)
        if ((uint16_t)(option[6 + 2 * k] | option[7 + 2 * k] << 8) != shell_description[k]) return EFI_NOT_FOUND;
    uint16_t path_length = (uint16_t)(option[4] | option[5] << 8);
    if (path_offset + path_length > size || !path_matches(option + path_offset, path_length)) return EFI_NOT_FOUND;
    *number = current;
    return 0;
}
static efi_status arm_return(struct efi_runtime_services *rt, uint16_t number)
{
    efi_status status = rt->set_variable(u"BootNext", &global_guid, EFI_VARIABLE_NV_BS_RT, sizeof(number), &number);
    if (status) return status;
    uint16_t check = 0;
    uint64_t size = sizeof(check);
    uint32_t attributes = 0;
    status = rt->get_variable(u"BootNext", &global_guid, &attributes, &size, &check);
    if (status) return status;
    return size == sizeof(check) && check == number ? 0 : EFI_UNSUPPORTED;
}

/* Like service(), but received bytes feed the command line instead of an echo
 * and the queued text is sent once the host asserts DTR. */
static void feed(const uint8_t *data, unsigned count, char *command, unsigned command_size)
{
    for (unsigned n = 0; n < count; n++) {
        char c = (char)data[n];
        if (c == '\r' || c == '\n') {
            if (line_length && !command[0]) {
                unsigned k = 0;
                for (; k < line_length && k + 1 < command_size; k++) command[k] = lower(line[k]);
                command[k] = 0;
            }
            line_length = 0;
        } else if (line_length < sizeof(line)) {
            line[line_length++] = c;
        }
    }
}
static efi_status boot_service(struct cdc *c, char *command, unsigned command_size)
{
    if (!c->configured || c->control_stage != CTL_IDLE) return 0;
    efi_status s;
    if (c->echo_bytes) { feed(c->rx, c->echo_bytes, command, command_size); c->echo_bytes = 0; }
    if (!c->rx_busy) {
        unsigned n = c->speed == USB_SUPER ? 1024 : c->speed == USB_HIGH ? 512 : 64;
        s = submit(c, 2, USB_OUT, c->rx, n);
        if (!s) { c->rx_busy = 1; c->rx_bytes = n; } else if (s != NOT_READY) return s;
    }
    if (!c->tx_busy && (c->lines & 1) && queue.length) {
        unsigned n = queue.length < 256 ? queue.length : 256;
        copy_bytes(c->tx, queue.data, n);
        s = submit(c, 2, USB_IN, c->tx, n);
        if (!s) {
            c->tx_busy = 1; c->tx_bytes = n;
            for (unsigned k = n; k < queue.length; k++) queue.data[k - n] = queue.data[k];
            queue.length -= n;
        } else if (s != NOT_READY) return s;
    }
    if (!c->notify_busy && c->notify_dirty) {
        const uint8_t notification[] = {0xa1,0x20,0,0,0,0,2,0,(c->lines & 1) ? 3 : 0,0};
        copy_bytes(c->notification, notification, sizeof(notification));
        s = submit(c, 1, USB_IN, c->notification, sizeof(notification));
        if (!s) { c->notify_busy = 1; c->notify_dirty = 0; } else if (s != NOT_READY) return s;
    }
    return 0;
}

static uint32_t mmio(uint64_t address) { return *(volatile uint32_t *)(uintptr_t)address; }
static void capture(uint32_t regs[], uint32_t qs[])
{
    for (unsigned n = 0; n < USB_EBS_REG_COUNT; n++) regs[n] = mmio(USB_EBS_DWC3_BASE + usb_ebs_regs[n]);
    for (unsigned n = 0; n < USB_EBS_QSCRATCH_COUNT; n++) qs[n] = mmio(USB_EBS_QSCRATCH_BASE + 4 * n);
}
static uint64_t now_ticks(void) { uint64_t v; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(v)); return v; }

static void banner(const struct q1n1_boot_result *r, uint64_t seconds_left,
                   int automatic)
{
    say("Q1N1 BOOT WINDOW v1 bootcurrent=");
    say_hex(r->boot_current, 4);
    say(r->entry_status ? " return=unavailable" : " return=available");
    say(" commands: proxy | proxy once | windows | shell | status; default ");
    say(automatic ? "q1n1" : "windows");
    say(" in 0x");
    say_hex(seconds_left, 2);
    say(" s\r\n");
}

efi_status q1n1_boot_window(efi_handle image, struct efi_system_table *st,
                            struct q1n1_boot_result *r, int automatic)
{
    struct efi_runtime_services *rt = st->runtime_services;
    window_image = image;
    held = 0;
    *r = (struct q1n1_boot_result){0};
    r->choice = Q1N1_BOOT_WINDOWS;
    queue.length = line_length = 0;
    out(st, "q1n1 boot window v1: USB0 CDC ACM 1209:316D serial A16-Q1N1-BOOT\n"
            "Keys: P proxy, O proxy once, W Windows, S or ESC shell.\n");
    out(st, automatic ? "Default: q1n1 after 25 s.\n" : "Default: Windows after 25 s.\n");
    if (!rt) return EFI_UNSUPPORTED;
    r->entry_status = shell_entry(rt, &r->boot_current);
    number(st, "BootCurrent: ", r->boot_current);
    out(st, r->entry_status ? "BootCurrent is not the q1n1 UEFI Shell entry; proxy cannot arm a return.\n"
                            : "BootCurrent is the q1n1 UEFI Shell entry; proxy arms it as BootNext.\n");
    uint8_t *config = find_firmware(st, CONFIG_IMAGE_SIZE, CONFIG_CODE_RVA, CONFIG_CODE_SIZE, CONFIG_CODE_FNV);
    if (!config || *(const uint64_t *)(config + USB_EBS_CONFIG_DWC3_TABLE_RVA) != USB_EBS_DWC3_BASE ||
        *(const uint64_t *)(config + USB_EBS_CONFIG_QSCRATCH_TABLE_RVA) != USB_EBS_QSCRATCH_BASE) {
        out(st, "REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.\n");
        return EFI_UNSUPPORTED;
    }
    efi_status status = prepare_usb0(image, st, 1);
    if (status) return status;
    struct cdc c = {0};
    c.fn = checked_usbfn(st);
    if (!c.fn) { out(st, "REFUSED: USB Function driver code or ABI differs from BIOS312.\n"); return EFI_UNSUPPORTED; }
    c.speed = USB_HIGH;
    const uint8_t coding[] = {0, 0xc2, 1, 0, 0, 0, 8};
    copy_bytes(c.line_coding, coding, 7);
    uint8_t **buffers[] = {&c.control, &c.rx, &c.tx, &c.notification};
    unsigned allocated = 0;
    status = st->boot->set_watchdog(300, 0, 0, NULL);
    int watchdog = !status;
    for (; !status && allocated < 4; allocated++) {
        status = c.fn->allocate(c.fn, DMA_SIZE, (void **)buffers[allocated]);
        if (status) break;
    }
    int started = 0;
    if (!status) { started = 1; status = c.fn->start(c.fn); }
    if (!status) status = c.fn->configure_ex(c.fn, &device_info, &ss_device);
    number(st, "USBFn initialization status: ", status);

    struct usb_ebs_snapshot *snap = &r->snapshot;
    snap->magic = USB_EBS_MAGIC; snap->size = sizeof(*snap);
    snap->dwc3_base = USB_EBS_DWC3_BASE; snap->qscratch_base = USB_EBS_QSCRATCH_BASE;
    int chose = 0, arm = 0;
    if (!status) {
        efi_status (*stall)(uint64_t) = (efi_status (*)(uint64_t))st->boot->stall;
        efi_status (*read_key)(struct efi_input *, void *) = st->console_in ?
            (efi_status (*)(struct efi_input *, void *))st->console_in->read_key : NULL;
        uint64_t hz; __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(hz));
        uint64_t start = now_ticks(), deadline = start + hz * WINDOW_SECONDS, drain = 0, last_banner = 0;
        unsigned previous_lines = 0;
        uint64_t previous_received = 0;
        char command[32] = {0};
        while (hz) {
            uint64_t now = now_ticks();
            union usb_payload payload = {0};
            uint64_t size = sizeof(payload);
            uint32_t event = 0;
            status = c.fn->event(c.fn, &event, &size, &payload);
            if (status == NOT_READY) { status = 0; event = USB_NONE; }
            if (status) break;
            if (event == USB_SPEED) snap->firmware_speed = payload.speed;
            status = event_step(&c, event, &payload, size);
            if (status) break;
            status = boot_service(&c, command, sizeof(command));
            if (status) break;
            if (drain) {
                if ((!queue.length && !c.tx_busy) || now > drain) break;
            } else {
                if ((c.lines & 1) && !(previous_lines & 1)) {
                    if (deadline < now + hz * HOST_SECONDS) deadline = now + hz * HOST_SECONDS;
                    last_banner = 0;
                }
                previous_lines = c.lines;
                if (c.received != previous_received) {
                    previous_received = c.received;
                    if (deadline < now + hz * TYPING_SECONDS) deadline = now + hz * TYPING_SECONDS;
                }
                if (!held && (c.lines & 1) && (!last_banner || now - last_banner > hz * 3)) {
                    banner(r, now < deadline ? (deadline - now) / hz : 0, automatic);
                    last_banner = now;
                }
                struct { uint16_t scan, unicode; } key;
                if (read_key && !read_key(st->console_in, &key)) {
                    char k = lower((char)key.unicode);
                    if (k == 'p') copy_bytes(command, "proxy", 6);
                    else if (k == 'o') copy_bytes(command, "proxy once", 11);
                    else if (k == 'w') copy_bytes(command, "windows", 8);
                    else if (k == 's' || key.scan == 23 || key.unicode == 27) copy_bytes(command, "shell", 6);
                }
                if (command[0]) {
                    const char *rest = command;
                    if (same_text(command, "proxy")) { r->choice = Q1N1_BOOT_PROXY; arm = 1; chose = 1; }
                    else if (same_text(command, "proxy once")) { r->choice = Q1N1_BOOT_PROXY; chose = 1; }
                    else if (same_text(command, "windows")) { r->choice = Q1N1_BOOT_WINDOWS; chose = 1; }
                    else if (same_text(command, "shell")) { r->choice = Q1N1_BOOT_SHELL; chose = 1; }
                    else if (same_text(command, "hold")) {
                        held = 1;
                        if (watchdog) { st->boot->set_watchdog(0, 0, 0, NULL); watchdog = 0; }
                        say("OK hold - no timeout; use ucsi, then proxy | windows | shell\r\n");
                    }
                    else if (starts_with(command, "ucsi", &rest)) {
                        const char *argument = rest;
                        uint64_t value = 0;
                        if (same_text(rest, "init")) {
                            efi_status s = q1n1_ucsi_init(window_image, st);
                            say("UCSI init status="); say_hex(s, 16);
                            say(" ready="); say_hex((uint64_t)q1n1_ucsi_ready(), 1); say("\r\n");
                        } else if (starts_with(rest, "cmd", &argument) && parse_hex(argument, &value)) {
                            static struct q1n1_ucsi_result result;
                            q1n1_ucsi_command(st, value, &result);
                            say("UCSI cci="); say_hex(result.cci, 8);
                            say(" len="); say_hex(result.length, 2);
                            say(" stable="); say_hex(result.stable, 1);
                            say(" status="); say_hex(result.status, 16);
                            say(" data=");
                            for (unsigned n = 0; n < result.length; n++) say_hex(result.data[n], 2);
                            say("\r\n");
                        } else {
                            say("ERR use: ucsi init | ucsi cmd <hex control>\r\n");
                        }
                    }
                    else if (same_text(command, "status")) {
                        say("STATUS el=");
                        uint64_t el; __asm__ volatile("mrs %0, CurrentEL" : "=r"(el));
                        say_hex(el >> 2, 1);
                        say(" configured="); say_hex(c.configured, 1);
                        say(" speed="); say_hex(c.speed, 1);
                        say(" bootcurrent="); say_hex(r->boot_current, 4);
                        say(" entry_status="); say_hex(r->entry_status, 16);
                        say("\r\n");
                    } else {
                        say("ERR unknown command\r\n");
                    }
                    command[0] = 0;
                }
                if (!held && !chose && now > deadline) {
                    say("TIMEOUT\r\n");
                    /* --auto chooses q1n1 even when USB0 enumerated but no
                     * host command arrived. With no USB0 host, EL2 discovers
                     * the direct USB1 link after ExitBootServices. */
                    r->choice = q1n1_timeout_choice(automatic, c.configured);
                    arm = automatic;
                    chose = 1;
                }
                if (chose) {
                    if (r->choice == Q1N1_BOOT_PROXY_NCM) {
                        /* Nothing to snapshot: the device-mode controller is
                         * left exactly as firmware had it. */
                        out(st, "No USB0 host; discovering a direct USB link.\n");
                    } else if (r->choice == Q1N1_BOOT_PROXY && !c.configured) {
                        out(st, "Proxy needs the Mac connected and configured; staying in the shell.\n");
                        r->choice = Q1N1_BOOT_SHELL;
                        arm = 0;
                    }
                    if ((r->choice == Q1N1_BOOT_PROXY || r->choice == Q1N1_BOOT_PROXY_NCM) && arm) {
                        r->arm_status = r->entry_status ? r->entry_status : arm_return(rt, r->boot_current);
                        r->return_armed = !r->arm_status;
                    }
                    say(r->choice == Q1N1_BOOT_PROXY ? "OK proxy" : r->choice == Q1N1_BOOT_SHELL ? "OK shell" : "OK windows");
                    if (r->choice == Q1N1_BOOT_PROXY) say(r->return_armed ? " return=armed" : " return=not-armed");
                    say("\r\n");
                    drain = now + hz * DRAIN_SECONDS;
                    r->configured = c.configured;
                    if (r->choice == Q1N1_BOOT_PROXY) capture(snap->running, snap->qs_running);
                }
            }
            if (event == USB_NONE) stall(1000);
        }
        if (c.configured || r->configured) snap->flags |= USB_EBS_FIRMWARE_CONFIGURED;
    }
    number(st, "Boot window USB status: ", status);
    number(st, "Firmware setup requests: ", c.setups);
    snap->firmware_setups = c.setups;
    snap->firmware_resets = c.resets;
    if (started) {
        efi_status stopped = c.fn->stop(c.fn);
        number(st, "USBFn stop status: ", stopped);
        if (stopped) {
            out(st, "STOP FAILED. Keeping DMA buffers and this app resident. Power-cycle the A16.\n");
            for (;;) __asm__ volatile("wfe");
        }
        snap->flags |= USB_EBS_FIRMWARE_STOPPED;
    }
    for (unsigned n = 0; n < allocated; n++) c.fn->free(c.fn, *buffers[n]);
    if (watchdog) st->boot->set_watchdog(0, 0, 0, NULL);
    if (status || !chose) {
        if (r->return_armed) rt->set_variable(u"BootNext", &global_guid, EFI_VARIABLE_NV_BS_RT, 0, NULL);
        r->return_armed = 0;
        r->choice = Q1N1_BOOT_WINDOWS;
        return status ? status : EFI_UNSUPPORTED;
    }
    out(st, r->choice == Q1N1_BOOT_PROXY ? "Choice: proxy\n"
          : r->choice == Q1N1_BOOT_PROXY_NCM ? "Choice: proxy over direct USB (no USB0 host)\n"
          : r->choice == Q1N1_BOOT_SHELL ? "Choice: shell\n" : "Choice: Windows\n");
    /* The NCM choice takes nothing over, so none of the device-mode snapshot
     * validation below applies to it. */
    if (r->choice != Q1N1_BOOT_PROXY) return 0;

    capture(snap->stopped, snap->qs_stopped);
    uint32_t id = usb_ebs_reg(snap->running, 0xc120), gctl = usb_ebs_reg(snap->running, 0xc110);
    if (!id || id == 0xffffffff || id != usb_ebs_reg(snap->stopped, 0xc120) || ((gctl >> 12) & 3) != 2 ||
        !(snap->qs_running[0x10 / 4] & (1u << 20)) || !(snap->flags & USB_EBS_FIRMWARE_CONFIGURED)) {
        number(st, "GSNPSID running: ", id);
        number(st, "GCTL running: ", gctl);
        number(st, "HS_PHY_CTRL running: ", snap->qs_running[0x10 / 4]);
        out(st, "REFUSED: firmware state is not a configured device-mode DWC3; not leaving firmware.\n");
        if (r->return_armed) rt->set_variable(u"BootNext", &global_guid, EFI_VARIABLE_NV_BS_RT, 0, NULL);
        r->return_armed = 0;
        r->choice = Q1N1_BOOT_WINDOWS;
        return EFI_UNSUPPORTED;
    }
    snap->checksum = usb_ebs_fnv((const uint8_t *)snap, offsetof(struct usb_ebs_snapshot, checksum));
    number(st, "Return armed (BootNext): ", r->return_armed);
    number(st, "Return arm status: ", r->arm_status);
    return 0;
}
