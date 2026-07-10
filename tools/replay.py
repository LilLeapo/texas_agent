"""全系统回放器: 读 sessions/*.jsonl 按原节奏(或加速)重播 —— 演示保险。

用法:
  python tools/replay.py sessions/xxx.jsonl               # 终端重播
  python tools/replay.py sessions/xxx.jsonl --speed 4     # 4 倍速
  python tools/replay.py sessions/xxx.jsonl --ws          # 重播进 ws hub (/pub), 前端照常吃流
  python tools/replay.py sessions/xxx.jsonl --instant     # 不等待, 一次性灌完
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--ws", action="store_true", help="转发到 ws hub /pub")
    ap.add_argument("--instant", action="store_true")
    args = ap.parse_args()

    msgs = [json.loads(line) for line in open(args.session, encoding="utf-8")]
    pub = None
    if args.ws:
        from texas_agent.bus import WsPublisher
        pub = WsPublisher()

    from texas_agent.engine_cli import render

    prev_ts = None
    for m in msgs:
        if not args.instant and prev_ts is not None:
            time.sleep(max(0, (m["ts"] - prev_ts) / args.speed))
        prev_ts = m["ts"]
        m["replayed"] = True
        if pub:
            pub.publish(m)
        render(m)
    if pub:
        time.sleep(0.5)  # 让发布队列排干
        pub.close()
    print(f"\n回放完成: {len(msgs)} 条消息")


if __name__ == "__main__":
    main()
