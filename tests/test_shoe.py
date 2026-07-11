"""靴口读牌(无机械臂过渡形态): 亮牌→VLM 认→防重读→补录兜底, 烧牌不读。"""

import pytest

from texas_agent.bus import LocalBus
from texas_agent.orchestrator import ScriptedOps
from texas_agent.prompter import Prompter
from texas_agent.vision.shoe import ShoeGod, ShoeStream


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    b.msgs = []
    b.subscribe(b.msgs.append)
    yield b
    b.close()


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def ask(self, prompt, image_bgr=None, timeout_s=None):
        return self.replies.pop(0) if self.replies else None


def make(bus, replies, ops_cards=()):
    return ShoeGod(bus, Prompter(bus), ScriptedOps(cards=list(ops_cards)),
                   FakeClient(replies), grab=lambda: object())


def test_burn_not_read(bus, capsys):
    god = make(bus, ["Ah"])
    assert god.next_card(kind="burn") is None
    assert not any(m["type"] == "dealer_prompt" for m in bus.msgs)  # 烧牌不提示亮牌


def test_reads_real_card(bus, capsys):
    god = make(bus, ["Ah"])
    assert god.next_card(kind="hole") == "Ah"
    assert any("靴口识别: Ah" in m.get("text", "") for m in bus.msgs)


def test_duplicate_forces_reshow(bus, capsys):
    god = make(bus, ["Ah", "Ah", "Kd"])
    assert god.next_card() == "Ah"
    assert god.next_card() == "Kd"      # 第二次读到重复 Ah → 重亮 → Kd
    assert any("已发过" in m.get("text", "") for m in bus.msgs
               if m["type"] == "dealer_prompt")


def test_vote_dominant_accepts_yolo(bus):
    """真实票型 5s:50 vs 9s:2 (2026-07-11 实测) → 压倒性多数直接采信。"""
    god = make(bus, ["不该被问到"])
    card, via = god._decide({"5s": 50, "9s": 2}, {"5s": (0.8, object())})
    assert (card, via) == ("5s", "yolo")
    assert god.reader.client.replies == ["不该被问到"]   # 没劳动 VLM


def test_vote_contested_goes_to_vlm(bus):
    """真实票型 Kc:20 vs Ks:17 (黑花色K混淆) → 最高置信帧交 VLM 仲裁。"""
    god = make(bus, ["Ks"])
    card, via = god._decide({"Kc": 20, "Ks": 17},
                            {"Kc": (0.77, object()), "Ks": (0.81, object())})
    assert (card, via) == ("Ks", "yolo+vlm")


def test_vote_too_few_goes_to_vlm(bus):
    """只瞄到三两帧 → 不够压倒性, VLM 定夺; VLM 也不确定 → uncertain。"""
    god = make(bus, [])
    card, via = god._decide({"7h": 3}, {"7h": (0.7, object())})
    assert card == "uncertain"


def test_unreadable_falls_to_ops(bus, capsys):
    god = make(bus, [], ops_cards=["Qh"])   # VLM 全程失联
    assert god.next_card() == "Qh"          # 两次失败后走网页补录
    assert god.last_via == "ops"


class FakeYolo:
    def __init__(self, card, conf):
        self.card, self.last_conf = card, conf

    def read_image(self, img):
        return self.card


def test_yolo_high_conf_no_vlm(bus, capsys):
    god = make(bus, ["不该被问到"])
    god.fast = FakeYolo("Ah", 0.95)
    assert god.next_card() == "Ah"
    assert god.last_via == "yolo"
    assert god.reader.client.replies == ["不该被问到"]   # VLM 没被调用


def test_yolo_low_conf_vlm_agrees(bus, capsys):
    god = make(bus, ["Kd"])                 # VLM 复核回答一致
    god.fast = FakeYolo("Kd", 0.7)
    assert god.next_card() == "Kd"
    assert god.last_via == "yolo+vlm"


def test_yolo_low_conf_vlm_disagrees_reshow(bus, capsys):
    # 第一轮: YOLO 说 Kd(低置信) 但 VLM 说 Ks → 分歧不猜;
    # 兜底链继续: VLM 全权读也说 Ks → 采信 Ks
    god = make(bus, ["Ks", "Ks"])
    god.fast = FakeYolo("Kd", 0.7)
    assert god.next_card() == "Ks"
    assert god.last_via == "vlm"


# ---- 流式旁路 (批次程序臂: 后台线程入队, 按序消费) ----

def arr(card, votes="x:40", frame=None, via="yolo"):
    return {"card": card, "via": via if card else None,
            "votes": votes, "conf": 0.9, "frame": frame}


def make_stream(bus, replies=(), ops_cards=(), hole_map=None, timeout=0.2):
    god = ShoeGod(bus, Prompter(bus), ScriptedOps(cards=list(ops_cards)),
                  FakeClient(list(replies)), hole_map=hole_map,
                  show_timeout_s=timeout)
    god.stream = ShoeStream(None, None, bus=bus)   # 不 start 线程, 手工填 arrivals
    return god


def test_stream_window_dominant():
    """压倒性票型直接采信, 不留仲裁帧要求。"""
    a = ShoeStream.decide_window({"5s": 50, "9s": 2}, {"5s": (0.8, "F")})
    assert (a["card"], a["via"], a["frame"]) == ("5s", "yolo", "F")


