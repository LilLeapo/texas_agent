#!/usr/bin/env python3
"""Interactive O6 calibration tool for playing-card grasping."""

from __future__ import annotations

import argparse
import ast
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

JOINT_NAMES = (
    "拇指弯曲",
    "拇指横摆",
    "食指弯曲",
    "中指弯曲",
    "无名指弯曲",
    "小指弯曲",
)
POSE_NAMES = ("open", "pre_pinch", "pinch", "release")
POSE_ALIASES = {
    "open": "open",
    "张开": "open",
    "pre_pinch": "pre_pinch",
    "prepinch": "pre_pinch",
    "预夹": "pre_pinch",
    "预夹持": "pre_pinch",
    "pinch": "pinch",
    "夹牌": "pinch",
    "夹持": "pinch",
    "release": "release",
    "松开": "release",
    "释放": "release",
}
DEFAULT_OPEN_POSE = [250] * 6
DEFAULT_OUTPUT = PROJECT_ROOT / "LinkerHand/config/O6_card_calibration.yaml"
JOG_STEPS = (1, 2, 5, 10, 20, 50)


def validate_pose(values: Iterable[Any]) -> list[int]:
    """Return a six-element uint8 pose or raise ValueError."""
    pose = list(values)
    if len(pose) != 6:
        raise ValueError("O6 姿态必须包含 6 个关节值")
    try:
        pose = [int(value) for value in pose]
    except (TypeError, ValueError) as exc:
        raise ValueError("关节值必须是整数") from exc
    if any(value < 0 or value > 255 for value in pose):
        raise ValueError("关节值必须在 0～255 之间")
    return pose


def validate_touch(values: Iterable[Any]) -> list[float]:
    touch = list(values)
    if len(touch) < 5:
        raise ValueError(f"触觉数据长度异常: {len(touch)}")
    try:
        return [float(value) for value in touch[:6]]
    except (TypeError, ValueError) as exc:
        raise ValueError("触觉数据包含非数值") from exc


def parse_pose_arguments(arguments: Iterable[str]) -> list[int]:
    """Parse either whitespace, CSV, or Python-list pose notation."""
    tokens = list(arguments)
    if not tokens:
        raise ValueError("用法: move <六个关节值>")
    text = " ".join(tokens).strip()
    if text.startswith("[") or text.startswith("("):
        try:
            values = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"姿态列表格式错误: {text}") from exc
        if not isinstance(values, (list, tuple)):
            raise ValueError("move 的列表参数必须是 list 或 tuple")
        return validate_pose(values)
    if "," in text:
        return validate_pose(part.strip() for part in text.split(","))
    return validate_pose(tokens)


def normalize_pose_name(name: str) -> str:
    normalized = name.strip().strip("<>").lower().replace("-", "_")
    pose_name = POSE_ALIASES.get(normalized)
    if pose_name is None:
        raise ValueError(f"姿态名必须是: {', '.join(POSE_NAMES)}")
    return pose_name


@dataclass
class JogState:
    joint_index: int = 0
    step: int = 5


JOG_HELP = """
实时微调：
  ↑/↓ 或 1～6   选择关节
  ←/→ 或 -/+    减小/增大当前关节
  [ / ]         减小/增大微调步长
  o             保存 open（张开）
  b             保存 pre_pinch（预夹）
  p             保存 pinch（夹牌）
  r             保存 release（松开）
  空格          刷新状态
  h 或 ?        显示本帮助
  Alt+字母      执行已绑定的动作
  q             退出实时微调
""".strip()


def normalize_shortcut(shortcut: str) -> str:
    raw_shortcut = shortcut.strip()
    if len(raw_shortcut) == 2 and raw_shortcut[0] == "\x1b" and raw_shortcut[1].isascii() and raw_shortcut[1].isalpha():
        return f"ALT+{raw_shortcut[1].upper()}"
    if len(raw_shortcut) == 3 and raw_shortcut.startswith("^[") and raw_shortcut[2].isascii() and raw_shortcut[2].isalpha():
        return f"ALT+{raw_shortcut[2].upper()}"
    normalized = raw_shortcut.upper().replace(" ", "").replace("-", "+")
    parts = normalized.split("+")
    if len(parts) != 2 or parts[0] not in {"ALT", "CTRL"}:
        raise ValueError("快捷键格式必须是 alt+字母 或 ctrl+字母")
    if len(parts[1]) != 1 or not parts[1].isascii() or not parts[1].isalpha():
        raise ValueError("快捷键只能绑定英文字母 A～Z")
    return f"{parts[0]}+{parts[1]}"


