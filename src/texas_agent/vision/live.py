"""M2+M3 组合: 现场相机适配器, 实现编排器的 vision 协议 (watch / read_card)。

期望驱动感知: watch() 盯住指定区域, 等场景静止后核验存在性变化。
其余区域出现变化 → wrong_zone; 超时 → timeout。
⚠️ 需现场硬件调参: 稳定门阈值 / HSV 范围 / 模板, 见 D2-3。
"""

from __future__ import annotations

import time

import cv2

from ..orchestrator import WatchEvent
from .calib import TableCalibration
from .matcher import UNCERTAIN, CardMatcher
from .stability import StabilityGate
from .zones import ZoneChecker


class LiveVision:
    def __init__(self, zones_yaml: str, top_cam_index: int = 0,
                 card_cam_index: int | None = None, template_dir: str = "templates"):
        self.calib = TableCalibration(zones_yaml)
        self.zones = ZoneChecker(zones_yaml)
        self.gate = StabilityGate()
        self.matcher = CardMatcher(template_dir)
        self.top = cv2.VideoCapture(top_cam_index)
        self.card_cam = cv2.VideoCapture(card_cam_index) if card_cam_index is not None else None
        self._baseline: dict[str, str] = {}

    def start(self) -> None:
        """开场标定 + 记录各区域基线三态。"""
        ok, frame = self.top.read()
        if not ok or not self.calib.calibrate(frame):
            raise RuntimeError("标定失败: 检查 ArUco 是否全部可见")
        warped = self.calib.warp(frame)
        self._baseline = {z: self.zones.tri_state(warped, z) for z in self.zones.zones}

    def watch(self, zones: list[str], expect: str = "appear",
              timeout: float = 20) -> WatchEvent:
        """等待期望区域出现存在性变化(none→back/face)。只在静止帧上核验。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok, frame = self.top.read()
            if not ok:
                time.sleep(0.1)
                continue
            if not self.gate.feed(frame):
                continue  # 手在桌上晃动 → 不判定
            warped = self.calib.warp(frame)
            for z in zones:
                state = self.zones.tri_state(warped, z)
                if state != self._baseline.get(z, "none") and state != "none":
                    self._baseline[z] = state
                    return WatchEvent(ok=True)
            # 期望区域没动静 → 查其它区域(荷官发错槽)
            for z, base in self._baseline.items():
                if z in zones or z in ("MUCK", "DECK"):
                    continue
                state = self.zones.tri_state(warped, z)
                if state != base and state != "none":
                    self._baseline[z] = state
                    return WatchEvent(ok=False, wrong_zone=z)
        return WatchEvent(ok=False, timeout=True)

    def read_card(self, zone: str) -> str:
        """公共牌位特写读牌; 无读牌相机时用顶视 warp 裁区域(精度受限)。"""
        if self.card_cam is not None:
            ok, frame = self.card_cam.read()
            if not ok:
                return UNCERTAIN
            card, _ = self.matcher.read(frame)
            return card
        ok, frame = self.top.read()
        if not ok:
            return UNCERTAIN
        roi = self.zones.crop(self.calib.warp(frame), zone)
        card, _ = self.matcher.read(roi)
        return card

    # ---- 慢循环 VLM 取图口 (VlmCardReader / DealAuditor 用, 主循环不依赖) ----

    def card_image(self, zone: str):
        """仲裁用牌面图: 优先 NCC 刚失败那帧的对齐裁图, 其次原始 ROI, 再现拍。"""
        if self.matcher.last_aligned is not None:
            return self.matcher.last_aligned
        if self.matcher.last_input is not None:
            return self.matcher.last_input
        if self.card_cam is not None:
            ok, frame = self.card_cam.read()
            return frame if ok else None
        ok, frame = self.top.read()
        if not ok:
            return None
        return self.zones.crop(self.calib.warp(frame), zone)

    def still_frame(self, timeout: float = 3.0):
        """审计用顶视静止帧: 等手离开画面; 超时给最后一帧, 拍不到给 None。"""
        deadline = time.time() + timeout
        frame = None
        while time.time() < deadline:
            ok, f = self.top.read()
            if not ok:
                time.sleep(0.05)
                continue
            frame = f
            if self.gate.feed(f):
                return f
        return frame
