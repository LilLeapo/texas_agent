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
