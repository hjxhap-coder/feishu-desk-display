#!/usr/bin/env python3
"""
造一份假的 state.json，飞书权限还没批下来时用它把原型跑通。

数据形状和 lark_pull.py 产出的完全一致，所以之后切换数据源不用改任何下游代码。

用法:
    python3 mock_state.py          # 写 state.json
    python3 mock_state.py --now    # 顺便打印出来
"""
import json
import os
import random
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")

TASKS = [
    ("整理本周工作周报", "high", "今天"),
    ("代码评审：固件网络自愈模块", "high", "今天"),
    ("结算明细按客户任务号口径重算", "mid", "明天"),
    ("回复客户：交付清单与验收口径确认", "mid", ""),
    ("质检通过率差距说明", "mid", "01-24"),
    ("整理 Prompt 可用性待复核清单", "low", ""),
    ("备份项目资产索引", "low", ""),
    ("更新 wiki 闭环记录", "low", ""),
]

EVENTS = [
    (9 * 60, 30, "站会", ""),
    (10 * 60, 60, "产品评审", "会议室 A"),
    (14 * 60, 30, "与同事 1:1", "线上"),
    (16 * 60, 45, "口径对齐", "会议室 B"),
]


def build(now=None):
    now = now or datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tasks = []
    for text, pri, due in TASKS:
        tasks.append({
            "text": text,
            "done": pri == "low" and random.random() < 0.5,
            "pri": pri if pri != "mid" else "",
            "due": due,
        })

    events = []
    now_ts = now.timestamp()
    for h, m, title, loc in EVENTS:
        st = day + timedelta(minutes=h)
        en = day + timedelta(minutes=h + m)
        events.append({
            "title": title,
            "start": int(st.timestamp()),
            "end": int(en.timestamp()),
            "start_ts": int(st.timestamp()),
            "end_ts": int(en.timestamp()),
            "start_hm": st.strftime("%H:%M"),
            "end_hm": en.strftime("%H:%M"),
            "location": loc,
            "now": st.timestamp() <= now_ts <= en.timestamp(),
            "next": False,
        })
    # 下一个：还没开始的第一条
    if not any(e["now"] for e in events):
        for e in events:
            if e["start_ts"] > now_ts:
                e["next"] = True
                break
    nxt = next((e for e in events if e["now"]), None) or \
        next((e for e in events if e["next"]), None)

    return {
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "user": "demo",
        "tasks": tasks,
        "events": events,
        "next_event": nxt,
        "open_tasks": sum(1 for t in tasks if not t["done"]),
        "total_tasks": len(tasks),
    }


def main():
    # --only-missing: 只补真正没拉到的那一路，已经是真数据的部分原样保留。
    #
    # 这个模式是必须的：飞书只要缺一个 scope（比如 task:task:read 没批），
    # 日程那边往往是有真数据的。如果这时候整体覆盖 state.json，
    # 真实日程就被假数据冲掉了 —— 而且屏上看不出来。
    only_missing = "--only-missing" in sys.argv

    st = build()
    st["ok_tasks"] = True
    st["ok_events"] = True
    st["errors"] = {}
    st["mock"] = {"tasks": True, "events": True}

    if only_missing and os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = None
        if cur:
            # cur 里的 ok_tasks / ok_events 说明当时哪一路拉到了
            if cur.get("ok_tasks", False):
                st["tasks"] = cur.get("tasks", [])
                st["open_tasks"] = cur.get("open_tasks", 0)
                st["total_tasks"] = cur.get("total_tasks", 0)
                st["mock"]["tasks"] = False
            if cur.get("ok_events", False):
                st["events"] = cur.get("events", [])
                st["next_event"] = cur.get("next_event")
                st["mock"]["events"] = False
            # 错误信息留着，屏上要显示"这一路是 mock 顶的"
            st["errors"] = cur.get("errors", {})

    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)
    print("mock state -> %s  (%d tasks, %d events)  mock=%s" %
          (STATE, st["total_tasks"], len(st["events"]), st["mock"]))


if __name__ == "__main__":
    main()
