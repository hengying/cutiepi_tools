#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""把"一个状态栏图标一个小进程"这件事抽出来。

状态栏里每个图标都要自己的一条 SNI 连接和 /StatusNotifierItem 对象路径，
所以电池之外的两个图标（亮度、旋转锁定）都是独立进程；这里统一做
"崩了按退避重启"的监督逻辑。
"""

import os
import subprocess
import sys
import time

RESTART_DELAYS = (2, 5, 10, 30, 60)


class TrayProcess(object):
    """把一个托盘图标脚本当子进程跑。"""

    def __init__(self, script_path, logger=None):
        self.script_path = os.path.abspath(script_path) if script_path else None
        self.log = logger or (lambda message: None)
        self.proc = None
        self._starts = 0
        self._next_start = 0.0
        self._started_at = 0.0

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if not self.script_path or not os.path.exists(self.script_path):
            self.log("找不到 %s" % self.script_path)
            return False
        try:
            self.proc = subprocess.Popen(
                [sys.executable, self.script_path],
                env=dict(os.environ),
            )
        except OSError as exc:
            self.log("启动 %s 失败：%s" % (os.path.basename(self.script_path), exc))
            return False
        self._starts += 1
        self._started_at = time.time()
        self.log("%s 已启动（pid=%d）" % (os.path.basename(self.script_path), self.proc.pid))
        return True

    def ensure(self):
        if self.running:
            if self._starts and time.time() - self._started_at > 120:
                self._starts = 0
            return True
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self.log(
                    "%s 退出（code=%s），稍后重启" % (os.path.basename(self.script_path), code)
                )
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
