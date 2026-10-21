#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""H3/H4 (full-install OPEN-QUESTIONS): read-only state of the NVMe function and the PCIe SMMUv3.

Reads only: the root port's and the endpoint's config header in ECAM (segment 5, the survey's
Acpi UID), BAR0's CAP/VS/CC/CSTS/AQA/ASQ/ACQ when Memory Space is on, and the SMMU's ID/control
registers plus the endpoint's stream table entry. SError strict mode is armed for the whole run.
"""
import json
import sys

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import q1n1proxy  # noqa: E402

ECAM = 0x7A0000000          # MCFG: segment 5, buses 0..1
SMMU = 0x15480000           # IORT node 0x174A, StreamID = segment << 16 | RID
SEGMENT = 5
out = {'reads': [], 'stopped': None}


def rd(p, addr, width=32, what=''):
    try:
        v = p.read64(addr) if width == 64 else p.read32(addr)
    except q1n1proxy.ProxyRemoteError as e:
        out['reads'].append({'addr': hex(addr), 'what': what, 'error': str(e)})
        raise
    out['reads'].append({'addr': hex(addr), 'what': what, 'value': hex(v)})
    return v


def header(p, base, label):
    words = [rd(p, base + off, what=f'{label} cfg+{off:#04x}') for off in range(0, 0x40, 4)]
    h = {'vendor': hex(words[0] & 0xFFFF), 'device': hex(words[0] >> 16),
         'command': hex(words[1] & 0xFFFF), 'status': hex(words[1] >> 16),
         'class': hex(words[2] >> 8), 'header_type': hex((words[3] >> 16) & 0x7F),
         'mse': bool(words[1] & 2), 'bme': bool(words[1] & 4)}
    if (words[3] >> 16) & 0x7F == 1:
        h.update(primary=words[6] & 0xFF, secondary=(words[6] >> 8) & 0xFF,
                 subordinate=(words[6] >> 16) & 0xFF, mem_window=hex(words[8]),
                 pref_window=hex(words[9]), pref_upper=(hex(words[10]), hex(words[11])))
    else:
        h['bars'] = [hex(w) for w in words[4:10]]
    # Walk the capability list for PCI Express (0x10): link status and device control.
    cap = words[13] & 0xFC if words[1] & (1 << 20) else 0
    seen = 0
    while cap and seen < 16:
        dw = rd(p, base + cap, what=f'{label} cap@{cap:#x}')
        if dw & 0xFF == 0x10:
            lnk = rd(p, base + cap + 0x10, what=f'{label} PCIe linkctl/sta')
            dev = rd(p, base + cap + 0x08, what=f'{label} PCIe devctl/sta')
            h['pcie'] = {'cap': hex(cap), 'link_status': hex(lnk >> 16),
                         'speed': (lnk >> 16) & 0xF, 'width': (lnk >> 20) & 0x3F,
                         'dll_active': bool(lnk & (1 << 29)), 'devctl': hex(dev & 0xFFFF)}
        cap = (dw >> 8) & 0xFC
        seen += 1
    return h, words


def main():
    p = q1n1proxy.connect(sys.argv[1] if len(sys.argv) > 1 else 'udp://en5')
    guard = p.serror_state()
    out['guard_before'] = {k: guard[k] for k in ('mechanism', 'flags')}
    p.serror_reset()
    p.serror_strict(True)
    try:
        rp, _ = header(p, ECAM, 'root port 0:0.0')
        out['root_port'] = rp
        bus = rp.get('secondary', 1)
        ep_base = ECAM + (bus << 20)
        ep, words = header(p, ep_base, f'endpoint {bus}:0.0')
        out['endpoint'] = ep
        rid = bus << 8
        out['stream_id'] = hex(SEGMENT << 16 | rid)
        bar = (words[4] & ~0xF) | (words[5] << 32 if (words[4] >> 1) & 3 == 2 else 0)
        out['bar0'] = hex(bar)
        if ep['mse'] and bar:
            nv = {}
            for off, name, w in ((0x00, 'CAP', 64), (0x08, 'VS', 32), (0x14, 'CC', 32), (0x1C, 'CSTS', 32),
                                 (0x24, 'AQA', 32), (0x28, 'ASQ', 64), (0x30, 'ACQ', 64)):
                nv[name] = hex(rd(p, bar + off, w, what=f'NVMe {name}'))
            cap = int(nv['CAP'], 16)
            nv['decoded'] = {'MQES': (cap & 0xFFFF) + 1, 'DSTRD': (cap >> 32) & 0xF,
                             'TO_500ms': (cap >> 24) & 0xFF, 'MPSMIN': 1 << (12 + ((cap >> 48) & 0xF)),
                             'CSS': hex((cap >> 37) & 0xFF),
                             'CC.EN': int(nv['CC'], 16) & 1, 'CSTS.RDY': int(nv['CSTS'], 16) & 1,
                             'CSTS.CFS': (int(nv['CSTS'], 16) >> 1) & 1}
            out['nvme'] = nv
        else:
            out['nvme'] = 'not read: Memory Space off or BAR0 unassigned'
        sm = {}
        for off, name, w in ((0x00, 'IDR0', 32), (0x04, 'IDR1', 32), (0x20, 'CR0', 32), (0x24, 'CR0ACK', 32),
                             (0x44, 'GBPA', 32), (0x80, 'STRTAB_BASE', 64), (0x88, 'STRTAB_BASE_CFG', 32)):
            sm[name] = hex(rd(p, SMMU + off, w, what=f'SMMU {name}'))
        cr0, gbpa = int(sm['CR0'], 16), int(sm['GBPA'], 16)
        cfg, base = int(sm['STRTAB_BASE_CFG'], 16), int(sm['STRTAB_BASE'], 16) & 0xFFFFFFFFFFC0
        sm['decoded'] = {'SMMUEN': cr0 & 1, 'GBPA.ABORT': (gbpa >> 20) & 1,
                         'FMT': (cfg >> 16) & 3, 'LOG2SIZE': cfg & 0x3F, 'SPLIT': (cfg >> 6) & 0x1F}
        if cr0 & 1:
            sid = SEGMENT << 16 | rid
            fmt, split = (cfg >> 16) & 3, (cfg >> 6) & 0x1F
            if fmt == 0:
                ste = base + sid * 64
            else:
                l1 = rd(p, base + (sid >> split) * 8, 64, what='SMMU L1STD')
                span = l1 & 0x1F
                l2 = l1 & 0xFFFFFFFFFFC0
                ste = l2 + (sid & ((1 << split) - 1)) * 64 if span else None
                sm['l1std'] = hex(l1)
            if ste:
                words = [rd(p, ste + 8 * i, 64, what=f'SMMU STE[{i}]') for i in range(4)]
                sm['ste'] = [hex(w) for w in words]
                sm['ste_decoded'] = {'V': words[0] & 1, 'CONFIG': (words[0] >> 1) & 7}
            else:
                sm['ste'] = 'L1 descriptor has no span: stream is not configured'
        out['smmu'] = sm
    except q1n1proxy.ProxyRemoteError as e:
        out['stopped'] = f'SError/abort: {e}'
    finally:
        after = p.serror_state()
        out['guard_after'] = {k: after[k] for k in after if k in ('before', 'during', 'unconsumed', 'last_when')}
        p.serror_strict(bool(guard['flags'] & q1n1proxy.SERROR_STRICT))
    print(json.dumps(out, indent=1))
    return 0 if out['stopped'] is None else 3


if __name__ == '__main__':
    sys.exit(main())
