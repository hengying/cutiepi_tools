#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""长按电源键后的确认对话框（GTK3，触摸友好）。

用法：
    python3 -m cutiepi_hud.dialog shutdown [--timeout 15]
    退出码 0 = 用户确认执行，1 = 取消/超时，2 = 出错

单独放一个进程：对话框崩了也不会影响守护进程；守护进程只需要看退出码。
"""

import sys
import time

CONFIRM = 0
CANCEL = 1
ERROR = 2


def run_dialog(title, message, confirm_text, cancel_text, timeout, danger=True):
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as exc:
        sys.stderr.write("需要 GTK3 / PyGObject：%s\n" % exc)
        return ERROR

    if not Gtk.init_check([])[0]:
        sys.stderr.write("没有可用的显示（请在桌面会话里运行）\n")
        return ERROR

    result = {"value": CANCEL}

    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_title("CutiePi")
    window.set_decorated(False)
    window.set_resizable(False)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.connect("destroy", Gtk.main_quit)
    window.connect("key-press-event", lambda _w, ev: _on_key(ev))

    def _on_key(event):
        if event.keyval == Gdk.KEY_Escape:
            result["value"] = CANCEL
            Gtk.main_quit()
            return True
        return False

    # 外观：全屏半透明遮罩 + 居中深色卡片，字号放大方便手指点
    css = b"""
    window, window.background, .background { background-color: transparent; }
    #overlay { background-color: rgba(0, 0, 0, 0.55); }
    #panel {
        background-color: rgba(24, 26, 30, 0.98);
        border: 2px solid rgba(255, 255, 255, 0.12);
        border-radius: 20px;
        padding: 26px;
    }
    #title { color: #ffffff; font-size: 34px; font-weight: bold; }
    #message { color: #ccd1d8; font-size: 20px; }
    #countdown { color: #f0b429; font-size: 17px; }
    button { font-size: 24px; padding: 16px 30px; border-radius: 14px; }
    #cancel { background-image: none; background-color: #3a3f47; color: #ffffff; }
    #confirm { background-image: none; background-color: #c62828; color: #ffffff; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    window.set_app_paintable(True)

    overlay = Gtk.Box()
    overlay.set_name("overlay")
    window.add(overlay)

    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    panel.set_name("panel")
    panel.set_size_request(620, -1)
    panel.set_valign(Gtk.Align.CENTER)
    panel.set_halign(Gtk.Align.CENTER)
    overlay.set_center_widget(panel)

    title_label = Gtk.Label(label=title)
    title_label.set_name("title")
    title_label.set_xalign(0.5)
    panel.pack_start(title_label, False, False, 0)

    message_label = Gtk.Label(label=message)
    message_label.set_name("message")
    message_label.set_line_wrap(True)
    message_label.set_justify(Gtk.Justification.CENTER)
    message_label.set_xalign(0.5)
    panel.pack_start(message_label, False, False, 0)

    countdown_label = Gtk.Label(label="")
    countdown_label.set_name("countdown")
    panel.pack_start(countdown_label, False, False, 0)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
    buttons.set_homogeneous(True)
    panel.pack_start(buttons, False, False, 10)

    cancel_button = Gtk.Button(label=cancel_text)
    cancel_button.set_name("cancel")
    confirm_button = Gtk.Button(label=confirm_text)
    confirm_button.set_name("confirm")
    buttons.pack_start(cancel_button, True, True, 0)
    buttons.pack_start(confirm_button, True, True, 0)

    def _cancel(_widget=None):
        result["value"] = CANCEL
        Gtk.main_quit()

    def _confirm(_widget=None):
        result["value"] = CONFIRM
        Gtk.main_quit()

    cancel_button.connect("clicked", _cancel)
    confirm_button.connect("clicked", _confirm)

    deadline = time.time() + float(timeout) if timeout else None

    def _tick():
        if deadline is None:
            return False
        left = int(round(deadline - time.time()))
        if left <= 0:
            _cancel()
            return False
        countdown_label.set_text("%d 秒后自动取消" % left)
        return True

    if deadline is not None:
        _tick()
        GLib.timeout_add(500, _tick)

    window.fullscreen()
    window.show_all()
    Gtk.main()
    return result["value"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return ERROR

    action = argv[0]
    timeout = 15.0
    if "--timeout" in argv:
        try:
            timeout = float(argv[argv.index("--timeout") + 1])
        except (IndexError, ValueError):
            pass

    if action == "shutdown":
        return run_dialog(
            "关机",
            "长按电源键 —— 要关闭平板吗？",
            "关机",
            "取消",
            timeout,
        )
    if action == "screen":
        return run_dialog(
            "屏幕",
            "要关闭屏幕吗？",
            "息屏",
            "取消",
            timeout,
        )
    sys.stderr.write("未知动作：%s\n" % action)
    return ERROR


if __name__ == "__main__":
    sys.exit(main())
