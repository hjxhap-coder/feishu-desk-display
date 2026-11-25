#!/usr/bin/env python3
"""
BDF 点阵字体 -> 我们的 TOF1 二进制格式 + 可编译的 C 数组。

为什么要自己定义格式：
    板子（ESP32）和 Mac（预览）必须用**同一份字形数据**，
    否则预览和真机会长歪。自己定义一个简单格式，两边共用同一个文件，
    就没有"两份数据不一致"这种问题。

TOF1 格式（小端）:
    header (16 bytes)
        magic[4]   = "TOF1"
        u8  version = 1
        u8  cell_h  = 18          每个字形的固定单元格高
        u8  cell_w  = 16          每个字形的固定单元格宽
        u8  ascent  = 14          基线在单元格里的行号
        u16 n_glyph
        u32 index_off
        u32 index_len
        u32 data_off
    index entry (12 bytes, 按 codepoint 升序)
        u32 codepoint
        u32 offset                字形位图在文件里的绝对偏移
        u8  advance               前进宽度（像素）
        u8  flags                 bit0 = 窄字形
        u16 reserved
    glyph bitmap (cell_w*cell_h/8 = 36 bytes)
        行优先，每行 16 bit = 2 字节，1 = 有墨

字形是"烤进"固定 16x18 单元格的：每个字形按自己的 advance 宽度左对齐，
超出宽度的部分裁掉。中文 16 宽刚好占满，ASCII 靠 advance 留出字距。
"""
import argparse
import os
import struct
import sys

CELL_W = 16
CELL_H = 18
ASCENT = 14
ROWBYTES = CELL_W // 8          # 2 bytes per row

MAGIC = b"TOF1"


def parse_bdf(path):
    """解析 BDF，返回 {codepoint: (bitmap_rows, advance)}。"""
    glyphs = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        in_char = False
        cp = None
        bbx = None
        dwidth = CELL_W
        rows = []
        in_bitmap = False

        for line in f:
            line = line.rstrip("\n")
            if line.startswith("STARTCHAR"):
                in_char = True
                cp = None
                bbx = None
                dwidth = CELL_W
                rows = []
                in_bitmap = False
                continue

            if not in_char:
                continue

            if line.startswith("ENCODING"):
                try:
                    cp = int(line.split()[1])
                except (IndexError, ValueError):
                    cp = None
            elif line.startswith("BBX"):
                p = line.split()
                bbx = (int(p[1]), int(p[2]), int(p[3]), int(p[4]))   # w h xoff yoff
            elif line.startswith("DWIDTH"):
                try:
                    dwidth = int(line.split()[1])
                except (IndexError, ValueError):
                    dwidth = CELL_W
            elif line.startswith("BITMAP"):
                in_bitmap = True
            elif line.startswith("ENDCHAR"):
                in_bitmap = False
                in_char = False
                if cp is not None and cp >= 0 and bbx is not None:
                    glyphs[cp] = (rows, bbx, dwidth)
            elif in_bitmap and line.strip():
                # 每行是 hex，位图左边对齐
                hexrow = line.strip()
                try:
                    val = int(hexrow, 16)
                except ValueError:
                    continue
                nbytes = (bbx[0] + 7) // 8
                rows.append((val, nbytes))
    return glyphs


def build_cell(rows, bbx, dwidth):
    """把一个 BDF 字形烤进 16x18 单元格，返回 (36 字节, advance)。

    位图搬运用整数推算，别用逐字节 |= —— 那样多字节行会互相污染。

    BDF 的位图是一串位，最左有效位在最高位。设 src_int 把 nbytes*8 位
    当成整数（最高位=最左），有效宽度 w 位占据高位。
    目标 16 位行：有效位要落在 [xoff, xoff+w)。
        t_int 的 bit(15-xoff-i) = src_int 的 bit(nbytes*8-1-i)
    => t_int = src_int >> (nbytes*8 - 16 + xoff)
    """
    w, h, xoff, yoff = bbx
    cell = bytearray(ROWBYTES * CELL_H)

    # 垂直位置：位图底边在基线下方 |yoff| 处，位图顶边在基线上方 (yoff + h) 处。
    # 单元格第 0 行对应基线上方 ASCENT 行，所以：
    #     y_paste = ASCENT - (yoff + h)
    # CJK: yoff=-2, h=16 -> 0（顶到格子顶）
    # 'A': yoff=0,  h=11 -> 3（坐在基线上）
    # '-': yoff=3,  h=1  -> 10（基线下 3 像素）
    y_paste = ASCENT - (yoff + h)
    if y_paste < 0:
        y_paste = 0

    for r, (val, nbytes) in enumerate(rows):
        rr = r + y_paste
        if rr >= CELL_H:
            break
        src_int = val & ((1 << (nbytes * 8)) - 1)
        shift = nbytes * 8 - CELL_W + xoff
        if shift >= 0:
            row = (src_int >> shift) & 0xFFFF
        else:
            row = (src_int << (-shift)) & 0xFFFF
        base = rr * ROWBYTES
        cell[base]     = (row >> 8) & 0xFF
        cell[base + 1] = row & 0xFF

    adv = dwidth if dwidth > 0 else CELL_W
    if adv > CELL_W:
        adv = CELL_W
    return bytes(cell), adv


