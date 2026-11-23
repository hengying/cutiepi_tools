#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""桌面通知（用于"切换后提示几秒就消失"）。

为什么不用状态栏图标的 ToolTip：触摸屏上没有"指针离开"事件，面板弹的
tooltip 气泡不会自己消失，还会一直显示缓存的旧文本。
桌面通知带 ``expire_timeout``，面板会按时收起，也不占任务栏。
"""

DEFAULT_TIMEOUT_MS = 2500


def notify(summary, body="", timeout_ms=DEFAULT_TIMEOUT_MS, app_name="cutiepi-hud"):
    """发一条会自动过期的桌面通知；成功返回 True，没有通知服务返回 False。"""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return False

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except (GLib.Error, OSError):
        return False
    if connection is None:
        return False

    try:
        connection.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                (app_name, 0, "", summary, body, [], {}, int(timeout_ms)),
            ),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return True
    except (GLib.Error, OSError):
        return False
