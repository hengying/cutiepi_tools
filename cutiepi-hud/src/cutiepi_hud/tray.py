"""把电池状态显示到系统状态栏（任务栏托盘）里。

为什么不画独立窗口：Wayland 下窗口无法自己定位，而且用户要求“就在状态栏里”。

两条实现路径，自动选择：

1. **StatusNotifierItem（SNI，KDE 托盘协议）** —— 树莓派 OS 新的任务栏
   `wf-panel-pi` 内置了 `org.kde.StatusNotifierWatcher` + host（源码
   src/widgets/tray/），它会用我们的 `IconPixmap` 画一个图标、把 `ToolTip`
   显示成悬停提示。waybar 之类也支持。
2. **XEmbed 托盘（GtkStatusIcon）** —— X11 下的 LXDE/lxpanel、xfce4-panel 等
   老式托盘用这个。是否真的被托盘接管可以用 `is_embedded()` 判断。

图标是用纯 Python 逐像素画出来的（4 倍超采样 + 盒式降采样做抗锯齿），
不依赖 cairo/Pillow，因此在任何树莓派 OS 上都能跑。
"""

import os
import threading
import time

# ---------------------------------------------------------------------------
# 图标绘制（纯 Python，可单测）
# ---------------------------------------------------------------------------
SUPERSAMPLE = 4

# 默认前景色：拿不到主题色时用它（中间灰，浅色/深色面板上都还能看清）
DEFAULT_FOREGROUND = (0.62, 0.64, 0.67)

# 手工指定颜色时用这两个（和系统图标观感接近）
FOREGROUND_FOR_LIGHT_PANEL = (0.10, 0.11, 0.13)   # 浅色面板 → 深色图标
FOREGROUND_FOR_DARK_PANEL = (0.93, 0.94, 0.95)    # 深色面板 → 浅色图标

# 状态色（和面板主题无关，浅色/深色背景都够醒目）
COLOR_CHARGING = (0.13, 0.72, 0.35)
COLOR_LOW = (0.90, 0.55, 0.05)
COLOR_CRITICAL = (0.85, 0.18, 0.16)
COLOR_BOLT = (0.05, 0.07, 0.09)


def _inside_round_rect(px, py, x0, y0, x1, y1, radius):
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    if radius <= 0:
        return True
    cx = min(max(px, x0 + radius), x1 - radius)
    cy = min(max(py, y0 + radius), y1 - radius)
    dx = px - cx
    dy = py - cy
    return dx * dx + dy * dy <= radius * radius


def _inside_polygon(px, py, points):
    inside = False
    count = len(points)
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        if (y0 > py) != (y1 > py):
            x_cross = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
            if px < x_cross:
                inside = not inside
    return inside


def _blend(pixels, width, px, py, color, alpha=1.0):
    if px < 0 or py < 0 or px >= width:
        return
    index = (py * width + px) * 4
    sr, sg, sb = color
    dr, dg, db, da = pixels[index : index + 4]
    out_alpha = alpha + da * (1.0 - alpha)
    if out_alpha <= 0.0:
        pixels[index : index + 4] = [0.0, 0.0, 0.0, 0.0]
        return
    pixels[index] = (sr * alpha + dr * da * (1.0 - alpha)) / out_alpha
    pixels[index + 1] = (sg * alpha + dg * da * (1.0 - alpha)) / out_alpha
    pixels[index + 2] = (sb * alpha + db * da * (1.0 - alpha)) / out_alpha
    pixels[index + 3] = out_alpha


def _fill_shape(pixels, width, height, predicate, color, alpha=1.0):
    for py in range(height):
        for px in range(width):
            if predicate(px + 0.5, py + 0.5):
                _blend(pixels, width, px, py, color, alpha)


def fill_color(percent, charging, present=True, foreground=None):
    """按电量/充电状态选择填充色。

    * 充电中 → 绿色（带闪电）
    * ≤10%   → 红色；≤20% → 橙色
    * 其它   → 跟随面板前景色（看起来就像系统自带图标）
    """
    fg = foreground or DEFAULT_FOREGROUND
    if not present or percent is None or percent < 0:
        return None  # 不填充
    if charging:
        return COLOR_CHARGING
    if percent <= 10:
        return COLOR_CRITICAL
    if percent <= 20:
        return COLOR_LOW
    return fg


