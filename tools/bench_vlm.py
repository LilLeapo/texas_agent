"""VLM 认牌基准测试: 一批牌面裁图 → 准确率/错判率/P50/P95 延迟。

用数据决定 VLM 当兜底(默认)还是可更激进。验收线: 错判必须为 0 (宁可不确定, 不可错),
单张往返 < 4s。

图片文件名即真值: 'Qh.jpg' / 'Qh_2.png' / 'as_dark.jpg' 均可(下划线前为牌面)。
模板采集前可先用手机拍的扑克照片凑。

用法: python tools/bench_vlm.py --dir photos/ [--url http://10.29.3.94:11434/v1]
                                [--model qwen2.5vl:32b] [--timeout 30] [--repeat 1]
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.cards import normalize          # noqa: E402
from texas_agent.vlm import CARD_PROMPT, parse_card_reply  # noqa: E402


def truth_from_name(path: Path) -> str | None:
    token = path.stem.split("_")[0].split("-")[0]
    try:
        return normalize(token)
    except ValueError:
        return None


def ask_vlm(url: str, model: str, image_path: Path, timeout: float) -> tuple[str, float]:
    """单张图 → (回复文本, 往返秒)。异常向上抛, 由调用方计失败。"""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CARD_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    dt = time.perf_counter() - t0
    return data["choices"][0]["message"]["content"], dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="牌面裁图目录, 文件名为真值")
    ap.add_argument("--url", required=True, help="如 http://<SPARK_IP>:11434/v1")
    ap.add_argument("--model", default="qwen2.5vl:32b")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--repeat", type=int, default=1, help="每张图重复次数(测延迟稳定性)")
    args = ap.parse_args()

    images = sorted(p for p in Path(args.dir).iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    cases = [(p, truth_from_name(p)) for p in images]
    skipped = [p.name for p, t in cases if t is None]
    cases = [(p, t) for p, t in cases if t is not None]
    if skipped:
        print(f"⚠ 文件名解析不出牌面, 跳过 {len(skipped)} 张: {', '.join(skipped)}")
    if not cases:
        sys.exit("目录里没有可用图片(文件名需形如 Qh.jpg)")

    correct, uncertain, wrong, failed = 0, 0, 0, 0
    latencies: list[float] = []
    for path, truth in cases:
        for _ in range(args.repeat):
            try:
                reply, dt = ask_vlm(args.url, args.model, path, args.timeout)
            except (urllib.error.URLError, TimeoutError, OSError, KeyError) as e:
                failed += 1
                print(f"  ✗ {path.name}: 请求失败 {e}")
                continue
            latencies.append(dt)
            got = parse_card_reply(reply)
            if got == truth:
                correct += 1
                mark = "✓"
            elif got is None:
                uncertain += 1
                mark = "?"
            else:
                wrong += 1
                mark = "✗✗ 错判"
            print(f"  {mark} {path.name}: 真值 {truth}, 回复 {got or '不确定'} "
                  f"({dt:.2f}s, 原文 {reply.strip()[:40]!r})")

    total = correct + uncertain + wrong
    if total == 0:
        sys.exit("全部请求失败, 检查端点/模型名")
    print(f"\n===== {args.model} @ {args.url} =====")
    print(f"样本 {total}: 正确 {correct} ({correct / total:.0%}), "
          f"不确定 {uncertain} ({uncertain / total:.0%}), "
          f"错判 {wrong} ({wrong / total:.0%}){', 请求失败 ' + str(failed) if failed else ''}")
    lat = sorted(latencies)
    if lat:
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        print(f"延迟: P50 {statistics.median(lat):.2f}s, P95 {p95:.2f}s, "
              f"最慢 {lat[-1]:.2f}s")
    print("验收线: 错判 = 0 且 P95 < 4s → 可当认牌仲裁员; "
          "错判 > 0 → 提高不确定门槛或换模型")
    if wrong:
        sys.exit(1)


if __name__ == "__main__":
    main()
