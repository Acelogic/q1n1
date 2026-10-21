#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Boot the real EFI binary in QEMU; inspect serial and capture its framebuffer.

QEMU validates the UEFI target, not Qualcomm Secure Launch or the GENI driver.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import socket
import struct
import subprocess
import time
import hashlib
import preload


def host_shift(host):
    return host << 16


def synthetic_smem(size=0x10000, host=19):
    """A minimal SMEM image (Linux smem.c layout) with an APPS<->host partition
    holding GLINK items 478/479/480 and the SoCCP SMP2P record 617."""
    image = bytearray(size)
    struct.pack_into('<I', image, 0xc0, 1)                 # initialized
    struct.pack_into('<I', image, 0x5c, 11 << 16)          # heap version 11
    toc = size - 0x1000
    part, part_size, cacheline = 0x1000, 0x8000, 64
    image[toc:toc + 4] = b'$TOC'
    struct.pack_into('<III', image, toc + 4, 1, 2, 0)
    struct.pack_into('<IIIHHI', image, toc + 32, part, part_size, 0, 0, host, cacheline)
    # Another pair's partition (ADSP<->host), listed but never to be opened:
    # it has no $PRT header, so reading it would be a structure error.
    struct.pack_into('<IIIHHI', image, toc + 80, 0x9000, 0x1000, 0, 2, host, cacheline)
    image[toc + 128:toc + 132] = b'SIII'
    struct.pack_into('<H', image, toc + 144, 1024)
    free_uncached, free_cached, items = 32, part_size, {}

    def uncached(item, length):
        nonlocal free_uncached
        struct.pack_into('<HHIHH', image, part + free_uncached, 0xa5a5, item, length, 0, 0)
        items[item] = part + free_uncached + 16
        free_uncached += 16 + length

    def cached(item, length):
        nonlocal free_cached
        header = free_cached - ((16 + cacheline - 1) & ~(cacheline - 1))
        struct.pack_into('<HHIHH', image, part + header, 0xa5a5, item, length, 0, 0)
        free_cached = header - length
        items[item] = part + free_cached

    uncached(478, 32)
    uncached(480, 0x400)
    uncached(617, 20 + 16 * 20)
    cached(479, 0x400)
    smp2p = items[617]
    struct.pack_into('<IBxxxHHHHI', image, smp2p, 0x504d5324, 1, host, 0, 16, 1, 0)
    image[smp2p + 20:smp2p + 36] = b'slave-kernel'.ljust(16, b'\0')
    struct.pack_into('<I', image, smp2p + 36, 2)            # ready
    struct.pack_into('<IIII', image, items[478], 0x40, 0x40, 0x18, 0x18)
    image[part:part + 4] = b'$PRT'
    struct.pack_into('<HHIII', image, part + 4, 0, host, part_size, free_uncached, free_cached)
    return bytes(image), items


NVME_EUI = '002538F261C3C291'
NVME_DISK_GUID = '1c98f6ba-8bab-4cae-8c35-bf82870d8cf2'


def nvme_gpt_image(path, blocks=131072, bs=512):
    """A 64 MiB disk with a real GPT (both copies) for the NVMe survey fixture."""
    import uuid, zlib
    def entry(t, g, first, last, name):
        return (uuid.UUID(t).bytes_le + uuid.UUID(g).bytes_le + struct.pack('<QQQ', first, last, 0) +
                name.encode('utf-16le').ljust(72, b'\0'))
    parts = [entry('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', '939a0a66-c61e-42b7-b975-5be3c2b80280', 2048, 20479, 'esp'),
             entry('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', '419baed0-542c-4292-a670-caaabe34e2fa', 20480, 61439, 'c'),
             entry('de94bba4-06d1-4d40-a16a-bfd50179d6ac', '0e71a636-b076-4ab6-9f50-6425f2a618a5', 102400, 122879, 're')]
    array = b''.join(parts).ljust(128 * 128, b'\0')
    last, ab = blocks - 1, len(array) // bs
    fu, lu, crc = 2 + ab, blocks - ab - 2, zlib.crc32(array)
    def header(mine, other, at):
        h = bytearray(struct.pack('<8sIIIIQQQQ16sQIII', b'EFI PART', 0x10000, 92, 0, 0, mine, other, fu, lu,
                                  uuid.UUID(NVME_DISK_GUID).bytes_le, at, 128, 128, crc))
        struct.pack_into('<I', h, 16, zlib.crc32(h))
        return bytes(h)
    img = bytearray(blocks * bs)
    # Recognisable, non-zero filler so a stray write anywhere changes the hash.
    for off in range(0, len(img), 4096):
        img[off:off + 16] = b'SNAPSURVEYFILLER'
    img[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF, 1, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF])
    img[510:512] = b'\x55\xaa'
    img[bs:2 * bs] = header(1, last, 2).ljust(bs, b'\0')
    img[2 * bs:2 * bs + len(array)] = array
    img[(last - ab) * bs:(last - ab) * bs + len(array)] = array
    img[last * bs:(last + 1) * bs] = header(last, 1, last - ab).ljust(bs, b'\0')
    path.write_bytes(img)
    return hashlib.sha256(img).hexdigest()


def nvme_survey_checks(check, gpt_survey, survey_disks):
    nv = [(d, dec, t, i) for d, dec, t, i in survey_disks if d['flags'] & 128]
    check(len(nv) == 1, f'NVMe fixture: exactly one NVMe disk recorded ({len(nv)})')
    d, dec, text, info = nv[0]
    check(not dec['errors'] and dec['disk_guid'] == NVME_DISK_GUID and len(dec['partitions']) == 3,
          f'NVMe fixture: GPT decoded, both CRCs, 3 partitions ({dec["errors"]})')
    check(d['reads'] == 5 and d['flags'] & 31 == 31, f'NVMe fixture: exactly five reads ({d["reads"]})')
    class A: expect_bytes = None; expect_disk_guid = NVME_DISK_GUID; expect_eui = 'eui.' + NVME_EUI
    problems = gpt_survey.check_expectations(dec, info, A)
    check(not problems, f'NVMe fixture: EUI64 pin matches ({problems}), order {dec.get("eui_order")}, path {text}')
    return dec.get('eui_order')


