"""D1-2 · 浏览器实时预览: 架相机/贴码/对区域全程不需要给 Spark 接显示器。

在 Spark 上跑, 笔记本浏览器开 http://<SPARK_IP>:8080 :
- raw  原始画面(架相机取景用)
- warp 标定俯视图 + 区域框三态着色(zone_viz 的浏览器版; 未标定则自动持续尝试)
- /recalib 重标定;  /snapshot 当前帧 JPEG(供 calib_check --image 离线分析)

用法: python tools/cam_server.py [--cam 0] [--port 8080] [--zones config/zones.yaml]
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.vision.calib import TableCalibration  # noqa: E402
from texas_agent.vision.matcher import RANKS, CardMatcher  # noqa: E402

# 模板采集顺序: 13 点数(黑桃) + 3 花色代表 (同 capture_templates.py)
CAPTURE_PLAN = [r + "s" for r in RANKS] + ["Ah", "Ad", "Ac"]
from texas_agent.vision.stability import StabilityGate  # noqa: E402
from texas_agent.vision.zones import ZoneChecker  # noqa: E402

COLOR = {"none": (128, 128, 128), "back": (255, 128, 0), "face": (0, 220, 0)}

PAGE = """<!doctype html><meta charset=utf-8><title>texas cam</title>
<body style="margin:0;background:#111;color:#eee;font-family:sans-serif">
<div style="padding:8px">
  <a href="/?view=raw" style="color:#8cf">raw 顶视</a> ·
  <a href="/?view=warp" style="color:#8cf">warp 俯视+区域</a> ·
  <a href="/?view=card" style="color:#8cf">card 读牌位</a> ·
  <a href="/recalib" style="color:#fc8">重标定</a> ·
  <a href="/capture" style="color:#f9a">采集模板(16张)</a> ·
  <a href="/capture_shot" style="color:#0d5;border:1px solid #0d5;border-radius:6px;padding:2px 12px;font-weight:bold">📸 拍这张</a> ·
  <a href="/capture_back" style="color:#fa0">↩ 重拍上一张</a> ·
  <a href="/capture?stop=1" style="color:#888">停止采集</a> ·
  <a href="/snapshot" style="color:#8f8">存一帧</a>
  <span id=s></span>
