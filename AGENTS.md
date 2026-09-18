# Working in this repository

q1n1 is a fork of m1n1 retargeted from Apple Silicon to the ASUS Zenbook A16
(UX3607OA / Snapdragon X2E-96-100). Firmware loads it as a UEFI application, it
takes the machine at EL2 after `ExitBootServices`, and it serves m1n1's proxy
protocol so a host can drive the hardware interactively.

`CLAUDE.md` and `GEMINI.md` are symlinks to this file.

## Two trees, one repository

**`platform/uefi/` is this fork's work.** The payload, the boot window, the
framebuffer console, GICv3 and the EL2 tick, the proxy request loop, the USB
stack, and the chainloadable stages. `tools/` holds the host side. Build it with
`make uefi`; that target is deliberately separate from the Apple object graph.

**`src/`, `proxyclient/`, `rust/`, `font/`, `sysinc/` are the retained m1n1
tree.** Still builds (`make all` produces `build/m1n1.macho` and
`build/m1n1.bin`) and still has upstream's pytest suite, but it targets Apple
Silicon and nothing in the UEFI payload links against it. Do not refactor it to
suit the fork. This repository no longer tracks upstream and has no `upstream`
remote, so there are no merges to keep clean — but there is also no reason to
churn that tree.

## Build and test

```shell
make uefi                            # build/uefi/q1n1.efi and the stages
python3 tools/test-uefi.py --proxy   # QEMU boots the real binary to EL2
python3 tools/test-q1n1-proxy.py     # native, ASan/UBSan
python3 tools/test-q1n1-xhci.py
python3 tools/test-q1n1-ncmproxy.py
```

The native suites link a driver against a register model — no firmware, no
physical MMIO — so they run anywhere. Two more have no runner and are compiled
directly:

```shell
clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror \
  -Iplatform/uefi tools/test-usb1-device.c platform/uefi/usb1-device.c \
  -o /tmp/test-usb1-device && /tmp/test-usb1-device

clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wall -Wextra -Werror \
  -Iplatform/uefi tools/test-qdwc3-identity.c \
  -o /tmp/test-qdwc3-identity && /tmp/test-qdwc3-identity
```

`-Werror` is on for both the PE and the stage builds. A warning is a failure.

There is no CI. Everything here is built and run locally, so nothing else will
catch a break for you — run the suites before calling a change done.

## Header dependencies matter more than usual

The UEFI Makefile passes `-MMD -MP`, and that is not a nicety. These objects
share structs that live in shared memory across a chainload, so when a header
changes and only some objects are rebuilt, the survivors disagree about where
the fields are. The failure is silent: counters read as pointers, a stage adopts
a block it cannot parse, and the corruption looks like a driver bug. If you add
a target, give it header dependencies.

## The hardware is the target

QEMU `virt` is a control, not the machine. It does not model this laptop's
Type-C hardware, its DWC3 instances, or its Qualcomm platform registers, so a
QEMU pass is necessary and not sufficient. When a change touches the payload,
chainload it and check:

```shell
python3 tools/a16ctl.py status                              # where the A16 is
python3 tools/q1n1proxy.py chainload build/uefi/q1n1-stage.bin
```

Chainloading replaces the running EL2 code in about 0.3 s without rebooting; the
client picks the free slot of the two. A reset from q1n1 returns to q1n1, so
this loop is cheap and recoverable — use it rather than reasoning about what the
hardware would probably do.

The framebuffer can be read back over the proxy, which is how the screenshot in
the README was taken. That is a real way to check what the payload drew.

## Writing things down

`docs/` records how each piece was established, **including what turned out to
be wrong** — see `docs/A16-DIRECT-USB-C.md` for the shape: the finding, the
automatic behaviour, the negative experiments, then the evidence with specific
log names. A successful transport call is not evidence of hardware state, and a
test that passes in QEMU is not evidence about the laptop. Say which machine a
result came from.

## Machine-specific values

The UEFI boot entry GUID, the shell's `fs2:` ESP mapping, the DWC3 base
addresses, and a BIOS 312 check are specific to one laptop. The SSH target and
remote directory come from the environment (`A16_SSH_TARGET`, `A16_SSH_ALIAS`,
`A16_REMOTE_DIR`) rather than being baked in. Keep new machine-specific values
out of the source where an environment variable or a probe will do.