class DryRunHand:
    """In-memory stand-in used to verify the workflow without hardware."""

    def __init__(self) -> None:
        self.position = DEFAULT_OPEN_POSE.copy()
        self.speed = [50] * 6
        self.torque = [60] * 6

    def finger_move(self, pose: Iterable[Any]) -> None:
        self.position = validate_pose(pose)

    def get_state(self) -> list[int]:
        return self.position.copy()

    def set_speed(self, speed: Iterable[Any]) -> None:
        self.speed = validate_pose(speed)

    def set_torque(self, torque: Iterable[Any]) -> None:
        self.torque = validate_pose(torque)

    def get_touch(self) -> list[float]:
        closure = max(0.0, (250 - self.position[0]) + (250 - self.position[2]))
        return [round(closure / 10, 2), round(closure / 12, 2), 0, 0, 0, 0]

    def get_force(self) -> list[list[float]]:
        return [self.get_touch(), [0] * 6, [0] * 6, [0] * 6]


class CalibrationSession:
    def __init__(
        self,
        hand: Any,
        *,
        hand_type: str,
        can_channel: str,
        modbus: str,
        output_path: Path,
        speed: int,
        torque: int,
        step: int,
        max_joint_step: int,
        move_delay: float,
        jog_delay: float,
        hold_time: float,
        position_tolerance: int,
        settle_min_time: float,
        settle_timeout: float,
        settle_poll: float,
        stable_samples: int,
        autosave: bool,
        dry_run: bool,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
    ) -> None:
        self.hand = hand
        self.hand_type = hand_type
        self.can_channel = can_channel
        self.modbus = modbus
        self.output_path = output_path
        self.speed = speed
        self.torque = torque
        self.step = step
        self.max_joint_step = max_joint_step
        self.move_delay = move_delay
        self.jog_delay = jog_delay
        self.hold_time = hold_time
        self.position_tolerance = position_tolerance
        self.settle_min_time = settle_min_time
        self.settle_timeout = settle_timeout
        self.settle_poll = settle_poll
        self.stable_samples = stable_samples
        self.autosave = autosave
        self.dry_run = dry_run
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.current_pose = self._read_initial_pose()
        self.poses: dict[str, list[int] | None] = {
            "open": None,
            "pre_pinch": None,
            "pinch": None,
            "release": None,
        }
        self.touch_baseline: list[float] | None = None
        self.touch_samples: dict[str, list[float]] = {}
        self.custom_actions: dict[str, list[int]] = {}
        self.shortcuts: dict[str, str] = {}

    def _read_initial_pose(self) -> list[int]:
        try:
            return validate_pose(self.hand.get_state())
        except Exception as exc:
            print(f"警告：无法读取当前姿态，将使用张开姿态作为编辑起点：{exc}")
            return DEFAULT_OPEN_POSE.copy()

    def configure_limits(self) -> None:
        self.hand.set_speed([self.speed] * 6)
        self.hand.set_torque([self.torque] * 6)

    def move(self, target: Iterable[Any], *, duration: float | None = None) -> None:
        """Interpolate a move so no command changes a joint too abruptly."""
        target_pose = validate_pose(target)
        start = self.current_pose.copy()
        largest_delta = max(abs(end - begin) for begin, end in zip(start, target_pose))
        steps = max(1, (largest_delta + self.max_joint_step - 1) // self.max_joint_step)
        total_duration = self.move_delay if duration is None else max(0.0, duration)
        for index in range(1, steps + 1):
            ratio = index / steps
            pose = [round(begin + (end - begin) * ratio) for begin, end in zip(start, target_pose)]
            self.hand.finger_move(pose)
            self.current_pose = pose
            if total_duration and index < steps:
                self.sleep(total_duration / steps)

    def adjust_joint(self, joint_index: int, delta: int, *, duration: float | None = None) -> None:
        if joint_index < 0 or joint_index >= 6:
            raise ValueError("关节编号必须在 0～5 之间")
        target = self.current_pose.copy()
        target[joint_index] = min(255, max(0, target[joint_index] + delta))
        self.move(target, duration=duration)

    def set_joint(self, joint_index: int, value: int) -> None:
        if joint_index < 0 or joint_index >= 6:
            raise ValueError("关节编号必须在 0～5 之间")
        target = self.current_pose.copy()
        target[joint_index] = value
        self.move(target)

    def save_pose(self, name: str) -> None:
        name = normalize_pose_name(name)
        self.poses[name] = self.current_pose.copy()
        print(f"已保存 {name}: {self.poses[name]}")
        if self.autosave:
            self.write()

    def goto_pose(self, name: str) -> None:
        name = normalize_pose_name(name)
        pose = self.poses[name]
        if pose is None:
            raise ValueError(f"姿态 {name} 尚未保存")
        self.move(pose)

    def play_pose(self, name: str) -> None:
        name = normalize_pose_name(name)
        self.goto_pose(name)
        status, actual_pose = self.wait_until_settled(self.poses[name])
        status_text = "已到位" if status == "reached" else "接触后已稳定"
        print(f"{name} {status_text}: {actual_pose}")

    def _resolve_action(self, name: str) -> tuple[str, list[int]]:
        candidate = name.strip().strip("<>")
        try:
            pose_name = normalize_pose_name(candidate)
        except ValueError:
            pose_name = ""
        if pose_name:
            pose = self.poses[pose_name]
            if pose is None:
                raise ValueError(f"姿态 {pose_name} 尚未保存")
            return pose_name, pose
        for action_name, pose in self.custom_actions.items():
            if action_name.casefold() == candidate.casefold():
                return action_name, pose
        raise ValueError(f"动作不存在: {name}")

    def save_custom_action(self, name: str) -> None:
        action_name = name.strip().strip("<>")
        if not action_name:
            raise ValueError("自定义动作名不能为空")
        if action_name.casefold() == "cycle":
            raise ValueError("cycle 是保留名称")
        try:
            normalize_pose_name(action_name)
        except ValueError:
            pass
        else:
            raise ValueError("内置姿态请使用 save，不要使用 saveas")
        self.custom_actions[action_name] = self.current_pose.copy()
        print(f"已保存自定义动作 {action_name}: {self.current_pose}")
        if self.autosave:
            self.write()

    def play_action(self, name: str) -> None:
        action_name, pose = self._resolve_action(name)
        self.move(pose)
        status, actual_pose = self.wait_until_settled(pose)
        status_text = "已到位" if status == "reached" else "接触后已稳定"
        print(f"{action_name} {status_text}: {actual_pose}")

    def bind_shortcut(self, shortcut: str, action_name: str) -> None:
        canonical_shortcut = normalize_shortcut(shortcut)
        resolved_name, _ = self._resolve_action(action_name)
        self.shortcuts[canonical_shortcut] = resolved_name
        print(f"已绑定 {canonical_shortcut} -> {resolved_name}")
        if self.autosave:
            self.write()

    def unbind_shortcut(self, shortcut: str) -> None:
        canonical_shortcut = normalize_shortcut(shortcut)
        if self.shortcuts.pop(canonical_shortcut, None) is None:
            raise ValueError(f"快捷键尚未绑定: {canonical_shortcut}")
        print(f"已解除快捷键: {canonical_shortcut}")
        if self.autosave:
            self.write()

    def delete_custom_action(self, name: str) -> None:
        action_name, _ = self._resolve_action(name)
        if action_name in POSE_NAMES:
            raise ValueError("不能删除内置姿态")
        del self.custom_actions[action_name]
        self.shortcuts = {
            shortcut: bound_action
            for shortcut, bound_action in self.shortcuts.items()
            if bound_action != action_name
        }
        print(f"已删除自定义动作: {action_name}")
        if self.autosave:
            self.write()

    def wait_until_settled(self, target: Iterable[Any]) -> tuple[str, list[int]]:
        """Wait for feedback to reach the target or stop changing under contact."""
        target_pose = validate_pose(target)
        started_at = self.monotonic()
        last_pose: list[int] | None = None
        stable_count = 0
        actual_pose = self.current_pose.copy()

        while True:
            try:
                actual_pose = validate_pose(self.hand.get_state())
            except Exception as exc:
                raise RuntimeError(f"无法读取实际关节位置，自动循环已中止: {exc}") from exc

            elapsed = self.monotonic() - started_at
            target_error = max(abs(actual - target) for actual, target in zip(actual_pose, target_pose))
            if target_error <= self.position_tolerance:
                self.current_pose = actual_pose
                return "reached", actual_pose

            if elapsed >= self.settle_min_time and last_pose is not None:
                feedback_change = max(abs(actual - previous) for actual, previous in zip(actual_pose, last_pose))
                if feedback_change <= self.position_tolerance:
                    stable_count += 1
                else:
                    stable_count = 0
                if stable_count >= self.stable_samples:
                    self.current_pose = actual_pose
                    return "stable", actual_pose

            if elapsed >= self.settle_timeout:
                raise RuntimeError(
                    "动作等待超时，自动循环已中止；"
                    f"目标={target_pose}，实际={actual_pose}，最大误差={target_error}"
                )

            last_pose = actual_pose
            self.sleep(self.settle_poll)

    def sample_touch(self, count: int = 5, interval: float = 0.05) -> list[float]:
        if count < 1:
            raise ValueError("采样次数必须大于 0")
        samples: list[list[float]] = []
        for index in range(count):
            samples.append(validate_touch(self.hand.get_touch()))
            if index < count - 1 and interval:
                self.sleep(interval)
        width = min(len(sample) for sample in samples)
        return [round(fmean(sample[column] for sample in samples), 3) for column in range(width)]

    def set_touch_baseline(self, count: int = 10) -> None:
        self.touch_baseline = self.sample_touch(count)
        print(f"已记录空载触觉基线: {self.touch_baseline}")
        if self.autosave:
            self.write()

    def record_touch(self, name: str, count: int = 5) -> None:
        name = normalize_pose_name(name)
        values = self.sample_touch(count)
        self.touch_samples[name] = values
        print(f"已记录 {name} 触觉: {values}")
        if self.touch_baseline:
            delta = [round(value - base, 3) for value, base in zip(values, self.touch_baseline)]
            print(f"相对基线增量: {delta}")
        if self.autosave:
            self.write()

    def run_cycle(self, count: int) -> None:
        if count < 1:
            raise ValueError("循环次数必须大于 0")
        missing = [name for name, pose in self.poses.items() if pose is None]
        if missing:
            raise ValueError(f"自动循环前请先保存全部姿态，缺少: {', '.join(missing)}")
        sequence = ("open", "pre_pinch", "pinch", "release", "open")
        for cycle_index in range(count):
            print(f"循环 {cycle_index + 1}/{count}")
            for name in sequence:
                print(f"  -> {name}")
                self.goto_pose(name)
                status, actual_pose = self.wait_until_settled(self.poses[name])
                status_text = "已到位" if status == "reached" else "接触后已稳定"
                print(f"     {status_text}: {actual_pose}")
                if name == "pinch":
                    self.sleep(self.hold_time)

    def document(self) -> dict[str, Any]:
        protocol = "RS485" if self.modbus != "None" else "CAN"
        return {
            "version": 1,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "device": {
                "model": "O6",
                "hand_type": self.hand_type,
                "protocol": protocol,
                "can_channel": self.can_channel if protocol == "CAN" else None,
                "modbus_port": self.modbus if protocol == "RS485" else None,
            },
            "joint_order": list(JOINT_NAMES),
            "parameters": {
                "speed": self.speed,
                "torque": self.torque,
                "manual_step": self.step,
                "jog_delay": self.jog_delay,
                "max_joint_step": self.max_joint_step,
                "position_tolerance": self.position_tolerance,
                "settle_min_time": self.settle_min_time,
                "settle_timeout": self.settle_timeout,
            },
            "poses": self.poses.copy(),
            "actions": self.custom_actions.copy(),
            "shortcuts": self.shortcuts.copy(),
            "touch": {
                "baseline": self.touch_baseline,
                "samples": self.touch_samples.copy(),
            },
        }

    def load(self, path: Path | None = None) -> Path:
        input_path = path or self.output_path
        if not input_path.is_file():
            raise ValueError(f"标定文件不存在: {input_path}")
        try:
            with input_path.open("r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"无法读取标定文件 {input_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("标定文件根节点必须是映射")
        device = data.get("device", {})
        if device and device.get("model") != "O6":
            raise ValueError(f"标定文件型号不是 O6: {device.get('model')}")
        stored_poses = data.get("poses")
        if not isinstance(stored_poses, dict):
            raise ValueError("标定文件缺少 poses")
        loaded: list[str] = []
        for name in POSE_NAMES:
            pose = stored_poses.get(name)
            if pose is not None:
                self.poses[name] = validate_pose(pose)
                loaded.append(name)

        stored_actions = data.get("actions", {})
        if stored_actions is not None and not isinstance(stored_actions, dict):
            raise ValueError("标定文件中的 actions 必须是映射")
        self.custom_actions = {
            str(name): validate_pose(pose)
            for name, pose in (stored_actions or {}).items()
        }
        stored_shortcuts = data.get("shortcuts", {})
        if stored_shortcuts is not None and not isinstance(stored_shortcuts, dict):
            raise ValueError("标定文件中的 shortcuts 必须是映射")
        self.shortcuts = {}
        for shortcut, action_name in (stored_shortcuts or {}).items():
            canonical_shortcut = normalize_shortcut(str(shortcut))
            resolved_name, _ = self._resolve_action(str(action_name))
            self.shortcuts[canonical_shortcut] = resolved_name

        touch = data.get("touch", {})
        if isinstance(touch, dict):
            baseline = touch.get("baseline")
            if baseline is not None:
                self.touch_baseline = validate_touch(baseline)
            samples = touch.get("samples")
            if isinstance(samples, dict):
                self.touch_samples = {
                    str(name): validate_touch(values)
                    for name, values in samples.items()
                    if values is not None
                }
        print(f"已加载标定文件: {input_path}")
        print(f"已加载姿态: {', '.join(loaded) if loaded else '无'}")
        if self.custom_actions:
            print(f"已加载自定义动作: {', '.join(self.custom_actions)}")
        if self.shortcuts:
            bindings = ", ".join(f"{key}->{value}" for key, value in self.shortcuts.items())
            print(f"已加载快捷键: {bindings}")
        return input_path

    def write(self, path: Path | None = None) -> Path:
        output_path = path or self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(self.document(), stream, allow_unicode=True, sort_keys=False)
        print(f"标定结果已写入: {output_path}")
        return output_path


HELP = """
命令：
  jog                          进入无需回车的实时键盘微调
  show                         显示当前编辑姿态和已保存姿态
  state                        重新读取灵巧手实际位置
  add <0-5> <增量>             调整单个关节，例如 add 2 -5
  + <0-5> [步长]               增大关节值，默认使用 --step
  - <0-5> [步长]               减小关节值，默认使用 --step
  set <0-5> <0-255>            设置单个关节绝对值
  move <六个关节值>             平滑移动到完整姿态，支持空格、CSV 或列表格式
  save <姿态名>                保存当前姿态并自动写入 YAML
  saveas <动作名>              将当前位置保存为任意命名的动作
  play <动作名>                回放内置姿态或自定义动作
  play cycle [次数]            回放完整循环
  actions                      列出姿态、自定义动作和快捷键
  bind <快捷键> <动作名>        绑定 alt+字母 或 ctrl+字母
  unbind <快捷键>              解除快捷键
  delete <动作名>              删除自定义动作
  goto <姿态名>                移动到已保存姿态
  touch [次数]                 读取触觉平均值
  baseline [次数]              保存空载触觉基线
  sample <姿态名> [次数]        保存当前触觉样本并显示基线增量
  force                        读取综合力传感器数据
  cycle [次数]                 循环 open→pre_pinch→pinch→release
  load [文件路径]              从 YAML 加载已保存姿态
  write [文件路径]             写入 YAML
  help                         显示帮助
  quit                         退出，不额外移动灵巧手

姿态名：open/张开、pre_pinch/预夹、pinch/夹牌、release/松开
关节号：0拇指弯曲，1拇指横摆，2食指弯曲，3中指，4无名指，5小指
""".strip()


def jog_status(session: CalibrationSession, state: JogState) -> str:
    joint_name = JOINT_NAMES[state.joint_index]
    return (
        f"关节 {state.joint_index + 1} {joint_name}，步长={state.step}，"
        f"姿态={session.current_pose}"
    )


def handle_jog_key(session: CalibrationSession, state: JogState, key: str) -> bool:
    """Apply one normalized jog key. Return False to leave jog mode."""
    if key in {"q", "Q", "CTRL_C"}:
        return False
    if key in session.shortcuts:
        print(f"\n快捷键 {key}: {session.shortcuts[key]}")
        session.play_action(session.shortcuts[key])
    elif key in {"UP", "DOWN"}:
        direction = -1 if key == "UP" else 1
        state.joint_index = (state.joint_index + direction) % 6
    elif key in {"1", "2", "3", "4", "5", "6"}:
        state.joint_index = int(key) - 1
    elif key in {"LEFT", "-", "a", "A"}:
        session.adjust_joint(state.joint_index, -state.step, duration=session.jog_delay)
    elif key in {"RIGHT", "+", "=", "d", "D"}:
        session.adjust_joint(state.joint_index, state.step, duration=session.jog_delay)
    elif key in {"[", "]"}:
        closest_index = min(range(len(JOG_STEPS)), key=lambda index: abs(JOG_STEPS[index] - state.step))
        offset = -1 if key == "[" else 1
        state.step = JOG_STEPS[min(len(JOG_STEPS) - 1, max(0, closest_index + offset))]
    elif key in {"o", "O"}:
        session.save_pose("open")
    elif key in {"b", "B"}:
        session.save_pose("pre_pinch")
    elif key in {"p", "P"}:
        session.save_pose("pinch")
    elif key in {"r", "R"}:
        session.save_pose("release")
    elif key in {"h", "H", "?"}:
        print(f"\n{JOG_HELP}")
    return True


def read_jog_key() -> str:
    char = sys.stdin.read(1)
    if char == "\x03":
        return "CTRL_C"
    if 1 <= ord(char) <= 26:
        return f"CTRL+{chr(ord('A') + ord(char) - 1)}"
    if char != "\x1b":
        return char
    second = sys.stdin.read(1)
    if second != "[":
        return f"ALT+{second.upper()}" if second.isalpha() else "ESC"
    third = sys.stdin.read(1)
    return {
        "A": "UP",
        "B": "DOWN",
        "C": "RIGHT",
        "D": "LEFT",
    }.get(third, "ESC")


def run_jog_mode(session: CalibrationSession) -> None:
    if not sys.stdin.isatty():
        raise ValueError("jog 模式需要在交互式终端中运行")
    try:
        import termios
        import tty
    except ImportError as exc:
        raise RuntimeError("当前系统不支持终端实时按键模式") from exc

    state = JogState(step=session.step if session.step in JOG_STEPS else 5)
    file_descriptor = sys.stdin.fileno()
    previous_settings = termios.tcgetattr(file_descriptor)
    print(JOG_HELP)
    print(jog_status(session, state), end="", flush=True)
    try:
        tty.setcbreak(file_descriptor)
        while True:
            key = read_jog_key()
            if key in session.shortcuts or key in {"o", "O", "b", "B", "p", "P", "r", "R", "h", "H", "?"}:
                print()
            if not handle_jog_key(session, state, key):
                break
            print(f"\r\033[2K{jog_status(session, state)}", end="", flush=True)
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous_settings)
        print("\n已退出实时微调模式")


def print_session(session: CalibrationSession) -> None:
    print(f"当前姿态: {session.current_pose}")
    for index, (name, value) in enumerate(zip(JOINT_NAMES, session.current_pose)):
        print(f"  {index}: {name:<6} = {value}")
    print("已保存姿态：")
    for name in POSE_NAMES:
        print(f"  {name:<10} {session.poses[name]}")
    if session.custom_actions:
        print("自定义动作：")
        for name, pose in session.custom_actions.items():
            print(f"  {name:<10} {pose}")
    if session.shortcuts:
        print("快捷键：")
        for shortcut, action_name in session.shortcuts.items():
            print(f"  {shortcut:<10} -> {action_name}")


def execute_command(session: CalibrationSession, line: str) -> bool:
    """Execute one command. Return False when the REPL should stop."""
    parts = shlex.split(line)
    if not parts:
        return True
    command, *args = parts
    command = command.lower()

    if command in {"quit", "exit", "q"}:
        return False
    if command in {"help", "h", "?"}:
        print(HELP)
    elif command == "jog":
        run_jog_mode(session)
    elif command == "show":
        print_session(session)
    elif command == "state":
        session.current_pose = validate_pose(session.hand.get_state())
        print(f"实际位置: {session.current_pose}")
    elif command == "add":
        if len(args) != 2:
            raise ValueError("用法: add <关节号> <增量>")
        session.adjust_joint(int(args[0]), int(args[1]))
        print(f"当前姿态: {session.current_pose}")
    elif command in {"+", "-"}:
        if len(args) not in {1, 2}:
            raise ValueError(f"用法: {command} <关节号> [步长]")
        amount = int(args[1]) if len(args) == 2 else session.step
        session.adjust_joint(int(args[0]), amount if command == "+" else -amount)
        print(f"当前姿态: {session.current_pose}")
    elif command == "set":
        if len(args) != 2:
            raise ValueError("用法: set <关节号> <0-255>")
        session.set_joint(int(args[0]), int(args[1]))
        print(f"当前姿态: {session.current_pose}")
    elif command == "move":
        session.move(parse_pose_arguments(args))
        print(f"当前姿态: {session.current_pose}")
    elif command == "save":
        if len(args) != 1:
            raise ValueError("用法: save <姿态名>")
        session.save_pose(args[0])
    elif command in {"saveas", "record"}:
        if not args:
            raise ValueError("用法: saveas <动作名>")
        session.save_custom_action(" ".join(args))
    elif command in {"play", "播放"}:
        if not args:
            raise ValueError("用法: play <动作名> 或 play cycle [次数]")
        action = args[0].strip().strip("<>").lower()
        if action in {"cycle", "循环"}:
            if len(args) > 2:
                raise ValueError("用法: play cycle [次数]")
            session.run_cycle(int(args[1]) if len(args) == 2 else 1)
        else:
            session.play_action(" ".join(args))
    elif command in {"actions", "动作"}:
        print_session(session)
    elif command == "bind":
        if len(args) < 2:
            raise ValueError("用法: bind <alt+字母|ctrl+字母> <动作名>")
        session.bind_shortcut(args[0], " ".join(args[1:]))
    elif command == "unbind":
        if len(args) != 1:
            raise ValueError("用法: unbind <快捷键>")
        session.unbind_shortcut(args[0])
    elif command == "delete":
        if not args:
            raise ValueError("用法: delete <动作名>")
        session.delete_custom_action(" ".join(args))
    elif command == "goto":
        if len(args) != 1:
            raise ValueError("用法: goto <姿态名>")
        session.goto_pose(args[0])
        print(f"当前姿态: {session.current_pose}")
    elif command == "touch":
        count = int(args[0]) if args else 5
        print(f"触觉平均值: {session.sample_touch(count)}")
    elif command == "baseline":
        session.set_touch_baseline(int(args[0]) if args else 10)
    elif command == "sample":
        if not args:
            raise ValueError("用法: sample <姿态名> [次数]")
        session.record_touch(args[0], int(args[1]) if len(args) > 1 else 5)
    elif command == "force":
        print(f"综合力数据: {session.hand.get_force()}")
    elif command == "cycle":
        session.run_cycle(int(args[0]) if args else 1)
    elif command == "load":
        session.load(Path(args[0]).expanduser() if args else None)
    elif command == "write":
        session.write(Path(args[0]).expanduser() if args else None)
    else:
        raise ValueError(f"未知命令: {command}；输入 help 查看帮助")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkerHand O6 扑克牌夹持标定工具")
    parser.add_argument("--hand-type", choices=("left", "right"), default="right")
    parser.add_argument("--can", default="can0", help="CAN 接口，默认 can0")
    parser.add_argument("--modbus", default="None", help="RS485 端口，例如 /dev/ttyUSB0")
    parser.add_argument("--speed", type=int, default=50, help="六关节速度，默认 50")
    parser.add_argument("--torque", type=int, default=60, help="六关节扭矩限制，默认 60")
    parser.add_argument("--step", type=int, default=5, help="手动微调步长，默认 5")
    parser.add_argument("--max-joint-step", type=int, default=5, help="平滑移动单次最大变化量")
    parser.add_argument("--move-delay", type=float, default=0.4, help="一次平滑移动的总时长")
    parser.add_argument("--jog-delay", type=float, default=0.08, help="实时微调单次动作时长")
    parser.add_argument("--hold", type=float, default=1.0, help="pinch 实际稳定后的保持时间")
    parser.add_argument("--position-tolerance", type=int, default=5, help="关节到位/稳定容差")
    parser.add_argument("--settle-min-time", type=float, default=0.5, help="判断动作稳定前的最短等待")
    parser.add_argument("--settle-timeout", type=float, default=5.0, help="等待动作完成的超时时间")
    parser.add_argument("--settle-poll", type=float, default=0.05, help="实际位置轮询间隔")
    parser.add_argument("--stable-samples", type=int, default=3, help="判定停止需要的连续稳定采样数")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-load", action="store_true", help="启动时不自动加载已有标定文件")
    parser.add_argument("--no-autosave", action="store_true", help="保存姿态后不自动写入 YAML")
    parser.add_argument("--play", help="加载后直接回放姿态或 cycle，不进入交互界面")
    parser.add_argument("--count", type=int, default=1, help="--play cycle 的循环次数")
    parser.add_argument("--dry-run", action="store_true", help="不连接硬件，用模拟数据验证流程")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in ("speed", "torque", "step", "max_joint_step"):
        value = getattr(args, name)
        if not 1 <= value <= 255:
            raise ValueError(f"--{name.replace('_', '-')} 必须在 1～255 之间")
    if not 0 <= args.position_tolerance <= 255:
        raise ValueError("--position-tolerance 必须在 0～255 之间")
    if args.stable_samples < 1:
        raise ValueError("--stable-samples 必须大于 0")
    if args.count < 1:
        raise ValueError("--count 必须大于 0")
    if min(args.move_delay, args.jog_delay, args.hold, args.settle_min_time, args.settle_timeout, args.settle_poll) < 0:
        raise ValueError("移动和保持时间不能为负数")
    if args.settle_timeout < args.settle_min_time:
        raise ValueError("--settle-timeout 不能小于 --settle-min-time")


def create_hand(args: argparse.Namespace) -> Any:
    if args.dry_run:
        return DryRunHand()
    from LinkerHand.linker_hand_api import LinkerHandApi

    return LinkerHandApi(
        hand_type=args.hand_type,
        hand_joint="O6",
        modbus=args.modbus,
        can=args.can,
    )


def close_hand(hand: Any) -> None:
    driver = getattr(hand, "hand", hand)
    close_method = getattr(driver, "close_can_interface", None) or getattr(driver, "close", None)
    if close_method:
        close_method()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    hand = create_hand(args)
    session = CalibrationSession(
        hand,
        hand_type=args.hand_type,
        can_channel=args.can,
        modbus=args.modbus,
        output_path=args.output,
        speed=args.speed,
        torque=args.torque,
        step=args.step,
        max_joint_step=args.max_joint_step,
        move_delay=args.move_delay,
        jog_delay=args.jog_delay,
        hold_time=args.hold,
        position_tolerance=args.position_tolerance,
        settle_min_time=args.settle_min_time,
        settle_timeout=args.settle_timeout,
        settle_poll=args.settle_poll,
        stable_samples=args.stable_samples,
        autosave=not args.no_autosave,
        dry_run=args.dry_run,
    )
    try:
        if not args.no_load and args.output.is_file():
            session.load()
        session.configure_limits()
        if args.play:
            action = args.play.strip().strip("<>").lower()
            if action in {"cycle", "循环"}:
                session.run_cycle(args.count)
            else:
                session.play_action(args.play)
            return 0
        mode = "无硬件演练" if args.dry_run else "硬件标定"
        print(f"O6 扑克牌标定程序（{mode}）")
        print(f"速度={args.speed}，扭矩={args.torque}，微调步长={args.step}")
        print("启动后未发送位置命令。输入 help 查看命令，先用 show 检查当前位置。")
        while True:
            try:
                if not execute_command(session, input("o6-cal> ")):
                    break
            except (ValueError, OSError, RuntimeError) as exc:
                print(f"错误: {exc}")
            except KeyboardInterrupt:
                print("\n当前操作已中断；灵巧手保持最后命令位置。")
    except (EOFError, KeyboardInterrupt):
        print("\n退出标定程序；灵巧手保持最后命令位置。")
    finally:
        close_hand(hand)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