</div>
<img src="/stream?view={view}" style="width:100vw">
</body>"""


class Cam:
    """独占相机的采集线程, 各 HTTP 客户端共享最新帧。"""

    def __init__(self, index: int, zones_yaml: str, card_index: int | None = None):
        self.index = index
        self.cap = self._open(index)
        self.calib = TableCalibration(zones_yaml)
        self.checker = ZoneChecker(zones_yaml)
        self.gate = StabilityGate()
        self.matcher = CardMatcher()   # 读牌位: 牌形检测叠加
        from texas_agent.vision.yolo_reader import YoloCardReader
        self.yolo = YoloCardReader.if_available()   # 有模型则实时读数走 YOLO
        self.read_label = "YOLO" if self.yolo else "NCC"
        self.frame = None
        self.still = False
        self.det = (None, None)   # 最近一次 marker 检测 (corners, ids)
        self.card_frame = None    # 读牌相机最新帧
        self.card_aligned = None  # 读牌相机检出的对齐牌形
        self.card_read = None     # 模板就绪后的实时 NCC 读牌 (card, conf)
        self.cap_idx = None       # 模板采集进度; None=未在采集
        self.cap_confirm = False  # "拍这张"按钮按下, 等牌稳定即拍
        self._show_streak = 0
        self.lock = threading.Lock()
        self.want_recalib = True
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()
        if card_index is not None:
            self.card_cap = self._open(card_index)
            threading.Thread(target=self._card_loop, daemon=True).start()
        else:
            self.card_cap = None

    @staticmethod
    def _open(src):
        cap = (cv2.VideoCapture(src, cv2.CAP_V4L2) if isinstance(src, str)
               else cv2.VideoCapture(src))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        return cap

    def _card_loop(self) -> None:
        while not self._stop:
            ok, f = self.card_cap.read()
            if not ok:
                time.sleep(0.2)
                continue
            aligned = self.matcher._align(f)
            read = None
            computed = False
            if self.cap_idx is None:
                if self.yolo is not None:                 # 实时读数: YOLO 整帧直读
                    self._nread = getattr(self, "_nread", 0) + 1
                    if self._nread % 2 == 0:              # 隔帧算, 保预览流畅
                        read = (self.yolo.read_image(f), self.yolo.last_conf)
                        computed = True
                elif aligned is not None and self.matcher.ready:
                    read, computed = self.matcher.read(f), True
            self._capture_step(aligned)
            with self.lock:
                self.card_frame, self.card_aligned = f, aligned
                if computed:
                    self.card_read = read
        self.card_cap.release()

    def _capture_step(self, aligned) -> None:
        """采集: 亮牌摆好 → 网页按"拍这张" → 牌稳定 3 帧即存模板 → 下一张。"""
        if self.cap_idx is None or self.cap_idx >= len(CAPTURE_PLAN):
            return
        self._show_streak = self._show_streak + 1 if aligned is not None else 0
        if not self.cap_confirm or self._show_streak < 3:
            return
        card = CAPTURE_PLAN[self.cap_idx]
        corner = self.matcher._corner(aligned)
        gray = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY)
        h = gray.shape[0]
        Path("templates").mkdir(exist_ok=True)
        cv2.imwrite(f"templates/rank_{card[0]}.png", gray[: h // 2, :])
        cv2.imwrite(f"templates/suit_{card[1]}.png", gray[h // 2:, :])
        print(f"✓ 采集 {card} ({self.cap_idx + 1}/{len(CAPTURE_PLAN)})", flush=True)
        self.cap_idx += 1
        self.cap_confirm = False
        if self.cap_idx >= len(CAPTURE_PLAN):
            self.matcher = CardMatcher()   # 重载模板 → ready, 实时读牌激活

    def stop(self) -> None:
        """优雅停机: 让采集线程自己收尾释放相机 —— 传帧中途被强杀会卡死相机固件。"""
        self._stop = True
        time.sleep(0.6)

    def _loop(self) -> None:
        fails = 0
        self._stop = False
        while not self._stop:
            ok, f = self.cap.read()
            if not ok:
                fails += 1
                if fails >= 2:   # 相机停流(UVC 卡死/休眠): 重开设备
                    print("⚠ 读帧失败, 重开相机", flush=True)
                    self.cap.release()
                    time.sleep(1)
                    self.cap = self._open()
                    fails = 0
                time.sleep(0.2)
                continue
            fails = 0
            # 门只看桌垫俯视图: 原始画面里背景占大头, 手臂动静会被平均稀释
            still = self.gate.feed(self.calib.warp(f)
                                   if self.calib.H is not None else f)
            det = self.calib.detector.detectMarkers(self.calib._rotate(f))[:2]
            if self.want_recalib and self.calib.calibrate(f):
                self.want_recalib = False
            with self.lock:
                self.frame, self.still, self.det = f, still, det
        self.cap.release()

    def jpeg(self, view: str) -> bytes | None:
        with self.lock:
            f = None if self.frame is None else self.frame.copy()
            still = self.still
            corners, ids = self.det
            card_f = None if self.card_frame is None else self.card_frame.copy()
            card_al = self.card_aligned
        if view == "card":   # 读牌位: 原始画面 + 牌形检测状态 + 对齐裁图小窗
            if card_f is None:
                return None
            with self.lock:
                read = self.card_read
            if self.cap_idx is not None:                       # 采集模式提示
                if self.cap_idx >= len(CAPTURE_PLAN):
                    cv2.putText(card_f, "CAPTURE DONE 16/16 - NCC READY", (10, 110),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 220, 0), 4)
                elif self.cap_confirm:
                    cv2.putText(card_f, f"CAPTURING {CAPTURE_PLAN[self.cap_idx]} - hold steady",
                                (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 200, 255), 4)
                else:
                    cv2.putText(card_f,
                                f"SHOW: {CAPTURE_PLAN[self.cap_idx]}  "
                                f"({self.cap_idx + 1}/{len(CAPTURE_PLAN)})  then press [capture]",
                                (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 220, 0), 4)
            elif read is not None:                             # 实时读牌读数
                card, conf = read
                good = card != "uncertain"
                cv2.putText(card_f, f"{self.read_label}: {card} ({conf:.2f})", (10, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                            (0, 220, 0) if good else (0, 160, 255), 5)
            if card_al is not None:
                cv2.putText(card_f, "CARD DETECTED", (10, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 220, 0), 4)
                inset = cv2.resize(card_al, (189, 264))
                card_f[10:274, card_f.shape[1] - 199:card_f.shape[1] - 10] = inset
            else:
                cv2.putText(card_f, "show a card...", (10, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, (128, 128, 128), 4)
            ok, buf = cv2.imencode(".jpg", card_f, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ok else None
        if f is None:
            return None
        if view == "raw":   # 标 marker 检测状态 (snapshot 用 clean 保持无叠加)
            f = self.calib._rotate(f)
            seen = sorted(int(i) for i in ids.flatten()) if ids is not None else []
            if ids is not None:
                cv2.aruco.drawDetectedMarkers(f, corners, ids)
            missing = [i for i in sorted(self.calib.markers) if i not in seen]
            cv2.putText(f, f"seen {seen}", (10, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 220, 0), 3)
            cv2.putText(f, f"missing {missing}" if missing else "ALL 8 OK",
                        (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                        (0, 0, 255) if missing else (0, 220, 0), 3)
        elif view == "warp":
            if self.calib.H is None:
                cv2.putText(f, "NOT CALIBRATED - markers in view?", (30, 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)
            else:
                f = self.calib.warp(f)
                for name, (x, y, w, h) in self.checker.zones.items():
                    st = self.checker.tri_state(f, name) if still else "none"
                    cv2.rectangle(f, (x, y), (x + w, y + h), COLOR[st], 2)
                    cv2.putText(f, f"{name}:{st if still else '...'}", (x, y - 4),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLOR[st], 1)
                cv2.putText(f, "STILL" if still else "MOVING", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 1,
                            (0, 220, 0) if still else (0, 0, 255), 2)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None


CAM: Cam | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静
        pass

    def _view(self) -> str:
        for v in ("warp", "card"):
            if "view=" + v in (self.path or ""):
                return v
        return "raw"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    jpg = CAM.jpeg(self._view())
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg + b"\r\n")
                    time.sleep(1 / 12)   # 12fps 预览, 省 WiFi
            except (BrokenPipeError, ConnectionResetError):
                return
        elif self.path.startswith("/snapshot"):
            jpg = CAM.jpeg("clean")   # 无叠加原图, 供 calib_check --image 分析
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(jpg or b"")
        elif self.path.startswith("/recalib"):
            CAM.want_recalib = True
            self.send_response(302)
            self.send_header("Location", "/?view=warp")
            self.end_headers()
        elif self.path.startswith("/capture_shot"):
            if CAM.cap_idx is not None:
                CAM.cap_confirm = True
            self.send_response(302)
            self.send_header("Location", "/?view=card")
            self.end_headers()
        elif self.path.startswith("/capture_back"):
            if CAM.cap_idx is not None and CAM.cap_idx > 0:
                CAM.cap_idx -= 1
                CAM.cap_confirm = False
            self.send_response(302)
            self.send_header("Location", "/?view=card")
            self.end_headers()
        elif self.path.startswith("/capture"):
            if "stop" in self.path:
                CAM.cap_idx, CAM.cap_confirm = None, False
            else:
                CAM.cap_idx, CAM.cap_confirm = 0, False
            self.send_response(302)
            self.send_header("Location", "/?view=card")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.replace("{view}", self._view()).encode())


def main() -> None:
    global CAM
    def cam_arg(v):
        return int(v) if str(v).isdigit() else v   # 设备号或 /dev/v4l/by-id 路径

    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=cam_arg, default=None,
                    help="顶视相机; 缺省读 config/table.yaml cameras.top_index")
    ap.add_argument("--card-cam", type=cam_arg, default=None,
                    help="读牌位相机; 缺省读 config/table.yaml cameras.card_index")
    ap.add_argument("--config", default="config/table.yaml")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--zones", default="config/zones.yaml")
    args = ap.parse_args()
    cam, card_index = args.cam, args.card_cam
    try:
        import yaml
        cams = yaml.safe_load(open(args.config, encoding="utf-8")).get("cameras", {})
    except Exception:
        cams = {}
    if cam is None:
        cam = cams.get("top_index", 0)
    if card_index is None:
        card_index = cams.get("card_index")
    CAM = Cam(cam, args.zones, card_index=card_index)

    def _bye(sig, frame):
        CAM.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print(f"🎥 http://0.0.0.0:{args.port}  (raw 取景 / warp 对区域 / recalib 重标定)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
