#!/usr/bin/env python3
"""Draws the app icons (app/icons/icon-*.png) using only the standard library.
Run once; re-run only if you change the design."""
import math
import struct
import zlib
from pathlib import Path

BG = (13, 19, 16)
TREE = (111, 207, 143)
TRUNK = (138, 106, 69)
WAVE = (240, 160, 48)


def inside_tree(x, y):
    # three stacked triangles (an evergreen) on the left-centre of the icon
    for top, bottom, half in ((0.16, 0.42, 0.15), (0.30, 0.58, 0.20), (0.44, 0.74, 0.25)):
        if top <= y <= bottom and abs(x - 0.40) <= half * (y - top) / (bottom - top):
            return TREE
    if 0.74 <= y <= 0.86 and abs(x - 0.40) <= 0.045:
        return TRUNK
    return None


def inside_wave(x, y):
    # sound arcs to the right of the tree: "listening"
    d = math.hypot(x - 0.52, y - 0.48)
    ang = math.degrees(math.atan2(y - 0.48, x - 0.52))
    if abs(ang) > 40:
        return None
    for r in (0.24, 0.33, 0.42):
        if abs(d - r) <= 0.025:
            return WAVE
    return None


def pixel(x, y):
    return inside_tree(x, y) or inside_wave(x, y) or BG


def render(size, ss=3):
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = pixel((px + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size)
                    for i in range(3):
                        acc[i] += c[i]
            row += bytes(v // (ss * ss) for v in acc)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "app" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for s in (180, 192, 512):
        (out / f"icon-{s}.png").write_bytes(render(s))
        print("wrote", out / f"icon-{s}.png")
