#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""旋转锁定托盘图标：点一下在"允许旋转 / 禁止旋转"之间切换。

单独一个进程的原因和亮度图标一样：状态栏每个图标都要自己的一条 SNI 连接和
/StatusNotifierItem 对象路径。

状态存在用户配置 ``~/.config/cutiepi-hud/config.ini`` 的 ``[rotation]`` 段，
守护进程每秒比对一次文件，看到变化就切换行为。

用法：
    python3 rotation_tray.py          # 前台运行（调试用）
"""

import os
import subprocess
import sys
import time

# 允许直接 `python3 rotation_tray.py` 运行（守护进程就是这么起的）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cutiepi_hud"

TOAST_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toast.py")


def toast_text(locked):
    """切换状态时弹出的提示文字。"""
    return "旋转已禁止" if locked else "旋转已允许"


def show_hint(text, holder):
    """切换状态时提示几秒：优先用桌面通知（面板会按时收起），失败才自绘气泡。"""
    from . import notify as notify_module

    if notify_module.notify(text):
        return True

    proc = holder.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        holder["proc"] = subprocess.Popen(
            [sys.executable, TOAST_SCRIPT, text], env=dict(os.environ)
        )
        return True
    except OSError as exc:
        _log("提示窗口启动失败：%s" % exc)
        return False


def _log(message):
    sys.stdout.write("%s INFO    %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


class RotationTrayProcess(object):
    """把旋转锁定图标当成子进程跑（守护进程用它）。"""

    def __init__(self, logger=None):
        from .trayprocess import TrayProcess

        self._inner = TrayProcess(os.path.abspath(__file__), logger)

    @property
    def running(self):
        return self._inner.running

    def start(self):
        return self._inner.start()

    def ensure(self):
        return self._inner.ensure()

    def stop(self):
        self._inner.stop()


def read_locked(store, default_locked=False):
    """读锁定状态；文件里没有该键时用默认值。"""
    locked, _angle = store.read()
    return default_locked if locked is None else bool(locked)


def main():
    from . import config as config_module
    from . import lockstate
    from . import tray as tray_module

    cfg = config_module.load_config()
    store = lockstate.RotationState()
    default_locked = cfg.as_bool("rotation", "locked")
    state = {"locked": read_locked(store, default_locked)}
    toast = {}

    def on_activate(_method):
        state["locked"] = not state["locked"]
        try:
            store.set_locked(state["locked"])
        except (IOError, OSError) as exc:
            _log("保存锁定状态失败：%s" % exc)
        provider.update_lock(state["locked"])
        # 只在"点图标切换"时提示 2.5 秒，之后不再显示任何 tooltip
        show_hint(toast_text(state["locked"]), toast)
        _log("旋转已%s" % ("禁止（保持当前方向）" if state["locked"] else "允许（跟随重力）"))

    provider = tray_module.TrayProvider(
        logger=_log,
        kind="rotation",
        on_activate=on_activate,
        id_suffix="rotation",
        icon_size=cfg.as_int("hud", "icon_size"),
        icon_color=cfg.get("hud", "icon_color"),
        host_id_name="cutiepi-rotation",
    )
    provider.start()
    provider.update_lock(state["locked"])
    _log("旋转锁定图标：%s（当前：%s）" % (
        provider.backend or "（等待托盘）",
        "已锁定" if state["locked"] else "允许旋转",
    ))

    try:
        while True:
            locked = read_locked(store, default_locked)
            if locked != state["locked"]:
                state["locked"] = locked
                provider.update_lock(locked)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        provider.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
