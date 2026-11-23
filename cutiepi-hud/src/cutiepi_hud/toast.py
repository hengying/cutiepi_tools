#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""一小块会自动消失的提示（toast），显示在状态栏下方。

为什么不直接用状态栏图标的 ToolTip：触摸屏上没有"指针离开"事件，
面板弹出的 tooltip 气泡不会自己消失，而且会一直显示它缓存的那份旧文本。
所以 ToolTip 永久留空，切换状态时用这个独立小窗口提示 2.5 秒。

用法：
    python3 toast.py "旋转已禁止" [--seconds 2.5]
"""

import os
import sys
import time

# 允许直接 `python3 toast.py` 运行（托盘子进程就是这么起的）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cutiepi_hud"

DEFAULT_SECONDS = 2.5


def run_toast(text, seconds=DEFAULT_SECONDS):
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as exc:
        sys.stderr.write("需要 GTK3 / PyGObject：%s\n" % exc)
        return 2

    if not Gtk.init_check([])[0]:
        sys.stderr.write("没有可用的显示（请在桌面会话里运行）\n")
        return 2

    deadline = time.time() + max(0.5, float(seconds))

    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_decorated(False)
    window.set_keep_above(True)
    # 不要出现在任务栏/Alt-Tab 里，它只是一闪而过的提示
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_accept_focus(False)
    window.set_focus_on_map(False)
    window.set_app_paintable(True)
    window.set_title(text)
    window.connect("destroy", Gtk.main_quit)

    css = b"""
    window, window.background, .background { background-color: transparent; }
    #card {
        background-color: rgba(28, 30, 35, 0.96);
        border: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 14px;
        padding: 12px 20px;
    }
    #text { color: #ffffff; font-size: 19px; font-weight: bold; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    card.set_name("card")
    # 放屏幕底部居中：避开状态栏图标和系统通知（通知常驻右上角）
    card.set_halign(Gtk.Align.CENTER)
    card.set_valign(Gtk.Align.END)
    card.set_margin_bottom(64)

    label = Gtk.Label(label=text)
    label.set_name("text")
    card.pack_start(label, False, False, 0)

    window.add(card)

    def close(_widget=None):
        Gtk.main_quit()
        return True

    # 点一下任意位置就提前关掉
    window.connect("button-press-event", close)

    def tick():
        if time.time() >= deadline:
            close()
            return False
        return True

    GLib.timeout_add(100, tick)
    window.fullscreen()
    window.show_all()
    Gtk.main()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    text = "就绪"
    seconds = DEFAULT_SECONDS
    rest = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--seconds" and index + 1 < len(argv):
            try:
                seconds = float(argv[index + 1])
            except ValueError:
                pass
            index += 2
            continue
        rest.append(item)
        index += 1
    if rest:
        text = " ".join(rest)
    return run_toast(text, seconds)


if __name__ == "__main__":
    sys.exit(main())
