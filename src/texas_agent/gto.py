"""M7 · GTO 基线层: 预计算查表, 零现场求解。

charts/{node}.json, node 键 = 9max_100bb/{位置}/{面对序列}。
查不到的节点静默返回 None —— 绝不编造。
措辞纪律: 一律称"GTO 基线 / 参考策略"; 离线求解缓存命中时 source 标 "solver_offline"。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import cards as C

POSITION_ZH = {
    "UTG": "枪口", "UTG1": "枪口+1", "MP": "中位", "LJ": "低劫持",
    "HJ": "高劫持", "CO": "关煞", "BTN": "庄位", "SB": "小盲", "BB": "大盲",
}


class GtoCharts:
    def __init__(self, charts_dir: str = "charts", stack_tag: str = "9max_100bb"):
        self.root = Path(charts_dir)
        self.stack_tag = stack_tag
        self._cache: dict[str, dict | None] = {}

    def _load(self, node: str) -> dict | None:
        if node not in self._cache:
            path = self.root / f"{node}.json"
            self._cache[node] = json.loads(path.read_text()) if path.exists() else None
        return self._cache[node]

    def node_key(self, engine, seat: int) -> str | None:
        """由(座位位置, 翻前行动序列)拼节点键; 罕见序列(limp 链/3bet+)返回 None。"""
        seq = [x for x in engine.preflop_sequence() if x[1] != "fold"]
        raises = [s for s, a in seq if a == "raise"]
        calls = [s for s, a in seq if a in ("call", "check")]
        pos = engine.position(seat)
        if not raises and not calls:
            return f"{self.stack_tag}/{pos}/RFI"
        if len(raises) == 1 and raises[0] != seat and not calls:
            return f"{self.stack_tag}/{pos}/vs_open_{engine.position(raises[0])}"
        return None

    def hint(self, engine, seat: int) -> dict | None:
        """AWAIT 翻前分支调用; 返回 gto_hint 消息载荷或 None(静默)。"""
        if engine.street_name() != "preflop":
            return None
        node = self.node_key(engine, seat)
        chart = self._load(node) if node else None
        if not chart:
            return None
        hole = engine.hole(seat)
        hero = C.grid_notation(hole) if hole else None
        n = len(chart["range"])
        summary = {}
        for freqs in chart["range"].values():
            for act, f in freqs.items():
                summary[act] = summary.get(act, 0) + f / n
        return {"type": "gto_hint", "seat": seat, "node": node, "hero": hero,
                "summary": {k: round(v, 4) for k, v in summary.items()},
                "matrix": chart["range"], "actions": chart["actions"],
                "source": chart.get("source", "chart")}

    @staticmethod
    def deviation_from_hint(hint: dict | None, action: str) -> dict | None:
        """玩家行动后调用, 传入行动前取好的 hint(行动会改变序列, 节点键必须提前取)。"""
        if not hint or not hint.get("hero"):
            return None
        freqs = hint["matrix"].get(hint["hero"])
        if freqs is None:
            return None
        return {"type": "gto_deviation", "seat": hint["seat"], "hand": hint["hero"],
                "node": hint["node"], "gto": freqs, "action": action}
