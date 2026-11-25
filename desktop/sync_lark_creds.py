#!/usr/bin/env python3
"""
飞书凭据同步工具。

lark-cli 把凭据加密存在 macOS Keychain + 本地 .enc 文件里。
这套工具能读出明文，也能写回去（这样 lark-cli 和板子共用同一份凭据）。

存储格式（lark-cli 1.0.68，逆向自 bin/lark-cli）：
    master.key.file          32 字节主密钥
    appsecret_<app_id>.enc   nonce(12) || AES-256-GCM 密文 || tag(16)
    <app_id>_<open_id>.enc   同上，内容是 token 的 JSON

用法：
    # 读出所有凭据（app_id / app_secret / refresh_token）
    python3 sync_lark_creds.py read

    # 用 device_code 换 token（自己走 device flow，不需要 lark-cli）
    python3 sync_lark_creds.py exchange --device-code XXX

    # 把新的 refresh_token 写回 lark-cli（保持 lark-cli 可用）
    python3 sync_lark_creds.py writeback --refresh-token XXX

    # 生成写进板子 NVS 的 CSV
    python3 sync_lark_creds.py nvs-csv --out wifi_nvs.csv
"""
import argparse
import base64
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, MAC_IP, WIFI_PASS, WIFI_SSID  # noqa: E402

DIR = os.path.expanduser("~/Library/Application Support/lark-cli")
MASTER = os.path.join(DIR, "master.key.file")
OPEN_ID = os.environ.get("LARK_OPEN_ID", "")
TOKEN_ENC = os.path.join(DIR, "%s_%s.enc" % (APP_ID, OPEN_ID))
SECRET_ENC = os.path.join(DIR, "appsecret_%s.enc" % APP_ID)
FEISHU = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"


def aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def key():
    with open(MASTER, "rb") as f:
        return f.read()


def decrypt_file(path, k=None):
    blob = open(path, "rb").read()
    return aesgcm()(k or key()).decrypt(blob[:12], blob[12:], None)


def encrypt_file(plain, path, k=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    a = AESGCM(k or key())
    nonce = os.urandom(12)
    ct = a.encrypt(nonce, plain, None)
    with open(path, "wb") as f:
        f.write(nonce + ct)
    os.chmod(path, 0o600)


def read_all():
    out = {"app_id": APP_ID}
    try:
        out["app_secret"] = decrypt_file(SECRET_ENC).decode()
    except Exception as e:
        out["app_secret_error"] = str(e)
    try:
        out.update(json.loads(decrypt_file(TOKEN_ENC)))
    except Exception as e:
        out["token_error"] = str(e)
    return out


def exchange(device_code):
    """用 device_code 换 token（带 app_secret，不依赖 lark-cli）。"""
    c = read_all()
    sec = c.get("app_secret")
    if not sec:
        print("拿不到 app_secret", file=sys.stderr)
        return 1
    body = json.dumps({
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": APP_ID,
        "client_secret": sec,
    })
    r = subprocess.run(["curl", "-s", "-m", "30", "-X", "POST", FEISHU,
                        "-H", "Content-Type: application/json", "-d", body],
                       capture_output=True, text=True)
    try:
        j = json.loads(r.stdout)
    except Exception:
        print("响应不是 JSON:", r.stdout[:300], file=sys.stderr)
        return 1
    if "error" in j:
        print("✗", j.get("error"), j.get("error_description"), file=sys.stderr)
        return 1
    return j


def writeback(tokens):
    """把新的 token 写回 lark-cli 的 .enc。"""
    cur = read_all()
    cur.update({
        "accessToken": tokens["access_token"],
        "refreshToken": tokens["refresh_token"],
        "expiresAt": int((time_now() + tokens.get("expires_in", 7200)) * 1000),
        "refreshExpiresAt": int((time_now() + tokens.get("refresh_token_expires_in", 604800)) * 1000),
    })
    if "scope" in tokens:
        cur["scope"] = tokens["scope"]
    plain = json.dumps(cur, ensure_ascii=False).encode()
    encrypt_file(plain, TOKEN_ENC)
    print("✓ 已写回 lark-cli: %s" % TOKEN_ENC)


def time_now():
    import time
    return time.time()


def nvs_csv(out, refresh_token=None, host="open.feishu.cn", port=443, tls=1,
            ssid=None, password=None, mac_ip=None):
    c = read_all()
    rt = refresh_token or c.get("refreshToken", "")
    sec = c.get("app_secret", "")
    ssid = WIFI_SSID if ssid is None else ssid
    password = WIFI_PASS if password is None else password
    mac_ip = MAC_IP if mac_ip is None else mac_ip
    rows = [
        ("wifi", None, None, None),
        ("wifi_ssid", "data", "string", ssid),
        ("wifi_pass", "data", "string", password),
        ("wifi_host", "data", "string", mac_ip),
        ("lark", None, None, None),
        ("lark_app_id", "data", "string", c.get("app_id", APP_ID)),
        ("lark_app_secret", "data", "string", sec),
        ("lark_rt1", "data", "string", rt[:2400]),
        ("lark_rt2", "data", "string", rt[2400:]),
        ("lark_host", "data", "string", host),
        ("lark_port", "data", "string", str(port)),
        ("lark_tls", "data", "string", str(tls)),
    ]
    with open(out, "w", encoding="utf-8") as f:
        f.write("key,type,encoding,value\n")
        for k, t, e, v in rows:
            if t is None:
                f.write("%s,namespace,,\n" % k)
            else:
                f.write("%s,%s,%s,%s\n" % (k, t, e, v))
    print("✓ 写出 %s" % out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["read", "exchange", "writeback", "nvs-csv"])
    ap.add_argument("--device-code")
    ap.add_argument("--refresh-token")
    ap.add_argument("--tokens-json", help="exchange 出来的 json 文件")
    ap.add_argument("--out")
    ap.add_argument("--host", default="open.feishu.cn")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--tls", type=int, default=1)
    a = ap.parse_args()

    if a.cmd == "read":
        c = read_all()
        for k, v in c.items():
            if isinstance(v, str) and len(v) > 50:
                print("  %-18s = %s…(%d)" % (k, v[:36], len(v)))
            else:
                print("  %-18s = %s" % (k, v))
        return 0

    if a.cmd == "exchange":
        j = exchange(a.device_code)
        if isinstance(j, int):
            return j
        print(json.dumps({k: (v[:40] + "…") if isinstance(v, str) and len(v) > 40 else v
                          for k, v in j.items()}, ensure_ascii=False, indent=1))
        if a.tokens_json:
            with open(a.tokens_json, "w") as f:
                json.dump(j, f)
        return 0

    if a.cmd == "writeback":
        j = json.load(open(a.tokens_json)) if a.tokens_json else json.loads(a.refresh_token)
        return 0 if writeback(j) is None else 1

    if a.cmd == "nvs-csv":
        rt = a.refresh_token
        if not rt and a.tokens_json:
            rt = json.load(open(a.tokens_json))["refresh_token"]
        nvs_csv(a.out, refresh_token=rt, host=a.host, port=a.port, tls=a.tls)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
