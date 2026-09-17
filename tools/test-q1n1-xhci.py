#!/usr/bin/env python3
"""Build and run the native xHCI/NCM harness with sanitizers.

The A16 is expensive to test against: a wedged device needs a physical replug,
so the driver's logic is exercised here first against a simulated controller.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    binary = ROOT / 'build' / 'test-q1n1-xhci'
    binary.parent.mkdir(parents=True, exist_ok=True)
    build = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
             f'-I{ROOT}/platform/uefi',
             str(ROOT / 'tools' / 'test-q1n1-xhci.c'),
             str(ROOT / 'platform' / 'uefi' / 'xhci.c'),
             str(ROOT / 'platform' / 'uefi' / 'ncm.c'),
             '-o', str(binary)]
    subprocess.run(build, check=True)
    result = subprocess.run([str(binary)])
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
