#!/usr/bin/env python3
"""把飞书凭据 + WiFi 配置写进板子的 NVS 分区。

注意：这里**整块重写** NVS（0x9000, 64KB）。因为板子的 NVS 里除了我们自己的
数据，还有 WiFi 驱动自己存的东西（nvs.net80211），擦掉后 WiFi 连接时会重新写，
那个过程会短暂阻塞 CPU0 —— 这正是之前触发看门狗死循环的原因之一。
所以写完之后要先让板子停在 bootloader（安静），再启动 app。

【NVS 大小 2026-11-13 从 24KB 扩到 64KB】原来 6 页装不下
PHY 校准 + WiFi 配置 + RT 两段 + AT 两段，2026-11-09 自动刷新时
写 lark_rt2 失败（NO_SPACE），NVS 里只剩半截 RT —— 板子从此拿不到 token。
改大小必须和固件的 partitions.csv 保持一致（否则 nvs_flash_init 会格式化分区）。

用法:
    python3 write_nvs.py [--no-reset]
"""
import csv
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sync_lark_creds as sync  # noqa: E402
from config import (APP_ID, APP_SECRET, MAC_IP, WIFI_PASS, WIFI_SSID,  # noqa: E402
                    need_wifi, resolve_port)

IDF_PY = sys.executable if os.environ.get("IDF_PATH") else os.path.expanduser(
    "~/.espressif/python_env/idf5.3_py3.9_env/bin/python3")
NVS_GEN = os.path.join(os.environ.get("IDF_PATH", os.path.expanduser("~/esp-idf")),
                       "components", "nvs_flash", "nvs_partition_generator", "nvs_partition_gen.py")
NVS_ADDR, NVS_SIZE = "0x9000", "0x10000"


def main():
    """返回 True = 写入成功。"""
    no_reset = "--no-reset" in sys.argv
    # --token-file：从 device flow 的响应 JSON 里取 token
    # （lark-cli 自己的凭据库里可能已经没有 user token 了）
    token_file = None
    for i, a in enumerate(sys.argv):
        if a == "--token-file" and i + 1 < len(sys.argv):
            token_file = sys.argv[i + 1]
    if token_file:
        import json
        with open(token_file, encoding="utf-8") as f:
            t = json.load(f)
        rt = t.get("refresh_token")
        c = {"app_id": t.get("client_id", APP_ID),
             "app_secret": t.get("client_secret", APP_SECRET)}
    else:
        c = sync.read_all()
        t = c
        rt = c.get("refreshToken") or c.get("refresh_token")
    if not rt or len(rt) < 100:
        raise SystemExit("读不到有效的 refresh_token，先跑 lark-cli auth login")
    # WiFi 凭据不全就别写了 —— 写进去板子连不上网，
    # 而症状只是「连不上」，很难联想到是凭据没配。
    need_wifi()
    if not c.get("app_id") or not c.get("app_secret"):
        raise SystemExit("缺少飞书应用凭据，请检查 desktop/config.local.json。")
    if not os.path.isfile(NVS_GEN):
        raise SystemExit("找不到 NVS 生成器，请先加载 ESP-IDF 环境。")
    port = resolve_port()
    with tempfile.TemporaryDirectory(prefix="esp32-nvs-") as workdir:
        return write_nvs(port, c, rt, t, no_reset, workdir)


