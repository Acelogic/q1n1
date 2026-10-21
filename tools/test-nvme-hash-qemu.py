#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The real native hash helper binary at EL2 in QEMU, on QEMU's NVMe controller.

QEMU's `max` CPU implements the ARMv8 SHA2 instructions, so this runs the exact
build/nvme-hash/nvme-hash.bin the A16 would run -- its EL2 register checks, the
SHA2 path, eight reads in flight, restore overlays -- through NativeHash.digest
itself, beside the replaced production helper (commit 3f2966d). Then
tools/nvme_hash_compare.py runs end to end with only its A16 locators replaced
(ACPI segment-5 root port, SMMU, Snapintosh partition header, stage-slot
placement, battery). QEMU's disk must come back unchanged and every NVMe I/O
command QEMU traced must be a Read.

QEMU is a control, not the A16: this proves the binary and the queue protocol,
not the SSD's timing or the A16's EL2 trap configuration.

    make uefi && python3 tools/test-nvme-hash-qemu.py
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import nvme_m3 as m3                                          # noqa: E402
import nvme_hash                                              # noqa: E402
import nvme_hash_compare as compare                           # noqa: E402
import q1n1proxy                                              # noqa: E402

MIB = 1 << 20
FIRMWARE = '/opt/homebrew/share/qemu/edk2-aarch64-code.fd'
OUT = ROOT / 'build/uefi/qemu-el2-nvme-hash'
CONTAINER = (2 * MIB, 61 * MIB)          # (offset, bytes): a stand-in for the Snapintosh container
checks = []


def check(condition, label):
    if not condition:
        raise SystemExit(f'FAIL: {label}')
    checks.append(label)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def disk_image(rng):
    """64 MiB: random, a zero range (as deallocated blocks read) and random."""
    return rng.randbytes(24 * MIB) + bytes(16 * MIB) + rng.randbytes(24 * MIB)


def qemu_locate(hw, info, record):
    """QEMU's NVMe function on bus 0 of the MCFG segment. EDK2 assigned BAR0;
    it drops Bus Master at ExitBootServices, so this fixture sets it again."""
    mcfg = m3.acpi_table(hw, info['acpi_rsdp'], b'MCFG')
    ecam, _, first_bus, _ = struct.unpack_from('<QHBB', mcfg, 44)
    for device in range(32):
        cfg = ecam + (first_bus << 20 | device << 15)
        words = m3.config_header(hw, cfg)
        if words[0] & 0xFFFF == 0xFFFF or words[2] >> 8 != m3.EXPECT_CLASS:
            continue
        bar = (words[4] & ~0xF) | (words[5] << 32 if (words[4] >> 1) & 3 == 2 else 0)
        command = words[1] & 0xFFFF
        if command & 6 != 6:
            hw.w32(cfg + 4, command | 6)
        record['pci'] = dict(ecam=hex(ecam), device=device, bar0=hex(bar), command_before=hex(command))
        return bar, 0
    raise m3.Stop('no NVMe function on QEMU bus 0')


class NoSmmu:
    """QEMU virt has no IOMMU in front of the NVMe function: bus addresses are physical."""
    mode, info = 'bypass', {}

    def reaches(self, base, size):
        return True


def qemu_arm_window(nvme, guid, record):
    record['window_armed'] = dict(partition=guid, first_lba=0, last_lba=nvme.nsze - 1,
                                  header=dict(container_offset=CONTAINER[0], container_image=CONTAINER[1]))
    return 0, nvme.nsze - 1


class QemuNativeHash(nvme_hash.NativeHash):
    """NativeHash with its helper in q1n1's host-scratch heap (EfiLoaderCode, so
    executable in QEMU) instead of the A16 stage-slot tail, which needs a stage
    built for the A16's addresses. Same leaf admission, upload and check."""

    def __init__(self, nvme, stage_builds=()):
        record = json.loads((nvme_hash.BUILD / 'build.json').read_text())
        binary = (nvme_hash.BUILD / 'nvme-hash.bin').read_bytes()
        check(sha(binary) == record['binary_sha256'], 'helper binary is its build record')
        info = nvme.hw.p.bootinfo()
        self.nvme, self.code, self.state = nvme, info['heap_base'] + 0x200000, nvme.w + 0x60000
        leaf = bytes.fromhex('000080d2c0035fd6')  # mov x0, #0; ret
        nvme.hw.write(self.code, leaf)
        nvme.hw.p.flush_code(self.code, len(leaf))
        nvme.hw.p.set_exc_guard(3)
        try:
            check(nvme.hw.p.call(self.code, timeout=3) == 0 and not nvme.hw.p.get_exc_count(), 'helper address runs')
        finally:
            nvme.hw.p.set_exc_guard(0)
        nvme.hw.write(self.code, binary)
        nvme.hw.p.flush_code(self.code, len(binary))
        check(nvme.hw.read(self.code, len(binary)) == binary, 'helper upload reads back')
        nvme.log['native_hash_helper'] = record
        nvme.log['native_hash_placement'] = dict(code=hex(self.code), state=hex(self.state), qemu_heap=True)


