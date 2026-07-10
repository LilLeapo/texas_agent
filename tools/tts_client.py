"""笔记本端 TTS 客户端: 订阅 ws hub, 用 macOS `say` 播报提词(可选解说)。

本体跑在 Spark(Linux 无 `say`)时, 声音由本工具在笔记本上出;
纯订阅者, 挂了/没开都不影响主循环 —— 与全系统降级哲学一致。

用法: python tools/tts_client.py --host <SPARK_IP> [--port 8766] [--commentary]
"""

from __future__ import annotations

import argparse
import queue
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.bus import subscribe_ws  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="ws hub 所在机器, 如 Spark 的 IP")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--commentary", action="store_true", help="解说也播报(默认只播提词)")
    ap.add_argument("--voice", default=None)
    args = ap.parse_args()
    if not shutil.which("say"):
        sys.exit("本机没有 `say` (需要 macOS)")

    q: queue.Queue = queue.Queue()
    types = {"dealer_prompt"} | ({"commentary"} if args.commentary else set())

    def on_msg(msg: dict) -> None:
        if msg.get("type") in types and not msg.get("replayed"):
            q.put(msg["text"])

    subscribe_ws(on_msg, url=f"ws://{args.host}:{args.port}")
    print(f"🔊 已订阅 ws://{args.host}:{args.port}, 播报: {', '.join(sorted(types))}")
    while True:
        text = q.get()
        print(f"  🔊 {text}")
        cmd = ["say"] + (["-v", args.voice] if args.voice else []) + [text]
        try:
            subprocess.run(cmd, check=False, timeout=20)
        except Exception:
            time.sleep(0.5)  # say 出错不退订, 下一条继续


if __name__ == "__main__":
    main()