def run_proxy_checks(socket_path, build_dir, preload_payload=None, nvme=False):
    """Drive the post-ExitBootServices proxy over QEMU's PL011 with the real client."""
    spec = importlib.util.spec_from_file_location('q1n1proxy', Path(__file__).resolve().parent / 'q1n1proxy.py')
    assert spec and spec.loader
    q1n1proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q1n1proxy)
    done = []

    def check(condition, label):
        if not condition:
            raise RuntimeError(f'proxy check failed: {label}')
        done.append(label)

    for attempt in range(40):
        try:
            link = q1n1proxy.Link(socket_path, timeout=10)
            break
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.25)
    else:
        raise RuntimeError('could not connect to the QEMU proxy socket')
    proxy = q1n1proxy.Proxy(link)
    link.nop(0)
    check(proxy.nop() is not None, 'P_NOP')
    info = proxy.bootinfo()
    check(info['magic'] == q1n1proxy.MAGIC, 'bootinfo magic')
    check(info['current_el'] == 2 and info['entry_el'] == 2, 'proxy runs at EL2 after ExitBootServices')
    check(info['mode'] == 2, 'UART proxy mode reported')
    check(info['heap_base'] and info['memory_map'] and info['fb_base'] and info['timer_hz'],
          'bootinfo heap, memory map, framebuffer and timer')

    import gpt_survey
    survey_header, survey_disks = gpt_survey.decode_record(gpt_survey.fetch(proxy))
    check(survey_header['phase'] == 4 and survey_header['recorded'] >= 1 and
          all(d['reads'] <= 5 and d['flags'] & 1 for d, _, _, _ in survey_disks),
          'read-only GPT survey recorded the QEMU disk through firmware Block I/O '
          f"({survey_header['handles']} handles, {survey_header['recorded']} disk(s), "
          f"flags {[','.join(d['flag_names']) for d, _, _, _ in survey_disks]}, "
          f"paths {[t for _, _, t, _ in survey_disks]})")
    if nvme:
        nvme_survey_checks(check, gpt_survey, survey_disks)

    heap = info['heap_base']
    cache = preload.resident_cache(proxy, info)
    preload_status = preload.diagnostics(proxy, info)
    if preload_payload is None:
        check(not cache, 'no manifest leaves cache absent and proxy usable')
        check(preload_status['phase'] == 2 and preload_status['manifests'] == 0,
              'diagnostics distinguish an absent manifest')
    else:
        check(preload_status['phase'] == 7 and preload_status['manifests'] == 1
              and preload_status['read_bytes'] == len(preload_payload),
              'diagnostics identify the completed SSD load and byte count')
        check(len(cache) == 1 and cache[0]['sha256'] == hashlib.sha256(preload_payload).hexdigest(),
              'EFI loaded and hashed the fixture from its filesystem')
        check(proxy.readmem(cache[0]['base'], len(preload_payload)) == preload_payload,
              'preloaded file bytes survive ExitBootServices')
        check(proxy.call(info['preload_verify'], 0) == 1, 'resident cache hash verifies')
        proxy.write8(cache[0]['base'], preload_payload[0] ^ 1)
        check(proxy.call(info['preload_verify'], 0) == 0, 'resident verifier rejects changed data')
        proxy.write8(cache[0]['base'], preload_payload[0])
        check(proxy.call(info['preload_verify'], 1) == 0, 'resident verifier rejects invalid index')
        destination = next(lo for kind, lo, hi in preload.memory_regions(proxy, info)
                           if kind == 7 and hi-lo >= len(preload_payload))
        preload.copy_cached(proxy, info, cache[0], destination, len(preload_payload))
        check(proxy.readmem(destination, len(preload_payload)) == preload_payload,
              'host cache-copy path reproduces the complete fixture in free RAM')
    payload = random.randbytes(128 * 1024)
    proxy.writemem(heap, payload)
    check(proxy.readmem(heap, len(payload)) == payload, '128 KiB memory round trip')
    proxy.write32(heap, 0xA5A5A5A5)
    check(proxy.read32(heap) == 0xA5A5A5A5, 'register write/read')

    # An unmapped VA above the 48-bit range: the guard must convert the fault.
    bad = 1 << 48
    proxy.set_exc_guard(q1n1proxy.GUARD_MARK)
    check(proxy.read32(bad) & 0xffffffff == q1n1proxy.GUARD_MARKER & 0xffffffff, 'GUARD_MARK marks a faulting read')
    check(proxy.get_exc_count() == 1, 'guarded fault counted')
    proxy.set_exc_guard(q1n1proxy.GUARD_OFF)
    try:
        proxy.readmem(bad, 64)
        check(False, 'memread of a bad address is refused')
    except q1n1proxy.ProxyRemoteError:
        check(True, 'memread of a bad address is refused')
    check(proxy.nop() is not None, 'proxy survives the guarded memread fault')

    # SError bracketing: ESB, DISR_EL1 and ISR_EL1 run around every access.
    # QEMU raises no SErrors, so this shows the instructions execute at EL2 and
    # nothing is invented; attribution is covered by tools/test-q1n1-proxy.py.
    # Reading the record itself is not bracketed (q1n1-proxy.h), so it adds nothing.
    serror = proxy.serror_state()
    check(serror['mechanism'] == 2, f"SError mechanism {serror['mechanism']} (2: ESB + DISR_EL1)")
    check(serror['checks'] >= 10 and serror['before'] == serror['during'] == serror['unconsumed'] == 0,
          f"every access bracketed ({serror['checks']} checks), no SError found")
    proxy.serror_strict(True)
    check(proxy.read32(heap) == 0xA5A5A5A5, 'strict SError mode: a clean access succeeds')
    proxy.serror_strict(False)

    # Upload and run code, then upload code that faults with no guard: the
    # target reports the exception over the same link and resumes.
    code = struct.pack('<3I', 0xD2824680, 0xD65F03C0, 0xD503201F)  # movz x0,#0x1234; ret; nop
    proxy.writemem(heap + 0x1000, code)
    proxy.flush_code(heap + 0x1000, len(code))
    check(proxy.call(heap + 0x1000) == 0x1234, 'P_CALL runs uploaded code')
    faulting = struct.pack('<3I', 0xD2E00020, 0xF9400000, 0xD65F03C0)  # movz x0,#1,lsl#48; ldr x0,[x0]; ret
    proxy.writemem(heap + 0x2000, faulting)
    proxy.flush_code(heap + 0x2000, len(faulting))
    returned = proxy.call(heap + 0x2000, timeout=20)
    check(len(proxy.exceptions) == 1, 'unguarded exception reported to the host')
    check(proxy.exceptions[0]['el'] == 2 and proxy.exceptions[0]['far'] == 1 << 48,
          'exception report carries EL and FAR')
    check(returned == 1 << 48, 'target resumed past the faulting instruction')

    # The EL2 timer tick: interrupts taken at EL2 through the GICv3, which is
    # what will service the link once a guest owns the CPU.
    def gic_stats():
        fields = struct.unpack('<9Q5I', proxy.readmem(info['gic_stats'], 92, live=True))
        names = 'distributor redistributor sgi interval ticks spurious other serviced skipped'.split()
        names += 'intid last_intid rate ready status'.split()
        return dict(zip(names, fields))

    before = gic_stats()
    check(info['gic_distributor'] and info['gic_redistributor'],
          f'GIC bases from the MADT (init status {before["status"]}, rsdp {info["acpi_rsdp"]:#x}, '
          f'gicd {info["gic_distributor"]:#x}, gicr {info["gic_redistributor"]:#x})')
    check(before['ready'] == 1 and before['intid'] == 26, 'EL2 physical timer PPI armed (INTID 26)')
    time.sleep(1.0)
    after = gic_stats()
    delta = after['ticks'] - before['ticks']
    check(delta > 100, f'timer interrupts are firing at EL2 ({delta} ticks in ~1 s, rate {after["rate"]})')
    check(after['last_intid'] == 26 and after['spurious'] == 0 and after['other'] == 0,
          f'only the timer PPI arrives (last {after["last_intid"]}, spurious {after["spurious"]}, other {after["other"]})')

    # Live battery: QEMU has no SMEM, SoCCP or IPCC, so the stage must refuse
    # at the SMEM step -- a guarded fault or a header that is not SMEM -- with
    # no unguarded exception, and the proxy must keep answering.
    check(info['battery_call'] and info['battery_result'] and info['battery_session'],
          'bootinfo publishes the live battery entry, record and session')
    for flags, label in ((q1n1proxy.LIVE_SURVEY, 'survey'), (0, 'read')):
        status, record = proxy.live_battery(flags, 200)
        check(status in (-2, -3, -4, -10) and record['phase'] == 2 and record['status'] == status,
              f'live battery {label} refuses on QEMU at the SMEM step (status {status}, '
              f'SMEM {record["smem_base"]:#x}, target pointer {record["target_pointer"]:#x})')
        check(record['doorbells'] == 0 and record['restarts'] == 0, f'live battery {label}: no doorbell, no restart')
    check(proxy.nop() is not None and not proxy.exceptions[1:], 'proxy survives the live battery refusals')
    # The same code on real AArch64 page tables, against an SMEM image in the
    # heap: it must find the partition, the GLINK items and the SMP2P record
    # through guarded reads, then stop at the IPCC frame QEMU does not have.
    smem, items = synthetic_smem()
    smem_base = (heap + 0x200000) & ~0xfff
    proxy.writemem(smem_base, smem)
    status, record = proxy.live_battery(q1n1proxy.LIVE_SURVEY, 200, 0, smem_base, len(smem))
    check(status == -10 and record['phase'] == 6 and record['smem_status'] == 0 and record['smem_version'] == 11,
          f'live battery parses an SMEM image on AArch64 and stops at the IPCC frame (status {status}, '
          f'phase {record["phase"]}, IPCC {record["ipcc_version"]:#x})')
    check(record['partition_offset'] == 0x1000 and record['desc_offset'] == items[478]
          and record['tx_offset'] == items[479] and record['tx_cached'] == 1 and record['rx_length'] == 0x400,
          'live battery finds 478/479/480 (479 on the cached list)')
    check(record['desc0'] == 0x40 and record['desc2'] == 0x18 and record['smp2p_value'] == 2
          and record['smp2p_status'] == 0, 'live battery reads the descriptor and SMP2P ready bit')
    check(record['smem_attr'] != 0xFFFFFFFFFFFFFFFF,
          f'live battery reads the EL2 page tables (heap MAIR attribute {record["smem_attr"]:#x})')
    check(record['partitions'] == 2 and record['partition'][1][0] == (2 | host_shift(19)),
          'live battery lists the foreign partition from the table alone')
    check(proxy.readmem(smem_base, len(smem)) == smem, 'survey wrote nothing')
    status, record = proxy.live_battery(0, 200, 0, smem_base, len(smem))
    check(status == -10 and record['phase'] == 6 and proxy.readmem(smem_base, len(smem)) == smem,
          'a read refuses before any write when the doorbell is unreachable')
    status, record = proxy.live_battery(q1n1proxy.LIVE_RELEASE, 200, 0, smem_base, len(smem))
    check(status == -10 and record['phase'] == 6 and proxy.readmem(smem_base, len(smem)) == smem,
          'a release refuses before any write when the doorbell is unreachable')
    status, record = proxy.live_battery(q1n1proxy.LIVE_LINUX | q1n1proxy.LIVE_RESYNC, 200, 0, smem_base, len(smem))
    check(status == -9 and record['phase'] == 0 and proxy.readmem(smem_base, len(smem)) == smem,
          'two restart modes are refused before anything is read')
    status, record = proxy.live_battery(q1n1proxy.LIVE_FORCE, 200, 0, smem_base, len(smem))
    check(status == -9 and record['phase'] == 0, 'force without linux/drain is refused before anything is read')

    # Adreno Stage 3 (gpu-el2.h): the ESP reserved the GPU arena, gpu_call
    # answers, the allow-list refuses before any SMC, and the one allowed SMC
    # reaches a real SMC instruction: QEMU has no EL3, so it is either UNDEFINED
    # (stepped over under the guard: -7) or answered by QEMU's own PSCI conduit.
    check(info['gpu_call'] and info['gpu_state'] and info['gpu_arena_size'] == 32 << 20
          and info['gpu_arena_flags'] == 1 and info['gpu_arena_base'] + (32 << 20) <= 1 << 32,
          f'bootinfo publishes gpu_call and a 32 MiB EFI arena below 4 GiB at {info["gpu_arena_base"]:#x}')

    def gpu_call(*args):
        return q1n1proxy.signed64(proxy.call(info['gpu_call'], *args))

    def gpu_record():
        return struct.unpack('<24Q', proxy.readmem(info['gpu_state'], 24 * 8))
    check(gpu_call(0) == info['gpu_state'] and gpu_record()[0] == 0x31555047314E3151, 'gpu_call query: the record')
    check(gpu_call(2, 0x42000C1B, 4, 0, 0) == -6 and gpu_record()[10] == 1 and gpu_record()[9] == 0,
          'gpu_call refuses arguments that are not allow-listed (the aperture call for CB0 needs 0xffff0000, '
          '0xffffffff), before any SMC')
    smc_status = gpu_call(2, 0x42002801, 1, 9, 0)
    record = gpu_record()
    check((smc_status == -7 and record[23] == 1) or (smc_status == 0 and record[9] == 1),
          f'the allowed SMC runs guarded (status {smc_status}, x0 {record[17]:#x}, undefined {record[23]})')
    check(proxy.nop() is not None and not proxy.exceptions[1:], 'proxy survives the SMC without an exception report')
    check(gpu_call(1, 32 << 20, 1 << 32, 0, 0) == 0 and gpu_record()[3] == info['gpu_arena_base'],
          'a claim finds the EFI arena (idempotent)')
    arena = info['gpu_arena_base']
    check(arena % (2 << 20) == 0, f'the EFI arena is 2 MiB aligned ({arena:#x})')
    check(gpu_record()[1] == 3, 'gpu_state version 3 (GPU_STORE32, zeroed arenas, the CP aperture SMC)')
    check(proxy.readmem(arena, 4096) == bytes(4096) and proxy.readmem(arena + (32 << 20) - 4096, 4096) == bytes(4096),
          'the EFI arena starts zeroed (no record survives a warm reset)')
    check(gpu_call(4, arena + 0x114044, 0x12345678, 0, 0) == 0
          and struct.unpack('<I', proxy.readmem(arena + 0x114044, 4))[0] == 0x12345678
          and proxy.readmem(arena + 0x114040, 4) == bytes(4),
          'GPU_STORE32 stores one word inside the arena and leaves its neighbours alone')
    check(gpu_call(4, arena + (32 << 20), 1, 0, 0) == -1 and gpu_call(4, arena - 4, 1, 0, 0) == -1
          and gpu_call(4, arena + 2, 1, 0, 0) == -1, 'GPU_STORE32 refuses outside the arena and unaligned')
    check(gpu_call(4, arena + 0x114044, 0, 0, 0) == 0 and proxy.nop() is not None and not proxy.exceptions[1:],
          'proxy survives GPU_STORE32 without an exception report')

    # Out-of-band call service for a long P_CALL (call-service.h): published,
    # callable, and on QEMU's UART proxy with no NCM link it services nothing.
    call_fields = ('magic version size armed session arms disarms polls datagrams queries stops '
                   'malformed refused replies reply_failures dropped_proxy latched taken').split()

    def call_state(address):
        return dict(zip(call_fields, struct.unpack(f'<{len(call_fields)}Q',
                                                   proxy.readmem(address, 8 * len(call_fields)))))

    def service(entry, *args):
        return q1n1proxy.signed64(proxy.call(entry, *args))
    check(info['call_service'] and info['call_service_state'], 'bootinfo publishes the call service')
    state = call_state(info['call_service_state'])
    check(state['magic'] == 0x4c4c4143314e3151 and state['version'] == 1 and state['size'] == 32 * 8
          and not state['armed'], 'call-service record: magic, version, size, disarmed')
    check(service(info['call_service'], 1, 0) == -2 and service(info['call_service'], 9) == -1,
          'call service refuses session 0 and an unknown operation')
    latch = heap + 0x3000
    proxy.writemem(latch, bytes(112))
    check(service(info['call_service'], 1, 0x5eed) == 0 and service(info['call_service'], 2, latch, 112) == 0
          and proxy.readmem(latch, 112) == bytes(112) and service(info['call_service'], 3) == 0,
          'ARM, POLL (nothing latched, nothing written) and DISARM')
    state = call_state(info['call_service_state'])
    check(not state['armed'] and state['session'] == 0x5eed and state['arms'] == 1 and state['polls'] == 1
          and state['disarms'] == 1 and not state['latched'], 'call-service record counts the calls')
    check(proxy.nop() is not None and not proxy.exceptions[1:], 'proxy survives the call service')

    # Chainload: upload a flat stage, jump to it, and prove the new code is the
    # one answering (it inherits bootinfo and increments stage_generation).
    stage = build_dir / 'q1n1-stage-qemu.bin'
    check(info['stage_base'] and info['stage_size'], f'stage region reserved at {info["stage_base"]:#x}')
    check(stage.exists(), 'stage binary built')
    check(info['stage_generation'] == 0, 'running payload is generation 0')
    # Chainload twice: the second one must land in the other slot, because a
    # stage cannot be overwritten while it is the code executing.
    seen = []
    for generation in (1, 2):
        chosen, data, link = proxy.pick_stage(stage)
        check(link != proxy.bootinfo()['image_base'], f'slot {link:#x} is not the running image')
        proxy.chainload(data)
        time.sleep(3)
        proxy.nop()
        after = proxy.bootinfo()
        check(after['stage_generation'] == generation,
              f'chainload {generation}: stage answers (generation {after["stage_generation"]})')
        check(after['image_base'] == link,
              f'chainload {generation}: stage reports its real load address {after["image_base"]:#x} == {link:#x}')
        check(after['heap_base'] == info['heap_base'] and after['memory_map'] == info['memory_map'],
              f'chainload {generation}: inherited the heap and memory map')
        check(after['gpt_survey'] == info['gpt_survey'] and after['gpt_survey_size'] == info['gpt_survey_size'],
              f'chainload {generation}: inherited the GPT survey record')
        check(proxy.read32(after['heap_base']) is not None, f'chainload {generation}: memory still readable')
        stage_serror = proxy.serror_state()
        check(stage_serror['mechanism'] == 2 and stage_serror['checks'] > 0 and stage_serror['during'] == 0,
              f'chainload {generation}: stage brackets accesses for SErrors')
        if preload_payload is not None:
            inherited_cache = preload.resident_cache(proxy, after)
            check(inherited_cache == cache and proxy.call(after['preload_verify'], 0) == 1,
                  f'chainload {generation}: reserved cache and fresh verifier survive')
            check(preload.diagnostics(proxy, after) == preload_status,
                  f'chainload {generation}: original EFI load diagnostics survive')
        stage_gic = struct.unpack('<9Q5I', proxy.readmem(after['gic_stats'], 92, live=True))
        check(stage_gic[13] == 0 and stage_gic[9] == 26, f'chainload {generation}: own GIC tick running')
        check(after['battery_call'] and after['battery_session'] != info['battery_session'],
              f'chainload {generation}: stage publishes its own live battery entry and session')
        check(after['gpu_call'] and after['gpu_state'] != info['gpu_state']
              and (after['gpu_arena_base'], after['gpu_arena_size'], after['gpu_arena_flags'])
              == (info['gpu_arena_base'], info['gpu_arena_size'], info['gpu_arena_flags'])
              and struct.unpack_from('<Q', proxy.readmem(after['gpu_state'] + 24, 8))[0] == info['gpu_arena_base'],
              f'chainload {generation}: stage publishes its own gpu_call and adopts the arena')
        status, record = proxy.live_battery(q1n1proxy.LIVE_SURVEY, 200)
        check(record['calls'] == 1 and record['phase'] == 2,
              f'chainload {generation}: stage live battery refuses cleanly (status {status})')
        stage_call = call_state(after['call_service_state'])
        check(after['call_service'] and after['call_service_state'] != info['call_service_state']
              and stage_call['magic'] == 0x4c4c4143314e3151 and not stage_call['arms']
              and service(after['call_service'], 1, 0x77) == 0 and service(after['call_service'], 3) == 0,
              f'chainload {generation}: stage publishes and answers its own call service')
        seen.append(link)
    check(seen[0] != seen[1], f'the two chainloads used different slots ({seen[0]:#x}, {seen[1]:#x})')

    tables = proxy.acpi_tables()
    check('SPCR' in tables and 'DSDT' in tables or 'FACP' in tables, f'ACPI tables readable: {sorted(tables)}')
    stats = struct.unpack('<12Q', proxy.readmem(info['proxy_stats'], 96))
    check(stats[9] == 1, 'target exception counter')
    return done, proxy


