"""M5 验收: 编排器闭环 —— 剧情牌整手无人值守跑完, 故障注入路由正确。"""

import pytest

from texas_agent.bus import LocalBus
from texas_agent.engine import Engine
from texas_agent.gto import GtoCharts
from texas_agent.orchestrator import (MockVision, Orchestrator, PresetGod,
                                      ScriptedInputs, ScriptedOps, WatchEvent)
from texas_agent.prompter import Prompter

SCRIPT = [("call", None)] * 3 + [("check", None),
          ("check", None), ("raise", 300), ("call", None), ("fold", None), ("call", None),
          ("raise", 500), ("call", None), ("fold", None),
          ("raise", 1000), ("call", None)]


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    b.msgs = []
    b.subscribe(b.msgs.append)
    yield b
    b.close()


def build(bus, vision=None):
    engine = Engine(bus, n_players=4, stacks=(20000,) * 4, blinds=(50, 100), mode="preset")
    god = PresetGod.from_file("config/deck_order.txt")
    return Orchestrator(bus, engine, vision=vision or MockVision(), god=god,
                        inputs=ScriptedInputs(list(SCRIPT)), ops=ScriptedOps(),
                        prompter=Prompter(bus), charts=GtoCharts("charts"))


def test_full_hand_headless(bus, capsys):
    snap = build(bus).run_hand()
    assert snap["complete"]
    assert [x["stack"] for x in snap["seats"]] == [22400, 18100, 19600, 19900]
    types = {m["type"] for m in bus.msgs}
    assert {"game_event", "dealer_prompt", "input_request", "agent_trace",
            "analysis_update", "gto_hint", "gto_deviation"} <= types


def test_equity_reversal_story(bus, capsys):
    """胜率之河按设计反转: 翻牌 P2 领先, 转牌后 P1 领先。"""
    build(bus).run_hand()
    ups = [m for m in bus.msgs if m["type"] == "analysis_update"]
    flop = next(m for m in ups if m["street"] == "flop")
    turn = next(m for m in ups if m["street"] == "turn")
    eq = lambda m, s: next(p["equity"]["win"] for p in m["players"] if p["seat"] == s)
    assert eq(flop, 1) > 0.6 > eq(flop, 0)
    assert eq(turn, 0) > 0.7 > eq(turn, 1)


def test_wrong_zone_triggers_correction(bus, capsys):
    """故障注入: 首张牌落错区 → 操作员确认按实际区域记录 → correction 事件。"""
    vision = MockVision(script=[WatchEvent(ok=False, wrong_zone="P2a")])
    orch = build(bus, vision=vision)
    orch.ops = ScriptedOps(confirms=[True])
    orch.run_hand()
    corr = [m for m in bus.msgs if m["type"] == "correction"]
    assert corr and corr[0]["old"] == "P1a" and corr[0]["new"] == "P2a"


def test_timeout_reprompts_then_alert(bus, capsys):
    """故障注入: 荷官持续无响应 → 温和重提一次 → 升级 alert。"""
    vision = MockVision(script=[WatchEvent(timeout=True), WatchEvent(timeout=True)])
    build(bus, vision=vision).run_hand()
    prompts = [m for m in bus.msgs if m["type"] == "dealer_prompt"]
    assert any(m["level"] == "again" for m in prompts)
    assert any(m["type"] == "alert" for m in bus.msgs)


def test_gto_hint_correct_node(bus, capsys):
    build(bus).run_hand()
    hints = [m for m in bus.msgs if m["type"] == "gto_hint"]
    assert hints[0]["node"] == "9max_100bb/UTG/RFI"
    assert hints[0]["hero"] == "99"
    devs = [m for m in bus.msgs if m["type"] == "gto_deviation"]
    assert devs and devs[0]["action"] == "call"
