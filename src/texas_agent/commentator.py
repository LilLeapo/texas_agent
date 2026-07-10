"""M6 · 解说员: {状态快照, 最近事件, 分析载荷} → ≤2 句中文。

主路模板句库(每类事件多条填空式, 零依赖、零延迟);
LLM 是增强路: 传入 llm_fn(prompt)->str, 2s 超时或异常即静默切回模板 —— 拔网线零报错。
同类事件 5s 去抖; TTS 走 Prompter 同款串行队列(独立实例, 避免抢占荷官提词)。
"""

from __future__ import annotations

import concurrent.futures
import queue
import random
import re
import shutil
import subprocess
import threading
import time

TEMPLATES = {
    "flop_leader": [
        "翻牌 {board}, {leader} 目前{cat}领先, 胜率 {eq:.0%}。",
        "{leader} 在 {board} 的翻牌面拿到{cat}, {eq:.0%} 的胜率占据主动。",
        "这个翻牌对 {leader} 很有利, {cat}在手, 胜率 {eq:.0%}。",
    ],
    "comeback": [
        "{player} 落后但还有 {cb:.0%} 的反超机会, 干净补牌 {clean} 张。",
        "别急着下结论, {player} 手里还有 {clean} 张干净补牌, 反超概率 {cb:.0%}。",
    ],
    "reversal": [
        "剧情反转! {player} 反超, 胜率来到 {eq:.0%}。",
        "这张牌彻底改变了局面, {player} 现在以 {eq:.0%} 领跑。",
    ],
    "danger": [
        "注意下一张牌: 成花概率 {flush:.0%}, 公共牌配对概率 {pair:.0%}。",
    ],
    "gto_dev": [
        "GTO 基线在这个位置的 {hand} 只有 {freq:.0%} 的 {action} 频率, {player} 还是选择了 {action}。",
        "{player} 的 {hand} 按参考策略应该{alt}, 实际选择了 {action}。",
    ],
    "settlement": [
        "本手结束, {winner} 收下 {amount} 的底池。",
        "尘埃落定, {winner} 赢下 {amount}。",
    ],
}


class Commentator:
    def __init__(self, bus, llm_fn=None, tts: bool = False, debounce_s: float = 5.0,
                 llm_timeout_s: float = 2.0, rng=None):
        self.bus = bus
        self.llm_fn = llm_fn
        self.debounce_s = debounce_s
        self.llm_timeout_s = llm_timeout_s
        self.rng = rng or random.Random()
        self._last: dict[str, float] = {}
        self._last_leader: int | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1) if llm_fn else None
        self._q: queue.Queue | None = None
        if tts and shutil.which("say"):
            self._q = queue.Queue()
            threading.Thread(target=self._tts_loop, daemon=True).start()

    # 由编排器/总线订阅喂消息
    def feed(self, msg: dict) -> str | None:
        kind, slots = self._pick(msg)
        if kind is None:
            return None
        # 去抖按消息来源分桶(分析类共用一桶, 免得换个句式就绕过去抖);
        # reversal 是全场最值钱的时刻, 豁免去抖
        bucket = "analysis" if msg.get("type") == "analysis_update" else kind
        now = time.time()
        if kind != "reversal" and now - self._last.get(bucket, 0) < self.debounce_s:
            return None
        self._last[bucket] = now
        text = self._llm_or_template(kind, slots, msg)
        if text:
            self.bus.emit({"type": "commentary", "text": text, "trigger": kind})
            if self._q is not None:
                self._q.put(text)
        return text

    def _tts_loop(self) -> None:
        while True:
            try:
                subprocess.run(["say", self._q.get()], check=False, timeout=20)
            except Exception:
                pass

    def _pick(self, msg) -> tuple[str | None, dict]:
        t = msg.get("type")
        if t == "analysis_update" and msg.get("mode") == "god" and msg["players"]:
            top = max(msg["players"], key=lambda p: p["equity"]["win"])
            name = f"P{top['seat'] + 1}"
            if self._last_leader is not None and top["seat"] != self._last_leader \
                    and msg["street"] in ("turn", "river"):
                self._last_leader = top["seat"]
                return "reversal", {"player": name, "eq": top["equity"]["win"]}
            self._last_leader = top["seat"]
            if msg["street"] == "flop":
                behind = [p for p in msg["players"] if p.get("comeback") and p.get("outs")]
                if behind and self.rng.random() < 0.5:
                    b = max(behind, key=lambda p: p["comeback"])
                    return "comeback", {"player": f"P{b['seat'] + 1}", "cb": b["comeback"],
                                        "clean": b["outs"]["clean"]}
                cat = (top.get("now") or {}).get("cat_zh", "领先牌")
                return "flop_leader", {"leader": name, "cat": cat,
                                       "eq": top["equity"]["win"],
                                       "board": " ".join(msg["board"])}
        if t == "gto_deviation":
            freq = msg["gto"].get(msg["action"] if msg["action"] != "raise" else "raise_2.5", 0.0)
            alt = max(msg["gto"], key=msg["gto"].get)
            return "gto_dev", {"player": f"P{msg['seat'] + 1}", "hand": msg["hand"],
                               "freq": freq, "action": msg["action"],
                               "alt": {"fold": "弃牌", "call": "跟注"}.get(
                                   alt, "加注" if alt.startswith("raise") else alt)}
        if t == "game_event" and msg.get("event") == "settlement":
            pays = msg["detail"]["payoffs"]
            w = max(range(len(pays)), key=lambda i: pays[i])
            return "settlement", {"winner": f"P{w + 1}", "amount": pays[w]}
        return None, {}

    def _llm_or_template(self, kind: str, slots: dict, msg: dict) -> str:
        template = self.rng.choice(TEMPLATES[kind]).format(**slots)
        # reversal 是全场最值钱的一句, 模板本身够有力, 不冒润色风险
        if not self.llm_fn or kind == "reversal":
            return template
        # 润色而非自由发挥: 模板句事实百分百正确, LLM 只负责说得更生动。
        # 实测 7B 按裸数据造句会搞混主语("对手胜率70%…对P2有利"), 润色则稳。
        prompt = (f"把这句德州扑克解说改写得更生动自然, 一句话≤40字: 「{template}」"
                  f"要求: 玩家称谓、数字、领先/落后关系一律不变; 金额单位是筹码, "
                  f"不得添加货币单位; 不得新增任何事实。只输出改写后的句子。")
        try:
            fut = self._pool.submit(self.llm_fn, prompt)
            out = (fut.result(timeout=self.llm_timeout_s) or "").strip()
            # 数字保全校验: 模板里的每个数字必须原样出现, 丢失/篡改即回退模板
            if not out or any(n not in out for n in re.findall(r"\d+", template)):
                return template
            return out
        except Exception:
            return template  # 超时/断网无缝降级