parser = argparse.ArgumentParser()
parser.add_argument('--el', choices=['1', '2'], default='2')
mode_args = parser.add_mutually_exclusive_group()
mode_args.add_argument('--preflight', action='store_true')
mode_args.add_argument('--fault', action='store_true')
mode_args.add_argument('--usb-probe', action='store_true')
mode_args.add_argument('--usb-role-probe', action='store_true')
mode_args.add_argument('--usb-role-change', action='store_true', help='Check active request refuses an unsupported firmware in QEMU')
mode_args.add_argument('--usb-controller-probe', action='store_true', help='Check RAM inventory refuses unsupported firmware')
mode_args.add_argument('--usb-serial', action='store_true', help='Check serial test refuses unsupported firmware')
mode_args.add_argument('--usb-serial-v3', action='store_true', help='Check inactive-start serial test refuses unsupported firmware')
mode_args.add_argument('--usb-typec-probe', action='store_true', help='Check passive Type-C probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-probe', action='store_true', help='Check UCSI buffer probe refuses unsupported firmware')
mode_args.add_argument('--usb-usbc-probe', action='store_true', help='Check Qualcomm USB-C buffer probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-reg-probe', action='store_true', help='Check UCSI register probe refuses unsupported firmware')
mode_args.add_argument('--usb-ucsi-status', action='store_true', help='Check connector queries refuse unsupported firmware')
mode_args.add_argument('--usb-ucsi-status-v2', action='store_true', help='Check full-block connector queries refuse unsupported firmware')
mode_args.add_argument('--usb-ucsi-device0', action='store_true', help='Check data-role request and serial branch refuse unsupported firmware')
mode_args.add_argument('--usb-ebs', action='store_true', help='Check EL2 USB takeover prep and takeover refuse unsupported firmware')
mode_args.add_argument('--usb-ebs-fixture', action='store_true', help='Run the QEMU-only takeover fixture through ExitBootServices and ResetSystem')
mode_args.add_argument('--proxy', action='store_true', help='Run the EL2 proxy over a PL011 and drive it with tools/q1n1proxy.py')
mode_args.add_argument('--boot-window', action='store_true', help='Check the A16 boot window refuses unsupported firmware and continues to Windows')
parser.add_argument('--firmware', default='/opt/homebrew/share/qemu/edk2-aarch64-code.fd')
parser.add_argument('--nvme-gpt', action='store_true', help='With --proxy: attach an NVMe disk holding a GPT; '
                    'the survey must decode it and the run must issue no NVMe write, flush or DSM')
