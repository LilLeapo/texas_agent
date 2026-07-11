#!/usr/bin/env python3
"""LAN coordinator running only on the computer physically connected to Panthera.

Responsibilities
----------------
* Own the CAN/USB connection to the 6-axis Panthera follower arm.
* Expose a small HTTP API for the Agent computer to submit a program such as
  ``A1,A2,B3,A4,B5``.
* Stream Server-Sent Events (SSE) to the Linker-hand computer when every arm
  waypoint is reached and its hand-control window begins.

Only this process may talk to the arm hardware.  Do not run backend.sh or any
other Panthera motion script while this server is running.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Generator
from uuid import uuid4

from flask import Flask, Response, jsonify, request
import numpy as np


THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from A_replay_trajectory import (  # noqa: E402
    ARM_JOINT_COUNT,
    DEFAULT_CONFIG,
    DEFAULT_HOLD_TIME,
    DEFAULT_MOVE_TIME,
    DEFAULT_RESET_MOVE_TIME,
    MAX_GRAVITY_PAUSE_DRIFT_RAD,
    RESET_POSITION,
    TrajectoryCancelled,
    arm_torque_limit,
    clamp_to_joint_limits,
    hold_current_pose,
    load_waypoint_sources,
    make_robot,
    precise_sleep,
    request_robot_state,
    safe_move_duration,
    select_waypoint_references,
    send_gravity_friction,
    smooth_move,
    warm_up_state,
)


DEFAULT_PORT = 5100
DEFAULT_HAND_TIMEOUT = 30.0


def bounded_seconds(value: Any, default: float, name: str, maximum: float = 600.0) -> float:
    """Read an API time parameter without allowing invalid or excessive values."""
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是秒数。") from exc
    if not np.isfinite(seconds) or seconds < 0.0 or seconds > maximum:
        raise ValueError(f"{name} 必须在 0 到 {maximum:g} 秒之间。")
    return seconds


class EventBroker:
    """Fan out compact SSE events to Agent/hand monitoring clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def publish(self, event: str, data: dict[str, Any]) -> None:
        message = {
            "event": event,
            "timestamp": time.time(),
            "data": data,
        }
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                # A stale monitor must never slow or block the robot worker.
                pass

    def stream(self) -> Generator[str, None, None]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(subscriber)
        try:
            while True:
                try:
                    message = subscriber.get(timeout=15.0)
                    payload = json.dumps(message["data"], ensure_ascii=False)
                    yield f"event: {message['event']}\ndata: {payload}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)


@dataclass(frozen=True)
class RunOptions:
    sequence: str | None
    hold_time: float | None
    move_time: float | None
    reset_time: float
    pause_mode: str
    wait_for_hand_done: bool
    hand_timeout: float


