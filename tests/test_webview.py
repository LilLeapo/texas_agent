"""荷官网页屏: 总线消息 → /state; 无相机也能跑(纯提词模式)。"""

import json
import urllib.request

import pytest

from texas_agent.bus import LocalBus
from texas_agent.webview import WebView


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    yield b
    b.close()


def test_webview_reflects_bus(bus):
    wv = WebView(bus, vision=None, port=0)   # 端口 0 = 随机可用端口
    port = wv._srv.server_port
    try:
        bus.emit({"type": "dealer_prompt", "text": "发翻牌 → C1 C2 C3",
                  "level": "normal", "tts": False})
        bus.emit({"type": "commentary", "text": "翻牌很精彩", "trigger": "flop_leader"})
        bus.emit({"type": "alert", "text": "发牌超时"})
        state = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/state", timeout=5))
        assert state["prompt"]["text"].endswith("发牌超时")   # alert 顶到提词位
        assert state["prompt"]["level"] == "alert"
        assert state["commentary"] == "翻牌很精彩"
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        assert "荷官屏".encode() in page
    finally:
        wv.close()


class DummyVision:
    force_pass = want_rebaseline = want_recalib = abort_hand = False
    calib = type("C", (), {"H": None})()


def test_ctl_buttons_set_vision_flags(bus):
    v = DummyVision()
    wv = WebView(bus, vision=v, port=0)
    port = wv._srv.server_port
    try:
        for cmd, attr in [("pass", "force_pass"), ("rebaseline", "want_rebaseline"),
                          ("recalib", "want_recalib")]:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ctl?cmd={cmd}", timeout=5)
            assert getattr(v, attr) is True
    finally:
        wv.close()


def test_restart_button_raises_from_ask(bus):
    import threading
    from texas_agent.orchestrator import HandRestart
    from texas_agent.webview import WebOps
    v = DummyVision()
    wv = WebView(bus, vision=v, port=0)
    port = wv._srv.server_port
    try:
        def press_restart():
            while wv.state["question"] is None:
                pass
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ctl?cmd=restart", timeout=5)

        threading.Thread(target=press_restart).start()
        with pytest.raises(HandRestart):
            WebOps(wv).confirm("卡死了?")
        assert v.abort_hand is True   # watch 循环的那头也会被打断
    finally:
        wv.close()


def test_webops_confirm_and_card(bus):
    import threading
    from texas_agent.webview import WebOps
    wv = WebView(bus, vision=None, port=0)
    port = wv._srv.server_port
    ops = WebOps(wv)
    try:
        def answer_when_asked(value):
            while wv.state["question"] is None:
                pass
            seq = wv.state["question"]["seq"]
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/answer?seq={seq}&v={value}", timeout=5)

        t = threading.Thread(target=answer_when_asked, args=("y",))
        t.start()
        assert ops.confirm("按 C4 记录?") is True
        t.join()
        t = threading.Thread(target=answer_when_asked, args=("as",))
        t.start()
        assert ops.ask_card("river C5") == "As"   # 容错大小写
        t.join()
    finally:
        wv.close()
