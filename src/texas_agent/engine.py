"""M1 · 规则引擎适配器 (PokerKit NLHE 之上的薄层)。

职责:
- next_required(): 告诉编排器"下一个应该发生的事件" —— DEAL / AWAIT / SHOWDOWN / COMPLETE。
  这是"期望驱动感知"的源头: 视觉只核验这里给出的期望。
- record_deal / apply / record_showdown: 接收现实世界发生的事, 推进状态。
- 每次变化 emit game_event(含全量快照); amend() 用操作重放实现赛后纠错(审计链保真)。

座位约定(与 PokerKit 一致): 0=小盲, 1=大盲, ..., n-1=庄位; 单挑时 0=庄位兼小盲。
对外展示用 1 基座位号 P1..Pn。底牌区 P{座位}a/b, 公共牌区 C1~C5。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pokerkit import Automation, NoLimitTexasHoldem

from . import cards as C

STREETS = ["preflop", "flop", "turn", "river"]

POSITIONS = {  # 座位 → 位置名 (GTO 节点键用), 索引即引擎座位号
    2: ["BB", "BTN"],  # 单挑: PokerKit 里 0 位收大盲, 1 位是庄位兼小盲、翻前先行动
    3: ["SB", "BB", "BTN"],
    4: ["SB", "BB", "UTG", "BTN"],
    5: ["SB", "BB", "UTG", "CO", "BTN"],
    6: ["SB", "BB", "UTG", "HJ", "CO", "BTN"],
    7: ["SB", "BB", "UTG", "LJ", "HJ", "CO", "BTN"],
    8: ["SB", "BB", "UTG", "MP", "LJ", "HJ", "CO", "BTN"],
    9: ["SB", "BB", "UTG", "UTG1", "MP", "LJ", "HJ", "CO", "BTN"],
}


@dataclass
class Need:
    kind: str                    # DEAL | AWAIT | SHOWDOWN | COMPLETE
    subkind: str | None = None   # DEAL: burn | hole | board
    zones: list[str] = field(default_factory=list)
    seat: int | None = None      # 0 基
    face: str | None = None      # down | up
    street: str = "preflop"

    def prompt_text(self) -> str:
        """给荷官提词器的中文指令文案。"""
        if self.kind == "DEAL":
            if self.subkind == "burn":
                return "烧牌"
            if self.subkind == "hole":
                return f"发底牌: P{self.seat + 1} → {self.zones[0]}"
            street_zh = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}[self.street]
            return f"发{street_zh} → {' '.join(self.zones)}"
        if self.kind == "AWAIT":
            return f"等待 P{self.seat + 1} 行动"
        if self.kind == "SHOWDOWN":
            return f"P{self.seat + 1} 亮牌或弃牌"
        return "本手结束"


class Engine:
    def __init__(self, bus, n_players: int = 4, stacks=None, blinds=(50, 100),
                 mode: str = "god", hand_no: int = 1):
        self.bus = bus
        self.n = n_players
        self.blinds = tuple(blinds)
        self.mode = mode
        self.hand_no = hand_no
        self.starting_stacks = tuple(stacks or (20000,) * n_players)
        self.oplog: list[tuple] = []   # 重放日志: ('deal',card,zone) ('action',seat,act,amt) ('show',seat,cards)
        self._hole_counts = [0] * n_players
        self._holes: list[list[str]] = [[] for _ in range(n_players)]  # 自行跟踪, 不受结算 kill 影响
        self._build_state()
        self._emit("hand_start", {"hand_no": hand_no, "blinds": list(self.blinds),
                                  "positions": POSITIONS[n_players]})

    # ---------- 状态构建与重放 ----------

    def _build_state(self):
        autos = [
            Automation.ANTE_POSTING, Automation.BET_COLLECTION,
            Automation.BLIND_OR_STRADDLE_POSTING, Automation.HAND_KILLING,
            Automation.CHIPS_PUSHING, Automation.CHIPS_PULLING,
        ]
        if self.mode in ("god", "preset"):
            # 上帝模式引擎知道全部底牌, 摊牌自动进行; 公开模式摊牌需人工/视觉录入
            autos.append(Automation.HOLE_CARDS_SHOWING_OR_MUCKING)
        self.state = NoLimitTexasHoldem.create_state(
            tuple(autos), True, 0, self.blinds, self.blinds[1],
            self.starting_stacks, self.n,
        )

    def _replay(self, oplog: list[tuple]):
        """从头重放操作日志 —— amend 纠错的实现基础。"""
        self._build_state()
        self._hole_counts = [0] * self.n
        self._holes = [[] for _ in range(self.n)]
        self.oplog = []
        for op in oplog:
            if op[0] == "deal":
                self.record_deal(op[1], op[2], _quiet=True)
            elif op[0] == "action":
                self.apply(op[2], op[3], seat=op[1], _quiet=True)
            elif op[0] == "show":
                self.record_showdown(op[1], op[2], _quiet=True)

    # ---------- 期望 ----------

    def next_required(self) -> Need:
        s = self.state
        street = self.street_name()
        if s.can_burn_card():
            return Need("DEAL", "burn", ["MUCK"], street=street)
        if s.can_deal_hole():
            seat = s.hole_dealee_index
            zone = f"P{seat + 1}{'ab'[self._hole_counts[seat]]}"
            return Need("DEAL", "hole", [zone], seat=seat, face="down", street=street)
        if s.can_deal_board():
            done = len(self.board())
            total = done + s.board_dealing_count
            zones = [f"C{i + 1}" for i in range(done, total)]
            return Need("DEAL", "board", zones, face="up", street=street)
        if s.actor_index is not None:
            return Need("AWAIT", seat=s.actor_index, street=street)
        # 未知底牌时无参 can_show_or_muck 返回 False, 用 showdown_index 判定摊牌待办
        if s.status and s.showdown_index is not None:
            return Need("SHOWDOWN", seat=s.showdown_index, street=street)
        return Need("COMPLETE", street=street)

    # ---------- 现实事件录入 ----------

    def record_deal(self, card: str | None, zone: str | None = None,
                    source: str | None = None, _quiet=False):
        s = self.state
        card = C.normalize(card) if card and card != C.UNKNOWN else C.UNKNOWN
        if s.can_burn_card():
            s.burn_card("??")
            kind = "burn"
        elif s.can_deal_hole():
            seat = s.hole_dealee_index
            self._hole_counts[seat] += 1
            self._holes[seat].append(card)
            s.deal_hole(card if card != C.UNKNOWN else "??")
            kind = "hole"
        elif s.can_deal_board():
            if card == C.UNKNOWN:
                raise ValueError("公共牌必须是已知牌面")
            s.deal_board(card)
            kind = "board"
        else:
            raise RuntimeError("当前不需要发牌")
        self.oplog.append(("deal", card, zone))
        if not _quiet:
            detail = {"deal_kind": kind, "card": card, "zone": zone,
                      "board": self.board()}
            if source:
                detail["source"] = source  # vlm(仲裁) / ops(补录), 缺省为主路
            self._emit("deal", detail)
            self._maybe_complete()

    def legal_actions(self) -> list[dict]:
        s = self.state
        acts: list[dict] = []
        if s.actor_index is None:
            return acts
        if s.can_fold():
            acts.append({"action": "fold"})
        if s.can_check_or_call():
            amt = s.checking_or_calling_amount
            acts.append({"action": "check"} if amt == 0 else {"action": "call", "amount": amt})
        if s.can_complete_bet_or_raise_to():
            acts.append({"action": "raise",
                         "min": s.min_completion_betting_or_raising_to_amount,
                         "max": s.max_completion_betting_or_raising_to_amount})
        return acts

    def apply(self, action: str, amount: int | None = None, seat: int | None = None, _quiet=False):
        s = self.state
        actor = s.actor_index
        if seat is not None and seat != actor:
            raise ValueError(f"当前行动者是 P{actor + 1}, 不是 P{seat + 1}")
        if action == "fold":
            s.fold()
        elif action in ("check", "call"):
            amount = s.checking_or_calling_amount
            s.check_or_call()
        elif action == "raise":
            s.complete_bet_or_raise_to(amount)
        else:
            raise ValueError(f"未知动作: {action}")
        self.oplog.append(("action", actor, action, amount))
        if not _quiet:
            self._emit("action", {"seat": actor, "action": action, "amount": amount})
            self._maybe_complete()

    def record_showdown(self, seat: int, hole: list[str] | None, _quiet=False):
        """公开模式摊牌: hole=None 表示弃牌不亮。"""
        s = self.state
        if hole:
            hole = [C.normalize(c) for c in hole]
            s.show_or_muck_hole_cards("".join(hole))
            self._holes[seat] = list(hole)
        else:
            s.show_or_muck_hole_cards(False)
        self.oplog.append(("show", seat, hole))
        if not _quiet:
            self._emit("showdown", {"seat": seat, "hole": hole})
            self._maybe_complete()

    def amend(self, op_index: int, new_card: str, author: str = "ops"):
        """纠错: 把第 op_index 条发牌操作的牌面改为 new_card, 全量重放。"""
        old = self.oplog[op_index]
        if old[0] != "deal":
            raise ValueError("只支持修正发牌操作")
        fixed = list(self.oplog)
        fixed[op_index] = ("deal", C.normalize(new_card), old[2])
        self._replay(fixed)
        self._emit("correction", {"author": author, "op_index": op_index,
                                  "old": old[1], "new": C.normalize(new_card), "zone": old[2]})

    # ---------- 查询 ----------

    def street_name(self) -> str:
        i = self.state.street_index
        return STREETS[i] if i is not None else ("complete" if not self.state.status else "preflop")

    def board(self) -> list[str]:
        return [repr(c[0]) for c in self.state.board_cards]

    def hole(self, seat: int) -> list[str] | None:
        """已知底牌(引擎自行跟踪, 结算 kill 不影响); 未知(公开模式)返回 None。"""
        hc = self._holes[seat]
        if len(hc) < 2 or any(x == C.UNKNOWN for x in hc):
            return None
        return list(hc)

    def payoffs(self) -> list[int]:
        return [self.state.stacks[i] - self.starting_stacks[i] for i in range(self.n)]

    def position(self, seat: int) -> str:
        return POSITIONS[self.n][seat]

    def preflop_sequence(self) -> list[tuple[int, str]]:
        """翻前行动序列 [(seat, action)], GTO 节点键生成用。"""
        seq = []
        for op in self.oplog:
            if op[0] == "action":
                seq.append((op[1], op[2]))
            if op[0] == "deal" and op[2] and op[2].startswith("C"):
                break
        return seq

    def snapshot(self) -> dict:
        s = self.state
        return {
            "hand_no": self.hand_no,
            "street": self.street_name(),
            "board": self.board(),
            "pot": s.total_pot_amount,
            "actor": s.actor_index,
            "seats": [
                {
                    "seat": i, "position": self.position(i),
                    "stack": s.stacks[i], "bet": s.bets[i],
                    "in_hand": bool(s.statuses[i]),
                    "hole": self.hole(i) if self.mode in ("god", "preset") else None,
                }
                for i in range(self.n)
            ],
            "complete": not s.status,
        }

    # ---------- 内部 ----------

    def _emit(self, event: str, detail: dict):
        self.bus.emit({"type": "game_event", "event": event,
                       "detail": detail, "state": self.snapshot()})

    def _maybe_complete(self):
        if not self.state.status:
            self._emit("settlement", {"payoffs": self.payoffs(),
                                      "stacks": list(self.state.stacks)})
