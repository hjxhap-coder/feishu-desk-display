#!/usr/bin/env python3
"""
读 TOF1 字库，用**同一份数据**渲染成 PNG。

这是字库的数据自检：字形对不对、有没有偏移错位、advance 对不对，
直接看图就知道。板子上的 C 代码读的是同一个 .bin，所以这里对 = 板子上对。

    python3 preview_font.py font16.bin "待办 14:00" -o /tmp/font_test.png
"""
import argparse
import struct
import sys

from PIL import Image


class TofFont:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = f.read()
        if self.data[:4] != b"TOF1":
            raise ValueError("不是 TOF1 文件")
        (ver, cell_h, cell_w, ascent, n) = struct.unpack_from("<BBBBI", self.data, 4)
        (self.index_off, self.index_len, self.data_off) = struct.unpack_from("<III", self.data, 12)
        self.version = ver
        self.cell_h = cell_h
        self.cell_w = cell_w
        self.ascent = ascent
        self.n = n
        self.rowbytes = cell_w // 8
        self.index = {}
        for i in range(n):
            cp, off, adv, flags, _ = struct.unpack_from("<IIBBH", self.data,
                                                        self.index_off + i * 12)
            self.index[cp] = (off, adv, flags)

    def glyph(self, cp):
        e = self.index.get(cp)
        if e is None:
            return None
        return self.data[e[0]:e[0] + self.rowbytes * self.cell_h]

    def advance(self, cp):
        e = self.index.get(cp)
        return e[1] if e else self.cell_w

    def has(self, cp):
        return cp in self.index

    def draw_text(self, img, x, y, text, ink=0):
        """把 text 画到 img (PIL, mode 'L')，返回结束时的 x。"""
        px = img.load()
        for ch in text:
            cp = ord(ch)
            g = self.glyph(cp)
            if g is None:
                x += self.cell_w          # 缺字：空一个格子
                continue
            for r in range(self.cell_h):
                base = r * self.rowbytes
                for c in range(self.cell_w):
                    bit = (g[base + (c >> 3)] >> (7 - (c & 7))) & 1
                    if bit:
                        xx, yy = x + c, y + r
                        if 0 <= xx < img.width and 0 <= yy < img.height:
                            px[xx, yy] = ink
            x += self.advance(cp)
        return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("text", nargs="?", default="待办日程 14:00 与同事 1:1")
    ap.add_argument("-o", "--out", default="/tmp/font_preview.png")
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--bg", type=int, default=255)
    ap.add_argument("--fg", type=int, default=0)
    a = ap.parse_args()

    f = TofFont(a.bin)
    print("字库: %d 字形, 单元格 %dx%d, 基线行 %d" %
          (f.n, f.cell_w, f.cell_h, f.ascent))

    W = 400
    lines = [a.text, "整理本周工作周报", "会议室 3A · 线上", "ABC abc 0123456789"]
    H = f.cell_h * len(lines) + 8
    img = Image.new("L", (W, H), a.bg)
    y = 4
    for ln in lines:
        f.draw_text(img, 4, y, ln, a.fg)
        y += f.cell_h

    img = img.resize((W * a.scale, H * a.scale), Image.NEAREST)
    img.save(a.out)
    print("写出 %s  (%dx%d)" % (a.out, img.width, img.height))

    miss = [c for c in a.text if not f.has(ord(c))]
    if miss:
        print("⚠ 缺字: %s" % "".join(miss))


if __name__ == "__main__":
    main()
