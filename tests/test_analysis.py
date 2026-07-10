"""M6 验收: 与手算抽查一致 + 0.5s 性能门 (手册 D2-1)。

基准局面: P1 AhKh(坚果同花听牌) vs P2 JcJs(暗三条), 翻牌 Jh 8h 3c。
手算: 未见牌 45 张; 红桃剩 9 张 → 下张成花 = 9/45 = 0.2;
P1 outs: 9 张红桃反超, 其中 3h 让 P2 成葫芦 → clean 8 / tainted 1。
"""

import pytest

from texas_agent.analysis import analyze

P1 = {"seat": 0, "hole": ["Ah", "Kh"], "in_hand": True}
P2 = {"seat": 1, "hole": ["Jc", "Js"], "in_hand": True}
FLOP = ["Jh", "8h", "3c"]


@pytest.fixture(scope="module")
def flop_result():
    return analyze([P1, P2], FLOP, pot=600,
                   actor={"seat": 0, "to_call": 300, "pot": 900, "eff_stack": 19000})


def test_perf_gate(flop_result):
    assert flop_result["elapsed_ms"] < 500


def test_next_card_flush_probability_exact(flop_result):
    assert flop_result["board_next"]["flush"] == round(9 / 45, 4)


def test_clean_tainted_outs(flop_result):
    p1, p2 = flop_result["players"]
    assert p1["outs"] == {"clean": 8, "tainted": 1}
    assert p2["outs"] is None  # 领先者无 outs
    assert p2["now"]["ahead"] is True
    assert p1["comeback"] == pytest.approx(p1["equity"]["win"] + 0, abs=1e-9)


def test_exact_enumeration_equity(flop_result):
    """C(45,2)=990 种走向精确枚举, 数值应完全确定。"""
    assert flop_result["n_runouts"] == 990
    p1 = flop_result["players"][0]
    assert p1["equity"]["win"] == pytest.approx(0.2556, abs=1e-4)


def test_nuts_holder(flop_result):
    assert flop_result["nuts"]["holder"] == 1  # 当前坚果 = 三条 J, P2 在手


def test_final_dist_sums_to_one(flop_result):
    for p in flop_result["players"]:
        assert sum(p["final_dist"].values()) == pytest.approx(1.0, abs=0.01)


def test_actor_pot_odds_mdf():
    r = analyze([P1, P2], FLOP, actor={"seat": 0, "to_call": 300, "pot": 900,
                                       "eff_stack": 19000})
    a = r["actor"]
    assert a["pot_odds"] == a["required_eq"] == 0.25   # 300/(900+300)
    assert a["alpha"] == pytest.approx(300 / 900, abs=1e-3)
    assert a["mdf"] == pytest.approx(1 - 300 / 900, abs=1e-3)


def test_preflop_monte_carlo_direction():
    r = analyze([P1, P2], [], pot=150)
    assert 0.40 < r["players"][0]["equity"]["win"] < 0.52  # AKs vs JJ ≈ 46/54
    assert r["players"][0]["now"] is None                  # 翻前无当前牌力概念


def test_public_mode_degrades():
    r = analyze([{"seat": 0, "hole": None, "in_hand": True},
                 {"seat": 1, "hole": ["Ah", "Kh"], "in_hand": True}], FLOP)
    assert r["mode"] == "public"
    assert r["deck_remaining"] is None and r["nuts"] is None
    assert r["players"][0]["outs"] is None


def test_river_certainty():
    r = analyze([P1, P2], ["Jh", "8h", "3c", "Qh", "6s"])
    eqs = {p["seat"]: p["equity"]["win"] for p in r["players"]}
    assert eqs == {0: 1.0, 1: 0.0}  # P1 坚果同花已成
