#!/usr/bin/env python3
"""
逐点示教与平滑回放工具（Panthera-HT 从臂，6 个机械臂关节）。

录制：
    python A_replay_trajectory.py record
    在 Gra+Fri 示教模式下用手调整机械臂；第一次空格保存并锁定一个点，第二次
    空格再切回 Gra+Fri 以拖动到下一个点；Ctrl+C 保存并退出。

回放：
    python A_replay_trajectory.py replay trajectory_A.json trajectory_B.json \\
        --points "A1,A2,B3,A4,B5"
    每段采用七次多项式平滑插值；每到一个点默认停留 5 秒；最后平滑回到 Reset
    零位 [0, 0, 0, 0, 0, 0] 并结束。

本工具刻意不控制第 7 个原夹爪电机，因此不会干扰外接 Linker 灵巧手。
运行本脚本时，不要同时运行 backend.sh、5_record_trajectory.py 或其他占用同一 CAN
板的脚本。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import select
import sys
import termios
import time
import tty
from typing import Any, Callable

import numpy as np

from Panthera_lib import Panthera


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "robot_param" / "Follower.yaml"
ARM_JOINT_COUNT = 6

# 示教时沿用原 5_record_trajectory.py 的保守力矩上限。
TEACH_TORQUE_LIMIT = np.array([15.0, 30.0, 30.0, 15.0, 5.0, 5.0])
FRICTION_COULOMB = np.array([0.15, 0.12, 0.12, 0.12, 0.04, 0.04])
FRICTION_VISCOUS = np.array([0.05, 0.05, 0.05, 0.03, 0.02, 0.02])
FRICTION_VELOCITY_THRESHOLD = 0.02

DEFAULT_MOVE_TIME = 3.0
DEFAULT_HOLD_TIME = 5.0
DEFAULT_RESET_MOVE_TIME = 3.0
CONTROL_RATE_HZ = 100
RESET_POSITION = np.zeros(ARM_JOINT_COUNT)

# 七次插值的最大速度约为 2.1875 * |delta| / duration。
# 这里将峰值限制在 YAML 速度上限的 60%，使轨迹有余量。
SEPTIC_PEAK_SPEED_FACTOR = 2.1875
REPLAY_VELOCITY_FRACTION = 0.60
MAX_GRAVITY_PAUSE_DRIFT_RAD = 0.12


class TrajectoryCancelled(RuntimeError):
    """Raised when an external coordinator requests a safe trajectory stop."""


def positive_seconds(value: str) -> float:
    """argparse type: require a finite non-negative duration."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是数字") from exc
    if not math.isfinite(seconds) or seconds < 0.0:
        raise argparse.ArgumentTypeError("必须是大于或等于 0 的有限秒数")
    return seconds


def precise_sleep(duration: float) -> None:
    """Sleep close to the requested control deadline without accumulating drift."""
    if duration <= 0.0:
        return
    deadline = time.perf_counter() + duration
    if duration > 0.002:
        time.sleep(duration - 0.001)
    while time.perf_counter() < deadline:
        pass


class TerminalKeyReader:
    """Read space presses immediately while leaving Ctrl+C as a normal signal."""

    def __enter__(self) -> "TerminalKeyReader":
        if not sys.stdin.isatty():
            raise RuntimeError("录制模式需要在 WSL 的交互式终端中运行。")
        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        # cbreak keeps ISIG enabled, so Ctrl+C still raises KeyboardInterrupt.
        tty.setcbreak(self._fd)
        return self

    def read_available(self) -> str:
        characters: list[str] = []
        while True:
            readable, _, _ = select.select([self._fd], [], [], 0.0)
            if not readable:
                break
            chunk = os.read(self._fd, 32)
            if not chunk:
                break
            characters.append(chunk.decode(errors="ignore"))
        return "".join(characters)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)


def make_robot(config_path: Path) -> Panthera:
    if not config_path.is_file():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")
    robot = Panthera(str(config_path))
    if robot.motor_count != ARM_JOINT_COUNT:
        raise RuntimeError(
            f"本工具只适用于 6 轴从臂；SDK 当前报告 {robot.motor_count} 个机械臂电机。"
        )
    return robot


