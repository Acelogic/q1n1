#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build tools/a16gpu-render/render-shaders.json: the ir3 variants of every `render` pipeline (host only, M3 Max).

For each scene of tools/a16gpu_render_scenes.py (vertex + fragment function of one wscts pipeline):
  1. the function constants the function declares (air2ir3 --list-fcs), every one set
     (0 unless the scene names it), the colour-attachment formats (MTLPixelFormat, class 0 for BGRA8/R8/RG8);
  2. the cache key (air2ir3 --key-only) looked up in the Stage 6 precompiled cache (CACHE-FORMAT.md 4: the whole
     cache is ignored unless its manifest's format/abi/chip/compiler_id equal this compiler's);
  3. the variant compiled online through the same entry point in every case; a cache hit must be byte-identical
     (sidecar and blob) -- so every entry here is both "what the cache holds" and "what an online compile gives";
  4. the pipeline const budget (air2ir3 --trim-constlen, BINDING-ABI 5.2): a stage the trim names is replaced by
     its safe_constlen variant (also looked up / compiled);
  5. the VS->FS varying map by name (BINDING-ABI 4.1) from the two sidecars;
  6. export_render_shaders (built against the same Mesa) reloads each blob with ir3_retrieve_variant and writes the
     fields the A16 tool programs, with Mesa's ir3_link_shaders for the named pairing.

  AIR2IR3_TREE=<stage6 tree> OUT=<scratch> tools/a16gpu-render/make-render-shaders.sh
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import a16gpu_render_scenes as SC  # noqa: E402

TREE = Path(os.environ['AIR2IR3_TREE'])
OUT = Path(os.environ['OUT'])
AIR2IR3 = TREE / 'build' / 'air2ir3'
CACHE = Path(os.environ.get('AIR2IR3_CACHE', TREE / 'cache'))


def run(*args, check=True):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if check and p.returncode:
        sys.exit(f'{args}: {p.stdout}{p.stderr}')
    return p


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compiler_id():
    return run(AIR2IR3, '--compiler-id').stdout.strip()


ABI = 6          # BINDING-ABI v6 (a16gpu_bindings.ABI_VERSION): UBO indices below 16


def cache_usable(cid):
    """The whole cache or nothing: its manifest's format/abi/chip/compiler_id must equal this compiler's
    (CACHE-FORMAT.md 4). A cache from another compiler build is ignored and every stage compiles online."""
    path = CACHE / 'manifest.json'
    if not path.is_file():
        return False, {}
    m = json.loads(path.read_text())
    ok = m.get('format_version') == 4 and m.get('abi_version') == ABI and m.get('chip_id') == '0x44070041' and \
        m.get('compiler_id') == cid
    return ok, m


def declared(bc, entry=None):
    """The function constants the function declares (air2ir3 --list-fcs: no compile, so a function whose
    unset constants would decide its body -- refused since ABI v4 -- still lists them)."""
    args = ['--entry', entry] if entry else []
    return json.loads(run(AIR2IR3, *args, '--list-fcs', bc).stdout)['function_constants']


MTL_OP = {'add': 0, 'subtract': 1, 'reverse_subtract': 2, 'min': 3, 'max': 4}
MTL_FACTOR = {'zero': 0, 'one': 1, 'source_alpha': 4, 'one_minus_source_alpha': 5, 'destination_alpha': 8,
              'one_minus_destination_alpha': 9}


def compile_stage(tag, bc, stage, fcs, rts, safe, use_cache, blend=None, textures=(), entry=None, extra=()):
    args = ['--quiet', '--stage', stage] + (['--entry', entry] if entry else []) + list(extra)
    for idx, val in sorted(SC.fc_resolve(declared(bc, entry), fcs).items()):
        args += ['--fc', f'{idx}={val}']
    for i, fmt in rts:
        args += ['--rt-format', f'{i}={SC.RT_FORMATS[fmt]["mtl"]}']
        b = (blend or {}).get(i)
        if b and b.get('enabled') and fmt in SC.XR_CLASS:       # blend is key material only for lowered attachments
            args += ['--blend', f'{i}=1,{MTL_OP[b["rgb_op"]]},{MTL_OP[b["alpha_op"]]},{MTL_FACTOR[b["src_rgb"]]},'
                                f'{MTL_FACTOR[b["dst_rgb"]]},{MTL_FACTOR[b["src_alpha"]]},{MTL_FACTOR[b["dst_alpha"]]}']
    for t in textures:
        if t['format'] in SC.XR_CLASS:                          # draw-time texture class (BINDING-ABI 3.3)
            args += ['--tex-class', f'{t["index"]}={SC.XR_CLASS[t["format"]]}']
    if safe:
        args += ['--safe-constlen']
    key = run(AIR2IR3, *args, '--key-only', bc).stdout.strip().split()[-1]
    side, blob = OUT / f'{tag}.json', OUT / f'{tag}.ir3v'
    run(AIR2IR3, *args, '--json', side, '--variant', blob, bc)
    s = json.loads(side.read_text())
    if s['key'] != key:
        sys.exit(f'{tag}: --key-only {key} != compiled key {s["key"]}')
    entry = CACHE / 'entries' / key[:2] / f'{key}.json'
    record = {'key': key, 'variant_id': s['variant_id'], 'args': args, 'sidecar_sha256': sha(side),
              'blob_sha256': sha(blob), 'safe_constlen': safe}
    if use_cache and entry.is_file():
        cached_blob = CACHE / s['blob']['path']
        if entry.read_bytes() != side.read_bytes() or cached_blob.read_bytes() != blob.read_bytes():
            sys.exit(f'{tag}: cache entry {key} differs from the online compile')
        src = json.loads(entry.with_name(f'{key}.src.json').read_text()) if entry.with_name(f'{key}.src.json').is_file() else {}
        record['cache'] = {'hit': True, 'id': src.get('id'), 'group': src.get('group'), 'identical_online': True}
    else:
        record['cache'] = {'hit': False, 'compiled': 'online (air2ir3_compile, same entry point)'}
    return s, side, blob, record


