#!/usr/bin/env python3
"""Build and run the NCM-proxy packet harness with sanitizers."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    binary = ROOT / 'build' / 'test-q1n1-ncmproxy'
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
                    f'-I{ROOT}/platform/uefi',
                    str(ROOT / 'tools' / 'test-q1n1-ncmproxy.c'),
                    str(ROOT / 'platform' / 'uefi' / 'ncm-proxy.c'),
                    '-o', str(binary)], check=True)
    return subprocess.run([str(binary)]).returncode


if __name__ == '__main__':
    sys.exit(main())
