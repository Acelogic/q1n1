#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Full-install M3: drive the A16's NVMe SSD read-only from the host, through q1n1's proxy.

  python3 tools/nvme_m3.py --device udp://en5 \\
      --survey build/full-install/survey-20260925.json \\
      --json build/full-install/nvme-m3-20260925.json [--timed-mib 256]

In order (docs/A16-NVME-M3.md):
 1. arm the proxy's SError strict guard;
 2. find the SSD from ACPI (MCFG segment 5, the root port's secondary bus) and pin it: Samsung
    144d, class 010802, BAR0 inside the root port's window, Memory Space and Bus Master on;
 3. prove where it can DMA: the PCIe SMMUv3 stream table entry for SID segment << 16 | RID must
    bypass or translate at stage 2 only, and every page of the DMA window must be identity-mapped
    read/write there. The window is memory the final EFI map calls Conventional, clear of q1n1's
    own regions; it is zeroed and cleaned before the controller sees it;
 4. take the controller over: a normal shutdown (CC.SHN) if firmware left it enabled, CC.EN 0,
    q1n1's admin queue, CC.EN 1;
 5. Identify controller and namespace 1 (pins the EUI64 and the size), APST, SMART, one I/O queue
    pair;
 6. read both GPT copies through the driver and compare them with the pre-ExitBootServices survey;
 7. optionally time sequential reads (--timed-mib) from the largest partition;
 8. delete its I/O queues and shut the controller down (CC.SHN, then CC.EN 0).

Read-only by construction: ADMIN_ALLOWED and IO_ALLOWED list every opcode this tool can build,
Set Features is limited to Number of Queues without the Save bit, and anything else is refused
before a byte reaches the queue or a doorbell. Nothing here can write the SSD.

Exit status: 0 done and the GPT matches; 2 a pin or check failed; 3 the controller misbehaved
(timeout, fatal status, error completion); 4 refused (would have needed something not allowed);
5 proxy link failed.
"""
import argparse
import datetime
import json
import os
import struct
import sys
import time
import uuid
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpt_survey  # noqa: E402
import preload  # noqa: E402
import q1n1proxy  # noqa: E402

PAGE = 4096
SEGMENT = 5                     # the survey's Acpi(PNP0A08, UID 5)
SMMU_BASE = 0x15480000          # IORT: every PCIe segment maps to SMMUv3 node 0x174A
EXPECT_VENDOR = 0x144D
EXPECT_CLASS = 0x010802
EXPECT_EUI64 = '002538F261C3C291'   # namespace 1, Identify byte order (survey eui_order "identify")
EFI_CONVENTIONAL = 7

ADMIN_ALLOWED = {0x00: 'Delete I/O SQ', 0x01: 'Create I/O SQ', 0x02: 'Get Log Page',
                 0x04: 'Delete I/O CQ', 0x05: 'Create I/O CQ', 0x06: 'Identify',
                 0x09: 'Set Features', 0x0A: 'Get Features'}
SET_FEATURES_ALLOWED = {0x07}   # Number of Queues
IO_ALLOWED = {0x02: 'Read'}

# Controller registers (NVMe 2.0 base spec, 3.1.4)
CAP, VS, INTMS, CC, CSTS, AQA, ASQ, ACQ = 0x00, 0x08, 0x0C, 0x14, 0x1C, 0x24, 0x28, 0x30
CC_EN, CC_SHN_NORMAL = 1, 1 << 14
CSTS_RDY, CSTS_CFS = 1, 2

ADMIN_ENTRIES, IO_ENTRIES = 32, 64


class Layout:
    """Where the queues, PRP lists and data live inside the DMA window (offsets from its base)."""

    def __init__(self, name, size, data, data_bytes, chunk):
        self.name, self.size, self.data, self.data_bytes, self.chunk = name, size, data, data_bytes, chunk
        self.asq, self.acq, self.iosq, self.iocq = 0x0000, 0x1000, 0x2000, 0x3000
        self.ident, self.aux, self.prp = 0x4000, 0x5000, 0x8000    # PRP lists: 8 pages, one per slot
        self.slots = data_bytes // chunk
        assert self.slots <= 8 and self.prp + 8 * PAGE <= data and data + data_bytes <= size

    def json(self):
        return dict(name=self.name, bytes=self.size, asq=self.asq, acq=self.acq, iosq=self.iosq, iocq=self.iocq,
                    ident=self.ident, aux=self.aux, prp=self.prp, data=self.data, data_bytes=self.data_bytes,
                    admin_entries=ADMIN_ENTRIES, io_entries=IO_ENTRIES)


# M3's own run: a 4 MiB window at the top of Conventional memory, 2 MiB of data, 256 KiB reads.
M3_LAYOUT = Layout('m3', 4 << 20, 2 << 20, 2 << 20, 256 << 10)
# Adopted by the Snapintosh monitor (compat/nvme-host.h mirrors it): 1 MiB inside the loader's
# reservation, a 256 KiB bounce buffer for reads of up to 128 KiB (the virtual ANS2's MDTS).
ADOPT_LAYOUT = Layout('adopt', 1 << 20, 0x10000, 256 << 10, 128 << 10)


class Stop(Exception):
    exit_code = 2

    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


COMPLETION_READS = 8   # live rereads of a new completion until two agree


class Misbehaved(Stop):
    exit_code = 3


class Refused(Stop):
    exit_code = 4


def hx(v):
    return f'{v:#x}'


class Hw:
    """The proxy surface this tool uses, logged."""

    def __init__(self, proxy):
        self.p = proxy
        self.ops = 0

    def r32(self, a):
        self.ops += 1
        return self.p.read32(a)

    def r64(self, a):
        self.ops += 1
        return self.p.read64(a)

    def w32(self, a, v):
        self.ops += 1
        self.p.write32(a, v)

    def read(self, a, n):
        self.ops += 1
        return self.p.readmem(a, n)

    def read_live(self, a, n):
        """Memory the controller writes by DMA (completion queues). A checksummed read reports
        a checksum error when a write lands between its two passes, and a guarded session
        freezes on it; live reads carry no data checksum (q1n1proxy.Proxy.readmem)."""
        self.ops += 1
        return self.p.readmem(a, n, live=True)

    def write(self, a, data):
        self.ops += 1
        self.p.writemem(a, data)

    def civac(self, a, n):
        self.ops += 1
        self.p.request(self.p.P_DC_CIVAC, a, n)

    def zero(self, a, n):
        self.ops += 1
        self.p.request(self.p.P_MEMSET64, a, 0, n)
        self.civac(a, n)


# ---------------------------------------------------------------- locate ---

def acpi_table(hw, rsdp, want):
    head = hw.read(rsdp, 36)
    if head[:8] != b'RSD PTR ':
        raise Stop(f'no RSDP at {rsdp:#x}')
    xsdt = struct.unpack_from('<Q', head, 24)[0]
    length = struct.unpack_from('<I', hw.read(xsdt, 8), 4)[0]
    body = hw.read(xsdt, length)
    for off in range(36, length, 8):
        addr = struct.unpack_from('<Q', body, off)[0]
        sig = hw.read(addr, 8)
        if sig[:4] == want:
            return hw.read(addr, struct.unpack_from('<I', sig, 4)[0])
    raise Stop(f'ACPI table {want.decode()} not found')


def config_header(hw, base):
    words = struct.unpack('<16I', hw.read(base, 64))
    return words


def locate(hw, info, record):
    mcfg = acpi_table(hw, info['acpi_rsdp'], b'MCFG')
    for off in range(44, len(mcfg), 16):
        ecam, seg, first_bus, last_bus = struct.unpack_from('<QHBB', mcfg, off)
        if seg == SEGMENT:
            break
    else:
        raise Stop(f'MCFG has no segment {SEGMENT}')
    root = ecam + (first_bus << 20)
    rp = config_header(hw, root)
    if (rp[3] >> 16) & 0x7F != 1:
        raise Stop('segment root 0:0.0 is not a bridge')
    secondary = (rp[6] >> 8) & 0xFF
    if not first_bus < secondary <= last_bus:
        raise Stop(f'root port secondary bus {secondary} outside MCFG buses {first_bus}..{last_bus}')
    mem_base = (rp[8] & 0xFFF0) << 16
    mem_limit = ((rp[8] >> 16) & 0xFFF0) << 16 | 0xFFFFF
    ep_cfg = ecam + (secondary << 20)
    ep = config_header(hw, ep_cfg)
    vendor, device, klass = ep[0] & 0xFFFF, ep[0] >> 16, ep[2] >> 8
    command = ep[1] & 0xFFFF
    bar_lo, bar_hi = ep[4], ep[5]
    bar = (bar_lo & ~0xF) | (bar_hi << 32 if (bar_lo >> 1) & 3 == 2 else 0)
    record['pci'] = dict(ecam=hx(ecam), buses=[first_bus, last_bus], root_port=hx(rp[0]), secondary=secondary,
                         window=[hx(mem_base), hx(mem_limit)], vendor=hx(vendor), device=hx(device),
                         class_code=hx(klass), command=hx(command), bar0=hx(bar))
    if vendor != EXPECT_VENDOR or klass != EXPECT_CLASS:
        raise Stop(f'endpoint {vendor:04x}:{device:04x} class {klass:06x} is not the pinned SSD')
    if not command & 2 or not command & 4:
        raise Stop(f'Memory Space / Bus Master not both on (command {command:#x}); this tool does not change them')
    if not (mem_base <= bar and bar + 0x4000 - 1 <= mem_limit):
        raise Stop(f'BAR0 {bar:#x} outside the root port window')
    rid = secondary << 8
    return bar, SEGMENT << 16 | rid


# ------------------------------------------------------------------ SMMU ---

class Smmu:
    """Read-only: what the PCIe SMMUv3 does with one stream (SMMUv3 spec 5.2, 3.3)."""

    def __init__(self, hw, sid, base=SMMU_BASE):
        self.hw, self.sid = hw, sid
        cr0 = hw.r32(base + 0x20)
        gbpa = hw.r32(base + 0x44)
        self.info = dict(cr0=hx(cr0), gbpa=hx(gbpa), sid=hx(sid))
        if not cr0 & 1:
            self.mode = 'abort' if gbpa & (1 << 20) else 'bypass'
            return
        strtab = hw.r64(base + 0x80) & 0xFFFFFFFFFFC0
        cfg = hw.r32(base + 0x88)
        fmt, split, log2size = (cfg >> 16) & 3, (cfg >> 6) & 0x1F, cfg & 0x3F
        self.info.update(strtab=hx(strtab), strtab_cfg=hx(cfg))
        if sid >> log2size:
            raise Stop(f'SID {sid:#x} is beyond the stream table ({log2size} bits)')
        if fmt == 0:
            ste = strtab + sid * 64
        else:
            l1 = hw.r64(strtab + (sid >> split) * 8)
            if not l1 & 0x1F:
                self.mode = 'unconfigured'
                return
            ste = (l1 & 0xFFFFFFFFFFC0) + (sid & ((1 << split) - 1)) * 64
        w = struct.unpack('<4Q', hw.read(ste, 32))
        self.info['ste'] = [hx(x) for x in w]
        if not w[0] & 1:
            self.mode = 'invalid'
            return
        config = (w[0] >> 1) & 7
        self.mode = {0: 'abort', 4: 'bypass', 5: 's1', 6: 's2', 7: 's1+s2'}.get(config, f'config {config}')
        if self.mode != 's2':
            return
        t0sz, sl0, tg = (w[2] >> 32) & 0x3F, (w[2] >> 38) & 3, (w[2] >> 46) & 3
        aa64 = (w[2] >> 51) & 1
        if tg != 0 or not aa64 or sl0 > 2:
            raise Stop(f'stage 2 is not AArch64 with a 4 KiB granule (TG {tg}, AA64 {aa64}, SL0 {sl0})')
        self.ipa_bits = 64 - t0sz
        self.start_level = 2 - sl0
        self.ttb = w[3] & 0xFFFFFFFFFFF0
        self.info['s2'] = dict(ipa_bits=self.ipa_bits, start_level=self.start_level, ttb=hx(self.ttb),
                               vmid=w[2] & 0xFFFF)

    def translate(self, ipa):
        """(output page, bytes the descriptor covers, S2AP) for ipa, or None if unmapped."""
        if ipa >> self.ipa_bits:
            return None
        table, level = self.ttb, self.start_level
        while True:
            shift = 12 + 9 * (3 - level)
            width = self.ipa_bits - shift if level == self.start_level else 9
            desc = self.hw.r64(table + 8 * ((ipa >> shift) & ((1 << width) - 1)))
            if not desc & 1:
                return None
            if level < 3 and desc & 2:
                table, level = desc & 0xFFFFFFFFF000, level + 1
                continue
            if level == 3 and not desc & 2:
                return None
            span = 1 << shift
            oa = (desc & 0xFFFFFFFFF000) & ~(span - 1)
            return oa + (ipa & (span - 1) & ~(PAGE - 1)), span - (ipa & (span - 1)), (desc >> 6) & 3

    def reaches(self, base, size):
        """True when every page of [base, base+size) is identity-mapped read/write for the SSD."""
        if self.mode == 'bypass':
            return True
        if self.mode != 's2':
            return False
        at = base
        while at < base + size:
            t = self.translate(at)
            if t is None or t[0] != at or t[2] != 3:
                return False
            at += t[1]
        return True


def q1n1_regions(info):
    own = [(info.get(b, 0), info.get(s, 0)) for b, s in (
        ('heap_base', 'heap_size'), ('dma_base', 'dma_size'), ('stage_base', 'stage_size'),
        ('image_base', 'image_size'), ('memory_map', 'memory_map_size'), ('preload_table', 'preload_size'),
        ('fb_base', 'fb_size'), ('gpu_arena_base', 'gpu_arena_size'), ('gpt_survey', 'gpt_survey_size'))]
    return [(b, s) for b, s in own if b and s]


def given_window(hw, info, smmu, base, layout, record):
    """The caller's DMA window (the Snapintosh reservation): Conventional, clear of q1n1, reachable."""
    size = layout.size
    if base % (64 << 10):
        raise Stop(f'DMA window {base:#x} is not 64 KiB aligned')
    regions = preload.memory_regions(hw.p, info)
    if not any(k == EFI_CONVENTIONAL and lo <= base and base + size <= hi for k, lo, hi in regions):
        raise Stop(f'DMA window {base:#x}+{size:#x} is not inside one Conventional region')
    if any(base < b + s and b < base + size for b, s in q1n1_regions(info)):
        raise Stop(f'DMA window {base:#x} overlaps q1n1\'s own memory')
    if not smmu.reaches(base, size):
        raise Stop(f'DMA window {base:#x} is not identity-mapped read/write for the SSD')
    record['window'] = dict(base=hx(base), bytes=size, given=True)
    return base


def pick_window(hw, info, smmu, record):
    regions = preload.memory_regions(hw.p, info)
    own = q1n1_regions(info)
    WINDOW = M3_LAYOUT.size
    tried = 0
    for kind, start, end in sorted(regions, key=lambda r: -r[2]):
        if kind != EFI_CONVENTIONAL:
            continue
        cand = (end - WINDOW) & ~((2 << 20) - 1)
        while cand >= start and tried < 64:
            if not any(cand < b + s and b < cand + WINDOW for b, s in own):
                tried += 1
                if smmu.reaches(cand, WINDOW):
                    record['window'] = dict(base=hx(cand), bytes=WINDOW, region=[hx(start), hx(end)], tried=tried)
                    return cand
            cand -= 2 << 20
    raise Stop(f'no Conventional 4 MiB window the SSD can reach (SMMU mode {smmu.mode}, {tried} tried)')


# ------------------------------------------------------------ controller ---

class Queue:
    def __init__(self, qid, sq, cq, entries, stride, bar):
        self.qid, self.sq, self.cq, self.entries = qid, sq, cq, entries
        self.sq_db = bar + 0x1000 + (2 * qid) * (4 << stride)
        self.cq_db = bar + 0x1000 + (2 * qid + 1) * (4 << stride)
        self.tail = self.head = 0
        self.phase = 1
        self.next_cid = 1
        self.outstanding = {}


class Nvme:
    def __init__(self, hw, bar, window, log, layout=M3_LAYOUT):
        self.hw, self.bar, self.w, self.log, self.l = hw, bar, window, log, layout
        self.lba_bytes = None
        self.nsze = None
        self.io = None

    def reg(self, off):
        return self.hw.r32(self.bar + off)

    def wait(self, what, cond, seconds):
        deadline = time.monotonic() + seconds
        while True:
            csts = self.reg(CSTS)
            if cond(csts):
                return csts
            if time.monotonic() > deadline:
                raise Misbehaved(f'timed out waiting for {what}', csts=hx(csts), cc=hx(self.reg(CC)))
            time.sleep(0.002)

    def takeover(self, record):
        hw, bar = self.hw, self.bar
        cap = hw.r64(bar + CAP)
        before = dict(cap=hx(cap), vs=hx(self.reg(VS)), cc=hx(self.reg(CC)), csts=hx(self.reg(CSTS)),
                      aqa=hx(self.reg(AQA)), asq=hx(hw.r64(bar + ASQ)), acq=hx(hw.r64(bar + ACQ)))
        self.stride = (cap >> 32) & 0xF
        self.timeout = max(1, (cap >> 24) & 0xFF) * 0.5
        mpsmin = 1 << (12 + ((cap >> 48) & 0xF))
        mqes = (cap & 0xFFFF) + 1
        record['controller_before'] = before
        record['cap'] = dict(mqes=mqes, dstrd=self.stride, timeout_s=self.timeout, mpsmin=mpsmin,
                             css=hx((cap >> 37) & 0xFF))
        if mpsmin > PAGE or not (cap >> 37) & 1 or mqes < IO_ENTRIES:
            raise Stop('controller capabilities do not allow 4 KiB pages, the NVM command set or 64 entries')
        cc, csts = self.reg(CC), self.reg(CSTS)
        steps = []
        if cc & CC_EN:
            if csts & CSTS_RDY and not csts & CSTS_CFS:
                hw.w32(bar + CC, cc | CC_SHN_NORMAL)
                csts = self.wait('shutdown complete', lambda s: (s >> 2) & 3 == 2, 10.0)
                steps.append(f'normal shutdown, CSTS {csts:#x}')
            hw.w32(bar + CC, 0)
            csts = self.wait('CSTS.RDY 0', lambda s: not s & CSTS_RDY, self.timeout)
            steps.append(f'CC.EN 0, CSTS {csts:#x}')
        hw.zero(self.w, self.l.size)
        self.admin = Queue(0, self.w + self.l.asq, self.w + self.l.acq, ADMIN_ENTRIES, self.stride, bar)
        hw.w32(bar + AQA, (ADMIN_ENTRIES - 1) << 16 | (ADMIN_ENTRIES - 1))
        for off, addr in ((ASQ, self.admin.sq), (ACQ, self.admin.cq)):
            hw.w32(bar + off, addr & 0xFFFFFFFF)
            hw.w32(bar + off + 4, addr >> 32)
        hw.w32(bar + CC, 4 << 20 | 6 << 16 | CC_EN)          # IOCQES 16 B, IOSQES 64 B, MPS 4 KiB, NVM
        csts = self.wait('CSTS.RDY 1', lambda s: s & (CSTS_RDY | CSTS_CFS), self.timeout)
        if csts & CSTS_CFS:
            raise Misbehaved('controller fatal status after enable', csts=hx(csts))
        steps.append(f'q1n1 admin queue, CC.EN 1, CSTS {csts:#x}')
        record['takeover'] = steps

    # --- commands -------------------------------------------------------

    def _check_io_command(self, opcode, nsid, prp1, prp2, cdw):
        if opcode not in IO_ALLOWED:
            raise Refused(f'I/O opcode {opcode:#x} is not on the read-only allow-list')

    def _submit(self, q, opcode, nsid=0, prp1=0, prp2=0, cdw=()):
        if q.qid == 0:
            if opcode not in ADMIN_ALLOWED:
                raise Refused(f'admin opcode {opcode:#x} is not on the read-only allow-list')
            if opcode == 0x09 and ((cdw[0] & 0xFF) not in SET_FEATURES_ALLOWED or cdw[0] >> 31):
                raise Refused(f'Set Features {cdw[0]:#x} is not on the allow-list (Number of Queues, no Save)')
        else:
            self._check_io_command(opcode, nsid, prp1, prp2, cdw)
        if len(q.outstanding) >= q.entries - 1:
            raise Stop('queue full')
        cid = q.next_cid
        q.next_cid = q.next_cid % 0xFFFF + 1
        words = list(cdw) + [0] * (6 - len(cdw))
        entry = struct.pack('<IIQQQQ6I', opcode | cid << 16, nsid, 0, 0, prp1, prp2, *words)
        at = q.sq + q.tail * 64
        self.hw.write(at, entry)
        self.hw.civac(at, 64)
        q.tail = (q.tail + 1) % q.entries
        q.outstanding[cid] = (opcode, time.monotonic())
        return cid

    def _ring(self, q):
        self.hw.w32(q.sq_db, q.tail)

    def _reap(self, q, cids, seconds=5.0):
        done = {}
        deadline = time.monotonic() + seconds
        while any(c not in done for c in cids):
            at = q.cq + q.head * 16
            self.hw.civac(at, 16)
            entry = self.hw.read_live(at, 16)
            if (struct.unpack_from('<I', entry, 12)[0] >> 16) & 1 == q.phase:
                # Live reads have no data checksum, and an entry read while it is written can be
                # torn: use it once two consecutive reads agree.
                for _ in range(COMPLETION_READS):
                    self.hw.civac(at, 16)
                    again = self.hw.read_live(at, 16)
                    if again == entry:
                        break
                    entry = again
                else:
                    raise Misbehaved(f'completion at {at:#x} on queue {q.qid} never read the same twice')
            dw0, _, dw2, dw3 = struct.unpack('<4I', entry)
            if (dw3 >> 16) & 1 != q.phase:
                if time.monotonic() > deadline:
                    csts = self.reg(CSTS)
                    raise Misbehaved(f'no completion on queue {q.qid} for {[c for c in cids if c not in done]}',
                                     csts=hx(csts))
                time.sleep(0.001)
                continue
            cid, status = dw3 & 0xFFFF, (dw3 >> 17) & 0x7FFF
            q.outstanding.pop(cid, None)
            done[cid] = (status, dw0, dw2 & 0xFFFF)
            q.head = (q.head + 1) % q.entries
            if q.head == 0:
                q.phase ^= 1
            self.hw.w32(q.cq_db, q.head)
        for c in cids:
            if done[c][0]:
                raise Misbehaved(f'command {c} on queue {q.qid} failed', status=hx(done[c][0]))
        return [done[c] for c in cids]

    def admin_cmd(self, opcode, nsid=0, prp1=0, cdw=(), seconds=5.0):
        cid = self._submit(self.admin, opcode, nsid, prp1, 0, cdw)
        self._ring(self.admin)
        return self._reap(self.admin, [cid], seconds)[0]

    def fetch(self, off, n):
        self.hw.civac(self.w + off, n)
        return self.hw.read(self.w + off, n)

    # --- bring-up ---------------------------------------------------------

    def identify(self, record):
        self.admin_cmd(0x06, prp1=self.w + self.l.ident, cdw=(1,))
        c = self.fetch(self.l.ident, PAGE)
        text = lambda a, b: c[a:b].decode('ascii', 'replace').strip()   # noqa: E731
        mdts = c[77]
        npss = c[263]
        power = []
        for i in range(npss + 1):
            d = c[2048 + 32 * i:2048 + 32 * (i + 1)]
            mp, flags = struct.unpack_from('<H', d, 0)[0], d[3]
            enlat, exlat = struct.unpack_from('<II', d, 4)
            power.append(dict(max_power_w=mp / (10000 if flags & 1 else 100), non_operational=bool(flags & 2),
                              entry_us=enlat, exit_us=exlat))
        ctrl = dict(vid=hx(struct.unpack_from('<H', c, 0)[0]), serial=text(4, 24), model=text(24, 64),
                    firmware=text(64, 72), mdts=mdts, max_transfer=(PAGE << mdts) if mdts else None,
                    version=hx(struct.unpack_from('<I', c, 80)[0]), rtd3e_us=struct.unpack_from('<I', c, 88)[0],
                    oacs=hx(struct.unpack_from('<H', c, 256)[0]), apsta=c[265] & 1,
                    sqes=hx(c[512]), cqes=hx(c[513]), nn=struct.unpack_from('<I', c, 516)[0],
                    oncs=hx(struct.unpack_from('<H', c, 520)[0]), vwc=hx(c[525]), power_states=power)
        record['identify_controller'] = ctrl
        self.max_transfer = min(self.l.chunk, ctrl['max_transfer'] or self.l.chunk)

        self.admin_cmd(0x06, nsid=1, prp1=self.w + self.l.ident, cdw=(0,))
        n = self.fetch(self.l.ident, PAGE)
        nsze, ncap, nuse = struct.unpack_from('<QQQ', n, 0)
        flbas = n[26] & 0xF
        formats = []
        for i in range(n[25] + 1):
            ms, lbads, rp = struct.unpack_from('<HBB', n, 128 + 4 * i)
            formats.append(dict(index=i, lba_bytes=1 << lbads, metadata=ms, relative_performance=rp & 3))
        eui = n[120:128].hex().upper()
        ns = dict(nsze=nsze, ncap=ncap, nuse=nuse, flbas=flbas, formats=formats, dlfeat=hx(n[33]),
                  nguid=n[104:120].hex().upper(), eui64=eui)
        record['identify_namespace'] = ns
        if eui != EXPECT_EUI64:
            raise Stop(f'namespace 1 EUI64 {eui} is not the pinned {EXPECT_EUI64}')
        self.lba_bytes = formats[flbas]['lba_bytes']
        self.nsze = nsze
        if formats[flbas]['metadata']:
            raise Stop('the current LBA format carries metadata; this tool reads plain LBAs only')

    def features(self, record):
        status, dw0, _ = self.admin_cmd(0x0A, prp1=self.w + self.l.aux, cdw=(0x0C,))
        record['apst'] = dict(enabled=dw0 & 1)
        self.admin_cmd(0x02, nsid=0xFFFFFFFF, prp1=self.w + self.l.aux, cdw=(127 << 16 | 0x02,))
        s = self.fetch(self.l.aux, 512)
        u128 = lambda o: int.from_bytes(s[o:o + 16], 'little')   # noqa: E731
        record['smart'] = dict(critical_warning=hx(s[0]), temperature_c=struct.unpack_from('<H', s, 1)[0] - 273,
                               available_spare=s[3], percent_used=s[5], data_units_read=u128(32),
                               data_units_written=u128(48), power_cycles=u128(112), power_on_hours=u128(128),
                               unsafe_shutdowns=u128(144), media_errors=u128(160), error_log_entries=u128(176))

    def io_queues(self, record):
        _, dw0, _ = self.admin_cmd(0x09, cdw=(0x07, 0))                  # one SQ, one CQ (0-based)
        record['queues_allocated'] = dict(sq=(dw0 & 0xFFFF) + 1, cq=(dw0 >> 16) + 1)
        self.admin_cmd(0x05, prp1=self.w + self.l.iocq, cdw=((IO_ENTRIES - 1) << 16 | 1, 1))    # PC, no IEN
        self.admin_cmd(0x01, prp1=self.w + self.l.iosq, cdw=((IO_ENTRIES - 1) << 16 | 1, 1 << 16 | 1))
        self.io = Queue(1, self.w + self.l.iosq, self.w + self.l.iocq, IO_ENTRIES, self.stride, self.bar)

    def delete_io_queues(self):
        if self.io is None:
            return
        self.admin_cmd(0x00, cdw=(1,))
        self.admin_cmd(0x04, cdw=(1,))
        self.io = None

    def shutdown(self, record):
        hw, bar = self.hw, self.bar
        steps = []
        cc = self.reg(CC)
        if cc & CC_EN:
            hw.w32(bar + CC, cc | CC_SHN_NORMAL)
            csts = self.wait('shutdown complete', lambda s: (s >> 2) & 3 == 2, 10.0)
            steps.append(f'normal shutdown, CSTS {csts:#x}')
            hw.w32(bar + CC, 0)
            csts = self.wait('CSTS.RDY 0', lambda s: not s & CSTS_RDY, self.timeout)
            steps.append(f'CC.EN 0, CSTS {csts:#x}')
        record['shutdown'] = steps

    # --- reads --------------------------------------------------------------

    def _read_cmd(self, slot, lba, count):
        nbytes = count * self.lba_bytes
        data = self.w + self.l.data + slot * self.l.chunk
        pages = (nbytes + PAGE - 1) // PAGE
        prp2 = 0
        if pages == 2:
            prp2 = data + PAGE
        elif pages > 2:
            lst = self.w + self.l.prp + slot * PAGE
            self.hw.write(lst, struct.pack(f'<{pages - 1}Q', *(data + PAGE * i for i in range(1, pages))))
            self.hw.civac(lst, 8 * (pages - 1))
            prp2 = lst
        return self._submit(self.io, 0x02, 1, data, prp2, (lba & 0xFFFFFFFF, lba >> 32, count - 1))

    def read(self, lba, count):
        """count LBAs from lba, fetched to the host."""
        if lba < 0 or count <= 0 or lba + count > self.nsze:
            raise Stop(f'read {lba}+{count} is outside namespace 1 ({self.nsze} LBAs)')
        per = self.max_transfer // self.lba_bytes
        out = bytearray()
        while count:
            n = min(per, count)
            cid = self._read_cmd(0, lba, n)
            self._ring(self.io)
            self._reap(self.io, [cid])
            out += self.fetch(self.l.data, n * self.lba_bytes)
            lba += n
            count -= n
        return bytes(out)

    def timed(self, lba, total_bytes):
        """Device-side sequential read rate: SLOTS commands in flight, data left on the target."""
        per = self.max_transfer // self.lba_bytes
        left = total_bytes // self.lba_bytes
        start = time.monotonic()
        moved = 0
        while left:
            cids = []
            for slot in range(self.l.slots):
                if not left:
                    break
                n = min(per, left)
                cids.append(self._read_cmd(slot, lba, n))
                lba += n
                left -= n
                moved += n * self.lba_bytes
            self._ring(self.io)
            self._reap(self.io, cids, 10.0)
        seconds = time.monotonic() - start
        return dict(bytes=moved, seconds=round(seconds, 3), mb_per_s=round(moved / seconds / 1e6, 1))


# ------------------------------------------------------------------- GPT ---

def read_gpt(nvme, last_block):
    bs = nvme.lba_bytes
    head = nvme.read(0, 34)
    lba0, primary = head[:bs], head[bs:2 * bs]
    entries_lba, count, esize = struct.unpack_from('<QII', primary, 72)
    array_blocks = (count * esize + bs - 1) // bs
    primary_array = head[2 * bs:] if entries_lba == 2 and array_blocks <= 32 else nvme.read(entries_lba, array_blocks)
    backup = nvme.read(last_block, 1)
    b_entries = struct.unpack_from('<Q', backup, 72)[0]
    backup_array = nvme.read(b_entries, array_blocks)
    return gpt_survey.decode_gpt(bs, last_block, lba0, primary, primary_array, backup, backup_array)


SNAP_TYPE = 'af494c0f-7849-4186-8d2c-9b05cf6ab8b8'   # full-install M4's partition type
HEADER_OFF = 512 << 10                                # the window header (Snapintosh tools/snapmac-window-header.py)
HEADER = struct.Struct('<8sII16sQQII6Q32s32s32s32s')


def arm_window(nvme, guid, record):
    """Find the Snapintosh partition through the driver and check its header: the window the monitor adopts."""
    decoded = read_gpt(nvme, nvme.nsze - 1)
    if decoded['errors']:
        raise Stop('the GPT read through the driver has errors', errors=decoded['errors'])
    hits = [p for p in decoded['partitions'] if p['type'] == SNAP_TYPE]
    if len(hits) != 1 or hits[0]['guid'] != guid.lower():
        raise Stop(f'expected one {SNAP_TYPE} partition with GUID {guid}', found=[p['guid'] for p in hits])
    part = hits[0]
    first, last = part['first_lba'], part['last_lba']
    per = 4096 // nvme.lba_bytes
    raw = nvme.read(first + HEADER_OFF // nvme.lba_bytes, per)
    f = HEADER.unpack_from(raw)
    crc = struct.unpack_from('<I', raw, 4092)[0]
    fields = dict(magic=f[0], version=f[1], size=f[2], guid=str(uuid.UUID(bytes_le=f[3])), first_lba=f[4],
                  last_lba=f[5], vblock=f[6], state=f[7], isc_offset=f[8], isc_region=f[9], isc_image=f[10],
                  container_offset=f[11], container_region=f[12], container_image=f[13],
                  isc_sha256=f[14].hex(), container_sha256=f[15].hex(), manifest_sha256=f[16].hex(),
                  provisioned=f[17].rstrip(b'\0').decode('ascii', 'replace'))
    problems = [name for name, ok in (
        ('magic', fields['magic'] == b'SNAPWIN1'), ('version', fields['version'] == 1), ('crc', zlib.crc32(raw[:4092]) == crc),
        ('guid', fields['guid'] == guid.lower()), ('lbas', (fields['first_lba'], fields['last_lba']) == (first, last)),
        ('vblock', fields['vblock'] == 4096), ('state', fields['state'] == 1)) if not ok]
    if problems:
        raise Stop(f'window header check failed: {problems}')
    magics = {}
    for name, off in (('isc', fields['isc_offset']), ('container', fields['container_offset'])):
        block = nvme.read(first + off // nvme.lba_bytes, per)
        magics[name] = block[32:36].decode('ascii', 'replace')
        if block[32:36] != b'NXSB':
            raise Stop(f'no APFS container superblock at the {name} region')
    fields['magic'] = 'SNAPWIN1'
    record['window_armed'] = dict(partition=part['guid'], first_lba=first, last_lba=last, header=fields, apfs=magics)
    return first, last


COMPARE = ('block_size', 'last_block', 'disk_guid', 'first_usable', 'last_usable', 'primary', 'backup',
           'partitions', 'unallocated', 'errors')


def compare_gpt(decoded, survey_path):
    survey = json.load(open(survey_path))
    disks = survey.get('disks') or []
    pinned = [d for d in disks if (d.get('path_info') or {}).get('nvme_eui_bytes') == EXPECT_EUI64]
    if len(pinned) != 1:
        raise Stop(f'the survey has {len(pinned)} disks with EUI64 {EXPECT_EUI64}')
    want = pinned[0]
    diffs = [k for k in COMPARE if json.loads(json.dumps(decoded.get(k))) != want.get(k)]
    return dict(survey=survey_path, equal=not diffs, differing_keys=diffs,
                partitions=len(decoded.get('partitions') or []), errors=decoded.get('errors'))


# ------------------------------------------------------------------ main ---

def run(proxy, args, record):
    hw = Hw(proxy)
    info = proxy.bootinfo()
    bar, sid = locate(hw, info, record)
    smmu = Smmu(hw, sid)
    record['smmu'] = dict(mode=smmu.mode, **smmu.info)
    if smmu.mode not in ('bypass', 's2'):
        raise Stop(f'the SSD stream is {smmu.mode}; this tool only works with bypass or an identity stage 2')
    if args.adopt_dma is not None:
        return adopt(hw, info, bar, sid, smmu, args, record)
    window = pick_window(hw, info, smmu, record)
    nvme = Nvme(hw, bar, window, record)
    record['stage'] = 'takeover'
    nvme.takeover(record)
    try:
        record['stage'] = 'identify'
        nvme.identify(record)
        nvme.features(record)
        nvme.io_queues(record)
        record['stage'] = 'gpt'
        survey_disk = json.load(open(args.survey))['disks'][0]
        last_block = nvme.nsze - 1
        if survey_disk.get('last_block') != last_block or survey_disk.get('block_size') != nvme.lba_bytes:
            raise Stop(f'namespace size {nvme.nsze} x {nvme.lba_bytes} differs from the survey')
        started = time.monotonic()
        decoded = read_gpt(nvme, last_block)
        record['gpt'] = compare_gpt(decoded, args.survey)
        record['gpt']['seconds'] = round(time.monotonic() - started, 3)
        if args.timed_mib:
            record['stage'] = 'timed'
            biggest = max(decoded['partitions'], key=lambda p: p['last_lba'] - p['first_lba'])
            size = args.timed_mib << 20
            if biggest['first_lba'] * nvme.lba_bytes + size > (biggest['last_lba'] + 1) * nvme.lba_bytes:
                raise Stop('timed read would leave the largest partition')
            record['timed'] = dict(partition=biggest['name'], first_lba=biggest['first_lba'],
                                   **nvme.timed(biggest['first_lba'], size))
            fetched = min(size, 16 << 20)
            t0 = time.monotonic()
            nvme.read(biggest['first_lba'], fetched // nvme.lba_bytes)
            dt = time.monotonic() - t0
            record['timed']['end_to_end'] = dict(bytes=fetched, seconds=round(dt, 3),
                                                 mb_per_s=round(fetched / dt / 1e6, 1))
    finally:
        record['stage'] = 'shutdown'
        try:
            nvme.delete_io_queues()
        finally:
            nvme.shutdown(record)
        record['proxy_ops'] = hw.ops
    return 0 if record.get('gpt', {}).get('equal') else 2


def adopt(hw, info, bar, sid, smmu, args, record):
    """Bring the SSD up in the caller's DMA window and leave it running for the Snapintosh monitor."""
    layout = ADOPT_LAYOUT
    window = given_window(hw, info, smmu, args.adopt_dma, layout, record)
    nvme = Nvme(hw, bar, window, record, layout)
    record['stage'] = 'takeover'
    nvme.takeover(record)
    record['stage'] = 'identify'
    nvme.identify(record)
    nvme.io_queues(record)
    record['stage'] = 'arm'
    first, last = arm_window(nvme, args.partition_guid, record)
    io = nvme.io
    if io.outstanding or nvme.admin.outstanding:
        raise Stop('commands still outstanding at hand-off')
    record['adopt'] = dict(
        bar=hx(bar), stride=nvme.stride, sid=hx(sid), smmu=smmu.mode, dma_base=hx(window), layout=layout.json(),
        admin=dict(tail=nvme.admin.tail, head=nvme.admin.head, phase=nvme.admin.phase),
        io=dict(tail=io.tail, head=io.head, phase=io.phase, next_cid=io.next_cid),
        lba_bytes=nvme.lba_bytes, nsze=nvme.nsze, max_transfer=nvme.max_transfer,
        window=dict(first_lba=first, last_lba=last, guid=args.partition_guid.lower()),
        timeout_s=nvme.timeout, left_running=True)
    record['stage'] = 'adopted'
    record['proxy_ops'] = hw.ops
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--device', help='tty, UNIX socket or udp://enN (default: discover)')
    ap.add_argument('--survey', help='gpt_survey.py JSON recorded before ExitBootServices (M3 mode)')
    ap.add_argument('--adopt-dma', type=lambda v: int(v, 0), help='adopt mode: the DMA window (the Snapintosh '
                    'reservation\'s NVMe area); the SSD is left running with its queues there')
    ap.add_argument('--partition-guid', help='adopt mode: the full-install partition (M4)')
    ap.add_argument('--json', required=True, help='record to write (refuses to overwrite)')
    ap.add_argument('--timed-mib', type=int, default=0, help='also time this many MiB of sequential reads')
    args = ap.parse_args(argv)
    if (args.adopt_dma is None) == (args.survey is None) or (args.adopt_dma is not None and not args.partition_guid):
        ap.error('give --survey (M3 read check) or --adopt-dma with --partition-guid (adopt)')
    if os.path.exists(args.json):
        raise SystemExit(f'{args.json} exists; not overwriting a record')
    record = dict(tool='nvme_m3', format=1, started=datetime.datetime.now(datetime.timezone.utc)
                  .isoformat(timespec='seconds'), device=args.device, stopped=None)
    status = 0
    proxy = None
    guard = None
    try:
        proxy = q1n1proxy.connect(args.device)
        guard = proxy.serror_state()
        proxy.serror_reset()
        proxy.serror_strict(True)
        status = run(proxy, args, record)
    except Stop as stop:
        record['stopped'] = dict(reason=stop.reason, stage=record.get('stage'), **stop.detail)
        status = stop.exit_code
    except (q1n1proxy.ProxyError, OSError) as problem:
        record['stopped'] = dict(reason=f'proxy link failed: {problem}', stage=record.get('stage'))
        status = 5
    finally:
        if proxy is not None and guard is not None:
            try:
                after = proxy.serror_state()
                record['serror'] = {k: after[k] for k in ('before', 'during', 'unconsumed')}
                proxy.serror_strict(bool(guard['flags'] & q1n1proxy.SERROR_STRICT))
                if status == 0 and any(record['serror'].values()):
                    record['stopped'] = dict(reason='SError counters are not zero after the run')
                    status = 3
            except (q1n1proxy.ProxyError, OSError) as problem:
                record['serror_error'] = str(problem)
        record['exit_code'] = status
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w') as out:
            json.dump(record, out, indent=1)
        summary = {k: record.get(k) for k in ('stopped', 'takeover', 'gpt', 'timed', 'window_armed', 'adopt', 'shutdown') if k in record}
        print(json.dumps(summary, indent=1))
    return status


if __name__ == '__main__':
    sys.exit(main())
