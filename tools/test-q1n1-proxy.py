#!/usr/bin/env python3
"""Drive the real q1n1 proxy loop (native harness over a pty) with the real client.

Covers framing, checksums, resynchronisation, every implemented opcode, and
bulk transfers larger than the target's 64 KiB bounce buffer. The AArch64
exception guards are not exercised here; tools/test-uefi.py --proxy does that.
"""
import importlib.util
from pathlib import Path
import random
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('q1n1proxy', ROOT / 'tools' / 'q1n1proxy.py')
assert spec and spec.loader
q1n1proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q1n1proxy)

checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


def main():
    global checks
    binary = ROOT / 'build' / 'test-q1n1-proxy'
    build = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror', '-DQ1N1_PROXY_HOST_TEST', f'-I{ROOT}/platform/uefi',
             str(ROOT / 'tools' / 'test-q1n1-proxy.c'), str(ROOT / 'platform' / 'uefi' / 'q1n1-proxy.c'),
             '-o', str(binary)]
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(build, check=True)

    target = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, text=True)
    assert target.stdout
    tty = target.stdout.readline().strip()
    scratch, adder, _info = (int(v, 16) for v in target.stdout.readline().split())
    try:
        link = q1n1proxy.Link(tty)
        proxy = q1n1proxy.Proxy(link)

        check(link.nop(0) == 0, 'features cleared')
        check(link.nop(q1n1proxy.FEATURE_DISABLE_DATA_CSUMS) == 1, 'data checksums can be disabled')
        check(link.nop(0) == 0, 'data checksums back on')

        info = proxy.bootinfo()
        check(info['magic'] == q1n1proxy.MAGIC, 'bootinfo magic')
        check(info['heap_base'] == scratch, 'bootinfo heap base')
        check(proxy.get_base() == 0x140000000, 'image base')

        proxy.write64(scratch, 0x0123456789ABCDEF)
        check(proxy.read64(scratch) == 0x0123456789ABCDEF, 'write64/read64')
        check(proxy.read32(scratch) == 0x89ABCDEF, 'read32 low half')
        check(proxy.read16(scratch) == 0xCDEF, 'read16')
        check(proxy.read8(scratch) == 0xEF, 'read8')
        proxy.write32(scratch + 4, 0xDEADBEEF)
        check(proxy.read32(scratch + 4) == 0xDEADBEEF, 'write32')
        proxy.write16(scratch + 8, 0x1234)
        check(proxy.read16(scratch + 8) == 0x1234, 'write16')
        proxy.write8(scratch + 10, 0x5A)
        check(proxy.read8(scratch + 10) == 0x5A, 'write8')

        proxy.write32(scratch, 0xF0F0F0F0)
        check(proxy.set32(scratch, 0x0F00) == 0xF0F0FFF0, 'set32 returns the new value')
        check(proxy.clear32(scratch, 0xF000) == 0xF0F00FF0, 'clear32')
        check(proxy.mask32(scratch, 0xFFFF, 0x1234) == 0xF0F01234, 'mask32')
        check(proxy.writeread32(scratch, 0xA5A5A5A5) == 0xA5A5A5A5, 'writeread32')

        proxy.memset32(scratch + 0x100, 0x11223344, 0x40)
        check(proxy.readmem(scratch + 0x100, 8) == bytes.fromhex('44332211' * 2), 'memset32')
        proxy.memcpy8(scratch + 0x200, scratch + 0x100, 0x40)
        check(proxy.readmem(scratch + 0x200, 0x40) == proxy.readmem(scratch + 0x100, 0x40), 'memcpy8')
        proxy.memset8(scratch + 0x200, 0, 0x40)
        check(proxy.readmem(scratch + 0x200, 0x40) == bytes(0x40), 'memset8')

        check(proxy.call(adder, 1, 2, 3, 4, 5) == 15, 'P_CALL with five arguments')
        start = time.monotonic()
        proxy.udelay(20000)
        check(time.monotonic() - start >= 0.015, 'P_UDELAY waits')
        check(proxy.get_exc_count() == 0, 'no exceptions on this host')

        # Bulk transfers across the target's 64 KiB bounce buffer, both modes.
        payload = random.randbytes(256 * 1024)
        for features in (0, q1n1proxy.FEATURE_DISABLE_DATA_CSUMS):
            link.nop(features)
            proxy.writemem(scratch + 0x1000, payload)
            check(proxy.readmem(scratch + 0x1000, len(payload)) == payload,
                  f'256 KiB round trip (features {features})')

        # A corrupted request must be rejected, and the stream must resynchronise.
        link.nop(0)
        message = struct.pack('<I', q1n1proxy.REQ_PROXY) + struct.pack('<7Q', proxy.P_NOP, 0, 0, 0, 0, 0, 0)
        link.transport.write(message + struct.pack('<I', 0xDEADBEEF))
        try:
            link.reply(q1n1proxy.REQ_PROXY)
            check(False, 'checksum error reported')
        except q1n1proxy.ProxyRemoteError:
            check(True, 'rejected as expected')
        link.transport.write(b'garbage bytes before a command\xff\x55')
        check(proxy.nop() == 0, 'resync after garbage')

        try:
            proxy.request(0xDEAD)
            check(False, 'unknown opcode rejected')
        except q1n1proxy.ProxyRemoteError:
            check(True, 'rejected as expected')
        check(proxy.nop() == 0, 'still alive after a bad opcode')

        stats = struct.unpack('<12Q', proxy.readmem(info['proxy_stats'], 96))
        check(stats[0] > 20 and stats[6] == 1 and stats[8] == 1,
              f'target counters (requests {stats[0]}, checksum errors {stats[6]}, bad commands {stats[8]})')

        proxy.reboot()
        check(target.wait(timeout=10) == 0, 'P_REBOOT reaches the platform hook')
    finally:
        if target.poll() is None:
            target.kill()
            target.wait()
    print(f'PASS: {checks} q1n1 proxy protocol checks (native harness, sanitizers on)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
