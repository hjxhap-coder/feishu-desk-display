#!/usr/bin/env python3
"""
把三屏喂给 ESP32。

  GET /s/standby.bin    15000 字节 packed 1-bit 帧 (400x300, MSB first, 1=纸白)
  GET /s/todo.bin
  GET /s/schedule.bin
  GET /s/<mode>.png     2x 预览，用浏览器瞄一眼
  GET /health           "ok <mode> <bytes> <sha1>"
  GET /                 三个预览并排的检查页

数据流:
    lark_pull.py  -->  state.json  -->  C 排版引擎（和板子同源）  -->  /s/<mode>.bin  -->  ESP32

排版不再用 PIL 单独画一套 —— 调的是板子上那份 C 代码，所以 Mac 预览
和真机像素级一致，不会出现"预览好看真机不一样"。

后台线程按 --pull-interval 调 lark_pull.py 刷新 state.json（飞书权限没批下来时
自动退回 mock 数据，保证原型照样能跑）。

  python3 serve_screen.py [--port 8000] [--pull-interval 120] [--no-pull]
"""
import argparse
import hashlib
import importlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

STATE = os.path.join(HERE, "state.json")
MODES = ("standby", "todo", "schedule")

# 板子上跑的排版引擎就在这里。Mac 端出帧也调它 —— 同一份 C 代码，
# 所以预览和真机像素级一致，不会出现"预览好看真机不一样"。
# 仓库根 = 本文件所在目录的上一级
FW_DIR = os.path.dirname(HERE)
PREVIEW_EXE = os.path.join(FW_DIR, "tools", "_build", "mac_preview")
FONT_BIN = os.path.join(FW_DIR, "font", "font16.bin")
PREVIEW_OUT = os.path.join(FW_DIR, "preview")

# UDP 广播发现：板子问 "WHO"，Mac 回 "TODO-SCREEN <ip>:<port>"。
# 用广播而不是 mDNS，因为这台 ESP-IDF 源码树没有 mdns 组件。
DISCOVERY_PORT = 9999

# 待机屏有钟，得按秒重画；另两屏跟着 state.json 走
STANDBY_TTL = 15.0

_lock = threading.Lock()
_cache = {}          # mode -> {"t": 时间戳, "bin": bytes, "png": bytes}
_state_mtime = None


def state_mtime():
    try:
        return os.path.getmtime(STATE)
    except OSError:
        return None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated_at": "", "tasks": [], "events": [], "open_tasks": 0}


