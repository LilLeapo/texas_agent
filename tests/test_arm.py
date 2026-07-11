"""机械臂协议: 指令回执、超时降级、臂驱动的取牌/发牌序列, 臂罢工人肉接管。"""

import pytest

from texas_agent.arm import ArmClient, SimArm
from texas_agent.bus import LocalBus
from texas_agent.engine import Engine
from texas_agent.gto import GtoCharts
from texas_agent.orchestrator import (MockVision, Orchestrator, PresetGod,
                                      ScriptedInputs, ScriptedOps)
from texas_agent.prompter import Prompter

SCRIPT = [("call", None)] * 3 + [("check", None)] + [("check", None)] * 12


@pytest.fixture
def bus():
    b = LocalBus(log=False)
    b.msgs = []
    b.subscribe(b.msgs.append)
    yield b
    b.close()


def make_http_stub(responses):
    """本地 HTTP 假臂 server: 按 action 返回 ok/fail。返回 (port, 收到的请求列表)。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply({"ok": True})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            self._reply(responses.get(body["action"], {"ok": True}))

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_port, seen


def test_http_arm_roundtrip():
    from texas_agent.arm import HttpArm
    port, seen = make_http_stub({})
    arm = HttpArm(f"http://127.0.0.1:{port}", timeout_s=3)
    assert arm.health() is True
    assert arm.deal_to("C1", face="up") is True
    assert seen[-1] == {"action": "deal_to", "zone": "C1", "face": "up"}
    assert arm.alive


def test_http_arm_failure_and_dead_server():
    from texas_agent.arm import HttpArm
    port, _ = make_http_stub({"pick_from_deck": {"ok": False, "reason": "miss"}})
    arm = HttpArm(f"http://127.0.0.1:{port}", timeout_s=3)
    assert arm.pick_from_deck() is False
    assert arm.home() is True            # 单次失败不判死
    dead = HttpArm("http://127.0.0.1:1", timeout_s=0.3)
    assert dead.health() is False
    assert dead.command("home") is False
    assert dead.command("home") is False
    assert not dead.alive                # 连续失败 → 离线, 整手跳过臂


def make_panthera_stub():
    """按机械臂组 robot_orchestrator_server 语义的假服务器:
    /api/run 异步 202 → moving → 每步 hand_window(等 done 若 wait) → complete(回零)。"""
    import json
    import threading
    import time as _t
    from http.server import BaseHTTPRequestHandler, HTTPServer

    S = {"phase": "idle", "run_id": None, "step": None, "runs": [],
         "done": set(), "lock": threading.Lock()}

    def worker(rid, refs, wait_hand):
        for step, _ref in enumerate(refs, 1):
            with S["lock"]:
                S.update(phase="moving", step=step)
            _t.sleep(0.03)
            with S["lock"]:
                S.update(phase="hand_window", step=step)
            t0 = _t.time()
            while wait_hand and (rid, step) not in S["done"]:
                if _t.time() - t0 > 5:
                    with S["lock"]:
                        S.update(phase="error")
                    return
                _t.sleep(0.01)
            if not wait_hand:
                _t.sleep(0.02)
        with S["lock"]:
            S.update(phase="complete", step=len(refs))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/health"):
                return self._reply({"success": True})
            with S["lock"]:
                st = {"phase": S["phase"], "run_id": S["run_id"], "step": S["step"]}
            self._reply({"success": True, "status": st})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith("/api/run"):
                refs = body["sequence"].split(",")
                with S["lock"]:
                    rid = f"r{len(S['runs']) + 1}"
                    S.update(run_id=rid, phase="queued", step=0)
                    S["runs"].append(body["sequence"])
                threading.Thread(target=worker, daemon=True,
                                 args=(rid, refs, body.get("wait_for_hand_done"))).start()
                self._reply({"success": True,
                             "status": {"run_id": rid, "phase": "queued"}}, 202)
            elif self.path.startswith("/api/hand/status"):
                S["done"].add((body["run_id"], int(body["step"])))
                self._reply({"success": True})

    srv = HTTPServer(("127.0.0.1", 0), H)
    import threading as _th
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_port, S


POINTS = {"deck_hover": "A1", "deck_pick": "A2", "camera": "A3", "MUCK": "A4",
          "C1": "B1", "C2": "B2", "C3": "B3", "C4": "B4", "C5": "B5",
          "P1a": "B6", "P1b": "B7", "P2a": "B8", "P2b": "B9"}


def make_panthera_arm(port, n_players=2):
    from texas_agent.arm import PantheraArm
    return PantheraArm(f"http://127.0.0.1:{port}", points=POINTS,
                       n_players=n_players,
                       hold_time=0.05, poll_s=0.02, run_timeout=8)


def need(subkind, zone, street="preflop"):
    from texas_agent.engine import Need
    return Need("DEAL", subkind, [zone], street=street,
                face="down" if subkind != "board" else "up")


def hand_autodone(S, spark_owned=("A3",)):
    """模拟灵巧手电脑: wait_for_hand_done 下**每一步**都要回 done ——
    有手部动作的步做完再回, 其余立即回; 只有 camera 步留给 Spark 认牌后回。"""
    import threading
    import time as _t

    def loop():
        for _ in range(500):
            with S["lock"]:
                phase, rid, step = S["phase"], S["run_id"], S["step"]
                seq = S["runs"][-1].split(",") if S["runs"] else []
            if phase == "hand_window" and rid and step and \
                    seq[step - 1] not in spark_owned:
                S["done"].add((rid, step))
            _t.sleep(0.01)
    threading.Thread(target=loop, daemon=True).start()


def test_panthera_holes_macro():
    """发手牌=一条宏观 run: 每张牌停 camera 窗口认牌, deal_to 放行。"""
    port, S = make_panthera_stub()
    hand_autodone(S)
    arm = make_panthera_arm(port, n_players=2)
    assert arm.health()
    for zone in ["P1a", "P2a", "P1b", "P2b"]:
        arm.on_need(need("hole", zone))          # 仅首卡真正提交 run
        assert arm.pick_from_deck() is True      # 等到该卡 camera 窗口
        assert arm.present_to_camera() is True
        assert arm.deal_to(zone) is True         # 认牌完成 → 放行去放牌
    assert S["runs"] == ["A1,A2,A3,B6,A1,A2,A3,B8,A1,A2,A3,B7,A1,A2,A3,B9"]
    import time as _t
    for _ in range(100):                         # 尾段自走收尾(途经模式下客户端不等它)
        if S["phase"] == "complete":
            break
        _t.sleep(0.05)
    assert S["phase"] == "complete"


def test_panthera_flop_macro_with_burn():
    """翻牌=烧牌+三张一条 run; 烧牌步不需要 Agent 等待。"""
    port, S = make_panthera_stub()
    hand_autodone(S)
    arm = make_panthera_arm(port)
    arm.on_need(need("burn", "MUCK", street="flop"))
    assert arm.pick_from_deck(present=False) is True   # 烧牌自走
    assert arm.deal_to("MUCK") is True
    for zone in ["C1", "C2", "C3"]:
        arm.on_need(need("board", zone, street="flop"))  # 宏观已在跑, 不重复提交
        assert arm.pick_from_deck() is True
        assert arm.deal_to(zone) is True
    assert S["runs"] == ["A1,A2,A4,A1,A2,A3,B1,A1,A2,A3,B2,A1,A2,A3,B3"]


def test_panthera_zone_change_aborts_macro():
    port, S = make_panthera_stub()
    hand_autodone(S)
    arm = make_panthera_arm(port, n_players=2)
    arm.on_need(need("hole", "P1a"))
    assert arm.pick_from_deck() is True
    assert arm.deal_to("P2b") is False           # 目标区与宏观计划不符 → 停臂降级


def test_panthera_dead_server_falls_back():
    from texas_agent.arm import PantheraArm
    dead = PantheraArm("http://127.0.0.1:1", points=POINTS,
                       http_timeout=0.3, poll_s=0.02, run_timeout=1)
    assert dead.health() is False
    dead.on_need(need("hole", "P1a"))            # 提交失败仅计数
    assert dead.pick_from_deck() is False
    dead.on_need(need("hole", "P1a"))
    assert not dead.alive                        # 连续失败 → 离线 → 整手人肉


def test_command_roundtrip(bus):
    sim = SimArm(bus, delay_s=0.05)
    arm = ArmClient(bus, timeout_s=2)
    assert arm.deal_to("P1a", face="down") is True
    assert sim.log[-1]["action"] == "deal_to" and sim.log[-1]["zone"] == "P1a"
    assert arm.alive


def test_timeout_marks_dead(bus):
    arm = ArmClient(bus, timeout_s=0.15)     # 没有臂在听
    assert arm.command("home") is False
    assert arm.command("home") is False
    assert not arm.alive                      # 连续失败 → 离线, 上层跳过臂


def test_sim_failure_ack(bus):
    SimArm(bus, delay_s=0.05, fail_actions=("pick_from_deck",))
    arm = ArmClient(bus, timeout_s=2)
    assert arm.pick_from_deck() is False
    assert arm.deal_to("C1", face="up") is True   # 其它动作不受影响


def test_full_hand_with_sim_arm(bus):
    """预排剧情牌 + 模拟臂整手: 每次发牌都由臂执行, 结算正确。"""
    SimArm(bus, delay_s=0.01)
    arm = ArmClient(bus, timeout_s=2)
    engine = Engine(bus, n_players=4, stacks=(20000,) * 4, blinds=(50, 100),
                    mode="preset")
    orch = Orchestrator(bus, engine, vision=MockVision(),
                        god=PresetGod.from_file("config/deck_order.txt"),
                        inputs=ScriptedInputs(list(SCRIPT)), ops=ScriptedOps(),
                        prompter=Prompter(bus), charts=GtoCharts("charts"),
                        arm=arm)
    snap = orch.run_hand()
    assert snap["complete"]
    deals = [m for m in bus.msgs if m["type"] == "arm_command"
             and m["action"] == "deal_to"]
    assert len(deals) == 16                   # 8 底牌 + 3 烧 + 5 公共
    assert {d["zone"] for d in deals} >= {"P1a", "P4b", "MUCK", "C1", "C5"}


def test_arm_failure_falls_back_to_human(bus, capsys):
    """臂全瘫: 每条指令失败 → alert + 人肉提词, 整手照样跑完。"""
    SimArm(bus, delay_s=0.01, fail_actions=("deal_to",))
    arm = ArmClient(bus, timeout_s=2)
    engine = Engine(bus, n_players=4, stacks=(20000,) * 4, blinds=(50, 100),
                    mode="preset")
    orch = Orchestrator(bus, engine, vision=MockVision(),
                        god=PresetGod.from_file("config/deck_order.txt"),
                        inputs=ScriptedInputs(list(SCRIPT)), ops=ScriptedOps(),
                        prompter=Prompter(bus), charts=GtoCharts("charts"),
                        arm=arm)
    snap = orch.run_hand()
    assert snap["complete"]                   # 臂瘫了牌局也不死
    assert any(m["type"] == "alert" and "机械臂" in m["text"] for m in bus.msgs)
