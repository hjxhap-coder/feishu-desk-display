#!/usr/bin/env bash
# 把一组 refresh_token 灌进板子，并**验证它真的能自动续期**。
#
# 为什么不能只看日志：板子刷屏时 USB console 的 TX 缓冲会溢出，
# 关键那几行（"refresh_token 已轮换并回读校验通过"）经常看不到。
# 但**结果**一定在 NVS 里 —— 新 RT 写进去了就说明轮换成功。
#
# 用法:
#   ./apply_board_token.py --token-file /tmp/board_token.json
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HOME/.espressif/python_env/idf5.3_py3.9_env/bin/python3"

echo "=== 1/4 灌 NVS ==="
"$PY" "$HERE/write_nvs.py" --token-file "$1"

echo
echo "=== 2/4 灌之前的 RT 指纹 ==="
"$PY" "$HERE/nvs_dump.py" | grep -E "refresh_tok|access_tok" || true

echo
echo "=== 3/4 等板子换 AT 并保存新 RT（45 秒）==="
sleep 45

echo
echo "=== 4/4 再读一次 NVS，看 RT 有没有被轮换掉 ==="
"$PY" "$HERE/nvs_dump.py" | grep -E "refresh_tok|access_tok|lark_at_exp" || true

cat <<'EOF'

------------------------------------------------------------------
判读:
  refresh_tok 的 iat 变了 + access_tok 不再缺失  →  ✓ 轮换成功，板子能无限续期
  refresh_tok 的 iat 没变 + access_tok 缺失      →  ✗ 板子没换到 AT（RT 已作废）
  access_tok 在但 iat 没变                        →  ✗ 换到了 AT 但新 RT 没存住
------------------------------------------------------------------
EOF
