#!/usr/bin/env python3
"""
验证：宋体黑（衬线体）在 1-bit 化之后笔画会不会被打散。

背景：设计约束写着「任何 Light 字重、任何衬线体在小字号下都会碎」。
v3 的页眉大字用了 Songti SC Black 23px —— 大字不是小字号，
但「衬线体会碎」这条得拿数据验证，不能靠嘴硬。

判据：
  1. 同一段文字分别用宋体黑 / 黑体 W6 渲染
  2. 4 倍画布 -> 缩到 1x -> 二值化（和真实流程一致）
  3. 统计每一行的「连续黑段数」
     笔画被打散 = 同一根横被切成好几段 = 黑段数暴涨
  4. 同时数孤立像素（<3px 的黑段），那是打碎的残渣
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H, S = 400, 300, 4
BLACK, WHITE = 0, 255

FACES = {
    "宋体黑 Songti SC Black": ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    "黑体 Hiragino W6":      ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
    "中黑 STHeiti Medium":   ("/System/Library/Fonts/STHeiti Medium.ttc", 1),
}


def render(text, path, idx, size):
    f = ImageFont.truetype(path, size * S, index=idx)
    img = Image.new("L", (W * S, H * S), WHITE)
    d = ImageDraw.Draw(img)
    d.text((20 * S, 40 * S), text, font=f, fill=BLACK)
    small = img.resize((W, H), Image.LANCZOS)
    return small.point(lambda v: 0 if v < 140 else 255, mode="1")


def segments(bw):
    """逐行数连续黑段，返回 (每行段数列表, 孤立段数)。"""
    px = bw.load()
    per_row = []
    lonely = 0
    for y in range(bw.height):
        segs = 0
        run = 0
        for x in range(bw.width):
            if not px[x, y]:          # 黑
                run += 1
            else:
                if run:
                    segs += 1
                    if run < 3:
                        lonely += 1
                    run = 0
        if run:
            segs += 1
            if run < 3:
                lonely += 1
        per_row.append(segs)
    return per_row, lonely


def gray_segments(path, idx, size):
    """同样的字，但不做二值化（用灰度判断），作为"理想"基准。"""
    f = ImageFont.truetype(path, size * S, index=idx)
    img = Image.new("L", (W * S, H * S), WHITE)
    d = ImageDraw.Draw(img)
    d.text((20 * S, 40 * S), "日程", font=f, fill=BLACK)
    small = img.resize((W, H), Image.LANCZOS)
    px = small.load()
    per_row = []
    for y in range(small.height):
        segs = 0
        run = 0
        for x in range(small.width):
            if px[x, y] < 140:
                run += 1
            else:
                if run:
                    segs += 1
                    run = 0
        if run:
            segs += 1
        per_row.append(segs)
    return per_row


TEXT = "日程"
sheets = []
print("字号 23px，文字「%s」" % TEXT)
print("%-24s %10s %10s %10s" % ("字体", "灰度段数", "1bit段数", "孤立段"))
print("-" * 60)
for name, (path, idx) in FACES.items():
    g = gray_segments(path, idx, 23)
    bw = render(TEXT, path, idx, 23)
    per, lonely = segments(bw)
    gs, bs = sum(g), sum(per)
    print("%-24s %10d %10d %10d" % (name, gs, bs, lonely))
    # 放大图存下来肉眼看
    bw.convert("L").resize((W * 2, H * 2), Image.NEAREST).save(
        "/tmp/font_%s.png" % name.split()[0])
    sheets.append((name, bw))

# 并排图
sheet = Image.new("L", (W * 2 * len(sheets) + 20 * (len(sheets) - 1), H * 2), 255)
for i, (_, bw) in enumerate(sheets):
    sheet.paste(bw.convert("L").resize((W * 2, H * 2), Image.NEAREST),
                (i * (W * 2 + 20), 0))
sheet.save("/tmp/font_compare.png")
print()
print("并排图: /tmp/font_compare.png")
