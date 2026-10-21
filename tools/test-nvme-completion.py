#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""nvme_m3's completion poll against the MEMREAD two-pass race (no hardware).

A checksummed MEMREAD checksums the range and then sends it. A completion the
controller writes by DMA between the two passes reports a checksum error, and
a guarded session freezes on it (Snapintosh installed-fresh-20261019-v75,
docs/INSTALLED-FRESH-ADMISSION-20261016.md there). The fake target below models
that race for checksummed reads, and torn entries for live reads, under the
production Hw and Nvme._reap:

1. the v75 race: a completion lands during a poll read. The pre-change poll
   (`--preimage`) fails with the proxy's checksum error; the live poll reaps it;
2. a torn live read (new phase, stale identifier and status) is not used; the
   entry is taken once two consecutive reads agree;
3. an entry that never reads the same twice refuses;
4. a failed status still refuses, and the doorbell gets the new head;
5. every completion-queue read is live; no other read is.

    python3 tools/test-nvme-completion.py [--preimage OLD_NVME_M3.py]
"""
import argparse
import importlib.util
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import q1n1proxy  # noqa: E402

BAR, CQ, ENTRIES = 0x10000000, 0x20000000, 4
checks = 0


def expect(ok, what):
    global checks
    checks += 1
    if not ok:
        raise SystemExit(f'FAIL: {what}')


def entry(cid, status=0, phase=1, dw0=0):
    return struct.pack('<4I', dw0, 0, 0, cid | phase << 16 | status << 17)


class Target:
    """Completion-queue memory, a DMA write landing at a chosen read, and torn live reads."""
    P_DC_CIVAC = 0x40

    def __init__(self, land_at=None, landed=b'', torn=(), unstable=False):
        self.mem = bytearray(16 * ENTRIES)
        self.land_at, self.landed, self.torn, self.unstable = land_at, landed, list(torn), unstable
        self.reads, self.doorbells = [], []

    def request(self, opcode, *args):
        assert opcode == self.P_DC_CIVAC

    def write32(self, address, value):
        self.doorbells.append((address, value))

    def readmem(self, address, size, live=False):
        assert CQ <= address and address + size <= CQ + len(self.mem)
        at = address - CQ
        self.reads.append((address, live))
        first = bytes(self.mem[at:at + size])          # the checksum pass
        if self.land_at is not None and len(self.reads) == self.land_at:
            self.mem[0:16] = self.landed                # the controller's DMA write
        second = bytes(self.mem[at:at + size])         # the data pass
        if not live:
            if first != second:
                raise q1n1proxy.ProxyError(f'read data checksum error at {address:#x} after 1 attempts')
            return second
        if self.torn and second[12:16] != bytes(4):
            return self.torn.pop(0)
        if self.unstable and second[12:16] != bytes(4):
            return struct.pack('<I', len(self.reads)) + second[4:]
        return second


def reap(module, target, cids, seconds=0.5):
    hw = module.Hw(target)
    nvme = module.Nvme(hw, BAR, 0, {})
    q = module.Queue(0, 0x30000000, CQ, ENTRIES, 0, BAR)
    return nvme._reap(q, cids, seconds), q


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--preimage', type=Path, help='an nvme_m3.py to run the v75 race against (expected to fail)')
    args = ap.parse_args()
    path = args.preimage or ROOT / 'tools/nvme_m3.py'
    spec = importlib.util.spec_from_file_location('nvme_m3_under_test', path)
    m3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m3)

    # 1. The v75 race: the identify completion lands during the third poll read.
    target = Target(land_at=3, landed=entry(1))
    try:
        done, q = reap(m3, target, [1])
    except q1n1proxy.ProxyError as error:
        print(f'FAIL: v75 race: {error}')
        return 1
    expect(done == [(0, 0, 0)] and q.head == 1, 'v75 race: completion not reaped')
    expect(target.doorbells == [(q.cq_db, 1)], 'doorbell not rung with the new head')
    expect(all(live for _, live in target.reads), 'a completion-queue read was checksummed')

    # 2. A torn live read: new phase, stale identifier and status; then the whole entry, twice.
    target = Target(land_at=1, landed=entry(7, dw0=0x55), torn=[entry(0, status=0x7fff)])
    done, q = reap(m3, target, [7])
    expect(done == [(0, 0x55, 0)], f'torn entry used: {done}')

    # 3. An entry that never reads the same twice refuses.
    target = Target(land_at=1, landed=entry(2), unstable=True)
    try:
        reap(m3, target, [2])
        expect(False, 'an unstable entry was used')
    except m3.Misbehaved as error:
        expect('never read the same twice' in str(error), f'refused for the wrong reason: {error}')

    # 4. A failed status still refuses.
    target = Target(land_at=1, landed=entry(3, status=0x2))
    try:
        reap(m3, target, [3])
        expect(False, 'a failed command was accepted')
    except m3.Misbehaved as error:
        expect('failed' in str(error), f'refused for the wrong reason: {error}')

    # 5. No completion: the bounded wait still ends (CSTS read through the register path).
    class Quiet(Target):
        def __init__(self):
            super().__init__()
            self.registers = []
    target = Quiet()
    hw = m3.Hw(target)
    hw.r32 = lambda address: target.registers.append(address) or 0
    nvme = m3.Nvme(hw, BAR, 0, {})
    try:
        nvme._reap(m3.Queue(0, 0x30000000, CQ, ENTRIES, 0, BAR), [4], 0.01)
        expect(False, 'no completion was accepted')
    except m3.Misbehaved as error:
        expect('no completion' in str(error) and target.registers, f'refused for the wrong reason: {error}')
    print(f'PASS: {checks} completion-poll checks ({path.name}, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
