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
from .orchestrator import (ConsoleOps, KeyboardInputs, NoneGod, Orchestrator,
                           PresetGod, ScriptedInputs)
from .prompter import Prompter
from .vision.live import LiveVision
from .vlm import DealAuditor, VlmCardReader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", help="预排牌序文件(上帝模式); 不给则 --public")
    ap.add_argument("--public", action="store_true", help="公开模式: 板面靠读牌+VLM 仲裁")
    ap.add_argument("--config", default="config/table.yaml")
    ap.add_argument("--session-tag", default="live")
    ap.add_argument("--ws", action="store_true", help="转发消息到 ws hub (前端/远端TTS)")
    ap.add_argument("--web-port", type=int, default=8080,
                    help="荷官网页屏端口(大字提词+俯视图), 0 关闭")
    args = ap.parse_args()
    if not args.deck and not args.public:
        sys.exit("选一个: --deck 预排牌序(主路) 或 --public 公开模式")

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    cams = cfg.get("cameras", {})

    bus = LocalBus(session_tag=args.session_tag, ws_forward=args.ws)
    bus.subscribe(render)

    print("▶ 打开顶视相机并标定 (桌面 ArUco 需全部可见, 且没有程序占用相机)...")
    vision = LiveVision(cfg.get("zones", "config/zones.yaml"),
                        top_cam_index=cams.get("top_index", 0),
                        card_cam_index=cams.get("card_index"),
                        template_dir=cams.get("template_dir", "templates"))
    try:
        vision.start()
    except RuntimeError as e:
        sys.exit(f"✗ {e}\n  排查: pkill -f cam_server 释放相机; 浏览器 warp 视图确认 marker 可见")
    print(f"✓ 标定完成; 读牌模板{'已' if vision.matcher.ready else '未'}采集"
          f"{'' if vision.matcher.ready else '(读牌走 VLM 仲裁→操作员)'}")

    webview = None
    if args.web_port:
        from .webview import WebView
        webview = WebView(bus, vision, port=args.web_port)
        print(f"✓ 荷官屏: http://<本机IP>:{args.web_port} (大字提词+俯视图, 可开朗读)")

    client = llm.from_config(args.config)
    vlm_reader = VlmCardReader(client, vision.card_image) if client else None
    if client:
        auditor = DealAuditor(bus, client, vision.still_frame)
        bus.subscribe(auditor.feed)
    if cfg.get("commentary", {}).get("enabled", True):
        commentator = Commentator(bus, llm_fn=client.ask_text if client else None,
                                  llm_timeout_s=3.5,
                                  tts=cfg.get("commentary", {}).get("tts", False))
        bus.subscribe(commentator.feed)
    print(f"✓ 慢循环 VLM: {'接入 ' + client.base_url if client else '未配置(全模板/操作员)'}")

    god = PresetGod.from_file(args.deck) if args.deck else NoneGod()
    stacks = cfg.get("stacks", [20000] * cfg.get("players", 4))
    engine = Engine(bus, n_players=cfg.get("players", 4), stacks=stacks,
                    blinds=tuple(cfg.get("blinds", [50, 100])),
                    mode="preset" if args.deck else "public")
    orch = Orchestrator(
        bus, engine, vision=vision, god=god, inputs=KeyboardInputs(),
        ops=ConsoleOps(),
        prompter=Prompter(bus, tts=cfg.get("prompter", {}).get("tts", False)),
        charts=GtoCharts(cfg.get("charts_dir", "charts")),
        expect_timeout=cfg.get("prompter", {}).get("expect_timeout_s", 20),
        vlm=vlm_reader)

    try:
        snap = orch.run_hand()
        print(f"\n✅ 一手完成。会话日志: {bus.session_path}")
        print(f"   终局筹码: {[x['stack'] for x in snap['seats']]}")
    except KeyboardInterrupt:
        print(f"\n⏹ 中断。会话日志: {bus.session_path}")
    finally:
        vision.close()
        if webview:
            webview.close()
        bus.close()


if __name__ == "__main__":
    main()
