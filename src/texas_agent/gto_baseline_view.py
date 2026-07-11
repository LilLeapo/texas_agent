"""S1b GTO 基线台: 教练/操作员参考屏, live_cli 可选内嵌启动。

来自 Claude Design 项目"牌桌智脑设计规范"里的 S1b GTO 基线台.dc.html: 13×13
起手矩阵(按位置显示基线加注/弃牌频率) + 当前节点卡 + 偏差对照卡 + 消息流,
用同一份 dc-runtime(support.js)原样渲染。真实数据接入(总线 gto_hint/
gto_deviation 消息取代内置剧本、离线求解图表取代占位 Chen 公式近似)是后续
工作。与转播大屏(broadcast_view.py)是两块独立的屏, 端口也分开。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DIR = (Path(__file__).parent / "static" / "gto_baseline").resolve()
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class GtoBaselineView:
    """GTO 基线台: 纯静态文件服务, 不订阅总线。"""

    def __init__(self, port: int = 8082):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                name = "index.html" if self.path in ("/", "") else self.path.lstrip("/")
                path = (_DIR / name).resolve()
                if _DIR not in path.parents or not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type",
                                 _MIME.get(path.suffix, "application/octet-stream"))
                self.end_headers()
                self.wfile.write(path.read_bytes())

        self._srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._srv.shutdown()
