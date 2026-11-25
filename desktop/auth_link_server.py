#!/usr/bin/env python3
"""常驻授权链接服务 —— 解决「device_code 10 分钟就过期，用户总错过」。

用法：
    python3 auth_link_server.py            # 起服务（前台）
    # 手机/电脑打开 http://<Mac IP>:8765/  -> 自动跳到当前最新的飞书验证页
    # 二维码直接指向这个固定地址，永远有效。

原理：
    飞书的 device_code 只能用一次、10 分钟过期，用户很难卡点。
    这里每隔 4 分钟自动签发一个新 device_code，**所有签发过的都继续轮询**，
    所以用户任何时刻点链接，授权都会落到某个仍然有效的 code 上，
    轮询线程一定能收到。

拿到 token 后写进 mac_token.json（Mac 端用），并打印出来。
"""
import http.server
import json
import os
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "mac_token.json")
PORT = 8765

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, APP_SECRET  # noqa: E402
DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
SCOPE = ("task:task:read calendar:calendar:read calendar:calendar.event:read "
         "calendar:calendar.free_busy:read contact:user.base:readonly offline_access")

STATE = {"codes": [], "current": None, "done": None, "lock": threading.Lock()}
_stop = threading.Event()


def post_json(url, obj, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": "http %d" % e.code}
    except Exception as e:                       # noqa: BLE001
        return {"error": str(e)}


def refresh_with_rt(rt):
    """用 refresh_token 换一对新的 (access_token, refresh_token)。

    【为什么要多这一步】飞书的 RT 是「收到请求就作废」的 ——
    响应一旦在路上丢了，RT 就废了且拿不到新的。
    Mac 端网络比板子好，先在这儿换一次：
      - 成功 -> 拿到的是**最新的** RT 和一个现成的 AT，
        板子直接带着这个 AT 启动，头两小时压根不用刷新；
      - 失败 -> 说明这个 RT 已经是死的，别灌给板子（灌了它也活不过 2 小时）。
    """
    r = post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "refresh_token": rt,
    })
    if "access_token" not in r:
        return None, r
    return {
        "access_token": r["access_token"],
        "refresh_token": r.get("refresh_token", rt),
        "expires_in": r.get("expires_in", 7200),
        "expires_at": int(time.time()) + int(r.get("expires_in", 7200)),
        "scope": r.get("scope", ""),
    }, r


