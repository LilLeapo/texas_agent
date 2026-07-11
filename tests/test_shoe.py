"""靴口读牌(无机械臂过渡形态): 亮牌→VLM 认→防重读→补录兜底, 烧牌不读。"""

import pytest

from texas_agent.bus import LocalBus
from texas_agent.orchestrator import ScriptedOps
from texas_agent.prompter import Prompter
from texas_agent.vision.shoe import ShoeGod


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
