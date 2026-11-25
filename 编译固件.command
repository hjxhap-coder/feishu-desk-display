#!/bin/bash
# 编译固件 —— 只编译，不碰板子
# 双击运行，完成后自动打开产物所在目录

DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$HOME/esp-build-v9"          # 构建产物目录，可改

echo "=========================================="
echo " 编译固件  ·  ESP-IDF"
echo "=========================================="
echo "  工程    : $DIR"
echo "  构建目录: $BUILD_DIR"
echo "  ⚠️  构建目录是可丢弃的：idf.py fullclean 会把它整个清空"
echo "      （包括你手动放进去的任何东西）。别在里面存需要留的东西。"
echo

# ESP-IDF 环境（工具链已装好，这一步只是把它们挂到 PATH）
if ! . "$HOME/esp-idf/export.sh" > /tmp/_idf_export.log 2>&1; then
  echo "  ✗ ESP-IDF 环境加载失败："
  grep -iE "^ERROR|not satisfied" /tmp/_idf_export.log | head -5
  read -r -p "按回车关闭..."
  exit 1
fi
if grep -qE "^ERROR|not satisfied" /tmp/_idf_export.log; then
  echo "  ✗ ESP-IDF 环境有报错，先别编："
  grep -iE "^ERROR|not satisfied" /tmp/_idf_export.log | head -5
  echo "     完整日志: /tmp/_idf_export.log"
  read -r -p "按回车关闭..."
  exit 1
fi

echo "  ESP-IDF : $(idf.py --version 2>/dev/null | tail -1)"
echo "  编译器  : $(xtensa-esp32s3-elf-gcc -dumpversion 2>/dev/null)"
echo "  目标    : $(grep -oE '"esp32[a-z0-9]+"' "$DIR/sdkconfig" 2>/dev/null | head -1 | tr -d '\"')"
echo
echo "  编译中（首次可能 3-5 分钟）..."
echo "----------------------------------------"

if idf.py -B "$BUILD_DIR" build; then
  echo
  echo "=========================================="
  echo " ✓ 编译成功"
  echo "=========================================="
  echo "  固件    : $BUILD_DIR/esp32_todo_screen.bin"
  echo "  大小    : $(stat -f%z "$BUILD_DIR/esp32_todo_screen.bin" 2>/dev/null) 字节"
  echo
  echo "  要烧到板子上吗？去运行「编译并烧录.command」"
  open "$BUILD_DIR" 2>/dev/null
else
  echo
  echo "  ✗ 编译失败，看上面的报错。"
fi
echo
read -r -p "按回车键关闭窗口..."