def ensure_preview_exe():
    """编译 Mac 端的排版程序（和板子同源的那份 C）。"""
    r = subprocess.run(
        [sys.executable, os.path.join(FW_DIR, "tools", "preview.py"), "--build-only"],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("[render] 编译排版程序失败:\n%s" % r.stderr[-800:], flush=True)
        return False
    return True


def bin_to_png(bin_bytes):
    """15000 字节的 1-bit 帧 -> 2x PNG（给浏览器预览用）。"""
    from PIL import Image
    W, H = 400, 300
    img = Image.new("L", (W, H), 255)
    px = img.load()
    for y in range(H):
        base = y * (W // 8)
        for x in range(W):
            bit = (bin_bytes[base + (x >> 3)] >> (7 - (x & 7))) & 1
            px[x, y] = 255 if bit else 0
    buf = io.BytesIO()
    img.convert("1").resize((W * 2, H * 2), 0).save(buf, "PNG")
    return buf.getvalue()


def c_render(mode):
    """用板子那份 C 代码出一帧。"""
    if not os.path.exists(PREVIEW_EXE):
        if not ensure_preview_exe():
            raise RuntimeError("排版程序没编出来")
    os.makedirs(PREVIEW_OUT, exist_ok=True)
    out_bin = os.path.join(PREVIEW_OUT, mode + ".bin")
    r = subprocess.run([PREVIEW_EXE, STATE, mode, FONT_BIN, out_bin],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("C 渲染失败 %s: %s" % (mode, r.stderr.strip()[-300:]))
    with open(out_bin, "rb") as f:
        data = f.read()
    if len(data) != 15000:
        raise RuntimeError("%s 帧长度 %d，不是 15000" % (mode, len(data)))
    return data


# 渲染器版本。
#   v1 = render.py       （初版，PIL）
#   v3 = render_v3.py    （隔壁定的设计规范 v3，PIL，多字号）
#   c  = components/shared 里的 C 排版引擎（和板子同源，单字号 16px 点阵）
RENDERERS = ("v1", "v3", "c")
_renderer = "v3"
_rd = None


def load_renderer(name):
    """按名字载入 PIL 渲染模块（c 档不需要）。"""
    global _rd
    if name == "v1":
        _rd = importlib.import_module("render")
    elif name == "v3":
        _rd = importlib.import_module("render_v3")
    return _rd


def frame(mode):
    """返回 (bin, png)。按模式分别判断缓存是否过期。"""
    global _state_mtime
    with _lock:
        mt = state_mtime()
        now = time.time()
        hit = _cache.get(mode)
        if hit:
            fresh = False
            if mode == "standby":
                fresh = (now - hit["t"]) < STANDBY_TTL
            else:
                fresh = (mt == _state_mtime and mt is not None)
            if fresh:
                return hit["bin"], hit["png"]

        if _renderer == "c":
            bin_bytes = c_render(mode)
        else:
            bw = _rd.RENDERERS[mode](load_state())
            bin_bytes = _rd.pack(bw)
        png = bin_to_png(bin_bytes)
        _cache[mode] = {"t": now, "bin": bin_bytes, "png": png}
        if mode != "standby":
            _state_mtime = mt
        return bin_bytes, png


def local_ip():
    """本机在局域网里的地址。UDP connect 只做选路，不真发包。"""
    for probe in ("8.8.8.8", "192.168.1.254", "192.168.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 53))
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def discovery_loop(port):
    """UDP 广播服务。

    板子广播 "WHO" -> Mac 单播回 "TODO-SCREEN <ip>:<port>"
    Mac 也每 5 秒主动广播一次，板子随时能捡到（板子重启也不怕）。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        print("[discovery] UDP %d 不可用（%s），板子将退回写死的 IP" % (DISCOVERY_PORT, e),
              flush=True)
        return

    ip = local_ip()
    msg = ("TODO-SCREEN %s:%d" % (ip, port)).encode()
    print("[discovery] 我是 %s:%d，监听 udp/%d" % (ip, port, DISCOVERY_PORT), flush=True)

    last = 0.0
    while True:
        now = time.time()
        if now - last > 5:
            last = now
            for dst in ("255.255.255.255", "127.0.0.1"):
                try:
                    sock.sendto(msg, (dst, DISCOVERY_PORT))
                except OSError:
                    pass
        sock.settimeout(0.5)
        try:
            data, addr = sock.recvfrom(128)
        except (socket.timeout, OSError):
            continue
        if b"WHO" in data:
            try:
                sock.sendto(msg, addr)
                print("[discovery] 回应 %s 的询问" % addr[0], flush=True)
            except OSError:
                pass


def pull_loop(interval):
    """后台定期拉飞书。

    退出码决定怎么兜底 —— 这里最容易出事：
      0  两边都拉到          -> 什么都不用做
      2  部分成功（缺 scope）-> **绝对不能退回 mock**。日程往往有真数据，
                                整体覆盖会把真日程冲成假数据，而且屏上看不出来。
      1  两边都没拉到        -> 退回 mock，且只补缺失的那一路
    """
    pull = os.path.join(HERE, "lark_pull.py")
    mock = os.path.join(HERE, "mock_state.py")
    py = sys.executable
    while True:
        try:
            r = subprocess.run([py, pull], capture_output=True, text=True,
                               timeout=90)
            tail = (r.stdout or "").strip().splitlines()
            last = tail[-1] if tail else ""
            if r.returncode == 0:
                print("[pull] %s" % last, flush=True)
            elif r.returncode == 2:
                print("[pull] 部分未授权，保留真实数据: %s" % last, flush=True)
            else:
                print("[pull] 拉取失败，用 mock 补缺失部分", flush=True)
                subprocess.run([py, mock, "--only-missing"],
                               capture_output=True, timeout=30)
        except Exception as e:
            print("[pull] 异常: %s" % e, flush=True)
        time.sleep(interval)


class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            body = ("ok %d %s" % (len(STATE), time.strftime("%H:%M:%S"))).encode()
            self._send(body, "text/plain")
            return
        if path == "/state.json":
            # 板子直接拉这个（过渡期）。最终形态板子自己调飞书，这个端点就不用了。
            try:
                with open(STATE, "rb") as f:
                    body = f.read()
            except OSError as e:
                self._send(("no state.json: %s" % e).encode(), "text/plain", 500)
                return
            self._send(body, "application/json")
            print("[%s] state.json <- %s (%d B)" % (time.strftime("%H:%M:%S"),
                                                    self.client_address[0], len(body)),
                  flush=True)
            return
        if path == "/":
            cards = "".join(
                '<div><b>%s</b><br><img src="/s/%s.png" width="400"></div>' % (m, m)
                for m in MODES)
            html = ("<html><meta charset=utf-8><meta http-equiv=refresh content=15>"
                    "<body style='background:#222;color:#ccc;font:13px monospace'>"
                    "<h3>todo screen — 三屏预览</h3>" + cards + "</body></html>")
            self._send(html.encode(), "text/html")
            return
        if path.startswith("/s/") and path.endswith(".bin"):
            mode = path[3:-4]
            if mode not in MODES:
                self._send(b"unknown mode", "text/plain", 404)
                return
            try:
                data, _ = frame(mode)
            except Exception as e:
                self._send(("render error: %s" % e).encode(), "text/plain", 500)
                return
            self._send(data, "application/octet-stream")
            print("[%s] %s <- %s (%d B)" % (time.strftime("%H:%M:%S"), mode,
                                            self.client_address[0], len(data)),
                  flush=True)
            return
        if path.startswith("/s/") and path.endswith(".png"):
            mode = path[3:-4]
            if mode not in MODES:
                self._send(b"unknown mode", "text/plain", 404)
                return
            _, png = frame(mode)
            self._send(png, "image/png")
            return
        self._send(b"not found", "text/plain", 404)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--pull-interval", type=int, default=120)
    ap.add_argument("--no-pull", action="store_true", help="不拉飞书，只读 state.json")
    ap.add_argument("--renderer", choices=RENDERERS, default="v3",
                    help="v1/v3 = PIL 渲染器；c = 板子同源的 C 排版引擎")
    a = ap.parse_args()

    global _renderer
    _renderer = a.renderer
    if _renderer != "c":
        load_renderer(_renderer)
        print("renderer: %s (%s)" % (_renderer, _rd.__name__), flush=True)
    else:
        print("renderer: c（板子同源的 C 排版引擎）", flush=True)

    if not os.path.exists(STATE):
        print("没有 state.json，先造一份 mock 的")
        subprocess.run([sys.executable, os.path.join(HERE, "mock_state.py")])

    if ensure_preview_exe():
        print("排版引擎: %s（和板子同源的 C 代码）" % PREVIEW_EXE, flush=True)
    else:
        print("✗ 排版程序编译不了，起不来", flush=True)
        return 1

    if not a.no_pull:
        t = threading.Thread(target=pull_loop, args=(a.pull_interval,), daemon=True)
        t.start()

    d = threading.Thread(target=discovery_loop, args=(a.port,), daemon=True)
    d.start()

    for m in MODES:
        frame(m)          # 预热，顺便把渲染错误暴露在启动时
    print("serving 3 modes on 0.0.0.0:%d  (GET /s/standby.bin | /s/todo.bin | /s/schedule.bin)"
          % a.port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
