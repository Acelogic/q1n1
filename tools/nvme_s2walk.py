#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only walk of the stage-2 table the firmware left on the NVMe stream (SID 0x50100).
4 KiB granule, 40-bit IPA, start level 1 (S2T0SZ 24, S2SL0 1), S2TTB 0x85396000 (h3h4-20260925)."""
import json, sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import q1n1proxy
TTB = 0x85396000
p = q1n1proxy.connect('udp://en5')
g = p.serror_state(); p.serror_reset(); p.serror_strict(True)
out = {'l1': {}, 'probe': {}}
def pte(addr): return p.read64(addr)
try:
    for i in range(1024):                       # 2 concatenated level-1 pages
        e = pte(TTB + 8 * i)
        if e & 1:
            kind = 'table' if e & 2 else 'block'
            info = {'raw': hex(e), 'kind': kind}
            if kind == 'table':
                l2 = e & 0xFFFFFFFFF000
                blocks = tables = 0; first = last = None
                for j in range(512):
                    f = pte(l2 + 8 * j)
                    if f & 1:
                        if f & 2: tables += 1
                        else: blocks += 1
                        first = j if first is None else first; last = j
                info.update(l2=hex(l2), l2_tables=tables, l2_blocks=blocks,
                            ipa_span=[hex((i << 30) + (first << 21)), hex((i << 30) + ((last + 1) << 21) - 1)] if first is not None else None)
            out['l1'][hex(i << 30)] = info
    for ipa in (0xc6413000, 0xc6414000, 0xb0000000, 0xb83d8000, 0xae9000000, 0x8c0000000):
        e = pte(TTB + 8 * (ipa >> 30)); path = [hex(e)]
        if e & 3 == 3:
            e = pte((e & 0xFFFFFFFFF000) + 8 * ((ipa >> 21) & 511)); path.append(hex(e))
            if e & 3 == 3:
                e = pte((e & 0xFFFFFFFFF000) + 8 * ((ipa >> 12) & 511)); path.append(hex(e))
        oa = (e & 0xFFFFFFFFF000) if e & 1 else None
        out['probe'][hex(ipa)] = {'path': path, 'valid': bool(e & 1), 'oa_page': hex(oa) if oa is not None else None}
except q1n1proxy.ProxyRemoteError as ex:
    out['stopped'] = str(ex)
finally:
    a = p.serror_state(); out['guard_after'] = {k: a[k] for k in ('before', 'during', 'unconsumed')}
    p.serror_strict(bool(g['flags'] & q1n1proxy.SERROR_STRICT))
print(json.dumps(out, indent=1))
