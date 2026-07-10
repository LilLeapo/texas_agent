"""M1 · 事件 hub。

- ws_hub 唯一实例: /pub ws://host:8765 (组件发布), /sub ws://host:8766 (前端/解说订阅)。
- 所有消息统一信封: {"type": ..., "seq": 自增, "ts": epoch 秒, ...载荷}。
- 逐行落盘 sessions/<会话>.jsonl —— 回放即演示保险。

两种用法:
- 独立进程:  python -m texas_agent.bus
- 进程内:    LocalBus —— CLI / 测试 / 单机演示用, 同样落 jsonl, 可选桥接到 ws hub。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path

PUB_PORT = 8765
SUB_PORT = 8766
SESSIONS_DIR = Path("sessions")


def _session_path(tag: str = "") -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "") + ".jsonl"
    return SESSIONS_DIR / name


class LocalBus:
    """进程内总线: emit 即 广播给本进程订阅者 + 落盘 + (可选)转发 ws hub。"""

    def __init__(self, session_tag: str = "", log: bool = True, ws_forward: bool = False):
        self.seq = 0
        self._subs: list = []  # callables(msg)
        self._lock = threading.RLock()  # 审计员等后台线程也会 emit; 订阅者重入 emit 用 R
        self._path = _session_path(session_tag) if log else None
        self._file = self._path.open("a", encoding="utf-8") if self._path else None
        self._pub = WsPublisher() if ws_forward else None

    @property
    def session_path(self) -> Path | None:
        return self._path

    def subscribe(self, fn) -> None:
        self._subs.append(fn)

    def emit(self, msg: dict) -> dict:
        with self._lock:
            self.seq += 1
            msg = {"seq": self.seq, "ts": round(time.time(), 3), **msg}
            if self._file:
                self._file.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self._file.flush()
            for fn in self._subs:
                fn(msg)
            if self._pub:
                self._pub.publish(msg)
        return msg

    def close(self) -> None:
        if self._file:
            self._file.close()
        if self._pub:
            self._pub.close()


class WsPublisher:
    """同步组件用的 ws 发布客户端: 后台线程连 /pub, 队列吞吐, 断线不阻塞主流程。"""

    def __init__(self, url: str = f"ws://localhost:{PUB_PORT}"):
        self.url = url
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def publish(self, msg: dict) -> None:
        self._q.put(json.dumps(msg, ensure_ascii=False))

    def close(self) -> None:
        self._q.put(None)

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        from websockets.asyncio.client import connect

        while True:
            item = await asyncio.to_thread(self._q.get)
            if item is None:
                return
            try:
                async with connect(self.url) as ws:
                    await ws.send(item)
                    while True:
                        nxt = await asyncio.to_thread(self._q.get)
                        if nxt is None:
                            return
                        await ws.send(nxt)
            except Exception:
                # hub 未起或断线: 丢弃当前条, 下一条重连 (jsonl 落盘才是真相源)
                time.sleep(0.5)


def subscribe_ws(callback, url: str = f"ws://localhost:{SUB_PORT}") -> threading.Thread:
    """后台线程订阅 /sub, 每条消息回调 callback(dict)。"""

    async def _sub() -> None:
        from websockets.asyncio.client import connect

        while True:
            try:
                async with connect(url) as ws:
                    async for raw in ws:
                        callback(json.loads(raw))
            except Exception:
                await asyncio.sleep(1)

    t = threading.Thread(target=lambda: asyncio.run(_sub()), daemon=True)
    t.start()
    return t


class Hub:
    """独立进程 hub: /pub 收 → 盖 seq/ts → 落盘 → 广播 /sub。"""

    def __init__(self, pub_port: int = PUB_PORT, sub_port: int = SUB_PORT, session_tag: str = "hub"):
        self.pub_port, self.sub_port = pub_port, sub_port
        self.seq = 0
        self.subscribers: set = set()
        self.path = _session_path(session_tag)
        self._file = self.path.open("a", encoding="utf-8")

    async def _handle_pub(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.seq += 1
            msg.setdefault("ts", round(time.time(), 3))
            msg["seq"] = self.seq  # hub 的 seq 为全局权威
            line = json.dumps(msg, ensure_ascii=False)
            self._file.write(line + "\n")
            self._file.flush()
            dead = []
            for sub in self.subscribers:
                try:
                    await sub.send(line)
                except Exception:
                    dead.append(sub)
            for d in dead:
                self.subscribers.discard(d)

    async def _handle_sub(self, ws) -> None:
        self.subscribers.add(ws)
        try:
            await ws.wait_closed()
        finally:
            self.subscribers.discard(ws)

    async def run(self) -> None:
        from websockets.asyncio.server import serve

        async with (
            serve(self._handle_pub, "0.0.0.0", self.pub_port),
            serve(self._handle_sub, "0.0.0.0", self.sub_port),
        ):
            print(f"[hub] /pub :{self.pub_port}  /sub :{self.sub_port}  → {self.path}")
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(Hub().run())
