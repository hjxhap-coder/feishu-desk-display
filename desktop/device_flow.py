#!/usr/bin/env python3
"""
飞书 OAuth v2 设备流：签发一组**新的** refresh_token 给板子。

为什么需要这个：
    飞书的 refresh_token 是**一次性**的 —— 用过就作废，而且必须立刻存回
    板子的 NVS。如果那一次响应没回来（网络抖一下），这组 token 就永久废了，
    只能重新授权。这个脚本就是那条重新授权的路。

流程（RFC 8628）：
    1. POST accounts.feishu.cn/oauth/v1/device_authorization   -> device_code + 验证 URL
    2. 用户在浏览器打开 URL 点确认
    3. 轮询 POST /open-apis/authen/v2/oauth/token          -> access_token + refresh_token

用法：
    python3 device_flow.py                 # 签发到本机文件
    python3 device_flow.py --write-nvs     # 签发后直接写进板子的 NVS
"""
import argparse
import json
import os
import subprocess
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, APP_SECRET, need, need_wifi  # noqa: E402
from mac_auth import DEVICE_URL, TOKEN_URL, SCOPE, post_form, post_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(tempfile.gettempdir(), "lark_new_tokens.json")


def request_device_code(scope):
    response = post_json(DEVICE_URL, {
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "scope": scope,
    })
    if "device_code" not in response:
        raise SystemExit("申请 device_code 失败：%s" % response.get("error", "未知错误"))
    return response


def poll_token(device_code, interval, expires_in):
    deadline = time.time() + expires_in
    cur = interval
    while time.time() < deadline:
        time.sleep(cur)
        r = post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
        })
        if "error" in r:
            err = r.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                cur = min(cur + 5, 60)
                continue
            raise SystemExit("授权失败: %s %s" % (err, r.get("error_description")))
        return r
    raise SystemExit("授权超时")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-nvs", action="store_true", help="签发后直接写进板子")
    ap.add_argument("--scope", default=None)
    a = ap.parse_args()

    need("APP_ID", "APP_SECRET")
    if a.write_nvs:
        need_wifi()
    scope = a.scope or SCOPE
    print("申请 device_code（scope %d 项）..." % len(scope.split()))
    d = request_device_code(scope)

    uri = d.get("verification_uri_complete") or d["verification_uri"]
    print("")
    print("  请在浏览器打开下面这个链接，登录飞书后点「确认授权」：")
    print("")
    print("    %s" % uri)
    print("")
    if d.get("user_code"):
        print("  （如果页面要你输验证码：%s）" % d["user_code"])
    print("  我在这儿等你确认，最长 %d 秒 ..." % d.get("expires_in", 240))

    tokens = poll_token(d["device_code"],
                        d.get("interval", 5),
                        d.get("expires_in", 240))
    print("")
    print("✓ 授权成功")
    print("  access_token  %d 字符" % len(tokens["access_token"]))
    print("  refresh_token %d 字符" % len(tokens["refresh_token"]))
    print("  scope         %d 项" % len(tokens.get("scope", "").split()))

    tokens["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 7200))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=tempfile.gettempdir(),
                                     prefix="lark-token-", delete=False) as token:
        json.dump(tokens, token, ensure_ascii=False)
    os.replace(token.name, TOKEN_FILE)
    print("  本机授权文件：%s" % TOKEN_FILE)

    if a.write_nvs:
        subprocess.run([sys.executable, os.path.join(HERE, "write_nvs.py"),
                        "--token-file", TOKEN_FILE], check=True)
        print("  板子已写入新凭据。")


if __name__ == "__main__":
    main()
