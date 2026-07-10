"""D1-3 · 模板采集: 交互逐张入库 52 张约 15 分钟。

把牌放到读牌位 → 画面对齐后按空格采集 → 自动裁角标存 rank/suit 模板。
每个点数只需采一次(任意花色), 花色模板从 4 张代表牌裁取。
按 q 退出, 按 r 重采当前张。

用法: python tools/capture_templates.py [--cam 1] [--out templates]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from texas_agent.vision.matcher import RANKS, CardMatcher  # noqa: E402

PLAN = [r + "s" for r in RANKS] + ["Ah", "Ad", "Ac"]  # 13 点数(黑桃) + 3 花色代表


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--out", default="templates")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    cap = cv2.VideoCapture(args.cam)
    matcher = CardMatcher(args.out)

    for card in PLAN:
        print(f"\n▶ 放置 {card}, 空格采集 / r 重来 / q 退出")
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            view = frame.copy()
            cv2.putText(view, f"place: {card}  [space]=capture", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            cv2.imshow("capture", view)
            k = cv2.waitKey(30) & 0xFF
            if k == ord("q"):
                return
            if k != ord(" "):
                continue
            aligned = matcher._align(frame)
            if aligned is None:
                print("  ✗ 未检出牌形, 调整位置/光照后重试")
                continue
            corner = matcher._corner(aligned)
            gray = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY)
            h = gray.shape[0]
            rank_img, suit_img = gray[: h // 2, :], gray[h // 2:, :]
            cv2.imwrite(str(out / f"rank_{card[0]}.png"), rank_img)
            cv2.imwrite(str(out / f"suit_{card[1]}.png"), suit_img)
            print(f"  ✓ 已存 rank_{card[0]}.png / suit_{card[1]}.png")
            break
    print("\n采集完成。用 洗乱重抽20张 验证: 识别 ≥19 且零错判(宁可不确定, 不可错)")


if __name__ == "__main__":
    main()
