"""M2 · 稳定门: 桌垫区域内"变化像素占比"低于阈值持续 hold_s = 静止。

所有区域核验只在静止帧上做 —— 荷官手臂/机械臂在桌上一律不判定,
这一条消化了"人在桌上操作"带来的全部遮挡与误报。

双重比对, 快慢通吃:
- 对上一帧: 抓快速动作(手臂挥过);
- 对 ~hold_s 前的帧: 抓慢速爬行(机械臂逐帧位移小, 但对秒级前的位移藏不住)。

⚠ 喂给 feed() 的应是**标定后的俯视图**(只含桌垫), 而不是原始整幅画面:
原始画面里桌面只占一小半, 动静会被大片静止背景平均稀释掉。
指标用"占比"而非"平均差": 一张牌约占桌垫 1%, 手臂 10%+, 噪声 ~0%。
"""

from __future__ import annotations

import time
from collections import deque

import cv2
import numpy as np


class StabilityGate:
    def __init__(self, pixel_diff: int = 25, ratio_threshold: float = 0.01,
                 hold_s: float = 0.8):
        self.pixel_diff = pixel_diff            # 单像素视为"变了"的灰度差
        self.ratio_threshold = ratio_threshold  # 变化像素占比超过即"在动"
        self.hold_s = hold_s
        self._hist: deque[tuple[float, np.ndarray]] = deque()
        self._still_since: float | None = None

    def _changed_ratio(self, a, b) -> float:
        return float(np.mean(cv2.absdiff(a, b) > self.pixel_diff))

    def feed(self, frame, now: float | None = None) -> bool:
        """喂一帧(建议俯视图), 返回"当前画面已静止满 hold_s"。"""
        now = time.time() if now is None else now
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        # 分辨率变化(标定前后)清空历史
        if self._hist and self._hist[-1][1].shape != gray.shape:
            self._hist.clear()
            self._still_since = None
        prev = self._hist[-1][1] if self._hist else None
        # 取"最近一个至少 hold_s 之前"的帧做慢速比对
        old = None
        for ts, g in reversed(self._hist):
            if ts <= now - self.hold_s:
                old = g
                break
        self._hist.append((now, gray))
        while self._hist and self._hist[0][0] < now - 2 * self.hold_s:
            self._hist.popleft()
        if prev is None or old is None:
            return False  # 历史不足: 尚不能宣布静止
        if self._changed_ratio(gray, prev) > self.ratio_threshold or \
                self._changed_ratio(gray, old) > self.ratio_threshold:
            self._still_since = None
            return False
        if self._still_since is None:
            self._still_since = now
        return (now - self._still_since) >= self.hold_s
