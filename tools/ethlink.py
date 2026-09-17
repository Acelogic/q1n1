#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Raw Ethernet transport over macOS BPF, for the q1n1 proxy on the NCM link.

The proxy protocol is a byte stream with its own framing, checksums and
resynchronisation, so it only needs a datagram pipe. Frames carry ethertype
0x88B5 -- IEEE 802's local experimental type -- which keeps q1n1 free of an IP
stack entirely.

Presents the same read(count, timeout) / write(data) surface as
q1n1proxy.Transport, so q1n1proxy.Link drives it unchanged.

Needs root: BPF is a raw link-layer device.
"""
import fcntl
import os
import select
import struct
import sys
import time

ETHERTYPE = 0x88B5
BPF_HEADER_CAPLEN = 8
BPF_MAX_DEVICES = 256


def _ioc(direction, group, number, length):
    return direction | ((length & 0x1FFF) << 16) | (ord(group) << 8) | number


_IOC_OUT, _IOC_IN = 0x40000000, 0x80000000
BIOCGBLEN = _ioc(_IOC_OUT, 'B', 102, 4)
BIOCSETIF = _ioc(_IOC_IN, 'B', 108, 32)       # struct ifreq
BIOCIMMEDIATE = _ioc(_IOC_IN, 'B', 112, 4)
BIOCSHDRCMPLT = _ioc(_IOC_IN, 'B', 117, 4)
BIOCSSEESENT = _ioc(_IOC_IN, 'B', 119, 4)


class EthError(RuntimeError):
    pass


def interface_mac(name):
    """The MAC of a local interface, read from ifconfig."""
    import subprocess
    out = subprocess.run(['ifconfig', name], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == 'ether':
            return bytes.fromhex(parts[1].replace(':', ''))
    raise EthError(f'no MAC address on {name}')


class EthTransport:
    """A byte stream carried in Ethernet frames on one interface."""

    def __init__(self, interface, peer=None, ethertype=ETHERTYPE):
        self.interface = interface
        self.ethertype = ethertype
        self.peer = peer                      # learned from the first frame if None
        self.source = interface_mac(interface)
        self.buffer = bytearray()
        self.fd = None
        self.frames_in = self.frames_out = 0

        for index in range(BPF_MAX_DEVICES):
            try:
                self.fd = os.open(f'/dev/bpf{index}', os.O_RDWR)
                break
            except OSError:
                continue
        if self.fd is None:
            raise EthError('no free /dev/bpf device (run as root?)')

        try:
            fcntl.ioctl(self.fd, BIOCSETIF, struct.pack('16s16x', interface.encode()))
            fcntl.ioctl(self.fd, BIOCIMMEDIATE, struct.pack('I', 1))
            fcntl.ioctl(self.fd, BIOCSHDRCMPLT, struct.pack('I', 1))
            # Without this every frame we send comes straight back to us.
            fcntl.ioctl(self.fd, BIOCSSEESENT, struct.pack('I', 0))
            size = struct.unpack('I', fcntl.ioctl(self.fd, BIOCGBLEN,
                                                  struct.pack('I', 0)))[0]
            self.read_size = size
        except OSError as error:
            os.close(self.fd)
            raise EthError(f'could not configure BPF on {interface}: {error}')
        os.set_blocking(self.fd, False)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _harvest(self):
        """Drain BPF and append the payloads of our frames to the buffer."""
        try:
            block = os.read(self.fd, self.read_size)
        except BlockingIOError:
            return 0
        except OSError:
            return 0
        offset, got = 0, 0
        while offset + 18 <= len(block):
            caplen, datalen, hdrlen = struct.unpack_from('<IIH', block, offset + BPF_HEADER_CAPLEN)
            if hdrlen < 18 or offset + hdrlen + caplen > len(block):
                break
            frame = block[offset + hdrlen:offset + hdrlen + caplen]
            offset += (hdrlen + caplen + 3) & ~3
            if len(frame) < 14:
                continue
            if struct.unpack('>H', frame[12:14])[0] != self.ethertype:
                continue
            if self.peer is None:
                self.peer = bytes(frame[6:12])
            self.buffer += frame[14:]
            self.frames_in += 1
            got += 1
        return got

    def read(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'expected {count} bytes, got {len(self.buffer)}')
            if not self._harvest():
                select.select([self.fd], [], [], min(remaining, 0.05))
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def write(self, data):
        if self.peer is None:
            raise EthError('no peer address yet: nothing has been received')
        view = memoryview(data)
        while view:
            chunk, view = view[:1400], view[1400:]
            frame = self.peer + self.source + struct.pack('>H', self.ethertype) + bytes(chunk)
            if len(frame) < 60:
                frame += b'\0' * (60 - len(frame))
            os.write(self.fd, frame)
            self.frames_out += 1

    def discover(self, timeout=10.0):
        """Wait for the target to announce itself, learning its MAC."""
        deadline = time.monotonic() + timeout
        while self.peer is None and time.monotonic() < deadline:
            if not self._harvest():
                select.select([self.fd], [], [], 0.05)
        return self.peer


def main():
    """Sniff the link so the framing can be checked without the proxy."""
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <interface> [seconds]', file=sys.stderr)
        return 2
    transport = EthTransport(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    print(f'listening on {sys.argv[1]} for ethertype {ETHERTYPE:#06x}, '
          f'source {":".join(f"{b:02x}" for b in transport.source)}')
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        transport._harvest()
        if transport.buffer:
            print(f'{len(transport.buffer)} bytes buffered, '
                  f'{transport.frames_in} frames, peer '
                  f'{":".join(f"{b:02x}" for b in transport.peer)}')
            print('  ' + bytes(transport.buffer[:64]).hex())
            del transport.buffer[:]
        time.sleep(0.05)
    transport.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
