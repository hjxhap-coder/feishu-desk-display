#!/usr/bin/env python3
"""用**裸 os.open** 和板子串口对话（不能用 pyserial）。

为什么不能用 pyserial：
    pyserial 在 open() 时会拉 DTR/RTS，而 ESP32-S3 的 USB-Serial/JTAG
    把这两根线当**复位信号**用 —— 一开端口板子就重启
    （rst:0x15 USB_UART_CHIP_RESET）。
    → 采集器（serial_logger.py）已经踩过这个坑，改用裸 os.open + termios。

用法:
    python3 raw_cmd.py "lark refresh" [超时秒]
    python3 raw_cmd.py --raw "lark refresh"     # 不加换行
"""
import glob
import os
import sys
import termios
import time


def pick_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        return None
    return max(ports, key=os.path.getmtime)


def open_port(path):
    """裸开串口：只设波特率，**不动 DTR/RTS**（动了会复位板子）。"""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def main():
    cmd = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    raw = "--raw" in sys.argv

    port = pick_port()
    if port is None:
        raise SystemExit("没有串口 —— 板子插了吗？")
    fd = open_port(port)
    try:
        time.sleep(0.2)
        try:
            while True:                       # 丢掉积压的旧输出
                if not os.read(fd, 4096):
                    break
        except BlockingIOError:
            pass
        os.write(fd, cmd.encode() if raw else (cmd + "\n").encode())
        t0 = time.time()
        buf = b""
        while time.time() - t0 < timeout:
            try:
                c = os.read(fd, 1024)
                if c:
                    buf += c
            except BlockingIOError:
                pass
            time.sleep(0.05)
    finally:
        os.close(fd)
    out = buf.decode("utf-8", "replace")
    print(out if out else "(板子没回话)")


if __name__ == "__main__":
    main()
