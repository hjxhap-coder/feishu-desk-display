#!/bin/bash
# 启动摆件的数据服务端
# 双击运行。板子靠它拉帧。
#
#  - 后台定期拉飞书（没权限时自动退回 mock 数据）
#  - 待机屏每 15 秒重画（带钟）
#  - caffeinate 保活，防止 Mac 睡眠导致摆件停更

DIR="$(cd "$(dirname "$0")" && pwd)/desktop"
cd "$DIR" || exit 1

echo "=========================================="
echo " 启动摆件服务端  ·  todo screen"
echo "=========================================="
echo "  服务地址 : http://$(ipconfig getifaddr en0 2>/dev/null):8000"
echo "  端点     : /s/standby.bin  /s/todo.bin  /s/schedule.bin"
echo "  预览页   : http://$(ipconfig getifaddr en0 2>/dev/null):8000/"
echo
echo "  ⚠️  这个窗口要开着，关了板子就拉不到帧了。"
echo "     （已挂 caffeinate，Mac 不会自动睡眠）"
echo "=========================================="
echo

# 保活：绑定到本脚本的 pid，脚本退出即失效
caffeinate -i -w $$ &
CAFF_PID=$!
trap 'kill $CAFF_PID 2>/dev/null' EXIT

python3 serve_screen.py --port 8000
