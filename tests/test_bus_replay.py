"""M1 验收: jsonl 落盘可回放; ws hub 端到端(发布→广播→订阅)。"""

import asyncio
import json
import threading
import time

import pytest

from texas_agent.bus import Hub, LocalBus, WsPublisher, subscribe_ws


def test_jsonl_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("texas_agent.bus.SESSIONS_DIR", tmp_path)
    bus = LocalBus(session_tag="t")
    sent = [bus.emit({"type": "game_event", "event": "x", "n": i}) for i in range(5)]
    bus.close()
    lines = [json.loads(x) for x in bus.session_path.read_text().splitlines()]
    assert lines == sent
    assert [m["seq"] for m in lines] == [1, 2, 3, 4, 5]


def test_ws_hub_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("texas_agent.bus.SESSIONS_DIR", tmp_path)
    hub = Hub(pub_port=18765, sub_port=18766, session_tag="hubtest")
    # 守护线程随进程退出, 无需显式停 loop (stop 会在线程里炸出未处理异常告警)
    threading.Thread(target=lambda: asyncio.run(hub.run()), daemon=True).start()
    time.sleep(0.5)

    got = []
    subscribe_ws(got.append, url="ws://localhost:18766")
    time.sleep(0.3)
    pub = WsPublisher(url="ws://localhost:18765")
    for i in range(3):
        pub.publish({"type": "agent_trace", "text": f"m{i}"})
    deadline = time.time() + 5
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.05)
    pub.close()

    assert [m["text"] for m in got] == ["m0", "m1", "m2"]
    assert [m["seq"] for m in got] == [1, 2, 3]          # hub 盖全局权威 seq
    logged = [json.loads(x) for x in hub.path.read_text().splitlines()]
    assert len(logged) == 3                              # 逐行落盘
