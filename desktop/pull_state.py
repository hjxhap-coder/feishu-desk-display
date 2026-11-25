#!/usr/bin/env python3
"""拿一份 state.json 给 Mac 端排版预览用。

**默认走板子自己的输出**，不从飞书拉。

为什么不自己从飞书拉：
  1) 板子已经把自己解析好的 state.json 打到串口上了 —— 那**就是**屏上那份
     数据，Mac 预览直接用它，像素级一致是**结构性保证**，不是靠两边对齐代码。
  2) 飞书的 refresh_token 是**一次性**的：Mac 拿板子那组去刷新 = 把板子的
     凭据打死（谁刷新谁作废对方）。要给 Mac 单独签一组，就是两份各自会过期
     的东西 —— 而 Mac 端只是预览，不值当。
  3) Python 里重写一遍飞书解析 = 两份实现，迟早对不上。
     （共享 C 层 todo_state.c 才是唯一的事实来源。）

用法：
    python3 pull_state.py                  # 从板子日志取最新一块
    python3 pull_state.py --wait 120       # 等一块**新**的（最多 120 秒）
    python3 pull_state.py --log /path.log  # 换日志文件
    python3 pull_state.py --from-lark      # 老路子：自己从飞书拉（需 mac_token.json）
"""
import argparse
import json
import os
import re
import sys
import time

LOG = "/tmp/board_serial.log"
BLOCK_RE = re.compile(
    r"===STATE_JSON_BEGIN===\s*(\{.*?\})\s*===STATE_JSON_END===", re.S)


def last_block(path):
    """取日志里最后一块完整的 state.json。返回 (json_str, 文件大小)。"""
    if not os.path.exists(path):
        raise SystemExit("没有 %s —— 采集器没跑？" % path)
    raw = open(path, "rb").read().decode("utf-8", errors="replace")
    blocks = BLOCK_RE.findall(raw)
    if not blocks:
        raise SystemExit("%s 里还没有 state.json 块" % path)
    return blocks[-1], len(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--out", default="state.json")
    ap.add_argument("--wait", type=int, default=0,
                    help="等一块新的 state.json，最多 N 秒")
    ap.add_argument("--from-lark", action="store_true",
                    help="老路子：自己从飞书拉（需要 mac_token.json）")
    a = ap.parse_args()

    if a.from_lark:
        import pull_state_lark
        pull_state_lark.pull(a.out)
        return

    if a.wait > 0:
        # 记下现在的文件大小，只认**新写入**的块
        try:
            size0 = os.path.getsize(a.log)
        except OSError:
            size0 = 0
        deadline = time.time() + a.wait
        while time.time() < deadline:
            time.sleep(3)
            try:
                if os.path.getsize(a.log) <= size0:
                    continue
            except OSError:
                continue
            try:
                blk, _ = last_block(a.log)
            except SystemExit:
                continue
            if blk:
                break
        else:
            raise SystemExit("%d 秒内没有新的 state.json" % a.wait)

    blk, _ = last_block(a.log)
    # 校验一下是完整 JSON（板子分块打印，理论上可能截断）
    try:
        obj = json.loads(blk)
    except Exception as e:
        raise SystemExit("最后一块 state.json 不是完整 JSON: %s" % e)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    print("→ %s：待办 %d 条（未完成 %d）/ 日程 %d 条  [更新于 %s]" %
          (a.out, len(obj.get("tasks", [])), obj.get("open_tasks", 0),
           len(obj.get("events", [])), obj.get("updated_at", "?")))


if __name__ == "__main__":
    main()
