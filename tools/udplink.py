#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""UDP-over-IPv6 transport for the q1n1 proxy on the NCM link.

q1n1 answers neighbour solicitations and speaks UDP itself, so this side is an
ordinary unprivileged socket -- no BPF, no root. The target's address is the
link-local EUI-64 of the MAC its NCM function assigned the host, which means it
can be computed here rather than configured.

Presents the same read(count, timeout) / write(data) surface as
q1n1proxy.Transport, so q1n1proxy.Link drives it unchanged.
"""
import socket
import struct
import subprocess
import sys
import time

PORT = 4919


class UdpError(RuntimeError):
    pass


def link_local(mac):
    """fe80:: with the EUI-64 of a MAC, the usual derivation."""
    eui = bytes([mac[0] ^ 0x02]) + mac[1:3] + b'\xff\xfe' + mac[3:6]
    return socket.inet_ntop(socket.AF_INET6, b'\xfe\x80' + b'\0' * 6 + eui)


def find_interface(prefix=b'\x12\x77\x60'):
    """The NCM interface, by the MAC prefix the Mac's device mode uses."""
    names = subprocess.run(['ifconfig', '-l'], capture_output=True, text=True).stdout.split()
    best = None
    for name in names:
        if not name.startswith('en'):
            continue
        text = subprocess.run(['ifconfig', name], capture_output=True, text=True).stdout
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0] == 'ether' and bytes.fromhex(parts[1].replace(':', '')).startswith(prefix):
                if 'status: active' in text:
                    return name
                best = best or name
    return best


def interface_mac(name):
    out = subprocess.run(['ifconfig', name], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == 'ether':
            return bytes.fromhex(parts[1].replace(':', ''))
    raise UdpError(f'no MAC address on {name}')


FIXED = 'fe80::4919'


def target_address(interface, host_mac=None):
    """Where q1n1 will be listening.

    q1n1 answers on two addresses: one derived from the MAC in the function's
    Ethernet descriptor, and the fixed `fe80::4919`. The fixed one is the
    default because the descriptor MAC is not visible from here.

    It is tempting to derive the address from the local interface's MAC, since
    those two values are often equal -- this used to do exactly that. They are
    not the same thing, and when they diverge the failure is silent: the
    solicitation goes unanswered and a working target looks dead. Pass
    `host_mac` to derive it anyway, but only with a MAC read off the target.
    """
    return link_local(host_mac) if host_mac else FIXED


class UdpTransport:
    """A byte stream carried in UDP datagrams to one link-local peer."""

    def __init__(self, interface, address, port=PORT):
        self.interface = interface
        self.address = address
        self.port = port
        self.scope = socket.if_nametoindex(interface)
        self.buffer = bytearray()
        self.frames_in = self.frames_out = 0
        # Host-to-target only. Reads stream back intact at any size (4 MiB
        # measured clean at ~9 MB/s), but a write is this side pushing datagrams
        # as fast as Python can build them while the target keeps a single NCM
        # transfer outstanding: whenever it is parsing a block, the device has
        # nowhere to put the next one. A single 32 KiB write survives, but
        # sustained ones need 4 KiB, which still gives ~3 MB/s. Giving the
        # driver a queue of receive TRBs is what would lift this.
        self.max_write = 4 << 10
        self.socket = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        # A bulk read arrives as an unpaced burst of 1400-byte datagrams and UDP
        # will not retransmit what the socket buffer could not hold, so ask for
        # as much room as the kernel will give.
        for size in (8 << 20, 4 << 20, 2 << 20, 1 << 20, 256 << 10):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
                break
            except OSError:
                continue
        self.rcvbuf = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self.socket.bind(('::', 0))
        self.socket.setblocking(False)
        self.peer = (address, port, 0, self.scope)

    def close(self):
        self.socket.close()

    def _harvest(self):
        got = 0
        while True:
            try:
                data, _ = self.socket.recvfrom(65535)
            except BlockingIOError:
                return got
            except OSError:
                return got
            if not data:
                return got
            self.buffer += data
            self.frames_in += 1
            got += 1

    def read(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'expected {count} bytes, got {len(self.buffer)}')
            if not self._harvest():
                import select
                select.select([self.socket], [], [], min(remaining, 0.05))
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def write(self, data):
        view = memoryview(data)
        while view:
            chunk, view = view[:1400], view[1400:]
            self.socket.sendto(bytes(chunk), self.peer)
            self.frames_out += 1


def main():
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <interface> [host-mac]', file=sys.stderr)
        return 2
    interface = sys.argv[1]
    mac = bytes.fromhex(sys.argv[2].replace(':', '')) if len(sys.argv) > 2 else None
    address = target_address(interface, mac)
    print(f'{interface}: target should be at [{address}%{interface}]:{PORT}')
    transport = UdpTransport(interface, address)
    transport.write(b'\0' * 4)
    print('probe sent; anything coming back will appear below')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if transport._harvest():
            print(f'  {len(transport.buffer)} bytes: {bytes(transport.buffer[:48]).hex()}')
            del transport.buffer[:]
        time.sleep(0.05)
    transport.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
