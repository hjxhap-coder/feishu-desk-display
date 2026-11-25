#!/usr/bin/env python3
"""检查真实数据里的每个码位在 16px 点阵字库里有没有字形。

为什么要有这个：
    飞书里的任务标题带着 emoji（"➡️" = U+27A1 U+FE0F）。
    字库是文泉驿点阵宋体，**只有 BMP 里的常用字**，变体选择符 U+FE0F
    肯定没有。缺字形时代码按 16px 步进画一个**空白格** ——
    屏上就多出一段莫名其妙的空隙，但程序不报错。

另外还核对 todo_font.c 里 cp_substitute() 的替换目标
**在字库里真的存在** —— 写错一个码位就是又一个空白格。

用法：
    python3 check_glyphs.py /tmp/board_state.json
    python3 check_glyphs.py --state-from-log /tmp/board_serial.log
"""
import argparse
import json
import os
import re
import struct
import sys
import unicodedata

# 固件工程的位置。合并成一个仓库后，字库和 C 源码就在**本仓库**里 ——
# 本文件在 desktop/ 下，往上一级就是固件工程。
# 也可用环境变量 FIRMWARE_DIR 覆盖。
FW_DIR = os.environ.get(
    "FIRMWARE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
FONT = os.path.join(FW_DIR, "font", "font16.bin")
FONT_C = os.path.join(FW_DIR, "components", "shared", "todo_font.c")


def load_font(path):
    d = open(path, "rb").read()
    assert d[:4] == b"TOF1", "不是 TOF1 字体：%r" % d[:4]
    cell_h = d[5]
    cell_w = d[6]
    n_glyph = d[8] | (d[9] << 8)
    index_off = struct.unpack_from("<I", d, 12)[0]
    glyphs = {}
    for i in range(n_glyph):
        off = index_off + i * 12
        cp = struct.unpack_from("<I", d, off)[0]
        glyphs[cp] = d[off + 8]
    return {"cell_w": cell_w, "cell_h": cell_h, "glyphs": glyphs}


def utf8_cps(s):
    out = []
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            continue
        out.append(cp)
    return out


def collect_texts(state):
    texts = []
    for t in state.get("tasks", []):
        texts.append(("task", t.get("text", "")))
    for e in state.get("events", []):
        texts.append(("event", e.get("title", "")))
        if e.get("location"):
            texts.append(("loc", e["location"]))
    return texts


def check_subst_table(f):
    """核对 cp_substitute() 的每个替换目标在字库里真的存在。"""
    src = open(FONT_C, encoding="utf-8").read()
    m = re.search(r"static uint32_t cp_substitute\(uint32_t cp\)\s*\{(.*?)\n\}", src, re.S)
    if not m:
        print("✗ 在 todo_font.c 里找不到 cp_substitute()")
        return False
    body = m.group(1)
    targets = sorted(set(int(x, 16) for x in re.findall(r"return 0x([0-9A-Fa-f]{4,6});", body)))
    srcs = sorted(set(int(x, 16) for x in re.findall(r"case 0x([0-9A-Fa-f]{4,6}):", body)))

    print("\n替代字形表：%d 个源码位 -> %d 个目标" % (len(srcs), len(targets)))
    bad = []
    for t in targets:
        if t in f["glyphs"]:
            print("  -> U+%04X 存在  adv=%d" % (t, f["glyphs"][t]))
        else:
            print("  -> U+%04X 缺失！" % t)
            bad.append(t)
    if bad:
        print("\n✗ %d 个目标码位在字库里不存在 —— 替换后会变成空白格" % len(bad))
        return False
    print("✓ 所有替换目标都在字库里")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("state", nargs="?")
    ap.add_argument("--state-from-log")
    a = ap.parse_args()

    if a.state_from_log:
        s = open(a.state_from_log, encoding="utf-8", errors="replace").read()
        blocks = re.findall(r"===STATE_JSON_BEGIN===\n(.*?)\n===STATE_JSON_END===", s, re.S)
        if not blocks:
            sys.exit("日志里没有 state.json 块")
        raw = blocks[-1]
    elif a.state:
        raw = open(a.state, encoding="utf-8").read()
    else:
        raw = sys.stdin.read()

    state = json.loads(raw)
    f = load_font(FONT)
    print("字库: %d 字形, cell %dx%d" % (len(f["glyphs"]), f["cell_w"], f["cell_h"]))

    ok = check_subst_table(f)
    glyphs = f["glyphs"]

    missing = {}
    for kind, text in collect_texts(state):
        for cp in utf8_cps(text):
            if cp not in glyphs:
                missing.setdefault(cp, []).append((kind, text))

    if not missing:
        print("\n数据里所有码位都有字形")
    else:
        print("\n缺 %d 个码位：" % len(missing))
        for cp, where in sorted(missing.items()):
            try:
                name = unicodedata.name(chr(cp))
            except ValueError:
                name = "<无名>"
            print("  U+%04X  %-28s 宽%dpx  出现 %d 次" %
                  (cp, name[:28], f["cell_w"], len(where)))
            print("      例：%s" % where[0][1][:40])

        vs = [cp for cp in missing if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF]
        if vs:
            print("\n其中 %d 个是变体选择符（VS16/VS15…）—— 这些是零宽字符，" % len(vs))
            print("   字体层已处理成 advance=0，不会再留 16px 空隙。")
            missing = {k: v for k, v in missing.items() if k not in vs}
        if missing:
            print("\n（其余 %d 个仍无字形，会显示成空白格 —— "
                  "除非加进替代表或字库，否则就是豆腐块）" % len(missing))

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
