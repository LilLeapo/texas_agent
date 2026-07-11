"""靴口读牌 · 上帝模式 god_source: shoe 的无机械臂过渡形态。

机械臂就位前由人代劳: 每张牌发出前, 系统提示"亮牌"→ 荷官把牌面对准
读牌相机(机械臂位下方) → VLM 高清认牌(真牌已验 0 错判) → 荷官再按提词发牌。
引擎因此拿到**真实牌面**, 胜率全部按真数据计算, 牌堆无需预排。

纪律:
- 烧牌不亮(分析纪律: 烧牌对信息集永远未知);
- 重复认出已发过的牌 = 误读或重复亮牌 → 要求重亮;
- 不确定/超时 → 网页补录兜底, 永不卡死;
- 机械臂就位后, 本类换成"机械臂过牌位自动亮牌", 接口不变。
"""

from __future__ import annotations

import time

from .. import cards as C
from ..vlm import UNCERTAIN, VlmCardReader
from .matcher import CardMatcher


class ShoeGod:
    def __init__(self, bus, prompter, ops, client, card_cam=None,
                 show_timeout_s: float = 30.0, grab=None, fast_reader=None,
                 arm=None):
        self.arm = arm                    # ArmClient: 有臂则取牌/亮牌由臂执行
        self.bus = bus
        self.prompter = prompter
        self.ops = ops
        self.card_cam = card_cam          # cv2.VideoCapture (LiveVision.card_cam)
        self.show_timeout_s = show_timeout_s
        self._grab_override = grab        # 测试注入
        self._matcher = CardMatcher()     # 仅用 _align 做"有牌出现"检测
        self.reader = VlmCardReader(client, image_source=lambda z: None)
        self.fast = fast_reader           # YoloCardReader, 毫秒级第一级, 可缺席
        self.verify_below = 0.85          # YOLO 置信低于此值时要求 VLM 复核一致
        self.dealt: set[str] = set()
        self.last_via: str | None = None  # 本张牌的认定来源, 编排器写进 deal.source

    # ---- 相机侧 ----

    def _try_read(self):
        """在超时窗内持续尝试识别, 返回 (card, via) 或 (uncertain, None)。

        不要求牌完整/对齐 —— YOLO 认的是角标, 切边、手持、只露一角都行:
        每帧直接喂整帧给 YOLO; YOLO 一直没认出且画面里有对齐牌形时, 约 4s
        一次请 VLM(整帧也可读)。"""
        if self._grab_override is not None:               # 测试注入: 单发旧语义
            g = self._grab_override()
            frame, aligned = g if isinstance(g, tuple) else (g, g)
            return self._read_pair(frame, aligned)
        if self.card_cam is None:
            return UNCERTAIN, None
        deadline = time.time() + self.show_timeout_s
        next_vlm = 0.0
        while time.time() < deadline:
            ok, frame = self.card_cam.read()
            if not ok:
                time.sleep(0.1)
                continue
            if self.fast is not None:
                card = self.fast.read_image(frame)        # 整帧, 角标即可
                if card not in (None, UNCERTAIN):
                    if self.fast.last_conf >= self.verify_below:
                        return card, "yolo"
                    v = self.reader.read_image(frame)     # 低置信: VLM 复核
                    if v == card:
                        return card, "yolo+vlm"
                    next_vlm = time.time() + 4            # 分歧: 不猜, 继续观察
                    continue
            if time.time() >= next_vlm:                   # YOLO 没认出: 定期 VLM
                aligned = self._matcher._align(frame)
                v = self.reader.read_image(aligned if aligned is not None else frame)
                if v not in (None, UNCERTAIN):
                    return v, "vlm"
                next_vlm = time.time() + 4
        return UNCERTAIN, None

    def _read_pair(self, frame, aligned):
        """单发识别(测试路径): YOLO → 低置信复核 → VLM。"""
        card, via = UNCERTAIN, None
        if frame is not None:
            if self.fast is not None:
                card, via = self.fast.read_image(frame), "yolo"
                if card not in (None, UNCERTAIN) and \
                        self.fast.last_conf < self.verify_below:
                    v = self.reader.read_image(aligned)
                    if v == card:
                        via = "yolo+vlm"
                    else:
                        card = UNCERTAIN
            if card in (None, UNCERTAIN):
                card, via = self.reader.read_image(aligned), "vlm"
        return card, via

    def _wait_clear(self, timeout: float = 10.0) -> None:
        """等牌离开读牌相机视野, 防止下一张把同一画面读两遍。
        有 YOLO 用它判"画面里还有没有牌"(切边/手持也认得), 否则退回牌形对齐。"""
        if self.card_cam is None:
            return
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline and misses < 3:
            ok, frame = self.card_cam.read()
            if not ok:
                time.sleep(0.1)
                continue
            if self.fast is not None:
                present = self.fast.read_image(frame) != UNCERTAIN
            else:
                present = self._matcher._align(frame) is not None
            misses = misses + 1 if not present else 0
            time.sleep(0.1)

    # ---- god 协议 ----

    def next_card(self, kind: str | None = None) -> str | None:
        if kind == "burn":
            self.last_via = None
            if self.arm is not None and self.arm.alive:
                self.arm.pick_from_deck(present=False)   # 烧牌: 取不亮, 放 MUCK 由编排器指挥
            return None                   # 烧牌不亮不读
        armed = False
        if self.arm is not None and self.arm.alive:
            armed = self.arm.pick_from_deck() and self.arm.present_to_camera()
        if not armed:                     # 无臂/臂失败: 人肉亮牌, 演示不死
            self.prompter.prompt("亮牌: 将下一张牌面对准读牌相机")
        fails = 0
        while True:
            card, via = self._try_read()
            if card in (None, UNCERTAIN):
                fails += 1
                if fails >= 2:                            # 三级: 网页补录
                    card, via = self.ops.ask_card("靴口认牌失败, 人工输入这张牌"), "ops"
                else:
                    self.prompter.prompt("没认出来, 请把牌拿稳、正对相机再亮一次",
                                         level="again")
                    continue
            card = C.normalize(card)
            if card in self.dealt:
                self.prompter.prompt(f"识别到 {card} 但这张已发过(疑似重读), 请重新亮牌",
                                     level="again")
                self._wait_clear()
                continue
            self.dealt.add(card)
            self.last_via = via
            self.bus.emit({"type": "agent_trace", "text": f"靴口识别: {card} ({via})"})
            self._wait_clear()
            return card
