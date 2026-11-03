#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""亮度托盘图标：状态栏上的太阳图标，点一下弹亮度滑块。

单独一个进程的原因：状态栏每个图标都要自己的一条 SNI 连接和
/StatusNotifierItem 对象路径，跟电池图标放在同一个进程里会冲突。

用法：
    python3 -m cutiepi_hud.brightness_tray        # 前台运行（调试用）
"""

import os
import subprocess
import sys
import time

# 允许直接 `python3 brightness_tray.py` 运行（子进程就是这么起的）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "cutiepi_hud"

RESTART_DELAYS = (2, 5, 10, 30, 60)
POPUP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightness_popup.py")


def _log(message):
    # 和守护进程的日志格式保持一致（时间 + INFO + 内容）
    sys.stdout.write("%s INFO    %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


class BrightnessTrayProcess(object):
    """把亮度图标当成子进程跑，崩了按退避重启（守护进程用它）。"""

    def __init__(self, logger=None):
        self.log = logger or (lambda message: None)
        self.proc = None
        self._starts = 0
        self._next_start = 0.0
        self._started_at = 0.0

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        try:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                env=dict(os.environ),
            )
        except OSError as exc:
            self.log("亮度图标启动失败：%s" % exc)
            return False
        self._starts += 1
        self._started_at = time.time()
        self.log("亮度图标已启动（pid=%d）" % self.proc.pid)
        return True

    def ensure(self):
        if self.running:
            if self._starts and time.time() - self._started_at > 120:
                self._starts = 0
            return True
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self.log("亮度图标退出（code=%s），稍后重启" % code)
                self.proc = None
        now = time.time()
        if now < self._next_start:
            return False
        delay = RESTART_DELAYS[min(self._starts, len(RESTART_DELAYS) - 1)]
        self._next_start = now + delay
        return self.start()

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None


def main():
    from . import backlight, config as config_module
    from . import tray as tray_module

    cfg = config_module.load_config()
    icon_color = cfg.get("hud", "icon_color")
    popup = {"proc": None}

    def on_activate(_method):
        # 已经开着就别再开一个
        if popup["proc"] is not None and popup["proc"].poll() is None:
            return
        popup["proc"] = subprocess.Popen(
            [sys.executable, POPUP_SCRIPT], env=dict(os.environ)
        )
        _log("弹出亮度滑块（pid=%d）" % popup["proc"].pid)

    provider = tray_module.TrayProvider(
        logger=_log,
        kind="brightness",
        on_activate=on_activate,
        id_suffix="brightness",
        icon_size=32,
        icon_color=icon_color,
        host_id_name="cutiepi-brightness",
    )
    provider.start()
    _log("亮度图标：%s" % (provider.backend or "（等待托盘）"))

    try:
        while True:
            level = backlight.get_brightness()
            max_level = backlight.max_brightness()
            provider.update_brightness(level, max_level)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        provider.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
