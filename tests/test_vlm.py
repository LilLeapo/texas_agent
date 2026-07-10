"""慢循环 VLM 验收: 仲裁员/审计员各自可死, 死了下层接住, 主循环零感知。"""

import pytest

from texas_agent.bus import LocalBus
from texas_agent.engine import Engine
from texas_agent.gto import GtoCharts
from texas_agent.llm import LlmClient, from_config
from texas_agent.orchestrator import (MockVision, NoneGod, Orchestrator,
                                      ScriptedInputs, ScriptedOps)
from texas_agent.prompter import Prompter
from texas_agent.vlm import DealAuditor, VlmCardReader, parse_card_reply


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    b.msgs = []
    b.subscribe(b.msgs.append)
    yield b
    b.close()


class FakeClient:
    """按脚本回话的假 LLM 端点; replies 耗尽或 None 模拟失联。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.prompts = []

    def ask(self, prompt, image_bgr=None, timeout_s=None):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


# ---------- 回复解析: 宁可不确定, 不可错判 ----------

def test_parse_card_reply():
    assert parse_card_reply("Qh") == "Qh"
    assert parse_card_reply("这张是 10h。") == "Th"
    assert parse_card_reply("答案: as") == "As"
    assert parse_card_reply("不确定") is None
    assert parse_card_reply("可能是 Qh, 但不确定") is None   # 犹豫一律不采信
    assert parse_card_reply("一张红色的牌") is None
    assert parse_card_reply(None) is None


# ---------- 认牌仲裁员 ----------

def test_reader_resolves_card():
    reader = VlmCardReader(FakeClient(["Qh"]), image_source=lambda z: object())
    assert reader.read_card("C4") == "Qh"


def test_reader_degrades_to_uncertain():
    dead = VlmCardReader(FakeClient([]), image_source=lambda z: object())
    assert dead.read_card("C4") == "uncertain"          # 端点失联
    noimg = VlmCardReader(FakeClient(["Qh"]), image_source=lambda z: None)
    assert noimg.read_card("C4") == "uncertain"         # 拿不到图

    def boom(zone):
        raise RuntimeError("camera gone")
    broken = VlmCardReader(FakeClient(["Qh"]), image_source=boom)
    assert broken.read_card("C4") == "uncertain"        # 任何异常不外溢


def test_reader_color_crosscheck():
    """像素红黑 vs VLM 花色矛盾 → 不采信 (实测抓到过黑桃 A 认成红桃 A)。"""
    import numpy as np
    red_card = np.full((300, 200, 3), 250, np.uint8)
    red_card[40:120, 40:160] = (40, 40, 220)            # 一块红墨
    reader = VlmCardReader(FakeClient(["As", "Ah"]), image_source=lambda z: red_card)
    assert reader.read_card("C4") == "uncertain"        # 红图报黑桃: 否决
    assert reader.read_card("C4") == "Ah"               # 红图报红桃: 通过
    blank = np.full((300, 200, 3), 250, np.uint8)
    reader2 = VlmCardReader(FakeClient(["As"]), image_source=lambda z: blank)
    assert reader2.read_card("C4") == "As"              # 墨量不足: 判不了, 不否决


def test_reader_two_step_fallback():
    """单发不确定 → 拆点数+花色两问 (实测人头牌单发常保守)。"""
    client = FakeClient(["不确定", "J", "黑桃"])
    reader = VlmCardReader(client, image_source=lambda z: object())
    assert reader.read_card("C2") == "Js"
    assert len(client.prompts) == 3
    # 两步里任何一步不确定 → 整体不确定
    reader2 = VlmCardReader(FakeClient(["不确定", "J", "不确定"]),
                            image_source=lambda z: object())
    assert reader2.read_card("C2") == "uncertain"


# ---------- 兜底链: vision → VLM → ops ----------

PUBLIC_SCRIPT = [("call", None), ("check", None)] + [("check", None)] * 6
BOARD_AND_SHOWDOWN = ["Jh", "8h", "3c", "Qc", "6s", "Ah", "Kh", "Kd", "Ks"]


def build_public(bus, vlm, ops_cards=()):
    engine = Engine(bus, n_players=2, stacks=(1000, 1000), blinds=(50, 100),
                    mode="public")
    return Orchestrator(bus, engine, vision=MockVision(), god=NoneGod(),
                        inputs=ScriptedInputs(list(PUBLIC_SCRIPT)),
                        ops=ScriptedOps(cards=list(ops_cards)),
                        prompter=Prompter(bus), charts=GtoCharts(), vlm=vlm)


def test_vlm_arbitrates_public_hand(bus):
    """公开模式整手: NCC 全不确定, VLM 仲裁认下全部板面+摊牌, 操作员零介入。"""
    reader = VlmCardReader(FakeClient(list(BOARD_AND_SHOWDOWN)),
                           image_source=lambda z: object())
    snap = build_public(bus, vlm=reader).run_hand()
    assert snap["complete"]
    deals = [m for m in bus.msgs if m["type"] == "game_event" and m["event"] == "deal"]
    board_deals = [m for m in deals if m["detail"]["deal_kind"] == "board"]
    assert [m["detail"]["card"] for m in board_deals] == ["Jh", "8h", "3c", "Qc", "6s"]
    assert all(m["detail"]["source"] == "vlm" for m in board_deals)


def test_dead_vlm_falls_to_ops(bus):
    """Spark 失联: 仲裁一律不确定 → 操作员补录, 事件标 source: ops, 零报错。"""
    dead = VlmCardReader(FakeClient([]), image_source=lambda z: object())
    snap = build_public(bus, vlm=dead, ops_cards=BOARD_AND_SHOWDOWN).run_hand()
    assert snap["complete"]
    board_deals = [m for m in bus.msgs if m["type"] == "game_event"
                   and m["event"] == "deal" and m["detail"]["deal_kind"] == "board"]
    assert all(m["detail"]["source"] == "ops" for m in board_deals)


def test_no_vlm_configured_falls_to_ops(bus):
    """未接 VLM(vlm=None): 与旧行为一致, 直接操作员补录, 不标 source: vlm。"""
    snap = build_public(bus, vlm=None, ops_cards=BOARD_AND_SHOWDOWN).run_hand()
    assert snap["complete"]
    assert not any(m.get("detail", {}).get("source") == "vlm"
                   for m in bus.msgs if m["type"] == "game_event")


# ---------- 发牌审计员 ----------

def deal_msg(board, in_hand=2):
    seats = [{"seat": i, "in_hand": i < in_hand, "hole": None} for i in range(4)]
    return {"type": "game_event", "event": "deal",
            "detail": {"deal_kind": "board" if board else "hole"},
            "state": {"street": "flop", "board": board, "seats": seats}}


def run_auditor(bus, replies, msg):
    auditor = DealAuditor(bus, FakeClient(replies), frame_source=lambda: object())
    auditor.feed(msg)
    auditor._pool.shutdown(wait=True)
    return auditor


def test_auditor_match(bus):
    auditor = run_auditor(bus, ["一致"], deal_msg(["Jh", "8h", "3c"]))
    reports = [m for m in bus.msgs if m["type"] == "audit_report"]
    assert reports and reports[0]["verdict"] == "match"
    assert not any(m["type"] == "alert" for m in bus.msgs)
    assert "Jh 8h 3c" in auditor.client.prompts[0]   # 对答案, 不是开放提问


def test_auditor_mismatch_alerts(bus):
    run_auditor(bus, ["不一致: 公共牌区只有 2 张明牌"], deal_msg(["Jh", "8h", "3c"]))
    reports = [m for m in bus.msgs if m["type"] == "audit_report"]
    assert reports and reports[0]["verdict"] == "mismatch"
    assert "2 张" in reports[0]["note"]
    assert any(m["type"] == "alert" for m in bus.msgs)


def test_auditor_silent_when_dead_or_unsure(bus):
    run_auditor(bus, [], deal_msg(["Jh"]))            # 失联
    run_auditor(bus, ["不确定"], deal_msg(["Jh"]))     # 看不清
    assert not any(m["type"] in ("audit_report", "alert") for m in bus.msgs)


def test_auditor_ignores_burn_and_other_msgs(bus):
    auditor = DealAuditor(bus, FakeClient(["一致"]), frame_source=lambda: object())
    auditor.feed({"type": "game_event", "event": "deal",
                  "detail": {"deal_kind": "burn"}, "state": {}})
    auditor.feed({"type": "analysis_update"})
    auditor._pool.shutdown(wait=True)
    assert not auditor.client.prompts


def test_auditor_skips_stale_backlog(bus):
    """积压时只审计最新一次发牌: 旧工单在新 deal 到达后作废。"""
    auditor = DealAuditor(bus, FakeClient(["一致", "一致"]),
                          frame_source=lambda: object())
    auditor._latest = 5
    auditor._audit(3, deal_msg(["Jh"]))   # 过期工单
    assert not auditor.client.prompts


# ---------- 统一客户端 ----------

def test_llm_client_never_raises():
    c = LlmClient("http://127.0.0.1:9", "m", timeout_s=0.3)
    assert c.ask("hi") is None            # 连不上: 静默 None


def test_from_config(tmp_path):
    assert from_config(str(tmp_path / "missing.yaml")) is None
    empty = tmp_path / "t.yaml"
    empty.write_text("llm:\n  base_url: ''\n", encoding="utf-8")
    assert from_config(str(empty)) is None    # base_url 留空 = 禁用
    full = tmp_path / "t2.yaml"
    full.write_text("llm:\n  base_url: http://x:11434/v1\n  model: m\n",
                    encoding="utf-8")
    client = from_config(str(full))
    assert client and client.base_url == "http://x:11434/v1" and client.model == "m"
