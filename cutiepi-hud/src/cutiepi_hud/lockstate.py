#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""旋转锁定：状态持久化 + 决策策略。

状态存在用户配置 ``~/.config/cutiepi-hud/config.ini`` 的 ``[rotation]`` 段：

    [rotation]
    locked = true          ; true=禁止自动旋转，false=跟随重力
    last_angle = 270       ; 上一次生效的屏幕角度（0/90/180/270）

为什么放配置文件：需求要求"永久保存"，而且这个文件本来就会持久化；
守护进程每次下发旋转都会写一次 ``last_angle``（一分钟也就几次），
写入走 ``config.write_user_overrides``，它带文件锁和原子替换。

``RotationPolicy`` 只做"该不该转、转到多少"的判断，不碰硬件，
因此可以在电脑上直接单测。
"""

import configparser
import os

from . import config as config_module

VALID_ANGLES = (0, 90, 180, 270)


def normalize_angle(value):
    """把配置里的值转成合法角度；非法返回 None。"""
    try:
        angle = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return angle if angle in VALID_ANGLES else None


def _parse_bool(value):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return None


class RotationState(object):
    """``[rotation]`` 段的读写。只读用户配置，不参与 /etc 的层叠。"""

    def __init__(self, user_path=None):
        self.user_path = user_path or config_module.user_conf_path()

    def signature(self):
        """文件的 (mtime_ns, size)，用来判断是否被改过；不存在返回 None。"""
        try:
            info = os.stat(self.user_path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def read(self):
        """返回 (locked, angle)；文件里没写该键时对应项为 None。"""
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        if os.path.exists(self.user_path):
            try:
                with open(self.user_path, "r") as handle:
                    parser.read_file(handle)
            except (configparser.Error, IOError, OSError):
                return (None, None)

        locked = None
        angle = None
        if parser.has_option("rotation", "locked"):
            locked = _parse_bool(parser.get("rotation", "locked"))
        if parser.has_option("rotation", "last_angle"):
            angle = normalize_angle(parser.get("rotation", "last_angle"))
        return (locked, angle)

    def write(self, locked=None, angle=None):
        """写回状态（只写传入的项），返回配置文件路径。"""
        items = {}
        if locked is not None:
            items["locked"] = bool(locked)
        if angle is not None:
            normalized = normalize_angle(angle)
            if normalized is None:
                return self.user_path
            items["last_angle"] = normalized
        if not items:
            return self.user_path
        return config_module.write_user_overrides({"rotation": items}, self.user_path)

    def set_locked(self, locked):
        return self.write(locked=locked)

    def set_angle(self, angle):
        return self.write(angle=angle)


class RotationPolicy(object):
    """把"锁定状态 + 上次角度"变成可测试的决策逻辑。"""

    def __init__(self, store, default_locked=False, logger=None):
        self.store = store
        self.default_locked = bool(default_locked)
        self.log = logger or (lambda message: None)
        self.locked = self.default_locked
        self.current_angle = None      # 屏幕上当前生效的角度
        self._persisted = None         # 已写入文件的角度（避免重复写盘）
        self._signature = None

    # -- 启动 / 轮询 -------------------------------------------------------
    def restore(self):
        """启动时读一次状态，返回 (locked, angle_or_None)。"""
        locked, angle = self.store.read()
        self.locked = self.default_locked if locked is None else bool(locked)
        self.current_angle = angle
        self._persisted = angle
        self._signature = self.store.signature()
        return (self.locked, self.current_angle)

    def poll(self):
        """检测状态文件变化；返回 'lock'、'unlock' 或 None。"""
        signature = self.store.signature()
        if signature == self._signature:
            return None
        self._signature = signature
        locked, angle = self.store.read()
        if angle is not None:
            self.current_angle = angle
        desired = self.locked if locked is None else bool(locked)
        if desired == self.locked:
            return None
        self.locked = desired
        return "lock" if desired else "unlock"

    # -- 决策 -------------------------------------------------------------
    def target_for(self, posture, degrees):
        """收到新姿态时应该下发的角度；None 表示保持现状。"""
        if degrees is None:
            return None
        if self.locked:
            return None
        return degrees

    def unlock_target(self, posture, degrees):
        """刚刚解锁时要不要立即纠正。"""
        if self.locked or degrees is None:
            return None
        return degrees

    def commit(self, degrees):
        """记录并持久化"当前生效角度"，返回是否真的写了文件。"""
        normalized = normalize_angle(degrees)
        if normalized is None:
            return False
        self.current_angle = normalized
        if self._persisted == normalized:
            return False
        try:
            self.store.set_angle(normalized)
        except (IOError, OSError) as exc:
            self.log("保存屏幕角度失败：%s" % exc)
            return False
        self._persisted = normalized
        return True

    def set_locked(self, locked):
        """主动设置锁定状态（状态栏图标/命令行用），返回是否变化。"""
        locked = bool(locked)
        if locked == self.locked:
            return False
        self.locked = locked
        try:
            self.store.set_locked(locked)
        except (IOError, OSError) as exc:
            self.log("保存旋转锁定状态失败：%s" % exc)
        self._signature = self.store.signature()
        return True

    def status_text(self):
        if self.locked:
            return "已锁定（保持当前方向）"
        return "跟随重力"
