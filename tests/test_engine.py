"""M1 验收: 引擎适配器 —— 含 all-in 边池、弃牌终局、纠错重放。"""

import pytest

from texas_agent.bus import LocalBus
from texas_agent.engine import Engine


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    yield b
    b.close()


def drive(engine, deck, actions):
    """把一手牌开到底: deck 按需供牌, actions 按序申报。"""
    deck, actions = list(deck), list(actions)
    while True:
        need = engine.next_required()
        if need.kind == "DEAL":
            card = "??" if need.subkind == "burn" else deck.pop(0)
            engine.record_deal(card, need.zones[0])
        elif need.kind == "AWAIT":
            a = actions.pop(0)
            engine.apply(a[0], a[1] if len(a) > 1 else None)
        else:
            return need


def test_full_hand_with_side_pot(bus):
    """三人不同筹码全下: 短码赢主池, 中码赢边池。"""
    e = Engine(bus, n_players=3, stacks=(500, 2000, 5000), blinds=(50, 100))
    # P1(SB,短码) AA, P2(BB,中码) KK, P3(BTN) QQ; 板面无人改善
    deck = ["As", "Kd", "Qh", "Ad", "Kc", "Qs", "2h", "7d", "8c", "3s", "9h"]
    acts = [("raise", 5000), ("call",), ("call",)]  # BTN 全下, 双双跟注(各自封顶)
    need = drive(e, deck, acts)
    assert need.kind == "COMPLETE"
    # 主池 500×3=1500 归 P1(AA); 边池 1500×2=3000 归 P2(KK); P3 未被跟注的 3000 退回
    assert list(e.state.stacks) == [1500, 3000, 3000]
    assert e.payoffs() == [1000, 1000, -2000]


def test_everyone_folds(bus):
    e = Engine(bus, n_players=4, stacks=(1000,) * 4, blinds=(50, 100))
    deck = ["As", "Kd", "Qh", "Jc", "Ad", "Kc", "Qs", "Jd"]
    need = drive(e, deck, [("fold",), ("fold",), ("fold",)])
    assert need.kind == "COMPLETE"
    assert e.payoffs() == [-50, 50, 0, 0]  # 大盲白捡小盲


def test_amend_replays_and_flips_result(bus):
    e = Engine(bus, n_players=2, stacks=(1000, 1000), blinds=(50, 100))
    deck = ["Ah", "Kd", "Kh", "Ks", "Jh", "8h", "3c", "Qc", "6s"]
    drive(e, deck, [("call",), ("check",)] + [("check",), ("check",)] * 3)
    assert e.payoffs() == [-100, 100]  # KdKs 一对 K 胜 AhKh 高牌
    idx = next(i for i, op in enumerate(e.oplog) if op[1] == "Qc")
    e.amend(idx, "Qh")  # 转牌改成红桃 Q → P1 同花
    assert e.board()[3] == "Qh"
    assert e.payoffs() == [100, -100]


def test_public_mode_manual_showdown(bus):
    """公开模式: 底牌以 ?? 录入, 摊牌时人工亮牌后才能结算。"""
    e = Engine(bus, n_players=2, stacks=(1000, 1000), blinds=(50, 100), mode="public")
    deck = ["??", "??", "??", "??", "Jh", "8h", "3c", "Qc", "6s"]
    need = drive(e, deck, [("call",), ("check",)] + [("check",), ("check",)] * 3)
    assert need.kind == "SHOWDOWN"
    e.record_showdown(need.seat, ["Ah", "Kh"])
    need2 = e.next_required()
    assert need2.kind == "SHOWDOWN"
    e.record_showdown(need2.seat, ["Kd", "Ks"])
    assert e.next_required().kind == "COMPLETE"
    assert e.payoffs() == [-100, 100]


def test_wrong_actor_rejected(bus):
    e = Engine(bus, n_players=2, stacks=(1000, 1000), blinds=(50, 100))
    for c in ["As", "Kd", "Ad", "Kc"]:
        e.record_deal(c, None)
    with pytest.raises(ValueError):
        e.apply("call", seat=0)  # 单挑翻前先行动的是庄位(座位1), 不是大盲(座位0)
