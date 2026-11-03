#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""亮度滑块弹窗（GTK3，触摸友好）。

从托盘图标点出来，全屏半透明遮罩 + 右上角卡片里的滑块。
拖动即生效（直接写 /sys/class/backlight/*/brightness，范围 1..15）。

用法：
    python3 -m cutiepi_hud.brightness_popup [--level 8] [--timeout 8]
退出码 0；最终亮度会打印到标准输出（`level=8`），方便调用方读取。
"""

import os
import sys
import time

# 允许直接 `python3 brightness_popup.py` 运行（托盘子进程就是这么起的）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cutiepi_hud"

DEFAULT_TIMEOUT = 8.0


def run_popup(level=None, max_level=15, timeout=DEFAULT_TIMEOUT, on_change=None):
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as exc:
        sys.stderr.write("需要 GTK3 / PyGObject：%s\n" % exc)
        return None

    if not Gtk.init_check([])[0]:
        sys.stderr.write("没有可用的显示（请在桌面会话里运行）\n")
        return None

    if level is None:
        level = 1
    level = max(1, min(int(max_level), int(level)))
    state = {"level": level, "done": False, "last_action": time.time()}

    def apply(value):
        value = max(1, min(int(max_level), int(round(value))))
        state["level"] = value
        state["last_action"] = time.time()
        if on_change is not None:
            try:
                on_change(value)
            except Exception:
                pass

    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_decorated(False)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_title("亮度")
    window.set_app_paintable(True)
    window.connect("destroy", Gtk.main_quit)

    css = b"""
    window, window.background, .background { background-color: transparent; }
    #shade { background-color: rgba(0, 0, 0, 0.35); }
    #card {
        background-color: rgba(26, 28, 33, 0.98);
        border: 2px solid rgba(255, 255, 255, 0.14);
        border-radius: 20px;
        padding: 22px;
    }
    #head { color: #ffffff; font-size: 26px; font-weight: bold; }
    #value { color: #ffd166; font-size: 42px; font-weight: bold; }
    #hint { color: #9aa1aa; font-size: 15px; }
    scale trough { min-height: 26px; min-width: 420px; border-radius: 13px;
                   background-color: #3a3f47; }
    scale highlight { background-color: #ffd166; border-radius: 13px; }
    scale slider { min-width: 40px; min-height: 40px; margin: -10px;
                   background-color: #ffffff; border-radius: 20px; }
    button { font-size: 26px; padding: 6px 18px; border-radius: 12px;
             background-image: none; background-color: #3a3f47; color: #ffffff; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.set_name("card")
    card.set_size_request(500, -1)
    card.set_halign(Gtk.Align.END)
    card.set_valign(Gtk.Align.START)
    card.set_margin_top(48)
    card.set_margin_end(28)

    head = Gtk.Label(label="屏幕亮度")
    head.set_name("head")
    head.set_xalign(0.0)

    value_label = Gtk.Label(label=str(level))
    value_label.set_name("value")
    value_label.set_xalign(1.0)

    header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    header_row.pack_start(head, True, True, 0)
    header_row.pack_start(value_label, False, False, 0)
    card.pack_start(header_row, False, False, 0)

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    minus = Gtk.Button(label="−")
    plus = Gtk.Button(label="＋")
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, max_level, 1)
    scale.set_draw_value(False)
    scale.set_value(level)
    scale.set_hexpand(True)
    scale.set_inverted(False)
    row.pack_start(minus, False, False, 0)
    row.pack_start(scale, True, True, 0)
    row.pack_start(plus, False, False, 0)
    card.pack_start(row, False, False, 0)

    hint = Gtk.Label(label="拖动滑块调整，点空白处关闭")
    hint.set_name("hint")
    hint.set_xalign(0.0)
    card.pack_start(hint, False, False, 0)

    def on_scale_changed(widget):
        raw = widget.get_value()
        # Gtk.Scale 是连续的，这里按整数步进吸附
        snapped = max(1, min(int(max_level), int(round(raw))))
        if abs(raw - snapped) > 0.01:
            widget.set_value(snapped)
            return
        value_label.set_text(str(snapped))
        apply(snapped)

    scale.connect("value-changed", on_scale_changed)
    minus.connect("clicked", lambda _b: scale.set_value(scale.get_value() - 1))
    plus.connect("clicked", lambda _b: scale.set_value(scale.get_value() + 1))

    def close(_widget=None):
        state["done"] = True
        Gtk.main_quit()

    def on_click(_widget, event):
        state["last_action"] = time.time()
        # 窗口是全屏的（左上角 0,0），所以 root 坐标就是窗口内坐标
        allocation = card.get_allocation()
        local_x, local_y = event.x_root, event.y_root
        if (
            allocation.x <= local_x <= allocation.x + allocation.width
            and allocation.y <= local_y <= allocation.y + allocation.height
        ):
            return False
        close()
        return True

    def on_key(_widget, event):
        if event.keyval == Gdk.KEY_Escape:
            close()
            return True
        if event.keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            scale.set_value(scale.get_value() - 1)
            return True
        if event.keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            scale.set_value(scale.get_value() + 1)
            return True
        return False

    window.connect("button-press-event", on_click)
    window.connect("key-press-event", on_key)

    shade = Gtk.EventBox()
    shade.set_name("shade")
    shade.add(card)
    window.add(shade)
    shade.connect("button-press-event", lambda _w, event: on_click(_w, event))

    # 空闲自动关闭
    def tick():
        if state["done"]:
            return False
        if timeout and time.time() - state["last_action"] > timeout:
            close()
            return False
        return True

    GLib.timeout_add(500, tick)
    window.fullscreen()
    window.show_all()
    scale.grab_focus()
    Gtk.main()
    return state["level"]


def main(argv=None):
    from . import backlight

    argv = list(sys.argv[1:] if argv is None else argv)
    level = backlight.get_brightness()
    max_level = backlight.max_brightness()
    timeout = DEFAULT_TIMEOUT
    if "--level" in argv:
        try:
            level = int(argv[argv.index("--level") + 1])
        except (IndexError, ValueError):
            pass
    if "--timeout" in argv:
        try:
            timeout = float(argv[argv.index("--timeout") + 1])
        except (IndexError, ValueError):
            pass

    result = run_popup(
        level=level,
        max_level=max_level,
        timeout=timeout,
        on_change=lambda value: backlight.set_brightness(value, max_level=max_level),
    )
    if result is None:
        return 2
    print("level=%d" % result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
