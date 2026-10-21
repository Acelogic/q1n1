#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Explicitly bounded NVMe Write/Flush on the existing q1n1 M3 controller.

See docs/A16-NVME-WRITES.md for failure analysis and physical qualification.
The original Nvme class and command-line tool remain read-only.
"""
import struct

from nvme_m3 import Nvme, PAGE, Refused


class WritableNvme(Nvme):
    def enable_writes(self, ranges):
        """Allow only these (first LBA, LBA count) ranges after Identify."""
        if not self.nsze or not self.lba_bytes or self.io is None:
            raise Refused('Identify and I/O queues are required before enabling writes')
        checked = []
        for first, count in ranges:
            if type(first) is not int or type(count) is not int or first < 0 or count <= 0 or first + count > self.nsze:
                raise Refused('invalid writable namespace range')
            checked.append((first, first + count))
        checked.sort()
        if not checked or any(b > c for (_, b), (c, _) in zip(checked, checked[1:])):
            raise Refused('writable ranges are empty or overlap')
        self.write_ranges = tuple(checked)

    def disable_writes(self):
        self.write_ranges = ()

    def _allow_write(self, first, count):
        if type(first) is not int or type(count) is not int or first < 0 or count <= 0:
            raise Refused('invalid write LBA/count')
        if not any(lo <= first and first + count <= hi for lo, hi in getattr(self, 'write_ranges', ())):
            raise Refused(f'write {first}+{count} is outside the explicitly enabled ranges')

    def _check_io_command(self, opcode, nsid, prp1, prp2, cdw):
        if opcode == 0x02:
            return super()._check_io_command(opcode, nsid, prp1, prp2, cdw)
        if not getattr(self, 'write_ranges', ()) or nsid != 1:
            raise Refused('namespace writes/flush are not enabled')
        if opcode == 0x00:
            if prp1 or prp2 or any(cdw):
                raise Refused('Flush must not contain data pointers or command dwords')
            return
        if opcode != 0x01 or len(cdw) != 3 or cdw[2] & ~0x4000ffff:
            raise Refused('only plain FUA NVM Write and Flush are permitted')
        if not cdw[2] & (1 << 30):
            raise Refused('writes must set FUA')
        first = cdw[0] | cdw[1] << 32
        count = (cdw[2] & 0xffff) + 1
        self._allow_write(first, count)
        size = count * self.lba_bytes
        if size > self.max_transfer or prp1 != self.w + self.l.data:
            raise Refused('write exceeds bounce buffer or controller transfer limit')
        pages = (size + PAGE - 1) // PAGE
        expected = 0 if pages == 1 else prp1 + PAGE if pages == 2 else self.w + self.l.prp
        if prp2 != expected:
            raise Refused('unexpected write PRP2')

    def write(self, lba, data):
        """Write a complete aligned payload with FUA; reject its entire range first."""
        if not isinstance(data, (bytes, bytearray)) or not data or not self.lba_bytes or len(data) % self.lba_bytes:
            raise Refused('write payload must be nonempty and LBA aligned')
        self._allow_write(lba, len(data) // self.lba_bytes)
        per = min(self.max_transfer, 65536 * self.lba_bytes)
        for off in range(0, len(data), per):
            chunk = data[off:off + per]
            count = len(chunk) // self.lba_bytes
            address = self.w + self.l.data
            pages = (len(chunk) + PAGE - 1) // PAGE
            prp2 = 0
            if pages == 2:
                prp2 = address + PAGE
            elif pages > 2:
                prp2 = self.w + self.l.prp
                entries = struct.pack(f'<{pages - 1}Q', *(address + PAGE * i for i in range(1, pages)))
                self.hw.write(prp2, entries)
                self.hw.civac(prp2, len(entries))
            self.hw.write(address, chunk)
            self.hw.civac(address, len(chunk))
            cid = self._submit(self.io, 0x01, 1, address, prp2,
                               (lba & 0xffffffff, lba >> 32, (count - 1) | (1 << 30)))
            self._ring(self.io)
            self._reap(self.io, [cid])
            self.log.setdefault('writes', []).append(dict(lba=lba, blocks=count, bytes=len(chunk), cid=cid, fua=True))
            lba += count

    def flush(self):
        cid = self._submit(self.io, 0x00, 1)
        self._ring(self.io)
        self._reap(self.io, [cid], seconds=15.0)
        self.log['flushes'] = self.log.get('flushes', 0) + 1
