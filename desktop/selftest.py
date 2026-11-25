#!/usr/bin/env python3
"""
In-process check of the Mac side: render -> pack -> HTTP -> unpack.

Starts the real serve_screen handler on an ephemeral loopback port, pulls
/screen.bin from it, then unpacks those bytes with the *exact* bit order the
ESP32 uses (row-major, MSB first, 1 = WHITE) and compares against the rendered
bitmap. Catches the failure that would otherwise only show up as a garbled
panel. No shell, no external network, no files written.

  python3.11 selftest.py
"""
import http.client
import os
import sys
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import json  # noqa: E402
import render_todo as rt  # noqa: E402
import serve_screen as ss  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("%-38s %s%s" % (name, "ok" if cond else "FAIL", ("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


srv = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
c.request("GET", "/health")
r = c.getresponse()
health = r.read().decode()
check("GET /health -> 200", r.status == 200, health)

c.request("GET", "/screen.bin")
r = c.getresponse()
data = r.read()
check("GET /screen.bin -> 200", r.status == 200, "status=%d" % r.status)
check("content-type octet-stream", r.getheader("Content-Type") == "application/octet-stream",
      str(r.getheader("Content-Type")))
check("content-length == 15000", r.getheader("Content-Length") == "15000")
check("body length == 15000", len(data) == 15000, "got %d" % len(data))
check("health agrees on size", health.split()[1:2] == [str(len(data))], health)

c.request("GET", "/screen.png")
r = c.getresponse()
png = r.read()
check("GET /screen.png -> real PNG", r.status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n")

srv.shutdown()

# ---- unpack exactly like the firmware does, compare to the rendered bitmap ----
W, H = rt.W, rt.H
todo = json.load(open(os.path.join(HERE, "todo.json"), encoding="utf-8"))
ref = rt.render(todo)
px = ref.load()

mismatch = 0
ones = 0
for y in range(H):
    for x in range(W):
        bit = (data[y * (W // 8) + (x >> 3)] >> (7 - (x & 7))) & 1
        want = 1 if px[x, y] else 0
        ones += bit
        if bit != want:
            mismatch += 1

check("firmware-order unpack round-trip", mismatch == 0, "%d mismatches / %d px" % (mismatch, W * H))
check("1=WHITE (ink is the minority)", ones > W * H * 0.5, "white %d/%d" % (ones, W * H))

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)