#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把状态栏电池图标渲染成一张预览图（开发/检查用，跑在电脑上即可）。

    python3 tests/render_preview.py [输出文件]

输出 tests/icon-preview.png：
  第一行 = 浅色面板（深色图标），第二行 = 深色面板（浅色图标），
  每格是 32px 图标放大 4 倍，方便肉眼确认"像不像电池、填充比例对不对、闪电有没有"。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from cutiepi_hud import tray  # noqa: E402

CASES = (
    (100, False, True, "100%"),
    (87, False, True, "87%"),
    (55, True, True, "55% 充电"),
    (20, False, True, "20% 低"),
    (8, True, True, "8% 充电"),
    (5, False, True, "5% 极低"),
    (-1, False, False, "无数据"),
)

LIGHT_PANEL = (240, 240, 240)
DARK_PANEL = (43, 45, 48)
LIGHT_ICON = (0.13, 0.14, 0.16)   # 浅色主题 -> 深图标
DARK_ICON = (0.93, 0.94, 0.95)    # 深色主题 -> 浅图标


def main():
    output = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "icon-preview.png")
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("需要 Pillow：pip install pillow")
        return 2

    size = 32
    zoom = 4
    padding = 10
    label_height = 16
    cell_w = size * zoom + padding * 2
    cell_h = size * zoom + padding * 2 + label_height

    rows = ((LIGHT_PANEL, LIGHT_ICON, "浅色面板"), (DARK_PANEL, DARK_ICON, "深色面板"))
    image = Image.new(
        "RGB",
        (cell_w * len(CASES) + 90, cell_h * len(rows)),
        (128, 128, 128),
    )
    draw = ImageDraw.Draw(image)

    for row, (panel, icon_color, row_label) in enumerate(rows):
        draw.text((6, row * cell_h + cell_h // 2), row_label, fill=(255, 255, 255))
        for index, (percent, charging, present, label) in enumerate(CASES):
            pixels = tray.render_icon(percent, charging, present, size, foreground=icon_color)
            icon = Image.new("RGBA", (size, size))
            icon.putdata([(r, g, b, a) for r, g, b, a in pixels])
            icon = icon.resize((size * zoom, size * zoom), Image.NEAREST)
            x = 90 + index * cell_w + padding
            y = row * cell_h + padding
            draw.rectangle([x, y, x + size * zoom - 1, y + size * zoom - 1], fill=panel)
            image.paste(icon, (x, y), icon)
            draw.text((x, y + size * zoom + 3), label, fill=(235, 235, 235))

    image.save(output)
    print("已生成 %s" % output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
