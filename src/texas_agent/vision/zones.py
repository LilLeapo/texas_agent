"""M2 · 区域三态核验: 无牌 / 背面 / 正面。

只回答封闭问题"期望的那件事发生了吗", 在稳定帧上:
- 牌背存在 = 牌背模板 NCC > 0.7 或 背色 HSV 占比阈值
- 牌面存在 = 白底占比 + 矩形轮廓
"""

from __future__ import annotations

import cv2
import numpy as np
import yaml

NONE, BACK, FACE = "none", "back", "face"


class ZoneChecker:
    def __init__(self, zones_yaml: str, back_template=None):
        cfg = yaml.safe_load(open(zones_yaml, encoding="utf-8"))
        self.px = cfg.get("px_per_mm", 1.0)
        self.zones = {name: [int(v * self.px) for v in rect]
                      for name, rect in cfg["zones"].items()}  # name → [x,y,w,h] px
        hsv = cfg.get("back_hsv", {"low": [100, 60, 40], "high": [130, 255, 255]})
        self.back_low, self.back_high = np.array(hsv["low"]), np.array(hsv["high"])
        self.back_template = back_template  # 灰度模板, D1-2 采集
        self.back_ratio_th = cfg.get("back_ratio_threshold", 0.35)
        self.white_ratio_th = cfg.get("white_ratio_threshold", 0.40)

    def crop(self, warped, name: str):
        x, y, w, h = self.zones[name]
        return warped[y:y + h, x:x + w]

    def tri_state(self, warped, name: str) -> str:
        roi = self.crop(warped, name)
        if roi.size == 0:
            return NONE
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        back_ratio = float(np.mean(cv2.inRange(hsv, self.back_low, self.back_high))) / 255
        if self.back_template is not None:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            t = cv2.resize(self.back_template, (gray.shape[1], gray.shape[0]))
            ncc = float(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED).max())
            if ncc > 0.7:
                return BACK
        if back_ratio > self.back_ratio_th:
            return BACK
        # 正面: 白底占比 + 矩形轮廓
        white = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 60, 255]))
        white_ratio = float(np.mean(white)) / 255
        if white_ratio > self.white_ratio_th and self._has_card_rect(white, roi.shape):
            return FACE
        return NONE

    @staticmethod
    def _has_card_rect(mask, shape) -> bool:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = shape[0] * shape[1]
        return any(cv2.contourArea(c) > 0.25 * area for c in contours)
