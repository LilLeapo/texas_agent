"""M2+M3 组合: 现场相机适配器, 实现编排器的 vision 协议 (watch / read_card)。

期望驱动感知: watch() 盯住指定区域, 等场景静止后核验存在性变化。
其余区域出现变化 → wrong_zone; 超时 → timeout。
⚠️ 需现场硬件调参: 稳定门阈值 / HSV 范围 / 模板, 见 D2-3。
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from ..orchestrator import HandRestart, WatchEvent
from .calib import TableCalibration
from .matcher import UNCERTAIN, CardMatcher
from .stability import StabilityGate
from .zones import ZoneChecker


class LiveVision:
    def __init__(self, zones_yaml: str, top_cam_index=0,
                 card_cam_index=None, template_dir: str = "templates", yolo=None):
        self.calib = TableCalibration(zones_yaml)
        self.zones = ZoneChecker(zones_yaml)
        self.gate = StabilityGate()
        self.matcher = CardMatcher(template_dir)
        self.yolo = yolo   # YoloCardReader: 读牌第一级(毫秒), 可缺席
        self.top = self._open(top_cam_index)
        self.card_cam = self._open(card_cam_index) if card_cam_index is not None else None
        self._baseline: dict[str, str] = {}
        self.last_frame = None  # 主循环最近一帧, 供网页荷官屏复用(勿并发读相机)
        # 网页操控按钮置位, watch 循环消费
        self.force_pass = False       # 强制通过当前核验
        self.want_rebaseline = False  # 手动整理过桌面后重记各区域基线
        self.want_recalib = False     # 相机被碰过后重新标定
        self.abort_hand = False       # 重开本手(抛 HandRestart)

    @staticmethod
    def _open(src):
        """src: 设备号 int 或路径 str (推荐 /dev/v4l/by-id/*, 不随重枚举漂移)。"""
        cap = (cv2.VideoCapture(src, cv2.CAP_V4L2) if isinstance(src, str)
               else cv2.VideoCapture(src))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        return cap

    def close(self) -> None:
        """释放相机。传帧中被强杀会卡死相机固件, 退出前务必走这里。"""
        self.top.release()
        if self.card_cam is not None:
            self.card_cam.release()

    def start(self, timeout: float = 8.0) -> None:
        """开场标定 + 记录各区域基线三态。多帧重试(首帧曝光未稳)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok, frame = self.top.read()
            if ok:
                self.last_frame = frame
            if ok and self.calib.calibrate(frame):
                warped = self.calib.warp(frame)
                self._baseline = {z: self.zones.tri_state(warped, z)
                                  for z in self.zones.zones}
                return
        raise RuntimeError("标定失败: 检查 ArUco 是否全部可见")

    def watch(self, zones: list[str], expect: str = "appear",
              timeout: float = 20) -> WatchEvent:
        """等待期望区域出现存在性变化。只在静止帧上核验, 且**每次核验开始时
        以首个静止帧重记全部基线** —— 手臂悬停遮挡/挪牌造成的历史漂移一步清零,
        不会把"遮挡后重现"误报成发错槽。"""
        deadline = time.time() + timeout
        fresh = False
        while time.time() < deadline:
            if self.abort_hand:
                self.abort_hand = False
                raise HandRestart
            if self.force_pass:   # 网页"强制通过": 操作员说算过就算过
                self.force_pass = False
                return WatchEvent(ok=True)
            ok, frame = self.top.read()
            if not ok:
                time.sleep(0.1)
                continue
            self.last_frame = frame
            if self.want_recalib:
                if self.calib.calibrate(frame):
                    self.want_recalib = False
            if self.want_rebaseline:   # 网页按钮: 立即重记基线并重新等静止
                self.want_rebaseline = False
                fresh = False
            warped = self.calib.warp(frame)
            if not self.gate.feed(warped):
                continue  # 手在桌垫上方晃动 → 不判定 (门只看桌垫, 不看背景)
            if not fresh:
                self._baseline = {z: self.zones.tri_state(warped, z)
                                  for z in self.zones.zones}
                fresh = True
                if expect == "appear" and zones and any(
                        self._baseline.get(z, "none") != "none" for z in zones):
                    # 目标区已有牌(烧牌堆二叠 / 荷官快手先放了): 视为已达成
                    return WatchEvent(ok=True)
                continue
            for z in zones:
                state = self.zones.tri_state(warped, z)
                if state != self._baseline.get(z, "none") and state != "none":
                    self._baseline[z] = state
                    return WatchEvent(ok=True)
            # 期望区域没动静 → 只有"开局时是空格、现在冒出牌"才算发错槽;
            # 已占用格的任何波动(遮挡/挪动/翻面)一律不报警, 下次核验自动归零
            for z, base in self._baseline.items():
                if z in zones or z in ("MUCK", "DECK"):
                    continue
                state = self.zones.tri_state(warped, z)
                if base == "none" and state != "none":
                    self._baseline[z] = state
                    return WatchEvent(ok=False, wrong_zone=z)
        return WatchEvent(ok=False, timeout=True)

    def read_card(self, zone: str) -> str:
        """读牌链第一二级: YOLO(毫秒, 姿态鲁棒) → NCC 模板(如已采集)。
        仍不确定由编排器兜底链继续(VLM 仲裁 → 操作员)。"""
        if self.yolo is not None:
            img = self.card_image(zone)   # 高清裁图(读牌相机或顶视逆单应)
            card = self.yolo.read_image(img)
            if card != UNCERTAIN:
                return card
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

    def card_image(self, zone: str, scale: int = 4):
        """仲裁用牌面图: 优先 NCC 刚失败那帧的对齐裁图, 其次读牌相机,
        最后从顶视原始帧按逆单应裁该区域(保留全部光学分辨率, 不经过 1px/mm 降采样)。"""
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
        return self.zone_image(zone, scale=scale, frame=frame)

    def zone_image(self, zone: str, scale: int = 4, frame=None):
        """顶视帧按逆单应裁高清牌区(YOLO 公共牌核验用)。

        frame 缺省取主循环最新帧(last_frame) —— 审计等后台线程禁止直接读相机
        (cv2.VideoCapture 不允许并发读), 走这里是线程安全的。"""
        if frame is None:
            frame = self.last_frame
        if frame is None or self.calib.H is None:
            return None
        x, y, w, h = self.zones.zones[zone]
        pad = 8  # 牌可能没摆正在框中央: 裁大一圈, 交给 _align 精确抠出主牌
        x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
        quad = np.float32([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
        src = cv2.perspectiveTransform(quad.reshape(1, 4, 2).astype(np.float64),
                                       np.linalg.inv(self.calib.H)).reshape(4, 2)
        dst = np.float32([[0, 0], [w * scale, 0], [w * scale, h * scale], [0, h * scale]])
        m = cv2.getPerspectiveTransform(src.astype(np.float32), dst)
        patch = cv2.warpPerspective(self.calib._rotate(frame), m,
                                    (w * scale, h * scale))
        card = self.matcher._align(patch)   # 抠出最大牌形, 剔掉混入的邻牌角
        return card if card is not None else patch

    def still_frame(self, timeout: float = 3.0):
        """审计用顶视静止帧: 等手离开画面; 超时给最后一帧, 拍不到给 None。"""
        deadline = time.time() + timeout
        frame = None
        while time.time() < deadline:
            ok, f = self.top.read()
            if not ok:
                time.sleep(0.05)
                continue
            frame = self.last_frame = f
            if self.gate.feed(self.calib.warp(f) if self.calib.H is not None else f):
                return f
        return frame