def render_icon(percent, charging, present=True, size=32, foreground=None):
    """返回 size x size 的像素列表，每项 (r, g, b, a)，取值 0~255。

    画的是一个电池：外框 + 正极触点 + 按电量填充的内部 + 充电时的闪电。
    foreground 是面板的前景色（0~1 的 RGB），用来画外框和正常电量的填充，
    这样浅色主题上是深色图标、深色主题上是浅色图标。
    """
    fg = foreground or DEFAULT_FOREGROUND
    size = int(size)
    scale = SUPERSAMPLE
    big = size * scale
    pixels = [0.0] * (big * big * 4)

    body_x0 = 0.06 * big
    body_y0 = 0.26 * big
    body_x1 = 0.84 * big
    body_y1 = 0.74 * big
    radius = 0.09 * big
    line = 0.075 * big
    nub_x0 = 0.855 * big
    nub_y0 = 0.40 * big
    nub_x1 = 0.945 * big
    nub_y1 = 0.60 * big

    outline_alpha = 0.95 if present else 0.45

    # 正极触点
    _fill_shape(
        pixels,
        big,
        big,
        lambda px, py: _inside_round_rect(px, py, nub_x0, nub_y0, nub_x1, nub_y1, 0.02 * big),
        fg,
        outline_alpha,
    )

    # 外壳（用“外框减内框”的方式画描边）
    def _outer(px, py):
        return _inside_round_rect(px, py, body_x0, body_y0, body_x1, body_y1, radius)

    def _inner(px, py):
        return _inside_round_rect(
            px, py,
            body_x0 + line, body_y0 + line,
            body_x1 - line, body_y1 - line,
            max(radius - line, 0.0),
        )

    _fill_shape(pixels, big, big, lambda px, py: _outer(px, py) and not _inner(px, py),
                fg, outline_alpha)

    # 电量填充
    color = fill_color(percent, charging, present, fg)
    if color is not None:
        ratio = max(0.0, min(1.0, percent / 100.0))
        inner_x0 = body_x0 + line * 1.6
        inner_x1 = body_x1 - line * 1.6
        inner_y0 = body_y0 + line * 1.6
        inner_y1 = body_y1 - line * 1.6
        fill_x1 = inner_x0 + (inner_x1 - inner_x0) * ratio
        if fill_x1 > inner_x0 + 0.01 * big:
            # 正常电量用前景色画填充，但比外框淡一点，这样"填了多少"更容易看出来
            alpha = 0.72 if color == fg else 0.95
            _fill_shape(
                pixels,
                big,
                big,
                lambda px, py: (inner_x0 <= px <= fill_x1 and inner_y0 <= py <= inner_y1),
                color,
                alpha,
            )

    # 充电闪电（画在填充之上，用深色“挖”出来）
    if charging:
        cx = (body_x0 + body_x1) / 2.0
        cy = (body_y0 + body_y1) / 2.0
        s = (body_y1 - body_y0)
        bolt = [
            (cx + 0.06 * s, cy - 0.46 * s),
            (cx - 0.30 * s, cy + 0.04 * s),
            (cx - 0.04 * s, cy + 0.04 * s),
            (cx - 0.12 * s, cy + 0.46 * s),
            (cx + 0.30 * s, cy - 0.04 * s),
            (cx + 0.04 * s, cy - 0.04 * s),
        ]
        _fill_shape(pixels, big, big, lambda px, py: _inside_polygon(px, py, bolt), COLOR_BOLT, 0.95)

    # 盒式降采样（同时做 alpha 预乘，避免边缘发灰）
    result = []
    inv = 1.0 / (scale * scale)
    for y in range(size):
        for x in range(size):
            sum_a = 0.0
            sum_r = 0.0
            sum_g = 0.0
            sum_b = 0.0
            for sy in range(scale):
                for sx in range(scale):
                    index = ((y * scale + sy) * big + (x * scale + sx)) * 4
                    alpha = pixels[index + 3]
                    sum_a += alpha
                    sum_r += pixels[index] * alpha
                    sum_g += pixels[index + 1] * alpha
                    sum_b += pixels[index + 2] * alpha
            if sum_a <= 0.0:
                result.append((0, 0, 0, 0))
                continue
            result.append(
                (
                    int(round(max(0.0, min(1.0, sum_r / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_g / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_b / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_a * inv)) * 255)),
                )
            )
    return result


