#!/usr/bin/env python3
"""等授权完成 → 自动灌板子 → 自动验证新 RT 有没有真存住。

授权链接服务（auth_link_server.py）拿到 token 后会自动调 write_nvs.py，
这个脚本只负责**验证结果** —— 因为板子刷屏时串口日志会丢，
唯一可靠的证据是 NVS 里 token 的 jti/iat 变了。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.expanduser("~/.espressif/python_env/idf5.3_py3.9_env/bin/python3")
BOARD_TOKEN = os.path.join(HERE, "board_token.json")


def nvs_state():
    out = subprocess.run([PY, os.path.join(HERE, "nvs_dump.py")],
                         capture_output=True, text=True, timeout=240)
    return out.stdout


def main():
    print("[%s] 等 board_token.json ..." % time.strftime("%H:%M:%S"), flush=True)
    deadline = time.time() + 6 * 3600
    while time.time() < deadline:
        if os.path.exists(BOARD_TOKEN):
            time.sleep(5)
            break
        time.sleep(5)
    else:
        print("超时，没等到授权", flush=True)
        return

    tok = json.load(open(BOARD_TOKEN, encoding="utf-8"))
    print("[%s] 拿到板子 token，refresh_token %d 字符" %
          (time.strftime("%H:%M:%S"), len(tok.get("refresh_token", ""))), flush=True)

    # 等服务把 NVS 灌完 + 板子重启 + 换 AT + 保存新 RT
    for wait in (40, 60, 90):
        time.sleep(wait)
        st = nvs_state()
        print("-" * 60, flush=True)
        print(st, flush=True)
        if "access_tok" in st and "缺失" not in st.split("access_tok")[1][:40]:
            print("✓ 板子换到 access_token 了！", flush=True)
            return
    print("还没看到 access_token，继续等…", flush=True)


if __name__ == "__main__":
    main()
