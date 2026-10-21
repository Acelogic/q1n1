#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline model test for the LLCC slice tool (tools/a16gpu_llcc.py: llcc-survey, llcc-activate, llcc-deactivate,
llcc-gpu), run through a16gpu_stage3.run against FakeStage3 (tools/test-a16gpu-stage3.py) extended with an LLCC
model: twelve banks behind the broadcast base (a write reaches every bank, a read returns the OR) and the
broadcast-and base (a read returns the AND), the v6 ACT_CTRL/STATUS/ACT_CLEAR handshake, COMMON_HW_INFO and
COMMON_STATUS0. The A16's state after UEFI is the model's start: every slice DEACTIVE (status 0x2), as the
b12v1 survey read. Checks agents/dvfs FAILURE-MODES.md L1-L7, L9, L11.

  python3 tools/test-a16gpu-llcc.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adreno_x2 as R  # noqa: E402
import a16gpu_stage3 as S3  # noqa: E402
import a16gpu_llcc as L  # noqa: E402

_spec = importlib.util.spec_from_file_location('stage3_test', TOOLS / 'test-a16gpu-stage3.py')
T3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T3)

checks = 0
BCAST, AND = 0x20400000, 0x20600000


def check(condition, label):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks += 1


class LlccFake(T3.FakeStage3):
    def __init__(self, banks=12, version=0x06010000, never_complete=False, serror_first_write=False,
                 disagree=None, active=(), **kw):
        super().__init__(**kw)
        self.banks = [dict() for _ in range(banks)]
        self.version, self.nbanks = version, banks
        self.never_complete, self.serror_first_write = never_complete, serror_first_write
        for b in self.banks:
            for n in range(32):
                b[4 + n * 0x1000] = L.ACTIVE if n in active else L.DEACTIVE
        if disagree:
            self.banks[3][disagree] = 0xFFFFFFFF
        self.llcc_writes = []
        self.gbif = {0x03D0F004: 0, 0x03D0F008: 0}

    def _read(self, addr):
        if addr == 0x2186400C:
            return self.nbanks << 28 | 0x5
        if addr == BCAST + 0x64000:
            return self.version
        if BCAST <= addr < BCAST + 0x100000:
            v = 0
            for b in self.banks:
                v |= b.get(addr - BCAST, 0)
            return v
        if AND <= addr < AND + 0x100000:
            v = 0xFFFFFFFF
            for b in self.banks:
                v &= b.get(addr - AND, 0)
            return v
        if addr in self.gbif:
            self._cx_ok(addr)
            return self.gbif[addr]
        return super()._read(addr)

    def _write(self, addr, value):
        if BCAST <= addr < BCAST + 0x100000 or AND <= addr < AND + 0x100000:
            if AND <= addr:
                self.violate('write', addr, 'the broadcast-and base is read only')
                return
            if self.serror_first_write and not self.llcc_writes:
                self.llcc_writes.append((addr, value))
                return self.raise_serror(addr)
            self.llcc_writes.append((addr, value))
            off = addr - BCAST
            n, sub = divmod(off, 0x1000)
            for b in self.banks:
                if off < 0x20000 and sub == 0:              # ACT_CTRL(n)
                    b[off] = value
                    if value & 1 and not self.never_complete:
                        op = value >> 1 & 3
                        st = b[4 + n * 0x1000]
                        if op == 1:
                            st = (st & ~L.DEACTIVE) | L.ACTIVE | L.ACT_COMPLETE
                        elif op == 2:
                            st = (st & ~L.ACTIVE) | L.DEACTIVE | L.ACT_COMPLETE
                        b[4 + n * 0x1000] = st
                elif off < 0x20000 and sub == 8:            # ACT_CLEAR(n)
                    if value & 1:
                        b[4 + n * 0x1000] &= ~L.ACT_COMPLETE
                elif off < 0x20000 and sub == 4:
                    self.violate('write', addr, 'TRP_STATUS is read only')
                else:
                    b[off] = value
            return
        if addr in self.gbif:
            self._cx_ok(addr)
            self.gbif[addr] = value
            return
        return super()._write(addr, value)


def run(fake, command, **kw):
    return S3.run(fake, command, S3.Args(**kw))


def llcc_log(fake, op):
    return [(a, v) for o, a, v in fake.log if o == op and R.block_of(a) and R.block_of(a).name in ('LLCC_BCAST', 'LLCC0')]


def no_violation(fake, label):
    check(not fake.violations, f'{label}: no model violation ({fake.violations[:3]})')


