# First confirmed q1n1 EL2 boot on the A16

Date: 2026-09-16. Target: ASUS Zenbook A16 UX3607OA, Snapdragon X2E-96-100,
BIOS UX3607OA.312, with Secure Boot disabled.

The user booted the installed `q1n1 UEFI Shell` entry from the existing NVMe
EFI partition. In that shell, `map fs*` identified NVMe partition 12 as `FS2:`.
Preflight returned normally and reported entry EL2, a captured GOP framebuffer,
and an ACPI SPCR UART. The subsequent command was:

```text
fs2:\EFI\q1n1\q1n1.efi --el2
```

The resulting user-supplied photograph shows:

- `EL2 CONFIRMED AFTER EXITBOOTSERVICES`.
- `ENTRY EL` and `CURRENT EL` both `0x2`.
- The standalone q1n1 framebuffer console, green status bar, and dragon logo.
- SPCR UART `0x00894000`, interface `0x13`, GSI `0x182`, baud code `0x7`.
- `UART TX DISABLED OR UNAVAILABLE` and `USB SERIAL NOT IMPLEMENTED`.

The binary installed for this test has SHA-256:
`379558299150a121220ee40b7e8095238aad3474c48a695e0b121bf93c41dd32`.
This identifies the installed artifact recorded before reboot; it is not a
measurement read back from the running payload. Its old preflight text advises
loading slbounce unconditionally. The local source has since corrected that
wording; the revised binary has not been installed on the A16.

Neither sltest nor slbounce was run in the prescribed test sequence. This proves
direct EL2 entry and post-EBS framebuffer execution for this machine's observed
firmware/configuration. It does not establish the behavior of other firmware
versions or Snapdragon systems, nor qualify the installed TCB for Secure Launch.

UART MMIO was intentionally disabled because `--uart` was omitted. No physical
serial transport, USB CDC, m1n1 proxy, guest hypervisor, SMP, owned page tables,
interrupt delivery, or sustained stability test is demonstrated. A still photo
does not establish that the heartbeat continues blinking.

The original photographs are retained locally under the ignored paths
`build/a16-install/preflight-el2.jpg` and
`build/a16-install/first-post-ebs-el2.jpg`. The installation manifest and BCD
backup are alongside them. The payload intentionally stays in its polling loop;
a power cycle returns to the normal firmware boot path with Windows first.

## Repeat boot

After another one-time shell boot selected over Windows SSH, the user ran the
same `--el2` command and supplied a second post-EBS photograph. It again shows
entry EL2, current EL2, and `EL2 CONFIRMED AFTER EXITBOOTSERVICES`, with MIDR
`0x512f0021`, framebuffer `0xf9a20000`, and the same SPCR values. The memory-map
descriptor count for this boot is `0x139`. UART TX remains disabled or unavailable,
and USB serial remains unimplemented. This repeats the boot result without
extending the transport or stability claims above.

The photo is retained as `build/a16-install/repeat-post-ebs-el2.jpg`, SHA-256
`00b762c84ba037a8532a759782a340dcd2cbf66555392cee6e1c828dded014d5`.
