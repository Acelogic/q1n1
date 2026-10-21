#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host-side prediction for `render`: the same scenes on Mesa llvmpipe, executing the NIR air2ir3 builds from the
same AIR (Stage 6 `nirexec`), compared with the M3 Max goldens under the same tolerance rule the A16 tool applies.

This is the expected-tolerance evidence for each function: llvmpipe runs the compiler's front end (AIR -> NIR,
function constants, constexpr samplers, the binding ABI) on a different rasteriser and float unit, so a function
that llvmpipe renders within tolerance of Metal has no front-end reason to fail on the A16; what is left for the
A16 is ir3's back end and the command stream. nirexec takes a single colour attachment in BGRA8/R8 and 2D textures,
so 7 scenes (RG8 or two attachments, 1D / 1D-array textures) have no prediction.

  AIR2IR3_TREE=<stage6 tree> python3 tools/a16gpu-render/llvmpipe_predict.py OUTDIR
"""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import a16gpu_render_scenes as SC  # noqa: E402
import a16gpu_render as RD  # noqa: E402

MTL_FACTOR = {'zero': 0, 'one': 1, 'source_alpha': 4, 'one_minus_source_alpha': 5, 'destination_alpha': 8,
              'one_minus_destination_alpha': 9}
MTL_OP = {'add': 0, 'subtract': 1, 'reverse_subtract': 2, 'min': 3, 'max': 4}


def supported(sc):
    if sc['group'] == 'probe':          # A16-only probes: their goldens are checked analytically (test-a16gpu-render.py);
        return False                    # nirexec's llvmpipe path does not take their fetch, A8 or UBO 16..30
    return len(sc['rts']) == 1 and sc['rts'][0]['index'] == 0 and \
        sc['rts'][0]['format'] in ('bgra8', 'r8', 'bgr10_xr', 'bgra10_xr') and all(t['type'] == '2d' for t in sc['textures'])


def convert(sc, tree, out):
    data = out / 'data'
    data.mkdir(parents=True, exist_ok=True)

    def put(tag, blob):
        p = data / f'{sc["name"]}.{tag}.bin'
        p.write_bytes(blob)
        return str(p)

    def bc(fn):
        return SC.PROBES / 'probes.air' if sc['lib'] == 'Probe' else tree / 'corpus' / sc['lib'] / f'{fn}.bc'

    def fc(stage, fn):
        entry = ['--entry', fn] if sc['lib'] == 'Probe' else []
        p = subprocess.run([tree / 'build' / 'air2ir3', *entry, '--list-fcs', bc(fn)], check=True, capture_output=True,
                           text=True)
        return {str(k): v for k, v in SC.fc_resolve(json.loads(p.stdout)['function_constants'], sc['fc'][stage]).items()}

    def st(stage, fn):
        d = {'bc': str(bc(fn)), 'metallib': SC.LIBS[sc['lib']], 'function': fn, 'fc': fc(stage, fn)}
        if sc['lib'] == 'Probe':
            d['entry'] = fn
        return d
    v = sc['vertex']
    scene = {'name': sc['name'], 'vs': st('vs', sc['vs']), 'fs': st('fs', sc['fs']),
             'vertex': {'stride': v['stride'], 'count': v['count'], 'primitive': 'triangle',
                        'attributes': v['attributes'], 'data': {'file': put('vtx', v['data'])} if v['data'] else {}},
             'buffers': {'vertex': [{'index': b['index'], 'device': b['device'], 'file': put(f'vb{b["index"]}', b['data'])}
                                    for b in sc['buffers']['vs']],
                         'fragment': [{'index': b['index'], 'device': b['device'], 'file': put(f'fb{b["index"]}', b['data'])}
                                      for b in sc['buffers']['fs']]},
             'textures': [{'index': t['index'], 'width': t['width'], 'height': t['height'], 'format': t['format'],
                           'file': put(f't{t["index"]}', t['data'])} for t in sc['textures']],
             'samplers': [{'index': s['index'], 'filter': s['mag'], 'address': s['address']} for s in sc['samplers']],
             'rt': {'format': sc['rts'][0]['format'], 'init_file': put('init', sc['rts'][0]['init'])},
             'width': sc['width'], 'height': sc['height'], 'clear': [0, 0, 0, 0], 'frames': 1, 'format': 'bgra8'}
    b = sc['blend'].get(0)
    if b and b.get('enabled'):
        scene['blend'] = {'enabled': 1, 'rgb_op': MTL_OP[b['rgb_op']], 'alpha_op': MTL_OP[b['alpha_op']],
                          'src_rgb': MTL_FACTOR[b['src_rgb']], 'dst_rgb': MTL_FACTOR[b['dst_rgb']],
                          'src_alpha': MTL_FACTOR[b['src_alpha']], 'dst_alpha': MTL_FACTOR[b['dst_alpha']]}
    (out / 'scenes').mkdir(exist_ok=True)
    path = out / 'scenes' / f'{sc["name"]}.json'
    path.write_text(json.dumps(scene, indent=1))
    return path


def main():
    tree = Path(os.environ['AIR2IR3_TREE'])
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    _shaders, goldens, _pins = RD.load_assets()
    report = {}
    for sc in SC.all_scenes():
        if not supported(sc):
            report[sc['name']] = {'prediction': None, 'why': 'A16 probe: analytic golden check' if sc['group'] == 'probe'
                                  else 'nirexec: one BGRA8/R8 attachment and 2D textures only'}
            continue
        path = convert(sc, tree, out)
        img = out / f'{sc["name"]}.lp.raw'
        p = subprocess.run([tree / 'build' / 'nirexec', path, img, '--frames', '1'], capture_output=True, text=True)
        if p.returncode:
            report[sc['name']] = {'prediction': None, 'why': f'nirexec failed: {(p.stdout + p.stderr).strip()[-300:]}'}
            continue
        rt = sc['rts'][0]
        cpp = RD.RT_HW[rt['format']][2]
        got = img.read_bytes()[:sc['width'] * sc['height'] * cpp]
        edges = RD.edge_pixels(sc['width'], sc['height'], sc['vertex']['triangles_px'])
        cmp = RD.compare(got, goldens[sc['name']]['images'][0], sc['width'], sc['height'], rt['format'],
                         sc['tolerance'], sc['edge_budget'], edges)
        cmp.pop('first_inside')
        report[sc['name']] = {'prediction': 'llvmpipe', **cmp}
        print(f'{sc["name"]:34s} ok={cmp["ok"]!s:5s} max {cmp["max_abs_diff"]:3d} mean {cmp["mean_abs_diff"]:7.4f} '
              f'inside>{sc["tolerance"]} {cmp["pixels_over_tolerance_inside"]:4d} edges {cmp["pixels_over_tolerance_on_edges"]}')
    (out / 'llvmpipe-vs-m3max.json').write_text(json.dumps(report, indent=1) + '\n')


if __name__ == '__main__':
    main()
