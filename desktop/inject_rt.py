#!/usr/bin/env python3
"""把新的 refresh_token 灌进板子。

**优先走串口注入**（板子的 console 已经改成 USB-Serial-JTAG，
所以串口命令收得到输入）。串口注入只改 lark_rt1/lark_rt2 两个 key，
不动 WiFi 驱动在同一个 NVS 分区里的数据。

只有串口不可用时才退回「整块重写 NVS」—— 那条路会把 WiFi 自己的
NVS 数据一起擦掉，导致 WiFi 连接时回写 NVS 触发 flash 阻塞，
在 300ms 的中断看门狗下必然超时重启。所以会明确警告。

用法:
    python3 inject_rt.py <refresh_token_file> [--force-nvs]
"""
import os
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import APP_ID, APP_SECRET, resolve_port  # noqa: E402

PORT = resolve_port()   # 串口插拔后端口名会变，自动探测
IDF_PY = os.path.expanduser(
    "~/.espressif/python_env/idf5.3_py3.9_env/bin/python3")
NVS_GEN = os.path.expanduser(
    "~/esp-idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py")


def via_serial(rt):
    """串口注入：只写 lark_rt1/lark_rt2，不碰别的。"""
    s = serial.Serial(PORT, 115200, timeout=0.3, write_timeout=10)
    time.sleep(0.4)
    s.reset_input_buffer()
    cmd = ("lark rt " + rt + "\n").encode()
    print("串口注入 %d 字节 ..." % len(cmd))
    for i in range(0, len(cmd), 64):
        s.write(cmd[i:i + 64])
        s.flush()
        time.sleep(0.02)

    deadline = time.time() + 25
    buf = b""
    while time.time() < deadline:
        chunk = s.read(512)
        if chunk:
            buf += chunk
            txt = buf.decode("utf-8", "replace")
            if "回读校验通过" in txt or "写入失败" in txt:
                break
    s.close()

    txt = buf.decode("utf-8", "replace")
    for line in txt.splitlines():
        if "refresh_token" in line or "写入失败" in line:
            print("  板子: %s" % line.strip())
    return "回读校验通过" in txt


def via_nvs(rt):
    """整块重写 NVS（会破坏 WiFi 的 NVS 数据，慎用）。"""
    import subprocess
    csv_path, bin_path = "/tmp/lark_inj.csv", "/tmp/lark_inj.bin"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("key,type,encoding,value\n")
        f.write("lark,namespace,,\n")
        f.write("lark_app_id,data,string,%s\n" % APP_ID)
        f.write("lark_app_secret,data,string,%s\n" % APP_SECRET)
        f.write("lark_rt1,data,string,%s\n" % rt[:2400])
        f.write("lark_rt2,data,string,%s\n" % rt[2400:])
        f.write("lark_host,data,string,open.feishu.cn\n")
        f.write("lark_port,data,string,443\n")
        f.write("lark_tls,data,string,1\n")

    subprocess.run([IDF_PY, NVS_GEN, "generate", csv_path, bin_path, "0x6000"],
                   check=True)
    esptool = [IDF_PY, "-m", "esptool", "--chip", "esp32s3",
               "--port", PORT, "--baud", "921600"]
    print("⚠ 整块重写 NVS —— 会连 WiFi 的 NVS 数据一起擦掉")
    subprocess.run(esptool + ["erase_region", "0x9000", "0x6000"], check=True)
    subprocess.run(esptool + ["write_flash", "0x9000", bin_path], check=True)
    return True


def main():
    path = sys.argv[1]
    force = "--force-nvs" in sys.argv
    rt = open(path).read().strip()
    if len(rt) < 100:
        raise SystemExit("token 太短，不像真的")

    if not force:
        print("=== 串口注入 ===")
        try:
            if via_serial(rt):
                print("✓ 串口注入成功")
                return
            print("串口注入没得到确认")
        except Exception as e:                    # noqa: BLE001
            print("串口注入失败: %s" % e)

    print("=== 退回整块写 NVS ===")
    if via_nvs(rt):
        print("✓ 已写入（记得重启板子）")


if __name__ == "__main__":
    main()
