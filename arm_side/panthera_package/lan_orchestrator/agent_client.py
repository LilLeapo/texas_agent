#!/usr/bin/env python3
"""Command-line client for the Agent computer.

Examples:
  python agent_client.py --server http://192.168.1.20:5100 library
  python agent_client.py --server http://192.168.1.20:5100 run --sequence "A1,A2,B3,A4,B5"
  python agent_client.py --server http://192.168.1.20:5100 stop
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(
    server: str,
    path: str,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Panthera-Token"] = token
    request = Request(f"{server.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接协调服务器：{exc.reason}") from exc


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Panthera LAN Agent client")
    parser.add_argument("--server", required=True, help="机械臂主机地址，例如 http://192.168.1.20:5100")
    parser.add_argument("--token", default="", help="与 orchestrator_config.json 一致的 api_token")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="检查服务器连通性")
    commands.add_parser("status", help="获取当前执行状态")
    commands.add_parser("library", help="读取 A/B/C 点位库及每个点的坐标")
    commands.add_parser("stop", help="请求安全停止当前运动")

    run = commands.add_parser("run", help="提交 Agent 编排的点位序列")
    run.add_argument("--sequence", required=True, help='例如 "A1,A2,B3,A4,B5"')
    run.add_argument("--hold-time", type=float, help="覆盖每个点的手部窗口秒数")
    run.add_argument("--move-time", type=float, help="覆盖每段最短平滑移动秒数")
    run.add_argument("--reset-time", type=float, help="回 Reset 零位的最短移动秒数")
    run.add_argument("--pause-mode", choices=("hold", "gravity-friction"), default="hold")
    run.add_argument(
        "--wait-for-hand-done", action="store_true",
        help="每点至少停留 hold-time，随后等待灵巧手客户端提交 done。",
    )
    run.add_argument("--hand-timeout", type=float, help="等待 hand done 的额外最长秒数，默认 30")

    args = parser.parse_args()
    if args.command == "health":
        print_json(request_json(args.server, "/api/health", args.token))
    elif args.command == "status":
        print_json(request_json(args.server, "/api/status", args.token))
    elif args.command == "library":
        print_json(request_json(args.server, "/api/library", args.token))
    elif args.command == "stop":
        print_json(request_json(args.server, "/api/stop", args.token, method="POST", payload={}))
    else:
        payload: dict[str, Any] = {
            "sequence": args.sequence,
            "pause_mode": args.pause_mode,
            "wait_for_hand_done": args.wait_for_hand_done,
        }
        for name in ("hold_time", "move_time", "reset_time", "hand_timeout"):
            value = getattr(args, name)
            if value is not None:
                payload[name] = value
        print_json(request_json(args.server, "/api/run", args.token, method="POST", payload=payload))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