def post_form(url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": "http %d" % e.code}
    except Exception as e:                       # noqa: BLE001
        return {"error": str(e)}


def issuer():
    """保底：每隔 2 分钟也签一个新 device_code（旧的继续轮询）。
    handler 每次访问都会当场签一个，这个线程只是多备几个，
    让人可以直接复制日志里的链接去点。"""
    while not _stop.is_set():
        d = post_json(DEVICE_URL, {"client_id": APP_ID, "client_secret": APP_SECRET,
                                   "scope": SCOPE})
        if "device_code" in d:
            url = d.get("verification_uri_complete") or d["verification_uri"]
            with STATE["lock"]:
                STATE["codes"].append({
                    "code": d["device_code"],
                    "url": url,
                    "purpose": STATE.get("next_purpose", "board"),
                    "exp": time.time() + d.get("expires_in", 600),
                })
                STATE["codes"] = [c for c in STATE["codes"] if c["exp"] > time.time()]
                STATE["current"] = url
            # 写一份当前链接，方便别处直接取
            try:
                with open("/tmp/lark_auth_url.txt", "w") as f:
                    f.write(url)
            except OSError:
                pass
            print("[%s] 签发新 device_code，当前链接: %s" %
                  (time.strftime("%H:%M:%S"), url), flush=True)
        else:
            print("[%s] 签发失败: %s" % (time.strftime("%H:%M:%S"), d), flush=True)
        _stop.wait(600)   # 备用 signer，10 分钟一个就够（handler 每次访问都会签）


def poller():
    """轮询待授权的 code —— 用户任何时刻点的链接都能被接住。

    【踩过的坑】一开始是「每 5 秒把**所有** code 全 poll 一遍」，
    等于每秒 1 个请求 —— 飞书直接回 `slow_down`
    （"The client is polling too frequently"），
    **用户明明授权成功了，token 却兑换不出来**，
    而且越轮询越被限流，恶性循环。

    正确做法（照飞书返回的 interval 走）：
      - 一次只 poll 一个 code（轮转），不是全部
      - 间隔 >= interval（飞书给 5 秒）
      - 遇到 slow_down 就把间隔翻倍
      - 旧的 code 及时清掉，别攒一堆
    """
    interval = 5
    idx = 0
    while not _stop.is_set():
        with STATE["lock"]:
            codes = [c for c in STATE["codes"] if c["exp"] > time.time()]
            # 只留最近的 3 个 —— 更老的用户不会去点，攒着只是招限流
            STATE["codes"] = codes[-3:]
        if not codes:
            _stop.wait(interval)
            continue

        c = codes[idx % len(codes)]
        idx += 1
        r = post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": c["code"],
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
        })

        err = r.get("error")
        print("[%s] poll code %s... -> %s" %
              (time.strftime("%H:%M:%S"), c["code"][:12],
               ("OK!" if not err else err)), flush=True)
        if err == "slow_down":
            interval = min(interval * 2, 60)
            print("[%s] 被飞书限流(slow_down)，轮询间隔调到 %d 秒" %
                  (time.strftime("%H:%M:%S"), interval), flush=True)
        elif err == "authorization_pending":
            interval = max(interval, 5)

        if err:
            _stop.wait(interval)
            continue

        # ---- 授权成功 ----
        c_code = c["code"]
        tok = {
            "access_token": r["access_token"],
            "refresh_token": r["refresh_token"],
            "expires_in": r.get("expires_in", 7200),
            "expires_at": int(time.time()) + int(r.get("expires_in", 7200)),
            "scope": r.get("scope", ""),
        }
        purpose = c.get("purpose", "board")
        if purpose == "mac":
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(tok, f, ensure_ascii=False, indent=1)
            os.chmod(TOKEN_FILE, 0o600)
            print("\n✓ 授权成功（Mac 端）！token 已存 %s" % TOKEN_FILE, flush=True)
        else:
            # 先在 Mac 端自己轮换一次 —— 网络好，响应不容易丢。
            # 成功就拿到「最新鲜的 RT + 现成的 AT」，板子直接带着 AT 启动。
            print("  在 Mac 端先轮换一次（确认 RT 可用、拿到现成的 AT）...", flush=True)
            fresh, why = refresh_with_rt(tok["refresh_token"])
            if fresh is None:
                print("  ✗ 这个 RT 已经用不了了：%s" % why.get("error_description"), flush=True)
                print("    不灌给板子（灌了它也活不过两小时），请重新授权一次。", flush=True)
                return
            tok = fresh
            print("  ✓ 轮换成功，拿到现成的 AT", flush=True)

            board_json = os.path.join(HERE, "board_token.json")
            with open(board_json, "w", encoding="utf-8") as f:
                json.dump(tok, f, ensure_ascii=False, indent=1)
            os.chmod(board_json, 0o600)
            print("\n✓ 授权成功（板子）！正在灌进 NVS ...", flush=True)
            here = os.path.dirname(os.path.abspath(__file__))
            res = subprocess.run(
                [sys.executable, os.path.join(here, "write_nvs.py"),
                 "--token-file", board_json],
                capture_output=True, text=True, timeout=300)
            print(res.stdout[-400:] or res.stderr[-400:], flush=True)
            print("  板子已重启，稍后读 NVS 验证", flush=True)
        print("  refresh_token %d 字符，有效期 %d 秒" %
              (len(tok["refresh_token"]), tok["expires_in"]), flush=True)
        with STATE["lock"]:
            STATE["done"] = time.strftime("%H:%M:%S")
            # 【坑】这里原来写着 `_stop.set()` —— 一次授权成功就把
            # issuer 和 poller 两个线程全停了，**服务变成一次性用品**：
            # 之后再点 /board 只会签发 device_code，**没人 poll 它**，
            # 授权永远停在 authorization_pending。
            # （2026-11-24 踩了：用户点了三次链接都没反应，
            #   原因就是 11/23 那次成功把服务搞停了。）
            # 现在改成：把**已经用掉的** code 从列表里去掉，服务继续跑，
            # 下次点链接照样能授权。
            STATE["codes"] = [c for c in STATE["codes"] if c["code"] != c_code]
        return


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                            # noqa: N802
        with STATE["lock"]:
            cur = STATE["current"]
            done = STATE["done"]
            # 路径决定用途：/board 给板子（默认），/mac 给 Mac 端。
            # 必须是**不同的授权实例**，同一组 token 两边会互相作废。
            purpose = "mac" if self.path.startswith("/mac") else "board"
            STATE["next_purpose"] = purpose
        if self.path.startswith("/status"):
            body = ("OK\ncurrent=%s\ndone=%s\n" % (cur, done)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 当场签一个**带正确用途**的 device_code 再跳过去 ——
        # 直接跳"当前那个"会串味：那个 code 可能是为另一个用途签的。
        d = post_json(DEVICE_URL, {"client_id": APP_ID, "client_secret": APP_SECRET,
                                   "scope": SCOPE})
        if "device_code" not in d:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"cannot issue device code")
            return
        url = d.get("verification_uri_complete") or d["verification_uri"]
        with STATE["lock"]:
            STATE["codes"].append({
                "code": d["device_code"],
                "url": url,
                "purpose": purpose,
                "exp": time.time() + d.get("expires_in", 600),
            })
            STATE["codes"] = [c for c in STATE["codes"] if c["exp"] > time.time()]
            STATE["current"] = url
        print("[%s] 为「%s」签发 device_code -> %s" %
              (time.strftime("%H:%M:%S"), purpose, url), flush=True)
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    threading.Thread(target=issuer, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    httpd = socketserver.TCPServer(("0.0.0.0", PORT), Handler)
    httpd.allow_reuse_address = True
    print("常驻授权链接服务已启动: http://<Mac IP>:%d/" % PORT, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
