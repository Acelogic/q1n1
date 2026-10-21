# SPDX-License-Identifier: MIT
"""Return the failed, never-executed hash call to its original EL2 caller.

The captured fault is an instruction permission abort at helper entry. The
default client skipped instructions in that same non-executable region; none
of the helper's prologue ran. Refuse any other fault/PC/LR before changing the
exception frame. No disk commands are issued by this recovery.
"""
from pathlib import Path
import json
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import q1n1proxy as qp

out = ROOT / 'build/full-install/nvme-hash-entry-recovery-20260925.json'
assert not out.exists()
failed = json.loads((ROOT / 'build/full-install/j700-cryptex-probe-20260925.json').read_text())
assert failed['stage'] == 'hash-sample' and not failed.get('writes')
window = int(failed['window']['base'], 0)
p = qp.connect('udp://en5')
p.resume_skip = False
try:
    info = p.bootinfo()
    address = info['exception']
    frame = p.read_exception(address)
    assert frame['esr'] == 0x8600000d and frame['el'] == 2
    assert window + 0x40000 <= frame['elr'] < window + 0x50000
    assert info['image_base'] <= frame['x30'] < info['image_base'] + info['image_size']
    record = dict(frame=frame, info=info, result='CAPTURED', disk_writes=False)
    out.write_text(json.dumps(record, indent=2) + '\n')
    marker = 0xffffffffffffffff
    p.write64(address, marker)
    p.write64(address + 8 * qp.EXCEPTION_FIELDS.index('elr'), frame['x30'])
    p.request(p.P_EXIT, qp.EXC_RET_HANDLED)
    echo, status, value = struct.unpack('<QqQ', p.link.reply(qp.REQ_PROXY, timeout=5))
    assert echo == p.P_CALL and status == 0 and value == marker
    p.nop()
    record.update(result='RECOVERED', parent_call_result=value)
    out.write_text(json.dumps(record, indent=2) + '\n')
    print('Recovered the faulted helper entry; proxy is responsive; no SSD writes.')
finally:
    p.link.close()
