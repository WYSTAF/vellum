#!/usr/bin/env python3
"""Generate Vellum's Windows application icon (multi-size .ico + PNG previews).

The mark is a "ribbon" — the same exchange-density strip the app shows for every
conversation: a warm (you) column and a cool (Claude) column read against a
charcoal ground, cut into a rounded tile.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# Render at 8x the target and downsample for clean edges.
CANVAS = 2048


def make_icon(size_px: int = CANVAS, *, tile: bool = True) -> Image.Image:
    s = size_px
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    u = s / 64.0  # design units

    # --- tile -------------------------------------------------------------
    if tile:
        r = 14.5 * u
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=(24, 23, 27, 255))
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, outline=(255, 255, 255, 26), width=max(1, int(1.2 * u)))
    else:
        r = 0

    # --- ribbon: 7 bars, density profile of a real conversation -----------
    # (kind, rel-height) kind: y=you, c=claude, t=tool
    bars = [
        ("y", 0.30),
        ("c", 0.62),
        ("t", 0.16),
        ("c", 0.40),
        ("y", 0.22),
        ("c", 0.86),
        ("t", 0.12),
    ]
    cols = {
        "y": (232, 163, 61, 255),      # --you / amber
        "c": (95, 189, 180, 255),      # --claude / teal
        "t": (140, 135, 168, 255),     # --machine / slate
    }

    x0, x1 = 16 * u, 48 * u
    gap = 2.6 * u
    n = len(bars)
    bw = (x1 - x0 - gap * (n - 1)) / n
    mid = s / 2.0
    max_h = 30 * u

    # The spine rule the bars hang from.
    d.line([9 * u, mid, s - 9 * u, mid], fill=(62, 61, 70, 255), width=max(1, int(1.1 * u)))

    for i, (kind, h) in enumerate(bars):
        hpx = max_h * h
        bx = x0 + i * (bw + gap)
        col = cols[kind]
        if kind == "t":
            # tool bars read as small hollow ticks on the rule
            d.rounded_rectangle([bx, mid - 2.4 * u, bx + bw, mid + 2.4 * u], radius=bw * 0.28, fill=col)
        else:
            up = i % 2 == 0
            y0, y1 = (mid - hpx, mid) if up else (mid, mid + hpx)
            d.rounded_rectangle([bx, y0, bx + bw, y1], radius=bw * 0.30, fill=col)

    # --- the read head: an unambiguous cursor across the ribbon -----------
    hy = mid - 15.5 * u
    d.line([11 * u, hy, s - 11 * u, hy], fill=(237, 235, 230, 230), width=max(1, int(1.3 * u)))
    tri = 4.2 * u
    d.polygon(
        [(11 * u, hy - tri), (11 * u, hy + tri), (11 * u + tri * 1.35, hy)],
        fill=(237, 235, 230, 255),
    )
    return img


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("assets")
    out_dir.mkdir(parents=True, exist_ok=True)

    base = make_icon(CANVAS)
    base.save(out_dir / "vellum_master.png")

    images = []
    for size in SIZES:
        im = make_icon(size * 8).resize((size, size), Image.LANCZOS)
        images.append(im)
        im.save(out_dir / f"vellum_{size}.png")

    ico_path = out_dir / "vellum.ico"
    images[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in SIZES])

    # A light-theme variant for taskbar/about artwork contrast checks.
    light = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(light)
    d.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=int(CANVAS * 0.226), fill=(246, 245, 242, 255))
    light.paste(base, (0, 0), None)
    light.save(out_dir / "vellum_light_bg.png")

    print(f"wrote {ico_path} ({ico_path.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