def to_argb_bytes(pixels):
    """SNI 的 IconPixmap 要求网络字节序的 ARGB32（A 在最前面）。"""
    buffer = bytearray(len(pixels) * 4)
    index = 0
    for red, green, blue, alpha in pixels:
        buffer[index] = alpha
        buffer[index + 1] = red
        buffer[index + 2] = green
        buffer[index + 3] = blue
        index += 4
    return bytes(buffer)


def to_rgba_bytes(pixels):
    """GdkPixbuf 要的 RGBA 顺序。"""
    buffer = bytearray(len(pixels) * 4)
    index = 0
    for red, green, blue, alpha in pixels:
        buffer[index] = red
        buffer[index + 1] = green
        buffer[index + 2] = blue
        buffer[index + 3] = alpha
        index += 4
    return bytes(buffer)


def build_sun_tooltip(level, max_level, available=True):
    """亮度图标的 (标题, 说明)。"""
    if not available or level is None:
        return ("亮度", "读不到背光设备")
    return ("亮度 %d/%d" % (int(level), int(max_level)), "点一下调整亮度")


def render_sun_icon(level, max_level=15, size=32, foreground=None):
    """返回 size x size 像素，画一个太阳：圆盘 + 8 条光芒，亮度越高越大。"""
    import math

    fg = foreground or DEFAULT_FOREGROUND
    size = int(size)
    scale = SUPERSAMPLE
    big = size * scale
    pixels = [0.0] * (big * big * 4)

    try:
        frac = max(0.0, min(1.0, float(level) / float(max(1, max_level))))
    except (TypeError, ValueError, ZeroDivisionError):
        frac = 0.0

    cx = cy = (big - 1) / 2.0
    core = big * (0.14 + 0.07 * frac)
    inner = core * 1.35
    outer = big * (0.28 + 0.19 * frac)
    ray_half = max(1.0, big * 0.045)
    angles = [index * math.pi / 4.0 for index in range(8)]

    for py in range(big):
        for px in range(big):
            dx = px + 0.5 - cx
            dy = py + 0.5 - cy
            distance = math.hypot(dx, dy)
            if distance <= core:
                _blend(pixels, big, px, py, fg, 1.0)
                continue
            if distance <= outer and distance >= inner:
                theta = math.atan2(dy, dx)
                for angle in angles:
                    diff = abs((theta - angle + math.pi) % (2.0 * math.pi) - math.pi)
                    if diff * distance <= ray_half:
                        _blend(pixels, big, px, py, fg, 1.0)
                        break

    result = []
    inv = 1.0 / (scale * scale)
    for y in range(size):
        for x in range(size):
            sum_a = 0.0
            sum_r = 0.0
            sum_g = 0.0
            sum_b = 0.0
            for sy in range(scale):
                for sx in range(scale):
                    index = ((y * scale + sy) * big + (x * scale + sx)) * 4
                    alpha = pixels[index + 3]
                    sum_a += alpha
                    sum_r += pixels[index] * alpha
                    sum_g += pixels[index + 1] * alpha
                    sum_b += pixels[index + 2] * alpha
            if sum_a <= 0.0:
                result.append((0, 0, 0, 0))
                continue
            result.append(
                (
                    int(round(max(0.0, min(1.0, sum_r / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_g / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_b / sum_a)) * 255)),
                    int(round(max(0.0, min(1.0, sum_a * inv)) * 255)),
                )
            )
    return result


def build_tooltip(percent, charging, present=True):
    """返回 (标题, 说明) 两段文本。"""
    if not present or percent is None or percent < 0:
        return ("电池", "读不到电池数据")
    title = "电池 %d%%" % int(percent)
    if charging:
        return (title, "充电中")
    return (title, "使用电池")


# ---------------------------------------------------------------------------
# 数字图标（5x7 点阵字体，画进托盘图标里）
# ---------------------------------------------------------------------------
# 每个字符 7 行、每行 5 位；'1' 表示该像素点亮
FONT_5X7 = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}

GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7


def _pick_font(text):
    """目前统一用 5x7 点阵（窄体 3x7 在 24px 面板上太糊，实测放弃）。"""
    return FONT_5X7, GLYPH_WIDTH


def _extent(text, glyph_width, kerning=0):
    count = len(text)
    if count == 0:
        return 0, GLYPH_HEIGHT
    return count * glyph_width + (count - 1) * kerning, GLYPH_HEIGHT


def _text_extent(text, kerning=1):
    """返回 (width, height)，单位=字体像素。"""
    count = len(text)
    if count == 0:
        return 0, GLYPH_HEIGHT
    width = count * GLYPH_WIDTH + (count - 1) * kerning
    return width, GLYPH_HEIGHT


