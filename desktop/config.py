#!/usr/bin/env python3
"""配置读取 —— 所有凭据的唯一入口。

优先级（从高到低）：

    1. 环境变量
    2. 本地配置文件 ``config.local.json``（在 .gitignore 里，**不要提交**）
    3. 代码里的默认值（都是空/占位，绝不含真实凭据）

用法::

    export LARK_APP_ID=cli_xxx
    export LARK_APP_SECRET=xxx
    # 或者
    cp config.example.json config.local.json && 编辑它

字段说明见 ``config.example.json``。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_FILE = os.path.join(HERE, "config.local.json")

_local = {}
if os.path.exists(LOCAL_FILE):
    with open(LOCAL_FILE, encoding="utf-8") as f:
        _local = json.load(f)

# config.example.json 只是**模板**，不作为取值来源 ——
# 否则用户没配置时会拿到模板里的占位符，还把它当真值写进板子。
_example = {}
_example_path = os.path.join(HERE, "config.example.json")
if os.path.exists(_example_path):
    with open(_example_path, encoding="utf-8") as f:
        _example = json.load(f)


def _get(key, env, default=None):
    """环境变量 > config.local.json > 代码默认值。"""
    v = os.environ.get(env)
    if v:
        return v
    if key in _local:
        return _local[key]
    return default


# ---- 飞书应用凭据 -----------------------------------------------------------
# app_id / app_secret 在飞书开放平台「开发者后台 -> 凭证与基础信息」拿。
APP_ID = _get("lark_app_id", "LARK_APP_ID", "")
APP_SECRET = _get("lark_app_secret", "LARK_APP_SECRET", "")

# ---- 板子连网 ---------------------------------------------------------------
# 写进板子 NVS 的 WiFi 凭据。只支持 2.4GHz。
WIFI_SSID = _get("wifi_ssid", "WIFI_SSID", "")
WIFI_PASS = _get("wifi_pass", "WIFI_PASS", "")

# 板子找不到飞书时回退到 Mac 的 /state.json —— 填 Mac 在局域网里的 IP。
MAC_IP = _get("mac_ip", "MAC_IP", "")

# ---- 串口 -------------------------------------------------------------------
# 板子用 USB CDC，插拔后端口名会变（usbmodem1101 <-> 11101）。
# 留空 = 自动探测第一个 /dev/cu.usbmodem* 。
SERIAL_PORT = _get("serial_port", "SERIAL_PORT", "")


def need(*names):
    """要求某些字段必须有值，否则给出可操作的报错（而不是让脚本半路崩）。"""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(
            "缺少配置: %s\n"
            "  设置环境变量，或复制 config.example.json 为 config.local.json 后填写。"
            % ", ".join(missing)
        )


def need_wifi():
    """写板子 NVS 前必须确认 WiFi 凭据齐全 —— 
    少了密码就写进去，板子连不上网，而且症状是「连不上」，很难查。"""
    need("WIFI_SSID", "WIFI_PASS")


def resolve_port():
    """串口端口：配置 > 环境变量 > 自动探测。"""
    if SERIAL_PORT:
        return SERIAL_PORT
    import glob
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        raise SystemExit("找不到串口（/dev/cu.usbmodem*）。插好 USB 线再试。")
    return ports[0]
