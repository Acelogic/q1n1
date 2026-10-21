// SPDX-License-Identifier: MIT
// render_metal_golden: the M3 Max Metal goldens for `a16gpu.py render` (Stage 6's A16 gate).
//
//   python3 tools/a16gpu_render_scenes.py --write-scenes /tmp/rscenes
//   swiftc -O tools/a16gpu-render/render_metal_golden.swift -o /tmp/rgold
//   /tmp/rgold /tmp/rscenes tools/a16gpu-render/goldens
//
// For every scene in scenes.json (the exact bytes the A16 tool uploads): build the render pipeline from the OS
// metallib (sha256-checked) with every declared function constant set (0 unless the scene names it), bind the
// buffers at their Metal indices (vertex data at buffer 30, as wscts did), the textures and API samplers, load
// every colour attachment with its initial contents, draw, and read back. Three frames, each its own command buffer
// (load + draw); frame 2 is written and must equal frame 0. Output: <out>/<name>.rt<i>.raw (tightly packed rows,
// Metal's byte order: BGRA8 B,G,R,A; RG8 R,G; R8 R) and <out>/goldens.json (device, OS, per image sha256).
import Foundation
import Metal
import CryptoKit

func fail(_ m: String) -> Never { FileHandle.standardError.write(("render_metal_golden: " + m + "\n").data(using: .utf8)!); exit(3) }
func sha256(_ d: [UInt8]) -> String { SHA256.hash(data: Data(d)).map { String(format: "%02x", $0) }.joined() }

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: render_metal_golden SCENEDIR OUTDIR [NAME ...]") }
let sceneDir = URL(fileURLWithPath: args[1]), outDir = URL(fileURLWithPath: args[2])
let only = Set(args.dropFirst(3))
try! FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let index = try! JSONSerialization.jsonObject(with: Data(contentsOf: sceneDir.appendingPathComponent("scenes.json"))) as! [String: Any]
let scenes = index["scenes"] as! [[String: Any]]
guard let dev = MTLCreateSystemDefaultDevice() else { fail("no Metal device") }
let queue = dev.makeCommandQueue()!
func file(_ rel: String) -> [UInt8] { [UInt8](try! Data(contentsOf: sceneDir.appendingPathComponent(rel))) }

var libs: [String: MTLLibrary] = [:]
func library(_ path: String, _ sha: String?) -> MTLLibrary {
    if let l = libs[path] { return l }
    let bytes = [UInt8](try! Data(contentsOf: URL(fileURLWithPath: path)))
    if let want = sha, sha256(bytes) != want { fail("\(path): sha256 \(sha256(bytes)) is not the pinned \(want)") }
    let l = try! dev.makeLibrary(URL: URL(fileURLWithPath: path))
    libs[path] = l
    return l
}

func function(_ lib: MTLLibrary, _ name: String, _ given: [String: Any]) -> MTLFunction {
    guard let f0 = lib.makeFunction(name: name) else { fail("no function \(name)") }
    let fcv = MTLFunctionConstantValues()
    var names = Set(given.keys)
    for (_, decl) in f0.functionConstantsDictionary {
        let v = (given[decl.name] as? NSNumber) ?? 0
        names.remove(decl.name)
        switch decl.type {
        case .bool: var b = v.intValue != 0; fcv.setConstantValue(&b, type: .bool, index: decl.index)
        case .uchar: var b = UInt8(v.intValue); fcv.setConstantValue(&b, type: .uchar, index: decl.index)
        case .int: var b = Int32(v.intValue); fcv.setConstantValue(&b, type: .int, index: decl.index)
        case .uint: var b = UInt32(v.uint32Value); fcv.setConstantValue(&b, type: .uint, index: decl.index)
        case .float: var b = v.floatValue; fcv.setConstantValue(&b, type: .float, index: decl.index)
        default: fail("\(name): function constant \(decl.name) of type \(decl.type.rawValue) not handled")
        }
    }
    if !names.isEmpty { fail("\(name): constants \(names) are not declared") }
    return try! lib.makeFunction(name: name, constantValues: fcv)
}

