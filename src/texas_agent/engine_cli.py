"""D1-1 验收工具: 命令行打完一手牌, jsonl 可回放。

用法:
  纯键盘(现场无预排):   python -m texas_agent.engine_cli
  预排牌序:             python -m texas_agent.engine_cli --deck config/deck_order.txt
  非交互演示(验收):     python -m texas_agent.engine_cli --deck config/deck_order.txt \
                            --script "c c c k / k k k k / k k k k / k k k k" --tts0

CLI 与现场版走同一个编排器循环 —— 只是适配器换成控制台。
"""

from __future__ import annotations

import argparse

from . import cards as C
from .bus import LocalBus
from .engine import Engine
from .gto import GtoCharts
from .orchestrator import (ConsoleOps, KeyboardInputs, MockVision, NoneGod,
                           Orchestrator, PresetGod, ScriptedInputs)
from .prompter import Prompter


class ManualGod:
    """无预排、无靴口相机: 每张牌由荷官口头报牌、键盘录入(D1-3 前的过渡形态)。"""

    def next_card(self) -> str:
        while True:
            raw = input("  ⌨️ 荷官报牌 (如 As, 烧牌直接回车): ").strip()
            if not raw:
                return C.UNKNOWN
            try:
                return C.normalize(raw)
            except ValueError:
                print("  ✗ 无效牌面")


def render(msg: dict) -> None:
    t = msg["type"]
    if t == "game_event":
        st = msg["state"]
        if msg["event"] in ("deal", "action", "hand_start", "settlement", "correction"):
            seats = "  ".join(
                f"P{x['seat'] + 1}{'(' + ','.join(x['hole']) + ')' if x['hole'] else ''}"
                f"[{x['stack']}{'F' if not x['in_hand'] else ''}]"
                for x in st["seats"])
            print(f"  ▸ {msg['event']:<10} 街[{st['street']}] 池[{st['pot']}] "
                  f"板[{' '.join(st['board']) or '-'}]  {seats}")
        if msg["event"] == "settlement":
            print(f"  💰 结算: {msg['detail']['payoffs']}")
    elif t == "analysis_update":
        eqs = "  ".join(f"P{p['seat'] + 1}:{p['equity']['win']:.0%}"
                        + (f"(反超{p['comeback']:.0%})" if p["comeback"] else "")
                        for p in msg["players"])
        print(f"  📊 胜率[{msg['mode']}] {eqs}   {msg['elapsed_ms']}ms")
    elif t == "gto_hint":
        s = " ".join(f"{k}:{v:.0%}" for k, v in msg["summary"].items())
        print(f"  ♟️ GTO基线 P{msg['seat'] + 1} {msg['node']} 手牌[{msg['hero']}] 范围汇总: {s}")
    elif t == "gto_deviation":
        print(f"  ♟️ 偏差 P{msg['seat'] + 1} {msg['hand']}: 基线{msg['gto']} 实际[{msg['action']}]")
    elif t == "commentary":
        print(f"  🎙️ {msg['text']}")
    elif t == "agent_trace":
        print(f"  · {msg['text']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", help="预排牌序文件 (god_source: preset)")
    ap.add_argument("--script", help="非交互动作脚本, 空格分隔: c/k/f/'r 300', '/'可作分隔注释")
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--stacks", default="20000")
    ap.add_argument("--blinds", default="50,100")
    ap.add_argument("--tts", action="store_true")
    ap.add_argument("--no-commentary", action="store_true")
    ap.add_argument("--session-tag", default="cli")
    args = ap.parse_args()

    bus = LocalBus(session_tag=args.session_tag)
    bus.subscribe(render)
    if not args.no_commentary:
        from . import llm
        from .commentator import Commentator
        client = llm.from_config()
        # 实测 Spark 上 32B 一句解说约 2.5~3s, 按手册放宽到 3.5s, 超时照旧降级模板
        commentator = Commentator(bus, llm_fn=client.ask_text if client else None,
                                  llm_timeout_s=3.5, debounce_s=0.0)
        bus.subscribe(commentator.feed)

    stacks = [int(x) for x in args.stacks.split(",")]
    if len(stacks) == 1:
        stacks *= args.players
    blinds = tuple(int(x) for x in args.blinds.split(","))

    if args.deck:
        god = PresetGod.from_file(args.deck)
        mode = "preset"
    elif args.script:
        raise SystemExit("--script 需要 --deck (非交互模式必须预排牌序)")
    else:
        god = ManualGod()
        mode = "god"

    inputs: object
    if args.script:
        toks = [t for t in args.script.split() if t != "/"]
        acts, i = [], 0
        while i < len(toks):
            if toks[i] == "r":
                acts.append(("raise", int(toks[i + 1])))
                i += 2
            else:
                acts.append(({"c": "call", "k": "check", "f": "fold"}[toks[i]], None))
                i += 1
        inputs = ScriptedInputs(acts)
    else:
        inputs = KeyboardInputs()

    engine = Engine(bus, n_players=args.players, stacks=stacks, blinds=blinds, mode=mode)
    orch = Orchestrator(bus, engine, vision=MockVision(), god=god, inputs=inputs,
                        ops=ConsoleOps(), prompter=Prompter(bus, tts=args.tts),
                        charts=GtoCharts())
    snap = orch.run_hand()
    print(f"\n✅ 一手完成。会话日志: {bus.session_path}")
    print(f"   终局筹码: {[x['stack'] for x in snap['seats']]}")
    bus.close()


if __name__ == "__main__":
    main()
