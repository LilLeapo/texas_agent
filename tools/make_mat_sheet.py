"""D0/D1-2 · 生成现场布置打印件: ArUco 贴纸 (A4, 100% 缩放打印) + 桌垫布局图。

- print/aruco_a4.png: 8 个 marker 按 zones.yaml 尺寸排版, 附 100mm 校尺
  (打印后用尺量校尺, 不是 100mm 就是打印机缩放了, 标定会歪)。
- print/mat_layout.png: 桌垫 mm 坐标布局图, 照着用卷尺在桌布上贴 marker/画区域。

用法: python tools/make_mat_sheet.py [--zones config/zones.yaml] [--out print]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

DPI = 300
PX_MM = DPI / 25.4  # 11.81 px/mm
A4 = (int(210 * PX_MM), int(297 * PX_MM))  # w, h


def mm(v: float) -> int:
    return int(round(v * PX_MM))


def make_aruco_sheet(cfg: dict, path: Path) -> None:
    aru = cfg["aruco"]
    size_mm = aru.get("marker_size_mm", 40)
    dict_name = aru.get("dictionary", "DICT_4X4_50")
    adict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dict_name))
    ids = sorted(int(k) for k in aru["markers"])

    canvas = np.full((A4[1], A4[0]), 255, np.uint8)
    cv2.putText(canvas, f"ArUco {dict_name}  {size_mm}mm  PRINT AT 100% SCALE",
                (mm(15), mm(12)), cv2.FONT_HERSHEY_SIMPLEX, 1.6, 0, 3)
    # 100mm 校尺
    y = mm(20)
    cv2.line(canvas, (mm(15), y), (mm(115), y), 0, 3)
    for t in range(0, 101, 10):
        cv2.line(canvas, (mm(15 + t), y - mm(2)), (mm(15 + t), y + mm(2)), 0, 2)
    cv2.putText(canvas, "this bar must measure exactly 100 mm",
                (mm(120), y + mm(2)), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)

    cols, cell_w, cell_h, top = 2, mm(95), mm(62), mm(30)
    side = mm(size_mm)
    for i, mid in enumerate(ids):
        r, c = divmod(i, cols)
        cx, cy = mm(12) + c * cell_w, top + r * cell_h
        img = cv2.aruco.generateImageMarker(adict, mid, side)
        canvas[cy:cy + side, cx:cx + side] = img
        cv2.putText(canvas, f"ID {mid}", (cx + side + mm(4), cy + side // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
        # 裁切参考线(marker 四周留 >=8mm 白边, 别切到黑边)
        cv2.rectangle(canvas, (cx - mm(8), cy - mm(8)),
                      (cx + side + mm(8), cy + side + mm(8)), 180, 1)
    cv2.imwrite(str(path), canvas)
    print(f"✓ {path}  (A4 @300DPI, 打印选 100%/实际大小, 勿选适应页面)")


def make_layout(cfg: dict, path: Path) -> None:
    w_mm, h_mm = cfg["mat_size_mm"]
    s = 1.5  # px per mm
    pad = 90
    canvas = np.full((int(h_mm * s) + pad * 2, int(w_mm * s) + pad * 2, 3), 255, np.uint8)

    def pt(x, y):
        return int(x * s) + pad, int(y * s) + pad

    cv2.rectangle(canvas, pt(0, 0), pt(w_mm, h_mm), (0, 0, 0), 2)
    cv2.putText(canvas, f"mat {w_mm} x {h_mm} mm  (origin: top-left, x right, y down)",
                (pad, pad - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    for mid, quad in cfg["aruco"]["markers"].items():
        (x0, y0), (x1, _), (_, y2) = quad[0], quad[1], quad[2]
        cv2.rectangle(canvas, pt(x0, y0), pt(x1, y2), (40, 40, 40), -1)
        cv2.putText(canvas, str(mid), (pt(x0, y0)[0] + 8, pt(x0, y2)[1] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        cv2.putText(canvas, f"({x0},{y0})", (pt(x0, y0)[0] - 10, pt(x0, y0)[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (120, 120, 120), 1)
    for name, (x, y, zw, zh) in cfg["zones"].items():
        color = (200, 80, 0) if name.startswith("C") else (0, 120, 0)
        if name in ("MUCK", "DECK"):
            color = (0, 0, 200)
        cv2.rectangle(canvas, pt(x, y), pt(x + zw, y + zh), color, 2)
        cv2.putText(canvas, name, (pt(x, y)[0] + 4, pt(x, y)[1] + 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(canvas, f"({x},{y})", (pt(x, y)[0] + 4, pt(x, y + zh)[1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1)
    cv2.imwrite(str(path), canvas)
    print(f"✓ {path}  (布局参考图: 按 mm 坐标用卷尺定位贴 marker)")


def make_pdf(out: Path) -> None:
    """两页 PDF: 第 1 页 ArUco 贴纸(严格 A4/300DPI 尺寸), 第 2 页布局参考图。"""
    try:
        from PIL import Image
    except ImportError:
        print("⚠ 未装 pillow, 跳过 PDF (pip install pillow)")
        return
    sheet = Image.open(out / "aruco_a4.png").convert("RGB")
    layout = Image.open(out / "mat_layout.png").convert("RGB")
    # 布局图铺到 A4 横页画布(同 300DPI), 保证 PDF 两页页幅一致、都是 A4
    page = Image.new("RGB", (A4[1], A4[0]), "white")
    k = min((A4[1] - 200) / layout.width, (A4[0] - 200) / layout.height)
    scaled = layout.resize((int(layout.width * k), int(layout.height * k)))
    page.paste(scaled, ((page.width - scaled.width) // 2,
                        (page.height - scaled.height) // 2))
    sheet.save(out / "print_pack.pdf", "PDF", resolution=DPI,
               save_all=True, append_images=[page])
    print(f"✓ {out / 'print_pack.pdf'}  (第1页按实际大小打印, 第2页仅参考)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zones", default="config/zones.yaml")
    ap.add_argument("--out", default="print")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.zones, encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    make_aruco_sheet(cfg, out / "aruco_a4.png")
    make_layout(cfg, out / "mat_layout.png")
    make_pdf(out)


if __name__ == "__main__":
    sys.exit(main())
