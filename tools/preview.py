#!/usr/bin/env python3
"""
Mac 端预览：用**和板子同一份 C 代码**渲染三屏，出 PNG。

    python3 tools/preview.py                # 用 desktop/state.example.json
    python3 tools/preview.py --state xxx.json --out /tmp/preview

为什么要这么绕：直接用 PIL 画一套预览，迟早和板子上的实现对不上。
这里是把 shared/ 里的 .c 编译成本机程序跑一遍，预览即真机。
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHARED = os.path.join(ROOT, "components", "shared")
FONT = os.path.join(ROOT, "font", "font16.bin")
BUILD = os.path.join(HERE, "_build")

MODES = ("standby", "todo", "schedule")
# 板子上单键前进的整条页面序列（Mac 端用同一份 C 代码算，不自己数）
SCREEN_MODES = ("screen",)

CFLAGS = [
    "-O2", "-Wall", "-std=c99",
    "-I", SHARED,
    "-I", os.path.join(SHARED, "cJSON"),
]


def build():
    os.makedirs(BUILD, exist_ok=True)
    exe = os.path.join(BUILD, "mac_preview")
    srcs = [
        os.path.join(HERE, "mac_preview.c"),
        os.path.join(HERE, "mac_font_stub.c"),
        os.path.join(SHARED, "todo_font.c"),
        os.path.join(SHARED, "todo_fb.c"),
        os.path.join(SHARED, "todo_state.c"),
        os.path.join(SHARED, "todo_layout.c"),
        os.path.join(SHARED, "cJSON", "cJSON.c"),
    ]
    cmd = ["cc"] + CFLAGS + srcs + ["-o", exe, "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise SystemExit("编译失败")
    return exe


def bin_to_png(data, path):
    from PIL import Image
    W, H = 400, 300
    img = Image.new("L", (W, H), 255)
    px = img.load()
    for y in range(H):
        base = y * (W // 8)
        for x in range(W):
            bit = (data[base + (x >> 3)] >> (7 - (x & 7))) & 1
            px[x, y] = 255 if bit else 0
    img.convert("1").save(path)
    img.resize((W * 2, H * 2), Image.NEAREST).save(path.replace(".png", "_2x.png"))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state",
                    default=os.path.join(ROOT, "desktop", "state.example.json"),
                    help="state.json 路径（默认用仓库内 desktop/state.example.json）")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "preview"))
    ap.add_argument("--build-only", action="store_true", help="只编译，不渲染")
    ap.add_argument("--bin-only", action="store_true", help="只出 .bin，不出 PNG")
    ap.add_argument("--page", type=int, default=0, help="渲染第几页（0 起步）")
    ap.add_argument("--all", action="store_true", help="每一页都渲染出来")
    a = ap.parse_args()

    if not os.path.exists(FONT):
        raise SystemExit("缺字库: %s（先跑 tools/make_font_bin.py）" % FONT)

    exe = build()
    if a.build_only:
        print("已编译: %s" % exe)
        return
    if not os.path.exists(a.state):
        raise SystemExit("缺 state.json: %s" % a.state)

    os.makedirs(a.out, exist_ok=True)

    def render(m, p, suffix):
        out_bin = os.path.join(a.out, m + suffix + ".bin")
        r = subprocess.run([exe, a.state, m, FONT, out_bin, str(p)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr)
            raise SystemExit("渲染 %s 失败" % m)
        with open(out_bin, "rb") as f:
            data = f.read()
        if len(data) != 15000:
            raise SystemExit("%s 帧长度 %d，不是 15000" % (m, len(data)))
        if not a.bin_only:
            bin_to_png(data, os.path.join(a.out, m + suffix + ".png"))
        print("  %-9s 15000 B -> %s" % (m, out_bin))

    # 整条页面序列：待机 -> 待办各页 -> 日程各页
    r = subprocess.run([exe, a.state, "screen", FONT, os.devnull, "0"],
                       capture_output=True, text=True)
    total = 1
    for tok in r.stderr.split():
        if tok.startswith("总序列"):
            total = int(tok.replace("总序列", "").replace("格", ""))
            break

    if a.all:
        for i in range(total):
            render("screen", i, "" if total <= 1 else "_p%d" % i)
    else:
        for m in MODES:
            render(m, a.page, "")

    if not a.all:
        print("（页面序列共 %d 格，加 --all 全部渲染出来）" % total)

    print("预览在: %s" % a.out)


if __name__ == "__main__":
    main()
