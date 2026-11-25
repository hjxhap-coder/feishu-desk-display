#!/usr/bin/env python3
"""给 **Mac 端** 签一组独立的飞书 token（板子那组完全不动）。

为什么要两组：
    飞书的 refresh_token 按**授权实例**独立签发，但**同一组**的 RT 只能用一次。
    谁拿组里的 RT 去刷新，谁就把同组的所有人打死了（板子会一直 invalid_grant）。
    所以：组 A 给 Mac 端（pull_state.py 用，自带续期），
          组 B 留在板子 NVS 里，谁也不碰。

流程（RFC 8628 device flow）：
    1. POST accounts.feishu.cn/oauth/v1/device_authorization
    2. 用户在浏览器点「确认授权」
    3. 轮询 open-apis/authen/v2/oauth/token 换 token
    4. 存进 mac_token.json

用法:
    python3 mac_auth.py            # 申请 + 打印链接 + 后台轮询
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "mac_token.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID as CLIENT_ID, APP_SECRET as CLIENT_SECRET  # noqa: E402
DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"

# 和板子实际需要的 scope 完全一致，不多不少
SCOPE = ("task:task:read calendar:calendar:read calendar:calendar.event:read "
         "calendar:calendar.free_busy:read contact:user.base:readonly offline_access")


def post_json(url, obj, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def post_form(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def main():
    d = post_json(DEVICE_URL, {"client_id": CLIENT_ID,
                                "client_secret": CLIENT_SECRET,
                                "scope": SCOPE})
    if "error" in d:
        raise SystemExit("申请 device_code 失败: %s" % d)
    url = d.get("verification_uri_complete") or d["verification_uri"]
    print("")
    print("请在浏览器打开（登录飞书后点「确认授权」）：")
    print("")
    print("    %s" % url)
    print("")
    print("  最长等 %d 秒 ..." % d.get("expires_in", 600), flush=True)
    with open("/tmp/mac_device_url.txt", "w") as f:
        f.write(url)

    deadline = time.time() + d.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(5)
        r = post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": d["device_code"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        if "error" in r:
            if r.get("error") in ("authorization_pending", "slow_down"):
                continue
            raise SystemExit("授权失败: %s %s" % (r.get("error"), r.get("error_description")))
        tok = {
            "access_token": r["access_token"],
            "refresh_token": r["refresh_token"],
            "expires_at": int(time.time()) + int(r.get("expires_in", 7200)),
            "scope": r.get("scope", ""),
        }
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(tok, f, ensure_ascii=False, indent=1)
        os.chmod(TOKEN_FILE, 0o600)
        print("")
        print("✓ Mac 端 token 已签发并保存到 %s" % TOKEN_FILE)
        print("  这组和板子那组**完全独立**，随便刷新都不会互相打死。")
        return
    raise SystemExit("授权超时")


if __name__ == "__main__":
    main()