parser.add_argument('--build-dir', type=Path, help='Use binaries from an isolated build directory')
parser.add_argument('--preload', action='store_true', help='With --proxy, exercise an SSD-cache fixture across EBS and chainloads')
args = parser.parse_args()
if args.preload and not args.proxy:
    parser.error('--preload requires --proxy')
if args.usb_serial_v3:
    args.usb_serial = True
if args.usb_ucsi_status_v2 or args.usb_ucsi_device0:
    args.usb_ucsi_status = True
root = Path(__file__).resolve().parents[1]
build_dir = args.build_dir.resolve() if args.build_dir else root / 'build' / 'uefi'
probe = args.usb_probe or args.usb_role_probe
active_usb = args.usb_role_change or args.usb_serial
mode = 'usb-role-change' if args.usb_role_change else 'usb-role-probe' if args.usb_role_probe else 'usb-probe' if probe else 'preflight' if args.preflight else 'fault' if args.fault else 'boot'
if args.usb_serial:
    mode = 'usb-serial-v3' if args.usb_serial_v3 else 'usb-serial'
if args.usb_typec_probe:
    mode = 'usb-typec-probe'
if args.usb_ucsi_probe:
    mode = 'usb-ucsi-probe'
if args.usb_usbc_probe:
    mode = 'usb-usbc-probe'
