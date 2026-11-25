#!/usr/bin/env python3
"""读板子 NVS 分区并解出飞书凭据 —— 比串口日志可靠得多。

为什么需要它：
    板子刷屏时 USB console 的 TX 缓冲会溢出，**大量日志被丢掉**，
    关键那几行（"refresh_token 已轮换并回读校验通过"）经常看不到。
    但**结果**一定在 NVS 里 —— 板子把新 RT 存进去就说明轮换成功了。

串口是**独占**资源：
    采集器（serial_logger.py）一直占着 /dev/cu.usbmodem1101，
    esptool 抢同一个端口会报 "device reports readiness to read but
    returned no data"，而且**板子会被留在下载模式**（boot:0x22）——
    应用不跑了，屏上停住。
    所以本脚本会：先停采集器 -> 读 -> 再把采集器拉起来。

用法：
    python3 nvs_dump.py                 # 读板子（0x9000, 24KB）并解析
    python3 nvs_dump.py --file x.bin    # 解析已有的 dump
    python3 nvs_dump.py --no-restart    # 读完不恢复采集器

输出：每个 token 的 jti / iat / exp，以及 NVS 分区用量。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
from config import resolve_port  # noqa: E402

PY = os.path.expanduser("~/.espressif/python_env/idf5.3_py3.9_env/bin/python3")
NVS_TOOL = os.path.expanduser(
    "~/esp-idf/components/nvs_flash/nvs_partition_tool/nvs_tool.py")
ESSPY = os.path.expanduser("~/.espressif/python_env/idf5.3_py3.9_env/bin/python3")
PORT = resolve_port()   # 串口插拔后端口名会变，自动探测
# NVS 分区现在是 64KB（2026-11-13 从 24KB 扩的）。
# 【坑】只读前 24KB 会**漏掉 token** —— 刷新几轮后 token 会迁到后面的页里，
# 解析出来就是「空的」，看着像凭据没了，其实好好的。必须读满整个分区。
NVS_ADDR, NVS_SIZE = "0x9000", "0x10000"
LOGGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_logger.py")


def port_holders():
    """谁占着串口。返回 (pid, cmd) 列表。"""
    try:
        r = subprocess.run(["lsof", "-t", PORT], capture_output=True, text=True)
    except Exception:
        return []
    out = []
    for pid in r.stdout.split():
        pid = pid.strip()
        if not pid:
            continue
        try:
            ps = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                capture_output=True, text=True).stdout.strip()
        except Exception:
            ps = ""
        out.append((pid, ps))
    return out


def stop_logger():
    """把占着串口的采集器停掉。返回 True 表示确实停了什么。"""
    stopped = False
    for pid, cmd in port_holders():
        if "serial_logger" in cmd:
            print("串口被采集器占着（pid %s），先停掉" % pid)
            subprocess.run(["kill", "-9", pid], capture_output=True)
            stopped = True
    if stopped:
        for _ in range(30):
            if not port_holders():
                break
            time.sleep(0.2)
    return stopped


def start_logger():
    """把采集器拉回来。"""
    d = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    subprocess.Popen(
        [PY, LOGGER],
        cwd=d,
        stdout=open("/tmp/logger.out", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True)
    print("采集器已重新拉起")


def read_flash(out_bin):
    cmd = [ESSPY, "-m", "esptool", "--chip", "esp32s3", "--port", PORT,
           "--baud", "921600", "--before", "default_reset", "--after", "hard_reset",
           "read_flash", NVS_ADDR, NVS_SIZE, out_bin]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    # read_flash 的成功标志是 "Read N bytes at 0x..."，不是 Hash of data verified
    if "bytes at 0x" not in (r.stdout + r.stderr):
        raise SystemExit("读 flash 失败:\n" + (r.stdout + r.stderr)[-600:])
    print("已读 NVS -> %s" % out_bin)


def parse_nvs(path):
    # nvs_tool 在**整个分区**上解析；如果分区里混进了非 NVS 数据
    # （比如分区表改了、旧的 app 二进制还在里面），它会直接抛异常。
    # 那种情况下退回逐页试，只保留能解析出来的部分。
    out = subprocess.run([PY, NVS_TOOL, "-d", "minimal", path],
                         capture_output=True, text=True).stdout
    if "Traceback" in out or not out.strip():
        print("(整个分区解析失败，逐页回退)")
        out = ""
        data = open(path, "rb").read()
        for off in range(0, len(data), 4096):
            tmp = "/tmp/nvs_page_%d.bin" % off
            with open(tmp, "wb") as f:
                f.write(data[off:off + 4096])
            r = subprocess.run([PY, NVS_TOOL, "-d", "minimal", tmp],
                               capture_output=True, text=True).stdout
            if "Traceback" not in r:
                out += r
    vals = {}
    for line in out.splitlines():
        m = re.match(r"\s*lark:(lark_\w+) = b'(.*)'\s*$", line)
        if m:
            vals[m.group(1)] = m.group(2).replace("\\x00", "")
    return vals


def jwt_payload(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return None


def show(name, tok):
    if not tok:
        print("  %-11s 缺失" % name)
        return None
    j = jwt_payload(tok)
    if not j:
        print("  %-11s %d 字符（不是完整 JWT —— 可能被分段截断）" % (name, len(tok)))
        return None
    def ts(k):
        v = j.get(k)
        return time.strftime('%m-%d %H:%M:%S', time.localtime(v)) if v else '-'
    print("  %-11s %d 字符  jti=%-20s iat=%s  exp=%s  at_exp=%s" % (
        name, len(tok), j.get('jti'), ts('iat'), ts('exp'), ts('at_exp')))
    return j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--no-restart", action="store_true",
                    help="读完不恢复采集器")
    a = ap.parse_args()
    path = a.file
    if not path:
        path = "/tmp/nvs_dump_now.bin"
        # 串口是独占的：采集器占着端口时 esptool 抢不到，
        # 报错还长得像「板子死了」（Packet content transfer stopped）。
        # 所以先把它停下来，用完再拉起来。
        was_running = stop_logger()
        try:
            read_flash(path)
        finally:
            if was_running and not a.no_restart:
                start_logger()
    v = parse_nvs(path)
    print("\n=== 飞书凭据（%s）===" % path)
    rt = (v.get("lark_rt1", "") + v.get("lark_rt2", ""))
    at = (v.get("lark_at1", "") + v.get("lark_at2", ""))
    show("refresh_tok", rt)
    show("access_tok", at)
    if v.get("lark_at_exp"):
        t = int(v["lark_at_exp"])
        print("  lark_at_exp  = %d (%s)" % (t, time.strftime('%m-%d %H:%M:%S', time.localtime(t))))
    if rt:
        print("\n  RT 两段长度: %d + %d" % (len(v.get('lark_rt1','')), len(v.get('lark_rt2',''))))


if __name__ == "__main__":
    main()