def write_nvs(port, credentials, rt, tokens, no_reset, workdir):
    csv_path = os.path.join(workdir, "nvs.csv")
    bin_path = os.path.join(workdir, "nvs.bin")
    half = len(rt) // 2
    if len(rt) - half > 3999:
        raise SystemExit("refresh_token 太长，无法写入 NVS。")

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        rows = csv.writer(f)
        rows.writerows([
            ("key", "type", "encoding", "value"),
            ("wifi", "namespace", "", ""),
            ("wifi_ssid", "data", "string", WIFI_SSID),
            ("wifi_pass", "data", "string", WIFI_PASS),
            ("wifi_host", "data", "string", MAC_IP),
            ("lark", "namespace", "", ""),
            ("lark_app_id", "data", "string", credentials["app_id"]),
            ("lark_app_secret", "data", "string", credentials["app_secret"]),
            ("lark_rt1", "data", "string", rt[:half]),
            ("lark_rt2", "data", "string", rt[half:]),
        ])

        at = tokens.get("access_token")
        if at and 100 < len(at) <= 7998:
            half_at = len(at) // 2
            rows.writerows([
                ("lark_at1", "data", "string", at[:half_at]),
                ("lark_at2", "data", "string", at[half_at:]),
                ("lark_at_exp", "data", "string", int(tokens.get("expires_at", 0))),
            ])
        rows.writerows([
            ("lark_host", "data", "string", "open.feishu.cn"),
            ("lark_port", "data", "string", "443"),
            ("lark_tls", "data", "string", "1"),
        ])

    subprocess.run([IDF_PY, NVS_GEN, "generate", csv_path, bin_path, NVS_SIZE],
                   check=True)

    # 【串口是独占的】采集器（serial_logger.py）占着端口时，
    # esptool 抢不到 -> write_flash 失败 -> 「✗ NVS 写入失败」，
    # 而授权服务只看到这一行，很容易误以为是别的问题。
    # （2026-11-24 踩过：授权成功但凭据没进板子，板子继续用死 RT。）
    # 所以写之前自己把采集器停掉，写完再拉起来。
    logger_pid = None
    try:
        r = subprocess.run(["pgrep", "-f", "serial_logger.py"],
                           capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p]
        if pids:
            # 【坑】要杀**全部**匹配 —— `pgrep -f serial_logger.py` 会同时命中
            # bash 包装进程（命令行里也含这个字符串）和真正的 python 进程。
            # 只杀第一个的话，包装死了、python 还在，端口照样被占着。
            logger_pid = pids[0]
            print("串口被采集器占着（pid %s），先停掉" % ",".join(pids))
            for pid in pids:
                subprocess.run(["kill", "-9", pid], capture_output=True)
            # **轮询等端口真正空闲** —— kill 之后 macOS 还要一会儿才释放
            # USB 设备，立刻写会报 "could not open port"（2026-11-24 踩过：
            # 杀了只等 1.5 秒，esptool 仍然打不开端口 -> 写入失败）
            for _ in range(40):
                time.sleep(0.25)
                r2 = subprocess.run(["lsof", port], capture_output=True, text=True)
                if not r2.stdout.strip():
                    print("  串口已释放")
                    break
    except Exception:
        pass

    after = "no_reset" if no_reset else "hard_reset"
    # --before default_reset：让 esptool 自己做 USB 复位进 download 模式。
    # app 把 GPIO0 推挽拉高 hold 了，经典 DTR/RTS 复位进不了 download 模式，
    # 但 ESP32-S3 的 USB-JTAG ROM 复位可以无视 GPIO0。
    cmd = [IDF_PY, "-m", "esptool", "--chip", "esp32s3", "--port", port,
           "--baud", "921600", "--before", "default_reset", "--after", after,
           "write_flash", NVS_ADDR, bin_path]
    ok = False
    for attempt in range(5):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and "Hash of data verified" in (r.stdout + r.stderr):
            print("✓ NVS 写入成功（第 %d 次）" % (attempt + 1))
            ok = True
            break
        time.sleep(2)
    if not ok:
        print("✗ NVS 写入失败")
        print(r.stderr[-400:])

    # 把采集器拉回来
    if logger_pid:
        try:
            subprocess.Popen(
                [IDF_PY, os.path.join(HERE, "serial_logger.py")],
                cwd=HERE, stdout=open("/tmp/logger.out", "ab"),
                stderr=subprocess.STDOUT, start_new_session=True)
            print("采集器已重新拉起")
        except Exception as e:
            print("采集器拉起失败: %s" % e)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
