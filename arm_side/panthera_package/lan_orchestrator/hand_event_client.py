#!/usr/bin/env python3
"""SSE client template for the computer controlling the Linker dexterous hand.

The file intentionally contains no Linker motor command: its protocol has not
yet been supplied.  Replace ``run_linker_hand_action`` with the existing hand
control function on that computer.  The client already receives the exact arm
waypoint and hand-window start event, and can report ``done`` to the robot
coordinator when the hand action is complete.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def post_json(server: str, path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Panthera-Token"] = token
    request = Request(
        f"{server.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def run_linker_hand_action(event: dict[str, Any]) -> bool:
    """Integration hook for the Linker-hand team.

    Return True only after the hand command for this waypoint is complete.
    The default implementation does nothing and returns False, so the user can
    use --auto-done for LAN smoke tests before the real hand SDK is integrated.
    """
    _ = event
    return False


def report_status(server: str, token: str, computer_id: str, state: str, event: dict[str, Any]) -> None:
    payload = {
        "computer_id": computer_id,
        "state": state,
        "run_id": event["run_id"],
        "step": event["step"],
    }
    response = post_json(server, "/api/hand/status", token, payload)
    print(f"[hand status] {state}: {response.get('accepted_done', False)}")


def process_event(args: argparse.Namespace, event_name: str, data: dict[str, Any]) -> None:
    if event_name == "hand_window_start":
        print(
            f"\n[hand window] run={data['run_id']} step={data['step']} "
            f"hold={data['hold_time']}s mode={data['pause_mode']}"
        )
        report_status(args.server, args.token, args.computer_id, "ready", data)
        finished = args.auto_done or run_linker_hand_action(data)
        if finished:
            report_status(args.server, args.token, args.computer_id, "done", data)
    elif event_name == "hand_window_tick":
        waiting = " waiting-for-done" if data.get("waiting_for_hand_done") else ""
        print(f"\r[hand window] step={data['step']} remaining={data['remaining']:.1f}s{waiting}   ", end="", flush=True)
    elif event_name in {"trajectory_completed", "trajectory_stopped", "trajectory_error"}:
        print(f"\n[trajectory] {event_name}: {data.get('message', '')}")
    elif event_name == "segment_start":
        print(f"\n[arm] moving to {data['reference']} ({data['move_duration']:.2f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Panthera Linker-hand SSE event client")
    parser.add_argument("--server", required=True, help="例如 http://192.168.1.20:5100")
    parser.add_argument("--token", default="", help="与协调服务器一致的 api_token")
    parser.add_argument("--computer-id", default="linker-hand-pc", help="本机名称")
    parser.add_argument(
        "--auto-done", action="store_true",
        help="仅联调：收到 hand_window_start 后立即提交 done，不控制真实灵巧手。",
    )
    args = parser.parse_args()

    headers = {"Accept": "text/event-stream"}
    if args.token:
        headers["X-Panthera-Token"] = args.token
    request = Request(f"{args.server.rstrip('/')}/api/events", headers=headers)
    print("Connecting to hand-event stream. Press Ctrl+C to stop.")

    try:
        with urlopen(request, timeout=60) as response:
            event_name = "message"
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        data = json.loads("\n".join(data_lines))
                        process_event(args, event_name, data)
                    event_name = "message"
                    data_lines = []
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
    except KeyboardInterrupt:
        print("\nHand event client stopped.")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"无法连接事件流：{exc}") from exc


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
