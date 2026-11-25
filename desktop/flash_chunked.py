#!/usr/bin/env python3
"""分块烧录 ESP32 —— 绕开「擦大块 flash 时 USB 断连」的问题。

背景：写 >100KB 的 flash 时，擦除期间芯片无法从 flash 取指，
USB-Serial-JTAG 的固件也跟着停摆 → USB 断连 → esptool 报
"The chip stopped responding"。但 24KB 的 NVS、22KB 的 bootloader
都能写成功 —— 说明阈值在「擦除量」上。

解法：把镜像切成 16KB 一块，逐块 write_flash。每次只擦 16KB，
芯片卡顿时间短到 USB 能扛过去。同次运行内记录已验证的块，失败时补写。
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import resolve_port  # noqa: E402

CH = 16 * 1024


def esptool(port, addr, path):
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port,
           "--baud", "921600", "--before", "default_reset",
           "--after", "hard_reset", "write_flash", addr, path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0 and "Hash of data verified" in (r.stdout + r.stderr)
    except Exception:
        return False


def flash_file(port, src, base, label, workdir):
    total = os.path.getsize(src)
    nblocks = (total + CH - 1) // CH
    os.makedirs(workdir, exist_ok=True)
    data = open(src, "rb").read()
    for i in range(nblocks):
        with open(os.path.join(workdir, "b%04d.bin" % i), "wb") as f:
            f.write(data[i * CH:(i + 1) * CH])

    donefile = os.path.join(workdir, "done")
    done = set()
    if os.path.exists(donefile):
        done = set(int(x) for x in open(donefile).read().split())
    ok = 0
    for i in range(nblocks):
        if i in done:
            ok += 1
            continue
        f = os.path.join(workdir, "b%04d.bin" % i)
        addr = base + i * CH
        for attempt in range(6):
            if esptool(port, hex(addr), f):
                with open(donefile, "a") as df:
                    df.write("%d\n" % i)
                ok += 1
                print("[%s] %s %d/%d ok=%d addr=0x%x" %
                      (time.strftime("%H:%M:%S"), label, i + 1, nblocks, ok, addr),
                      flush=True)
                break
            time.sleep(1)
        else:
            print("[%s] %s %d/%d FAILED addr=0x%x" %
                  (time.strftime("%H:%M:%S"), label, i + 1, nblocks, addr),
                  flush=True)
    print("%s DONE ok=%d/%d" % (label, ok, nblocks), flush=True)
    return ok


def missing(workdir, n):
    done = set()
    p = os.path.join(workdir, "done")
    if os.path.exists(p):
        done = set(int(x) for x in open(p).read().split())
    return [i for i in range(n) if i not in done]


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build_dir = os.path.expanduser(os.environ.get("BUILD_DIR", os.path.join(repo, "build")))
    tasks = [
        (os.path.join(build_dir, "partition_table", "partition-table.bin"), 0x8000, 0x1000, "ptbl"),
        (os.path.join(build_dir, "esp32_todo_screen.bin"), 0x20000, 0x2F0000, "app"),
        (os.path.join(repo, "font", "font16.bin"), 0x310000, 0x100000, "font"),
    ]
    for path, _, limit, label in tasks:
        if not os.path.isfile(path) or not os.path.getsize(path):
            raise SystemExit("缺少 %s 文件：%s" % (label, path))
        if os.path.getsize(path) > limit:
            raise SystemExit("%s 文件超出目标分区：%s" % (label, path))

    port = resolve_port()
    with tempfile.TemporaryDirectory(prefix="esp32-flash-") as work:
        for src, base, _, label in tasks:
            workdir = os.path.join(work, label)
            blocks = (os.path.getsize(src) + CH - 1) // CH
            for attempt in range(12):
                if flash_file(port, src, base, label, workdir) == blocks:
                    break
                print("[%s] 还有缺块，本轮 %d/12" % (label, attempt + 1), flush=True)
            else:
                raise SystemExit("%s 烧录失败，剩余块：%s" % (label, missing(workdir, blocks)))
    print("烧录完成，分区表、固件与字库均已验证。", flush=True)


if __name__ == "__main__":
    main()
