#!/usr/bin/env python3
"""决定性实验：device flow 签发的 refresh_token 是不是「一出生就死的」。

背景：11-03 10:21 签发的一组 RT（iat=10:21:23），
用户授权成功后板子拿去刷新 —— 飞书回 invalid_grant「已被撤销」。
那组 RT 签发后**一次都没被成功用过**，所以要么：
  (a) 有别的东西抢先消费了它
  (b) 飞书 device flow 返回的 RT 本身有问题

这个脚本能区分两者：签 code → 等授权 → **拿到 token 的那一瞬间**立刻用它刷新。
  - 立刻刷新成功 → (a)，当时是被别人消费的
  - 立刻刷新也失败 → (b)，飞书返回的 RT 有问题

用法：
    python3 exp_auth.py --code <device_code>
    python3 exp_auth.py            # 自己签一个并打印链接
"""
import os
import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, APP_SECRET  # noqa: E402
DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
SCOPE = ("task:task:read calendar:calendar:read calendar:calendar.event:read "
         "calendar:calendar.free_busy:read contact:user.base:readonly offline_access")


def pj(url, obj):
    r = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST")
    r.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(
        r, timeout=20, context=ssl.create_default_context()).read().decode())


def pf(url, data):
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        return 200, json.loads(urllib.request.urlopen(r, timeout=20).read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def issue():
    d = pj(DEVICE_URL, {"client_id": APP_ID, "client_secret": APP_SECRET, "scope": SCOPE})
    print("请点这个链接授权：")
    print("  " + d["verification_uri_complete"])
    print("  验证码: " + d["user_code"])
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code")
    a = ap.parse_args()
    if a.code:
        d = {"device_code": a.code}
    else:
        d = issue()
    code = d["device_code"]
    print("[%s] 开始轮询（每 6 秒）..." % time.strftime("%H:%M:%S"), flush=True)

    while True:
        st, r = pf(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": code, "client_id": APP_ID, "client_secret": APP_SECRET})
        if st == 200 and "access_token" in r:
            break
        if r.get("error") in ("slow_down", "authorization_pending"):
            time.sleep(6)
            continue
        print("授权失败:", r)
        return

    rt = r["refresh_token"]
    print("[%s] ✓ 授权成功！refresh_token %d 字符" %
          (time.strftime("%H:%M:%S"), len(rt)), flush=True)
    print("  立刻用它刷新一次（关键实验）...", flush=True)
    st2, r2 = pf(TOKEN_URL, {
        "grant_type": "refresh_token", "client_id": APP_ID,
        "client_secret": APP_SECRET, "refresh_token": rt})
    print("  → HTTP %s" % st2, flush=True)
    if st2 == 200 and "access_token" in r2:
        print("  ✓✓ 签发即用没问题！结论：是**别人**消费了当时那组 RT。", flush=True)
        json.dump({"access_token": r2["access_token"], "refresh_token": r2["refresh_token"],
                   "expires_in": r2.get("expires_in", 7200), "scope": r2.get("scope", "")},
                  open("/tmp/board_token.json", "w"))
        print("  已存 /tmp/board_token.json（这是**轮换后**的，可以直接灌板子）", flush=True)
    else:
        print("  ✗ 签发即死！结论：飞书 device flow 返回的 RT 本身有问题。", flush=True)
        print("     %s" % r2.get("error_description"), flush=True)


if __name__ == "__main__":
    main()
