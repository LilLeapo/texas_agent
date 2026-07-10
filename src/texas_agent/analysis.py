"""M6 · 全量分析层。

原则: 信息集内可算尽算, 一次枚举、顺路填满。
计算纪律: 一切概率只以"牌桌信息集"(各家已知底牌 + 已发公共牌)为条件,
烧牌视为未知、绝不使用预排牌序的未来信息 —— 否则胜率变成剧透。

- 上帝模式(全部在手玩家底牌已知): flop 起对剩余走向精确枚举, preflop 蒙特卡洛;
- 公开模式(底牌未知): 对随机手牌蒙特卡洛, 领先关系/补牌/剩余牌堆置 null(前端显示 "—")。

所有核心档指标在同一个走向循环内累加, 翻牌圈四人局全量 < 0.5s。
"""

from __future__ import annotations

import itertools
import random
import time

from . import cards as C

MC_SAMPLES = 3000


def analyze(players, board, pot=0, actor=None, mc_samples=MC_SAMPLES, rng=None):
    """核心入口。

    players: [{'seat': int, 'hole': ['Ah','Kh'] | None, 'in_hand': bool}]
    board:   已发公共牌
    actor:   当前行动者上下文 {'seat', 'to_call', 'pot', 'eff_stack'} 或 None
    返回 analysis_update 消息载荷(不含信封)。
    """
    t0 = time.perf_counter()
    rng = rng or random.Random(7)
    board = list(board)
    active = [p for p in players if p["in_hand"]]
    known = [p for p in active if p.get("hole")]
    god = len(known) == len(active) and len(active) >= 1
    street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[len(board)]
    to_come = 5 - len(board)

    seen = set(board)
    for p in known:
        seen.update(p["hole"])
    unseen = [c for c in C.FULL_DECK if c not in seen]

    # ---------- 主循环: 胜率 + 终局牌型分布 ----------
    acc = {p["seat"]: {"win": 0, "tie": 0, "eq": 0.0, "cats": dict.fromkeys(C.CATEGORY_ORDER, 0)}
           for p in active}
    n_runs = 0
    if god and to_come <= 2:
        runouts = itertools.combinations(unseen, to_come)  # 精确枚举
        sample_holes = False
    else:
        runouts = range(mc_samples)                        # 蒙特卡洛
        sample_holes = not god

    for r in runouts:
        if isinstance(r, tuple):
            runout = list(r)
            holes = {p["seat"]: p["hole"] for p in active}
        else:
            need = to_come + 2 * (len(active) - len(known)) if sample_holes else to_come
            draw = rng.sample(unseen, need)
            runout, draw = draw[:to_come], draw[to_come:]
            holes = {}
            for p in active:
                if p.get("hole"):
                    holes[p["seat"]] = p["hole"]
                else:
                    holes[p["seat"]], draw = draw[:2], draw[2:]
        full = board + runout
        scores = {s: C.evaluate(h + full) for s, h in holes.items()}
        best = max(scores.values())
        winners = [s for s, sc in scores.items() if sc == best]
        for s, sc in scores.items():
            a = acc[s]
            a["cats"][C.category(sc)] += 1
            if s in winners:
                if len(winners) == 1:
                    a["win"] += 1
                else:
                    a["tie"] += 1
                a["eq"] += 1 / len(winners)
        n_runs += 1

    # ---------- 当前牌力 / 坚果 / 领先 (需要 flop 后 + 已知底牌) ----------
    now_score = {}
    nuts_info = None
    if board and known:
        for p in known:
            now_score[p["seat"]] = C.evaluate(p["hole"] + board)
        lead = max(now_score.values()) if now_score else None

        # 共享一次 C(未见于公共牌的牌, 2) 枚举: 坚果榜 + 牌力分位 + 第 N 坚果
        pool = [c for c in C.FULL_DECK if c not in board]
        combo_scores = {}
        for combo in itertools.combinations(pool, 2):
            combo_scores[combo] = C.evaluate(list(combo) + board)
        best_combo, best_sc = max(combo_scores.items(), key=lambda kv: kv[1])
        holder = next((p["seat"] for p in known
                       if now_score[p["seat"]] == best_sc), None)
        nuts_info = {"desc": f"{best_combo[0]}{best_combo[1]} {C.CATEGORY_ZH[C.category(best_sc)]}",
                     "holder": holder}

    # ---------- 下一张危险牌条 + 干净/被污染补牌 (flop/turn) ----------
    # out 定义: 落后者拿到后能击败对手【当前】牌力的下一张牌;
    # 其中对手【同时】改善并反压的为 tainted ("你成花、他恰好成船"), 其余为 clean。
    board_next = None
    outs = {}
    if board and to_come >= 1 and god and len(known) >= 2:
        board_next = {"flush": 0, "straight": 0, "pair_board": 0, "overcard": 0}
        behind = [p["seat"] for p in known
                  if now_score[p["seat"]] < max(now_score.values())]
        outs = {s: {"clean": 0, "tainted": 0} for s in behind}
        windows_now = _straight_windows(board)
        b_ranks = {c[0] for c in board}
        b_max = max(C.rank_of(c) for c in board)
        for c in unseen:
            suit_n = sum(1 for b in board if b[1] == c[1]) + 1
            if suit_n >= 3:
                board_next["flush"] += 1
            if _straight_windows(board + [c]) - windows_now:
                board_next["straight"] += 1
            if c[0] in b_ranks:
                board_next["pair_board"] += 1
            if C.rank_of(c) > b_max:
                board_next["overcard"] += 1
            sc_next = {p["seat"]: C.evaluate(p["hole"] + board + [c]) for p in known}
            for s in behind:
                opp_now = max(sc for t, sc in now_score.items() if t != s)
                opp_next = max(sc for t, sc in sc_next.items() if t != s)
                if sc_next[s] > opp_now:                       # 反超对手当前牌力
                    clean = sc_next[s] > opp_next              # 对手没有恰好改善反压
                    outs[s]["clean" if clean else "tainted"] += 1
        board_next = {k: round(v / len(unseen), 4) for k, v in board_next.items()}

    # ---------- 牌力分位 / 第 N 坚果 (逐玩家, 复用 combo_scores) ----------
    players_out = []
    for p in active:
        s = p["seat"]
        a = acc[s]
        entry = {"seat": s,
                 "equity": {"win": round(a["win"] / n_runs, 4), "tie": round(a["tie"] / n_runs, 4)},
                 "final_dist": {k: round(v / n_runs, 4) for k, v in a["cats"].items() if v},
                 "now": None, "outs": None, "comeback": None}
        if s in now_score:
            sc = now_score[s]
            others = [(combo, csc) for combo, csc in combo_scores.items()
                      if p["hole"][0] not in combo and p["hole"][1] not in combo]
            beat = sum(1 for _, csc in others if sc > csc)
            distinct_above = len({csc for _, csc in combo_scores.items() if csc > sc})
            ahead = god and len(known) >= 2 and sc == max(now_score.values())
            entry["now"] = {"cat": C.category(sc), "cat_zh": C.CATEGORY_ZH[C.category(sc)],
                            "hs_pct": round(beat / len(others), 4),
                            "nut_rank": distinct_above + 1,
                            "ahead": ahead if god and len(known) >= 2 else None}
            if god and len(known) >= 2 and not ahead:
                entry["comeback"] = round(a["eq"] / n_runs, 4)
            if s in outs:
                entry["outs"] = outs[s]
        players_out.append(entry)

    # ---------- 行动者赔率 ----------
    actor_out = None
    if actor and actor.get("to_call") is not None:
        to_call, apot = actor["to_call"], actor.get("pot", pot)
        eff = actor.get("eff_stack", 0)
        actor_out = {"seat": actor["seat"], "to_call": to_call,
                     "pot_odds": round(to_call / (apot + to_call), 4) if to_call else 0.0,
                     "required_eq": round(to_call / (apot + to_call), 4) if to_call else 0.0,
                     "spr": round(eff / apot, 2) if apot else None}
        if to_call:  # 面对下注: MDF 与 α (pot 已含对手下注)
            pot_before = max(apot - to_call, 1)
            alpha = to_call / (to_call + pot_before)
            actor_out["alpha"] = round(alpha, 4)
            actor_out["mdf"] = round(1 - alpha, 4)

    return {
        "type": "analysis_update", "street": street,
        "mode": "god" if god else "public",
        "board": board,
        "board_next": board_next,
        "deck_remaining": ({s: sum(1 for c in unseen if c[1] == s) for s in C.SUITS}
                           if god else None),
        "players": players_out,
        "actor": actor_out,
        "nuts": nuts_info if god else None,
        "n_runouts": n_runs,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _straight_windows(cards) -> set[int]:
    """可被两张手牌补全的顺子窗口集合: 含 >=3 个不同点数的 5 连窗口 (轮子 A 计低)。"""
    ranks = {C.rank_of(c) for c in cards}
    if 12 in ranks:
        ranks.add(-1)  # A 作 1
    wins = set()
    for lo in range(-1, 9):
        if len(ranks & set(range(lo, lo + 5))) >= 3:
            wins.add(lo)
    return wins


def emit_update(bus, engine):
    """从引擎取信息集, 计算并广播 analysis_update。引擎每次变化后调用。"""
    snap = engine.snapshot()
    if len(snap["board"]) in (1, 2):
        return None  # 翻牌发到一半, 等三张齐再算
    players = [{"seat": x["seat"], "hole": x["hole"], "in_hand": x["in_hand"]}
               for x in snap["seats"]]
    if sum(1 for p in players if p["in_hand"]) < 2:
        return None
    if not any(p["hole"] for p in players if p["in_hand"]):
        return None  # 翻前公开模式无可算内容
    actor = None
    if snap["actor"] is not None:
        la = engine.legal_actions()
        call = next((a for a in la if a["action"] in ("call", "check")), None)
        seat = snap["actor"]
        actor = {"seat": seat, "to_call": (call.get("amount", 0) if call else 0),
                 "pot": snap["pot"],
                 "eff_stack": snap["seats"][seat]["stack"]}
    payload = analyze(players, snap["board"], pot=snap["pot"], actor=actor)
    return bus.emit(payload)