def _text_scale(text, size, margin=2, kerning=1):
    """挑一个让文字尽量大、又能塞进 size x size 的整数放大倍数。"""
    width, height = _text_extent(text, kerning)
    if width <= 0 or height <= 0:
        return 1
    usable = max(1, size - margin * 2)
    return max(1, min(usable // width, usable // height))


def render_number_icon(percent, charging, present=True, size=32, foreground=None):
    """把百分比直接画成数字（比电池图形更容易一眼读出具体数值）。"""
    fg = foreground or DEFAULT_FOREGROUND
    size = int(size)

    if not present or percent is None or percent < 0:
        text = "--"
        color = (0.45, 0.47, 0.50)
    else:
        percent = max(0, min(100, int(percent)))
        text = str(percent)
        color = fill_color(percent, charging, present, fg)

    # 按位数挑字体和放大倍数，尽量把数字画大：
    #   1 位 5x7 @4 倍（28px 高）、2 位 5x7 @3 倍（21px 高）、
    #   3 位 3x7 @3 倍（21px 高，100% 时用）
    font, glyph_width = _pick_font(text)
    kerning = 0 if len(text) >= 2 else 1
    width = _extent(text, glyph_width, kerning)[0]
    if len(text) >= 3:
        scale = 2
    elif len(text) == 2:
        scale = 3 if width * 3 <= size - 2 else 2
    else:
        scale = 4 if GLYPH_HEIGHT * 4 <= size - 2 else 3
    text_w = width * scale
    text_h = GLYPH_HEIGHT * scale
    origin_x = (size - text_w) // 2
    origin_y = (size - text_h) // 2

    pixels = [(0, 0, 0, 0)] * (size * size)
    grid = [[False] * size for _ in range(size)]

    cursor = 0
    for char in text:
        glyph = font.get(char)
        if glyph is None:
            cursor += glyph_width + kerning
            continue
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit != "1":
                    continue
                base_x = origin_x + (cursor + col_index) * scale
                base_y = origin_y + row_index * scale
                for dy in range(scale):
                    for dx in range(scale):
                        px = base_x + dx
                        py = base_y + dy
                        if 0 <= px < size and 0 <= py < size:
                            grid[py][px] = True
        cursor += glyph_width + kerning

    value = tuple(int(round(max(0.0, min(1.0, channel)) * 255)) for channel in color)
    filled = []
    for py in range(size):
        for px in range(size):
            if grid[py][px]:
                filled.append((px, py, value))

    # 画一个很淡的圆角底，避免数字"飘"在浅色面板上；然后画数字
    result = []
    for py in range(size):
        for px in range(size):
            result.append((0, 0, 0, 0))
    for px, py, rgb in filled:
        result[py * size + px] = (rgb[0], rgb[1], rgb[2], 255)
    return result


# ---------------------------------------------------------------------------
# 取面板前景色（浅色主题用深色图标，深色主题用浅色图标）
# ---------------------------------------------------------------------------
# 放在子进程里做：即使某台机器上 GTK 起不来/卡住，也不会影响守护进程
THEME_PROBE = r'''
import sys
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk
except Exception as exc:  # noqa: BLE001
    sys.stderr.write("no gi: %s\n" % exc)
    sys.exit(1)

ok, _argv = Gtk.init_check([])
if not ok:
    sys.exit(2)

name = ""
dark = "?"
settings = Gtk.Settings.get_default()
if settings is not None:
    name = settings.get_property("gtk-theme-name") or ""
    dark = "1" if settings.get_property("gtk-application-prefer-dark-theme") else "0"

window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
context = window.get_style_context()
try:
    fg = context.get_color(Gtk.StateFlags.NORMAL)
except TypeError:
    fg = context.get_color()
try:
    bg = context.get_background_color(Gtk.StateFlags.NORMAL)
except TypeError:
    bg = context.get_background_color()

print("name %s" % name)
print("dark %s" % dark)
print("fg %.4f %.4f %.4f" % (fg.red, fg.green, fg.blue))
print("bg %.4f %.4f %.4f" % (bg.red, bg.green, bg.blue))
'''


def probe_theme(timeout=10.0):
    """返回 {'name': str, 'dark': bool, 'fg': (r,g,b)|None, 'bg': (r,g,b)|None}。

    任何一步失败都只是缺字段，不会抛异常。
    """
    import subprocess
    import sys

    result = {"name": "", "dark": None, "fg": None, "bg": None}
    try:
        output = subprocess.check_output(
            [sys.executable, "-c", THEME_PROBE],
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            universal_newlines=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return result

    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        key, values = parts[0], parts[1:]
        if key == "name":
            result["name"] = " ".join(values)
        elif key == "dark" and values:
            result["dark"] = values[0] == "1"
        elif key in ("fg", "bg") and len(values) == 3:
            try:
                result[key] = tuple(float(value) for value in values)
            except ValueError:
                pass
    return result


def theme_foreground(timeout=10.0):
    """返回主题前景色 (r, g, b)（0~1），拿不到返回 None。"""
    return probe_theme(timeout=timeout)["fg"]


def resolve_foreground(mode, probed=None):
    """决定图标用哪种前景色。

    mode:
        light → 面板是浅色，画深色图标
        dark  → 面板是深色，画浅色图标
        auto  → 用探测到的主题前景色，探测不到就用中间灰
    """
    mode = (mode or "auto").strip().lower()
    if mode == "light":
        return FOREGROUND_FOR_LIGHT_PANEL
    if mode == "dark":
        return FOREGROUND_FOR_DARK_PANEL
    if probed:
        return tuple(probed)
    return DEFAULT_FOREGROUND


def probe_watcher():
    """诊断用：查询当前桌面有没有 SNI watcher / host。

    返回 {'ok': bool, 'error': str|None, 'has_watcher': bool,
          'host_registered': bool, 'items': [str], 'bus': str|None}
    """
    from . import session as session_module

    result = {
        "ok": False,
        "error": None,
        "has_watcher": False,
        "host_registered": False,
        "items": [],
        "bus": None,
    }
    result["bus"] = session_module.ensure_session_bus()
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:
        result["error"] = "缺少 PyGObject（python3-gi）：%s" % exc
        return result

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as exc:
        result["error"] = "连不上会话总线：%s" % exc
        return result
    if connection is None:
        result["error"] = "没有会话总线（不是图形会话？）"
        return result

    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (WATCHER_NAME,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        result["has_watcher"] = bool(reply.unpack()[0])
    except Exception as exc:
        result["error"] = "查询 %s 失败：%s" % (WATCHER_NAME, exc)
        return result

    if not result["has_watcher"]:
        result["ok"] = True
        return result

    for name, key in (
        ("IsStatusNotifierHostRegistered", "host_registered"),
        ("RegisteredStatusNotifierItems", "items"),
    ):
        try:
            reply = connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (WATCHER_IFACE, name)),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            value = reply.unpack()[0]
            while hasattr(value, "unpack"):
                value = value.unpack()
            if key == "items":
                result["items"] = [str(item) for item in value]
            else:
                result[key] = bool(value)
        except Exception as exc:
            result["error"] = "读取 %s 失败：%s" % (name, exc)

    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# SNI（StatusNotifierItem）
# ---------------------------------------------------------------------------
SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
    <signal name="NewTitle"/>
  </interface>
</node>
"""

class TrayUnavailable(Exception):
    pass


class TrayProvider(object):
    """把电池状态送进系统状态栏。

    用法：
        tray = TrayProvider(logger=print)
        tray.start()                 # 起一个 GLib 主循环线程
        tray.update(87, False)       # 状态变了就调用，线程安全
        tray.stop()
    """

    def __init__(self, logger=None, icon_size=32, id_suffix=None, foreground=None,
                 icon_color="auto", icon_style="number", kind="battery", on_activate=None,
                 host_id_name="cutiepi-battery"):
        self.log = logger or (lambda message: None)
        self.icon_size = int(icon_size)
        self.id_suffix = id_suffix
        self.foreground = foreground
        self.icon_color = (icon_color or "auto").strip().lower()
        self.icon_style = (icon_style or "number").strip().lower()
        self.kind = kind
        self.on_activate = on_activate
        self.host_id_name = host_id_name
        self.backend = None          # 'sni' / 'xembed' / None
        self.status = "未启动"
        self.host_registered = False
        self._thread = None
        self._loop = None
        self._conn = None
        self._bus_name = None
        self._state = {"percent": -1, "charging": False, "present": False}
        self._brightness = {"level": None, "max": 15}
        self._lock = threading.Lock()
        self._last_icon = None
        self._gtk_icon = None
        self._started = threading.Event()
        self._stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._started.wait(timeout=20)
        return self.backend

    def stop(self):
        self._stop.set()
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:
                pass
        self._loop = None

    def update(self, percent, charging, present=True):
        """线程安全：更新状态并让状态栏重画。"""
        percent = -1 if percent is None else int(percent)
        with self._lock:
            if (
                percent == self._state["percent"]
                and bool(charging) == self._state["charging"]
                and bool(present) == self._state["present"]
            ):
                return False
            self._state = {
                "percent": percent,
                "charging": bool(charging),
                "present": bool(present),
            }
        try:
            self._refresh()
        except Exception as exc:  # 不能因为画图失败把守护进程带崩
            self.log("状态栏图标更新失败：%s" % exc)
        return True

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    # -- 亮度图标（kind="brightness"）--------------------------------------
    def update_brightness(self, level, max_level=15):
        """线程安全：更新亮度并让状态栏重画。"""
        try:
            level = int(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        max_level = int(max_level or 15)
        with self._lock:
            if level == self._brightness["level"] and max_level == self._brightness["max"]:
                return False
            self._brightness = {"level": level, "max": max_level}
        try:
            self._refresh()
        except Exception as exc:
            self.log("亮度图标更新失败：%s" % exc)
        return True

    def brightness_snapshot(self):
        with self._lock:
            return dict(self._brightness)

    # -- 后端选择 ---------------------------------------------------------
    def _run(self):
        if self.foreground is None:
            probed = None
            if self.icon_color == "auto":
                probed = theme_foreground()
            self.foreground = resolve_foreground(self.icon_color, probed)
            self.log(
                "图标前景色：%.2f %.2f %.2f（icon_color=%s，%s）"
                % (
                    self.foreground[0],
                    self.foreground[1],
                    self.foreground[2],
                    self.icon_color,
                    "主题探测" if probed else "默认值",
                )
            )

        # 开机时我们常常比面板起得早：watcher 还不存在，注册会失败。
        # 所以这里一直重试（间隔最长 30 秒），直到注册成功。
        attempt = 0
        while not self._stop.is_set():
            try:
                if self._try_sni():
                    return
            except TrayUnavailable as exc:
                if attempt == 0:
                    self.log("SNI 托盘暂时不可用：%s（会继续重试）" % exc)
                else:
                    self.log("SNI 托盘仍不可用（第 %d 次重试）：%s" % (attempt + 1, exc))
            except Exception as exc:
                self.log("SNI 初始化异常：%s（会继续重试）" % exc)

            # 重试两轮后顺便试试 X11 的老式托盘（XEmbed）
            if attempt >= 2:
                try:
                    if self._try_xembed():
                        return
                except Exception as exc:
                    self.log("XEmbed 托盘初始化异常：%s" % exc)

            attempt += 1
            self.status = "等待任务栏托盘（第 %d 次重试）" % attempt
            self._stop.wait(min(30, 3 * attempt))

        self.status = "已停止（没有找到可用的系统托盘）"
        self.log(self.status)
        self._started.set()

    # -- SNI -------------------------------------------------------------
    def _ensure_sni_connection(self):
        """申请总线名并导出对象（可以重复调用，只做一次）。"""
        if self._conn is not None:
            return self._conn

        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        from . import session as session_module

        bus = session_module.ensure_session_bus()
        if bus:
            self.log("SNI：使用会话总线 %s" % bus)

        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            raise TrayUnavailable("连不上会话总线（%s）" % exc.message)
        if connection is None:
            raise TrayUnavailable("拿不到会话总线连接")

        suffix = self.id_suffix or "%d" % int(time.time() * 1000 % 1000000)
        self._bus_name = "org.kde.StatusNotifierItem-%d-%s" % (os.getpid(), suffix)
        result = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (self._bus_name, 0)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        ).unpack()[0]
        if result not in (1, 4):  # 1=PrimaryOwner 4=AlreadyOwner
            raise TrayUnavailable("D-Bus 名字被占用（result=%s）" % result)
        self.log("SNI：已申请总线名 %s" % self._bus_name)

        node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        connection.register_object(
            SNI_PATH,
            node.interfaces[0],
            self._on_method_call,
            self._on_get_property,
            None,
        )
        self._conn = connection
        self.log("SNI：已导出 %s（%s）" % (SNI_PATH, SNI_IFACE))

        # 任务栏重启（换新 watcher）时自动重新注册
        try:
            connection.signal_subscribe(
                "org.freedesktop.DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "/org/freedesktop/DBus",
                WATCHER_NAME,
                Gio.DBusSignalFlags.NONE,
                self._on_watcher_changed,
            )
        except Exception:
            pass
        return connection

    def _on_watcher_changed(self, connection, sender, path, iface, signal, params):
        try:
            name, old_owner, new_owner = params.unpack()
        except Exception:
            return
        if new_owner:
            self.log("任务栏的托盘服务重新出现，重新注册图标")
            self._register_with_watcher()

    def _register_with_watcher(self):
        from gi.repository import Gio, GLib

        if self._conn is None:
            return False
        try:
            self._conn.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_IFACE,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception as exc:
            self.log("重新注册托盘图标失败：%s" % exc)
            return False
        return True

    def _watcher_owned(self, connection):
        from gi.repository import Gio, GLib

        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (WATCHER_NAME,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return bool(reply.unpack()[0])

    def _try_sni(self):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        connection = self._ensure_sni_connection()

        if not self._watcher_owned(connection):
            raise TrayUnavailable("还没有 %s（任务栏可能还没起来）" % WATCHER_NAME)

        if not self._register_with_watcher():
            raise TrayUnavailable("RegisterStatusNotifierItem 调用失败")

        self.backend = "sni"
        self.log("SNI：已调用 RegisterStatusNotifierItem")

        self.host_registered = self._query_host_registered(connection)
        if self.host_registered:
            self.status = "已注册到 SNI 托盘（%s）" % self._bus_name
        else:
            self.status = "watcher 在但没有 host：任务栏可能没有启用托盘图标区"
        self.log(self.status)

        if self._loop is None:
            self._loop = GLib.MainLoop()

        self._refresh()
        self._started.set()

        # 有些面板会漏掉注册瞬间的信号：8 秒后再发一次，确保它去取图标；
        # 也给 watcher 一点时间更新清单，避免误报"没列出我们"
        def _verify():
            try:
                items = self._query_registered_items(self._conn)
                # 清单条目有两种写法：'总线名' 或 '总线名/对象路径'，两种都算注册成功
                if items is not None and not any(
                    item == self._bus_name or item.split("/")[0] == self._bus_name
                    for item in items
                ):
                    self.log(
                        "watcher 没有列出我们（%s）：任务栏可能不会显示图标" % self._bus_name
                    )
                self._refresh()
            except Exception:
                pass
            return False

        GLib.timeout_add(8000, _verify)
        self._loop.run()
        return True

    def _query_host_registered(self, connection):
        from gi.repository import Gio, GLib

        try:
            reply = connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (WATCHER_IFACE, "IsStatusNotifierHostRegistered")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            value = reply.unpack()[0]
            while hasattr(value, "unpack"):
                value = value.unpack()
            return bool(value)
        except Exception:
            return False

    def _query_registered_items(self, connection):
        from gi.repository import Gio, GLib

        try:
            reply = connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (WATCHER_IFACE, "RegisteredStatusNotifierItems")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            value = reply.unpack()[0]
            while hasattr(value, "unpack"):
                value = value.unpack()
            return [str(item) for item in value]
        except Exception:
            return None

    def _on_get_property(self, connection, sender, object_path, interface_name, property_name):
        from gi.repository import GLib

        if self.kind == "brightness":
            state = self.brightness_snapshot()
            title, tooltip = build_sun_tooltip(state["level"], state["max"])
        else:
            state = self.snapshot()
            title, tooltip = build_tooltip(
                state["percent"] if state["present"] else None,
                state["charging"],
                state["present"],
            )
        if property_name == "Category":
            return GLib.Variant("s", "SystemServices")
        if property_name == "Id":
            return GLib.Variant("s", self.host_id_name)
        if property_name == "Title":
            return GLib.Variant("s", title)
        if property_name == "Status":
            return GLib.Variant("s", "Active")
        if property_name == "IconName":
            return GLib.Variant("s", "")
        if property_name == "IconPixmap":
            return GLib.Variant("a(iiay)", [(self.icon_size, self.icon_size, self._icon_bytes())])
        if property_name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], title, tooltip))
        if property_name == "ItemIsMenu":
            return GLib.Variant("b", False)
        return None

    def _on_method_call(self, connection, sender, object_path, interface_name,
                        method_name, parameters, invocation):
        # 亮度图标：左键/中键点击 -> 弹亮度滑块
        if self.kind == "brightness" and self.on_activate is not None:
            if method_name in ("Activate", "SecondaryActivate", "ContextMenu"):
                try:
                    self.on_activate(method_name)
                except Exception as exc:
                    self.log("处理点击失败：%s" % exc)
                invocation.return_value(None)
                return
        # 电池图标：点一下什么都不做（避免弹出空菜单）
        invocation.return_value(None)

    def _pixels(self):
        """按当前状态渲染图标像素（带缓存，避免每次 GetAll 都重画）。"""
        if self.kind == "brightness":
            state = self.brightness_snapshot()
            key = ("brightness", state["level"], state["max"], self.icon_size, self.foreground)
            with self._lock:
                cached = self._last_icon
                if cached is not None and cached[0] == key:
                    return cached[1]
            pixels = render_sun_icon(
                state["level"] if state["level"] is not None else 1,
                state["max"],
                self.icon_size,
                foreground=self.foreground,
            )
            with self._lock:
                self._last_icon = (key, pixels)
            return pixels

        state = self.snapshot()
        key = (
            state["percent"],
            state["charging"],
            state["present"],
            self.icon_size,
            self.foreground,
            self.icon_style,
        )
        with self._lock:
            cached = self._last_icon
            if cached is not None and cached[0] == key:
                return cached[1]
        if self.icon_style == "battery":
            pixels = render_icon(
                state["percent"],
                state["charging"],
                state["present"],
                self.icon_size,
                foreground=self.foreground,
            )
        else:
            pixels = render_number_icon(
                state["percent"],
                state["charging"],
                state["present"],
                self.icon_size,
                foreground=self.foreground,
            )
        with self._lock:
            self._last_icon = (key, pixels)
        return pixels

    def _icon_bytes(self):
        """SNI 用的 ARGB 字节。"""
        return to_argb_bytes(self._pixels())

    def _icon_rgba(self):
        """GdkPixbuf 用的 RGBA 字节。"""
        return to_rgba_bytes(self._pixels())

    def _refresh(self):
        state = self.snapshot()
        if self.backend == "sni" and self._conn is not None:
            from gi.repository import GLib  # noqa: F401  (确认 gi 可用)

            # GDBus 的 emit_signal 是线程安全的，可以直接从守护进程线程发
            self._conn.emit_signal(None, SNI_PATH, SNI_IFACE, "NewIcon", None)
            self._conn.emit_signal(None, SNI_PATH, SNI_IFACE, "NewToolTip", None)
        elif self.backend == "xembed" and self._gtk_icon is not None:
            from gi.repository import GLib

            # GTK 只能在托盘线程里碰，必须切线程
            GLib.idle_add(self._update_xembed, state)

    # -- XEmbed（GTK3 已废弃但仍可用，X11 老托盘） -------------------------
    def _try_xembed(self):
        import os

        if not os.environ.get("DISPLAY"):
            raise TrayUnavailable("没有 DISPLAY")
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GdkPixbuf, GLib, Gtk

        if not Gtk.init_check([])[0]:
            raise TrayUnavailable("GTK 初始化失败（没有可用的显示）")

        self._gtk_icon = Gtk.StatusIcon()
        self._gtk_icon.set_title("电池")
        self._gtk_icon.set_visible(True)
        self.backend = "xembed"
        self._update_xembed(self.snapshot())

        self._loop = GLib.MainLoop()
        self._started.set()

        # 1 秒后检查托盘是否接管了图标
        def _probe():
            embedded = bool(self._gtk_icon is not None and self._gtk_icon.is_embedded())
            if embedded:
                self.status = "已嵌入 XEmbed 系统托盘"
            else:
                self.status = "GtkStatusIcon 已创建但托盘没有接管（可能不是 X11 托盘）"
            self.log(self.status)
            return False

        GLib.timeout_add(1000, _probe)
        self._loop.run()
        return True

    def _update_xembed(self, state):
        from gi.repository import GdkPixbuf, GLib

        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(self._icon_rgba()),
            GdkPixbuf.Colorspace.RGB,
            True,
            8,
            self.icon_size,
            self.icon_size,
            self.icon_size * 4,
        )
        self._gtk_icon.set_from_pixbuf(pixbuf)
        title, tooltip = build_tooltip(
            state["percent"] if state["present"] else None,
            state["charging"],
            state["present"],
        )
        self._gtk_icon.set_tooltip_text("%s（%s）" % (title, tooltip))
