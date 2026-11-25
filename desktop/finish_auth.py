#!/usr/bin/env python3
"""轮询 device flow 的授权结果，拿到 token 后直接注入板子。

用法:
    python3 finish_auth.py --device-code <code>          # 只取 token
    python3 finish_auth.py --device-code <code> --inject # 取到后写进板子并验证

为什么注入走串口而不是重烧 NVS：
    整块擦 NVS 会把 WiFi 配置一起抹掉（要重灌）。
    板子上有 `lark rt <token>` 命令，敲一下就写进 NVS 并回读校验。
"""
import os
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, APP_SECRET, resolve_port  # noqa: E402
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
PORT = resolve_port()   # 串口插拔后端口名会变，自动探测


def post_form(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def poll(device_code, deadline_s=600):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        r = post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
        })
        if "error" not in r:
            return r
        err = r.get("error")
        if err in ("authorization_pending", "slow_down"):
            time.sleep(5)
            continue
        raise SystemExit("授权失败: %s %s" % (err, r.get("error_description")))
    raise SystemExit("授权超时")


def inject(rt):
    """把 refresh_token 敲进板子的串口。"""
    import serial
    s = serial.Serial(PORT, 115200, timeout=0.3)
    time.sleep(0.5)
    s.reset_input_buffer()
    cmd = "lark rt " + rt
    print("--> 发送 lark rt <%d 字符>" % len(rt), flush=True)
    s.write(cmd.encode())
    s.write(b"\n")
    s.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 40:
        chunk = s.read(1024)
        if chunk:
            buf += chunk
            sys.stdout.write(chunk.decode(errors="replace"))
            sys.stdout.flush()
        else:
            time.sleep(0.2)
    s.close()
    return buf.decode(errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-code", required=True)
    ap.add_argument("--inject", action="store_true")
    a = ap.parse_args()

    print("等授权中 ...", flush=True)
    tok = poll(a.device_code)
    rt = tok["refresh_token"]
    at = tok["access_token"]
    print("")
    print("✓ 授权成功")
    print("  access_token  %d 字符" % len(at))
    print("  refresh_token %d 字符" % len(rt))
    with open("/tmp/lark_new_tokens.json", "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False)

    if a.inject:
        out = inject(rt)
        if "回读校验通过" in out:
            print("\n✓ 板子已接收并回读校验通过，3 秒后自动重启")
        else:
            print("\n!! 没看到板子的回执，请检查串口")


if __name__ == "__main__":
    main()
