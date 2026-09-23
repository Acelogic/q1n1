#!/usr/bin/env python3
"""Host side of the q1n1 proxy: m1n1's uartproxy protocol over USB CDC ACM.

The wire format matches m1n1 (proxyclient/m1n1/proxy.py), so upstream's
UartInterface/M1N1Proxy can drive the same target. This module has no
third-party dependencies: it talks to a tty or a UNIX socket directly, so it
works without pyserial on a stock macOS python3.

  python3 tools/q1n1proxy.py info
  python3 tools/q1n1proxy.py peek 0x0a60c120
  python3 tools/q1n1proxy.py dump 0x0a600000 0x200
  python3 tools/q1n1proxy.py reboot
  python3 tools/q1n1proxy.py shell
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import select
import socket
import struct
import sys
import termios
import time
from typing import Callable, Optional

MAGIC = 0x4F464E49314E3151  # "Q1N1INFO"
REQ_NOP = 0x00AA55FF
REQ_PROXY = 0x01AA55FF
REQ_MEMREAD = 0x02AA55FF
REQ_MEMWRITE = 0x03AA55FF
REQ_BOOT = 0x04AA55FF
REQ_EVENT = 0x05AA55FF
CMD_LEN = 56
REPLY_LEN = 36
EVENT_HDR_LEN = 8
ST_OK = 0
CHECKSUM_SENTINEL = 0xD0DECADE
DATA_END_SENTINEL = 0xB0CACC10
FEATURE_DISABLE_DATA_CSUMS = 1
GUARD_MARKER = 0xACCE5515ABAD1DEA
STAGE_MAGIC = 0x3147545331314E51  # "Q1N1STG1" at offset 8 of a stage image
# Offset 24 holds a config word the host patches before uploading. The low two
# bits pick the boot emblem and its placement; 0 (the q1n1 dragon, centred) is
# what an unpatched stage draws.
STAGE_LOGO = {'centre': 0, 'corner': 1, 'none': 2, 'asahi': 3}

START_BOOT, START_EXCEPTION = 0, 1
EXC_RET_UNHANDLED, EXC_RET_HANDLED = 1, 2
GUARD_OFF, GUARD_SKIP, GUARD_MARK, GUARD_RETURN = 0, 1, 2, 3

# platform/uefi/main.c struct q1n1_bootinfo, all u64 in this order.
BOOTINFO_FIELDS = (
    'magic version size '
    'image_base image_size entry_el current_el mode '
    'system_table runtime_services config_tables config_count acpi_rsdp smbios3 '
    'fb_base fb_size fb_width fb_height fb_stride fb_format '
    'memory_map memory_map_size memory_map_stride memory_map_version '
    'heap_base heap_size dma_base dma_size '
    'usb_dwc3 usb_qscratch usb_snapshot usb_stats proxy_stats exception '
    'boot_current return_armed arm_status entry_status timer_hz '
    'gic_distributor gic_redistributor gic_stats '
    'stage_base stage_size stage_generation '
    'xhci_base xhci_stats ncm_stats ncm_proxy_stats usb_ports usb_port_count '
    'preload_table preload_size preload_status preload_verify preload_diagnostics'
).split()

# platform/uefi/main.c struct q1n1_exception.
EXCEPTION_FIELDS = [f'x{n}' for n in range(31)] + ['sp', 'spsr', 'elr', 'esr', 'far', 'el', 'vector']

PROXY_SERIAL = 'A16-Q1N1-EL2'
DIRECT_SERIAL = 'A16-Q1N1-EL2B'
PROXY_INTERFACE = 1   # first CDC ACM data interface = proxy port
CONSOLE_INTERFACE = 3  # second data interface = q1n1 console


class ProxyError(RuntimeError):
    pass


class ProxyTimeout(ProxyError):
    pass


class ProxyRemoteError(ProxyError):
    pass


def checksum(data, value=0xDEADBEEF):
    for byte in data:
        value = (value * 31337 + (byte ^ 0x5A)) & 0xFFFFFFFF
    return value ^ 0xADDEDBAD


def find_ports(serial=PROXY_SERIAL, interface=PROXY_INTERFACE):
    """Reuse the tested ioreg walk from the USB serial test tool (its main()
    is guarded, so importing it runs no test)."""
    path = Path(__file__).resolve().parent / 'test-a16-usb-serial.py'
    spec = importlib.util.spec_from_file_location('a16_usb_serial', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ports, _ = module.inventory(serial, interface)
    if serial == PROXY_SERIAL:
        direct, _ = module.inventory(DIRECT_SERIAL, interface)
        ports += [path for path in direct if path not in ports]
    return ports


class Transport:
    """Raw byte stream: a tty path (CDC ACM or pty) or a UNIX socket path."""

    def __init__(self, path):
        self.path = str(path)
        self.sock = None
        if Path(self.path).is_socket():
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(self.path)
            self.sock.setblocking(False)
            self.fd = self.sock.fileno()
        else:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                mode = termios.tcgetattr(self.fd)
                # Raw: no echo, no canonical mode, no flow control, no translation.
                mode[0] = mode[1] = mode[3] = 0
                mode[2] = (mode[2] | termios.CLOCAL | termios.CREAD) & ~termios.CRTSCTS
                mode[6][termios.VMIN] = 0
                mode[6][termios.VTIME] = 0
                termios.tcsetattr(self.fd, termios.TCSANOW, mode)
                termios.tcflush(self.fd, termios.TCIOFLUSH)
            except termios.error:
                pass  # a pipe or plain file is fine too
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def close(self):
        if self.sock:
            self.sock.close()
        else:
            os.close(self.fd)

    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self.fd], [], max(0, left))[1]:
                raise ProxyTimeout('serial write timed out; transport may have disconnected')
            try:
                sent = os.write(self.fd, view)
            except BlockingIOError:
                continue
            if sent <= 0:
                raise ProxyError('serial transport closed during write')
            view = view[sent:]

    def read(self, count, timeout=5.0):
        out = bytearray()
        deadline = time.monotonic() + timeout
        while len(out) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProxyTimeout(f'expected {count} bytes, got {len(out)}')
            if not select.select([self.fd], [], [], remaining)[0]:
                continue
            try:
                block = os.read(self.fd, count - len(out))
            except BlockingIOError:
                continue
            if not block:
                time.sleep(0.005)
                continue
            out += block
        return bytes(out)


class Link:
    """m1n1 uartproxy framing."""

    def __init__(self, path, debug=False, timeout=5.0, tty_out=None, transport=None):
        # A caller may supply its own transport -- the raw Ethernet link in
        # tools/ethlink.py presents the same read/write surface as a tty.
        self.transport = transport if transport is not None else Transport(path)
        self.debug = debug
        self.timeout = timeout
        self.features = 0
        self.tty_out = tty_out if tty_out is not None else sys.stderr
        self.exception_handler: Optional[Callable[[int, int], None]] = None
        self.boot_seen = 0
        self.read_retries = 0

    def close(self):
        self.transport.close()

    def cmd(self, command, payload=b''):
        if len(payload) > CMD_LEN:
            raise ValueError('payload too long')
        message = struct.pack('<I', command) + payload.ljust(CMD_LEN, b'\0')
        message += struct.pack('<I', checksum(message))
        if self.debug:
            print(f'<< {message.hex()}', file=sys.stderr)
        self.transport.write(message)

    def unknown(self, data):
        if self.tty_out:
            self.tty_out.write(data.decode('utf-8', 'replace'))
            self.tty_out.flush()

    def reply(self, command, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        window = b''
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProxyTimeout(f'no reply to {command:#x}')
            byte = self.transport.read(1, left)
            window = (window + byte)[-4:]
            if len(window) < 4 or window[:3] != b'\xff\x55\xaa':
                if len(window) == 4:
                    self.unknown(window[:1])
                continue
            kind = struct.unpack('<I', window)[0]
            if kind == REQ_EVENT:
                header = self.transport.read(EVENT_HDR_LEN - 4, timeout)
                length = struct.unpack('<HH', header)[0]
                self.transport.read(length + 4, timeout)
                window = b''
                continue
            body = self.transport.read(REPLY_LEN - 4, timeout)
            if self.debug:
                print(f'>> {window.hex()}{body.hex()}', file=sys.stderr)
            status, data, check = struct.unpack('<i24sI', body)
            if check != checksum(window + body[:-4]):
                raise ProxyError('reply checksum error')
            if kind == REQ_BOOT and status == ST_OK and kind != command:
                self.handle_boot(data)
                window = b''
                continue
            if kind != command:
                raise ProxyError(f'reply for {kind:#x}, expected {command:#x}')
            if status != ST_OK:
                raise ProxyRemoteError(f'target reported status {status}')
            return data

    def handle_boot(self, data):
        reason, code, info = struct.unpack('<IIQ', data[:16])
        self.boot_seen += 1
        if reason == START_EXCEPTION and self.exception_handler:
            self.exception_handler(code, info)
        elif reason == START_EXCEPTION:
            print(f'q1n1: unhandled exception callback, info {info:#x}', file=sys.stderr)

    def nop(self, features=FEATURE_DISABLE_DATA_CSUMS):
        self.cmd(REQ_NOP, struct.pack('<Q', features))
        self.features = struct.unpack('<Q', self.reply(REQ_NOP)[:8])[0]
        return self.features

    def data_checksum(self, data):
        return CHECKSUM_SENTINEL if self.features & FEATURE_DISABLE_DATA_CSUMS else checksum(data)

    def proxyreq(self, request, no_reply=False, timeout=None):
        self.cmd(REQ_PROXY, request)
        if no_reply:
            return None
        return self.reply(REQ_PROXY, timeout)

    def readmem(self, address, size, timeout=None, attempts=3):
        """Read target memory, retrying a corrupted payload.

        The transport reads exactly `size` bytes or raises, so a checksum
        mismatch means the bytes were corrupted in flight with the stream still
        in sync -- reissuing the request is safe. Rare, but not never: the CDC
        link carries thousands of small reads once a USB host driver is polling
        rings over it, so a lost payload should cost a retry, not the session.
        """
        if not size:
            return b''
        timeout = self.timeout if timeout is None else timeout
        for attempt in range(attempts):
            self.cmd(REQ_MEMREAD, struct.pack('<QQ', address, size))
            expected = struct.unpack('<I', self.reply(REQ_MEMREAD, timeout)[:4])[0]
            data = self.transport.read(size, max(timeout, size / 65536))
            if self.features & FEATURE_DISABLE_DATA_CSUMS:
                sentinel = struct.unpack('<I', self.transport.read(4, timeout))[0]
                if sentinel != DATA_END_SENTINEL:
                    raise ProxyError('read data sentinel error')
            if expected == self.data_checksum(data):
                return data
            self.read_retries += 1
            if self.debug:
                print(f'!! read checksum error at {address:#x} ({size} bytes), '
                      f'attempt {attempt + 1}/{attempts}', file=sys.stderr)
        raise ProxyError(f'read data checksum error at {address:#x} after {attempts} attempts')

    def writemem(self, address, data, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        self.cmd(REQ_MEMWRITE, struct.pack('<QQI', address, len(data), self.data_checksum(data)))
        for offset in range(0, len(data), 8192):
            self.transport.write(data[offset:offset + 8192])
        if self.features & FEATURE_DISABLE_DATA_CSUMS:
            self.transport.write(struct.pack('<I', DATA_END_SENTINEL))
        self.reply(REQ_MEMWRITE, max(timeout, len(data) / 65536))


class Proxy:
    """m1n1 opcode numbers; only the ones q1n1 implements are exposed."""

    P_NOP, P_EXIT, P_CALL, P_GET_BOOTARGS, P_GET_BASE = 0x000, 0x001, 0x002, 0x003, 0x004
    P_VECTOR = 0x00B
    P_UDELAY, P_SET_EXC_GUARD, P_GET_EXC_COUNT = 0x006, 0x007, 0x008
    P_REBOOT = 0x010
    P_WRITE64, P_WRITE32, P_WRITE16, P_WRITE8 = 0x100, 0x101, 0x102, 0x103
    P_READ64, P_READ32, P_READ16, P_READ8 = 0x104, 0x105, 0x106, 0x107
    P_SET64, P_SET32, P_SET16, P_SET8 = 0x108, 0x109, 0x10A, 0x10B
    P_CLEAR64, P_CLEAR32, P_CLEAR16, P_CLEAR8 = 0x10C, 0x10D, 0x10E, 0x10F
    P_MASK64, P_MASK32, P_MASK16, P_MASK8 = 0x110, 0x111, 0x112, 0x113
    P_WRITEREAD64, P_WRITEREAD32, P_WRITEREAD16, P_WRITEREAD8 = 0x114, 0x115, 0x116, 0x117
    P_MEMCPY64, P_MEMCPY32, P_MEMCPY16, P_MEMCPY8 = 0x200, 0x201, 0x202, 0x203
    P_MEMSET64, P_MEMSET32, P_MEMSET16, P_MEMSET8 = 0x204, 0x205, 0x206, 0x207
    P_IC_IALLUIS, P_IC_IALLU, P_IC_IVAU = 0x300, 0x301, 0x302
    P_DC_IVAC, P_DC_ZVA, P_DC_CVAC = 0x303, 0x307, 0x308
    P_DC_CVAU, P_DC_CIVAC = 0x309, 0x30A
    P_FB_INIT, P_FB_SHUTDOWN = 0xD00, 0xD01

    def __init__(self, link: Link):
        self.link = link
        link.exception_handler = self.on_exception
        self.exceptions = []
        self.resume_skip = True

    def fb_console(self, enabled):
        """Enable q1n1 status drawing, or yield the panel without clearing it.

        This does not power off the display. Disabled state persists across
        calls so inspecting a stopped guest cannot overwrite its console.
        Returns the previous enabled state.
        """
        return self.request(self.P_FB_INIT if enabled else self.P_FB_SHUTDOWN, 0)

    def request(self, opcode, *args, no_reply=False, timeout=None):
        values = list(args) + [0] * (6 - len(args))
        reply = self.link.proxyreq(struct.pack('<7Q', opcode, *[v & 0xFFFFFFFFFFFFFFFF for v in values]),
                                   no_reply=no_reply, timeout=timeout)
        if no_reply or reply is None:
            return None
        echo, status, value = struct.unpack('<QqQ', reply)
        if echo != opcode:
            raise ProxyError(f'opcode echo {echo:#x}, expected {opcode:#x}')
        if status != 0:
            raise ProxyRemoteError(f'opcode {opcode:#x} rejected (status {status})')
        return value

    def on_exception(self, code, info):
        """Report the target's unhandled exception, then resume past it."""
        registers = self.read_exception(info)
        self.exceptions.append(registers)
        print(f"q1n1 exception: ESR {registers['esr']:#x} ELR {registers['elr']:#x} "
              f"FAR {registers['far']:#x} EL{registers['el']} vector {registers['vector']}",
              file=sys.stderr)
        if self.resume_skip:
            self.write64(info + 8 * EXCEPTION_FIELDS.index('elr'), registers['elr'] + 4)
            self.request(self.P_EXIT, EXC_RET_HANDLED)

    def read_exception(self, address):
        data = self.link.readmem(address, 8 * len(EXCEPTION_FIELDS))
        values = struct.unpack(f'<{len(EXCEPTION_FIELDS)}Q', data)
        return dict(zip(EXCEPTION_FIELDS, values))

    def nop(self):
        return self.request(self.P_NOP)

    def bootinfo(self):
        address = self.request(self.P_GET_BOOTARGS)
        magic, version, size = struct.unpack('<3Q', self.link.readmem(address, 24))
        if magic != MAGIC or version != 1 or size < 24 or size % 8 or size > 4096:
            raise ProxyError(f'invalid bootinfo header: magic={magic:#x}, version={version}, size={size}')
        count = min(size // 8, len(BOOTINFO_FIELDS))
        data = self.link.readmem(address, 8 * count)
        values = dict.fromkeys(BOOTINFO_FIELDS, 0)
        values.update(zip(BOOTINFO_FIELDS, struct.unpack(f'<{count}Q', data)))
        values['address'] = address
        return values

    def get_base(self):
        return self.request(self.P_GET_BASE)

    def read64(self, address):
        return self.request(self.P_READ64, address)

    def read32(self, address):
        return self.request(self.P_READ32, address)

    def read16(self, address):
        return self.request(self.P_READ16, address)

    def read8(self, address):
        return self.request(self.P_READ8, address)

    def write64(self, address, value):
        self.request(self.P_WRITE64, address, value)

    def write32(self, address, value):
        self.request(self.P_WRITE32, address, value)

    def write16(self, address, value):
        self.request(self.P_WRITE16, address, value)

    def write8(self, address, value):
        self.request(self.P_WRITE8, address, value)

    def set32(self, address, value):
        return self.request(self.P_SET32, address, value)

    def clear32(self, address, value):
        return self.request(self.P_CLEAR32, address, value)

    def mask32(self, address, clear, set_):
        return self.request(self.P_MASK32, address, clear, set_)

    def writeread32(self, address, value):
        return self.request(self.P_WRITEREAD32, address, value)

    def memcpy8(self, dst, src, size):
        self.request(self.P_MEMCPY8, dst, src, size)

    def memset8(self, dst, value, size):
        self.request(self.P_MEMSET8, dst, value, size)

    def memset32(self, dst, value, size):
        self.request(self.P_MEMSET32, dst, value, size)

    def ic_ivau(self, address, size):
        self.request(self.P_IC_IVAU, address, size)

    def dc_cvau(self, address, size):
        self.request(self.P_DC_CVAU, address, size)

    def flush_code(self, address, size):
        """Make uploaded instructions visible to the target's fetch path."""
        self.dc_cvau(address, size)
        self.ic_ivau(address, size)

    def call(self, address, *args, timeout=None):
        return self.request(self.P_CALL, address, *args, timeout=timeout)

    def udelay(self, microseconds):
        self.request(self.P_UDELAY, microseconds, timeout=10 + microseconds / 1e6)

    def set_exc_guard(self, mode):
        self.request(self.P_SET_EXC_GUARD, mode)

    def get_exc_count(self):
        return self.request(self.P_GET_EXC_COUNT)

    def reboot(self):
        self.request(self.P_REBOOT, no_reply=True)

    @staticmethod
    def stage_configure(data, logo=None, xhci=False):
        """Patch the stage's config word: where it places the emblem, and
        whether it brings up USB1 as an xHCI host on the way in."""
        if logo is None and not xhci:
            return data
        value = STAGE_LOGO[logo] if logo is not None else 0
        if xhci:
            value |= 4          # STAGE_XHCI in platform/uefi/main.c
        return data[:24] + struct.pack('<Q', value) + data[32:]

    @staticmethod
    def stage_header(data):
        """A stage image carries its magic and link address at offset 8."""
        if len(data) < 24:
            raise ProxyError('too short to be a stage image')
        magic, link = struct.unpack('<QQ', data[8:24])
        if magic != STAGE_MAGIC:
            raise ProxyError('not a q1n1 stage image (bad magic)')
        return link

    def pick_stage(self, path):
        """Choose the slot that is not the one currently executing: a stage
        cannot be overwritten while it runs, so builds come in pairs."""
        info = self.bootinfo()
        candidates = [Path(path)]
        name = Path(path).name
        sibling = name[:-8] + '.bin' if name.endswith('-alt.bin') else name[:-4] + '-alt.bin'
        candidates.append(Path(path).with_name(sibling))
        for candidate in candidates:
            if not candidate.exists():
                continue
            data = candidate.read_bytes()
            link = self.stage_header(data)
            if link == info['image_base']:
                continue
            if not (info['stage_base'] <= link < info['stage_base'] + info['stage_size']):
                continue
            return candidate, data, link
        raise ProxyError(f'no stage image for a free slot in {info["stage_base"]:#x}'
                         f'..{info["stage_base"] + info["stage_size"]:#x} (running at {info["image_base"]:#x})')

    def chainload(self, data, base=None):
        """Upload a flat stage and jump to it. The stage inherits this
        bootinfo, re-takes the hardware and bumps stage_generation, so the
        caller can prove the new code is the one answering."""
        info = self.bootinfo()
        self.check_stage_transport(data, info)
        base = base or self.stage_header(data)
        if not base:
            raise ProxyError('the payload reserved no stage region')
        if base == info['image_base']:
            raise ProxyError('that stage is linked for the address it would overwrite')
        if len(data) > info['stage_size']:
            raise ProxyError(f'stage is {len(data)} bytes, region is {info["stage_size"]}')
        self.writemem(base, data)
        if self.readmem(base, len(data)) != data:
            raise ProxyError('stage did not verify after upload')
        self.flush_code(base, len(data))
        # The target replies before it jumps (m1n1 does the same), so consume
        # that reply here or it desynchronises the next request.
        self.request(self.P_VECTOR, base, info['address'])
        return {'base': base, 'bytes': len(data), 'generation_before': info['stage_generation']}

    def check_stage_transport(self, data, info):
        """Refuse a layout change that would reset a configured Mac NCM link."""
        if not info.get('usb_ports'):
            return
        count = info.get('usb_port_count', 0)
        if not 1 <= count <= 2:
            raise ProxyError('invalid USB port table size')
        states = self.readmem(info['usb_ports'], count * 80, live=True)
        if not any(struct.unpack_from('<Q', states, i * 80 + 8)[0] == 1 for i in range(count)):
            return
        if len(data) < 48 or struct.unpack_from('<Q', data, 32)[0] != 0x3154525055533151:
            raise ProxyError('stage has no USB handoff contract; refusing to reset a live NCM link')
        offset = struct.unpack_from('<Q', data, 40)[0] - self.stage_header(data)
        if offset < 48 or offset + 24 > len(data):
            raise ProxyError('stage USB handoff contract is outside the image')
        current = self.readmem(info['usb_ports'] - 24, 24)
        if data[offset:offset + 24] != current:
            raise ProxyError('incompatible USB state layout; use a cold boot of the new EFI image')

    def write_chunk(self):
        """Largest write the transport wants in one go, 0 for no limit.

        Only the host-to-target direction needs this. Reads stream out of the
        target at whatever rate it manages and arrive intact; writes are the
        host pushing datagrams as fast as it can generate them, which outruns a
        receiver that keeps one transfer outstanding at a time.
        """
        return getattr(self.link.transport, 'max_write', 0)

    def readmem(self, address, size, live=False):
        """Read target memory.

        A normal read checksums the range and then sends it, so memory that the
        target changes between the two passes (driver counters, MMIO) reports a
        checksum error. live=True drops the data checksum for that read and
        relies on the end-of-data sentinel instead.
        """
        if not live:
            return self.link.readmem(address, size)
        saved = self.link.features
        try:
            self.link.nop(FEATURE_DISABLE_DATA_CSUMS)
            return self.link.readmem(address, size)
        finally:
            self.link.nop(saved)

    def writemem(self, address, data):
        limit = self.write_chunk()
        if limit and len(data) > limit:
            for offset in range(0, len(data), limit):
                self.link.writemem(address + offset, data[offset:offset + limit])
            return
        self.link.writemem(address, data)

    def acpi_tables(self):
        """Walk RSDP -> XSDT and return {signature: address}."""
        rsdp = self.bootinfo()['acpi_rsdp']
        if not rsdp:
            return {}
        xsdt = struct.unpack('<Q', self.readmem(rsdp + 24, 8))[0]
        length = struct.unpack('<I', self.readmem(xsdt + 4, 4))[0]
        entries = self.readmem(xsdt + 36, max(0, length - 36))
        tables = {}
        for offset in range(0, len(entries) - 7, 8):
            address = struct.unpack('<Q', entries[offset:offset + 8])[0]
            signature = self.readmem(address, 4).decode('ascii', 'replace')
            tables[signature] = address
        return tables


def discover_endpoints(serial=PROXY_SERIAL, interface=PROXY_INTERFACE, network=True):
    endpoints = list(find_ports(serial, interface))
    if network and serial == PROXY_SERIAL and interface == PROXY_INTERFACE:
        import udplink
        endpoints += ['udp://' + name for name in udplink.find_interfaces()]
    return endpoints


def connect(device=None, serial=PROXY_SERIAL, interface=PROXY_INTERFACE, debug=False, wait=0.0):
    """Find and handshake a CDC or USB-NCM endpoint on any Mac/A16 port.

    Failed discovery handshakes are safe to retry. Operations on an established
    connection are never automatically replayed across a disconnect.
    """
    deadline = time.monotonic() + wait
    errors = {}
    while True:
        candidates = [str(device)] if device else discover_endpoints(serial, interface)
        for path in candidates:
            link = transport = None
            try:
                if path.startswith('udp://'):
                    import udplink
                    name = path[6:]
                    transport = udplink.UdpTransport(name, udplink.target_address(name))
                elif not Path(path).exists():
                    continue
                link = Link(None if transport else path, debug=debug, timeout=1.0,
                            transport=transport)
                proxy = Proxy(link)
                link.nop(0)  # Reset negotiation left by a previous client on this cable.
                proxy.nop()
                link.timeout = 5.0
                proxy.endpoint = path
                return proxy
            except (OSError, RuntimeError, TimeoutError) as problem:
                errors[path] = str(problem)
                try:
                    if link is not None:
                        link.close()
                    elif transport is not None:
                        transport.close()
                except OSError:
                    pass
        if time.monotonic() >= deadline:
            details = '; '.join(f'{path}: {error}' for path, error in errors.items())
            raise ProxyError('no responding q1n1 CDC/NCM endpoint' + (f' ({details})' if details else ''))
        time.sleep(0.25)


def hexdump(data, base=0):
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        text = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f'{base + offset:016x}  {chunk.hex(" ", 1):<47}  |{text}|')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--device', help='tty, UNIX socket, or udp://enN (default: discover CDC and USB NCM)')
    parser.add_argument('--serial', default=PROXY_SERIAL)
    parser.add_argument('--interface', type=int, default=PROXY_INTERFACE)
    parser.add_argument('--wait', type=float, default=0.0, help='seconds to wait for the port')
    parser.add_argument('--debug', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='print bootinfo and proxy/USB counters')
    peek = sub.add_parser('peek', help='read a register or memory word')
    peek.add_argument('address')
    peek.add_argument('--width', type=int, default=32, choices=[8, 16, 32, 64])
    poke = sub.add_parser('poke', help='write a register or memory word')
    poke.add_argument('address')
    poke.add_argument('value')
    poke.add_argument('--width', type=int, default=32, choices=[8, 16, 32, 64])
    dump = sub.add_parser('dump', help='hexdump target memory')
    dump.add_argument('address')
    dump.add_argument('size')
    dump.add_argument('--out', type=Path)
    dump.add_argument('--live', action='store_true',
                      help='memory that changes while it is read (counters, MMIO): skip the data checksum')
    load = sub.add_parser('load', help='upload a file into target memory')
    load.add_argument('file', type=Path)
    load.add_argument('address', nargs='?', help='default: bootinfo heap base')
    call = sub.add_parser('call', help='call target code')
    call.add_argument('address')
    call.add_argument('args', nargs='*')
    chain = sub.add_parser('chainload', help='upload a flat stage and run it')
    chain.add_argument('--xhci', action='store_true',
                       help='legacy stage flag; current payloads discover both ports automatically')
    chain.add_argument('file', type=Path)
    chain.add_argument('--base', type=lambda v: int(v, 0))
    chain.add_argument('--settle', type=float, default=6.0)
    chain.add_argument('--logo', choices=sorted(STAGE_LOGO),
                       help='dragon centred (default), corner per generation, '
                            'none, or the upstream asahi mark')
    sub.add_parser('acpi', help='list ACPI tables')
    sub.add_parser('reboot', help='reset the machine (BootNext decides where it lands)')
    sub.add_parser('shell', help='interactive python shell with p (proxy) bound')
    args = parser.parse_args()

    number = lambda text: int(text, 0)
    proxy = connect(args.device, args.serial, args.interface, args.debug, args.wait)
    if args.command == 'info':
        print(f'endpoint               {proxy.endpoint}')
        values = proxy.bootinfo()
        for name in ('address',) + tuple(BOOTINFO_FIELDS):
            print(f'{name:22} {values[name]:#x}')
        if values['usb_ports'] and values['usb_port_count'] <= 2:
            names = 'base role connected attempts disconnects last_error device_stats host_stats ncm_stats proxy_stats'.split()
            roles = {0: 'searching', 1: 'host/NCM', 2: 'device/CDC', 3: 'unsupported'}
            for i in range(values['usb_port_count']):
                record = struct.unpack('<10Q', proxy.readmem(values['usb_ports'] + i * 80, 80, live=True))
                port = dict(zip(names, record))
                print(f"USB{i}: {roles.get(port['role'], 'unknown')} connected={port['connected']} "
                      f"attempts={port['attempts']} disconnects={port['disconnects']} error={port['last_error']:#x}")
        stats = struct.unpack('<12Q', proxy.readmem(values['proxy_stats'], 96))
        names = ('requests proxy_calls memreads memwrites read_bytes write_bytes checksum_errors '
                 'timeouts bad_commands exceptions announcements last_opcode').split()
        print()
        for name, value in zip(names, stats):
            print(f'proxy.{name:20} {value}')
    elif args.command == 'peek':
        reader = {8: proxy.read8, 16: proxy.read16, 32: proxy.read32, 64: proxy.read64}[args.width]
        value = int(reader(number(args.address)) or 0)
        marker = value & 0xFFFFFFFF == GUARD_MARKER & 0xFFFFFFFF
        print(f'{number(args.address):#x}: {value:#0{args.width // 4 + 2}x}' +
              ('   (guard marker: the access faulted)' if marker else ''))
    elif args.command == 'poke':
        writer = {8: proxy.write8, 16: proxy.write16, 32: proxy.write32, 64: proxy.write64}[args.width]
        writer(number(args.address), number(args.value))
        print('written')
    elif args.command == 'dump':
        address, size = number(args.address), number(args.size)
        data = proxy.readmem(address, size, live=args.live)
        if args.out:
            args.out.write_bytes(data)
            print(f'{len(data)} bytes -> {args.out}')
        else:
            hexdump(data, address)
    elif args.command == 'load':
        data = args.file.read_bytes()
        address = number(args.address) if args.address else proxy.bootinfo()['heap_base']
        start = time.monotonic()
        proxy.writemem(address, data)
        back = proxy.readmem(address, len(data))
        elapsed = time.monotonic() - start
        print(f'{len(data)} bytes at {address:#x} in {elapsed:.2f} s '
              f'({len(data) / elapsed / 1024:.0f} KiB/s), verify: {"ok" if back == data else "MISMATCH"}')
    elif args.command == 'call':
        value = proxy.call(number(args.address), *[number(a) for a in args.args])
        print(f'returned {value:#x}')
    elif args.command == 'chainload':
        chosen, data, link = proxy.pick_stage(args.file)
        data = proxy.stage_configure(data, args.logo, getattr(args, 'xhci', False))
        if chosen != args.file:
            print(f'running stage occupies that slot; using {chosen.name} (linked {link:#x})')
        start = time.monotonic()
        report = proxy.chainload(data, args.base)
        print(f'uploaded {report["bytes"]} bytes to {report["base"]:#x} in {time.monotonic() - start:.2f} s; '
              f'jumping (generation was {report["generation_before"]})')
        proxy.link.close()
        time.sleep(args.settle)
        fresh = connect(args.device, args.serial, args.interface, args.debug, wait=60)
        info = fresh.bootinfo()
        fresh.link.close()
        if info['image_base'] != link or info['stage_generation'] <= report['generation_before']:
            raise SystemExit(f'chainload did not take over: still generation {info["stage_generation"]} '
                             f'at {info["image_base"]:#x}; inspect the target handoff status')
        print(f'stage answering: generation {info["stage_generation"]}, image {info["image_base"]:#x}, '
              f'EL{info["current_el"]}, heap {info["heap_base"]:#x}')
    elif args.command == 'acpi':
        for signature, address in sorted(proxy.acpi_tables().items()):
            print(f'{signature}  {address:#x}')
    elif args.command == 'reboot':
        proxy.reboot()
        print('reset requested')
    elif args.command == 'shell':
        import code
        banner = 'q1n1 proxy shell: p = proxy, p.read32(...), p.bootinfo(), p.reboot()'
        code.interact(banner=banner, local={'p': proxy, 'proxy': proxy, 'struct': struct})
    return 0


if __name__ == '__main__':
    sys.exit(main())
