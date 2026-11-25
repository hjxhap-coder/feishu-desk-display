#!/usr/bin/env python3
"""串口采集：把板子的 console 输出持续写进 /tmp/board_serial.log。

三个坑（都踩过）：
  1) **不能用 pyserial 打开端口**。pyserial 在 open() 时会拉 DTR/RTS，
     而 ESP32-S3 的 USB-Serial/JTAG 把这两根线当**复位信号**用
     （esptool 就是靠它复位进 download 模式的）。
     → 一开端口板子就重启（rst:0x15 USB_UART_CHIP_RESET），采集器永远
       只能看到「板子刚重启」，抓不到运行中的日志。
     → 解法：**裸 os.open() + termios**，只设速率，绝不碰 DTR/RTS。
  2) **不能用 readline()**。pyserial 的 readline() 超时抛异常而不是返回空，
     板子安静半秒就抛 → 关端口重连 → 又触发 1)。
  3) 端口名会变（usbmodem1101 <-> 11101），用 glob 自适应；
     有多个时挑最近改动过的那个。
"""
import glob
import os
import sys
import termios
import time

LOG = "/tmp/board_serial.log"
# 命令文件：把命令写进去，采集器就会发到板子。
# 【为什么需要】串口是**独占**的 —— 采集器开着的时候没法另外发命令，
# 而停掉采集器再发命令，板子吐出来的内容没人读，USB 缓冲直接冲掉。
# 走同一个 fd 就两头都有了。
CMD_FILE = "/tmp/board_cmd.txt"


def pick_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        return None
    return max(ports, key=lambda p: os.path.getmtime(p))


def open_port(path):
    """裸开串口：只设波特率，**不动 DTR/RTS**（动了会复位板子）。"""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                       # iflag: 纯 raw
        attrs[1] = 0                       # oflag
        attrs[3] = 0                       # lflag: 不做 canonical/回显
        if hasattr(termios, "B115200"):
            attrs[4] = termios.B115200     # ispeed
            attrs[5] = termios.B115200     # ospeed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception as e:                # noqa: BLE001
        print("termios 设置失败（继续用默认速率）:", e, file=sys.stderr)
    return fd


def _maybe_send_cmd(fd):
    """命令文件里有新内容就发给板子。

    【按 mtime 判断，不按内容】—— 按内容去重的话，
    连发两次同一条命令（"next"）第二次会被当成重复吃掉。
    """
    global _last_cmd_mtime
    try:
        if not os.path.exists(CMD_FILE):
            return
        mt = os.path.getmtime(CMD_FILE)
        if mt == _last_cmd_mtime:
            return
        with open(CMD_FILE, "r", encoding="utf-8") as fh:
            cmd = fh.read().strip()
        if not cmd:
            return
        _last_cmd_mtime = mt
        os.write(fd, (cmd + "\n").encode())
    except OSError:
        pass


_last_cmd_mtime = None


def main():
    f = open(LOG, "ab", buffering=0)
    fd = None
    cur = None
    while True:
        port = pick_port()
        if port is None:
            if fd is not None:
                os.close(fd)
                fd = None
            time.sleep(1)
            continue
        if fd is not None and port == cur:
            # 端口没变，接着读
            # 【顺序很重要】先看命令文件，再读串口。
            # 之前把 _maybe_send_cmd 放在读之后，板子安静时 os.read 抛
            # BlockingIOError 直接 continue —— 命令就永远发不出去。
            # 板子大部分时间是安静的，等于命令通道形同虚设。
            _maybe_send_cmd(fd)
            try:
                # 【一次读 4096 而不是 512】—— 主机读得慢，
                # **设备端的 USB TX FIFO 就会溢出**，而且环形缓冲
                # 溢出时丢的是**最旧的**字节（不是最新的）——
                # 于是收到的是「前面少一截、后面完整」的数据，
                # 看起来像帧错位/乱码，极难定位。
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.005)   # 别睡 50ms，主机读得慢 = 设备端丢数据
                continue
            except OSError:
                chunk = b""
            if chunk:
                # 直接写字节 —— 之前每次 decode+encode，慢到跟不上板子刷屏，
                # 结果关键那几行（WiFi 连接过程）全被 USB 缓冲吞掉。
                f.write(chunk)
                f.flush()
            continue

        # 端口变了（复位后重新枚举）：重开
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        cur = port
        try:
            fd = open_port(port)
        except Exception as e:            # noqa: BLE001
            print("打开 %s 失败: %s" % (port, e), file=sys.stderr)
            time.sleep(1)
            continue
        f.write(("\n=== 连接 %s (%s) ===\n" % (port, time.strftime("%H:%M:%S"))).encode())
        f.flush()


if __name__ == "__main__":
    main()
