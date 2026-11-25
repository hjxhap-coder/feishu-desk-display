#!/usr/bin/env python3
"""
假飞书 API —— 让板子在权限批下来之前就能把整条链路跑通。

板子会打这几个接口（和真飞书一模一样）：
    POST /open-apis/auth/v4/oidc/refresh_access_token
    GET  /open-apis/task/v2/tasks?page_size=50
    GET  /open-apis/calendar/v4/calendars/primary/events/instance_view

用 NVS 覆盖板子的接口地址就能指到这里：
    nvs key "host" = "<Mac 的 IP>"
    nvs key "port" = "8099"
    nvs key "tls"  = "0"

    python3 mock_lark_server.py --port 8099
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_TOKEN = "mock-access-token-abc123"

# 假待办。字段名跟飞书原始 API 一样（summary / completed / priority / due.timestamp）
FAKE_TASKS = [
    {"summary": "整理本周工作周报", "completed": False,
     "priority": "p0", "due": {"timestamp": "1792700000000"}},
    {"summary": "代码评审：固件网络自愈模块", "completed": False,
     "priority": "p0", "due": {"timestamp": "1792786400000"}},
    {"summary": "结算明细按客户任务号口径重算", "completed": False,
     "priority": "p2", "due": {"timestamp": "1792786400000"}},
    {"summary": "回复客户：交付清单与验收口径确认", "completed": False},
    {"summary": "质检通过率差距说明", "completed": False,
     "due": {"timestamp": "1792872800000"}},
    {"summary": "整理 Prompt 可用性待复核清单", "completed": True,
     "priority": "p3"},
    {"summary": "备份项目资产索引", "completed": True, "priority": "p3"},
    {"summary": "更新 wiki 闭环记录", "completed": True, "priority": "p3"},
]


def fake_events():
    """按当前时间造三条日程：一条已过、一条进行中、一条未来。"""
    now = int(time.time())
    base = now - now % 60
    return [
        {"summary": "站会",
         "start_time": {"timestamp": str(base - 7200)},
         "end_time":   {"timestamp": str(base - 5400)},
         "location": "会议室 3A"},
        {"summary": "产品评审",
         "start_time": {"timestamp": str(base - 1800)},
         "end_time":   {"timestamp": str(base + 1800)},
         "location": "线上"},
        {"summary": "项目周会",
         "start_time": {"timestamp": str(base + 5400)},
         "end_time":   {"timestamp": str(base + 9000)},
         "location": "会议室 2B"},
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        if h != "Bearer " + MOCK_TOKEN:
            self._json({"code":99991663, "msg":"invalid access token"}, 200)
            return False
        return True

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        if "refresh_access_token" in self.path:
            self._json({"code":0, "msg":"ok",
                        "data":{"access_token":MOCK_TOKEN,
                                "refresh_token":"mock-refresh",
                                "expires_in":7200,
                                "token_type":"Bearer"}})
            return
        if "tenant_access_token" in self.path:
            self._json({"code":0, "msg":"ok",
                        "tenant_access_token":MOCK_TOKEN, "expire":7200})
            return
        self._json({"code":404, "msg":"no such endpoint"}, 404)

    def do_GET(self):
        if not self._auth_ok():
            return
        if "/task/v2/tasks" in self.path:
            self._json({"code":0, "msg":"ok",
                        "data":{"items":FAKE_TASKS, "has_more":False}})
            return
        if "/events/instance_view" in self.path:
            self._json({"code":0, "msg":"ok",
                        "data":{"items":fake_events()}})
            return
        self._json({"code":404, "msg":"no such endpoint"}, 404)

    def log_message(self, fmt, *a):
        print("[mock-lark] %s" % (fmt % a), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    print("假飞书 API 起在 0.0.0.0:%d" % a.port, flush=True)
    print("  token = %s" % MOCK_TOKEN, flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
