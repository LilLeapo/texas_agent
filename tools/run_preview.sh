#!/bin/bash
# 布置/调试预览 (raw 取景 / warp 对区域): 浏览器开 http://<SPARK_IP>:8080
# 结束: Ctrl-C (会干净释放相机)。跑牌局前必须先停掉它。
cd "$(dirname "$0")/.."
pkill -f "tools/[c]am_server\.py" 2>/dev/null && sleep 1
exec .venv/bin/python -u tools/cam_server.py
