#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Drive the real q1n1 proxy loop (native harness over a pty) with the real client.

Covers framing, checksums, resynchronisation, every implemented opcode, and
bulk transfers larger than the target's 64 KiB bounce buffer. The AArch64
exception guards are not exercised here; tools/test-uefi.py --proxy does that.
The SError bracketing is: the harness models a pending SError with a countdown
of platform polls, so each case places one before or during a chosen request,
edge-like (consumed when found) or level-sensitive (found again on every poll).
The deliberate A-unmasked window in main.c cannot be triggered in QEMU (it
raises no SErrors), so the harness binary also runs a model of that window
(`--window`) against the payload's own q1n1_serror_window_spsr().
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


def serror_checks(proxy, scratch, schedule):
    """P_Q1N1_SERROR and the before/during bracketing, in both modes.

    A P_CALL is itself bracketed: the countdown is armed inside it, so its own
    'during' poll takes one. countdown 2 therefore lands on the next target
    request's 'before' poll, and 3 on its 'during' poll."""
    remote_error = q1n1proxy.ProxyRemoteError

    def arm(polls, syndrome, level=0):
        proxy.call(schedule, polls, syndrome, level)

    def expect_failure(action, status, label):
        try:
            action()
        except remote_error as problem:
            check(problem.status == status, f'{label}: status {problem.status}, want {status}')
            return
        check(False, f'{label}: request failed')

    state = proxy.serror_state()
    check(state['magic'] == q1n1proxy.SERROR_MAGIC and state['mechanism'] == 2 and state['flags'] == 0,
          'SError record: magic, RAS mechanism, not strict by default')
    check(state['checks'] > 20 and state['before'] == state['during'] == 0,
          f'every earlier target access was bracketed ({state["checks"]} checks, none found)')
    proxy.serror_reset()
    check(proxy.serror_state()['checks'] == 0, 'reset zeroes the counters (reading the record is not bracketed)')

    # Default mode: counted, attributed, and the request still succeeds.
    proxy.write32(scratch, 0x11111111)
    arm(3, 0x80000011)
    check(proxy.read32(scratch) == 0x11111111, 'non-strict: a read that raised an SError still returns')
    state = proxy.serror_state()
    check(state['during'] == 1 and state['before'] == 0 and state['last_when'] == 2,
          'non-strict: the SError is attributed to the access (during)')
    check(state['last_opcode'] == proxy.P_READ32 and state['last_address'] == scratch
          and state['last_syndrome'] == 0x80000011, 'record names the request, address and syndrome')

    proxy.serror_strict(True)
    check(proxy.serror_state()['flags'] == 1, 'strict mode set')

    arm(3, 0x80000022)
    expect_failure(lambda: proxy.write32(scratch, 0x22222222), q1n1proxy.S_SERROR,
                   'strict: write that raised an SError fails')
    check(proxy.read32(scratch) == 0x22222222, 'strict: that write did reach memory (during, not refused)')

    arm(2, 0x80000033)
    expect_failure(lambda: proxy.write32(scratch, 0x33333333), q1n1proxy.S_SERROR,
                   'strict: write with an SError already pending fails')
    check(proxy.read32(scratch) == 0x22222222, 'strict: and it was not performed')
    state = proxy.serror_state()
    check(state['before'] == 1 and state['refused'] == 1 and state['last_when'] == 1
          and state['last_opcode'] == proxy.P_WRITE32, 'strict: counted as before and refused')

    # Requests that touch no target memory do not poll: the SError waits for
    # the next one that does.
    arm(2, 0x80000044)
    proxy.nop()
    proxy.get_exc_count()
    expect_failure(lambda: proxy.write32(scratch, 0x44444444), q1n1proxy.S_SERROR,
                   'strict: NOP/GET_EXC_COUNT leave a pending SError for the next access')
    check(proxy.read32(scratch) == 0x22222222, 'strict: that access was refused')

    # Memory transfers fail with ST_XFRERR and the stream stays in step.
    arm(2, 0x80000055)
    expect_failure(lambda: proxy.readmem(scratch, 64), q1n1proxy.ST_XFRERR, 'strict: memread refused')
    check(proxy.nop() == 0, 'stream in step after a refused memread')
    arm(3, 0x80000066)
    expect_failure(lambda: proxy.readmem(scratch, 64), q1n1proxy.ST_XFRERR, 'strict: memread that raised one fails')
    check(proxy.nop() == 0, 'stream in step after a failed memread')
    before = proxy.readmem(scratch + 0x400, 0x3000)
    block = random.randbytes(0x3000)
    arm(2, 0x80000077)
    expect_failure(lambda: proxy.writemem(scratch + 0x400, block), q1n1proxy.ST_XFRERR, 'strict: memwrite refused')
    check(proxy.readmem(scratch + 0x400, 0x3000) == before, 'strict: refused memwrite wrote nothing')
    arm(3, 0x80000088)
    expect_failure(lambda: proxy.writemem(scratch + 0x400, block), q1n1proxy.ST_XFRERR,
                   'strict: memwrite that raised one fails')
    check(proxy.readmem(scratch + 0x400, 0x3000) == block, 'strict: its data had been written')
    state = proxy.serror_state()
    check(state['before'] == 4 and state['during'] == 4 and state['refused'] == 4,
          f'strict: totals before {state["before"]} during {state["during"]} refused {state["refused"]}')

    # A level-sensitive SError: the source stays asserted, so every bracketed
    # access finds it again and strict mode refuses all of them. Reading the
    # record is not bracketed: it is never refused and changes nothing, so the
    # host still learns which access raised it.
    proxy.write32(scratch, 0x55555555)
    arm(3, 0x800000AA, level=1)
    expect_failure(lambda: proxy.write32(scratch, 0x66666666), q1n1proxy.S_SERROR,
                   'strict, level SError: the write that raised it fails')
    state = proxy.serror_state()
    check(state['last_opcode'] == proxy.P_WRITE32 and state['last_address'] == scratch
          and state['last_when'] == 2 and state['last_syndrome'] == 0x800000AA,
          'strict, level SError: the record reads back and names the write, not the record read')
    check(state['unconsumed'] >= 1, 'strict, level SError: reported still pending (unconsumed)')
    check(proxy.serror_state() == state, 'reading the record twice changes nothing in it')
    record = len(q1n1proxy.SERROR_FIELDS) * 8
    check(proxy.readmem(state['address'] + 8, 16) == proxy.readmem(state['address'], record)[8:24],
          'a read inside the record is served too')
    expect_failure(lambda: proxy.readmem(state['address'], record + 8), q1n1proxy.ST_XFRERR,
                   'strict, level SError: a read running past the record is bracketed and refused')
    expect_failure(lambda: proxy.readmem(scratch, 64), q1n1proxy.ST_XFRERR,
                   'strict, level SError: other memreads refused')
    expect_failure(lambda: proxy.read32(scratch), q1n1proxy.S_SERROR, 'strict, level SError: register reads refused')
    after = proxy.serror_state()
    check(after['refused'] == state['refused'] + 3 and after['before'] == state['before'] + 3,
          'strict, level SError: each refusal counted as before')
    proxy.serror_strict(False)
    arm(0, 0)
    check(proxy.read32(scratch) == 0x66666666, 'source released: accesses work again, the write had landed')

    # REVIEW-2: an SError that stays pending although the poll could neither
    # defer nor take it is still refused in strict mode, and counted unconsumed.
    proxy.write32(scratch, 0x77777777)
    proxy.serror_reset()
    proxy.serror_strict(True)
    arm(2, 0x800000BB, level=2)
    expect_failure(lambda: proxy.write32(scratch, 0x88888888), q1n1proxy.S_SERROR,
                   'strict, unconsumable SError: the next access is refused')
    state = proxy.serror_state()
    check(state['unconsumed'] >= 1 and state['refused'] == 1 and state['before'] == 0,
          f'strict, unconsumable SError: refused and counted unconsumed ({state["unconsumed"]})')
    proxy.serror_strict(False)
    arm(0, 0)
    check(proxy.read32(scratch) == 0x77777777, 'strict, unconsumable SError: the refused write did not land')

    proxy.serror_strict(False)
    expect_failure(lambda: proxy.request(proxy.P_Q1N1_SERROR, 7), -1, 'unknown SError command rejected')
    check(proxy.serror_state()['flags'] == 0, 'strict mode cleared')