def build(glyphs, keep_ranges):
    """按码位范围筛选字形，返回 (二进制, 统计)。"""
    selected = {}
    for cp, (rows, bbx, dwidth) in glyphs.items():
        if not any(lo <= cp <= hi for lo, hi in keep_ranges):
            continue
        cell, adv = build_cell(rows, bbx, dwidth)
        selected[cp] = (cell, adv)
    return selected


def pack(selected):
    """打包成 TOF1 二进制。"""
    index = []
    blob = bytearray()

    for cp in sorted(selected):
        cell, adv = selected[cp]
        index.append((cp, len(blob), adv))
        blob += cell

    n = len(index)
    header_len = 24
    index_off = header_len
    index_len = n * 12
    data_off = index_off + index_len

    out = bytearray()
    out += MAGIC
    out += struct.pack("<BBBBI", 1, CELL_H, CELL_W, ASCENT, n)
    out += struct.pack("<III", index_off, index_len, data_off)
    assert len(out) == header_len, len(out)

    for cp, off, adv in index:
        out += struct.pack("<IIBBH", cp, data_off + off, adv, 0, 0)
    assert len(out) == data_off

    out += blob
    return bytes(out)


def emit_c(bin_data, out_c, symbol="TODO_FONT_BIN"):
    """生成 C 数组文件。

    用 TODO_FONT_EMBED 宏包起来：画面由 Mac 渲染时（设计迭代阶段）
    不需要把字库编进固件，省 1MB flash、烧录快 3 倍。
    """
    lines = []
    lines.append("/* 自动生成，勿手改 —— 用 tools/make_font_bin.py 重新生成 */")
    lines.append('#include "todo_font.h"')
    lines.append("")
    lines.append("#ifndef TODO_FONT_EMBED")
    lines.append("#define TODO_FONT_EMBED 1")
    lines.append("#endif")
    lines.append("")
    lines.append("#if TODO_FONT_EMBED")
    lines.append("const unsigned char %s[] = {" % symbol)
    body = []
    for i in range(0, len(bin_data), 16):
        chunk = bin_data[i:i + 16]
        body.append("    " + "".join("0x%02x," % b for b in chunk))
    lines.append("\n".join(body))
    lines.append("};")
    lines.append("")
    lines.append("const unsigned int %s_LEN = %d;" % (symbol, len(bin_data)))
    lines.append("")
    lines.append("#else")
    lines.append("const unsigned char %s[] = { 0 };" % symbol)
    lines.append("const unsigned int  %s_LEN = 0;" % symbol)
    lines.append("#endif")
    lines.append("")
    with open(out_c, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


DEFAULT_RANGES = [
    (0x0020, 0x007E),    # ASCII
    (0x00A0, 0x00FF),    # Latin-1
    (0x2010, 0x201F),    # 常用标点 ———
    (0x2026, 0x2026),    # …
    (0x2030, 0x203B),    # ‰ ‹ › ※
    (0x2100, 0x2109),    # ℃ ℉
    (0x2190, 0x2193),    # ↑↓→
    (0x25A0, 0x25CF),    # ■ □ ● ○ 等几何符号
    (0x3000, 0x303F),    # CJK 标点 。，、！？（）
    (0x4E00, 0x9FFF),    # CJK 统一表意文字（全部 20992）
    (0xFF00, 0xFFEF),    # 全角字符
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bdf")
    ap.add_argument("--bin", default=None, help="输出 TOF1 二进制")
    ap.add_argument("--c", default=None, help="输出 C 数组")
    ap.add_argument("--stats-only", action="store_true")
    a = ap.parse_args()

    print("解析 %s ..." % a.bdf)
    glyphs = parse_bdf(a.bdf)
    print("  BDF 里共 %d 个字形" % len(glyphs))

    selected = build(glyphs, DEFAULT_RANGES)
    print("  按范围筛选后剩 %d 个" % len(selected))

    data = pack(selected)
    print("  TOF1 二进制 %d 字节 (%.0f KB)" % (len(data), len(data) / 1024.0))

    # 覆盖率自检：常用汉字是否都在
    probe = "待办日程一二三四五六七八九十零年月日时分秒星期今天明天"
    missing = [c for c in probe if ord(c) not in selected]
    if missing:
        print("  ✗ 缺字: %s" % "".join(missing))
        return 1
    print("  ✓ 自检通过：常用字齐全")

    if a.stats_only:
        return 0

    if a.bin:
        with open(a.bin, "wb") as f:
            f.write(data)
        print("  写出 %s" % a.bin)
    if a.c:
        emit_c(data, a.c)
        print("  写出 %s" % a.c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
