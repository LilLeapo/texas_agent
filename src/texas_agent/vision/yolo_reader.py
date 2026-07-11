"""YOLO 读牌: 预训练 52 类扑克检测, ONNX CPU 推理(~200ms), 认牌链最快一级。

模型 sroot/yolo11s-playing-cards-detector (AGPL-3.0, 基于公开数据集微调),
检测牌角的"点数+花色"角标。失败模式良性: 认不出倾向无检测而非错猜 →
返 uncertain 交 VLM 兜底, 不破坏"宁可不确定"红线。
输入用原始帧(无需对齐), 姿态/尺度鲁棒正是它替代 NCC 的理由。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .. import cards as C

UNCERTAIN = "uncertain"

# data.yaml 顺序, 勿动
CLASSES = [
    "10C", "10D", "10H", "10S", "2C", "2D", "2H", "2S", "3C", "3D", "3H", "3S",
    "4C", "4D", "4H", "4S", "5C", "5D", "5H", "5S", "6C", "6D", "6H", "6S",
    "7C", "7D", "7H", "7S", "8C", "8D", "8H", "8S", "9C", "9D", "9H", "9S",
    "AC", "AD", "AH", "AS", "JC", "JD", "JH", "JS", "KC", "KD", "KH", "KS",
    "QC", "QD", "QH", "QS",
]
DEFAULT_MODEL = "models/cards_yolo11s.onnx"


class YoloCardReader:
    def __init__(self, model_path: str = DEFAULT_MODEL, conf_threshold: float = 0.60,
                 imgsz: int = 640, session=None):
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.imgsz = imgsz
        self._sess = session          # 测试可注入
        self.last_conf = 0.0

    @classmethod
    def if_available(cls, model_path: str = DEFAULT_MODEL) -> "YoloCardReader | None":
        """模型文件与 onnxruntime 都在才返回实例, 否则 None(链路自动跳过)。"""
        if not Path(model_path).exists():
            return None
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return None
        return cls(model_path)

    def _session(self):
        if self._sess is None:
            import onnxruntime
            self._sess = onnxruntime.InferenceSession(
                self.model_path, providers=["CPUExecutionProvider"])
        return self._sess

    def _letterbox(self, img, shrink: float = 1.0):
        h, w = img.shape[:2]
        k = self.imgsz / max(h, w) * shrink
        nh, nw = int(round(h * k)), int(round(w * k))
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, np.uint8)
        y, x = (self.imgsz - nh) // 2, (self.imgsz - nw) // 2
        canvas[y:y + nh, x:x + nw] = cv2.resize(img, (nw, nh))
        return canvas

    def read_image(self, img) -> str:
        """BGR 图 → 牌面 | uncertain。多尺度重试: 训练分布里角标是小目标,
        紧裁牌图直接放大会出域, 缩小重试能救回(实测 0.00 → 0.79)。"""
        if img is None:
            self.last_conf = 0.0
            return UNCERTAIN
        for shrink in (1.0, 0.6, 0.4):
            card = self._read_at(img, shrink)
            if card != UNCERTAIN:
                return card
        return UNCERTAIN

    def detect_all(self, img) -> dict[str, float]:
        """全帧多检: 返回 {牌面: 最高置信}。公共牌横带用 —— 几张明牌并存,
        按类别聚合(一张牌两角标 → 同类两框), 多尺度取并集补小目标域差。"""
        found: dict[str, float] = {}
        if img is None:
            return found
        for shrink in (1.0, 0.6):
            try:
                blob = self._letterbox(img, shrink)[:, :, ::-1].astype(np.float32) / 255.0
                blob = blob.transpose(2, 0, 1)[None]
                out = self._session().run(
                    None, {self._session().get_inputs()[0].name: blob})[0]
                pred = out[0].T if out.ndim == 3 else out.T
                scores = pred[:, 4:]
                best_per_box = scores.max(axis=1)
                keep = best_per_box >= self.conf_threshold
                if not keep.any():
                    continue
                for cid, cf in zip(scores[keep].argmax(axis=1), best_per_box[keep]):
                    card = C.normalize(CLASSES[int(cid)])
                    found[card] = max(found.get(card, 0.0), float(cf))
            except Exception:
                continue
        return found

    def _read_at(self, img, shrink: float) -> str:
        try:
            self.last_conf = 0.0
            blob = self._letterbox(img, shrink)[:, :, ::-1].astype(np.float32) / 255.0
            blob = blob.transpose(2, 0, 1)[None]
            out = self._session().run(None, {self._session().get_inputs()[0].name: blob})[0]
            pred = out[0].T if out.ndim == 3 else out.T   # (8400, 4+52)
            scores = pred[:, 4:]
            best_per_box = scores.max(axis=1)
            keep = best_per_box >= self.conf_threshold
            if not keep.any():
                return UNCERTAIN
            cls_ids = scores[keep].argmax(axis=1)
            confs = best_per_box[keep]
            # 按类别聚合(一张牌两个角标 → 同类两框); 不同类并存 = 多张牌/误检
            by_cls: dict[int, float] = {}
            for cid, cf in zip(cls_ids, confs):
                by_cls[int(cid)] = max(by_cls.get(int(cid), 0.0), float(cf))
            ranked = sorted(by_cls.items(), key=lambda kv: -kv[1])
            if len(ranked) > 1 and ranked[1][1] >= self.conf_threshold:
                return UNCERTAIN            # 两种牌同框, 不猜
            cid, conf = ranked[0]
            self.last_conf = conf
            return C.normalize(CLASSES[cid])
        except Exception:
            return UNCERTAIN