func vertexFormat(_ s: String) -> MTLVertexFormat {
    switch s { case "float": return .float; case "float2": return .float2; case "float3": return .float3
    case "float4": return .float4; default: fail("vertex format \(s)") }
}
func rtFormat(_ s: String) -> (MTLPixelFormat, Int) {
    switch s { case "bgra8": return (.bgra8Unorm, 4); case "rg8": return (.rg8Unorm, 2); case "r8": return (.r8Unorm, 1)
    case "bgr10_xr": return (.bgr10_xr, 4); case "bgra10_xr": return (.bgra10_xr, 8)
    default: fail("render-target format \(s)") }
}
func texFormat(_ s: String) -> (MTLPixelFormat, Int) {
    switch s { case "rgba8": return (.rgba8Unorm, 4); case "rgba16f": return (.rgba16Float, 8)
    case "bgr10_xr": return (.bgr10_xr, 4); case "bgra10_xr": return (.bgra10_xr, 8); case "a8": return (.a8Unorm, 1)
    default: fail("texture format \(s)") }
}
func blendFactor(_ s: String) -> MTLBlendFactor {
    switch s { case "zero": return .zero; case "one": return .one; case "source_alpha": return .sourceAlpha
    case "one_minus_source_alpha": return .oneMinusSourceAlpha; case "destination_alpha": return .destinationAlpha
    case "one_minus_destination_alpha": return .oneMinusDestinationAlpha; default: fail("blend factor \(s)") }
}
func blendOp(_ s: String) -> MTLBlendOperation {
    switch s { case "add": return .add; case "subtract": return .subtract; case "reverse_subtract": return .reverseSubtract
    case "min": return .min; case "max": return .max; default: fail("blend op \(s)") }
}
func address(_ s: String) -> MTLSamplerAddressMode {
    switch s { case "clamp_to_edge": return .clampToEdge; case "repeat": return .repeat
    case "mirrored_repeat": return .mirrorRepeat; case "clamp_to_zero": return .clampToZero; default: fail("address \(s)") }
}

