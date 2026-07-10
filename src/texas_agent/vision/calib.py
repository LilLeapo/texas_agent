"""M2 · 顶视标定: 桌垫 ArUco → 全点拟合单应(吃掉广角畸变)。

zones.yaml 里 aruco 段给出每个 marker 在桌垫毫米坐标系的四角位置;
findHomography 用全部角点(RANSAC), 6~8 个分散 marker 足以稳定。
竖屏输出记得旋转(rotate 参数)。
"""

from __future__ import annotations

import cv2
import numpy as np
import yaml


class TableCalibration:
    def __init__(self, zones_yaml: str):
        cfg = yaml.safe_load(open(zones_yaml, encoding="utf-8"))
        self.mat_w, self.mat_h = cfg["mat_size_mm"]
        self.px_per_mm = cfg.get("px_per_mm", 1.0)
        self.rotate = cfg.get("camera_rotate", 0)  # 0/90/180/270
        self.markers = {int(k): np.array(v, dtype=np.float32)
                        for k, v in cfg["aruco"]["markers"].items()}  # id → 4x2 桌垫mm四角
        dict_name = cfg["aruco"].get("dictionary", "DICT_4X4_50")
        self.detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dict_name)))
        self.H: np.ndarray | None = None

    def calibrate(self, frame) -> bool:
        """检测全部 marker, 全点拟合单应。返回是否成功(≥4 marker)。"""
        frame = self._rotate(frame)
        corners, ids, _ = self.detector.detectMarkers(frame)
        if ids is None:
            return False
        src, dst = [], []
        for quad, mid in zip(corners, ids.flatten()):
            if int(mid) in self.markers:
                src.extend(quad.reshape(4, 2))
                dst.extend(self.markers[int(mid)] * self.px_per_mm)
        if len(src) < 16:  # 至少 4 个 marker × 4 角
            return False
        self.H, _ = cv2.findHomography(np.array(src), np.array(dst), cv2.RANSAC, 3.0)
        return self.H is not None

    def warp(self, frame):
        """原始帧 → 桌垫俯视图(像素 = mm × px_per_mm)。"""
        if self.H is None:
            raise RuntimeError("尚未标定")
        size = (int(self.mat_w * self.px_per_mm), int(self.mat_h * self.px_per_mm))
        return cv2.warpPerspective(self._rotate(frame), self.H, size)

    def _rotate(self, frame):
        code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(self.rotate)
        return cv2.rotate(frame, code) if code else frame
