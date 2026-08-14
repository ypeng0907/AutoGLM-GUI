#!/usr/bin/env python3
"""生成 AutoGLM-GUI 应用图标.

设计：圆角方形 + 蓝紫渐变背景 + 白色手机轮廓（代表被自动操作的设备）
+ 手机内一个光标箭头与点击涟漪（代表 GUI 自动化操作）。

用法:
    uv run python scripts/generate_icon.py

输出:
    scripts/icon_source.png  (1024x1024 主源图)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).parent.parent
OUT_PATH = Path(__file__).parent / "icon_source.png"

SIZE = 1024
# 高分辨率超采样倍数，绘制后缩小以获得抗锯齿边缘
SS = 4


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return (
        int(round(_lerp(c1[0], c2[0], t))),
        int(round(_lerp(c1[1], c2[1], t))),
        int(round(_lerp(c1[2], c2[2], t))),
    )


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _diagonal_gradient(
    size: int,
    top_left: tuple[int, int, int],
    bottom_right: tuple[int, int, int],
) -> Image.Image:
    """生成对角线渐变背景（左上 -> 右下）。"""
    base = Image.new("RGB", (size, size))
    px = base.load()
    max_d = (size - 1) * 2
    for y in range(size):
        for x in range(size):
            t = (x + y) / max_d
            px[x, y] = _lerp_color(top_left, bottom_right, t)
    return base


def _draw_phone_and_cursor(draw: ImageDraw.ImageDraw, s: int) -> None:
    """在画布上绘制白色手机轮廓与自动化光标。s 为超采样后的画布尺寸。"""
    white = (255, 255, 255, 255)

    # 手机主体（圆角竖屏），居中偏上
    phone_w = int(s * 0.42)
    phone_h = int(s * 0.56)
    phone_x = (s - phone_w) // 2
    phone_y = int(s * 0.17)
    phone_r = int(phone_w * 0.22)
    stroke = max(2, int(s * 0.018))

    # 手机外框（描边）
    draw.rounded_rectangle(
        [phone_x, phone_y, phone_x + phone_w, phone_y + phone_h],
        radius=phone_r,
        outline=white,
        width=stroke,
    )

    # 顶部听筒小横条
    speaker_w = int(phone_w * 0.26)
    speaker_h = max(2, int(s * 0.012))
    sx = (s - speaker_w) // 2
    sy = phone_y + int(phone_h * 0.09)
    draw.rounded_rectangle(
        [sx, sy, sx + speaker_w, sy + speaker_h],
        radius=speaker_h // 2,
        fill=white,
    )

    # 点击涟漪（同心圆弧），位于屏幕中部
    cx = int(s * 0.44)
    cy = int(s * 0.52)
    for i, rr in enumerate((int(s * 0.11), int(s * 0.155))):
        arc_w = max(2, int(s * 0.014) - i * int(s * 0.003))
        draw.arc(
            [cx - rr, cy - rr, cx + rr, cy + rr],
            start=200,
            end=340,
            fill=white,
            width=arc_w,
        )

    # 光标箭头（自动化操作的手指/指针），指向涟漪中心
    # 用一个经典的箭头指针多边形
    ax = int(s * 0.47)
    ay = int(s * 0.47)
    scale = s * 0.16
    # 箭头（局部坐标，指向左上，尖端在原点），再平移到 (ax, ay)
    arrow = [
        (0.0, 0.0),
        (0.0, 0.78),
        (0.22, 0.58),
        (0.37, 0.92),
        (0.52, 0.85),
        (0.37, 0.52),
        (0.66, 0.5),
    ]
    pts = [(ax + px * scale, ay + py * scale) for (px, py) in arrow]
    draw.polygon(pts, fill=white)
    # 给箭头描一圈背景色边，制造与涟漪的层次（用略透明白描边即可，这里省略）


def generate() -> Path:
    canvas = SIZE * SS

    # 背景渐变（浅蓝 -> 深蓝）
    top_left = (56, 152, 255)   # 明亮天蓝
    bottom_right = (24, 82, 214)  # 深蓝
    grad = _diagonal_gradient(SIZE, top_left, bottom_right)
    grad = grad.resize((canvas, canvas), Image.Resampling.LANCZOS).convert("RGBA")

    # 前景层（手机 + 光标）
    fg = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    fdraw = ImageDraw.Draw(fg)
    _draw_phone_and_cursor(fdraw, canvas)

    composed = Image.alpha_composite(grad, fg)

    # 圆角遮罩
    radius = int(canvas * 0.235)  # macOS 风格圆角
    mask = _rounded_mask(canvas, radius)
    composed.putalpha(mask)

    # 缩小到目标尺寸（抗锯齿）
    final = composed.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    final.save(OUT_PATH, format="PNG")
    print(f"\u2713 Generated source icon: {OUT_PATH} ({SIZE}x{SIZE})")
    return OUT_PATH


if __name__ == "__main__":
    generate()