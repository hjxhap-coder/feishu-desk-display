#!/usr/bin/env python3
"""板子真机 vs Mac 预览 —— **逐像素**比对每一屏。

为什么要有这个：
    「Mac 预览 = 板子真机」这句话一直是**靠人眼在屏上看**来验收的。
    但板子上有些东西 Mac 复现不了 —— 「现在」指针、待机屏的时钟、
    正在进行的事件高亮，全都跟**渲染那一刻的时刻**有关。
    不同步时刻去比，两边必然不一样，比出来的差异全是假的。

怎么比：
    1. 从串口日志里取出板子导出的帧（每帧带 screen/mode/page/now）
    2. 板子给了 now —— Mac 侧就用**同一个 now** 渲染同一屏
    3. 逐字节比

板子侧的帧格式（在 main.cpp 的 dump_frame()）：
    ===FRAME_BEGIN screen=1 mode=todo page=0 now=1784512345 bytes=15000===
    <15000 字节二进制>
    ===FRAME_END===

用法：
    python3 compare_frames.py                       # 比对 /tmp/board_serial.log 里的
    python3 compare_frames.py --log /path/x.log     # 比对指定的日志
    python3 compare_frames.py --keep /tmp/frames    # 把帧存下来
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 仓库根 = 本文件所在目录的上一级（desktop/ 的上面就是固件工程）
PROJECT = os.path.dirname(HERE)
PREVIEW_BIN = os.path.join(PROJECT, "tools", "_build", "mac_preview")
FONT = os.path.join(PROJECT, "font", "font16.bin")
FRAME_BYTES = 15000
FRAME_W, FRAME_H = 400, 300

HEADER_RE = re.compile(
    rb"===FRAME_BEGIN screen=(\d+) mode=(\w+) page=(\d+) now=(\d+) "
    rb"state=(\d+) bytes=(\d+)===")
STATE_RE = re.compile(rb"===STATE_JSON_BEGIN===\s*(\{.*?\})\s*===STATE_JSON_END===", re.S)


def build_preview():
    """编译 mac_preview（改了 tools/mac_preview.c 要重编）。"""
    import glob
    shared = os.path.join(PROJECT, "components", "shared")
    srcs = [
        os.path.join(PROJECT, "tools", "mac_preview.c"),
        os.path.join(PROJECT, "tools", "mac_font_stub.c"),
        os.path.join(shared, "todo_font.c"),
        os.path.join(shared, "todo_fb.c"),
        os.path.join(shared, "todo_state.c"),
        os.path.join(shared, "todo_layout.c"),
        os.path.join(shared, "cJSON", "cJSON.c"),
    ]
    os.makedirs(os.path.dirname(PREVIEW_BIN), exist_ok=True)
    cmd = ["cc", "-O2", "-Wall", "-std=c99", "-I", shared,
           "-I", os.path.join(shared, "cJSON")] + srcs + ["-o", PREVIEW_BIN, "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        raise SystemExit("编译 mac_preview 失败")
    return PREVIEW_BIN


def extract(log_path):
    """从日志里取出 state.json 和所有帧。"""
    data = open(log_path, "rb").read()

    states = STATE_RE.findall(data)
    state = states[-1] if states else None

    frames = []
    pos = 0
    while True:
        m = HEADER_RE.search(data, pos)
        if not m:
            break
        screen = int(m.group(1))
        mode = m.group(2).decode()
        page = int(m.group(3))
        now = int(m.group(4))
        state_len = int(m.group(5))
        nbytes = int(m.group(6))
        start = m.end()
        # 跳过**头行的行尾**（\r\n 或 \n，1~2 字节）。
        # 【关键】不能「跳过所有连续换行」—— 二进制帧的头几个字节
        # 经常就是 0x0A/0x0D，会被当换行吃掉，整幅图错位。
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif start < len(data) and data[start:start + 1] in (b"\n", b"\r"):
            start += 1

        # 【顺序：帧在前、state 在后】两个长度都在头里 → 按长度取，一个字节都不用猜
        body = data[start:start + nbytes]
        start += nbytes
        inner = None
        if state_len > 0 and start + state_len <= len(data):
            inner = data[start:start + state_len]
            start += state_len          # ← 也要跳过 state，否则下一帧的位置算错
            for marker in (b"===STATE_JSON_BEGIN===", b"===STATE_JSON_END==="):
                inner = inner.replace(marker, b"")
            inner = inner.strip()
        frames.append({
            "screen": screen, "mode": mode, "page": page, "now": now,
            "bytes": body,
            "state": inner,          # 这一帧用的数据
            "complete": len(body) == nbytes,
        })
        pos = start          # start 已经指向「帧 + state」之后
    return state, frames


def render_local(state_path, screen, now, out_bin):
    """用板子给的时刻渲染同一屏。"""
    cmd = [PREVIEW_BIN, state_path, "screen", FONT, out_bin, str(screen), str(now)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    if not os.path.exists(out_bin):
        return None
    return open(out_bin, "rb").read()


def diff_pixels(a, b):
    """返回不一致的像素坐标（前后各取一个字节）。"""
    if len(a) != len(b):
        return None, "长度不同：板子 %d 字节，Mac %d 字节" % (len(a), len(b))
    rb = FRAME_W // 8
    bad = []
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            y, xb = divmod(i, rb)
            for bit in range(8):
                if ((a[i] >> (7 - bit)) & 1) != ((b[i] >> (7 - bit)) & 1):
                    bad.append((xb * 8 + bit, y))
    return bad, None


def to_png(data, path):
    try:
        from PIL import Image
    except ImportError:
        return False
    img = Image.new("L", (FRAME_W, FRAME_H), 255)
    px = img.load()
    rb = FRAME_W // 8
    for y in range(FRAME_H):
        for x in range(FRAME_W):
            byte = data[y * rb + (x >> 3)]
            if not (byte >> (7 - (x & 7))) & 1:
                px[x, y] = 0
    img.save(path)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/tmp/board_serial.log")
    ap.add_argument("--keep", default=None, help="把帧存到这个目录")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.log):
        raise SystemExit("没有 %s —— 采集器在跑吗？" % a.log)

    state, frames = extract(a.log)
    if not frames:
        raise SystemExit("日志里没有帧 —— 板子上按一下 KEY，它才会导出帧")
    # 帧块里自带那一帧用的 state.json（最准）；
    # 日志里独立 dump 的那份只作为兜底。
    if state is None and not any(f.get("state") for f in frames):
        raise SystemExit("日志里没有 state.json —— 没法在 Mac 侧渲染同一屏")

    state_path = "/tmp/_cmp_state.json"
    if state is not None:
        open(state_path, "wb").write(state)
    print("state.json: %d 字节；帧: %d 个\n" % (len(state) if state else 0, len(frames)))

    if not a.no_build:
        build_preview()
    if not os.path.exists(PREVIEW_BIN):
        raise SystemExit("没有 %s，先跑一次 tools/preview.py" % PREVIEW_BIN)

    if a.keep:
        os.makedirs(a.keep, exist_ok=True)

    all_ok = True
    for i, fr in enumerate(frames):
        # 用**这一帧自带**的 state.json（没有才退回日志里独立 dump 的那份）
        st = fr.get("state") or state
        if st is None:
            print("[%d] 没有 state.json，跳过" % i)
            all_ok = False
            continue
        if st is not state:
            open(state_path, "wb").write(st)
        else:
            open(state_path, "wb").write(state)
        tag = "screen%d(%s p%d)" % (fr["screen"], fr["mode"], fr["page"])
        if not fr["complete"]:
            print("[%d] %-22s ✗ 帧不完整（串口可能漏字节）" % (i, tag))
            all_ok = False
            continue
        local = render_local(state_path, fr["screen"], fr["now"], "/tmp/_cmp_local.bin")
        if local is None:
            print("[%d] %-22s ✗ Mac 侧渲染失败" % (i, tag))
            all_ok = False
            continue
        bad, err = diff_pixels(fr["bytes"], local)
        if err:
            print("[%d] %-22s ✗ %s" % (i, tag, err))
            all_ok = False
            continue
        if not bad:
            print("[%d] %-22s ✓ 逐像素一致（%d 字节）" % (i, tag, FRAME_BYTES))
        else:
            xs = [p[0] for p in bad]
            ys = [p[1] for p in bad]
            print("[%d] %-22s ✗ %d 个像素不同  x=%d..%d y=%d..%d"
                  % (i, tag, len(bad), min(xs), max(xs), min(ys), max(ys)))
            all_ok = False
        if a.keep:
            open(os.path.join(a.keep, "board_%d.bin" % i), "wb").write(fr["bytes"])
            open(os.path.join(a.keep, "mac_%d.bin" % i), "wb").write(local)
            to_png(fr["bytes"], os.path.join(a.keep, "board_%d.png" % i))
            to_png(local, os.path.join(a.keep, "mac_%d.png" % i))

    print("\n%s" % ("全部一致 ✅" if all_ok else "有差异 ❌"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
