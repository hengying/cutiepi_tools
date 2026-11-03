"""小工具（跨平台占位）。

程序只跑在树莓派上，但开发机可能是 Windows；
这里把只有 Linux 才有的接口包一层，方便在电脑上跑测试和 --help / diagnose。
"""

import os


def uid():
    """当前用户 uid；非 POSIX 平台返回 0。"""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return 0
    return getuid()


def runtime_dir():
    """XDG_RUNTIME_DIR，没有就用 /run/user/<uid>；都不存在时返回 None。"""
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value and os.path.isdir(value):
        return value
    guess = "/run/user/%d" % uid()
    if os.path.isdir(guess):
        return guess
    return None
