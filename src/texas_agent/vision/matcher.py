"""M3 · 读牌匹配器 —— 一套匹配器, 两个安装位(靴口微距 / 公共牌特写)。

流程: 固定 ROI → Canny+minAreaRect 细对齐 → 裁角标 →
      13 点数模板 NCC + 红黑 HSV 判色 + 4 花色形状 NCC。
置信 < 0.75 报"不确定"(宁可不确定, 不可错判)。
模板由 tools/capture_templates.py 交互采集(52 张约 15 分钟)。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

UNCERTAIN = "uncertain"
RANKS = "23456789TJQKA"
RED_SUITS, BLACK_SUITS = "hd", "sc"


class CardMatcher:
    def __init__(self, template_dir: str = "templates", conf_threshold: float = 0.75,
                 corner_frac=(0.22, 0.30)):
        self.conf_threshold = conf_threshold
        self.corner_frac = corner_frac  # 角标占牌宽/高比例
        self.last_input = None    # 最近一次 read 的原始 ROI (VLM 仲裁取图用)
        self.last_aligned = None  # 最近一次对齐后的牌面裁图
        self.rank_templates: dict[str, np.ndarray] = {}
        self.suit_templates: dict[str, np.ndarray] = {}
        root = Path(template_dir)
        for r in RANKS:
            p = root / f"rank_{r}.png"
            if p.exists():
                self.rank_templates[r] = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        for s in "shdc":
            p = root / f"suit_{s}.png"
            if p.exists():
                self.suit_templates[s] = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)

    @property
    def ready(self) -> bool:
        return len(self.rank_templates) == 13 and len(self.suit_templates) == 4

    def read(self, roi_bgr) -> tuple[str, float]:
        """ROI(含整张牌) → (牌面 | 'uncertain', 置信)。"""
        card = self._align(roi_bgr)
        self.last_input, self.last_aligned = roi_bgr, card
        if card is None:
            return UNCERTAIN, 0.0
        corner = self._corner(card)
        rank, rc = self._match(cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY),
                               self.rank_templates, upper_half=True)
        suit_zone = corner[corner.shape[0] // 2:, :]
        color_red = self._is_red(suit_zone)
        cands = RED_SUITS if color_red else BLACK_SUITS
        suit, sc = self._match(cv2.cvtColor(suit_zone, cv2.COLOR_BGR2GRAY),
                               {k: v for k, v in self.suit_templates.items() if k in cands})
        conf = min(rc, sc)
        if rank is None or suit is None or conf < self.conf_threshold:
            return UNCERTAIN, conf
        return rank + suit, conf

    def double_card_check(self, roi_bgr) -> bool:
        """上帝模式靴口: 双张粘连 = 侧视两条白边。数白色横条数量。"""
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        _, th = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
        row_white = np.mean(th, axis=1) / 255
        bands, inside = 0, False
        for v in row_white:
            if v > 0.6 and not inside:
                bands, inside = bands + 1, True
            elif v < 0.3:
                inside = False
        return bands >= 2

    # ---- 内部 ----

    def _align(self, roi):
        """Canny → 最大轮廓 minAreaRect → 旋转矫正裁出牌面。"""
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < 0.2 * roi.shape[0] * roi.shape[1]:
            return None
        rect = cv2.minAreaRect(c)
        (cx, cy), (w, h), ang = rect
        if w < h:
            w, h, ang = h, w, ang + 90  # 统一横置再转正
        m = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
        rot = cv2.warpAffine(roi, m, (roi.shape[1], roi.shape[0]))
        card = cv2.getRectSubPix(rot, (int(w), int(h)), (cx, cy))
        if card.shape[0] < card.shape[1]:  # 竖置输出
            card = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
        return card

    def _corner(self, card):
        h, w = card.shape[:2]
        return card[: int(h * self.corner_frac[1]), : int(w * self.corner_frac[0])]

    @staticmethod
    def _is_red(zone_bgr) -> bool:
        hsv = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2HSV)
        red = cv2.inRange(hsv, np.array([0, 70, 60]), np.array([10, 255, 255])) \
            | cv2.inRange(hsv, np.array([170, 70, 60]), np.array([180, 255, 255]))
        dark = cv2.inRange(hsv, np.array([0, 0, 0]), np.array([180, 255, 90]))
        return int(np.sum(red)) > int(np.sum(dark))

    @staticmethod
    def _match(gray, templates: dict, upper_half: bool = False):
        if upper_half:
            gray = gray[: gray.shape[0] // 2, :]
        best, best_score = None, -1.0
        for name, t in templates.items():
            if t is None:
                continue
            th, tw = t.shape
            if gray.shape[0] < th or gray.shape[1] < tw:
                scale = min(gray.shape[0] / th, gray.shape[1] / tw)
                t = cv2.resize(t, (max(1, int(tw * scale)), max(1, int(th * scale))))
            score = float(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED).max())
            if score > best_score:
                best, best_score = name, score
        return best, best_score
