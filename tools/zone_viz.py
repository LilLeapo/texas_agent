"""D1-2 · 区域可视化叠加: 实时显示标定后的俯视图 + zones.yaml 区域框 + 三态判定。

用法: python tools/zone_viz.py [--cam 0] [--zones config/zones.yaml]
键位: c 重标定 / q 退出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.vision.calib import TableCalibration  # noqa: E402
from texas_agent.vision.stability import StabilityGate  # noqa: E402
from texas_agent.vision.zones import ZoneChecker  # noqa: E402

COLOR = {"none": (128, 128, 128), "back": (255, 128, 0), "face": (0, 220, 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--zones", default="config/zones.yaml")
    args = ap.parse_args()
    calib = TableCalibration(args.zones)
    checker = ZoneChecker(args.zones)
    gate = StabilityGate()
    cap = cv2.VideoCapture(args.cam)
    calibrated = False

    while True:
        ok, frame = cap.read()
        if not ok:
            continue
        if not calibrated:
            calibrated = calib.calibrate(frame)
            cv2.imshow("zones", frame)
            if cv2.waitKey(30) & 0xFF == ord("q"):
                break
            continue
        warped = calib.warp(frame)
        still = gate.feed(warped)
        for name in checker.zones:
            x, y, w, h = checker.zones[name]
            state = checker.tri_state(warped, name) if still else "none"
            cv2.rectangle(warped, (x, y), (x + w, y + h), COLOR[state], 2)
            cv2.putText(warped, f"{name}:{state if still else '...'}", (x, y - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR[state], 1)
        cv2.putText(warped, "STILL" if still else "MOVING", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (0, 220, 0) if still else (0, 0, 255), 2)
        cv2.imshow("zones", warped)
        k = cv2.waitKey(30) & 0xFF
        if k == ord("q"):
            break
        if k == ord("c"):
            calibrated = False
    cap.release()


if __name__ == "__main__":
    main()
