"""机械臂总线协议: Agent 发 arm_command, 臂侧回 arm_ack。

设计纪律与慢循环一致: **臂可以死** —— 任何指令超时/失败, ArmClient 返 False,
上层自动降级人肉提词(提词器一直都在), 牌局永不卡死。
机械臂组只需实现一个订阅者: 收 arm_command → 执行 → 回 arm_ack(同 id)。
联调用 SimArm 顶替, 真臂到货换掉即可, Agent 侧零改动。
"""

from __future__ import annotations

import threading
import time


class _ArmActions:
    """动作语义封装, ArmClient(总线/模拟) 与 HttpArm(真臂 server) 共用。"""

    ACTIONS = ("home", "pick_from_deck", "present_to_camera", "deal_to", "sweep")

    def home(self) -> bool:
        return self.command("home")

    def pick_from_deck(self, present: bool = True) -> bool:
        return self.command("pick_from_deck")

    def present_to_camera(self) -> bool:
        return self.command("present_to_camera")

    def deal_to(self, zone: str, face: str = "down") -> bool:
        return self.command("deal_to", zone=zone, face=face)

    def sweep(self) -> bool:
        return self.command("sweep")


class HttpArm(_ArmActions):
    """真臂: 臂控电脑(局域网)开 HTTP server, 本类是 Spark 侧客户端。

    契约(见 arm_side/README.md): POST {base}/arm {"action","zone","face"}
    → {"ok": true|false, "reason": ""}; GET /health 探活。
    超时/连不上/ok=false 一律返 False, 上层自动降级人肉提词。
    """

    def __init__(self, base_url: str, timeout_s: float = 25.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s     # 臂动作本身要几秒, 上限放宽
        self.alive = True
        self._fails = 0

    def health(self) -> bool:
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3) as r:
                return bool(json.load(r).get("ok"))
        except Exception:
            return False

    def command(self, action: str, **kw) -> bool:
        import json
        import urllib.request
        try:
            body = json.dumps({"action": action, **kw}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/arm", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                ok = bool(json.load(r).get("ok"))
        except Exception:
            ok = False
        self._fails = 0 if ok else self._fails + 1
        self.alive = self._fails < 2
        return ok


class PantheraArm:
    """对接机械臂组的 LAN 协调服务器 —— **阶段级宏观原语**模式。

    发手牌/翻牌/转牌/河牌各提交为**一条完整 run**(臂+灵巧手自主执行, 每阶段只回零一次):
        发手牌: [hover,pick,camera,P1a] × 8 人牌
        翻牌:   [hover,pick,MUCK] + [hover,pick,camera,C1..C3]
        转/河:  [hover,pick,MUCK] + [hover,pick,camera,C4/C5]
    Agent 唯一介入点 = 每个 camera 步的 hand_window: 臂悬停亮牌, Spark 认牌完成后
    回 done 放行(deal_to 触发) —— 认牌耗时任意长皆可; 其余步的 done 由灵巧手电脑回。
    宏观 run 由编排器的 on_need 钩子在每阶段首个 DEAL 需求时提交;
    引擎/顶视落位核验仍逐张进行, 与臂的连续动作天然流水线。
    任何失败/超时返 False → 上层人肉降级; 宏观 run 会被 /api/stop 停掉。
    """

    def __init__(self, base_url: str, token: str = "", points: dict | None = None,
                 n_players: int = 4, bus=None,
                 hold_time: float = 0.5, recognition_timeout: float = 120.0,
                 wait_hand: bool = True, http_timeout: float = 10.0,
                 run_timeout: float = 120.0, poll_s: float = 0.3):
        self.bus = bus                # 可选: 轨迹提交/失败发 agent_trace 进日志
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.points = points or {}
        self.n_players = n_players
        self.hold_time = hold_time
        self.recognition_timeout = recognition_timeout
        self.wait_hand = wait_hand
        self.http_timeout = http_timeout
        self.run_timeout = run_timeout
        self.poll_s = poll_s
        # 进行中的宏观 run: {rid, zones[卡片区顺序], camera_steps[对应步号], ci 当前卡}
        self._macro: dict | None = None
        self.alive = True
        self._fails = 0

    # ---- HTTP ----

    def _api(self, path: str, payload: dict | None = None) -> dict:
        import json
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Panthera-Token"] = self.token
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:      # 409 已有任务等: 读 body 报因
            body = json.loads(e.read() or b"{}")
        if not body.get("success"):
            raise RuntimeError(body.get("error", "arm server error"))
        return body

    def health(self) -> bool:
        try:
            self._api("/api/health")
            return True
        except Exception:
            return False

    def _status(self) -> dict:
        return self._api("/api/status")["status"]

    def _start(self, refs: list[str]) -> str:
        st = self._api("/api/run", {
            "sequence": ",".join(refs),
            "hold_time": self.hold_time,
            "pause_mode": "hold",
            "wait_for_hand_done": self.wait_hand,
            "hand_timeout": self.recognition_timeout,
        })["status"]
        return st["run_id"]

    def _post_done(self, run_id: str, step: int) -> None:
        self._api("/api/hand/status", {
            "computer_id": "spark-agent", "state": "done",
            "run_id": run_id, "step": step})

    def _wait(self, run_id: str, until, timeout: float) -> dict:
        """轮询 status 直到 until(st) 为真; error/stopped 或超时抛异常。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._status()
            if st.get("run_id") == run_id and st.get("phase") in ("error", "stopped"):
                raise RuntimeError(f"arm run {st['phase']}: {st.get('message')}")
            if until(st):
                return st
            time.sleep(self.poll_s)
        raise RuntimeError("arm run 超时")

    def _wait_complete(self, run_id: str) -> None:
        self._wait(run_id, lambda st: st.get("run_id") == run_id
                   and st.get("phase") == "complete", self.run_timeout)

    def _refs(self, *names: str) -> list[str]:
        missing = [n for n in names if n not in self.points]
        if missing:
            raise RuntimeError(f"arm_points.yaml 缺点位映射: {missing}")
        return [self.points[n] for n in names]

    def _ok(self, fn) -> bool:
        try:
            fn()
            self._fails = 0
            self.alive = True
            return True
        except Exception as exc:
            print(f"  ⚠ 机械臂: {exc}")
            self._fails += 1
            self.alive = self._fails < 2
            return False

    # ---- 宏观编排 ----

    def _hole_order(self, first_zone: str) -> list[str]:
        order = [f"P{i + 1}a" for i in range(self.n_players)] + \
                [f"P{i + 1}b" for i in range(self.n_players)]
        return order[order.index(first_zone):] if first_zone in order else order

    def _wait_idle(self) -> None:
        """新阶段开始前等上一条宏观 run 收尾(放完最后一张后的回零)。"""
        deadline = time.time() + self.run_timeout
        while time.time() < deadline:
            st = self._status()
            if not st.get("running") and st.get("phase") not in ("moving", "hand_window",
                                                                 "queued", "reset"):
                return
            time.sleep(self.poll_s)
        raise RuntimeError("上一条轨迹迟迟未结束")

    def _start_macro(self, zones: list[str], burn: bool) -> None:
        refs: list[str] = []
        camera_steps: list[int] = []
        if burn:
            refs += self._refs("deck_hover", "deck_pick", "MUCK")
        for z in zones:
            refs += self._refs("deck_hover", "deck_pick", "camera", z)
            camera_steps.append(len(refs) - 1)   # camera 是本组第 3 步
        self._wait_idle()
        rid = self._start(refs)
        self._macro = {"rid": rid, "zones": list(zones),
                       "camera_steps": camera_steps, "ci": 0}
        if self.bus:
            self.bus.emit({"type": "agent_trace",
                           "text": f"🤖 臂轨迹已提交({len(refs)}步): "
                                   f"{'烧牌+' if burn else ''}{'/'.join(zones)}"})

    def _macro_alive(self) -> bool:
        if self._macro is None:
            return False
        if self._macro["ci"] < len(self._macro["zones"]):
            return True
        self._macro = None       # 所有卡片已放行, 剩余回零自走
        return False

    def on_need(self, need) -> None:
        """编排器每个 DEAL 需求调用: 阶段首卡时提交整段宏观 run。"""
        if not self.alive or need.kind != "DEAL":
            return
        try:
            if self._macro_alive():
                return
            if need.subkind == "hole":
                self._start_macro(self._hole_order(need.zones[0]), burn=False)
            elif need.subkind == "burn":
                zones = {"flop": ["C1", "C2", "C3"], "turn": ["C4"],
                         "river": ["C5"]}.get(need.street, [])
                if zones:
                    self._start_macro(zones, burn=True)
            elif need.subkind == "board":
                self._start_macro([need.zones[0]], burn=False)  # 恢复场景的保险
        except Exception as exc:
            print(f"  ⚠ 机械臂宏观编排失败: {exc}")
            if self.bus:
                self.bus.emit({"type": "alert", "text": f"机械臂轨迹提交失败: {exc}"})
            self._fails += 1
            self.alive = self._fails < 2

    # ---- 动作原语 (与 ArmClient/HttpArm 同接口, 语义映射到宏观 run) ----

    def home(self) -> bool:
        return True   # 每条 run 结束服务器自动回零位, 即待机

    def pick_from_deck(self, present: bool = True) -> bool:
        # 途经识别: 不等亮牌窗口 —— 臂取牌/移动期间 Spark 持续读相机流,
        # 认出即提前回 done(服务器支持), 臂在亮牌点只作最短停顿; 认不出时
        # 窗口自然拦住臂, 认多久等多久。烧牌步同样自走(灵巧手回 done)。
        return self._macro is not None

    def present_to_camera(self) -> bool:
        return self._macro is not None   # pick 已等到 camera 窗口

    def deal_to(self, zone: str, face: str = "down") -> bool:
        if zone == "MUCK":
            return self._macro is not None   # 烧牌落位由宏观 run 完成
        def go():
            m = self._macro
            if m is None:
                raise RuntimeError("无进行中的宏观轨迹")
            expected = m["zones"][m["ci"]]
            if zone != expected:
                self.stop()
                self._macro = None
                raise RuntimeError(f"目标区改变({expected}→{zone}), 已停宏观轨迹")
            self._post_done(m["rid"], m["camera_steps"][m["ci"]])  # 放行, 臂去放牌
            m["ci"] += 1
        return self._ok(go)

    def sweep(self) -> bool:
        return False

    def stop(self) -> None:
        try:
            self._api("/api/stop", {})
        except Exception:
            pass


class PantheraBatchArm:
    """对接臂组"程序制"调度服务器 (arm_side/AGENT_LINUX_BATCH_INTEGRATION 文档)。

    臂侧已把动作封装为整程序: OPENING=连发 8 张底牌, C1..C5=发单张公共牌
    (取→亮→放全流程); 灵巧手握手在臂控电脑内部完成。Agent 职责只剩:
    - 每阶段首个 DEAL 需求时提交程序/批次(POST 202, 轮询 status 至 idle 才能发下一条);
    - 识别为纯旁路: 牌途经 Link2C 时 ShoeGod 持续读流认出, 不阻塞臂;
    - 机器流程无实体烧牌 → skip_burn=True, 编排器自动逻辑烧牌。
    任何失败返 False → 人肉降级; /api/stop 软停。
    """

    skip_burn = True

    def __init__(self, base_url: str, token: str = "", bus=None,
                 http_timeout: float = 10.0, idle_timeout: float = 420.0,
                 poll_s: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.bus = bus
        self.http_timeout = http_timeout
        self.idle_timeout = idle_timeout
        self.poll_s = poll_s
        self._submitted: set[str] = set()   # 已提交的阶段(一手一进程, 无需复位)
        self.alive = True
        self._fails = 0

    def _api(self, path: str, payload: dict | None = None) -> dict:
        import json
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Panthera-Token"] = self.token
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            import json as _j
            body = _j.loads(e.read() or b"{}")
            raise RuntimeError(body.get("error", f"HTTP {e.code}"))

    def health(self) -> bool:
        try:
            self._api("/api/status")
            return True
        except Exception:
            return False

    def _idle(self) -> bool:
        st = self._api("/api/status").get("status", {})
        return not st.get("running") and st.get("phase") == "idle"

    def _wait_idle(self) -> None:
        deadline = time.time() + self.idle_timeout
        while time.time() < deadline:
            if self._idle():
                return
            time.sleep(self.poll_s)
        raise RuntimeError("臂长时间未回到 idle")

    def _submit(self, key: str, label: str, post) -> None:
        self._wait_idle()
        try:
            post()
        except Exception:
            # 幂等保护: 网络抖动可能在服务器已受理后才断 —— 盲目重试会让臂
            # 把同一街发两遍牌。查一次状态: 臂在跑就视为提交成功。
            time.sleep(self.poll_s)
            try:
                if not self._idle():
                    self._submitted.add(key)
                    if self.bus:
                        self.bus.emit({"type": "agent_trace",
                                       "text": f"🤖 臂指令提交遇网络抖动但已受理: {label}"})
                    return
            except Exception:
                pass
            raise
        self._submitted.add(key)
        if self.bus:
            self.bus.emit({"type": "agent_trace", "text": f"🤖 臂指令已提交: {label}"})

    def on_need(self, need) -> None:
        if not self.alive or need.kind != "DEAL":
            return
        try:
            if need.subkind == "hole" and "holes" not in self._submitted:
                if not self._idle():
                    # 臂侧已本地触发"发牌"(OPENING 在跑): 采用之, 不重复提交
                    self._submitted.add("holes")
                    if self.bus:
                        self.bus.emit({"type": "agent_trace",
                                       "text": "🤖 臂侧已本地启动发牌, Agent 采用并转入旁路识别"})
                else:   # 发牌意图 → 专用端点(执行 B1..B8 后回 Reset 等待)
                    self._submit("holes", "发牌(B1..B8)",
                                 lambda: self._api("/api/deal/start", {}))
            elif need.subkind in ("burn", "board"):
                street = need.street
                if street in self._submitted:
                    return
                programs = {"flop": ["C1", "C2", "C3"], "turn": ["C4"],
                            "river": ["C5"]}.get(street)
                if programs is None:
                    return
                if len(programs) == 1:
                    self._submit(street, programs[0],
                                 lambda: self._api(f"/api/programs/{programs[0]}/run", {}))
                else:
                    self._submit(street, "+".join(programs),
                                 lambda: self._api("/api/batch/run",
                                                   {"programs": programs}))
        except Exception as exc:
            print(f"  ⚠ 臂程序提交失败: {exc}")
            if self.bus:
                self.bus.emit({"type": "alert", "text": f"机械臂程序提交失败: {exc}"})
            self._fails += 1
            self.alive = self._fails < 2

    # ---- 原语接口: 批次模式下臂自主执行, 这些只是"进行中"信号 ----

    def pick_from_deck(self, present: bool = True) -> bool:
        return self.alive and bool(self._submitted)

    def present_to_camera(self) -> bool:
        return self.alive and bool(self._submitted)

    def deal_to(self, zone: str, face: str = "down") -> bool:
        return True    # 放牌在程序内; 落位由顶视 watch 独立核验

    def home(self) -> bool:
        return True

    def sweep(self) -> bool:
        return False

    def stop(self) -> None:
        try:
            self._api("/api/stop", {})
        except Exception:
            pass


class ArmClient(_ArmActions):
    """模拟/总线路径: 同步指令接口, 内部走总线等回执(联调 SimArm 用)。"""

    def __init__(self, bus, timeout_s: float = 15.0):
        self.bus = bus
        self.timeout_s = timeout_s
        self._acks: dict[int, dict] = {}
        self._next_id = 0
        self.alive = True          # 连续失败后置 False, 上层可显示"臂离线"
        self._fails = 0
        bus.subscribe(self._on_msg)

    def _on_msg(self, msg: dict) -> None:
        if msg.get("type") == "arm_ack":
            self._acks[msg["id"]] = msg

    def command(self, action: str, **kw) -> bool:
        """发指令并等回执; 超时/ok=false 返 False(上层降级人肉)。"""
        self._next_id += 1
        cid = self._next_id
        self.bus.emit({"type": "arm_command", "id": cid, "action": action, **kw})
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            ack = self._acks.pop(cid, None)
            if ack is not None:
                ok = bool(ack.get("ok"))
                self._fails = 0 if ok else self._fails + 1
                self.alive = self._fails < 2
                return ok
            time.sleep(0.05)
        self._fails += 1
        self.alive = self._fails < 2
        return False


class SimArm:
    """模拟机械臂: 收 arm_command 延时回 ack。全链路联调/测试用。

    fail_actions 可注入故障(演练"臂罢工→人肉降级"); 真臂到货后
    机械臂组按同协议实现订阅者, 本类退役。
    """

    def __init__(self, bus, delay_s: float = 0.4, fail_actions: tuple = ()):
        self.bus = bus
        self.delay_s = delay_s
        self.fail_actions = set(fail_actions)
        self.log: list[dict] = []
        bus.subscribe(self._on_msg)

    def _on_msg(self, msg: dict) -> None:
        if msg.get("type") != "arm_command":
            return
        self.log.append(msg)

        def ack():
            time.sleep(self.delay_s)
            ok = msg["action"] not in self.fail_actions
            self.bus.emit({"type": "arm_ack", "id": msg["id"], "ok": ok,
                           **({} if ok else {"reason": "simulated failure"})})

        threading.Thread(target=ack, daemon=True).start()
