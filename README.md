# q1n1: m1n1 on a Snapdragon laptop

A fork of [m1n1](https://github.com/AsahiLinux/m1n1) retargeted from Apple
Silicon to the ASUS Zenbook A16 (UX3607OA / Snapdragon X2E-96-100). Where m1n1
is loaded by iBoot, q1n1 is a UEFI application: firmware loads it, it takes the
machine at EL2 after `ExitBootServices`, and then serves m1n1's proxy protocol
so a host can drive the hardware interactively.

**[Installation and usage →](docs/A16-INSTALL.md)**

## What works

- **EL2 after ExitBootServices** on real hardware, with a framebuffer console.
  Entered directly, without slbounce. See the
  [first hardware boot record](docs/A16-FIRST-EL2-BOOT.md).
- **The m1n1 proxy protocol at EL2** over USB, with guarded MMIO access, memory
  transfer and code upload, so the usual m1n1 host-side idioms work.
- **Boot control**: `tools/a16ctl.py` boots the laptop into q1n1 from Windows,
  from its boot window, or from q1n1 itself — a q1n1-to-q1n1 restart never
  passes through Windows. Windows stays first in the firmware boot order and a
  failed payload falls back to it.
- **Chainloading**: replace the running EL2 code in ~0.2 s without rebooting,
  which is what makes this iterable at all.
- **Its own USB stack, both directions.** q1n1 drives the USB0 DWC3 as a device
  to serve a CDC ACM console, and drives the USB1 xHCI as a *host* to enumerate
  the Mac at the other end of a bare C-to-C cable as a CDC-NCM adapter. The
  proxy then runs over that link as UDP over IPv6 link-local, unprivileged and
  with no dock in the path — 4.6 MB/s write, 8.5 MB/s read.
- **UCSI 2.1** register access and connector queries.

Not yet: the guest hypervisor, SMP, or any Apple-specific drivers.

## Records

Each piece has a written record of how it was established, including the things
that turned out to be wrong:

| | |
| --- | --- |
| [A16-INSTALL.md](docs/A16-INSTALL.md) | build, install, boot, connect, recover |
| [A16-Q1N1-PROXY.md](docs/A16-Q1N1-PROXY.md) | EL2 proxy, boot control, xHCI/NCM, the UDP transport |
| [A16-FIRST-EL2-BOOT.md](docs/A16-FIRST-EL2-BOOT.md) | first EL2 execution on hardware |
| [A16-USB-EL2-SERIAL.md](docs/A16-USB-EL2-SERIAL.md) | USB after ExitBootServices |
| [A16-USB-DOCK.md](docs/A16-USB-DOCK.md) | CDC ACM serial through the dock |
| [A16-UCSI-REGISTERS.md](docs/A16-UCSI-REGISTERS.md) | USB-C transport and data-role experiments |
| [A16-BOOT.md](docs/A16-BOOT.md) | boot and serial guide |

## Build

```shell
make uefi        # build/uefi/q1n1.efi and the chainloadable stages
```

Then run the tests — QEMU boots the real binary to EL2, and three native suites
run the drivers against simulators under sanitizers:

```shell
python3 tools/test-uefi.py --proxy
python3 tools/test-q1n1-proxy.py
python3 tools/test-q1n1-xhci.py
python3 tools/test-q1n1-ncmproxy.py
```

## Credit

This is a fork of the [Asahi Linux](https://asahilinux.org/) project's m1n1. The
proxy protocol, the host client structure and most of the tree are theirs; MIT
licence inherited. Upstream's README follows.

The original m1n1 build and its upstream documentation remain below.

## Upstream m1n1: A bootloader and experimentation playground for Apple Silicon

## Building

You need an `aarch64-linux-gnu-gcc` cross-compiler toolchain (or a native one, if running on ARM64).
You will also need to install the `aarch64-unknown-none-softfloat` toolchain for rust.

```shell
$ rustup target add aarch64-unknown-none-softfloat
```

```shell
$ git clone --recursive https://github.com/AsahiLinux/m1n1.git
$ cd m1n1
$ make
```

To build on a native ARM64 machine:
* On Linux, use `make ARCH=`.
* On macOS using Homebrew:
```shell
$ brew install llvm lld
$ make
```
* On macOS using MacPorts:
```shell
$ sudo port install llvm clang
$ sudo port select llvm llvm-mp-<version>
$ make
```

The output will be in `build/m1n1.macho`.

To build verbosely, use `make V=1`.

### Building using the container setup

If you have a container runtime installed, like Podman or Docker, you can make use of the compose setup, which contains all build dependencies.

```shell
$ git clone --recursive https://github.com/AsahiLinux/m1n1.git
$ cd m1n1
$ podman-compose run m1n1 make
$ # or
$ docker-compose run m1n1 make
```

## Usage

Our [wiki](https://asahilinux.org/docs/sw/m1n1-user-guide/) has more information on how to
use m1n1.

To install on an OS container based on macOS <12.1, use `m1n1.macho`:

```shell
kmutil configure-boot -c m1n1.macho -v <path to your OS volume>
```

To install on an OS container based on macOS >=12.1, use `m1n1.bin`:

```shell
kmutil configure-boot -c m1n1.bin --raw --entry-point 2048 --lowest-virtual-address 0 -v <path to your OS volume>
```

## Payloads

m1n1 supports running payloads by simple concatenation:

```shell
$ cat build/m1n1.macho Image.gz build/dtb/apple-j274.dtb initramfs.cpio.gz > m1n1-payload.macho
$ cat build/m1n1.bin Image.gz build/dtb/apple-j274.dtb initramfs.cpio.gz > m1n1-payload.bin
```

Supported payload file formats:

* Kernel images (or compatible). Must be compressed or last payload.
* Devicetree blobs (FDT). May be uncompressed or compressed.
* Initramfs cpio images. Must be compressed.

Supported compression formats:

* gzip
* xz

## License

m1n1 is licensed under the MIT license, as included in the [LICENSE](LICENSE) file.

* Copyright The Asahi Linux Contributors

Please see the Git history for authorship information.

Portions of m1n1 are based on mini:

* Copyright (C) 2008-2010 Hector Martin "marcan" <marcan@marcan.st>
* Copyright (C) 2008-2010 Sven Peter <sven@svenpeter.dev>
* Copyright (C) 2008-2010 Andre Heider <a.heider@gmail.com>

m1n1 embeds libfdt, which is dual [BSD](3rdparty_licenses/LICENSE.BSD-2.libfdt) and
[GPL-2](3rdparty_licenses/LICENSE.GPL-2) licensed and copyright:

* Copyright (C) 2014 David Gibson <david@gibson.dropbear.id.au>
* Copyright (C) 2018 embedded brains GmbH
* Copyright (C) 2006-2012 David Gibson, IBM Corporation.
* Copyright (C) 2012 David Gibson, IBM Corporation.
* Copyright 2012 Kim Phillips, Freescale Semiconductor.
* Copyright (C) 2016 Free Electrons
* Copyright (C) 2016 NextThing Co.

The ADT code in mini is also based on libfdt and subject to the same license.

m1n1 embeds [minlzma](https://github.com/ionescu007/minlzma), which is
[MIT](3rdparty_licenses/LICENSE.minlzma) licensed and copyright:

* Copyright (c) 2020 Alex Ionescu

m1n1 embeds a slightly modified version of [tinf](https://github.com/jibsen/tinf), which is
[ZLIB](3rdparty_licenses/LICENSE.tinf) licensed and copyright:

* Copyright (c) 2003-2019 Joergen Ibsen

m1n1 embeds portions taken from
[arm-trusted-firmware](https://github.com/ARM-software/arm-trusted-firmware), which is
[BSD](3rdparty_licenses/LICENSE.BSD-3.arm) licensed and copyright:

* Copyright (c) 2013-2020, ARM Limited and Contributors. All rights reserved.

m1n1 embeds [Doug Lea's malloc](ftp://gee.cs.oswego.edu/pub/misc/malloc.c) (dlmalloc), which is in
the public domain ([CC0](3rdparty_licenses/LICENSE.CC0)).

m1n1 embeds portions of [PDCLib](https://github.com/DevSolar/pdclib), which is in the public
domain ([CC0](3rdparty_licenses/LICENSE.CC0)).

m1n1 embeds the [Source Code Pro](https://github.com/adobe-fonts/source-code-pro) font, which is
licensed under the [OFL-1.1](3rdparty_licenses/LICENSE.OFL-1.1) license and copyright:

* Copyright 2010-2019 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'. All Rights Reserved. Source is a trademark of Adobe in the United States and/or other countries.
* This Font Software is licensed under the SIL Open Font License, Version 1.1.

m1n1 embeds portions of the [dwc3 usb linux driver](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/usb/dwc3/core.h?id=7bc5a6ba369217e0137833f5955cf0b0f08b0712), which was [BSD-or-GPLv2 dual-licensed](3rdparty_licenses/LICENSE.BSD-3.dwc3) and copyright
* Copyright (C) 2010-2011 Texas Instruments Incorporated - http://www.ti.com

m1n1 embeds portions of [musl-libc](https://musl.libc.org/)'s floating point library, which are MIT licensed and copyright
* Copyright (c) 2017-2018, Arm Limited.

m1n1 embeds some rust crates. Licenses can be found in the vendor directory for every crate.