def controller(proxy, record):
    hw = m3.Hw(proxy)
    info = proxy.bootinfo()
    bar, _ = qemu_locate(hw, info, record)
    window = m3.pick_window(hw, info, NoSmmu(), record)
    nv = m3.Nvme(hw, bar, window, record)
    nv.takeover(record)
    nv.identify(record)
    nv.io_queues(record)
    return nv


def chainload(socket):
    """Run under a chainloaded stage, as the A16 does (nvme_hash_compare.py requires one)."""
    proxy = q1n1proxy.connect(socket, wait=20)
    try:
        _, data, link = proxy.pick_stage(ROOT / 'build/uefi/q1n1-stage-qemu.bin')
        proxy.chainload(data)
        time.sleep(3)
        proxy.nop()
        info = proxy.bootinfo()
        check(info['stage_generation'] == 1 and info['image_base'] == link and info['current_el'] == 2,
              f'QEMU stage chainloaded at {link:#x}, EL2')
    finally:
        proxy.link.close()


def direct(socket, disk, replaced, rng):
    """NativeHash.digest with each helper: whole disk, odd ranges, overlays."""
    proxy = q1n1proxy.connect(socket, wait=20)
    proxy.resume_skip = False
    info = proxy.bootinfo()
    check(info['current_el'] == 2, 'q1n1 proxy at EL2')
    record = {}
    nv = controller(proxy, record)
    try:
        check(nv.lba_bytes == 512 and nv.nsze * 512 == len(disk) and nv.max_transfer == 256 << 10,
              f'QEMU namespace: {nv.nsze} x {nv.lba_bytes}, {nv.max_transfer}-byte reads')
        nh = QemuNativeHash(nv)
        new = (nvme_hash.BUILD / 'nvme-hash.bin').read_bytes()
        ranges = [(0, nv.nsze), (7, 3 * 32768 + 5), (100003, 17), (5, 1), (32768 - 3, 9)]
        rows = [(3 * 4096 + 100, rng.randbytes(5000)), (16 * MIB - 2048, rng.randbytes(8192)),
                (40 * MIB, rng.randbytes(262144 + 512))]
        masked = bytearray(disk)
        for at, data in rows:
            masked[at:at + len(data)] = data
        timing = {}
        for label, code in (('new', new), ('replaced', replaced)):
            compare.upload(nh, code, max(len(new), len(replaced)))
            nv.hw.write(nh.state + compare.TELEMETRY_OFF, bytes(compare.TELEMETRY.size))
            for lba, count in ranges:
                t0 = time.monotonic()
                value = nh.digest(lba, count)
                if (lba, count) == ranges[0]:
                    timing[label] = time.monotonic() - t0
                check(value == sha(disk[lba * 512:(lba + count) * 512]), f'{label} helper: LBA {lba} + {count}')
                if label == 'new':
                    t = compare.telemetry(nh)
                    check(t['magic'] == compare.TELEMETRY_MAGIC and t['status'] == 0 and t['el'] == 2 and
                          t['sha_path'] == 1 and (t['isar0'] >> 12) & 15 >= 1 and t['depth'] == 8,
                          f'new helper took the SHA2 path at EL2 (path {t["sha_path"]:#x}, '
                          f'CPTR_EL2 {t["cptr"]:#x}, HCR_EL2 {t["hcr"]:#x}, ISAR0 {t["isar0"]:#x})')
                else:
                    check(not compare.telemetry(nh)['magic'], 'the replaced helper writes no telemetry')
            check(nh.digest(0, nv.nsze, overlays=rows) == sha(masked), f'{label} helper: restore overlays')
        print(f'TCG seconds for the whole disk: new {timing["new"]:.2f}, replaced {timing["replaced"]:.2f}')
    finally:
        nv.delete_io_queues()
        nv.shutdown(record)
        proxy.link.close()


