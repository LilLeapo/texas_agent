"""M5 · 编排器 (Agent 本体)。

感知→决策→行动闭环, "行动"= 指挥人类荷官(提词)后用视觉核验。
所有外设都是可插拔适配器 —— executor: robot|human 的热切换、
现场相机/键盘/操作员与测试桩共用同一循环, 代码零改动。

每个分支进出发 agent_trace; 纠错走 correction(引擎重放实现)。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import analysis
from . import cards as C
from .engine import Engine, Need
from .gto import GtoCharts
from .prompter import Prompter

UNCERTAIN = "uncertain"


class HandRestart(RuntimeError):
    """操作员请求重开本手(网页按钮): 各阻塞点抛出, live_cli 捕获后整进程重启。"""


@dataclass
class WatchEvent:
    ok: bool = True
    timeout: bool = False
    wrong_zone: str | None = None   # 牌实际落在的区域(与期望不符时)


# ---------- 适配器协议的默认桩 (现场换成相机/键盘/操作员台) ----------

class MockVision:
    """无相机运行: 一切期望立即视为达成。script 可注入 wrong_zone/timeout 序列。"""

    def __init__(self, script: list[WatchEvent] | None = None):
        self.script = list(script or [])

    def watch(self, zones: list[str], expect: str, timeout: float = 20) -> WatchEvent:
        return self.script.pop(0) if self.script else WatchEvent()

    def read_card(self, zone: str) -> str:
        return UNCERTAIN  # 无读牌相机: 交操作员补录


class PresetGod:
    """预排牌序 (god_source: preset): 按 deck_order 逐张供牌, 相机降级为核验。"""

    def __init__(self, deck_order: list[str]):
        self.deck = [C.normalize(c) for c in deck_order]
        if len(set(self.deck)) != len(self.deck):
            raise ValueError("deck_order 含重复牌")
        self.i = 0

    @classmethod
    def from_file(cls, path) -> "PresetGod":
        tokens = []
        for line in open(path, encoding="utf-8"):
            line = line.split("#")[0]
            tokens += line.split()
        return cls(tokens)

    def next_card(self, kind: str | None = None) -> str:
        if self.i >= len(self.deck):
            raise RuntimeError("deck_order 耗尽")
        c = self.deck[self.i]
        self.i += 1
        return c


class NoneGod:
    """公开模式: 无靴口信息。"""

    def next_card(self, kind: str | None = None) -> str | None:
        return None


class KeyboardInputs:
    """键盘申报动作(主路)。语音申报是弹性项, 接口相同。"""

    def get(self, seat: int, legal: list[dict]) -> tuple[str, int | None]:
        opts = " / ".join(_fmt_action(a) for a in legal)
        while True:
            raw = input(f"  P{seat + 1} 行动 [{opts}]: ").strip().lower().split()
            try:
                return _parse_action(raw, legal)
            except ValueError as e:
                print(f"  ✗ {e}")


class ScriptedInputs:
    def __init__(self, actions: list[tuple[str, int | None]]):
        self.actions = list(actions)

    def get(self, seat: int, legal: list[dict]) -> tuple[str, int | None]:
        return self.actions.pop(0)


class ConsoleOps:
    """操作员台: 确认弹窗 + 两键补牌。"""

    def confirm(self, question: str) -> bool:
        return input(f"  ❓ {question} [y/n]: ").strip().lower() in ("y", "yes", "")

    def ask_card(self, context: str) -> str:
        while True:
            raw = input(f"  ⌨️ 补录牌面 ({context}), 如 As/Th: ").strip()
            try:
                return C.normalize(raw)
            except ValueError:
                print("  ✗ 无效牌面")


class ScriptedOps:
    def __init__(self, cards: list[str] | None = None, confirms: list[bool] | None = None):
        self.cards, self.confirms = list(cards or []), list(confirms or [])

    def confirm(self, question: str) -> bool:
        return self.confirms.pop(0) if self.confirms else True

    def ask_card(self, context: str) -> str:
        return self.cards.pop(0)


def _fmt_action(a: dict) -> str:
    if a["action"] == "call":
        return f"call {a['amount']}(c)"
    if a["action"] == "raise":
        return f"raise 金额(r n), {a['min']}~{a['max']}"
    return f"{a['action']}({a['action'][0]})"


def _parse_action(raw: list[str], legal: list[dict]) -> tuple[str, int | None]:
    if not raw:
        raise ValueError("请输入动作")
    names = {a["action"] for a in legal}
    word = raw[0]
    full = {"f": "fold", "c": "call", "k": "check", "r": "raise"}.get(word, word)
    if full == "call" and "check" in names:
        full = "check"
    if full not in names:
        raise ValueError(f"非法动作 {word}")
    if full == "raise":
        if len(raw) < 2:
            raise ValueError("raise 需要金额, 如: r 300")
        return "raise", int(raw[1])
    return full, None


# ---------- 编排器 ----------

class Orchestrator:
    def __init__(self, bus, engine: Engine, vision=None, god=None, inputs=None,
                 ops=None, prompter: Prompter | None = None,
                 charts: GtoCharts | None = None, expect_timeout: float = 20,
                 vlm=None, arm=None):
        self.bus = bus
        self.engine = engine
        self.vision = vision or MockVision()
        self.god = god or NoneGod()
        self.inputs = inputs or KeyboardInputs()
        self.ops = ops or ConsoleOps()
        self.prompter = prompter or Prompter(bus)
        self.charts = charts or GtoCharts()
        self.expect_timeout = expect_timeout
        self.vlm = vlm  # 认牌仲裁员(VlmCardReader), 可缺席; 兜底链居中, 死了跳过
        self.arm = arm  # ArmClient: 有臂则发牌动作由臂执行, 失败自动落回人肉提词
        self._cur_street: str | None = None   # 街转换检测 → god.new_phase

    def _trace(self, text: str):
        self.bus.emit({"type": "agent_trace", "text": text})

    def run_hand(self) -> dict:
        """跑完一手, 返回结算快照。"""
        while True:
            need = self.engine.next_required()
            if need.kind == "DEAL":
                self._deal(need)
            elif need.kind == "AWAIT":
                self._await(need)
            elif need.kind == "SHOWDOWN":
                self._showdown(need)
            else:
                self._trace("本手结束")
                return self.engine.snapshot()

    # ---- DEAL: 提词 → 视觉核验 → 定牌面 → 录入 ----

    def _deal(self, need: Need):
        if need.street != self._cur_street:
            self._cur_street = need.street
            if hasattr(self.god, "new_phase"):
                # 流式读牌: 先划阶段界(丢上一街杂散窗)再提交臂程序, 序号不跨街错位
                self.god.new_phase(need.street)
        if self.arm is not None and hasattr(self.arm, "on_need"):
            self.arm.on_need(need)   # 宏观臂: 阶段首卡时提交整段轨迹/程序
        if need.subkind == "burn" and getattr(self.arm, "skip_burn", False):
            # 程序制机器流程无实体烧牌: 逻辑烧牌自动记账, 不提词不核验
            self._trace("机器流程无实体烧牌, 逻辑烧牌自动记录")
            self.engine.record_deal(C.UNKNOWN, need.zones[0])
            return
        # 先取牌面: 预排立即返回; 靴口模式在此完成"亮牌→VLM 认牌"交互(烧牌不亮)
        card = self.god.next_card(kind=need.subkind)
        if self.arm is not None and self.arm.alive:
            ok = self.arm.deal_to(need.zones[0],
                                  face=need.face or "down")
            if not ok:
                self.bus.emit({"type": "alert",
                               "text": f"机械臂执行失败, 请荷官接管: {need.prompt_text()}"})
        robot = self.arm is not None and getattr(self.arm, "alive", False)
        if robot and need.subkind == "hole" and getattr(self.arm, "skip_burn", False):
            # 批次程序臂: 8 张底牌一气呵成, 顶视区域框待校且逐张 watch 拖慢全程;
            # 落位由臂程序保证, 牌面识别在过牌位旁路完成 → 顶视核验跳过
            self._trace(f"臂程序发底牌 → {need.zones[0]} (顶视逐张核验跳过)")
            self.engine.record_deal(card or C.UNKNOWN, need.zones[0],
                                    source=getattr(self.god, "last_via", None))
            analysis.emit_update(self.bus, self.engine)
            return
        self._trace(f"提示荷官: {need.prompt_text()}; 等待 {'/'.join(need.zones)}")
        self.prompter.prompt(need.prompt_text())
        retried = False
        while True:
            ev = self.vision.watch(need.zones, expect="appear", timeout=self.expect_timeout)
            if ev.timeout:
                if not retried:
                    retried = True
                    if not robot:
                        self.prompter.prompt(need.prompt_text(), level="again")
                    continue
                if robot:
                    # 机械臂发牌是确定性的: 落位没观测到多半是区域框偏差(物理待调),
                    # 记录告警但照常推进, 监控永不卡死
                    self._trace(f"落位未观测到(区域框偏差?), 信任机械臂: {need.zones[0]}")
                    self.bus.emit({"type": "alert",
                                   "text": f"落位未观测到: {need.zones[0]} (区域框待调)"})
                    break
                self.prompter.prompt(f"荷官超时未响应: {need.prompt_text()}", level="alert")
                self.bus.emit({"type": "alert", "text": f"发牌超时: {need.prompt_text()}"})
                continue
            if ev.wrong_zone:
                if robot:
                    # 臂不需要人来确认: 记录偏差, 按期望区域入账(逻辑座位不受影响)
                    self._trace(f"落点偏差: 期望 {need.zones[0]}, 观测 {ev.wrong_zone}"
                                f" (物理位置待调, 按期望记录)")
                    break
                if self.ops.confirm(f"看到牌落在 {ev.wrong_zone}, 期望是 {need.zones[0]}, 按 {ev.wrong_zone} 记录?"):
                    self.bus.emit({"type": "correction", "author": "ops",
                                   "field": "zone", "old": need.zones[0], "new": ev.wrong_zone})
                    need.zones[0] = ev.wrong_zone
                    break
                self.prompter.prompt(f"请把牌移到 {need.zones[0]}", level="again")
                continue
            break

        if need.subkind == "burn":
            self.engine.record_deal(C.UNKNOWN, need.zones[0])   # 烧牌对信息集永远未知
            return
        source = getattr(self.god, "last_via", None)   # 靴口: yolo/yolo+vlm/vlm/ops
        if card is None and need.face == "up":
            card = self.vision.read_card(need.zones[0])   # 公开模式读板面
        if card in (None, UNCERTAIN) and need.face == "up":
            card, source = self._arbitrate(need.zones[0]), "vlm"
        if card in (None, UNCERTAIN) and need.face == "up":
            self._trace("识别不确定, 暂停推进, 请操作员补录")
            card, source = self.ops.ask_card(f"{need.street} {need.zones[0]}"), "ops"
        self.engine.record_deal(card or C.UNKNOWN, need.zones[0], source=source)
        analysis.emit_update(self.bus, self.engine)

    def _arbitrate(self, zone: str) -> str:
        """认牌仲裁: NCC 不确定 → VLM 第二意见。失联/仍不确定返 uncertain。"""
        if self.vlm is None:
            return UNCERTAIN
        card = self.vlm.read_card(zone)
        if card not in (None, UNCERTAIN):
            self._trace(f"NCC 不确定, VLM 仲裁认定 {card}")
            return card
        return UNCERTAIN

    # ---- AWAIT: input_request → GTO 基线 → 应用 → 偏差 ----

    def _await(self, need: Need):
        legal = self.engine.legal_actions()
        self._trace(f"等待 P{need.seat + 1} 行动")
        self.bus.emit({"type": "input_request", "seat": need.seat,
                       "legal": [_fmt_action(a) for a in legal], "timeout_s": 30})
        hint = self.charts.hint(self.engine, need.seat)
        if hint:
            self.bus.emit(hint)
        action, amount = self.inputs.get(need.seat, legal)
        self.engine.apply(action, amount, seat=need.seat)
        dev = GtoCharts.deviation_from_hint(hint, action)
        if dev:
            self.bus.emit(dev)
        analysis.emit_update(self.bus, self.engine)

    # ---- SHOWDOWN (公开模式手动亮牌) ----

    def _showdown(self, need: Need):
        self._trace(f"摊牌: P{need.seat + 1}")
        cards = []
        for i in ("第1张", "第2张"):
            c = self.vision.read_card(f"P{need.seat + 1}")
            if c == UNCERTAIN:
                c = self._arbitrate(f"P{need.seat + 1}")
            if c == UNCERTAIN:
                c = self.ops.ask_card(f"P{need.seat + 1} 摊牌{i}")
            cards.append(c)
        self.engine.record_showdown(need.seat, cards)
        analysis.emit_update(self.bus, self.engine)