if args.usb_ucsi_reg_probe:
    mode = 'usb-ucsi-reg-probe'
if args.usb_ucsi_status:
    mode = 'usb-ucsi-status-v2' if args.usb_ucsi_status_v2 else 'usb-ucsi-status'
if args.usb_ucsi_device0:
    mode = 'usb-ucsi-device0'
if args.usb_controller_probe:
    mode = 'usb-controller-probe'
if args.usb_ebs or args.usb_ebs_fixture:
    mode = 'usb-ebs-fixture' if args.usb_ebs_fixture else 'usb-ebs'
if args.proxy:
    mode = 'proxy'
if args.boot_window:
    mode = 'boot-window'
out = build_dir / f'qemu-el{args.el}-{mode}'
esp = out / 'esp'
esp.mkdir(parents=True, exist_ok=True)
preload_payload = None
preload_path = esp / 'Snapintosh' / 'q1n1-preload.bin'
if args.preload:
    preload_path.parent.mkdir(exist_ok=True)
    preload_payload = bytes(range(256)) * 513 + b'EFI cache fixture'
    (preload_path.parent / 'fixture.bin').write_bytes(preload_payload)
    preload_path.write_bytes(preload.manifest([
        ('\\Snapintosh\\fixture.bin', len(preload_payload), hashlib.sha256(preload_payload).hexdigest())]))
