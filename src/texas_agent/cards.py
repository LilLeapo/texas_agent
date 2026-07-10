"""牌的表示与工具函数。

全系统统一牌面字符串格式: 点数大写 + 花色小写, 如 'As', 'Td', '7h'。
未知牌(烧牌/公开模式底牌)统一 '??'。
"""

from __future__ import annotations

import eval7

RANKS = "23456789TJQKA"  # 从小到大
SUITS = "shdc"
FULL_DECK: list[str] = [r + s for r in RANKS for s in SUITS]

UNKNOWN = "??"

# eval7.handtype 输出 → 系统类别 key
HANDTYPE_KEYS = {
    "High Card": "high",
    "Pair": "pair",
    "Two Pair": "two_pair",
    "Trips": "trips",
    "Straight": "straight",
    "Flush": "flush",
    "Full House": "full_house",
    "Quads": "quads",
    "Straight Flush": "straight_flush",
}
CATEGORY_ORDER = [
    "high", "pair", "two_pair", "trips", "straight",
    "flush", "full_house", "quads", "straight_flush",
]
CATEGORY_ZH = {
    "high": "高牌", "pair": "一对", "two_pair": "两对", "trips": "三条",
    "straight": "顺子", "flush": "同花", "full_house": "葫芦",
    "quads": "四条", "straight_flush": "同花顺",
}


def normalize(card: str) -> str:
    """容错输入: 'as' → 'As', '10h' → 'Th'。非法牌抛 ValueError。"""
    c = card.strip().replace("10", "T")
    if len(c) != 2:
        raise ValueError(f"非法牌面: {card!r}")
    r, s = c[0].upper(), c[1].lower()
    if r not in RANKS or s not in SUITS:
        raise ValueError(f"非法牌面: {card!r}")
    return r + s


def rank_of(card: str) -> int:
    """点数序号, 2=0 ... A=12。"""
    return RANKS.index(card[0])


def suit_of(card: str) -> str:
    return card[1]


_EVAL7_CACHE: dict[str, eval7.Card] = {}


def to_eval7(card: str) -> eval7.Card:
    c = _EVAL7_CACHE.get(card)
    if c is None:
        c = _EVAL7_CACHE[card] = eval7.Card(card)
    return c


def evaluate(cards: list[str]) -> int:
    """eval7 分值, 越大越强。接受 5~7 张。"""
    return eval7.evaluate([to_eval7(c) for c in cards])


def category(score: int) -> str:
    return HANDTYPE_KEYS[eval7.handtype(score)]


def grid_notation(hole: list[str]) -> str:
    """两张底牌 → 169 起手牌格记法: 'AA' / 'AKs' / 'Q8o'。"""
    a, b = sorted(hole, key=rank_of, reverse=True)
    if a[0] == b[0]:
        return a[0] * 2
    return a[0] + b[0] + ("s" if a[1] == b[1] else "o")