def end_to_end(socket, disk):
    """tools/nvme_hash_compare.py itself, with QEMU locators patched in."""
    m3.locate, m3.arm_window = qemu_locate, qemu_arm_window
    m3.Smmu = lambda hw, sid, base=None: NoSmmu()
    compare.NativeHash = QemuNativeHash
    compare.SAMPLE = 32 << 10    # host reads cross QEMU's emulated PL011 at ~18 KB/s
    q1n1proxy.Proxy.live_battery = lambda self, *a, **k: (0, dict(plausible=1, ac=1, percent=100, text='QEMU'))
    container = disk[CONTAINER[0]:CONTAINER[0] + CONTAINER[1]]
    args = argparse.Namespace(device=socket, partition_guid='qemu', chunks='all', stream=True, stream_old=True,
                              expect_container=sha(container), stage_build=[])
    record = {}
    compare.run(args, record, lambda: None)
    rows = record['chunks']
    check(len(rows) == 4 and all(r['new'] == r['replaced'] == sha(container[r['offset']:r['offset'] + r['bytes']])
                                 for r in rows), 'compare tool: every chunk equal between helpers and to hashlib')
    check(record['sample']['new'] == record['sample']['replaced'] == sha(container[:compare.SAMPLE]),
          'compare tool: host-read sample equal to both helpers')
    check(record['stream']['new']['sha256'] == record['stream']['replaced']['sha256'] == sha(container),
          'compare tool: whole-container stream equal between helpers and to hashlib')
    summary = record['summary']
    check(summary['new_helper']['sha_paths'] == ['0x1'] and summary['chunks'] == 4 and 'speedup' in summary,
          'compare tool: telemetry summary')
    check(record['serror'] == dict(before=0, during=0, unconsumed=0) and record['shutdown'],
          'compare tool: zero SErrors, normal shutdown')
    print('compare summary:', json.dumps(summary))


def main():
    rng = random.Random(20260928)
    nvme_hash.build()
    replaced = compare.replaced_helper()
    check(sha(replaced) == compare.REPLACED_SHA256, 'replaced helper rebuilt byte for byte')
    OUT.mkdir(parents=True, exist_ok=True)
    esp = OUT / 'esp'
    esp.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'build/uefi/q1n1.efi', esp / 'q1n1.efi')
    (esp / 'startup.nsh').write_text('fs0:\nq1n1.efi --uart-proxy\necho Q1N1_SHELL_RETURN_OK\n')
    disk = disk_image(rng)
    image, trace, serial = OUT / 'nvme.img', OUT / 'nvme-trace.log', OUT / 'serial.log'
    image.write_bytes(disk)
    for stale in (trace, serial):
        stale.unlink(missing_ok=True)
    socket = f'/tmp/q1n1-nvme-hash-{os.getpid()}.sock'   # AF_UNIX path limit; tools/test-uefi.py does the same
    cmd = ['qemu-system-aarch64', '-machine', 'virt,virtualization=on,gic-version=3', '-accel', 'tcg',
           '-cpu', 'max', '-smp', '1', '-m', '512M', '-bios', FIRMWARE, '-drive', f'format=raw,file=fat:rw:{esp}',
           '-device', 'ramfb', '-display', 'none', '-monitor', 'none', '-nic', 'none',
           '-chardev', f'socket,id=q1n1serial,path={socket},server=on,wait=off,logfile={serial}',
           '-serial', 'chardev:q1n1serial', '-no-reboot',
           '-drive', f'if=none,id=nvm,format=raw,file={image}', '-device', 'nvme,id=nvmectl,serial=NVMEHASH01',
           '-device', f'nvme-ns,drive=nvm,nsid=1,eui64=0x{m3.EXPECT_EUI64}',
           '-trace', 'enable=pci_nvme_io_cmd', '-D', str(trace)]
    with (OUT / 'qemu.log').open('w') as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and proc.poll() is None:
                if serial.exists() and 'Leaving firmware for the q1n1 proxy.' in serial.read_text(errors='replace'):
                    break
                time.sleep(0.2)
            else:
                raise SystemExit(f'FAIL: q1n1 did not reach its proxy; see {serial}')
            chainload(socket)
            direct(socket, disk, replaced, rng)
            end_to_end(socket, disk)
            proxy = q1n1proxy.connect(socket, wait=10)
            proxy.reboot()
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    check(sha(image.read_bytes()) == sha(disk), 'QEMU disk unchanged')
    io = [line for line in trace.read_text(errors='replace').splitlines() if 'pci_nvme_io_cmd' in line]
    check(io and all("'NVME_NVM_CMD_READ'" in line for line in io), f'{len(io)} NVMe I/O commands, all Read')
    print(f'PASS {len(checks)} checks: ' + '; '.join(checks[-6:]))


if __name__ == '__main__':
    main()