else:
    preload_path.unlink(missing_ok=True)
binary = 'q1n1-usb-role-change.efi' if args.usb_role_change else 'q1n1-usb-role-probe.efi' if args.usb_role_probe else 'q1n1-usb-probe.efi' if probe else 'q1n1.efi'
if args.usb_serial:
    binary = 'q1n1-usb-serial-v3.efi' if args.usb_serial_v3 else 'q1n1-usb-serial.efi'
if args.usb_typec_probe:
    binary = 'q1n1-usb-typec-probe.efi'
if args.usb_ucsi_probe:
    binary = 'q1n1-usb-ucsi-probe.efi'
if args.usb_usbc_probe:
    binary = 'q1n1-usb-usbc-probe.efi'
if args.usb_ucsi_reg_probe:
    binary = 'q1n1-usb-ucsi-reg-probe.efi'
if args.usb_ucsi_status:
    binary = 'q1n1-usb-ucsi-status-v2.efi' if args.usb_ucsi_status_v2 else 'q1n1-usb-ucsi-status.efi'
if args.usb_ucsi_device0:
    binary = 'q1n1-usb-ucsi-device0.efi'
if args.usb_controller_probe:
    binary = 'q1n1-usb-controller-probe.efi'
if args.usb_ebs:
    binary = 'q1n1-usb-ebs.efi'
    shutil.copyfile(build_dir / 'q1n1-usb-ebs-prep.efi', esp / 'q1n1-usb-ebs-prep.efi')
if args.usb_ebs_fixture:
    binary = 'usb-ebs-fixture.efi'
shutil.copyfile(build_dir / binary, esp / binary)
options = '' if args.preflight or probe else '--el2 --uart'
if active_usb:
    options = '--device0'
