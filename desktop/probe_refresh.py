#!/usr/bin/env python3
"""探针：dump 飞书 refresh_token 刷新的**完整原始响应**。

为什么需要它：
    板子的 refresh_token 在飞书是**一次性的** —— 用一次就作废。
    板子必须拿到响应里的新 refresh_token 存回 NVS，否则两小时后必死。
    但板子的日志显示轮换后的新 RT **根本没写进 NVS**
    （NVS 里只剩最初那组，页 2 至今全空 = 新 RT 从没落盘），
    而代码里 `rt_changed = (rt && strcmp(rt, old) != 0)` 为 false
    —— 说明**响应里没有 refresh_token 字段**。

    这个脚本把响应原样打出来，别猜。

用法：
    python3 probe_refresh.py --refresh-token <rt>
    python3 probe_refresh.py --token-file /tmp/lark_new_tokens.json
"""
import os
import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID as CLIENT_ID, APP_SECRET as CLIENT_SECRET  # noqa: E402


def post_form(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-token")
    ap.add_argument("--token-file")
    a = ap.parse_args()

    rt = a.refresh_token
    if not rt and a.token_file:
        rt = json.load(open(a.token_file, encoding="utf-8")).get("refresh_token")
    if not rt:
        raise SystemExit("没有 refresh_token")

    print("用 RT(前 30=%s...) 调一次刷新 —— **这个 RT 会被消费掉**" % rt[:30])
    status, body = post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": rt,
    })
    print("HTTP %d" % status)
    try:
        d = json.loads(body)
    except Exception:
        print("非 JSON 响应：", body[:800])
        return
    print("顶层字段: %s" % sorted(d.keys()))
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 120:
            print("  %-26s = <%d 字符> %s..." % (k, len(v), v[:40]))
        elif isinstance(v, dict):
            print("  %-26s = dict keys=%s" % (k, sorted(v.keys())))
        else:
            print("  %-26s = %r" % (k, v))

    # 结论
    box = d.get("data") if isinstance(d.get("data"), dict) else d
    new_rt = box.get("refresh_token")
    if new_rt:
        print("\n✓ 响应里有 refresh_token（%d 字符）—— 板子应该能存下来" % len(new_rt))
        print("  但板子 rt_changed 判定为 false，说明它取的字段位置不对")
    else:
        print("\n✗ 响应里**没有** refresh_token —— 飞书不轮换 RT")
        print("  → 一次性 RT 用掉就没了，板子必须换别的续期方式")


if __name__ == "__main__":
    main()
