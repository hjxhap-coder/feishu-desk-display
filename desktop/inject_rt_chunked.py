#!/usr/bin/env python3
"""把 refresh_token 注入板子 —— 分块慢速写。

为什么不能一次写完：
    `lark rt <token>` 是一条 4900 字符的命令。USB-Serial/JTAG 的硬件 FIFO
    只有 64 字节，IDF 驱动没有流控 —— 一次灌 4900 字节，设备来不及取，
    **数据直接丢**。症状：板子完全没反应（连「未知命令」都不回）。

做法：32 字节一块，每块之间停 8ms，让设备侧把 FIFO 抽干。

正确性验证不能只看「写成功」的回执 —— NVS 回读校验只能证明
「板子收到的和板子存的一致」，证明不了「板子收到的是我们发的那串」。
所以注入后还要用 `lark rtout` 把板子存的那串读回来做前缀比对。
"""
import json
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import resolve_port  # noqa: E402

PORT = resolve_port()   # 串口插拔后端口名会变，自动探测
CHUNK = 32
GAP = 0.008


def send_line(s, line, echo_wait=6.0):
    """分块写一行命令，返回板子的回话。"""
    data = line.encode()
    s.reset_input_buffer()
    for i in range(0, len(data), CHUNK):
        s.write(data[i:i + CHUNK])
        s.flush()
        time.sleep(GAP)
    s.write(b"\n")
    s.flush()

    buf = b""
    t0 = time.time()
    while time.time() - t0 < echo_wait:
        chunk = s.read(1024)
        if chunk:
            buf += chunk
        else:
            time.sleep(0.05)
    return buf.decode(errors="replace")


def main():
    tok = json.load(open("/tmp/lark_new_tokens.json"))
    rt = tok["refresh_token"]
    print("要注入的 refresh_token: %d 字符" % len(rt))

    s = serial.Serial(PORT, 115200, timeout=0.3)
    time.sleep(0.6)

    # 先探一下通道是不是活的
    probe = send_line(s, "status", echo_wait=4.0)
    ok = "screen" in probe
    print("通道自检: %s" % ("OK" if ok else "**没回话**"))
    if not ok:
        print(probe[-400:])
        s.close()
        return 1

    # 注入
    print("注入中（分块写，约 %.1f 秒）..." % (len(rt) / CHUNK * GAP))
    t0 = time.time()
    out = send_line(s, "lark rt " + rt, echo_wait=12.0)
    print("耗时 %.1f 秒" % (time.time() - t0))
    tail = [l for l in out.splitlines() if l.strip() and "ADDBA" not in l]
    print("---- 板子回话（过滤掉 WiFi 噪音）----")
    for l in tail[-14:]:
        print("   " + l)
    print("---- 判定 ----")
    print("看到回读校验通过 :", "回读校验通过" in out)
    print("看到写入失败     :", "写入失败" in out)

    # 交叉验证：把板子里存的那串读回来，比对
    time.sleep(1.0)
    out2 = send_line(s, "lark rtout", echo_wait=10.0)
    s.close()
    if "RTBEGIN" in out2:
        got = out2.split("RTBEGIN", 1)[1].split("RTEND", 1)[0]
        got = got.strip()
        print("板子存的 RT 长度   : %d" % len(got))
        print("我们要的 RT 长度   : %d" % len(rt))
        print("前缀一致(前40字符) : %s" % (got[:40] == rt[:40]))
        print("完全一致           : %s" % (got == rt))
    else:
        print("rtout 没拿到 RT（可能 NVS 写入时 flash 擦除把 USB 输出吞了）")


if __name__ == "__main__":
    sys.exit(main())
