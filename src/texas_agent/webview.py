"""荷官网页屏: 大字提词 + 实时俯视图 + 解说字幕, 由 live_cli 内嵌启动。

总线订阅者, 浏览器每 0.5s 轮询 /state; 视频复用主循环刚读过的帧
(LiveVision.last_frame), 不额外碰相机 —— cv2.VideoCapture 不允许两处并发读。
可选浏览器端朗读(页面右上角开关), 顶替 Linux 上没有的 say。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

_COLOR = {"none": (128, 128, 128), "back": (255, 128, 0), "face": (0, 220, 0)}

_PAGE = """<!doctype html><meta charset=utf-8><title>荷官屏</title>
<body style="margin:0;background:#0b0f0b;color:#eee;font-family:sans-serif;text-align:center">
<div style="display:flex;justify-content:space-between;padding:6px 12px;color:#888">
  <span id=street></span>
  <label><input type=checkbox id=tts>🔊 朗读提词</label>
</div>
<div id=prompt style="font-size:7vh;font-weight:bold;padding:2vh 4vw;min-height:9vh"></div>
<div id=commentary style="color:#8fd18f;font-size:3vh;min-height:4vh;padding:0 4vw"></div>
<img id=cam src="/stream" style="max-width:96vw;max-height:62vh;margin-top:1vh"
     onerror="this.style.display='none'">
<script>
let lastSeq = 0;
async function tick(){
  try{
    const s = await (await fetch('/state')).json();
    const p = document.getElementById('prompt');
    p.textContent = s.prompt.text;
    p.style.color = {normal:'#fff', again:'#ffb84d', alert:'#ff5555'}[s.prompt.level]||'#fff';
    document.getElementById('commentary').textContent = s.commentary||'';
    document.getElementById('street').textContent = s.street||'';
    if(s.prompt.seq !== lastSeq){
      lastSeq = s.prompt.seq;
      if(document.getElementById('tts').checked && s.prompt.text){
        speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(s.prompt.text);
        u.lang = 'zh-CN'; u.rate = 1.15;
        speechSynthesis.speak(u);
      }
    }
  }catch(e){}
  setTimeout(tick, 500);
}
tick();
</script></body>"""


class WebView:
    def __init__(self, bus, vision=None, port: int = 8080):
        self.vision = vision
        self.port = port
        self.state = {"prompt": {"text": "等待开局…", "level": "normal", "seq": 0},
                      "commentary": "", "street": ""}
        bus.subscribe(self.feed)
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/state"):
                    body = json.dumps(view.state, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    try:
                        while True:
                            jpg = view.jpeg()
                            if jpg:
                                self.wfile.write(
                                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                                self.wfile.write(jpg + b"\r\n")
                            time.sleep(1 / 8)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_PAGE.encode())

        self._srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def feed(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "dealer_prompt":
            self.state["prompt"] = {"text": msg["text"], "level": msg["level"],
                                    "seq": msg.get("seq", time.time())}
        elif t == "alert":
            self.state["prompt"] = {"text": "⚠ " + msg["text"], "level": "alert",
                                    "seq": msg.get("seq", time.time())}
        elif t == "commentary":
            self.state["commentary"] = msg["text"]
        elif t == "game_event":
            st = msg.get("state", {})
            self.state["street"] = (f"街[{st.get('street', '')}] "
                                    f"池[{st.get('pot', '')}]")

    def jpeg(self) -> bytes | None:
        """主循环最近一帧 → 俯视图+区域三态框。无相机/无帧返 None。"""
        v = self.vision
        frame = getattr(v, "last_frame", None) if v else None
        if frame is None or v.calib.H is None:
            return None
        try:
            warped = v.calib.warp(frame)
            for name, (x, y, w, h) in v.zones.zones.items():
                st = v.zones.tri_state(warped, name)
                cv2.rectangle(warped, (x, y), (x + w, y + h), _COLOR[st], 2)
                cv2.putText(warped, f"{name}:{st}", (x, y - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, _COLOR[st], 1)
            ok, buf = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ok else None
        except Exception:
            return None

    def close(self) -> None:
        self._srv.shutdown()