if args.usb_controller_probe:
    options = ''
if args.usb_typec_probe or args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
    options = ''
if args.fault:
    options += ' --fault-test'
if args.usb_ucsi_device0:
    options = '--device0'
if args.usb_ebs or args.usb_ebs_fixture:
    options = '--takeover'
if args.proxy:
    options = '--uart-proxy'
if args.boot_window:
    options = '--boot'
setup = ''
if args.usb_role_probe:
    shutil.copyfile(root / 'build/uefi/usb-role-fixture.efi', esp / 'usb-role-fixture.efi')
    setup = 'load usb-role-fixture.efi\n'
branch = ''
if args.usb_ebs:
    setup = 'q1n1-usb-ebs-prep.efi --device0\necho Q1N1_PREP_RETURNED\n'
if args.usb_ucsi_device0:
    branch = 'if %lasterror% == 0 then\n  echo Q1N1_UNEXPECTED_SERIAL_BRANCH\nelse\n  echo Q1N1_ROLE_REJECT_BRANCH_OK\nendif\n'
(esp / 'startup.nsh').write_text(f'fs0:\n{setup}{binary} {options}\n{branch}echo Q1N1_SHELL_RETURN_OK\n')
qmp_path = f'/tmp/q1n1-qmp-{os.getpid()}.sock'
serial = out / 'serial.log'
# Popen can return before QEMU truncates its serial file. Never accept a boot
# marker or a success report left behind by an earlier run in this directory.
for stale in (serial, out / 'result.json', out / 'screen.ppm'):
    stale.unlink(missing_ok=True)
proxy_socket = f'/tmp/q1n1-proxy-{os.getpid()}.sock'
# The proxy needs a bidirectional console; the log file still records everything.
serial_args = (['-chardev', f'socket,id=q1n1serial,path={proxy_socket},server=on,wait=off,logfile={serial}',
                '-serial', 'chardev:q1n1serial'] if args.proxy else ['-serial', f'file:{serial}'])
cmd = ['qemu-system-aarch64', '-machine',
       f'virt,virtualization={"on" if args.el == "2" else "off"},gic-version=3',
       '-accel', 'tcg', '-cpu', 'max', '-smp', '1', '-m', '512M',
       '-bios', args.firmware, '-drive', f'format=raw,file=fat:rw:{esp}',
       '-device', 'ramfb', '-display', 'none', '-monitor', 'none', '-nic', 'none',
       *serial_args, '-qmp', f'unix:{qmp_path},server=on,wait=off',
       '-no-reboot']
nvme_img = out / 'nvme-gpt.img'
nvme_trace = out / 'nvme-trace.log'
nvme_hash = None
if args.nvme_gpt:
    assert args.proxy, '--nvme-gpt needs --proxy'
    nvme_hash = nvme_gpt_image(nvme_img)
    nvme_trace.unlink(missing_ok=True)
    cmd += ['-drive', f'if=none,id=nvm,format=raw,file={nvme_img}',
            '-device', 'nvme,id=nvmectl,serial=SNAPSURVEY01',
            '-device', f'nvme-ns,drive=nvm,nsid=1,eui64=0x{NVME_EUI}',
            '-trace', 'enable=pci_nvme_io_cmd', '-trace', 'enable=pci_nvme_admin_cmd',
            '-D', str(nvme_trace)]
if probe:
    cmd += ['-device', 'qemu-xhci']
expected = ('REFUSED: Matching BIOS312 UsbConfigDxe code not found.' if active_usb else
            'Discovery complete. Returning to shell.' if probe else
            'Preflight only.' if args.preflight else
            'EL2 CONFIRMED AFTER EXITBOOTSERVICES' if args.el == '2' else
            'EL2 NOT REACHED - STILL IN EL1')
if args.usb_typec_probe:
    expected = 'REFUSED: Matching BIOS312 UsbPwrCtrlDxe code not found.'
if args.usb_controller_probe:
    expected = 'REFUSED: Loaded UsbConfigDxe is not the inspected BIOS312 image.'
if args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
    expected = 'REFUSED: Matching BIOS312 PmicGlinkDxe code not found.'
if args.usb_ebs:
    expected = 'REFUSED: no valid prep snapshot from this boot. Firmware not exited.'
if args.usb_ebs_fixture:
    expected = 'Snapshot valid. Leaving firmware'
if args.proxy:
    expected = 'Leaving firmware for the q1n1 proxy.'
if args.boot_window:
    expected = 'REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.'
