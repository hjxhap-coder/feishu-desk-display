#!/bin/bash
# 填 WiFi 凭据 + Mac 地址 —— 写进板子的 NVS，不用重新编译固件
# 双击运行
#
# 原理：esptool 把一个 nvs 分区镜像刷到 0x9000（NVS 分区），
#       板子启动时优先读 NVS，读不到才用 todo_config.h 里的默认值。
#
# ⚠️ ESP32-S3 只支持 2.4GHz WiFi。5GHz 的名字（xxx-5G）板子根本扫不到。

DIR="$(cd "$(dirname "$0")" && pwd)"
IDF="$HOME/esp-idf"
ESPTOOL="$HOME/.espressif/python_env/idf5.3_py3.9_env/bin/esptool.py"
GEN="$IDF/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"

echo "=========================================="
echo " 填 WiFi 凭据  ·  todo screen"
echo "=========================================="
echo

# --- 找串口 ---
PORT=""
for p in /dev/cu.usbmodem*; do
  [ -e "$p" ] && PORT="$p" && break
done
if [ -z "$PORT" ]; then
  echo "  ✗ 找不到 /dev/cu.usbmodem*"
  echo "    请检查 USB 线（有些线只能充电），"
  echo "    并关掉 Termius 等占用串口的软件。"
  read -r -p "按回车关闭..."; exit 1
fi
echo "  串口    : $PORT"
echo

# --- Mac 的 IP ---
IP="$(ipconfig getifaddr en0 2>/dev/null)"
[ -z "$IP" ] && IP="$(ipconfig getifaddr en1 2>/dev/null)"
if [ -n "$IP" ]; then
  echo "  本机 IP : $IP"
  read -r -p "  板子就用这个 IP 吗？(回车=是 / n=手填) " A
  if [ "$A" = "n" ] || [ "$A" = "N" ]; then
    read -r -p "  输入 Mac 的 IP: " IP
  fi
else
  read -r -p "  探测不到 IP，手动输入 Mac 的 IP: " IP
fi
echo

# --- WiFi ---
echo "  ⚠️  只支持 2.4GHz。xxx-5G 这种名字板子扫不到。"
SSID_AUTO="$(networksetup -getairportnetwork en0 2>/dev/null | awk -F': ' 'NF>1{print $NF}')"
if [ -n "$SSID_AUTO" ]; then
  echo "  当前 WiFi: $SSID_AUTO"
  read -r -p "  就用这个 SSID 吗？(回车=是 / 直接输别的) " SSID
  [ -z "$SSID" ] && SSID="$SSID_AUTO"
else
  read -r -p "  WiFi 名称 (SSID): " SSID
fi
read -r -s -p "  WiFi 密码: " PASS
echo
echo

if [ -z "$SSID" ]; then
  echo "  ✗ SSID 不能为空，退出。"
  read -r -p "  按回车关闭..."
  exit 1
fi
if [ -z "$PASS" ]; then
  echo "  ✗ 密码不能为空（公开仓库里不预置任何默认密码）。"
  read -r -p "  按回车关闭..."
  exit 1
fi

echo "  名称: $SSID"
echo "  密码: $(echo "$PASS" | sed 's/././g')  ($(printf '%s' "$PASS" | wc -c | tr -d ' ') 个字符)"
echo "  地址: $IP"
echo

# --- 生成 nvs 镜像 ---
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/wifi.csv" <<CSV
key,type,encoding,value
ssid,data,string,$SSID
pass,data,string,$PASS
host,data,string,$IP
CSV

if ! python3 "$GEN" generate "$TMP/wifi.csv" "$TMP/wifi_nvs.bin" 0x6000; then
  echo "  ✗ 生成 NVS 镜像失败"
  read -r -p "按回车关闭..."; exit 1
fi

# --- 写进板子 ---
echo "  正在写入 NVS (0x9000) ..."
if python3 "$ESPTOOL" --chip esp32s3 --port "$PORT" --baud 460800 \
     write_flash 0x9000 "$TMP/wifi_nvs.bin"; then
  echo
  echo "=========================================="
  echo " ✓ 配网完成"
  echo "=========================================="
  echo "  拔掉再插上 USB 线，板子会用新凭据连网。"
  echo "  串口日志里应该能看到:"
  echo "      wifi ssid=\"$SSID\" (from NVS)"
  echo "      host from NVS: $IP"
  echo "      wifi up, ip x.x.x.x"
  echo
  echo "  注意：写 NVS 不会动固件，屏幕上的程序还是原来那个。"
else
  echo
  echo "  ✗ 写入失败。重新插拔一下 USB 线再试。"
fi
echo
read -r -p "按回车键关闭窗口..."
