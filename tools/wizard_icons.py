#!/usr/bin/env python3
"""Wizard icon family: one glass orb (Wizard UX style) per product, each with its own colours and glyph.

    python3 tools/wizard_icons.py OUT_DIR

Writes OUT_DIR/<name>-<size>.png at 1024, 512, 192, 180, 96, 48, 32, 16 for:
  oscar     Oscar (personal Wizard App)   violet -> pink, wizard hat
  wizardos  Wizard OS (James's Wizard UX) cyan -> violet, sparkle
  wam       Wizard App Manager            mint -> blue, app graph (three linked nodes)
Every icon is full-bleed with the orb inside the 80% maskable safe zone, so one image serves
"any", "maskable" and apple-touch.
"""
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 1024
BG = np.array([5, 3, 12], dtype=np.float32)

PALETTES = {
    "oscar": ((143, 99, 255), (255, 92, 207)),
    "wizardos": ((63, 224, 255), (63, 107, 255)),
    "wam": ((79, 240, 176), (63, 107, 255)),
}


def hexrgb(c):
    return np.array(c, dtype=np.float32)


def smooth(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def orb(cool, warm, small=False):
    """Dark glass orb, light from top-left, cool rim top/right, warm caustic bottom-left."""
    y, x = np.mgrid[0:S, 0:S].astype(np.float32) + 0.5
    cx = cy = S / 2
    R = S * (0.43 if small else 0.36)
    dx, dy = x - cx, y - cy
    r = np.sqrt(dx * dx + dy * dy) / R
    ang = np.arctan2(dy, dx)
    cool, warm = hexrgb(cool), hexrgb(warm)

    # Background: near-black with a faint tint of the cool colour behind the orb.
    img = np.broadcast_to(BG, (S, S, 3)).copy()
    halo = np.exp(-((r - 0.2) ** 2) / 1.6)[..., None] * 0.10
    img = img * (1 - halo) + cool * halo

    inside = (r <= 1.0)[..., None]
    # Rim colour by angle: warm toward bottom-left (135 deg in screen coords), cool elsewhere.
    w = ((1 + np.cos(ang - math.radians(135))) / 2) ** 1.6
    rim = cool * (1 - w[..., None]) + warm * w[..., None]
    core = np.array([22, 14, 52], dtype=np.float32)
    # Lit body: a vertical volume gradient (lighter low, where light refracts through) plus the coloured rim.
    vol = smooth(-1.0, 1.0, dy / R)[..., None]
    core = core * (1 - vol * 0.55) + (cool * 0.35 + warm * 0.25) * vol * 0.55
    glow = smooth(0.30, 1.0, r) ** 1.35
    body = core * (1 - glow[..., None]) + rim * glow[..., None]
    # Soft top-left window highlight.
    hx, hy = cx - R * 0.38, cy - R * 0.42
    hl = np.exp(-(((x - hx) / (R * 0.42)) ** 2 + ((y - hy) / (R * 0.30)) ** 2))[..., None]
    body = body + (np.array([215, 205, 255], dtype=np.float32) - body) * hl * 0.30
    # Crisp curved specular window near the top.
    sx, sy = cx - R * 0.10, cy - R * 0.70
    sp = smooth(0.95, 0.70, np.sqrt(((x - sx) / (R * 0.52)) ** 2 + ((y - sy) / (R * 0.16)) ** 2))
    sp = sp * smooth(-0.05, 0.25, ((y - (sy - R * 0.02)) / R) * -1 + 0.2)
    body = body + (np.array([255, 255, 255], dtype=np.float32) - body) * sp[..., None] * 0.55
    # Warm caustic pooling on the bottom-left inner edge.
    cx2, cy2 = cx - R * 0.35, cy + R * 0.55
    ca = np.exp(-(((x - cx2) / (R * 0.45)) ** 2 + ((y - cy2) / (R * 0.22)) ** 2))[..., None]
    body = body + (warm - body) * ca * 0.45
    img = np.where(inside, body, img)

    # Hairline rim (brighter at the bottom), antialiased.
    # Antialiased edge plus a thin bright bounce only along the bottom (no flat sticker outline).
    edge = smooth(1.0 + 1.5 / R, 1.0 - 1.5 / R, r)[..., None]
    img = img * edge + np.broadcast_to(BG, (S, S, 3)) * (1 - edge) if False else img
    line = np.exp(-((r - 0.985) * R / (3.0 if not small else 4.0)) ** 2)
    bottom = smooth(0.15, 1.0, np.sin(ang)) ** 1.4
    rimc = np.array([235, 236, 255], dtype=np.float32)
    img = img + (rimc - img) * (line * bottom)[..., None] * 0.75
    # Antialias the orb edge against the background.
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB"), R


def bez(p0, p1, p2, p3, n=48):
    pts = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def sparkle(d, cx, cy, r, inner=0.18, fill=(255, 255, 255, 255)):
    """Four-point star with concave sides (Wizard UX's ✦)."""
    pts = []
    for k in range(4):
        a = math.radians(-90 + 90 * k)
        tip = (cx + r * math.cos(a), cy + r * math.sin(a))
        a2 = math.radians(-90 + 90 * (k + 1))
        tip2 = (cx + r * math.cos(a2), cy + r * math.sin(a2))
        mid = math.radians(-45 + 90 * k)
        ctrl = (cx + r * inner * math.cos(mid), cy + r * inner * math.sin(mid))
        pts += bez(tip, ctrl, ctrl, tip2, 24)[:-1]
    d.polygon(pts, fill=fill)


def glyph_layer(name, R, small=False):
    """White-lavender glyph drawn at 2x, returned at S with a soft glow underneath."""
    k = 2
    G = Image.new("RGBA", (S * k, S * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(G)
    c = S * k / 2
    u = R * k  # orb radius in layer pixels
    ink = (246, 242, 255, 255)
    if name == "wizardos":
        sparkle(d, c, c + u * 0.02, u * (0.62 if small else 0.56), inner=0.16, fill=ink)
        if not small:
            sparkle(d, c + u * 0.46, c - u * 0.42, u * 0.15, inner=0.2, fill=ink)
    elif name == "oscar":
        # Wizard hat: soft cone whose tip droops over to the left, thick brim, violet band, star.
        s = 1.1 if small else 1.0
        oy = u * 0.06  # sit a little lower in the orb
        bl, br = (c - u * 0.42 * s, c + u * 0.28 * s + oy), (c + u * 0.42 * s, c + u * 0.28 * s + oy)
        peak = (c + u * 0.02 * s, c - u * 0.50 * s + oy)
        tip = (c - u * 0.40 * s, c - u * 0.30 * s + oy)
        left = bez(bl, (c - u * 0.30 * s, c - u * 0.02 * s + oy), (c - u * 0.18 * s, c - u * 0.30 * s + oy), peak)
        flop_top = bez(peak, (c - u * 0.10 * s, c - u * 0.62 * s + oy), (c - u * 0.32 * s, c - u * 0.52 * s + oy), tip)
        flop_under = bez(tip, (c - u * 0.26 * s, c - u * 0.40 * s + oy), (c - u * 0.06 * s, c - u * 0.44 * s + oy), (c + u * 0.08 * s, c - u * 0.36 * s + oy))
        right = bez((c + u * 0.08 * s, c - u * 0.36 * s + oy), (c + u * 0.22 * s, c - u * 0.10 * s + oy), (c + u * 0.32 * s, c + u * 0.08 * s + oy), br)
        # Cone and drooping tip are drawn as two overlapping shapes so they merge with no gap or hole.
        right_from_peak = bez(peak, (c + u * 0.14 * s, c - u * 0.22 * s + oy), (c + u * 0.30 * s, c + u * 0.04 * s + oy), br)
        d.polygon(left + right_from_peak[1:], fill=ink)
        d.polygon(flop_top + flop_under[1:] + [(c + u * 0.04 * s, c - u * 0.30 * s + oy), (c - u * 0.10 * s, c - u * 0.30 * s + oy)], fill=ink)
        # Brim with thickness: lighter top ellipse over a slightly darker underside.
        d.ellipse([c - u * 0.70 * s, c + u * 0.19 * s + oy, c + u * 0.70 * s, c + u * 0.47 * s + oy], fill=(196, 182, 255, 255))
        d.ellipse([c - u * 0.70 * s, c + u * 0.16 * s + oy, c + u * 0.70 * s, c + u * 0.41 * s + oy], fill=ink)
        band = (124, 82, 255, 255)
        d.polygon(bez((c - u * 0.40 * s, c + u * 0.10 * s + oy), (c - u * 0.1 * s, c + u * 0.17 * s + oy), (c + u * 0.1 * s, c + u * 0.17 * s + oy), (c + u * 0.40 * s, c + u * 0.10 * s + oy))
                  + bez((c + u * 0.43 * s, c + u * 0.24 * s + oy), (c + u * 0.1 * s, c + u * 0.32 * s + oy), (c - u * 0.1 * s, c + u * 0.32 * s + oy), (c - u * 0.43 * s, c + u * 0.24 * s + oy)), fill=band)
        if not small:
            sparkle(d, c + u * 0.06 * s, c - u * 0.10 * s + oy, u * 0.12, inner=0.2, fill=band)
    elif name == "wam":
        # App graph: interface -> app -> system app, joined by wires.
        sz = 1.05 if small else 0.92
        nodes = [(c - u * 0.50 * sz, c - u * 0.26 * sz, u * 0.15 * sz), (c, c + u * 0.02 * sz, u * 0.24 * sz), (c + u * 0.50 * sz, c + u * 0.30 * sz, u * 0.15 * sz)]
        wire = max(8, int(u * (0.13 if small else 0.10)))
        for (x1, y1, r1), (x2, y2, r2) in zip(nodes, nodes[1:]):
            p0, p3 = (x1 + r1 * 0.6, y1), (x2 - r2 * 0.6, y2)
            mx = (p0[0] + p3[0]) / 2
            # One smooth stroke: stamp overlapping discs along the curve (no segment joints to show).
            for (px, py) in bez(p0, (mx, p0[1]), (mx, p3[1]), p3, 400):
                d.ellipse([px - wire / 2, py - wire / 2, px + wire / 2, py + wire / 2], fill=ink)
        for i, (x, y, r) in enumerate(nodes):
            d.ellipse([x - r, y - r, x + r, y + r], fill=ink)
            if i == 1:
                d.ellipse([x - r * 0.46, y - r * 0.46, x + r * 0.46, y + r * 0.46], fill=(46, 92, 255, 255))
    G = G.resize((S, S), Image.LANCZOS)
    glow = G.split()[3].filter(ImageFilter.GaussianBlur(S * 0.02))
    return G, glow


def render(name, small=False):
    cool, warm = PALETTES[name]
    base, R = orb(cool, warm, small)
    G, glow = glyph_layer(name, R, small)
    glow_col = Image.new("RGB", (S, S), tuple(int(v) for v in cool))
    base = Image.composite(glow_col, base, glow.point(lambda a: int(a * 0.55)))
    base.paste(G, (0, 0), G)
    return base


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "icons-out")
    out.mkdir(parents=True, exist_ok=True)
    for name in PALETTES:
        big, small = render(name), render(name, small=True)
        for size in (1024, 512, 192, 180, 96):
            big.resize((size, size), Image.LANCZOS).save(out / f"{name}-{size}.png", optimize=True)
        for size in (48, 32, 16):
            small.resize((size, size), Image.LANCZOS).save(out / f"{name}-{size}.png", optimize=True)
        small.resize((256, 256), Image.LANCZOS).save(out / f"{name}.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        print(name, "ok")


if __name__ == "__main__":
    main()