def test_registry_and_words():
    check(R.REGS['LLCC_BCAST_TRP_ACT_CTRL'].at(9) == 0x20409000 and R.REGS['LLCC_BCAST_TRP_ACT_CLEAR'].at(1) == 0x20401008,
          'ACT_CTRL(9) 0x20409000, ACT_CLEAR(1) 0x20401008 (llcc-qcom.c:49-50 on the broadcast base)')
    check(R.REGS['LLCC_BCAST_TRP_ATTR0'].at(1) == 0x20441040 and R.REGS['LLCC_BCAST_TRP_ATTR3'].at(9) == 0x20441254
          and R.REGS['LLCC_BCAST_AND_TRP_ATTR2'].at(11) == 0x206412D0, 'ATTR0..3 at 0x41000/08/10/14 + 64 n (v6)')
    check(R.REGS['LLCC_BCAST_WRS_EN'].addr == 0x20442080 and R.REGS['LLCC_BCAST_ALGO_ALLOC3'].addr == 0x20442040,
          'WRS_EN 0x42080, ALLOC3 0x42040 (llcc_v6_reg_offset)')
    check(R.REGS['GBIF_SCACHE_CNTL0'].addr == 0x03D0F004 and R.REGS['GBIF_SCACHE_CNTL1'].addr == 0x03D0F008,
          'GBIF_SCACHE_CNTL0/1 at 0x3c01/0x3c02 dwords')
    cpu = L.attrs(L.TABLES['linux']['cpuss'], 12)
    check(cpu == (0, 0xFFF, 0x1000, 7680 * 1024 // 12 >> 6), f'CPUSS (linux) ATTR words {cpu}')
    htw = L.attrs(L.TABLES['linux']['gpuhtw'], 12)
    check(htw == (0, 0xFFF, 0x1100, 512 * 1024 // 12 >> 6), f'GPUHTW: fixed-size bit 8, priority 1 << 12 ({htw})')
    check(L.attrs(L.TABLES['windows-12']['cpuss'], 12)[3] == 10240 * 1024 // 12 >> 6, 'windows-12 max_cap 10240 KiB')
    check(L.gpu_scid_words(9) == (9 << 22 | 9 << 16 | 9 << 10 | 1 << 8, 0x09249249),
          'GPU SCID 9: CNTL0 fields + BIT(8), CNTL1 five fields (a8xx_llc_activate)')


def test_activate_deactivate():
    fake = LlccFake()
    status, out = run(fake, 'llcc-survey')
    check(status == 0 and out['llcc']['banks'] == 12 and not llcc_log(fake, 'w'), f'survey read-only ({out["stopped"]})')
    check(all(not v['status']['active_any_bank'] and v['status']['deactive_any_bank']
              for v in out['llcc']['slices'].values()), 'survey: every slice deactivated (the A16 after UEFI)')
    status, out = run(fake, 'llcc-activate')
    check(status == 0, f'llcc-activate completes ({out["stopped"]})')
    for name, scid in (('cpuss', 1), ('gpu', 9), ('gpuhtw', 11)):
        e = out['llcc']['slices'][name]
        check(e.get('activated') and e['after']['active_all_banks'] and not e['after']['deactive_any_bank'],
              f'{name}: active in every bank')
        st = fake.banks[0][4 + scid * 0x1000]
        check(st == L.ACTIVE, f'{name}: ACT_COMPLETE cleared by ACT_CLEAR, status {st:#x}')
    b = fake.banks[7]
    check(b[0x42080] >> 9 & 1 and b[0x42088] >> 9 & 1 and b[0x42008] >> 9 & 1 and not b[0x42080] >> 1 & 1,
          'GPU: WRS_EN, WRS_CACHEABLE_EN, STALE_EN set; CPUSS WRS_EN clear')
    check(all(b[0x42040] >> s & 1 for s in (1, 9, 11)), 'vict_prio (ALLOC3) for all three')
    check(b[0x41000 + 64 * 9 + 0x14] == 7680 * 1024 // 12 >> 6, 'GPU ATTR3 in a bank')
    writes = llcc_log(fake, 'w')
    first_act = next(i for i, (a, _v) in enumerate(writes) if a == 0x20401000)
    check(all(0x20441000 <= a < 0x20443000 for a, _v in writes[:first_act]), 'CPUSS: configured before its activation')
    check([v for a, v in writes if a == 0x20401000] == [3, 2], 'ACT_CTRL: opcode 1 with trigger (3), then without (2)')
    no_violation(fake, 'activate')
    # L4: already active -> nothing written for it.
    n = len(llcc_log(fake, 'w'))
    status, out = run(fake, 'llcc-activate')
    check(status == 0 and all(e.get('skipped') for e in out['llcc']['slices'].values())
          and len(llcc_log(fake, 'w')) == n, 'second activate: every slice already active, nothing written (L4)')
    # L9: deactivate.
    status, out = run(fake, 'llcc-deactivate', slices='gpu,gpuhtw')
    check(status == 0 and out['llcc']['slices']['gpu']['deactivated'] and
          not out['llcc']['slices']['gpu']['after']['active_any_bank'], 'deactivate GPU/GPUHTW (L9)')
    check(fake.banks[0][4 + 1 * 0x1000] == L.ACTIVE, 'CPUSS untouched by --slices gpu,gpuhtw')
    check([v for a, v in llcc_log(fake, 'w') if a == 0x20409000][-2:] == [5, 4], 'deactivate: opcode 2 (5, then 4)')
    no_violation(fake, 'deactivate')
    # Windows table by bank count.
    fake = LlccFake()
    status, out = run(fake, 'llcc-activate', table='windows', slices='cpuss')
    check(status == 0 and out['llcc']['table'] == 'windows-12' and
          fake.banks[0][0x41000 + 64 + 0x14] == 10240 * 1024 // 12 >> 6, 'windows table: LLCI12CH values for 12 banks')


def test_refusals():
    for kw, step, words, label in (
            (dict(version=0x04010000), 'LL0', 'not 6.x', 'L1: version 4.1 refused'),
            (dict(banks=0), 'LL0', 'banks', 'L1/L11: zero banks refused'),
            (dict(banks=6), 'LL0', 'banks', 'L1: six banks refused')):
        fake = LlccFake(**kw)
        status, out = run(fake, 'llcc-activate')
        check(status == 2 and out['stopped']['step'] == step and words in out['stopped']['reason']
              and not llcc_log(fake, 'w'), f'{label}, nothing written')
    fake = LlccFake(serror_first_write=True)
    status, out = run(fake, 'llcc-activate')
    check(status == 3 and len(llcc_log(fake, 'w')) <= 1, f'L2: SError on the first write stops at once (exit {status})')
    fake = LlccFake(never_complete=True)
    status, out = run(fake, 'llcc-activate', slices='cpuss')
    acts = [a for a, _v in llcc_log(fake, 'w')]
    check(status == 2 and 0x20401008 not in acts, 'L3: no ACT_COMPLETE: stop, no ACT_CLEAR')
    fake = LlccFake(disagree=0x42040)
    status, out = run(fake, 'llcc-activate', slices='cpuss')
    check(status == 2 and 'differs between banks' in out['stopped']['reason'] and 0x20401000 not in
          [a for a, _v in llcc_log(fake, 'w')], 'L5: banks disagree on a shared bit word: refused before activation')
    fake = LlccFake()
    status, out = run(fake, 'llcc-activate', slices='cpuss,nope')
    check(status == 2 and not llcc_log(fake, 'w'), 'unknown slice name refused')


def test_gpu():
    with T3.synthetic_firmware() as fw:
        fake = LlccFake()
        status, out = run(fake, 'llcc-gpu')
        check(status != 0 and fake.gbif[0x03D0F008] == 0, 'L7: before the GPU is up: refused, nothing written')
        T3.happy_path(fake, fw)
        status, out = run(fake, 'llcc-gpu')
        check(status == 2 and 'not active' in out['stopped']['reason'] and fake.gbif[0x03D0F008] == 0,
              'L7: GPU slice not active: refused')
        check(run(fake, 'llcc-activate', slices='gpu,gpuhtw')[0] == 0, 'activate GPU/GPUHTW with the GPU up')
        status, out = run(fake, 'llcc-gpu')
        check(status == 0 and fake.gbif == {0x03D0F004: 9 << 22 | 9 << 16 | 9 << 10 | 1 << 8, 0x03D0F008: 0x09249249},
              f'llcc-gpu writes the SCID words ({out["stopped"]})')
        status, out = run(fake, 'llcc-gpu', clear=True)
        check(status == 0 and fake.gbif == {0x03D0F004: 0, 0x03D0F008: 0}, 'llcc-gpu --clear')
        no_violation(fake, 'llcc-gpu')


def test_cli():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    S3.add_parsers(sub)
    a = parser.parse_args(['llcc-activate', '--write', '--slices', 'cpuss', '--table', 'windows'])
    check(a.slices == 'cpuss' and a.table == 'windows' and a.write, 'CLI llcc-activate')
    check(S3.COMMANDS['llcc-activate'][1] and S3.COMMANDS['llcc-gpu'][1] and not S3.COMMANDS['llcc-survey'][1],
          'activate/gpu write; survey does not')


def main():
    for test in (test_registry_and_words, test_activate_deactivate, test_refusals, test_gpu, test_cli):
        test()
    print(f'PASS: {checks} a16gpu LLCC model checks (offline, no hardware)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
