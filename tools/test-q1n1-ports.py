#!/usr/bin/env python3
"""Run the port lifecycle, device halt and reader discovery regressions."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    flags = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
             '-Iplatform/uefi']
    for name, sources in (
        ('test-q1n1-usb-ports', ['tools/test-q1n1-usb-ports.c', 'platform/uefi/usb-ports.c']),
        ('test-qdwc3-stop', ['tools/test-qdwc3-stop.c']),
        ('test-q1n1-boot-choice', ['tools/test-q1n1-boot-choice.c']),
    ):
        binary = build / name
        subprocess.run(flags + sources + ['-o', str(binary)], cwd=ROOT, check=True)
        subprocess.run([str(binary)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, 'tools/test-q1n1-discovery.py'], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
