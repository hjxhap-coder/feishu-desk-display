#!/usr/bin/env python3
"""老路子：Mac 自己从飞书拉一份 state.json。

**默认不走这条路**（见 pull_state.py 的说明）——
只有「板子不在线但还想看数据」时才用，而且需要 mac_token.json。

为什么要保留：
    板子没连上 WiFi / 没授权的时候，Mac 上还想看到数据长什么样，
    这是唯一的数据来源。

代价：
    一组 refresh_token 只能用一次 —— 这条路的 RT 和板子的 RT 是**两次独立
    device flow** 签出来的，互不影响，但各自都会在第一次网络丢包时死掉。
"""
import sys
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

API = "https://open.feishu.cn"
CST = timezone(timedelta(hours=8))

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac_token.json")
REFRESH_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID as CLIENT_ID, APP_SECRET as CLIENT_SECRET  # noqa: E402


def get(path, token):
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", "Bearer " + token)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode())


def pick_time(v):
    """飞书的时间字段：{"timestamp": "1793030400", "is_dst": false} 或纯字符串。"""
    if v is None:
        return 0
    if isinstance(v, dict):
        ts = v.get("timestamp")
        if ts is None:
            return 0
        try:
            return int(ts)
        except (TypeError, ValueError):
            return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def hm_str(ts):
    return datetime.fromtimestamp(ts, CST).strftime("%H:%M")


def task_done(it):
    """完成态在 completed_at（毫秒时间戳字符串，"0" = 未完成）+ 子项 items[].completed。"""
    ca = it.get("completed_at")
    if ca is None:
        return False
    if isinstance(ca, str):
        if ca.strip() in ("", "0"):
            return False
        try:
            return int(ca) > 0
        except ValueError:
            return False
    if isinstance(ca, (int, float)):
        return ca > 0
    return False


def post_form(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def mac_access_token():
    """Mac 端**自己那组** token 的 access_token，过期就用自己那组的 RT 续期。

    为什么要单独一组：飞书的 refresh_token 是按授权实例独立的，
    但**同一组**的 RT 只能用一次 —— 谁拿它刷新，谁就把对方打死了。
    板子那组 RT 只存在板子 NVS 里，Mac 端用自己这组，两边互不影响。
    """
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("没有 %s —— 先跑一次 device flow 给 Mac 端签一组" % TOKEN_FILE)
    tok = json.load(open(TOKEN_FILE, encoding="utf-8"))
    now = int(time.time())
    if tok.get("expires_at", 0) > now + 300:
        return tok["access_token"]
    r = post_form(REFRESH_URL, {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": tok["refresh_token"],
    })
    if "error" in r:
        raise SystemExit("Mac 端 token 续期失败: %s %s（要重新签一组）"
                         % (r.get("error"), r.get("error_description")))
    tok["access_token"] = r["access_token"]
    tok["refresh_token"] = r.get("refresh_token", tok["refresh_token"])
    tok["expires_at"] = now + int(r.get("expires_in", 7200))
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=1)
    print("Mac 端 token 已续期（新的 refresh_token 也存好了）")
    return tok["access_token"]


def pull(out_path):
    at = mac_access_token()

    now = int(time.time())
    today0 = now - (now % 86400)
    start, end = today0, today0 + 86399

    # 和板子一样分两次拉：未完成全量 + 已完成最近 20 条
    items = get("/open-apis/task/v2/tasks?page_size=100&completed=false",
                at).get("data", {}).get("items", [])
    items += get("/open-apis/task/v2/tasks?page_size=20&completed=true",
                 at).get("data", {}).get("items", [])

    cal = get("/open-apis/calendar/v4/calendars/primary/events/instance_view"
              "?start_time=%d&end_time=%d" % (start, end), at)
    ev_raw = cal.get("data", {}).get("items", [])
    events_list = []
    for it in ev_raw:
        if "events" in it:
            events_list.extend(it.get("events", []))
        else:
            events_list.append(it)

    tasks = []
    open_n = 0
    for it in items:
        summary = it.get("summary") or it.get("title") or it.get("name") or ""
        if not summary:
            continue
        done = task_done(it)
        if done:
            # 只保留「今天完成的」—— 沉底的语义是「今天搞定了什么」。
            ca = it.get("completed_at")
            try:
                ts = int(ca) // 1000
            except (TypeError, ValueError):
                ts = 0
            if ts and not (today0 <= ts < today0 + 86400):
                continue
        else:
            open_n += 1
        tasks.append({"text": summary, "done": done, "pri": "", "due": ""})

    events = []
    for it in events_list:
        summary = it.get("summary") or it.get("title") or it.get("subject") or ""
        if not summary:
            continue
        s = pick_time(it.get("start_time"))
        e = pick_time(it.get("end_time"))
        if not s:
            continue
        loc = it.get("location")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        elif not isinstance(loc, str):
            loc = ""
        events.append({
            "start_hm": hm_str(s),
            "end_hm": hm_str(e) if e > s else "",
            "location": loc or "",
            "title": summary,
            # 零长度事件（end == start，飞书里有些日程就是这样）只算开始后的那一分钟。
            "now": bool(now >= s and now < (e if e > s else s + 60)),
        })

    # 和板子一样按开始时间排序（飞书返回的不是时间顺序）
    events.sort(key=lambda ev: ev["start_hm"])

    state = {
        "updated_at": datetime.fromtimestamp(now, CST).strftime("%Y-%m-%d %H:%M"),
        "tasks": tasks,
        "events": events,
        "open_tasks": open_n,
        "total_tasks": len(tasks),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print("→ %s：待办 %d 条（未完成 %d）/ 日程 %d 条" %
          (out_path, len(tasks), open_n, len(events)))
