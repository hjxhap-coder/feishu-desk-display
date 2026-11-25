#!/usr/bin/env python3
"""
从《快雪时晴帖》扫描图里取出「快雪」两字，转成 1-bit 素材。

为什么要特殊处理：
  1. 帖是黄褐色的绢本，直接全局阈值会把纸的老化斑和纤维全变成黑点。
  2. 黑墨在 RGB 上 R≈G≈B，黄纸的 R 明显高于 B —— 所以用 **R-B** 当"墨浓度"，
     墨迹(黑) 浓度低、纸(黄) 浓度高，分离得干干净净。
  3. 1-bit 化会打散飞白，所以先按目标尺寸缩放，再二值化，笔画才不断。

输出:
    assets/kuaixue_ink.png     1-bit，1=纸白 0=墨黑
    assets/kuaixue_preview.png 放大预览
"""
import os
import sys
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
os.makedirs(ASSETS, exist_ok=True)

SRC = "/tmp/kx2.jpg"

# 「快雪」两字在右列的粗略位置（1080x1296 原图坐标）
CROP = (630, 495, 805, 735)

# R-B 阈值：< TH 判为墨。实测这张绢本 38 最合适。
TH = 44


def ink_mask(img):
    """返回墨浓度图：0 = 墨（黑），255 = 纸（白）。用 R-B 分离黄纸上的黑墨。

    实测这张绢本：R-B 在 -20..20 是墨，60 以上是纸，20..60 是笔画边缘。
    阈值取 38 能把底噪切干净又不吃掉笔画。

    注意方向：R-B 越小越像墨，所以**不要**再 invert —— invert 会把纸判成墨。
    """
    from PIL import ImageChops
    r, _, b = img.split()
    diff = ImageChops.subtract(r, b)      # 黄纸大，黑墨小
    return diff


def despeckle(bw, min_area=6):
    """去掉孤立的墨点（面积小于 min_area 的连通域）。

    阈值再怎么调都留得下几个 JPEG 噪点，孤立噪点在 1-bit 屏上特别扎眼。
    用连通域面积过滤最干净 —— 形态学开运算会把飞白也一起吃掉。
    """
    import numpy as np
    from collections import deque
    # 注意：PIL 的 mode "1" 转 numpy 时白色是 True、黑色是 False。
    # 直接 == 0 会把白纸当成墨，整张图反色。先转 L 再按亮度判黑才稳。
    a = (np.asarray(bw.convert("L")) < 128).astype(np.uint8)   # 1 = 墨
    h, w = a.shape
    seen = np.zeros_like(a)
    out = a.copy()
    for y in range(h):
        for x in range(w):
            if not a[y, x] or seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = 1
            comp = []
            while q:
                cy, cx = q.popleft()
                comp.append((cy, cx))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and a[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = 1
                            q.append((ny, nx))
            if len(comp) < min_area:
                for cy, cx in comp:
                    out[cy, cx] = 0
    # out 里 1=墨 0=纸，而目标格式是 1=纸白 0=墨黑 —— 要取反
    return Image.fromarray((1 - out) * 255, mode="L").convert("1")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else SRC
    if not os.path.exists(src):
        print("找不到源图: %s" % src)
        sys.exit(1)

    im = Image.open(src).convert("RGB")
    crop = im.crop(CROP)

    # 目标输出高度（1-bit 屏上左栏）
    TARGET_H = 180
    scale = TARGET_H / crop.height
    crop = crop.resize((max(1, int(crop.width * scale)), TARGET_H), Image.LANCZOS)

    # 墨浓度 -> 二值
    mask = ink_mask(crop)
    bw = mask.point(lambda v: 0 if v < TH else 255, mode="1")   # 0=墨黑 255=纸白
    bw = despeckle(bw)

    out = os.path.join(ASSETS, "kuaixue_ink.png")
    bw.save(out)

    # 预览：放到白底上放大看
    prev = bw.convert("L").resize((bw.width * 2, bw.height * 2), Image.NEAREST)
    prev.save(os.path.join(ASSETS, "kuaixue_preview.png"))

    # 统计一下二值化后的墨占比，太高说明底噪进来了
    hist = bw.convert("L").histogram()
    ink = hist[0]
    total = bw.width * bw.height
    print("裁剪尺寸 : %dx%d -> %s" % (CROP[2]-CROP[0], CROP[3]-CROP[1],
                                       (bw.width, bw.height)))
    print("墨占比   : %.1f%%  (正常 15~35%%)" % (100.0 * ink / total))
    print("输出     : %s" % out)


if __name__ == "__main__":
    main()
