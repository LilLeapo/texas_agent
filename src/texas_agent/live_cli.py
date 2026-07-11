"""D1-4 · 现场版装配: 真相机 + 提词 + 引擎 + 慢循环 VLM 全链路跑真实的一手。

与 engine_cli 同一个编排器循环, 只是适配器换成现场件:
顶视相机核验落位、VLM 认牌仲裁/发牌审计、LLM 解说; 键盘申报行动。
任何慢循环组件失联自动降级, 不阻塞牌局。

用法(在 Spark 上, 先停掉占相机的 cam_server):
  预排剧情牌(主路):  python -m texas_agent.live_cli --deck config/deck_order.txt
  公开模式:          python -m texas_agent.live_cli --public
  转发 ws hub:       加 --ws (需先起 python -m texas_agent.bus)
"""

from __future__ import annotations

import argparse
import sys

import yaml

from . import llm
from .bus import LocalBus
from .commentator import Commentator
from .engine import Engine
from .engine_cli import parse_action_script, render
from .gto import GtoCharts
from .orchestrator import (ConsoleOps, HandRestart, KeyboardInputs, NoneGod,
                           Orchestrator, PresetGod, ScriptedInputs)
from .prompter import Prompter
from .vision.live import LiveVision
from .vlm import DealAuditor, VlmCardReader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", help="预排牌序文件(上帝模式-预排)")
    ap.add_argument("--public", action="store_true", help="公开模式: 板面靠读牌+VLM 仲裁")
    ap.add_argument("--shoe", action="store_true",
                    help="上帝模式-靴口: 每张牌先亮给读牌相机(VLM 认), 真实数据, 免预排")
    ap.add_argument("--robot", action="store_true",
                    help="机械臂执行(含靴口认牌); 失败自动人肉降级")
    ap.add_argument("--arm-url", help="臂控电脑 server, 如 http://<Windows_IP>:5100")
    ap.add_argument("--arm-token", default="", help="Panthera 服务器 api_token")
    ap.add_argument("--arm-style", choices=("batch", "points"), default="batch",
                    help="batch=程序制(OPENING/C1..C5, 现行文档); points=点位序列制(旧)")
    ap.add_argument("--arm-points", default="config/arm_points.yaml",
                    help="points 模式的语义点位映射")
    ap.add_argument("--sim-arm", action="store_true",
                    help="挂模拟机械臂(联调用, 收指令延时回执)")
    ap.add_argument("--config", default="config/table.yaml")
    ap.add_argument("--session-tag", default="live")
    ap.add_argument("--ws", action="store_true", help="转发消息到 ws hub (前端/远端TTS)")
    ap.add_argument("--web-port", type=int, default=8080,
                    help="荷官网页屏端口(大字提词+俯视图), 0 关闭")
    ap.add_argument("--broadcast-port", type=int, default=8081,
                    help="转播大屏端口(观众席, 现仍是内置模拟剧情), 0 关闭")
    ap.add_argument("--script", help="动作脚本(同 engine_cli), 免键盘: 'c c c k / k k k k'")
    args = ap.parse_args()
    if args.robot:
        args.shoe = True   # 机械臂流程内含靴口认牌
    if not args.deck and not args.public and not args.shoe:
        sys.exit("选一个: --deck 预排(剧情牌) / --shoe 靴口读牌(真实数据) / --public 公开模式")

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    cams = cfg.get("cameras", {})

    bus = LocalBus(session_tag=args.session_tag, ws_forward=args.ws)
    bus.subscribe(render)

    from .vision.yolo_reader import YoloCardReader
    yolo = YoloCardReader.if_available()
    print(f"▶ 打开顶视相机并标定... (YOLO 读牌: {'✓' if yolo else '未装载'})")
    vision = LiveVision(cfg.get("zones", "config/zones.yaml"),
                        top_cam_index=cams.get("top_index", 0),
                        card_cam_index=cams.get("card_index"),
                        template_dir=cams.get("template_dir", "templates"),
                        yolo=yolo)
    try:
        vision.start()
    except RuntimeError as e:
        sys.exit(f"✗ {e}\n  排查: pkill -f cam_server 释放相机; 浏览器 warp 视图确认 marker 可见")
    print(f"✓ 标定完成; 读牌模板{'已' if vision.matcher.ready else '未'}采集"
          f"{'' if vision.matcher.ready else '(读牌走 VLM 仲裁→操作员)'}")

    webview = None
    ops: object = ConsoleOps()
    if args.web_port:
        from .webview import WebOps, WebView
        webview = WebView(bus, vision, port=args.web_port)
        ops = WebOps(webview)   # 确认/补录也走网页
        print(f"✓ 荷官屏: http://<本机IP>:{args.web_port} "
              f"(提词+俯视图+操作员按钮, 可开朗读)")

    broadcast = None
    if args.broadcast_port:
        from .broadcast_view import BroadcastView
        broadcast = BroadcastView(port=args.broadcast_port)
        print(f"✓ 转播大屏: http://<本机IP>:{args.broadcast_port} "
              f"(观众席, 数据未接, 播的是内置模拟剧情)")

    client = llm.from_config(args.config)
    vlm_reader = VlmCardReader(client, vision.card_image) if client else None
    if client:
        # 审计双层: 顶视 YOLO 逐格核对公共牌(毫秒) → VLM 整帧对答案
        board_reader = ((lambda z: yolo.read_image(vision.zone_image(z)))
                        if yolo else None)
        auditor = DealAuditor(bus, client, vision.still_frame,
                              board_reader=board_reader)
        bus.subscribe(auditor.feed)
    if cfg.get("commentary", {}).get("enabled", True):
        commentator = Commentator(bus, llm_fn=client.ask_text if client else None,
                                  llm_timeout_s=3.5,
                                  tts=cfg.get("commentary", {}).get("tts", False))
        bus.subscribe(commentator.feed)
    print(f"✓ 慢循环 VLM: {'接入 ' + client.base_url if client else '未配置(全模板/操作员)'}")

    prompter = Prompter(bus, tts=cfg.get("prompter", {}).get("tts", False))
    arm = None
    if args.robot:
        if args.arm_url and args.arm_style == "batch":
            from .arm import PantheraBatchArm
            arm = PantheraBatchArm(args.arm_url, token=args.arm_token, bus=bus)
            print(f"✓ 臂程序制服务器: {args.arm_url} "
                  f"(探活: {'✓ 在线' if arm.health() else '✗ 不通, 将走人肉降级'}; "
                  f"OPENING/C1..C5, 无实体烧牌)")
        elif args.arm_url:
            from .arm import PantheraArm
            try:
                points = yaml.safe_load(open(args.arm_points, encoding="utf-8")) or {}
            except FileNotFoundError:
                sys.exit(f"✗ 缺 {args.arm_points}: 按 config/arm_points.example.yaml"
                         " 填写点位映射(语义名 → A1/B3 引用)")
            arm = PantheraArm(args.arm_url, token=args.arm_token,
                              points=points.get("points", points),
                              n_players=cfg.get("players", 4), bus=bus)
            print(f"✓ Panthera 点位序列服务器: {args.arm_url} "
                  f"(探活: {'✓ 在线' if arm.health() else '✗ 不通, 将走人肉降级'})")
        elif args.sim_arm:
            from .arm import ArmClient, SimArm
            arm = ArmClient(bus)
            SimArm(bus)
            print("✓ 模拟机械臂已挂载 (真臂用 --arm-url http://<ARM_PC_IP>:8090)")
        else:
            sys.exit("✗ --robot 需要 --arm-url <臂控电脑server> 或 --sim-arm(联调)")
        print("✓ 机械臂模式: 取牌/亮牌/发牌由臂执行, 失败自动落回人肉提词")
    if args.shoe:
        if vision.card_cam is None:
            sys.exit("✗ --shoe 需要读牌相机: 在 config/table.yaml cameras.card_index 填相机号")
        if client is None:
            sys.exit("✗ --shoe 需要 VLM 认牌: 配置 llm.base_url")
        from .vision.shoe import ShoeGod
        # 批次程序臂 → 流式旁路: 后台线程独占读牌相机, 飞行窗投票按序入队
        stream = arm is not None and getattr(arm, "skip_burn", False)
        hole_map = None
        phys = (cfg.get("arm") or {}).get("hole_deal_order") or []
        if stream and phys:
            n = cfg.get("players", 4)
            ez = [f"P{i + 1}a" for i in range(n)] + [f"P{i + 1}b" for i in range(n)]
            if sorted(phys) == sorted(ez):
                hole_map = [phys.index(z) for z in ez]
                print(f"✓ 底牌臂序重映射: 臂 {'→'.join(phys)} → 引擎 {'/'.join(ez)}")
            else:
                print(f"⚠ arm.hole_deal_order 与 {n} 人桌不匹配, 不重映射(按引擎序直录)")
        board_scan = None
        if yolo is not None:
            def board_scan():
                ok, f = vision.top.read()   # 仅主线程(next_card 内)调用, 读相机安全
                if ok:
                    vision.last_frame = f
                return yolo.detect_all(vision.board_band())
        god = ShoeGod(bus, prompter, ops, client, card_cam=vision.card_cam,
                      fast_reader=yolo, arm=arm, stream=stream, hole_map=hole_map,
                      board_scan=board_scan,
                      phys_order=phys if hole_map else None)
        mode = "god"
        if stream and god.stream is not None:
            print("✓ 读牌旁路: 后台常驻线程(飞行窗投票), 主流程阻塞也不漏过牌")
            if webview is not None:
                webview.shoe = god.stream
                print(f"✓ 牌靴实况: http://<本机IP>:{args.web_port}/shoe "
                      f"(过牌识别调试用)")
        print(f"✓ 靴口模式: 亮牌认牌链 = "
              f"{'YOLO(毫秒)→' if yolo else ''}VLM→网页补录, 真实数据驱动")
    elif args.deck:
        god, mode = PresetGod.from_file(args.deck), "preset"
    else:
        god, mode = NoneGod(), "public"
    stacks = cfg.get("stacks", [20000] * cfg.get("players", 4))
    engine = Engine(bus, n_players=cfg.get("players", 4), stacks=stacks,
                    blinds=tuple(cfg.get("blinds", [50, 100])), mode=mode)
    if args.script:
        inputs = ScriptedInputs(parse_action_script(args.script))
    elif webview is not None:
        from .webview import WebInputs
        inputs = WebInputs(webview)   # 网页申报动作(轮次衔接入口)
        print("✓ 行动申报: 网页按钮 (轮到谁, 荷官屏弹谁的合法动作)")
    else:
        inputs = KeyboardInputs()
    orch = Orchestrator(
        bus, engine, vision=vision, god=god, inputs=inputs,
        ops=ops,
        prompter=prompter,
        charts=GtoCharts(cfg.get("charts_dir", "charts")),
        expect_timeout=cfg.get("prompter", {}).get("expect_timeout_s", 20),
        vlm=vlm_reader, arm=arm)

    code = 0
    try:
        snap = orch.run_hand()
        print(f"\n✅ 一手完成。会话日志: {bus.session_path}")
        print(f"   终局筹码: {[x['stack'] for x in snap['seats']]}")
    except HandRestart:
        print("\n🔁 网页请求重开本手, 进程干净退出交外层重启")
        code = 9   # tmux 外层 while 循环识别此码后重启
    except KeyboardInterrupt:
        print(f"\n⏹ 中断。会话日志: {bus.session_path}")
    finally:
        if getattr(god, "stream", None) is not None:   # 先停读牌线程再放相机
            god.stream.stop()
            god.stream.join(timeout=2)
        vision.close()
        if webview:
            webview.close()
        if broadcast:
            broadcast.close()
        bus.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
