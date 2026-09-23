# Leave guest console pixels in place

The A16's native XNU panel console continued past serial KDP, but q1n1
resumed polling after the probe returned. The OLED grid/side movement then
moved XNU's pixels while XNU retained its original text coordinates. q1n1's
status block also overwrote later kernel messages. This was reported by the
user and confirmed in a read-only GOP framebuffer capture.

The UEFI console now starts with automatic movement disabled. The proxy
implements m1n1's `P_FB_INIT=0xd00` and `P_FB_SHUTDOWN=0xd01` as ownership
controls for this already initialized GOP console. Shutdown requires
`restore_logo=0`, preserves scanout/pixels and suppresses status polling
draws across subsequent calls. INIT explicitly restores q1n1 drawing.
Both return the previous enabled state. `Proxy.fb_console(enabled)` exposes
the controls; the retained Apple-specific tree is unchanged.

Snapintosh yields the panel before entering the guest and keeps it yielded
after return. Kernel progress and failure messages therefore remain visible
while the Mac inspects the stopped guest. No new transport or shared USB
structure layout is introduced.

Validation: `make uefi`; 37 native proxy checks including retained ownership
and unsupported-option refusal; 42 xHCI/NCM checks; 48 NCM packet checks;
29 USB port lifecycle checks plus fallback/reset/identity suites; and
`test-uefi.py --proxy`, 39 QEMU checks with EL2 boot and both stage slots.
These results are in `build/uefi/panel-ownership-{build,qemu}-20260921.log`.

A16 deployment: stage generation 0 to 1 at `b0000000`, 637,576 bytes uploaded,
read back and verified in 0.21 seconds. Live ownership returns previous
state 1 followed by 0, proving it persists across requests. Stage SHA-256
`7efc5c96474851a216e29111d34572fdae49701b36fe58396817c6164bc6bd4a`.
`build/uefi/panel-ownership-chainload-20260921.log` records the result.
The final native run leaves ownership disabled after guest return. Two full
2880×1800 panel captures completed 27.97 seconds apart are byte-identical,
with readable late kernel logs and no status overlay. The PNG SHA-256 is
`7e972662af56fb03842bd9da5786abe83a8309969e105cb562efcec7e1c187b8`.
The final native kernel/pixel-stability check is recorded in
[Snapintosh's console evidence](../../Snapintosh/docs/PANEL-CONSOLE-20260921.md).

The deployed stage is in RAM. `build/uefi/q1n1.efi` is also rebuilt
(SHA-256 `795ca5ef56ef41d8876ff96425ce1a79dbecfc225213e91e19195760a072f5cd`),
but has not been installed on the ESP in this session.
