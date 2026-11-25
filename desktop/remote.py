#!/usr/bin/env python3
"""和板子说话 —— 走 TCP 8099 端口，不用插 USB 线。

    python3 remote.py status                # 扫网段找板子并查状态
    python3 remote.py <ip> status           # 指定 IP
    python3 remote.py <ip> ota http://<mac>:8080/fw.bin   # 无线升级
    python3 remote.py <ip> rt <refresh_token>             # 注入凭据
    python3 remote.py <ip> reboot

板子要和你在同一个 WiFi 下（它连的是你自己的路由器，192.168.0.x 网段）。
"""
import socket
import sys
import time

PORT = 8099


def send(ip, cmd, timeout=20):
    try:
        s = socket.create_connection((ip, PORT), timeout=timeout)
    except OSError as e:
        return None, str(e)
    s.settimeout(timeout)
    s.sendall((cmd + "\n").encode())
    buf = b""
    try:
        while True:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n") and b"\n" not in buf[:-1]:
                break
    except socket.timeout:
        pass
    finally:
        s.close()
    return buf.decode("utf-8", "replace"), None


def scan():
    """在 192.168.0.x 里找开着 8099 的板子。"""
    hits = []
    for i in range(1, 255):
        ip = "192.168.0.%d" % i
        try:
            s = socket.create_connection((ip, PORT), timeout=0.3)
            s.close()
            hits.append(ip)
            print("  找到 %s" % ip)
        except OSError:
            pass
    return hits


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    if args[0] == "scan":
        hits = scan()
        if hits:
            print("板子: %s" % ", ".join(hits))
        else:
            print("没扫到。板子必须和 Mac 在同一个 WiFi 下。")
        return

    ip = args[0]
    cmd = " ".join(args[1:])
    if not cmd:
        cmd = "status"

    print("→ %s: %s" % (ip, cmd if cmd != "status" else "status"))
    out, err = send(ip, cmd)
    if err:
        print("✗ 连不上 %s:%d (%s)" % (ip, PORT, err))
        sys.exit(1)
    print(out.rstrip())


if __name__ == "__main__":
    main()
