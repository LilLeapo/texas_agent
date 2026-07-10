"""慢循环 VLM 组件: 认牌仲裁员 + 发牌审计员 (跑在局域网 Spark 上, 可以死)。

纪律与 commentator/llm 相同: VLM 只当眼睛和嘴, 不当手也不当脑 ——
牌局推进永远不等它, 任何失败下层接住(仲裁跳操作员补录 / 审计静默消失)。
提问一律封闭问题(52 选 1 / 对答案), 禁开放式"你看到了什么"。
"""

from __future__ import annotations

import concurrent.futures
import re

from . import cards as C

UNCERTAIN = "uncertain"

CARD_PROMPT = ("这是一张扑克牌的牌面照片。它是 52 张扑克牌中的哪一张? "
               "先看角标颜色(红色=红桃h/方块d, 黑色=黑桃s/梅花c), 再看花色形状和点数。"
               "只回答牌面代码, 点数大写+花色小写, 如 'Qh'、'As'、'Td'。"
               "看不清或不确定就只回答'不确定', 禁止猜测。")

_CARD_RE = re.compile(r"(10|[2-9TJQKAtjqka])\s*([shdcSHDC])")
_UNCERTAIN_RE = re.compile(r"不确定|uncertain|unsure|unclear", re.IGNORECASE)


def parse_card_reply(text: str | None) -> str | None:
    """VLM 回复 → 牌面; 不确定/答非所问/None 一律 None(宁可不确定, 不可错判)。"""
    if not text or _UNCERTAIN_RE.search(text):
        return None
    m = _CARD_RE.search(text)
    if m is None:
        return None
    try:
        return C.normalize(m.group(1) + m.group(2))
    except ValueError:
        return None


class VlmCardReader:
    """认牌仲裁员: NCC 置信不足时的第二意见, read_card 协议与 vision 相同。

    兜底链位置: vision.read_card → 本仲裁 → ops.ask_card。
    """

    def __init__(self, client, image_source, timeout_s: float = 6.0):
        self.client = client              # llm.LlmClient
        self.image_source = image_source  # callable(zone) -> BGR 图 | None
        self.timeout_s = timeout_s

    def read_card(self, zone: str) -> str:
        try:
            img = self.image_source(zone)
            if img is None:
                return UNCERTAIN
            reply = self.client.ask(CARD_PROMPT, image_bgr=img,
                                    timeout_s=self.timeout_s)
            card = parse_card_reply(reply)
            if card is None:
                return UNCERTAIN
            if _color_consistent(img, card[1]) is False:
                return UNCERTAIN  # VLM 报的花色颜色与像素红黑矛盾 → 不采信
            return card
        except Exception:
            return UNCERTAIN


def _color_consistent(image_bgr, suit: str):
    """像素级红/黑判色 vs VLM 花色的交叉核验(实测抓到过黑桃认成红桃)。

    True=一致, False=矛盾, None=判不了(墨量太少/无 cv2, 不否决)。
    """
    try:
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
        red = int(np.sum(cv2.inRange(hsv, np.array([0, 70, 60]), np.array([10, 255, 255])) |
                         cv2.inRange(hsv, np.array([170, 70, 60]), np.array([180, 255, 255]))))
        dark = int(np.sum(cv2.inRange(hsv, np.array([0, 0, 0]), np.array([180, 255, 90]))))
        if red + dark < 255 * 30:   # 墨迹不足 30 像素: 不下结论
            return None
        return (red > dark) == (suit in "hd")
    except Exception:
        return None


class DealAuditor:
    """发牌审计员: 总线订阅者, 每次 deal 后异步拿顶视静止帧向 VLM 对答案。

    只有建议权 —— 不阻塞主循环; 结论走 audit_report, 不符加发 alert;
    VLM 失联/看不清什么都不发。积压时只审计最新一次发牌, 过期帧作废。
    """

    def __init__(self, bus, client, frame_source, timeout_s: float = 20.0):
        self.bus = bus
        self.client = client
        self.frame_source = frame_source  # callable() -> 顶视静止帧 | None
        self.timeout_s = timeout_s
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._latest = 0

    def feed(self, msg: dict) -> None:
        if msg.get("type") != "game_event" or msg.get("event") != "deal":
            return
        if msg["detail"]["deal_kind"] == "burn":
            return  # 烧牌背面朝下, 照片核不出信息
        self._latest += 1
        self._pool.submit(self._audit, self._latest, msg)

    def _audit(self, ticket: int, msg: dict) -> None:
        try:
            if ticket != self._latest:
                return  # 已有更新的发牌, 本次作废
            frame = self.frame_source()
            if frame is None:
                return
            st = msg["state"]
            reply = self.client.ask(self._question(st), image_bgr=frame,
                                    timeout_s=self.timeout_s)
            verdict, note = self._parse(reply)
            if verdict is None:
                return  # 失联或看不清: 静默消失
            self.bus.emit({"type": "audit_report", "street": st["street"],
                           "board": st["board"], "verdict": verdict, "note": note})
            if verdict == "mismatch":
                self.bus.emit({"type": "alert", "text": f"发牌审计不符: {note}"})
        except Exception:
            pass

    @staticmethod
    def _question(st: dict) -> str:
        board = st["board"]
        board_desc = (f"公共牌区从左到右应是 {' '.join(board)} 共 {len(board)} 张明牌"
                      if board else "公共牌区应没有明牌")
        live = sum(1 for x in st["seats"] if x["in_hand"])
        return (f"这是德州扑克桌的顶视照片。系统记录: {board_desc}; "
                f"{live} 位未弃牌玩家每人面前应有 2 张背面朝上的底牌。"
                f"照片与记录一致吗? 只回答'一致', 或'不一致: '加一句原因; "
                f"看不清就只回答'不确定'。")

    @staticmethod
    def _parse(reply: str | None) -> tuple[str | None, str]:
        if not reply or _UNCERTAIN_RE.search(reply):
            return None, ""
        if "不一致" in reply:
            return "mismatch", reply.split(":", 1)[-1].split("：", 1)[-1].strip() or reply
        if "一致" in reply:
            return "match", ""
        return None, ""
