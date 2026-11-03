"""桌面会话环境。

守护进程可能比桌面会话先启动（systemd user service、autostart 竞争），
所以这里做两件事：

1. 补全 WAYLAND_DISPLAY / DISPLAY / XAUTHORITY 等变量；
2. 等桌面真正就绪（socket 出现）再启动叠加层。
"""

import os
import re
import time

from . import util

WAYLAND_NAMES = ("wayland-0", "wayland-1", "wayland-2", "wayland-3")


def _runtime_dir():
    value = util.runtime_dir()
    if value:
        os.environ.setdefault("XDG_RUNTIME_DIR", value)
    return value


def _find_wayland():
    runtime = _runtime_dir()
    current = os.environ.get("WAYLAND_DISPLAY")
    if current:
        path = current if os.path.isabs(current) else os.path.join(runtime or "", current)
        if os.path.exists(path):
            return current
    if not runtime:
        return None
    for name in WAYLAND_NAMES:
        if os.path.exists(os.path.join(runtime, name)):
            return name
    try:
        for name in sorted(os.listdir(runtime)):
            if name.startswith("wayland-") and not name.endswith(".lock"):
                if os.path.exists(os.path.join(runtime, name)):
                    return name
    except OSError:
        pass
    return None


def _find_x11():
    current = os.environ.get("DISPLAY")
    if current:
        match = re.match(r"^:(\d+)", current)
        if match and os.path.exists("/tmp/.X11-unix/X%s" % match.group(1)):
            return current
    for number in range(0, 4):
        if os.path.exists("/tmp/.X11-unix/X%d" % number):
            return ":%d" % number
    return None


def session_bus_address():
    """找到桌面会话的 D-Bus 总线地址。

    在 SSH / 控制台里启动时，`DBUS_SESSION_BUS_ADDRESS` 通常是空的，
    这时 GDBus 会去"自动启动"一个新总线——托盘项就注册到了那个孤立的总线上，
    任务栏当然看不到。用户总线固定是 $XDG_RUNTIME_DIR/bus，直接用它是可靠的。
    """
    current = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if current:
        path = None
        for part in current.split(";"):
            if part.startswith("unix:path="):
                path = part[len("unix:path="):]
                break
        if path is None or os.path.exists(path):
            return current

    runtime = _runtime_dir()
    if runtime:
        candidate = os.path.join(runtime, "bus")
        if os.path.exists(candidate):
            return "unix:path=%s" % candidate
    return None


def ensure_session_bus():
    """把 DBUS_SESSION_BUS_ADDRESS 修正成桌面会话那条总线，返回地址或 None。"""
    address = session_bus_address()
    if address:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    return address


def probe():
    """不等待，只报告当前环境。"""
    return {"wayland": _find_wayland(), "x11": _find_x11(), "runtime_dir": _runtime_dir()}


def ensure_display_env(timeout=90.0, logger=None, interval=0.5):
    """等待并准备会话环境，返回 {'backend': 'wayland'|'x11'|None, ...}。"""
    log = logger or (lambda message: None)
    deadline = time.time() + float(timeout)
    reported = False

    while True:
        info = probe()
        bus = ensure_session_bus()
        if info["wayland"]:
            os.environ["WAYLAND_DISPLAY"] = info["wayland"]
            if not os.environ.get("XDG_RUNTIME_DIR") and info["runtime_dir"]:
                os.environ["XDG_RUNTIME_DIR"] = info["runtime_dir"]
            # Wayland 会话里 GDK 需要能回退到 X11 以外的后端，这里清掉老的 DISPLAY 也不必，
            # 但要让 GDK 优先选 Wayland
            os.environ.pop("GDK_BACKEND", None)
            if not os.environ.get("XAUTHORITY"):
                guess = os.path.join(os.path.expanduser("~"), ".Xauthority")
                if os.path.exists(guess):
                    os.environ["XAUTHORITY"] = guess
            log("检测到 Wayland 会话：WAYLAND_DISPLAY=%s" % info["wayland"])
            log("会话总线：%s" % (bus or "未找到（托盘图标可能注册不上）"))
            return {"backend": "wayland", "display": info["wayland"], "bus": bus}

        if info["x11"]:
            os.environ["DISPLAY"] = info["x11"]
            if not os.environ.get("XAUTHORITY"):
                guess = os.path.join(os.path.expanduser("~"), ".Xauthority")
                if os.path.exists(guess):
                    os.environ["XAUTHORITY"] = guess
            log("检测到 X11 会话：DISPLAY=%s" % info["x11"])
            log("会话总线：%s" % (bus or "未找到（托盘图标可能注册不上）"))
            return {"backend": "x11", "display": info["x11"], "bus": bus}

        if time.time() >= deadline:
            log("等待桌面会话超时（%.0f 秒），没有找到 Wayland 或 X11 socket" % float(timeout))
            return {"backend": None, "display": None}

        if not reported:
            log("等待桌面会话就绪…")
            reported = True
        time.sleep(interval)
