"""M2 · 稳定门: 全图帧差低于阈值持续 hold_s = 静止。

所有区域核验只在静止帧上做 —— 荷官手臂扫过一律不判定,
这一条消化了"人在桌上操作"带来的全部遮挡与误报。
"""

from __future__ import annotations

import time

import cv2
import numpy as np


class StabilityGate:
    def __init__(self, diff_threshold: float = 4.0, hold_s: float = 0.8):
        self.diff_threshold = diff_threshold  # 平均绝对帧差 (灰度 0~255)
        self.hold_s = hold_s
        self._prev = None
        self._still_since: float | None = None

    def feed(self, frame, now: float | None = None) -> bool:
        """喂一帧, 返回"当前画面已静止满 hold_s"。"""
        now = time.time() if now is None else now
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self._prev is None:
            self._prev, self._still_since = gray, None
            return False
        diff = float(np.mean(cv2.absdiff(gray, self._prev)))
        self._prev = gray
        if diff > self.diff_threshold:
            self._still_since = None
            return False
        if self._still_since is None:
            self._still_since = now
        return (now - self._still_since) >= self.hold_s
