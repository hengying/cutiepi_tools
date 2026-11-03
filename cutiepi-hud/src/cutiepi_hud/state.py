"""运行时状态文件。

守护进程把状态写成一个很小的 key=value 文件，C 叠加层每秒读一次。
这样 Python 和叠加层之间不需要任何 IPC 复杂度，叠加层挂掉也不会拖垮守护进程。
"""

import os
import tempfile

RUNTIME_KEYS = (
    "battery_percent",
    "charging",
    "battery_present",
    "battery_mv",
    "posture",
    "rotation",
    "mcu_port",
    "mcu_ok",
)


def default_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "cutiepi-hud", "state")
    return os.path.join(tempfile.gettempdir(), "cutiepi-hud-state")


class StateFile(object):
    def __init__(self, path=None):
        self.path = path or default_path()
        self._last = None

    def _ensure_dir(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory, mode=0o700)
            except OSError:
                pass

    def format(self, values):
        lines = []
        for key in RUNTIME_KEYS:
            value = values.get(key)
            if value is None:
                continue
            if isinstance(value, bool):
                value = 1 if value else 0
            lines.append("%s=%s" % (key, value))
        return "\n".join(lines) + "\n"

    def write(self, values, force=False):
        """原子写入；内容没变化时跳过（除非 force）。"""
        text = self.format(values)
        if not force and text == self._last:
            return False
        self._ensure_dir()
        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        self._last = text
        return True

    def read(self):
        try:
            with open(self.path, "r") as handle:
                raw = handle.read()
        except (IOError, OSError):
            return {}
        values = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values
