#!/bin/bash
# 现场跑牌局: 自动释放预览占用的相机; 网页"🔁 重开本手"按钮会自动重启新一手。
# 结束: 终端 Ctrl-C 一次, 等它自己退出(会干净释放相机)。千万别 kill -9。
# 追加参数透传给 live_cli, 如: bash tools/run_live.sh --public
cd "$(dirname "$0")/.."
pkill -f "tools/[c]am_server\.py" 2>/dev/null && sleep 1
while :; do
  PYTHONUNBUFFERED=1 .venv/bin/python -m texas_agent.live_cli \
    --deck config/deck_order.txt \
    --script "c c c k / k k k k / k k k k / k k k k" \
    --session-tag live "$@"
  code=$?
  [ "$code" != "9" ] && break
  echo "=== 🔁 重开本手 ==="
  sleep 2
done
echo "已退出, 相机已释放"