def validate_joint_vector(values: Any, label: str, max_abs: float | None = 20.0) -> np.ndarray:
    values = np.asarray(values, dtype=float).reshape(-1)
    if values.size != ARM_JOINT_COUNT:
        raise RuntimeError(f"{label} 长度异常：期望 {ARM_JOINT_COUNT}，实际 {values.size}")
    if not np.all(np.isfinite(values)) or (max_abs is not None and np.any(np.abs(values) > max_abs)):
        raise RuntimeError(
            f"{label} 无效：{values.tolist()}。请确认 USBIP 已 attach、CAN 电机连接正常。"
        )
    return values


def request_robot_state(robot: Panthera) -> tuple[np.ndarray, np.ndarray]:
    """Request a fresh CAN state before taking a snapshot or resuming a segment."""
    robot.send_get_motor_state_cmd()
    robot.motor_send_cmd()
    positions = validate_joint_vector(robot.get_current_pos(), "关节位置")
    velocities = validate_joint_vector(robot.get_current_vel(), "关节速度")
    return positions, velocities


def warm_up_state(robot: Panthera) -> tuple[np.ndarray, np.ndarray]:
    """The SDK starts with placeholder values until a few state replies arrive."""
    last_error: Exception | None = None
    for _ in range(8):
        try:
            positions, velocities = request_robot_state(robot)
            return positions, velocities
        except Exception as exc:  # The first few packets may still be stale.
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError("无法读取有效关节状态") from last_error


def arm_torque_limit(robot: Panthera) -> np.ndarray:
    configured = validate_joint_vector(robot.max_torque, "最大力矩配置", max_abs=100.0)
    return np.minimum(configured, TEACH_TORQUE_LIMIT)


def send_gravity_friction(robot: Panthera, positions: np.ndarray, velocities: np.ndarray) -> None:
    """Gra+Fri: zero stiffness/damping with gravity and friction feed-forward."""
    gravity = validate_joint_vector(robot.get_Gravity(positions), "重力补偿力矩", max_abs=100.0)
    friction = validate_joint_vector(
        robot.get_friction_compensation(
            velocities,
            FRICTION_COULOMB,
            FRICTION_VISCOUS,
            FRICTION_VELOCITY_THRESHOLD,
        ),
        "摩擦补偿力矩",
        max_abs=100.0,
    )
    torque = np.clip(gravity + friction, -arm_torque_limit(robot), arm_torque_limit(robot))
    zeros = np.zeros(ARM_JOINT_COUNT)
    robot.pos_vel_tqe_kp_kd(zeros, zeros, torque, zeros, zeros)


def hold_current_pose(robot: Panthera) -> None:
    """End safely by holding the most recently measured pose, not an old waypoint."""
    try:
        positions, _ = request_robot_state(robot)
        robot.Joint_Pos_Vel(positions, np.zeros(ARM_JOINT_COUNT), arm_torque_limit(robot))
    except Exception as exc:
        print(f"\n[提示] 无法发送最终保持命令：{exc}")


def waypoint_filename() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return SCRIPT_DIR / f"waypoints_{timestamp}.json"


def normalise_output_path(path_arg: str | None) -> Path:
    if path_arg is None:
        return waypoint_filename()
    path = Path(path_arg).expanduser()
    return path if path.suffix else path.with_suffix(".json")


