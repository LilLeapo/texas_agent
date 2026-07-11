"""S1 转播大屏: 观众席转播大屏前端, live_cli 可选内嵌启动。

来自 Claude Design 项目"牌桌智脑设计规范"里的 S1 转播大屏.dc.html, 用其自带的
dc-runtime(support.js, 单文件 React 运行时; 首次打开从 unpkg CDN 拉 React/
ReactDOM)原样渲染 —— 页面内置的模拟牌局剧情(SCRIPT 数组 + 底部播放控制条)照旧
播放。真实数据接入(总线事件驱动取代内置剧情)是后续工作, 与荷官屏(webview.py)
是两块独立的屏, 端口也分开。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DIR = (Path(__file__).parent / "static" / "broadcast").resolve()
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class BroadcastView:
    """转播大屏: 纯静态文件服务, 不订阅总线。"""

    def __init__(self, port: int = 8081):
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
