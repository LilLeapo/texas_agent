"""D1-2 · 无头标定自检: SSH 就能跑, 不需要显示器。

抓一帧(或读图片) → 检测 ArUco → 报告认到/缺失的 marker → 拟合单应 →
存两张图: 原始帧(画出检测框) + 标定后俯视图。拿去人眼核对区域对不对位。

用法: python tools/calib_check.py [--cam 0 | --image 照片.jpg]
                                  [--zones config/zones.yaml] [--out /tmp/calib]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.vision.calib import TableCalibration  # noqa: E402
from texas_agent.vision.zones import ZoneChecker  # noqa: E402


def grab(cam: int):
    cap = cv2.VideoCapture(cam)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    frame = None
    t0 = time.time()
    n = 0
    while time.time() - t0 < 5:      # 丢 1s 帧等自动曝光稳定
        ok, f = cap.read()
        if ok:
            frame, n = f, n + 1
            if n > 15:
                break
    cap.release()
    return frame


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--image", help="用照片代替相机")
    ap.add_argument("--zones", default="config/zones.yaml")
    ap.add_argument("--out", default="/tmp/calib")
    args = ap.parse_args()

    frame = cv2.imread(args.image) if args.image else grab(args.cam)
    if frame is None:
        sys.exit("✗ 拿不到画面: 检查相机/图片路径")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    calib = TableCalibration(args.zones)
    rot = calib._rotate(frame)
    corners, ids, _ = calib.detector.detectMarkers(rot)
    seen = sorted(int(i) for i in ids.flatten()) if ids is not None else []
    expected = sorted(calib.markers)
    missing = [i for i in expected if i not in seen]
    extra = [i for i in seen if i not in expected]

    view = rot.copy()
    if ids is not None:
        cv2.aruco.drawDetectedMarkers(view, corners, ids)
    cv2.imwrite(str(out / "detect.jpg"), view)
    print(f"检出 marker: {seen or '无'}")
    if missing:
        print(f"⚠ 缺失: {missing} (被遮挡/太小/反光/出画?)")
    if extra:
        print(f"⚠ 画面里有配置外的 marker: {extra}")

    if calib.calibrate(frame):
        warped = calib.warp(frame)
        checker = ZoneChecker(args.zones)
        for name, (x, y, w, h) in checker.zones.items():
            cv2.rectangle(warped, (x, y), (x + w, y + h), (0, 200, 0), 2)
            cv2.putText(warped, name, (x + 2, y + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
        cv2.imwrite(str(out / "warped.jpg"), warped)
        print(f"✓ 标定成功 ({len(seen)} marker)。俯视图+区域框: {out}/warped.jpg")
        print("  人眼核对: 桌垫四角是否方正、区域框是否落在实际牌位上")
    else:
        print(f"✗ 标定失败 (有效 marker <4)。看 {out}/detect.jpg 排查")
        sys.exit(1)


if __name__ == "__main__":
    main()
