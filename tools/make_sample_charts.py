"""生成占位 GTO 图表 (D0 正式转录前的假数据, 让矩阵组件与查表链路先跑通)。

范围为公开常识的近似, source 标 "placeholder" —— 演示叙事与联调够用,
正式数据按手册 D0: 手工转录公开图表 或 采用开源求解器随附图表(核对许可)。

用法: python tools/make_sample_charts.py [charts目录]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RANKS = "AKQJT98765432"


def expand(spec: str) -> dict[str, float]:
    """迷你范围语言 → {手牌: 频率}。
    支持: '77+' 'ATs+' 'KQo' 'A5s-A2s' '55' 及 'A5s:0.55' 混合频率。"""
    out: dict[str, float] = {}
    for tok in spec.split():
        freq = 1.0
        if ":" in tok:
            tok, f = tok.split(":")
            freq = float(f)
        if "-" in tok:  # A5s-A2s / 22-TT
            hi, lo = tok.split("-")
            if hi[0] == hi[1]:  # 对子区间, 两端任意顺序
                a, b = sorted((RANKS.index(hi[0]), RANKS.index(lo[0])))
                for k in range(a, b + 1):
                    out[RANKS[k] * 2] = freq
            else:
                h, s = hi[0], hi[2]
                a, b = sorted((RANKS.index(hi[1]), RANKS.index(lo[1])))
                for k in range(a, b + 1):
                    out[h + RANKS[k] + s] = freq
        elif tok.endswith("+"):
            tok = tok[:-1]
            if tok[0] == tok[1]:  # 77+
                for k in range(RANKS.index(tok[0]) + 1):
                    out[RANKS[k] * 2] = freq
            else:  # ATs+ → AJs AQs AKs 含 ATs
                h, s = tok[0], tok[2]
                for k in range(RANKS.index(h) + 1, RANKS.index(tok[1]) + 1):
                    out[h + RANKS[k] + s] = freq
        else:
            out[tok] = freq
    return out


RFI = {  # 位置: (范围, 尺度)
    "UTG":  ("77+ ATs+ KJs+ QJs JTs AJo+ KQo A5s:0.5", "raise_2.5"),
    "UTG1": ("66+ A9s+ KTs+ QTs+ JTs T9s ATo+ KQo A5s-A4s", "raise_2.5"),
    "MP":   ("55+ A7s+ K9s+ Q9s+ J9s+ T9s 98s ATo+ KJo+ A5s-A3s", "raise_2.5"),
    "LJ":   ("44+ A5s+ K9s+ Q9s+ J9s+ T8s+ 98s 87s A9o+ KJo+ QJo", "raise_2.5"),
    "HJ":   ("33+ A2s+ K8s+ Q9s+ J8s+ T8s+ 97s+ 87s 76s A8o+ KTo+ QJo", "raise_2.5"),
    "CO":   ("22+ A2s+ K5s+ Q8s+ J8s+ T7s+ 97s+ 86s+ 76s 65s A5o+ K9o+ QTo+ JTo", "raise_2.5"),
    "BTN":  ("22+ A2s+ K2s+ Q4s+ J6s+ T6s+ 96s+ 85s+ 75s+ 64s+ 54s A2o+ K8o+ Q9o+ J9o+ T9o 98o", "raise_2.5"),
    "SB":   ("22+ A2s+ K2s+ Q2s+ J4s+ T6s+ 96s+ 86s+ 75s+ 65s 54s A2o+ K7o+ Q9o+ J9o+ T8o+ 98o", "raise_3.0"),
}

VS_OPEN = {  # (hero位置, 开局加注位): (3bet范围, 跟注范围)
    ("BTN", "CO"): ("JJ+ AQs+ AKo A5s-A4s:0.5 KQs:0.35",
                    "22-TT ATs-AJs KTs+ QTs+ JTs T9s 98s AQo:0.5"),
    ("BB", "BTN"): ("99+ ATs+ KQs A5s-A2s AQo+ 76s:0.3",
                    "22-88 A2s-A9s K5s+ Q8s+ J8s+ T7s+ 96s+ 86s+ 75s+ 65s 54s ATo-AJo KTo+ QTo+ JTo"),
    ("BB", "SB"):  ("TT+ ATs+ KTs+ AJo+ KQo A5s-A2s:0.6 T9s:0.4",
                    "22-99 A2s-A9s K2s+ Q4s+ J7s+ T7s+ 97s+ 86s+ 76s 65s A2o+ K9o+ Q9o+ J9o+ T9o"),
}


def all_hands() -> list[str]:
    hands = []
    for i, a in enumerate(RANKS):
        for j, b in enumerate(RANKS):
            if i == j:
                hands.append(a + a)
            elif i < j:
                hands.append(a + b + "s")
            else:
                hands.append(b + a + "o")
    return hands


def build(node: str, actions: list[str], layers: list[tuple[str, dict]]) -> dict:
    """layers: [(动作, 范围)], 先命中先得, 余量归 fold。"""
    rng = {}
    for hand in all_hands():
        freqs, left = {}, 1.0
        for act, spec in layers:
            f = min(spec.get(hand, 0.0), left)
            if f > 0:
                freqs[act] = round(f, 2)
                left = round(left - f, 4)
        if left > 1e-9:
            freqs["fold"] = round(left, 2)
        rng[hand] = freqs
    return {"node": node, "actions": actions, "source": "placeholder", "range": rng}


def main(root: str = "charts") -> None:
    base = Path(root) / "9max_100bb"
    n = 0
    for pos, (spec, sizing) in RFI.items():
        node = f"9max_100bb/{pos}/RFI"
        chart = build(node, [sizing, "fold"], [(sizing, expand(spec))])
        p = base / pos / "RFI.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(chart, ensure_ascii=False, indent=1))
        n += 1
    for (hero, opener), (spec3, specc) in VS_OPEN.items():
        node = f"9max_100bb/{hero}/vs_open_{opener}"
        chart = build(node, ["raise_3bet", "call", "fold"],
                      [("raise_3bet", expand(spec3)), ("call", expand(specc))])
        p = base / hero / f"vs_open_{opener}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(chart, ensure_ascii=False, indent=1))
        n += 1
    print(f"生成 {n} 张占位图表 → {base}")


if __name__ == "__main__":
    main(*sys.argv[1:])