var manifest: [[String: Any]] = []
for sc in scenes {
    let name = sc["name"] as! String
    if !only.isEmpty && !only.contains(name) { continue }
    let W = sc["width"] as! Int, H = sc["height"] as! Int
    let lib = library(sc["metallib"] as! String, sc["metallib_sha256"] as? String)
    let fc = sc["fc"] as! [String: [String: Any]]
    let pd = MTLRenderPipelineDescriptor()
    pd.vertexFunction = function(lib, sc["vs"] as! String, fc["vs"] ?? [:])
    pd.fragmentFunction = function(lib, sc["fs"] as! String, fc["fs"] ?? [:])
    let vj = sc["vertex"] as! [String: Any]
    let attrs = vj["attributes"] as! [[String: Any]]
    if !attrs.isEmpty {
        let vd = MTLVertexDescriptor()
        for a in attrs {
            let i = a["index"] as! Int
            vd.attributes[i].format = vertexFormat(a["format"] as! String)
            vd.attributes[i].offset = a["offset"] as! Int
            vd.attributes[i].bufferIndex = 30
        }
        vd.layouts[30].stride = vj["stride"] as! Int
        pd.vertexDescriptor = vd
    }
    let rts = sc["rts"] as! [[String: Any]]
    let blend = sc["blend"] as! [String: [String: Any]]
    var targets: [(Int, MTLTexture, Int, [UInt8])] = []
    for r in rts {
        let i = r["index"] as! Int
        let (fmt, bpp) = rtFormat(r["format"] as! String)
        pd.colorAttachments[i].pixelFormat = fmt
        if let b = blend[String(i)], (b["enabled"] as? Bool) == true {
            let ca = pd.colorAttachments[i]!
            ca.isBlendingEnabled = true
            ca.rgbBlendOperation = blendOp(b["rgb_op"] as! String); ca.alphaBlendOperation = blendOp(b["alpha_op"] as! String)
            ca.sourceRGBBlendFactor = blendFactor(b["src_rgb"] as! String); ca.destinationRGBBlendFactor = blendFactor(b["dst_rgb"] as! String)
            ca.sourceAlphaBlendFactor = blendFactor(b["src_alpha"] as! String); ca.destinationAlphaBlendFactor = blendFactor(b["dst_alpha"] as! String)
        }
        let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: fmt, width: W, height: H, mipmapped: false)
        td.usage = [.renderTarget, .shaderRead]; td.storageMode = .shared
        let initial = file(r["file"] as! String)
        if initial.count != W * H * bpp { fail("\(name): rt\(i) initial contents are \(initial.count) bytes") }
        targets.append((i, dev.makeTexture(descriptor: td)!, bpp, initial))
    }
    let pso: MTLRenderPipelineState
    do { pso = try dev.makeRenderPipelineState(descriptor: pd) } catch { fail("\(name): pipeline: \(error)") }

    var textures: [(Int, MTLTexture)] = []
    for t in sc["textures"] as! [[String: Any]] {
        let (fmt, bpp) = texFormat(t["format"] as! String)
        let w = t["width"] as! Int, h = t["height"] as! Int, layers = t["layers"] as! Int
        let td = MTLTextureDescriptor()
        td.pixelFormat = fmt; td.width = w; td.height = h; td.usage = [.shaderRead]; td.storageMode = .shared
        switch t["type"] as! String {
        case "2d": td.textureType = .type2D
        case "1d": td.textureType = .type1D
        case "1d_array": td.textureType = .type1DArray; td.arrayLength = layers
        default: fail("texture type") }
        let tex = dev.makeTexture(descriptor: td)!
        let data = file(t["file"] as! String)
        if data.count != w * h * layers * bpp { fail("\(name): texture \(t["index"]!) is \(data.count) bytes") }
        for layer in 0..<layers {
            let off = layer * w * h * bpp
            data.withUnsafeBytes { tex.replace(region: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0, slice: layer,
                                                withBytes: $0.baseAddress! + off, bytesPerRow: w * bpp, bytesPerImage: w * h * bpp) }
        }
        textures.append((t["index"] as! Int, tex))
    }
    var samplers: [(Int, MTLSamplerState)] = []
    for s in sc["samplers"] as! [[String: Any]] {
        let sd = MTLSamplerDescriptor()
        sd.minFilter = (s["min"] as! String) == "linear" ? .linear : .nearest
        sd.magFilter = (s["mag"] as! String) == "linear" ? .linear : .nearest
        let am = address(s["address"] as! String)
        sd.sAddressMode = am; sd.tAddressMode = am; sd.rAddressMode = am
        samplers.append((s["index"] as! Int, dev.makeSamplerState(descriptor: sd)!))
    }
    let bufs = sc["buffers"] as! [String: [[String: Any]]]
    var mbufs: [(String, Int, MTLBuffer)] = []
    for st in ["vs", "fs"] {
        for b in bufs[st] ?? [] {
            let d = file(b["file"] as! String)
            mbufs.append((st, b["index"] as! Int, dev.makeBuffer(bytes: d, length: d.count, options: .storageModeShared)!))
        }
    }
    let vfile = vj["file"] as? String
    let vbuf: MTLBuffer? = vfile.map { let d = file($0); return dev.makeBuffer(bytes: d, length: d.count, options: .storageModeShared)! }

    var frames: [[[UInt8]]] = []
    for _ in 0..<3 {
        let rp = MTLRenderPassDescriptor()
        for (i, tex, bpp, initial) in targets {
            initial.withUnsafeBytes { tex.replace(region: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: W * bpp) }
            rp.colorAttachments[i].texture = tex; rp.colorAttachments[i].loadAction = .load; rp.colorAttachments[i].storeAction = .store
        }
        let cb = queue.makeCommandBuffer()!
        let enc = cb.makeRenderCommandEncoder(descriptor: rp)!
        enc.setRenderPipelineState(pso)
        enc.setCullMode(.none)
        if let vb = vbuf { enc.setVertexBuffer(vb, offset: 0, index: 30) }
        for (st, i, b) in mbufs { if st == "vs" { enc.setVertexBuffer(b, offset: 0, index: i) } else { enc.setFragmentBuffer(b, offset: 0, index: i) } }
        for (i, t) in textures { enc.setFragmentTexture(t, index: i) }
        for (i, s) in samplers { enc.setFragmentSamplerState(s, index: i) }
        enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: vj["count"] as! Int)
        enc.endEncoding()
        cb.commit(); cb.waitUntilCompleted()
        if let e = cb.error { fail("\(name): GPU error \(e)") }
        var images: [[UInt8]] = []
        for (_, tex, bpp, _) in targets {
            var out = [UInt8](repeating: 0, count: W * H * bpp)
            tex.getBytes(&out, bytesPerRow: W * bpp, from: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0)
            images.append(out)
        }
        frames.append(images)
    }
    if frames[0] != frames[2] { fail("\(name): frame 0 differs from frame 2") }
    var outs: [[String: Any]] = []
    for (k, (i, _, bpp, initial)) in targets.enumerated() {
        let img = frames[2][k]
        let path = outDir.appendingPathComponent("\(name).rt\(i).raw")
        try! Data(img).write(to: path)
        var distinct = Set<[UInt8]>()
        var changed = 0
        for p in 0..<(W * H) {
            let px = Array(img[p * bpp..<(p + 1) * bpp])
            distinct.insert(px)
            if px != Array(initial[p * bpp..<(p + 1) * bpp]) { changed += 1 }
        }
        outs.append(["index": i, "file": path.lastPathComponent, "bytes": img.count, "sha256": sha256(img),
                     "distinct_values": distinct.count, "pixels_changed_by_draw": changed])
    }
    manifest.append(["name": name, "digest": sc["digest"] as! String, "rts": outs])
    print("render_metal_golden: \(name): " + outs.map { "rt\($0["index"]!) \(($0["sha256"] as! String).prefix(16)) distinct \($0["distinct_values"]!) changed \($0["pixels_changed_by_draw"]!)" }.joined(separator: ", "))
}
let os = ProcessInfo.processInfo.operatingSystemVersionString
let doc: [String: Any] = ["format": 1, "device": dev.name, "os": os, "frames": 3, "frame_written": 2, "goldens": manifest]
let data = try! JSONSerialization.data(withJSONObject: doc, options: [.prettyPrinted, .sortedKeys])
try! data.write(to: outDir.appendingPathComponent(only.isEmpty ? "goldens.json" : "goldens.partial.json"))
