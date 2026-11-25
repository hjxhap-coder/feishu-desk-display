#!/usr/bin/env python3
"""
抓包服务器 —— 看看板子到底发出了什么。

把板子的 NVS 里的 lark_host 指向这里、lark_tls=0，它就会用明文 HTTP
把请求发过来，这里原样打印请求头和 body。

    python3 echo_server.py --port 8098
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dump(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(n) if n else b''
        print("=" * 60, flush=True)
        print("请求行:", self.requestline, flush=True)
        for k, v in self.headers.items():
            mark = "  <<< 超长!" if len(v) > 600 else ""
            print("  %s: %s%s" % (k, (v[:120] + "…(%d)" % len(v)) if len(v) > 120 else v, mark), flush=True)
        print("-" * 60, flush=True)
        print("body 长度: %d" % len(body), flush=True)
        if body:
            try:
                s = body.decode('utf-8')
            except Exception:
                s = repr(body[:200])
            print("body 前 200 字符: %s" % s[:200], flush=True)
            print("body 后 80 字符 : %s" % s[-80:], flush=True)
            import json
            try:
                j = json.loads(s)
                print("✓ JSON 合法, 字段: %s" % list(j.keys()), flush=True)
                for k, v in j.items():
                    print("    %-14s = %s" % (k, (v[:50] + "…(%d)" % len(v)) if isinstance(v, str) and len(v) > 50 else v), flush=True)
            except Exception as e:
                print("✗ JSON 解析失败: %s" % e, flush=True)
        resp = b'{"code":0,"data":{"access_token":"mock","expires_in":7200,"refresh_token":"mock"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    do_POST = _dump
    do_GET = _dump

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    a = ap.parse_args()
    print("抓包服务器起在 0.0.0.0:%d" % a.port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
