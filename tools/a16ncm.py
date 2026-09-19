#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Speak q1n1 over USB NCM using unprivileged IPv6 UDP.

Current payloads serve both ports automatically. The legacy serve command is
retained for older payloads that require an explicit transport handoff.
"""
import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import q1n1proxy as q1n1                                    # noqa: E402
import udplink                                              # noqa: E402

LLVM_NM = '/opt/homebrew/opt/llvm/bin/llvm-nm'


def symbols(elf):
    """Map symbol name to address for a stage image."""
    out = subprocess.run([LLVM_NM, str(elf)], capture_output=True, text=True, check=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            table[parts[2]] = int(parts[0], 16)
    return table


def stage_for(proxy):
    """Pick the ELF matching the slot the target is actually running."""
    base = proxy.bootinfo()['image_base']
    for name in ('hardware', 'hardware-alt'):
        elf = ROOT / 'build' / 'uefi' / 'stage' / f'{name}.elf'
        if not elf.exists():
            continue
        table = symbols(elf)
        entry = table.get('q1n1_ncm_proxy_run')
        if entry and (entry & ~0xFFFFF) == (base & ~0xFFFFF):
            return table
    raise SystemExit(f'no stage ELF matches the running image at {base:#x}; rebuild first')


def cmd_serve(args):
    proxy = q1n1.connect(device=args.device)
    info = proxy.bootinfo()
    if info.get('usb_ports'):
        proxy.link.close()
        print('Both ports are already served automatically; use talk or q1n1proxy.py.')
        return 0
    table = stage_for(proxy)
    print(f"generation {info['stage_generation']}, image {info['image_base']:#x}")

    if args.retry:
        status = proxy.request(proxy.P_CALL, table['q1n1_xhci_retry'])
        print(f'q1n1_xhci_retry -> {status}')
        if status:
            return 1

    print(f'handing the proxy to the NCM link for up to {args.seconds:.0f}s of quiet...')
    started = time.monotonic()
    frames = proxy.request(proxy.P_CALL, table['q1n1_ncm_proxy_run'], int(args.seconds),
                           timeout=args.seconds + 120)
    print(f'console has it back after {time.monotonic() - started:.1f}s; '
          f'{frames} frames were received over Ethernet')
    return 0


def cmd_talk(args):
    if not args.interface:
        names = udplink.find_interfaces()
        if not names:
            raise SystemExit('no active Apple USB NCM interface')
        for name in names:
            try:
                probe = q1n1.connect(device='udp://' + name, wait=1)
                probe.link.close()
                args.interface = name
                break
            except (OSError, q1n1.ProxyError):
                continue
        if not args.interface:
            raise SystemExit('no responding q1n1 USB NCM endpoint')
    mac = bytes.fromhex(args.mac.replace(':', '')) if args.mac else None
    address = udplink.target_address(args.interface, mac)
    transport = udplink.UdpTransport(args.interface, address)
    link = q1n1.Link(None, debug=args.debug, transport=transport)
    proxy = q1n1.Proxy(link)

    # The target learns our address from the first datagram we send, so we
    # speak first; its own `ready` stays false until then.
    print(f'probing [{address}%{args.interface}]:{udplink.PORT} ...')
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        try:
            proxy.nop()
            break
        except Exception:
            transport.buffer.clear()
            time.sleep(0.3)
    else:
        print('no answer over UDP', file=sys.stderr)
        return 1
    # Probing retries in a loop, so replies to the attempts that timed out can
    # still be in flight. Anything left now is stale and would be read as the
    # answer to the next request.
    time.sleep(0.2)
    transport._harvest()
    transport.buffer.clear()

    print(f'proxy answering over UDP from {address}')

    info = proxy.bootinfo()
    print(f"  generation {info['stage_generation']}  EL{info['current_el']}  "
          f"image {info['image_base']:#x}  heap {info['heap_base']:#x}")

    value = proxy.read32(info['image_base'])
    print(f'  read32({info["image_base"]:#x}) = {value:#010x}')

    payload = bytes(range(256)) * 16                        # 4 KiB round trip
    scratch = info['heap_base'] + 0x200000
    started = time.monotonic()
    proxy.writemem(scratch, payload)
    back = proxy.readmem(scratch, len(payload))
    elapsed = time.monotonic() - started
    print(f'  {len(payload)} byte round trip in {elapsed * 1000:.0f} ms: '
          f'{"MATCH" if back == payload else "MISMATCH"}')

    started, count = time.monotonic(), 0
    while time.monotonic() - started < args.seconds:
        proxy.nop()
        count += 1
    rate = count / (time.monotonic() - started)
    print(f'  {count} round trips in {args.seconds:.0f}s ({rate:.0f}/s)')
    print(f'  datagrams: {transport.frames_in} in, {transport.frames_out} out')
    transport.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--debug', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve', help='hand the proxy to the NCM link (over the console)')
    serve.add_argument('--device', help='console serial device')
    serve.add_argument('--seconds', type=float, default=45.0,
                       help='idle time after which the console takes over again')
    serve.add_argument('--retry', action='store_true',
                       help='bring the NCM link up first (after a replug)')

    talk = sub.add_parser('talk', help='speak the proxy protocol over raw Ethernet')
    talk.add_argument('--interface', help="the Mac's NCM interface (default: discover active USB NCM ports)")
    talk.add_argument('--seconds', type=float, default=3.0, help='round-trip benchmark length')
    talk.add_argument('--wait', type=float, default=20.0, help='seconds to wait for an answer')
    talk.add_argument('--mac', help="the MAC the NCM descriptor assigned this host "
                                    "(default: the interface's own)")

    args = parser.parse_args()
    return {'serve': cmd_serve, 'talk': cmd_talk}[args.command](args)


if __name__ == '__main__':
    sys.exit(main() or 0)
