#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The native hash helper against Python's hashlib (platform/uefi/nvme-hash.c).

1. Builds the real helper (tools/nvme_hash.py) and checks its code: the ARMv8
   SHA2 instructions are there, vector registers appear only where that block
   function was inlined, and it reads the ID/trap registers but never writes a
   system register or calls a higher exception level. The helper it replaces
   must still rebuild, from commit 3f2966d, to the production 3ac12420...
   (tools/nvme_hash_compare.py runs the two side by side on the A16).
2. Compiles tools/test-nvme-hash.c, which includes the helper source, natively
   with ASan/UBSan and again optimised, and runs it on this arm64 host's own
   SHA2 instructions: NIST FIPS 180 vectors, every length through 300 bytes and
   random lengths to 1 MiB, split updates that cross between the SHA2 and
   portable paths, the block function from random states, and the path choice
   for each register state; then complete digests through a modelled NVMe
   queue (in-order, reversed and shuffled completions, 512- and 4096-byte
   LBAs, 1 to 64 MiB per call, odd ranges, queue and command-identifier wrap,
   restore overlays, and one injected failure per error case).

Every expected digest is hashlib's. No hardware.

    python3 tools/test-nvme-hash.py
"""
import hashlib
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import nvme_hash                                              # noqa: E402

LLVM = nvme_hash.LLVM
MIB = 1 << 20
NIST = [  # FIPS 180-2 appendix B and the empty message
    (b'abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'),
    (b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
    (b'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq',
     '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'),
    (b'abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrs'
     b'mnopqrstnopqrstu', 'cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1'),
    (b'a' * 1000000, 'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0'),
]
ORDER = dict(inorder='IN_ORDER', reversed='REVERSED', shuffled='SHUFFLED')
INJECT = dict(none='NONE', status='BAD_STATUS', cid='FOREIGN_CID', sqid='WRONG_SQID', fatal='FATAL', stall='STALL')


def check(condition, label):
    if not condition:
        raise SystemExit(f'FAIL: {label}')


def helper_code():
    """Build the helper and audit its instructions, function by function."""
    record = nvme_hash.build()
    text = subprocess.check_output([str(LLVM / 'llvm-objdump'), '-d', '--no-show-raw-insn',
                                    str(nvme_hash.BUILD / 'nvme-hash.elf')], text=True)
    functions, name = {}, None
    for line in text.splitlines():
        head = re.match(r'^[0-9a-f]+ <(\S+)>:', line)
        if head:
            name = head.group(1)
            functions[name] = []
        elif name and re.match(r'^\s+[0-9a-f]+:', line):
            functions[name].append(line.split(':', 1)[1].strip())
    code = [i for body in functions.values() for i in body]
    mnemonics = {i.split()[0] for i in code if i.split()}
    for needed in ('sha256h', 'sha256h2', 'sha256su0', 'sha256su1'):
        check(needed in mnemonics, f'helper has {needed}')
    for register in ('id_aa64isar0_el1', 'id_aa64pfr0_el1', 'hcr_el2', 'cptr_el2', 'currentel'):
        check(any(i.startswith('mrs') and register in i.lower() for i in code), f'helper reads {register}')
    check(not mnemonics & {'msr', 'svc', 'hvc', 'smc', 'eret', 'sys', 'sysl'},
          f'no system-register write or exception call: {sorted(mnemonics & {"msr", "svc", "hvc", "smc", "eret"})}')
    vector = re.compile(r'\b[vqds]\d+\b')
    for fn, body in functions.items():
        uses = [i for i in body if vector.search(i.split(None, 1)[1] if ' ' in i else '')]
        if uses:
            check(any(i.split()[0] == 'sha256h' for i in body),
                  f'{fn} uses vector registers outside the SHA2 block function: {uses[:3]}')
    check(functions and next(iter(functions)) == 'nvme_hash_run', 'entry is first')
    import nvme_hash_compare
    check(hashlib.sha256(nvme_hash_compare.replaced_helper()).hexdigest() == nvme_hash_compare.REPLACED_SHA256,
          'the replaced helper rebuilds byte for byte')
    print(f'helper {record["binary_sha256"]} ({record["bytes"]} bytes): SHA2 instructions, ID/trap reads, '
          f'no system-register writes; vector registers only in {sum(1 for b in functions.values() if any(i.split()[0] == "sha256h" for i in b))} '
          'inlined SHA2 block copies')
    return record


def vectors(rng):
    messages = [m for m, _ in NIST]
    messages += [bytes(rng.getrandbits(8) for _ in range(n)) for n in range(0, 301)]
    messages += [rng.randbytes(n) for n in (511, 512, 513, 4095, 4096, 4097, 65535, 65536, 65537, 262144, MIB)]
    messages += [rng.randbytes(rng.randrange(1, MIB)) for _ in range(40)]
    blob, table = bytearray(), []
    for m in messages:
        table.append((len(blob), len(m), hashlib.sha256(m).hexdigest()))
        blob += m
    for (m, want), (_, _, got) in zip(NIST, table):
        check(want == got, 'hashlib agrees with FIPS 180-2')
    return bytes(blob), table


def disk_image(rng):
    """81 MiB: random, zeros (a deallocated range reads so), a block-numbered
    pattern and random again, so no chunk is a shifted copy of another."""
    parts = [rng.randbytes(20 * MIB), bytes(16 * MIB),
             b''.join(i.to_bytes(8, 'little') * 64 for i in range(14 * MIB // 512)), rng.randbytes(31 * MIB)]
    return b''.join(parts)


def main():
    rng = random.Random(20260928)
    record = helper_code()
    blob, table = vectors(rng)
    disk = disk_image(rng)
    size = len(disk)
    payload = bytearray()
    overlays = []                                            # (disk_byte, length, payload offset)

    def overlay_set(rows):
        first = len(overlays)
        for at, length in rows:
            overlays.append((at, length, len(payload)))
            payload.extend(rng.randbytes(length))
        return first, len(rows)

    def expected(lba_bytes, first, count, rows=None):
        data = bytearray(disk[first * lba_bytes:(first + count) * lba_bytes])
        begin = first * lba_bytes
        for at, length, off in (overlays[rows[0]:rows[0] + rows[1]] if rows else ()):
            lo, hi = max(at, begin), min(at + length, begin + len(data))
            if lo < hi:
                data[lo - begin:hi - begin] = payload[off + lo - at:off + hi - at]
        return hashlib.sha256(data).hexdigest()

    cases = []

    def case(name, lba_bytes=512, first=0, count=None, call=16 * MIB, transfer=256 << 10, sha2=1,
             order='inorder', queue=(0, 0, 1, 1), rows=None, inject='none', at=0, status=0):
        count = size // lba_bytes - first if count is None else count
        digest = expected(lba_bytes, first, count, rows) if not status else ''
        cases.append((name, lba_bytes, first, count, call // lba_bytes, transfer,
                      sha2, order, queue, rows or (0, 0), inject, at, status, digest))

    case('full-sha2')
    case('full-portable', sha2=0)
    case('full-sha2-reversed', order='reversed')
    case('full-sha2-shuffled', order='shuffled')
    case('full-mixed-paths-shuffled', sha2=2, order='shuffled')
    case('lba4096-sha2-shuffled', lba_bytes=4096, order='shuffled')
    case('lba4096-portable-reversed', lba_bytes=4096, sha2=0, order='reversed')
    case('odd-range', first=7, count=3 * 32768 + 5, order='shuffled')
    case('odd-range-portable', first=7, count=3 * 32768 + 5, sha2=0, order='reversed')
    case('64mib-calls', call=64 * MIB, order='reversed')
    case('1mib-calls', first=1234, count=10 * MIB // 512 + 3, call=MIB, order='shuffled')
    case('call-not-transfer-multiple', call=16 * MIB + 512, count=40 * MIB // 512)
    for blocks in (1, 8, 9, 16, 17, 511, 512, 513):
        case(f'tiny-{blocks}', first=100003, count=blocks, order='shuffled')
    for transfer in (4096, 8192, 12288, 131072):
        case(f'transfer-{transfer}', first=41, count=3 * MIB // 512 + 1, transfer=transfer, order='shuffled')
    case('queue-and-cid-wrap', count=20 * MIB // 512, queue=(60, 60, 0, 65530), order='reversed')
    case('queue-wrap-portable', count=5 * MIB // 512, queue=(63, 63, 1, 65535), sha2=0, order='shuffled')
    boundary = overlay_set([(100, 4096), ((256 << 10) - 1000, 3000), (16 * MIB - 2048, 8192),
                            (21 * MIB + 3, 777), (size - 5000, 5000), (40 * MIB, 262144 + 512)])
    case('overlays', rows=boundary, order='shuffled')
    case('overlays-portable', rows=boundary, sha2=0, order='reversed')
    case('overlays-subrange', first=(16 * MIB - 4096) // 512, count=3 * MIB // 512, rows=boundary,
         order='shuffled')
    spread = overlay_set([(rng.randrange(0, size // 4096) * 4096, 4096) for _ in range(256)])
    case('overlays-256-rows', rows=spread, order='shuffled')
    case('overlays-lba4096', lba_bytes=4096, rows=boundary, order='reversed')
    for kind, at, status in (('status', 5, 5), ('cid', 3, 4), ('sqid', 0, 4), ('fatal', 2, 2), ('stall', 1, 3)):
        case(f'error-{kind}', count=16 * MIB // 512, inject=kind, at=at, status=status, order='inorder')
    case('error-status-late', count=48 * MIB // 512, inject='status', at=130, status=5, order='shuffled')
    bad = overlay_set([(size + 4096, 512)])
    case('error-overlay-outside', count=16 * MIB // 512, rows=bad, status=6)

    with tempfile.TemporaryDirectory(prefix='nvme-hash-test-') as scratch:
        scratch = Path(scratch)
        (scratch / 'disk.img').write_bytes(disk)
        (scratch / 'overlays.bin').write_bytes(bytes(payload))
        (scratch / 'vectors.bin').write_bytes(blob)
        lines = ['static const struct vector_spec VECTORS[] = {']
        lines += [f'    {{ {o}u, {n}u, "{h}" }},' for o, n, h in table]
        lines += ['};', 'static const struct overlay_spec OVERLAYS[] = {']
        lines += [f'    {{ {a}u, {n}u, {p}u }},' for a, n, p in overlays]
        lines += ['};', 'static const struct case_spec CASES[] = {']
        for (name, lba_bytes, first, count, call, transfer, sha2, order, (tail, head, phase, cid), (r0, rn),
             inject, at, status, digest) in cases:
            lines.append(f'    {{ "{name}", {lba_bytes}, {first}u, {count}u, {call}u, {transfer}u, {sha2}, '
                         f'{ORDER[order]}, {tail}, {head}, {phase}, {cid}, {r0}, {rn}, {INJECT[inject]}, {at}, '
                         f'{status}, "{digest}" }},')
        lines.append('};')
        (scratch / 'cases.h').write_text('\n'.join(lines) + '\n')
        builds = [('sanitized', ['-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all']),
                  ('optimised', ['-O2'])]
        for label, flags in builds:
            binary = ROOT / f'build/test-nvme-hash-{label}'
            binary.parent.mkdir(exist_ok=True)
            subprocess.run(['clang', '-std=gnu11', *flags, '-Wall', '-Wextra', '-Werror', '-pthread',
                            '-I' + str(ROOT / 'platform/uefi'), '-I' + str(scratch),
                            str(ROOT / 'tools/test-nvme-hash.c'), str(ROOT / 'platform/uefi/sha256.c'),
                            '-o', str(binary)], check=True)
            print(f'--- {label} build')
            result = subprocess.run([str(binary), str(scratch / 'disk.img'), str(scratch / 'overlays.bin'),
                                     str(scratch / 'vectors.bin')], text=True, capture_output=True)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            check(result.returncode == 0 and result.stdout.rstrip().splitlines()[-1].startswith('PASS'),
                  f'{label} harness')
    print(f'PASS: helper {record["binary_sha256"]}, {len(table)} SHA-256 vectors, {len(cases)} queue cases, '
          'sanitized and optimised')


if __name__ == '__main__':
    main()
