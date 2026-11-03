"""管理"独立角标"进程（C 程序 hud-overlay，备用方案）。

电池状态默认显示在系统状态栏（见 tray.py）。只有面板没有托盘图标区、
配置成 hud/mode = overlay 时才会用到这个进程。

* 叠加层崩溃/被关掉会自动重启（带退避，避免疯狂重启）；
* 二进制不存在时只记一条警告，守护进程继续跑（旋转和日志不受影响）。
"""

import os
import subprocess
import time

RESTART_DELAYS = (1, 2, 5, 10, 30, 60)


class OverlayProcess(object):
    def __init__(self, binary, state_path, margin_top=40, margin_right=8, logger=None):
        self.binary = binary
        self.state_path = state_path
        self.margin_top = int(margin_top)
        self.margin_right = int(margin_right)
        self.log = logger or (lambda message: None)
        self.proc = None
        self._starts = 0
        self._started_at = 0.0
        self._next_start = 0.0
        self._missing_warned = False

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if not self.binary or not os.path.exists(self.binary):
            if not self._missing_warned:
                self.log("叠加层程序不存在：%s（电量只在日志里显示）" % self.binary)
                self._missing_warned = True
            return False

        command = [
            self.binary,
            "--state-file",
            self.state_path,
            "--margin-top",
            str(self.margin_top),
            "--margin-right",
            str(self.margin_right),
        ]
        env = dict(os.environ)
        try:
            self.proc = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            self.log("启动叠加层失败：%s" % exc)
            return False
        self._starts += 1
        self._started_at = time.time()
        self.log("叠加层已启动（pid=%d）" % self.proc.pid)
        return True

    def ensure(self):
        """按退避策略必要时重启。"""
        if self.running:
            # 稳定运行超过 1 分钟就重置退避，下次崩溃时能立刻重启
            if self._starts and time.time() - self._started_at > 60:
                self._starts = 0
            return True
        now = time.time()
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self.log("叠加层退出（code=%s），稍后重启" % code)
                self.proc = None
        if now < self._next_start:
            return False
        delay = RESTART_DELAYS[min(self._starts, len(RESTART_DELAYS) - 1)]
        self._next_start = now + delay
        return self.start()

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.log("叠加层已停止")
        self.proc = None
