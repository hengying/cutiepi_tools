"""背光控制（短按电源键息屏用）。

面板背光设备是 rpi_backlight（见 002_资料 里的 cutiepi-panel 设备树），
install.sh 会装一条 udev 规则把 bl_power 的写权限给 video 组。
"""

import glob
import os

BACKLIGHT_GLOB = "/sys/class/backlight/*"


def find_backlight():
    for path in sorted(glob.glob(BACKLIGHT_GLOB)):
        if os.path.exists(os.path.join(path, "bl_power")):
            return path
    return None


def _write(path, value):
    try:
        with open(path, "w") as handle:
            handle.write(str(value))
        return True
    except (IOError, OSError):
        return False


def set_power(on, path=None):
    """开/关背光，返回是否成功。"""
    path = path or find_backlight()
    if not path:
        return False
    return _write(os.path.join(path, "bl_power"), 0 if on else 1)


def get_power(path=None):
    path = path or find_backlight()
    if not path:
        return None
    try:
        with open(os.path.join(path, "bl_power"), "r") as handle:
            return handle.read().strip() == "0"
    except (IOError, OSError):
        return None


# ---------------------------------------------------------------------------
# 亮度：rpi_backlight 的 brightness 原生范围就是 1..15（max_brightness=15）
# ---------------------------------------------------------------------------
DEFAULT_MAX_LEVEL = 15


def _read_int(path):
    try:
        with open(path, "r") as handle:
            return int(handle.read().strip() or 0)
    except (IOError, OSError, ValueError):
        return None


def max_brightness(path=None):
    """背光最大值（这台机器是 15）。拿不到就返回默认值。"""
    path = path or find_backlight()
    if not path:
        return DEFAULT_MAX_LEVEL
    value = _read_int(os.path.join(path, "max_brightness"))
    return value if value and value > 0 else DEFAULT_MAX_LEVEL


def get_brightness(path=None):
    path = path or find_backlight()
    if not path:
        return None
    return _read_int(os.path.join(path, "brightness"))


def clamp_level(level, max_level=None):
    max_level = max_level or DEFAULT_MAX_LEVEL
    try:
        level = int(round(float(level)))
    except (TypeError, ValueError):
        return None
    return max(1, min(int(max_level), level))


def set_brightness(level, path=None, max_level=None):
    """设置亮度（1..max），返回是否成功。"""
    path = path or find_backlight()
    if not path:
        return False
    target = clamp_level(level, max_level or max_brightness(path))
    if target is None:
        return False
    return _write(os.path.join(path, "brightness"), target)


def adjust_brightness(delta, path=None):
    """在当前亮度上加减，返回 (旧值, 新值)。"""
    current = get_brightness(path) or 1
    target = clamp_level(current + delta, max_brightness(path))
    set_brightness(target, path)
    return current, target

