#!/usr/bin/env python3
"""
Render the todo list into a 400x300 1-bit bitmap for the RLCD panel.

The Mac does all the layout (incl. Chinese text); the ESP32 only blits bytes.
Outputs:
  out/screen.png  - human preview (400x300)
  out/screen_2x.png
  out/screen.bin  - 15000 bytes, row-major, MSB first, 1 = WHITE(paper)
"""
import json, os, sys, time
from datetime import datetime
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
W, H = 400, 300

FONT_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
]

WEEK = "一二三四五六日"


def font(size):
    for path, idx in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
    return ImageFont.load_default()


def text_w(d, s, f):
    return d.textbbox((0, 0), s, font=f)[2]


def wrap(d, s, f, maxw):
    """Greedy wrap that works for CJK (no spaces) and latin alike."""
    lines, cur = [], ""
    for ch in s:
        if text_w(d, cur + ch, f) <= maxw:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def render(todo, now=None):
    now = now or datetime.now()
    # work at 4x then downscale: gives crisp anti-aliased shapes to threshold
    S = 4
    img = Image.new("L", (W * S, H * S), 255)
    d = ImageDraw.Draw(img)
    M = 8 * S

    f_head = font(19 * S)
    f_item = font(15 * S)
    f_small = font(11 * S)

    y = M - 2 * S
    # ---- header -----------------------------------------------------------
    d.text((M, y), "%d月%d日 周%s" % (now.month, now.day, WEEK[now.weekday()]), font=f_head, fill=0)
    t = now.strftime("%H:%M")
    d.text((W * S - M - d.textbbox((0, 0), t, font=f_head)[2], y + 3 * S), t, font=f_head, fill=0)
    y += 27 * S
    d.line([(M, y), (W * S - M, y)], fill=0, width=S)

    # ---- items ------------------------------------------------------------
    y += 8 * S
    items = todo.get("items", [])
    done = sum(1 for i in items if i.get("done"))
    row_gap = 4 * S
    maxw = W * S - 2 * M - 20 * S
    for it in items:
        lines = wrap(d, it.get("text", ""), f_item, maxw)
        line_h = 19 * S
        block = line_h * len(lines)
        if y + block > H * S - 22 * S:          # footer space
            d.text((M, y), "… +%d 项" % (len(items) - items.index(it)), font=f_small, fill=0)
            break
        # checkbox
        bx, by, bs = M, y + 2 * S, 13 * S
        if it.get("done"):
            d.rectangle([bx, by, bx + bs, by + bs], fill=0)
            d.line([(bx + 3 * S, by + 7 * S), (bx + 6 * S, by + 10 * S)], fill=255, width=2 * S)
            d.line([(bx + 6 * S, by + 10 * S), (bx + 11 * S, by + 3 * S)], fill=255, width=2 * S)
        else:
            d.rectangle([bx, by, bx + bs, by + bs], outline=0, width=S)
        if it.get("pri") == "high" and not it.get("done"):
            d.rectangle([bx + 4 * S, by + 4 * S, bx + 9 * S, by + 9 * S], fill=0)
        # text
        tx = bx + bs + 7 * S
        for i, ln in enumerate(lines):
            d.text((tx, y + i * line_h), ln, font=f_item, fill=0)
            if it.get("done") and i == 0:
                wln = text_w(d, ln, f_item)
                mid = y + line_h // 2
                d.line([(tx, mid), (tx + wln, mid)], fill=0, width=S)
        y += block + row_gap

    # ---- footer -----------------------------------------------------------
    fy = H * S - 18 * S
    d.line([(M, fy - 3 * S), (W * S - M, fy - 3 * S)], fill=0, width=S)
    d.text((M, fy), "%d/%d 完成" % (done, len(items)), font=f_small, fill=0)
    upd = now.strftime("%H:%M") + " 更新"
    d.text((W * S - M - text_w(d, upd, f_small), fy), upd, font=f_small, fill=0)

    img = img.resize((W, H), Image.LANCZOS)
    bw = img.point(lambda v: 0 if v < 140 else 255, mode="1")
    return bw


def pack(bw, path):
    px = bw.load()
    rb = W // 8
    data = bytearray()
    for y in range(H):
        for xb in range(rb):
            b = 0
            for i in range(8):
                if px[xb * 8 + i, y]:
                    b |= 1 << (7 - i)
            data.append(b)
    open(path, "wb").write(bytes(data))
    return len(data)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "todo.json")
    todo = json.load(open(src))
    bw = render(todo)
    os.makedirs(OUT, exist_ok=True)
    bw.save(os.path.join(OUT, "screen.png"))
    bw.convert("L").resize((W * 2, H * 2), Image.NEAREST).save(os.path.join(OUT, "screen_2x.png"))
    n = pack(bw, os.path.join(OUT, "screen.bin"))
    ink = 100.0 * (~np.asarray(bw, dtype=bool)).sum() / (W * H)
    print("rendered %d items -> %s (%d bytes, ink %.1f%%)" % (len(todo.get("items", [])), OUT, n, ink))