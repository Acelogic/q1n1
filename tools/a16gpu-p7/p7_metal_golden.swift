// SPDX-License-Identifier: MIT
// p7_metal_golden: render the A16 P7 scene with real Metal on this Mac (M3 Max) and write the colour
// attachment's raw BGRA8 bytes. The scene (pipeline, vertices, matrix, texture, sampler, clear) is written by
// `python3 tools/a16gpu_p6p7.py --write-scene DIR`, the single source of truth the A16 tool uploads from, so
// the golden and the Adreno draw use byte-identical inputs.
//
//   python3 tools/a16gpu_p6p7.py --write-scene /tmp/p7scene
//   swiftc -O tools/a16gpu-p7/p7_metal_golden.swift -o /tmp/p7gold && /tmp/p7gold /tmp/p7scene tools/a16gpu-p7/p7-golden-m3max.bgra
//
// The functions are SkyLight's own SimpleVertex and SimpleTextureFragment from the OS metallib (its sha256
// is checked against the scene's pin). Three frames are rendered as separate command buffers (clear + draw),
// frame 2 is written and compared with frame 0, as the Stage 6 goldens (mtlgold.swift) do.
import Foundation
import Metal
import CryptoKit

func fail(_ m: String) -> Never { FileHandle.standardError.write(("p7_metal_golden: " + m + "\n").data(using: .utf8)!); exit(3) }

let args = CommandLine.arguments
guard args.count == 3 else { fail("usage: p7_metal_golden SCENE_DIR out.bgra") }
let dir = URL(fileURLWithPath: args[1])
let scene = try! JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("scene.json"))) as! [String: Any]
let W = scene["width"] as! Int, H = scene["height"] as! Int
let libPath = scene["metallib"] as! String
let libData = try! Data(contentsOf: URL(fileURLWithPath: libPath))
let libSha = SHA256.hash(data: libData).map { String(format: "%02x", $0) }.joined()
guard libSha == scene["metallib_sha256"] as! String else { fail("\(libPath) sha256 \(libSha) is not the scene's pin") }

guard let dev = MTLCreateSystemDefaultDevice() else { fail("no Metal device") }
let lib = try! dev.makeLibrary(URL: URL(fileURLWithPath: libPath))
let pd = MTLRenderPipelineDescriptor()
pd.vertexFunction = lib.makeFunction(name: scene["vertex_function"] as! String)
pd.fragmentFunction = lib.makeFunction(name: scene["fragment_function"] as! String)
guard pd.vertexFunction != nil, pd.fragmentFunction != nil else { fail("functions not in the library") }
pd.colorAttachments[0].pixelFormat = .bgra8Unorm
let vd = MTLVertexDescriptor()
for (i, a) in (scene["attributes"] as! [[String: Any]]).enumerated() {
    guard a["format"] as! String == "float2" else { fail("attribute format") }
    vd.attributes[i].format = .float2
    vd.attributes[i].offset = a["offset"] as! Int
    vd.attributes[i].bufferIndex = 0
}
vd.layouts[0].stride = scene["stride"] as! Int
pd.vertexDescriptor = vd
let pso = try! dev.makeRenderPipelineState(descriptor: pd)

let rtd = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: W, height: H, mipmapped: false)
rtd.usage = [.renderTarget, .shaderRead]; rtd.storageMode = .shared
let rt = dev.makeTexture(descriptor: rtd)!

let tex = scene["texture"] as! [String: Any]
let tw = tex["width"] as! Int, th = tex["height"] as! Int
guard tex["format"] as! String == "rgba8" else { fail("texture format") }
let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: tw, height: th, mipmapped: false)
td.usage = [.shaderRead]; td.storageMode = .shared
let t = dev.makeTexture(descriptor: td)!
let texels = try! Data(contentsOf: dir.appendingPathComponent(tex["file"] as! String))
guard texels.count == tw * th * 4 else { fail("texture file size") }
texels.withUnsafeBytes { t.replace(region: MTLRegionMake2D(0, 0, tw, th), mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: tw * 4) }

let sj = scene["sampler"] as! [String: Any]
let sd = MTLSamplerDescriptor()
guard sj["filter"] as! String == "linear", sj["address"] as! String == "clamp_to_edge" else { fail("sampler") }
sd.minFilter = .linear; sd.magFilter = .linear; sd.mipFilter = .notMipmapped
sd.sAddressMode = .clampToEdge; sd.tAddressMode = .clampToEdge; sd.rAddressMode = .clampToEdge
let samp = dev.makeSamplerState(descriptor: sd)!

func floats(_ key: String) -> [Float] { (scene[key] as! [NSNumber]).map { $0.floatValue } }
let vtx = floats("vertices"), mvp = floats("mvp"), clear = floats("clear")
let vb = dev.makeBuffer(bytes: vtx, length: vtx.count * 4, options: .storageModeShared)!
let ub = dev.makeBuffer(bytes: mvp, length: mvp.count * 4, options: .storageModeShared)!
let q = dev.makeCommandQueue()!
var out = [UInt8](repeating: 0, count: W * H * 4), first = [UInt8]()
for f in 0..<3 {
    let rp = MTLRenderPassDescriptor()
    rp.colorAttachments[0].texture = rt; rp.colorAttachments[0].loadAction = .clear; rp.colorAttachments[0].storeAction = .store
    rp.colorAttachments[0].clearColor = MTLClearColor(red: Double(clear[0]), green: Double(clear[1]), blue: Double(clear[2]), alpha: Double(clear[3]))
    let cb = q.makeCommandBuffer()!
    let enc = cb.makeRenderCommandEncoder(descriptor: rp)!
    enc.setRenderPipelineState(pso)
    enc.setCullMode(.none)
    enc.setVertexBuffer(vb, offset: 0, index: 0)
    enc.setVertexBuffer(ub, offset: 0, index: scene["mvp_buffer_index"] as! Int)
    enc.setFragmentTexture(t, index: 0)
    enc.setFragmentSamplerState(samp, index: 0)
    enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: scene["vertex_count"] as! Int)
    enc.endEncoding()
    cb.commit(); cb.waitUntilCompleted()
    rt.getBytes(&out, bytesPerRow: W * 4, from: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0)
    if f == 0 { first = out }
}
try! Data(out).write(to: URL(fileURLWithPath: args[2]))
var d0 = 0
for i in 0..<out.count { d0 = max(d0, abs(Int(out[i]) - Int(first[i]))) }
let outSha = SHA256.hash(data: Data(out)).map { String(format: "%02x", $0) }.joined()
print("p7_metal_golden: \(args[2]) \(W)x\(H) bgra8 on \(dev.name), metallib \(libSha.prefix(16)), sha256 \(outSha), max |frame0 - frame2| = \(d0)")
