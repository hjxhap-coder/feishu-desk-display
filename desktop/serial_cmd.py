#!/usr/bin/env python3
"""和板子串口对话：发一条命令，等它的回话。

用法:
    python3 serial_cmd.py "lark rtout" [超时秒]
"""
import signal
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import resolve_port  # noqa: E402

PORT = resolve_port()   # 串口插拔后端口名会变，自动探测


class Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout()


def main():
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)

    s = serial.Serial(PORT, 115200, timeout=0.3)
    time.sleep(0.4)
    s.reset_input_buffer()
    s.write((cmd + "\n").encode())
    s.flush()

    buf = b""
    try:
        while True:
            chunk = s.read(1024)
            if chunk:
                buf += chunk
    except Timeout:
        pass
    finally:
        signal.alarm(0)
        s.close()

    out = buf.decode("utf-8", "replace")
    print(out)
    if "RTBEGIN" in out:
        rt = out.split("RTBEGIN", 1)[1].split("RTEND", 1)[0].strip()
        open("/tmp/cur_rt.txt", "w").write(rt)
        print("=== 拿到 rt，长度 %d，已存 /tmp/cur_rt.txt ===" % len(rt))


if __name__ == "__main__":
    main()
