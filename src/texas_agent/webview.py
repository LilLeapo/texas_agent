"""荷官网页屏: 大字提词 + 实时俯视图 + 解说字幕 + 操作员控制, live_cli 内嵌启动。

- 总线订阅者, 浏览器 0.5s 轮询 /state; 视频复用主循环刚读过的帧
  (LiveVision.last_frame), 不额外碰相机 —— cv2.VideoCapture 不允许两处并发读。
- 操作员确认/两键补录(WebOps)与人工控制(强制通过/重设基线/重新标定)都在页面上,
  终端不再是必需品; 可选浏览器端朗读, 顶替 Linux 上没有的 say。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

from . import cards as C
from .orchestrator import HandRestart

_COLOR = {"none": (128, 128, 128), "back": (255, 128, 0), "face": (0, 220, 0)}

_PAGE = """<!doctype html><meta charset=utf-8><title>荷官屏</title>
<body style="margin:0;background:#0b0f0b;color:#eee;font-family:sans-serif;text-align:center">
<div style="display:flex;justify-content:space-between;padding:6px 12px;color:#888">
  <span id=street></span>
  <span id=toast style="color:#8fd18f"></span>
  <label><input type=checkbox id=tts>🔊 朗读提词</label>
</div>
<div id=prompt style="font-size:6.5vh;font-weight:bold;padding:1.5vh 4vw;min-height:8vh"></div>
<div id=question style="display:none;background:#2a2418;margin:0 4vw 1vh;padding:1.5vh;border-radius:14px">
  <div id=qtext style="font-size:3.6vh;color:#ffd280;margin-bottom:1.2vh"></div>
  <div id=qconfirm style="display:none">
    <button class=big onclick="answer('y')">✓ 确认</button>
    <button class=big onclick="answer('n')">✗ 否</button>
  </div>
  <div id=qcard style="display:none">
    <input id=cardin placeholder="如 As / Th" style="font-size:3.2vh;width:26vw;text-align:center">
    <button class=big onclick="answer(document.getElementById('cardin').value)">提交</button>
  </div>
</div>
<div id=commentary style="color:#8fd18f;font-size:2.8vh;min-height:3.5vh;padding:0 4vw"></div>
<img id=cam src="/stream" style="max-width:96vw;max-height:56vh;margin-top:0.5vh"
     onerror="this.style.display='none'">
<div style="position:fixed;bottom:10px;left:0;right:0">
  <button class=ctl onclick="ctl('pass')">⏭ 强制通过本步</button>
  <button class=ctl onclick="ctl('rebaseline')">🧹 重设区域基线</button>
  <button class=ctl onclick="ctl('recalib')">🎯 重新标定</button>
  <button class=ctl style="border-color:#a44;color:#f99"
    onclick="if(confirm('收牌重码后整手重来, 确定?')) ctl('restart')">🔁 重开本手</button>
</div>
<style>
.big{font-size:3.6vh;padding:1vh 4vw;margin:0 2vw;border-radius:10px;border:0;background:#3a6;color:#fff}
.big:last-child{background:#a44}
.ctl{font-size:2.2vh;padding:0.8vh 2vw;margin:0 1vw;border-radius:8px;border:1px solid #555;background:#222;color:#ccc}
</style>
<script>
let lastSeq = 0, qSeq = 0;
async function tick(){
  try{
    const s = await (await fetch('/state')).json();
    const p = document.getElementById('prompt');
    p.textContent = s.prompt.text;
    p.style.color = {normal:'#fff', again:'#ffb84d', alert:'#ff5555'}[s.prompt.level]||'#fff';
    document.getElementById('commentary').textContent = s.commentary||'';
    document.getElementById('street').textContent = s.street||'';
    const q = s.question, box = document.getElementById('question');
    if(q){
      qSeq = q.seq;
      box.style.display = 'block';
      document.getElementById('qtext').textContent = q.text;
      document.getElementById('qconfirm').style.display = q.kind==='confirm'?'block':'none';
      document.getElementById('qcard').style.display = q.kind==='card'?'block':'none';
    } else { box.style.display = 'none'; }
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
async function ctl(cmd){
  await fetch('/ctl?cmd='+cmd);
  const t = document.getElementById('toast');
  t.textContent = '已发送: '+cmd; setTimeout(()=>t.textContent='', 2500);
}
async function answer(v){
  await fetch('/answer?seq='+qSeq+'&v='+encodeURIComponent(v));
  document.getElementById('cardin').value='';
}
tick();
</script></body>"""


class WebView:
    def __init__(self, bus, vision=None, port: int = 8080):
        self.vision = vision
        self.state = {"prompt": {"text": "等待开局…", "level": "normal", "seq": 0},
                      "commentary": "", "street": "", "question": None}
        self._qseq = 0
        self._answer: tuple[int, str] | None = None
        self.restart_requested = False
        bus.subscribe(self.feed)
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _txt(self, body: str, ctype="text/plain"):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                path, _, query = self.path.partition("?")
                qs = urllib.parse.parse_qs(query)
                if path == "/state":
                    self._txt(json.dumps(view.state, ensure_ascii=False),
                              "application/json")
                elif path == "/ctl":
                    view.ctl(qs.get("cmd", [""])[0])
                    self._txt("ok")
                elif path == "/answer":
                    try:
                        view._answer = (int(qs.get("seq", ["0"])[0]),
                                        qs.get("v", [""])[0])
                    except ValueError:
                        pass
                    self._txt("ok")
                elif path == "/stream":
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
                    self._txt(_PAGE, "text/html")

        self._srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    # ---- 总线 → 页面状态 ----

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

    # ---- 人工控制按钮 → LiveVision 标志位 ----

    def ctl(self, cmd: str) -> None:
        if cmd == "restart":
            self.restart_requested = True
            if self.vision is not None:
                self.vision.abort_hand = True
            return
        v = self.vision
        if v is None:
            return
        if cmd == "pass":
            v.force_pass = True
        elif cmd == "rebaseline":
            v.want_rebaseline = True
        elif cmd == "recalib":
            v.want_recalib = True

    # ---- 操作员问答(阻塞主循环, 这正是操作员环节的语义) ----

    def ask(self, kind: str, text: str) -> str:
        self._qseq += 1
        seq = self._qseq
        self.state["question"] = {"kind": kind, "text": text, "seq": seq}
        self._answer = None
        try:
            while True:
                if self.restart_requested:
                    raise HandRestart
                a = self._answer
                if a and a[0] == seq:
                    return a[1]
                time.sleep(0.2)
        finally:
            self.state["question"] = None

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


class WebOps:
    """操作员台的网页版: 确认弹窗 + 两键补牌都在荷官屏上, 同时镜像到终端。"""

    def __init__(self, view: WebView):
        self.view = view

    def confirm(self, question: str) -> bool:
        print(f"  ❓ {question} (网页上回答)")
        return self.view.ask("confirm", question) in ("y", "yes")

    def ask_card(self, context: str) -> str:
        text = f"补录牌面: {context}"
        while True:
            raw = self.view.ask("card", text)
            try:
                return C.normalize(raw)
            except ValueError:
                text = f"'{raw}' 无效, 重输 (如 As/Th): {context}"
