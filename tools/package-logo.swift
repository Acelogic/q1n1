// SPDX-License-Identifier: MIT
// Deterministic PNG -> opaque RGBA packaging for the boot framebuffer.
import AppKit
import Foundation

guard CommandLine.arguments.count == 3,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fatalError("usage: swift tools/package-logo.swift INPUT.png OUTPUT.bin")
}
let width = cg.width, height = cg.height
var pixels = [UInt8](repeating: 0, count: width * height * 4)
pixels.withUnsafeMutableBytes { bytes in
    let ctx = CGContext(data: bytes.baseAddress, width: width, height: height,
                        bitsPerComponent: 8, bytesPerRow: width * 4,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.setFillColor(NSColor.black.cgColor)
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
}
try Data(pixels).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
print("\(width)x\(height): \(pixels.count) RGBA bytes")
