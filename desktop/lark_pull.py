#!/usr/bin/env python3
"""
把飞书的待办和日程拉到 state.json，板子只管显示。

数据流:
    lark-cli  --as user  -->  state.json  -->  渲染器  -->  1-bit 帧  -->  ESP32

用法:
    lark-cli 拉一次:            python3 lark_pull.py
    持续拉(默认 120s 一轮):    python3 lark_pull.py --watch
    预览到终端:                python3 lark_pull.py --show

注意: 飞书的凭据只在 Mac 上，板子永远拿不到。state.json 也不含任何飞书密钥。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LARK = os.path.join(os.path.expanduser("~"), ".local", "bin", "lark-cli")

# 飞书任务的优先级字符串 -> 屏幕上的档位
PRI_MAP = {"urgent": "high", "high": "high", "medium": "mid", "low": "low"}

# app_link 里带 unix 时间戳，作为时间解析失败的兜底
_TS_RE = re.compile(r"[?&]startTime=(\d+)")


def run(args, timeout=45):
    """跑 lark-cli。

    返回 (data, err)：
      成功 -> (解析后的 JSON, None)
      失败 -> (None, 人类可读的错误说明)

    失败必须和"成功但结果为空"区分开。飞书没授权时 task 会返回
    {"ok": false, "error": {"subtype": "missing_scope"}}，如果当成空列表处理，
    屏上会显示"0 条待办" —— 看起来像没任务，其实是没权限。
    """
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(LARK) + ":" + env.get("PATH", "")
    try:
        p = subprocess.run([LARK] + args, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        msg = "超时 %ds" % timeout
        print("  [warn] %s %s" % (args[0], msg))
        return None, msg
    out = (p.stdout or "").strip()
    err_out = (p.stderr or "").strip()

    # lark-cli 把 "ok: false" 的错误 JSON 写进 stderr，stdout 反而是空的。
    # 只看 stdout 会把「缺权限」误判成「无输出」。
    data = None
    for blob in (out, err_out):
        if not blob:
            continue
        try:
            data = json.loads(blob)
            break
        except json.JSONDecodeError:
            continue

    if data is None:
        msg = "无输出（退出码 %d）%s" % (
            p.returncode, ("stderr: " + err_out[:100]) if err_out else "")
        print("  [warn] %s %s" % (args[0], msg))
        return None, msg

    if isinstance(data, dict) and data.get("ok") is False:
        err = data.get("error", {}) or {}
        msg = err.get("message", "?")
        missing = err.get("missing_scopes") or []
        if missing:
            msg = "缺权限 " + ", ".join(missing)
        print("  [warn] %s 失败: %s" % (args[0], msg[:160]))
        return None, msg
    return data, None


# --------------------------------------------------------------------------
# 待办
# --------------------------------------------------------------------------
def pull_tasks():
    """拉所有任务（已完成 + 未完成），返回 (item 列表, 错误说明)。"""
    data, err = run(["task", "+get-my-tasks", "--as", "user", "--format", "json",
                     "--page-all"])
    if data is None:
        return None, err
    items = data.get("data") or data.get("items") or data.get("tasks") or []
    out = []
    for t in items:
        summary = (t.get("summary") or t.get("title") or "").strip()
        if not summary:
            continue
        done = bool(t.get("completed") or t.get("done"))
        pri = PRI_MAP.get((t.get("priority") or "").lower(), "")
        due = ""
        d = t.get("due") or {}
        if isinstance(d, dict):
            ts = d.get("timestamp") or d.get("due_date")
            if ts:
                due = _fmt_ts(ts)
        out.append({"text": summary, "done": done, "pri": pri, "due": due})
    return out, None


def _fmt_ts(ts):
    """飞书时间戳（秒或毫秒）-> 'MM-DD' 或 'MM-DD HH:MM'。"""
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return ""
    if v > 1e12:              # 毫秒
        v //= 1000
    if v <= 0:
        return ""
    dt = datetime.fromtimestamp(v)
    today = datetime.now().date()
    if dt.date() == today:
        return "今天 " + dt.strftime("%H:%M")
    if dt.date() == today + timedelta(days=1):
        return "明天"
    if dt.date() == today - timedelta(days=1):
        return "昨天"
    return dt.strftime("%m-%d")


# --------------------------------------------------------------------------
# 日程
# --------------------------------------------------------------------------
def pull_events():
    """拉今天的日程，返回 (按开始时间升序的事件列表, 错误说明)。"""
    data, err = run(["calendar", "+agenda", "--as", "user", "--format", "json"])
    if data is None:
        return None, err
    events = data.get("data") or data.get("events") or data.get("items") or []
    out = []
    for e in events:
        title = (e.get("summary") or e.get("title") or e.get("subject") or "").strip()
        if not title:
            continue
        link = e.get("app_link") or ""
        st = _pick_ts(e.get("start") or e.get("start_time") or {}, link)
        et = _pick_ts(e.get("end") or e.get("end_time") or {})
        if st and not et:
            et = st + 1800
        elif st and et <= st:
            # 零时长的瞬时日程（飞书上就只记了一个点），不编结束时间
            et = 0
        loc = e.get("location")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        elif not loc:
            # 没有地点但有会议链接 = 线上
            loc = "线上" if (e.get("vchat") or {}).get("meeting_url") else ""
        out.append({
            "title": title,
            "start": st,
            "end": et,
            "start_ts": st,
            "end_ts": et,
            "location": loc or "",
        })
    out.sort(key=lambda x: x["start_ts"] or 0)
    return out, None


def _pick_ts(node, raw=None):
    """日程的 start/end 结构不统一，掏出一个 unix 秒时间戳。

    飞书 calendar +agenda 实际给的是
        "start_time": {"datetime": "2026-10-22T10:30:00+08:00", "timezone": ...}
    所以 datetime 字符串必须支持。只认 timestamp 会把时间解析成 0。
    兜底再从 app_link 的 startTime= 里捞。
    """
    if isinstance(node, dict):
        for k in ("timestamp", "date", "time", "datetime"):
            v = node.get(k)
            if v is None:
                continue
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v).timestamp()
                except ValueError:
                    continue
            try:
                iv = int(v)
                return iv // 1000 if iv > 1e12 else iv
            except (TypeError, ValueError):
                continue
    elif isinstance(node, (int, float)):
        v = int(node)
        return v // 1000 if v > 1e12 else v
    if raw:
        m = _TS_RE.search(raw)
        if m:
            return int(m.group(1))
    return 0


def _hhmm(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def build_state():
    """拉一次，组装成 state。

    返回 (state, errors)：
      state 为 None 表示两边都失败（没数据可写）
      errors 是 {来源: 原因}，只包含失败的那几路

    关键：ok_tasks / ok_events 明确标出"这一路是真数据还是没拉到"。
    下游（渲染器）靠它区分"没有待办"和"待办没授权"。
    """
    now = datetime.now()
    tasks, t_err = pull_tasks()
    events, e_err = pull_events()

    errors = {}
    if t_err:
        errors["tasks"] = t_err
    if e_err:
        errors["events"] = e_err

    if tasks is None and events is None:
        return None, errors

    tasks = tasks or []
    events = events or []

    # 标出"正在进行"和"下一个"
    now_ts = now.timestamp()
    for e in events:
        e["start_hm"] = _hhmm(e["start_ts"])
        e["end_hm"] = _hhmm(e["end_ts"])
        e["now"] = bool(e["start_ts"] and e["end_ts"]
                        and e["start_ts"] <= now_ts <= e["end_ts"])
        e["next"] = bool(e["start_ts"] and e["start_ts"] > now_ts
                         and not any(x.get("now") for x in events))
    nxt = next((e for e in events if e["now"]), None) or \
        next((e for e in events if e["next"]), None)

    state = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "user": os.environ.get("USER", ""),
        "tasks": tasks,
        "events": events,
        "next_event": nxt,
        "open_tasks": sum(1 for t in tasks if not t["done"]),
        "total_tasks": len(tasks),
        "ok_tasks": t_err is None,
        "ok_events": e_err is None,
        "mock": {"tasks": False, "events": False},
        "errors": errors,
    }
    return state, errors


def write_state(state):
    """原子写，避免板子正好在读时读到半个文件。"""
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def show(state):
    print("=" * 46)
    print(" 更新于 %s" % state["updated_at"])
    print("=" * 46)
    print(" 待办 %d 条（未完成 %d）" % (state["total_tasks"], state["open_tasks"]))
    for t in state["tasks"][:12]:
        mark = "x" if t["done"] else " "
        pri = {"high": " !", "mid": " ~", "low": ""}.get(t.get("pri", ""), "")
        due = ("  [%s]" % t["due"]) if t["due"] else ""
        print("  [%s]%s %s%s" % (mark, pri, t["text"], due))
    print(" 日程 %d 条" % len(state["events"]))
    for e in state["events"][:10]:
        flag = "*" if e.get("now") else (" " if not e.get("next") else ">")
        rng = "%s-%s" % (e["start_hm"], e["end_hm"]) if e["end_hm"] else e["start_hm"]
        loc = ("  @%s" % e["location"]) if e.get("location") else ""
        print("  %s %-11s %s%s" % (flag, rng, e["title"], loc))
    print("=" * 46)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="持续拉取")
    ap.add_argument("--interval", type=int, default=120, help="轮询间隔秒")
    ap.add_argument("--show", action="store_true", help="拉到终端里看")
    args = ap.parse_args()

    if not os.path.exists(LARK):
        print("找不到 lark-cli: %s" % LARK)
        sys.exit(1)

    rc = 0
    while True:
        print("[pull] %s" % datetime.now().strftime("%H:%M:%S"))
        st, errors = build_state()
        if st is None:
            print("  [FAIL] 两边都没拉到数据，保留上一份 state.json")
            rc = 1
        else:
            write_state(st)
            if errors:
                # 部分成功：日程是真的、待办没权限。绝不能退回 mock —— 会把真数据冲掉。
                print("  [PARTIAL] 待办 %d / 日程 %d   缺: %s" % (
                    st["total_tasks"], len(st["events"]),
                    "; ".join("%s(%s)" % (k, v) for k, v in errors.items())))
                rc = 2
            else:
                print("  [OK] 待办 %d / 日程 %d" %
                      (st["total_tasks"], len(st["events"])))
                rc = 0
            if args.show:
                show(st)
        if not args.watch:
            break
        time.sleep(args.interval)

    sys.exit(rc)


if __name__ == "__main__":
    main()