def save_waypoints(
    path: Path,
    waypoints: list[dict[str, Any]],
    move_time: float,
    hold_time: float,
    config_path: Path,
) -> None:
    payload = {
        "format": "panthera_waypoints_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": str(config_path),
        "arm_joint_count": ARM_JOINT_COUNT,
        "defaults": {
            "move_duration": move_time,
            "hold_duration": hold_time,
        },
        "waypoints": waypoints,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def load_waypoints(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise FileNotFoundError(f"轨迹文件不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "panthera_waypoints_v1":
        raise ValueError("不是本工具生成的 waypoint JSON 文件。")
    if data.get("arm_joint_count") != ARM_JOINT_COUNT:
        raise ValueError("轨迹关节数量与当前 6 轴从臂不匹配。")
    waypoints = data.get("waypoints")
    if not isinstance(waypoints, list) or not waypoints:
        raise ValueError("文件中没有可回放的预设点。")

    for index, waypoint in enumerate(waypoints, start=1):
        if not isinstance(waypoint, dict):
            raise ValueError(f"第 {index} 个点格式错误。")
        waypoint["positions"] = validate_joint_vector(waypoint.get("positions"), f"第 {index} 个点").tolist()
        for key, default in (("move_duration", DEFAULT_MOVE_TIME), ("hold_duration", DEFAULT_HOLD_TIME)):
            value = waypoint.get(key, default)
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {index} 个点的 {key} 不是数字。") from exc
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"第 {index} 个点的 {key} 必须大于或等于 0。")
            waypoint[key] = value
    return data, waypoints


def load_waypoint_sources(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Load positional trajectory files as a reusable A/B/C... point library."""
    if len(paths) > 26:
        raise ValueError("一次最多组合 26 个轨迹文件（A 到 Z）。")
    sources: dict[str, dict[str, Any]] = {}
    for offset, path in enumerate(paths):
        alias = chr(ord("A") + offset)
        _, waypoints = load_waypoints(path)
        sources[alias] = {"path": path, "waypoints": waypoints}
    return sources


def select_waypoint_references(
    selection: str | None,
    sources: dict[str, dict[str, Any]],
) -> list[tuple[str, int]]:
    """Parse a programmable multi-file sequence, e.g. ``A1,A2,B3,A4,B5``.

    A/B/C correspond to the first/second/third JSON positional argument.
    Point references may be repeated and may appear in any order.  Ranges are
    also accepted within one source: ``A1-A3`` and ``A1-3``.
    """
    aliases = list(sources)
    if selection is None or selection.strip().lower() in {"", "all"}:
        if len(aliases) != 1:
            raise ValueError("组合多个轨迹文件时必须提供 --points，例如 A1,A2,B3。")
        only_alias = aliases[0]
        return [(only_alias, index) for index in range(1, len(sources[only_alias]["waypoints"]) + 1)]

    references: list[tuple[str, int]] = []
    for token in selection.split(","):
        token = token.strip().upper()
        if not token:
            raise ValueError("--points 格式错误：逗号之间不能留空。")

        if "-" in token:
            start_text, end_text = (part.strip() for part in token.split("-", maxsplit=1))
        else:
            start_text, end_text = token, None

        def parse_reference(text: str, inherited_alias: str | None = None) -> tuple[str, int]:
            if not text:
                raise ValueError("--points 点号不能为空。")
            alias = text[0] if text[0].isalpha() else inherited_alias
            number_text = text[1:] if text[0].isalpha() else text
            if alias is None:
                if len(aliases) != 1:
                    raise ValueError("组合多个文件时每个点必须带前缀，例如 A1 或 B3。")
                alias = aliases[0]
            if alias not in sources or not number_text.isdigit():
                raise ValueError("--points 格式应类似 A1,A2,B3 或 A1-A3。")
            index = int(number_text)
            maximum = len(sources[alias]["waypoints"])
            if index < 1 or index > maximum:
                raise ValueError(f"{alias}{index} 超出范围：{alias} 文件只有 1 到 {maximum} 号点。")
            return alias, index

        first_alias, first_index = parse_reference(start_text)
        if end_text is None:
            references.append((first_alias, first_index))
            continue

        last_alias, last_index = parse_reference(end_text, inherited_alias=first_alias)
        if last_alias != first_alias:
            raise ValueError("范围不能跨文件；请写 A1-A3,B1-B2。")
        if first_index > last_index:
            raise ValueError("范围必须递增，例如 A3-A5，不能写 A5-A3。")
        references.extend((first_alias, index) for index in range(first_index, last_index + 1))

    return references


def clamp_to_joint_limits(robot: Panthera, positions: np.ndarray) -> np.ndarray:
    lower = validate_joint_vector(robot.joint_limits["lower"], "关节下限")
    upper = validate_joint_vector(robot.joint_limits["upper"], "关节上限")
    if np.any(positions < lower) or np.any(positions > upper):
        raise ValueError(
            f"预设点超出从臂关节限位：{positions.tolist()}；合法范围为 {lower.tolist()} 至 {upper.tolist()}。"
        )
    return positions


def safe_move_duration(robot: Panthera, start: np.ndarray, end: np.ndarray, requested: float) -> float:
    """Increase a requested duration whenever the septic peak speed would be unsafe."""
    velocity_limit = validate_joint_vector(robot.velocity_limits, "速度限幅配置", max_abs=100.0)
    allowed_peak_speed = np.maximum(velocity_limit * REPLAY_VELOCITY_FRACTION, 0.05)
    required = float(np.max(SEPTIC_PEAK_SPEED_FACTOR * np.abs(end - start) / allowed_peak_speed))
    return max(0.25, requested, required)


def smooth_move(
    robot: Panthera,
    start: np.ndarray,
    end: np.ndarray,
    duration: float,
    should_cancel: Callable[[], bool] | None = None,
    on_tick: Callable[[float, float], None] | None = None,
) -> None:
    """Move one segment with zero velocity and acceleration at both endpoints."""
    control_rate = CONTROL_RATE_HZ
    dt = 1.0 / control_rate
    steps = max(1, int(math.ceil(duration * control_rate)))
    started_at = time.perf_counter()
    torque_limit = arm_torque_limit(robot)

    for step in range(steps):
        if should_cancel is not None and should_cancel():
            raise TrajectoryCancelled("收到停止请求")
        elapsed = min((step + 1) * dt, duration)
        target_time = started_at + elapsed
        position, velocity, _ = robot.septic_interpolation(start, end, duration, elapsed)
        robot.Joint_Pos_Vel(position, velocity, torque_limit)
        if on_tick is not None:
            on_tick(elapsed, duration)
        precise_sleep(target_time - time.perf_counter())

    robot.Joint_Pos_Vel(end, np.zeros(ARM_JOINT_COUNT), torque_limit)


def pause_at_waypoint(robot: Panthera, target: np.ndarray, hold_time: float, pause_mode: str) -> np.ndarray:
    """Keep the arm at the point, or optionally run Gra+Fri during the hand window."""
    if hold_time <= 0.0:
        return target

    print(f"  已到达；停留 {hold_time:.1f}s（{pause_mode}）")
    started_at = time.perf_counter()
    next_status_at = started_at
    torque_limit = arm_torque_limit(robot)
    while True:
        now = time.perf_counter()
        remaining = max(0.0, hold_time - (now - started_at))
        if now >= next_status_at or remaining <= 0.0:
            print(f"\r  停留中：{remaining:4.1f}s  ", end="", flush=True)
            next_status_at = now + 0.2
        if remaining <= 0.0:
            print()
            break

        positions, velocities = request_robot_state(robot)
        if pause_mode == "gravity-friction":
            send_gravity_friction(robot, positions, velocities)
        else:
            robot.Joint_Pos_Vel(target, np.zeros(ARM_JOINT_COUNT), torque_limit)
        precise_sleep(min(0.01, remaining))

    actual_positions, _ = request_robot_state(robot)
    drift = float(np.max(np.abs(actual_positions - target)))
    if pause_mode == "gravity-friction" and drift > MAX_GRAVITY_PAUSE_DRIFT_RAD:
        raise RuntimeError(
            f"Gra+Fri 停留期间偏离 {drift:.3f} rad，已停止回放以避免下一段追赶跳动。"
        )
    return actual_positions if pause_mode == "gravity-friction" else target


def record_waypoints(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser().resolve()
    output_path = normalise_output_path(args.file)
    robot = make_robot(config_path)
    warm_up_state(robot)
    waypoints: list[dict[str, Any]] = []
    last_space_at = 0.0
    last_status_at = 0.0

    teach_mode = True
    held_position: np.ndarray | None = None

    print("\nGra+Fri 示教已启动：")
    print("  - 手动拖动机械臂到一个目标姿态")
    print("  - 第一次按 空格 保存并锁定当前 6 轴坐标")
    print("  - 再次按 空格 切回 Gra+Fri，拖动到下一个点")
    print("  - 按 Ctrl+C 结束并保存所有预设点")
    print("  - 本脚本不会控制 Linker 灵巧手\n")

    try:
        with TerminalKeyReader() as keyboard:
            while True:
                positions, velocities = request_robot_state(robot)

                now = time.perf_counter()
                keys = keyboard.read_available()
                if " " in keys and now - last_space_at >= 0.25:
                    last_space_at = now
                    if teach_mode:
                        waypoint = {
                            "index": len(waypoints) + 1,
                            "recorded_at": datetime.now(timezone.utc).isoformat(),
                            "positions": [round(float(value), 8) for value in positions],
                            "move_duration": float(args.move_time),
                            "hold_duration": float(args.hold_time),
                        }
                        waypoints.append(waypoint)
                        held_position = positions.copy()
                        teach_mode = False
                        print(
                            f"\n已保存并锁定预设点 #{waypoint['index']}: "
                            + ", ".join(f"{value:.3f}" for value in positions)
                        )
                        print("再次按空格切回 Gra+Fri，然后可拖动到下一个点。")
                    else:
                        teach_mode = True
                        held_position = None
                        print("\n已切回 Gra+Fri：现在可手动拖动机械臂。")

                if teach_mode:
                    send_gravity_friction(robot, positions, velocities)
                else:
                    # The recorded point is actively held until the next space
                    # press releases it back into Gra+Fri teaching mode.
                    robot.Joint_Pos_Vel(
                        held_position,
                        np.zeros(ARM_JOINT_COUNT),
                        arm_torque_limit(robot),
                    )

                if now - last_status_at >= 0.25:
                    status = " ".join(f"J{i + 1}:{value:+.3f}" for i, value in enumerate(positions))
                    mode_label = "Gra+Fri（可拖动）" if teach_mode else "定点保持（空格释放）"
                    print(
                        f"\r{mode_label} | 已记录 {len(waypoints)} 点 | {status}   ",
                        end="",
                        flush=True,
                    )
                    last_status_at = now
                precise_sleep(0.01)
    except KeyboardInterrupt:
        print()
        if waypoints:
            save_waypoints(output_path, waypoints, args.move_time, args.hold_time, config_path)
            print(f"已保存 {len(waypoints)} 个预设点：{output_path}")
        else:
            print("未记录任何点，不创建轨迹文件。")
    finally:
        hold_current_pose(robot)


def replay_waypoints(args: argparse.Namespace) -> None:
    trajectory_paths = [Path(file).expanduser().resolve() for file in args.files]
    sources = load_waypoint_sources(trajectory_paths)
    selected_references = select_waypoint_references(args.points, sources)
    config_path = Path(args.config).expanduser().resolve()
    robot = make_robot(config_path)
    current_position, _ = warm_up_state(robot)

    try:
        source_text = "; ".join(
            f"{alias}={source['path'].name}（{len(source['waypoints'])} 点）"
            for alias, source in sources.items()
        )
        selected_text = ", ".join(f"{alias}{index}" for alias, index in selected_references)
        print(f"\n载入点位库：{source_text}")
        print(f"本次编排（{len(selected_references)} 步）：{selected_text}")
        print(f"暂停模式：{args.pause_mode}；默认/文件内停留时间可用 --hold-time 覆盖。")
        if not args.yes:
            input("请清空机械臂运动范围、确认急停可用；按 Enter 开始，Ctrl+C 取消：")

        for waypoint_number, (source_alias, source_index) in enumerate(selected_references, start=1):
            waypoint = sources[source_alias]["waypoints"][source_index - 1]
            target = clamp_to_joint_limits(robot, np.asarray(waypoint["positions"], dtype=float))
            requested_move = args.move_time if args.move_time is not None else waypoint["move_duration"]
            requested_hold = args.hold_time if args.hold_time is not None else waypoint["hold_duration"]
            duration = safe_move_duration(robot, current_position, target, requested_move)

            print(
                f"\n点 {waypoint_number}/{len(selected_references)}（{source_alias}{source_index}）："
                f"平滑移动 {duration:.2f}s，随后停留 {requested_hold:.2f}s"
            )
            if duration > requested_move + 1e-6:
                print("  已按关节速度限幅自动延长移动时间。")
            smooth_move(robot, current_position, target, duration)
            current_position = pause_at_waypoint(robot, target, requested_hold, args.pause_mode)

        reset_target = clamp_to_joint_limits(robot, RESET_POSITION)
        reset_duration = safe_move_duration(robot, current_position, reset_target, args.reset_time)
        print(f"\n所有预设点完成；平滑回 Reset 零位，预计 {reset_duration:.2f}s。")
        if reset_duration > args.reset_time + 1e-6:
            print("  Reset 段已按关节速度限幅自动延长移动时间。")
        smooth_move(robot, current_position, reset_target, reset_duration)
        current_position = reset_target
        print("已回到 Reset 零位，回放结束。")
    except KeyboardInterrupt:
        print("\n回放已由 Ctrl+C 停止。")
    finally:
        hold_current_pose(robot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Panthera 逐点 Gra+Fri 示教与平滑回放（不控制外接灵巧手）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Gra+Fri 示教：空格记录/释放，Ctrl+C 保存")
    record.add_argument("-o", "--file", help="输出 JSON 文件；默认保存在本脚本目录")
    record.add_argument("--config", default=str(DEFAULT_CONFIG), help="从臂 YAML 配置文件")
    record.add_argument(
        "--move-time", type=positive_seconds, default=DEFAULT_MOVE_TIME,
        help=f"写入每个点的最短平滑移动时间，默认 {DEFAULT_MOVE_TIME}s",
    )
    record.add_argument(
        "--hold-time", type=positive_seconds, default=DEFAULT_HOLD_TIME,
        help=f"写入每个点后的默认停留时间，默认 {DEFAULT_HOLD_TIME}s",
    )
    record.set_defaults(handler=record_waypoints)

    replay = subparsers.add_parser("replay", help="平滑回放一个或多个 JSON 点位库")
    replay.add_argument(
        "files", nargs="+",
        help="点位 JSON 文件；第 1/2/3 个文件依次对应 A/B/C 前缀",
    )
    replay.add_argument("--config", default=str(DEFAULT_CONFIG), help="从臂 YAML 配置文件")
    replay.add_argument(
        "--move-time", type=positive_seconds,
        help="覆盖所有点的最短平滑移动时间；默认使用文件中的值",
    )
    replay.add_argument(
        "--hold-time", type=positive_seconds,
        help="覆盖所有点的停留时间；默认使用文件中的值（通常为 5s）",
    )
    replay.add_argument(
        "--points",
        help="按任意顺序编排点位，可重复；多文件用 A1,A2,B3,A4,B5，单文件也兼容 1,3-5。",
    )
    replay.add_argument(
        "--pause-mode", choices=("hold", "gravity-friction"), default="hold",
        help="停留时保持机械臂位置（默认），或运行 Gra+Fri；后者若漂移过大将中止回放。",
    )
    replay.add_argument(
        "--reset-time", type=positive_seconds, default=DEFAULT_RESET_MOVE_TIME,
        help=f"全部点位完成后回到 Reset 零位的最短移动时间，默认 {DEFAULT_RESET_MOVE_TIME}s",
    )
    replay.add_argument("-y", "--yes", action="store_true", help="跳过开始前的 Enter 确认")
    replay.set_defaults(handler=replay_waypoints)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        sys.exit(1)
