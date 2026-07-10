"""D2-2 验收: 解说模板触发、去抖、LLM 超时/断网无缝降级零报错。"""

import time

from texas_agent.bus import LocalBus
from texas_agent.commentator import Commentator


def flop_msg(leader_eq=0.7):
    return {"type": "analysis_update", "mode": "god", "street": "flop",
            "board": ["Jh", "8h", "3c"],
            "players": [
                {"seat": 0, "equity": {"win": 1 - leader_eq}, "comeback": 1 - leader_eq,
                 "outs": {"clean": 8, "tainted": 1}, "now": {"cat_zh": "高牌"}},
                {"seat": 1, "equity": {"win": leader_eq}, "comeback": None,
                 "outs": None, "now": {"cat_zh": "三条"}}]}


def turn_msg():
    m = flop_msg(leader_eq=0.24)
    m["street"] = "turn"
    m["players"][0]["equity"]["win"] = 0.76
    m["players"][1]["equity"]["win"] = 0.24
    return m


def test_templates_and_reversal():
    bus = LocalBus(log=False)
    c = Commentator(bus, debounce_s=0.0)
    t1 = c.feed(flop_msg())
    assert t1 and ("P2" in t1 or "P1" in t1)
    t2 = c.feed(turn_msg())
    assert t2 and "P1" in t2  # 转牌反超触发 reversal 类
    bus.close()


def test_debounce():
    bus = LocalBus(log=False)
    c = Commentator(bus, debounce_s=60.0)
    kinds = [c.feed(flop_msg()) for _ in range(3)]
    assert kinds[0] is not None and kinds[1] is None and kinds[2] is None
    bus.close()


def test_llm_timeout_falls_back_to_template():
    bus = LocalBus(log=False)
    def dead_llm(prompt):
        time.sleep(10)  # 模拟断网卡死
    c = Commentator(bus, llm_fn=dead_llm, llm_timeout_s=0.2, debounce_s=0.0)
    t0 = time.time()
    text = c.feed(flop_msg())
    assert text is not None            # 降级模板, 零报错
    assert time.time() - t0 < 2.0      # 不被 LLM 拖死
    bus.close()


def test_llm_exception_falls_back():
    bus = LocalBus(log=False)
    def broken_llm(prompt):
        raise ConnectionError("网线被拔")
    c = Commentator(bus, llm_fn=broken_llm, debounce_s=0.0)
    assert c.feed(flop_msg()) is not None
    bus.close()


def test_llm_dropping_numbers_falls_back():
    """数字保全: 润色丢了模板里的数字 → 不采信, 回退模板。"""
    bus = LocalBus(log=False)
    def lossy_llm(prompt):
        return "这个翻牌对领先者非常有利!"   # 数字全丢
    c = Commentator(bus, llm_fn=lossy_llm, debounce_s=0.0)
    text = c.feed(flop_msg())
    # flop 可能触发 flop_leader 或 comeback 两类模板, 但都带数字 —— 回退即胜利
    assert text is not None and text != "这个翻牌对领先者非常有利!"
    assert any(ch.isdigit() for ch in text)
    bus.close()


def test_reversal_never_uses_llm():
    """反转是全场最值钱的一句, 永远走模板, 不冒润色风险。"""
    bus = LocalBus(log=False)
    msgs = []
    bus.subscribe(msgs.append)
    calls = []
    def spy_llm(prompt):
        calls.append(prompt)
        return "改写句 76% 24%"
    c = Commentator(bus, llm_fn=spy_llm, debounce_s=0.0)
    c.feed(flop_msg())                 # flop_leader/comeback: 会走润色
    n_before = len(calls)
    text = c.feed(turn_msg())          # reversal
    assert text is not None
    assert msgs[-1]["type"] == "commentary" and msgs[-1]["trigger"] == "reversal"
    assert len(calls) == n_before      # reversal 没调 LLM
    bus.close()