def window_checks(binary):
    """The SError vector's return path inside the deliberate window (main.c)."""
    source = (ROOT / 'platform' / 'uefi' / 'main.c').read_text()
    start = source.index('if ((vector & 3) == 3 && serror_window)')
    handler = source[start:source.index('return;', start)]
    check('spsr = q1n1_serror_window_spsr(spsr);' in handler and 'msr spsr_el2, %0' in handler
          and 'msr spsr_el1, %0' in handler, 'main.c: the window handler returns through q1n1_serror_window_spsr')
    result = subprocess.run([str(binary), '--window'], capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    for line in lines[:-1]:
        check(False, f'window model: {line}')
    passed, failures = (int(v) for v in lines[-1].split()[1:])
    check(result.returncode == 0 and failures == 0 and passed == 6, f'window model: {lines[-1]}')
    global checks
    checks += passed - 1


def main():
    global checks
    binary = ROOT / 'build' / 'test-q1n1-proxy'
    build = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror', '-DQ1N1_PROXY_HOST_TEST', f'-I{ROOT}/platform/uefi',
             str(ROOT / 'tools' / 'test-q1n1-proxy.c'), str(ROOT / 'platform' / 'uefi' / 'q1n1-proxy.c'),
             '-o', str(binary)]
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(build, check=True)
    window_checks(binary)

    target = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, text=True)
    assert target.stdout
    tty = target.stdout.readline().strip()
    scratch, adder, _info = (int(v, 16) for v in target.stdout.readline().split())
    schedule = int(target.stdout.readline(), 16)
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
        check(proxy.fb_console(False) == 1, 'panel ownership yields from enabled state')
        check(proxy.fb_console(False) == 0, 'panel ownership stays yielded across requests')
        check(proxy.fb_console(True) == 0, 'panel console can be restored explicitly')
        check(proxy.fb_console(True) == 1, 'restored panel state is retained')

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

        serror_checks(proxy, scratch, schedule)

        try:
            proxy.request(proxy.P_FB_SHUTDOWN, 1)
            check(False, 'unsupported logo restoration rejected')
        except q1n1proxy.ProxyRemoteError:
            check(True, 'unsupported logo restoration rejected')
        check(proxy.fb_console(False) == 1, 'rejected option preserved panel ownership')

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