def test_stream_window_contested_keeps_frame():
    """胶着票型(Kc:20 vs Ks:17)不在线程里仲裁: card=None + 最高置信帧留给消费侧。"""
    a = ShoeStream.decide_window({"Kc": 20, "Ks": 17},
                                 {"Kc": (0.77, "f1"), "Ks": (0.81, "f2")})
    assert a["card"] is None and a["frame"] == "f2"


def test_stream_hole_remap(bus):
    """臂序 P3b→P3a→P2b→P2a→P1b→P1a→P4b→P4a, 引擎序 P1a..P4a,P1b..P4b:
    引擎第 k 张 ← 臂第 hole_map[k] 张, 座位归属与实桌一致。"""
    phys = ["P3b", "P3a", "P2b", "P2a", "P1b", "P1a", "P4b", "P4a"]
    ez = [f"P{i}a" for i in (1, 2, 3, 4)] + [f"P{i}b" for i in (1, 2, 3, 4)]
    hole_map = [phys.index(z) for z in ez]
    god = make_stream(bus, hole_map=hole_map)
    cards = ["2c", "3c", "4c", "5c", "6c", "7c", "8c", "9c"]  # 到达(臂)顺序
    god.stream.arrivals = [arr(c) for c in cards]
    got = [god.next_card(kind="hole") for _ in range(8)]
    assert got == ["7c", "5c", "3c", "9c", "6c", "4c", "2c", "8c"]


def test_stream_new_phase_discards_stale(bus):
    """街界快照: 上一街的杂散窗被丢弃, 序号从本街第一张重新算。"""
    god = make_stream(bus)
    god.stream.arrivals = [arr("2h"), arr("3h")]   # 上一街残留
    god.new_phase("flop")
    god.stream.arrivals.append(arr("Ah"))
    assert god.next_card(kind="board") == "Ah"


def test_stream_timeout_alerts_and_ops(bus):
    """过牌位超时未见牌(空靴/空取/漏识别) → 报警 + 网页补录, 永不卡死。"""
    god = make_stream(bus, ops_cards=["Qh"])
    assert god.next_card(kind="board") == "Qh"
    assert god.last_via == "ops"
    assert any("疑似空靴/空取" in m.get("text", "") for m in bus.msgs
               if m["type"] == "alert")


def test_stream_contested_vlm_at_consumption(bus):
    """胶着窗在消费侧仲裁: VLM 看最高置信帧定夺, 线程不被拖住。"""
    god = make_stream(bus, replies=["Ks"])
    god.stream.arrivals = [arr(None, votes="Kc:20 Ks:17", frame=object())]
    assert god.next_card(kind="board") == "Ks"
    assert god.last_via == "yolo+vlm"


def test_stream_duplicate_goes_to_ops(bus):
    """流式误读成已发过的牌 → 不猜, 转补录。"""
    god = make_stream(bus, ops_cards=["Td"])
    god.dealt.add("Ah")
    god.stream.arrivals = [arr("Ah")]
    assert god.next_card(kind="board") == "Td"
    assert god.last_via == "ops"


def test_stream_board_static_scan_before_ops(bus):
    """公共牌飞行识别未定 → 人工前先顶视静置扫描: 检出牌−已发牌=新牌。
    (2026-07-11 实测 C3/C4 飞行帧全胶着, 而静置明牌清晰可辨)"""
    god = make_stream(bus, ops_cards=["不该问到人工"])
    god.dealt = {"Qh", "9d"}
    god.board_scan = lambda: {"Qh": 0.9, "9d": 0.8, "2d": 0.85}
    god.stream.arrivals = [arr(None, votes="9d:9 2d:8", frame=object())]
    assert god.next_card(kind="board") == "2d"
    assert god.last_via == "top-yolo"


def test_stream_preview_hole_zone_immediately(bus):
    """捕获即预览: 不等引擎按序入账(重映射会攒到第6张才涌出),
    到达序号→物理牌位立刻上总线, 前端实时画牌。"""
    god = make_stream(bus, hole_map=[5, 3, 1, 7, 4, 2, 0, 6])
    god.phys_order = ["P3b", "P3a", "P2b", "P2a", "P1b", "P1a", "P4b", "P4a"]
    god.stream.on_push = god._preview
    god.new_phase("preflop")
    god.stream._push(arr("9c"))
    pv = [m for m in bus.msgs if m["type"] == "deal_preview"]
    assert pv and (pv[0]["zone"], pv[0]["card"]) == ("P3b", "9c")


def test_stream_preview_board_zone(bus):
    """转牌街第 1 个到达 → C4; 超出本街张数的杂散窗不预览。"""
    god = make_stream(bus)
    god.stream.on_push = god._preview
    god.new_phase("turn")
    god.stream._push(arr("Jd"))
    god.stream._push(arr("As"))    # 杂散(转牌只有一张)
    pv = [m for m in bus.msgs if m["type"] == "deal_preview"]
    assert len(pv) == 1 and (pv[0]["zone"], pv[0]["card"]) == ("C4", "Jd")


def test_stream_board_scan_ambiguous_falls_to_ops(bus):
    """静置扫描检出两张未知牌(异常) → 不猜, 按时限转人工。"""
    god = make_stream(bus, ops_cards=["Td"])
    god.board_scan = lambda: {"2d": 0.9, "3d": 0.9}
    god.stream.arrivals = [arr(None, votes="x", frame=object())]
    god._board_scan_new = lambda timeout=0: None   # 免等 30s 轮询
    assert god.next_card(kind="board") == "Td"
    assert god.last_via == "ops"
