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
from texas_agent.vision.stability import StabilityGate  # noqa: E402
from texas_agent.vision.zones import ZoneChecker  # noqa: E402

COLOR = {"none": (128, 128, 128), "back": (255, 128, 0), "face": (0, 220, 0)}

PAGE = """<!doctype html><meta charset=utf-8><title>texas cam</title>
<body style="margin:0;background:#111;color:#eee;font-family:sans-serif">
<div style="padding:8px">
  <a href="/?view=raw" style="color:#8cf">raw 原始</a> ·
  <a href="/?view=warp" style="color:#8cf">warp 俯视+区域</a> ·
  <a href="/recalib" style="color:#fc8">重标定</a> ·
  <a href="/snapshot" style="color:#8f8">存一帧</a>
  <span id=s></span>
</div>
<img src="/stream?view={view}" style="width:100vw">
</body>"""


class Cam:
    """独占相机的采集线程, 各 HTTP 客户端共享最新帧。"""

    def __init__(self, index: int, zones_yaml: str):
        self.index = index
        self.cap = self._open()
        self.calib = TableCalibration(zones_yaml)
        self.checker = ZoneChecker(zones_yaml)
        self.gate = StabilityGate()
        self.frame = None
        self.still = False
        self.det = (None, None)   # 最近一次 marker 检测 (corners, ids)
        self.lock = threading.Lock()
        self.want_recalib = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        cap = cv2.VideoCapture(self.index)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        return cap

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
            still = self.gate.feed(f)
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
        return "warp" if "view=warp" in (self.path or "") else "raw"

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
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.replace("{view}", self._view()).encode())


def main() -> None:
    global CAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--zones", default="config/zones.yaml")
    args = ap.parse_args()
    CAM = Cam(args.cam, args.zones)

    def _bye(sig, frame):
        CAM.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print(f"🎥 http://0.0.0.0:{args.port}  (raw 取景 / warp 对区域 / recalib 重标定)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
