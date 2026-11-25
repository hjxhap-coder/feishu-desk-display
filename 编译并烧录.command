#!/bin/bash
# 编译并烧录 —— 会覆盖板子上唯一的固件副本，动手前会再确认一次
# 双击运行

DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$HOME/esp-build-v9"

echo "=========================================="
echo " 编译并烧录  ·  ESP32-S3"
echo "=========================================="

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

# --- 环境 ---
if ! . "$HOME/esp-idf/export.sh" > /tmp/_idf_export.log 2>&1; then
  echo "  ✗ ESP-IDF 环境加载失败："
  grep -iE "^ERROR|not satisfied" /tmp/_idf_export.log | head -5
  read -r -p "按回车关闭..."; exit 1
fi

echo "  ESP-IDF : $(idf.py --version 2>/dev/null | tail -1)"
echo
echo "  ⚠️  即将把固件写进板子。"
echo "      · 分区表是单槽的，factory 一份就是全部，没有 OTA 备份"
echo "      · 覆盖的就是现在屏幕上跑的那个程序"
echo "      · 写坏了不会变砖（ROM bootloader 在芯片里），但要重刷才能救回来"
echo
echo "  后悔药: ~/esp_backup/ 里有 10 月 10 日的 16MB 全片备份"
echo "          想回退就跑「恢复10月10日固件.command」"
echo

read -r -p "  确认烧录？输入 yes 继续，其它任意键取消: " ANS
if [ "$ANS" != "yes" ]; then
  echo
  echo "  已取消，板子没动。"
  read -r -p "按回车关闭..."; exit 0
fi

echo
echo "  [1/2] 编译..."
if ! idf.py -B "$BUILD_DIR" build; then
  echo "  ✗ 编译失败，没有烧录。板子没动。"
  read -r -p "按回车关闭..."; exit 1
fi

echo
echo "  [2/2] 烧录到 $PORT ..."
if idf.py -B "$BUILD_DIR" -p "$PORT" flash; then
  echo
  echo "=========================================="
  echo " ✓ 烧录完成，板子已自动重启"
  echo "=========================================="
  echo "  想确认板子状态，跑桌面上的「刷新板卡状态.command」"
else
  echo
  echo "  ✗ 烧录失败。板子现在可能在 bootloader 里，"
  echo "    重新插拔一下 USB 线让它自己重启。"
fi
echo
read -r -p "按回车键关闭窗口..."
