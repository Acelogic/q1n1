#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Look at a `render` run: a contact sheet of every target the A16 wrote, next to its M3 Max golden and the
per-pixel difference (x8), plus the numbers `render` computed. Host only; needs Pillow.

  python3 tools/a16gpu-render/sheet.py build/a16-gpu/stage3-render-<time>.json out.png

The readbacks are the files `render` saved next to its JSON (<stem>.<function>.rt<i>.raw, tight rows).
"""
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import a16gpu_render as RD  # noqa: E402
import a16gpu_render_scenes as SC  # noqa: E402


def to_rgb(raw, fmt, w, h):
    if fmt in ('bgr10_xr', 'bgra10_xr'):            # XR codes -> [0, 1] for display (values outside are clipped)
        im = Image.new('RGB', (w, h))
        px = im.load()
        for i in range(w * h):
            b, g, r = RD.channels(raw, fmt, i)[:3]
            px[i % w, i // w] = tuple(max(0, min(255, int((n - 384) / 510 * 255))) for n in (r, g, b))
        return im
    if fmt == 'bgra8':
        return Image.frombytes('RGBA', (w, h), raw, 'raw', 'BGRA').convert('RGB')
    im = Image.new('RGB', (w, h))
    px = im.load()
    cpp = RD.RT_HW[fmt][2]
    for i in range(w * h):
        c = raw[cpp * i:cpp * i + cpp]
        px[i % w, i // w] = (c[0], c[1] if cpp > 1 else c[0], 0 if cpp > 1 else c[0])
    return im


def main():
    run = Path(sys.argv[1])
    out = json.loads(run.read_text())
    _shaders, goldens, _pins = RD.load_assets()
    stem = run.with_suffix('')
    tiles = []
    for sc in SC.all_scenes():
        res = out.get('render', {}).get('results', {}).get(sc['name'])
        for r in sc['rts']:
            path = Path(f'{stem}.{sc["name"]}.rt{r["index"]}.raw')
            if not path.is_file():
                continue
            got = path.read_bytes()
            gold = goldens[sc['name']]['images'][r['index']]
            w, h = sc['width'], sc['height']
            diff = bytes(min(255, 8 * abs(a - b)) for a, b in zip(got, gold))   # raw bytes (XR: indicative)
            cmp = next((c for c in (res or {}).get('rts', []) if c['index'] == r['index']), {})
            label = f'{sc["name"]} rt{r["index"]} max {cmp.get("max_abs_diff")} {"ok" if cmp.get("ok") else "OVER"}'
            tiles.append((label, [to_rgb(x, r['format'], w, h) for x in (got, gold, diff)]))
    scale, pad = 2, 6
    tw, th = 3 * (96 * scale + pad), 64 * scale + 16
    sheet = Image.new('RGB', (2 * tw, ((len(tiles) + 1) // 2) * th), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    for k, (label, ims) in enumerate(tiles):
        x0, y0 = (k % 2) * tw, (k // 2) * th
        draw.text((x0 + 2, y0 + 1), label[:70], fill=(255, 255, 255))
        for j, im in enumerate(ims):
            sheet.paste(im.resize((96 * scale, 64 * scale), Image.NEAREST), (x0 + j * (96 * scale + pad), y0 + 14))
    sheet.save(sys.argv[2])
    print(f'{sys.argv[2]}: {len(tiles)} targets (A16 | M3 Max golden | |diff| x8)')


if __name__ == '__main__':
    main()