start = time.monotonic()
with (out / 'qemu.log').open('w') as err:
    proc = subprocess.Popen(cmd, stdout=err, stderr=err)
    try:
        deadline = start + 55
        found = False
        while time.monotonic() < deadline and proc.poll() is None:
            if serial.exists() and expected in serial.read_text(errors='replace'):
                found = True
                break
            time.sleep(0.2)
        if not found:
            raise RuntimeError(f'Boot marker missing: {expected}; inspect {serial}')
        proxy_checks, proxy = (None, None)
        if args.proxy:
            proxy_checks, proxy = run_proxy_checks(proxy_socket, build_dir, preload_payload, nvme=args.nvme_gpt)
        # Give the console, logo and optional exception screen time to finish.
        # The fixture needs ExitBootServices plus the driver's 1 s reset timeout.
        time.sleep(6 if args.usb_ebs_fixture else 1)
        with socket.socket(socket.AF_UNIX) as qmp:
            qmp.connect(qmp_path)
            stream = qmp.makefile('rwb', buffering=0)
            json.loads(stream.readline())
            for request in [dict(execute='qmp_capabilities'),
                            dict(execute='screendump', arguments={'filename': str(out / 'screen.ppm')})]:
                stream.write(json.dumps(request).encode() + b'\n')
                while True:
                    reply = json.loads(stream.readline())
                    if 'error' in reply:
                        raise RuntimeError(reply)
                    if 'return' in reply:
                        break
        if args.proxy:
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            # No status bar any more: check the console actually rendered text.
            lit = sum(1 for n in range(0, len(ppm[3]), 3) if ppm[3][n] or ppm[3][n+1] or ppm[3][n+2])
            assert lit > 20000, f'proxy console drew almost nothing ({lit} lit pixels)'
            assert proxy is not None
            proxy.reboot()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise RuntimeError('P_REBOOT did not reset QEMU')
            if args.nvme_gpt:
                assert hashlib.sha256(nvme_img.read_bytes()).hexdigest() == nvme_hash, 'NVMe fixture disk changed'
                trace = nvme_trace.read_text(errors='replace')
                io = [l for l in trace.splitlines() if 'pci_nvme_io_cmd' in l]
                bad = [l for l in io if "'NVME_NVM_CMD_READ'" not in l]
                assert io and not bad, f'non-read NVMe I/O during the run: {bad[:5]}'
                proxy_checks.append(f'NVMe fixture disk unchanged (sha256); {len(io)} NVMe I/O commands, all Read')
            print(f'{len(proxy_checks)} proxy checks: ' + '; '.join(proxy_checks))
        elif args.boot_window:
            log = serial.read_text(errors='replace')
            assert 'Q1N1_SHELL_RETURN_OK' in log, 'boot window did not return to the shell'
            assert 'Leaving firmware' not in log
            assert 'q1n1 boot window v1' in log
            assert 'REQUESTING USB0 DEVICE MODE' not in log
        elif args.usb_ebs_fixture:
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            assert tuple(ppm[3][:3]) == (53, 227, 139), 'Post-EBS fixture did not draw the EL2 status bar'
            log = serial.read_text(errors='replace')
            assert 'QEMU FIXTURE: fake controller in RAM' in log and 'Q1N1_SHELL_RETURN_OK' not in log
            # The fixture's driver times out, stops, waits 20 s, then calls ResetSystem;
            # QEMU runs with -no-reboot, so a working reset ends the process.
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise RuntimeError('ResetSystem after ExitBootServices did not end QEMU')
        elif args.usb_ebs:
            log = serial.read_text(errors='replace')
            assert 'REFUSED: UsbConfigDxe USB0 core/qscratch table differs from BIOS312.' in log
            assert 'Q1N1_PREP_RETURNED' in log and 'Q1N1_SHELL_RETURN_OK' in log
            assert 'REQUESTING USB0 DEVICE MODE' not in log and 'Snapshot valid' not in log
            assert 'Prep complete' not in log
        elif args.preflight or probe or active_usb or args.usb_controller_probe or args.usb_typec_probe or args.usb_ucsi_probe or args.usb_usbc_probe or args.usb_ucsi_reg_probe or args.usb_ucsi_status:
            log = serial.read_text(errors='replace')
            assert 'Leaving firmware now.' not in log
            assert 'Q1N1_SHELL_RETURN_OK' in log, 'EFI application did not return to shell'
            if args.usb_controller_probe:
                assert 'No role change called.' in log
                assert 'CONTROLLER:' not in log
            if active_usb:
                assert 'No role change called.' in log
                assert 'REQUESTING USB0 DEVICE MODE' not in log
            if args.usb_typec_probe:
                assert 'No vendor callbacks called.' in log
                assert 'Snapshot complete.' not in log
            if args.usb_ucsi_probe:
                assert 'No UCSI read requested.' in log
                assert 'Requesting one 48-byte UCSI buffer read' not in log
            if args.usb_usbc_probe:
                assert 'No USB-C read requested.' in log
                assert 'Requesting one 132-byte Qualcomm USB-C status read' not in log
            if args.usb_ucsi_reg_probe or args.usb_ucsi_status:
                assert 'No UCSI register read requested.' in log
                assert 'Reading register:' not in log
                assert 'UCSI query/control:' not in log
                if args.usb_ucsi_device0:
                    assert 'Q1N1_ROLE_REJECT_BRANCH_OK' in log
                    assert 'Q1N1_UNEXPECTED_SERIAL_BRANCH' not in log
            if probe:
                assert 'USB FUNCTION: not installed' in log
                assert 'USB2 HOST handles: 0x0000000000000001' in log
                assert 'SERIAL IO handles: 0x0000000000000001' in log
                assert 'Revision: 0x0000000000010000' in log
                assert 'Path:' in log, 'Device-path conversion was not exercised'
                assert 'enumeration failed' not in log and 'Interface query failed' not in log
                if args.usb_role_probe:
                    assert 'QCOM USB CONFIG handles: 0x0000000000000003' in log
                    assert "Matches USB function driver's data checks." in log
                    assert "Matches XHCI host driver's data checks." in log
                    assert "Outside both drivers' selector range." in log
        else:
            # QEMU's P6 screenshot preserves the actual GOP scanout.
            ppm = (out / 'screen.ppm').read_bytes().split(b'\n', 3)
            assert ppm[0] == b'P6' and ppm[2] == b'255'
            expected_rgb = (255, 48, 64) if args.fault else (53, 227, 139) if args.el == '2' else (255, 179, 71)
            assert tuple(ppm[3][:3]) == expected_rgb, 'Incorrect exception-level/fault framebuffer status'
        report = {'passed': True, 'el': int(args.el), 'mode': mode,
                  'expected': expected, 'seconds': round(time.monotonic()-start, 2),
                  'hardware_test': False}
        (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        print(out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        Path(qmp_path).unlink(missing_ok=True)
        if args.proxy:
            Path(proxy_socket).unlink(missing_ok=True)
