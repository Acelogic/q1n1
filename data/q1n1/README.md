# q1n1 dragon boot emblem

Generated with the built-in image generation tool on 2026-09-16.
`dragon-source.png` is the original generated asset. The standalone PNGs in
`data/bootlogo_{48,128,256}.png` and corresponding RGBA `.bin` files replace the
fork's boot logo; they no longer point into the upstream artwork submodule.

Prompt:

> Use case: logo-brand. Asset type: square boot-screen emblem for q1n1, an experimental Snapdragon laptop bootloader. Create a polished original emblem: a compact stylized dragon head and curling flame-tail forming a subtle lowercase q silhouette, inspired by the idea of Snapdragon silicon. Strong simple silhouette, bold graceful geometry, minimal internal detail, clean vector-like raster rendering, flat crimson red with a small warm orange accent, high contrast on a perfectly solid pure black background. Center the mark in a square canvas with generous even black margins; emblem fills roughly 70% of the width and height. It must remain clear at 48, 128, and 256 pixels. No letters, no words, no gradients, no shadows, no mockup, no circuit-board background, no border, no Asahi artwork. This is an original community project mark, not the official Qualcomm or Snapdragon logo.

Packaging uses macOS `sips` to resize and `tools/package-logo.swift` to convert
the raster to opaque RGBA bytes. The UEFI target converts those bytes to the
GOP pixel format. The legacy build embeds the same three raw boot assets.