def varying_map(vs, fs):
    vn = {v['name']: v['slot'] for v in vs['reflection']['varyings']}
    out = []
    for v in fs['reflection']['varyings']:
        if v['name'] not in vn:
            sys.exit(f'FS varying {v["name"]} has no VS output of that name (a pipeline-creation error in Metal)')
        out.append((32 + v['slot'], 32 + vn[v['name']], v['name']))
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cid = compiler_id()
    use_cache, manifest = cache_usable(cid)
    lines, meta = [], {}
    probe_air = SC.PROBES / 'probes.air'
    if sha(probe_air) != SC.PROBE_AIR_SHA256:
        sys.exit(f'{probe_air}: not the pinned probes.air (a16gpu-render/probes/make-probes.sh, then re-pin)')
    for sc in SC.all_scenes():
        name = sc['name']
        if sc['lib'] == 'Probe':            # our own probe functions: one .air, by entry point
            vbc = fbc = probe_air
            ventry, fentry = sc['vs'], sc['fs']
        else:
            vbc = TREE / 'corpus' / sc['lib'] / f'{sc["vs"]}.bc'
            fbc = TREE / 'corpus' / sc['lib'] / f'{sc["fs"]}.bc'
            ventry = fentry = None
        extra = sc.get('compile_args', ())
        rts = [(r['index'], r['format']) for r in sc['rts']]
        vs, vside, vblob, vrec = compile_stage(f'{name}.vs', vbc, 'vertex', sc['fc']['vs'], [], False, use_cache,
                                               entry=ventry)
        fs, fside, fblob, frec = compile_stage(f'{name}.fs', fbc, 'fragment', sc['fc']['fs'], rts, False, use_cache,
                                               sc['blend'], sc['textures'], entry=fentry, extra=extra)
        vl, fl = vs['ir3']['stats']['constlen'], fs['ir3']['stats']['constlen']
        trim = int(run(AIR2IR3, '--trim-constlen', f'{vl},{fl}').stdout.strip().split()[-1], 0)
        if trim & 1:
            vs, vside, vblob, vrec = compile_stage(f'{name}.vs.safe', vbc, 'vertex', sc['fc']['vs'], [], True, use_cache,
                                                   entry=ventry)
        if trim & 2:
            fs, fside, fblob, frec = compile_stage(f'{name}.fs.safe', fbc, 'fragment', sc['fc']['fs'], rts, True,
                                                   use_cache, sc['blend'], sc['textures'], entry=fentry, extra=extra)
        vmap = varying_map(vs, fs)
        lines.append(f'{name} {vblob} {fblob} {vside} {fside} ' +
                     (','.join(f'{a}={b}' for a, b, _n in vmap) or '-'))
        meta[name] = {'lib': sc['lib'], 'vs_function': sc['vs'], 'fs_function': sc['fs'], 'trim_constlen_mask': trim,
                      'varying_map': [{'fs_slot': a, 'vs_slot': b, 'name': n} for a, b, n in vmap],
                      'compile': {'vs': vrec, 'fs': frec}, 'scene_digest': SC.scene_digest(sc), 'group': sc['group']}
    (OUT / 'list.txt').write_text('\n'.join(lines) + '\n')
    p = run(OUT / 'export_render_shaders', OUT / 'list.txt')
    doc = json.loads(p.stdout)
    for name, m in meta.items():
        doc['pipelines'][name].update(m)
    doc['compiler_id'] = cid
    doc['cache'] = {'usable': use_cache, 'manifest': manifest}   # no local path in the output
    doc['order'] = [sc['name'] for sc in SC.all_scenes()]
    out = OUT / 'render-shaders.json'
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
    hits = sum(m['compile'][s]['cache']['hit'] for m in meta.values() for s in ('vs', 'fs'))
    print(f'{out} {sha(out)}  ({len(meta)} pipelines, {hits}/{2 * len(meta)} stages hit the cache, compiler {cid})')


if __name__ == '__main__':
    main()
