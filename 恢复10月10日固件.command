#!/bin/bash
# 恢复 2026-10-10 的全片备份 —— 刷板救砖用
# 双击运行

BACKUP="$HOME/esp_backup/esp32s3_full_16MB_20261010_214157.bin"
EXPECT=16777216   # 16MB 整片镜像应有的大小

echo "=========================================="
echo " 恢复出厂固件（10 月 10 日全片备份）"
echo "=========================================="

if [ ! -f "$BACKUP" ]; then
  echo "  ✗ 找不到备份文件：$BACKUP"
  read -r -p "按回车关闭..."; exit 1
fi

SIZE=$(stat -f%z "$BACKUP")
if [ "$SIZE" -ne "$EXPECT" ]; then
  echo "  ✗ 备份大小不对：$SIZE 字节（应为 $EXPECT）"
  echo "    备份可能被截断过，别拿它刷板子。"
  read -r -p "按回车关闭..."; exit 1
fi
echo "  备份    : $BACKUP"
echo "  大小    : $SIZE 字节 ✓"
echo

PORT=""
for p in /dev/cu.usbmodem*; do
  [ -e "$p" ] && PORT="$p" && break
done
if [ -z "$PORT" ]; then
  echo "  ✗ 找不到 /dev/cu.usbmodem*，请检查 USB 线。"
  read -r -p "按回车关闭..."; exit 1
fi
echo "  串口    : $PORT"
echo

. "$HOME/esp-idf/export.sh" > /tmp/_idf_export.log 2>&1
if [ $? -ne 0 ]; then
  echo "  ✗ ESP-IDF 环境加载失败"
  read -r -p "按回车关闭..."; exit 1
fi

echo "  ⚠️  这会整片覆盖：bootloader / 分区表 / nvs / 应用"
echo "      板子上的现状会被完全抹掉，回到 10 月 10 日那天。"
echo
read -r -p "  确认恢复？输入 yes 继续，其它任意键取消: " ANS
if [ "$ANS" != "yes" ]; then
  echo; echo "  已取消，板子没动。"
  read -r -p "按回车关闭..."; exit 0
fi

echo
echo "  正在写入 16MB（要一会儿，别拔线）..."
python -m esptool --chip esp32s3 -p "$PORT" -b 460800 \
  --before default_reset --after hard_reset \
  write_flash 0x0 "$BACKUP"

RC=$?
echo
if [ $RC -eq 0 ]; then
  echo "=========================================="
  echo " ✓ 恢复完成，板子已自动重启"
  echo "=========================================="
else
  echo "  ✗ 恢复失败（返回码 $RC）。重新插拔 USB 线再试。"
fi
echo
read -r -p "按回车键关闭窗口..."