class RobotOrchestrator:
    """Single-owner arm worker with an Agent API and a hand-control handshake."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        self.host = str(raw.get("server", {}).get("host", "0.0.0.0"))
        self.port = int(raw.get("server", {}).get("port", DEFAULT_PORT))
        self.api_token = str(raw.get("server", {}).get("api_token", "")).strip()

        root = config_path.parent
        robot_config = Path(raw.get("robot_config", str(DEFAULT_CONFIG))).expanduser()
        self.robot_config_path = (root / robot_config).resolve() if not robot_config.is_absolute() else robot_config

        trajectory_files = raw.get("trajectory_files")
        if not isinstance(trajectory_files, list) or not trajectory_files:
            raise ValueError("配置文件必须提供非空 trajectory_files 列表。")
        source_paths = [
            (root / Path(item)).resolve() if not Path(item).expanduser().is_absolute() else Path(item).expanduser()
            for item in trajectory_files
        ]
        self.sources = load_waypoint_sources(source_paths)

        self.robot = None
        self.events = EventBroker()
        self._state_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._hand_done: set[tuple[str, int]] = set()
        self._hand_status: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {
            "phase": "starting",
            "running": False,
            "run_id": None,
            "step": None,
            "total_steps": 0,
            "sequence": [],
            "message": "server is starting",
        }

    def initialise_hardware(self) -> None:
        self.robot = make_robot(self.robot_config_path)
        positions, _ = warm_up_state(self.robot)
        self._set_state(phase="idle", running=False, message="robot ready", current_positions=positions.tolist())
        self.events.publish("server_ready", self.status())

    def _set_state(self, **changes: Any) -> None:
        with self._state_lock:
            self._state.update(changes)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
            state["hand_clients"] = dict(self._hand_status)
        state["sources"] = {
            alias: {
                "file": source["path"].name,
                "point_count": len(source["waypoints"]),
            }
            for alias, source in self.sources.items()
        }
        return state

    def library(self) -> dict[str, Any]:
        return {
            alias: {
                "file": source["path"].name,
                "points": [
                    {
                        "reference": f"{alias}{index}",
                        "positions": waypoint["positions"],
                        "move_duration": waypoint["move_duration"],
                        "hold_duration": waypoint["hold_duration"],
                    }
                    for index, waypoint in enumerate(source["waypoints"], start=1)
                ],
            }
            for alias, source in self.sources.items()
        }

    def _parse_run_options(self, payload: dict[str, Any]) -> RunOptions:
        pause_mode = payload.get("pause_mode", "hold")
        if pause_mode not in {"hold", "gravity-friction"}:
            raise ValueError("pause_mode 只能是 hold 或 gravity-friction。")
        return RunOptions(
            sequence=payload.get("sequence"),
            hold_time=(
                bounded_seconds(payload.get("hold_time"), DEFAULT_HOLD_TIME, "hold_time")
                if payload.get("hold_time") is not None else None
            ),
            move_time=(
                bounded_seconds(payload.get("move_time"), DEFAULT_MOVE_TIME, "move_time")
                if payload.get("move_time") is not None else None
            ),
            reset_time=bounded_seconds(payload.get("reset_time"), DEFAULT_RESET_MOVE_TIME, "reset_time"),
            pause_mode=pause_mode,
            wait_for_hand_done=bool(payload.get("wait_for_hand_done", False)),
            hand_timeout=bounded_seconds(
                payload.get("hand_timeout"), DEFAULT_HAND_TIMEOUT, "hand_timeout"
            ),
        )

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.robot is None:
            raise RuntimeError("机械臂尚未初始化。")
        options = self._parse_run_options(payload)
        references = select_waypoint_references(options.sequence, self.sources)
        if not references:
            raise ValueError("编排不能为空。")

        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("已有轨迹正在执行；请先调用 /api/stop。")
            run_id = uuid4().hex[:12]
            self._cancel_event.clear()
            self._hand_done.clear()
            self._state.update({
                "phase": "queued",
                "running": True,
                "run_id": run_id,
                "step": 0,
                "total_steps": len(references),
                "sequence": [f"{alias}{index}" for alias, index in references],
                "message": "trajectory queued",
            })
            self._worker = threading.Thread(
                target=self._run_worker,
                args=(run_id, references, options),
                daemon=True,
            )
            self._worker.start()

        response = self.status()
        self.events.publish("trajectory_queued", response)
        return response

    def request_stop(self) -> dict[str, Any]:
        with self._state_lock:
            active = self._worker is not None and self._worker.is_alive()
            if active:
                self._cancel_event.set()
                self._state.update({"phase": "stopping", "message": "stop requested"})
        status = self.status()
        if active:
            self.events.publish("trajectory_stop_requested", status)
        return status

    def update_hand_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        computer_id = str(payload.get("computer_id", "hand-client")).strip() or "hand-client"
        state = str(payload.get("state", "ready")).strip().lower()
        run_id = str(payload.get("run_id", ""))
        try:
            step = int(payload.get("step", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("step 必须是整数。") from exc

        with self._state_lock:
            current_run = self._state.get("run_id")
            self._hand_status[computer_id] = {
                "state": state,
                "run_id": run_id,
                "step": step,
                "updated_at": time.time(),
            }
            accepted_done = state == "done" and run_id == current_run and step > 0
            if accepted_done:
                self._hand_done.add((run_id, step))

        result = {
            "accepted": True,
            "accepted_done": accepted_done,
            "status": self.status(),
        }
        self.events.publish("hand_status", {"computer_id": computer_id, **result})
        return result

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise TrajectoryCancelled("Agent requested stop")

    def _hand_window(
        self,
        run_id: str,
        step: int,
        target: np.ndarray,
        hold_time: float,
        options: RunOptions,
    ) -> np.ndarray:
        """Hold the arm during hand control and optionally wait for hand_done."""
        assert self.robot is not None
        minimum_deadline = time.perf_counter() + hold_time
        final_deadline = minimum_deadline + options.hand_timeout if options.wait_for_hand_done else minimum_deadline
        next_event_at = 0.0
        torque_limit = arm_torque_limit(self.robot)

        self._set_state(phase="hand_window", step=step, message="hand-control window active")
        self.events.publish("hand_window_start", {
            "run_id": run_id,
            "step": step,
            "hold_time": hold_time,
            "pause_mode": options.pause_mode,
            "wait_for_hand_done": options.wait_for_hand_done,
        })

        while True:
            self._raise_if_cancelled()
            now = time.perf_counter()
            minimum_remaining = max(0.0, minimum_deadline - now)
            hand_done = (run_id, step) in self._hand_done
            if now >= minimum_deadline and (not options.wait_for_hand_done or hand_done):
                break
            if options.wait_for_hand_done and now >= final_deadline:
                raise RuntimeError(f"第 {step} 步等待灵巧手 done 超时。")

            positions, velocities = request_robot_state(self.robot)
            if options.pause_mode == "gravity-friction":
                send_gravity_friction(self.robot, positions, velocities)
            else:
                self.robot.Joint_Pos_Vel(target, np.zeros(ARM_JOINT_COUNT), torque_limit)

            if now >= next_event_at:
                self.events.publish("hand_window_tick", {
                    "run_id": run_id,
                    "step": step,
                    "remaining": minimum_remaining,
                    "waiting_for_hand_done": options.wait_for_hand_done and now >= minimum_deadline,
                })
                next_event_at = now + 0.2
            precise_sleep(0.01)

        actual_positions, _ = request_robot_state(self.robot)
        drift = float(np.max(np.abs(actual_positions - target)))
        if options.pause_mode == "gravity-friction" and drift > MAX_GRAVITY_PAUSE_DRIFT_RAD:
            raise RuntimeError(
                f"第 {step} 步 Gra+Fri 窗口漂移 {drift:.3f} rad；为避免追赶跳动而中止。"
            )
        self.events.publish("hand_window_complete", {
            "run_id": run_id,
            "step": step,
            "drift": drift,
            "hand_done": hand_done,
        })
        return actual_positions if options.pause_mode == "gravity-friction" else target

    def _run_worker(
        self,
        run_id: str,
        references: list[tuple[str, int]],
        options: RunOptions,
    ) -> None:
        assert self.robot is not None
        result_event = "trajectory_completed"
        try:
            current_position, _ = request_robot_state(self.robot)
            self._set_state(phase="moving", message="trajectory running")
            self.events.publish("trajectory_started", self.status())

            for step, (alias, source_index) in enumerate(references, start=1):
                self._raise_if_cancelled()
                waypoint = self.sources[alias]["waypoints"][source_index - 1]
                target = clamp_to_joint_limits(self.robot, np.asarray(waypoint["positions"], dtype=float))
                requested_move = options.move_time if options.move_time is not None else waypoint["move_duration"]
                requested_hold = options.hold_time if options.hold_time is not None else waypoint["hold_duration"]
                duration = safe_move_duration(self.robot, current_position, target, requested_move)

                self._set_state(
                    phase="moving",
                    step=step,
                    message=f"moving to {alias}{source_index}",
                    current_reference=f"{alias}{source_index}",
                    move_duration=duration,
                    hold_duration=requested_hold,
                )
                self.events.publish("segment_start", {
                    "run_id": run_id,
                    "step": step,
                    "total_steps": len(references),
                    "reference": f"{alias}{source_index}",
                    "move_duration": duration,
                    "hold_duration": requested_hold,
                })

                last_progress_event = -1.0

                def on_tick(elapsed: float, total: float) -> None:
                    nonlocal last_progress_event
                    progress = elapsed / total
                    if progress - last_progress_event >= 0.1 or progress >= 1.0:
                        self.events.publish("segment_progress", {
                            "run_id": run_id,
                            "step": step,
                            "reference": f"{alias}{source_index}",
                            "progress": progress,
                        })
                        last_progress_event = progress

                smooth_move(
                    self.robot,
                    current_position,
                    target,
                    duration,
                    should_cancel=self._cancel_event.is_set,
                    on_tick=on_tick,
                )
                self.events.publish("waypoint_reached", {
                    "run_id": run_id,
                    "step": step,
                    "reference": f"{alias}{source_index}",
                })
                current_position = self._hand_window(run_id, step, target, requested_hold, options)

            self._raise_if_cancelled()
            reset_target = clamp_to_joint_limits(self.robot, RESET_POSITION)
            reset_duration = safe_move_duration(self.robot, current_position, reset_target, options.reset_time)
            self._set_state(phase="reset", step=len(references), message="returning to reset")
            self.events.publish("reset_start", {
                "run_id": run_id,
                "move_duration": reset_duration,
                "target": RESET_POSITION.tolist(),
            })
            smooth_move(
                self.robot,
                current_position,
                reset_target,
                reset_duration,
                should_cancel=self._cancel_event.is_set,
            )
            self._set_state(phase="complete", running=False, message="trajectory complete; at reset")
        except TrajectoryCancelled as exc:
            result_event = "trajectory_stopped"
            self._set_state(phase="stopped", running=False, message=str(exc))
        except Exception as exc:
            result_event = "trajectory_error"
            self._set_state(phase="error", running=False, message=str(exc))
        finally:
            hold_current_pose(self.robot)
            self.events.publish(result_event, self.status())


def create_app(orchestrator: RobotOrchestrator) -> Flask:
    app = Flask(__name__)

    def check_token() -> Response | None:
        if not orchestrator.api_token:
            return None
        if request.headers.get("X-Panthera-Token") != orchestrator.api_token:
            return jsonify({"success": False, "error": "invalid API token"}), 401
        return None

    @app.get("/api/health")
    def health() -> Response:
        return jsonify({"success": True, "status": orchestrator.status()})

    @app.get("/api/status")
    def status() -> Response:
        denied = check_token()
        return denied if denied is not None else jsonify({"success": True, "status": orchestrator.status()})

    @app.get("/api/library")
    def library() -> Response:
        denied = check_token()
        return denied if denied is not None else jsonify({"success": True, "library": orchestrator.library()})

    @app.post("/api/run")
    def run() -> Response:
        denied = check_token()
        if denied is not None:
            return denied
        try:
            status_data = orchestrator.start_run(request.get_json(silent=True) or {})
            return jsonify({"success": True, "status": status_data}), 202
        except (ValueError, RuntimeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @app.post("/api/stop")
    def stop() -> Response:
        denied = check_token()
        return denied if denied is not None else jsonify({"success": True, "status": orchestrator.request_stop()})

    @app.post("/api/hand/status")
    def hand_status() -> Response:
        denied = check_token()
        if denied is not None:
            return denied
        try:
            return jsonify({"success": True, **orchestrator.update_hand_status(request.get_json(silent=True) or {})})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get("/api/events")
    def events() -> Response:
        denied = check_token()
        if denied is not None:
            return denied
        return Response(
            orchestrator.events.stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Panthera LAN robot orchestration server")
    parser.add_argument(
        "--config",
        default=str(THIS_DIR / "orchestrator_config.json"),
        help="协调服务器 JSON 配置文件",
    )
    parser.add_argument("--host", help="覆盖配置中的监听地址")
    parser.add_argument("--port", type=int, help="覆盖配置中的监听端口")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"找不到 {config_path}。请从 orchestrator_config.example.json 复制一份并填写轨迹文件名。"
        )
    orchestrator = RobotOrchestrator(config_path)
    orchestrator.initialise_hardware()
    app = create_app(orchestrator)
    host = args.host or orchestrator.host
    port = args.port or orchestrator.port
    print(f"Panthera LAN orchestrator: http://{host}:{port}")
    print("Only this process may control the arm CAN connection. Press Ctrl+C to stop the server.")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
