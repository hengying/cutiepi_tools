"""从 IIO sysfs 读取 MPU6050 加速度。

设备由 config.txt 里的 dtoverlay=mpu6050-i2c5 提供，正常情况下是
/sys/bus/iio/devices/iio:device0。这里不写死编号，按名字/通道自动找。
"""

import glob
import os
import re

IIO_ROOT = "/sys/bus/iio/devices"


def _read(path):
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return None


def _read_int(path):
    value = _read(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class IioAccelerometer(object):
    """按通道读取加速度；没找到设备时 available 为 False。"""

    def __init__(self, device="auto"):
        self.device = device or "auto"
        self.path = None
        self.name = None
        self._resolve()

    # -- 设备发现 ---------------------------------------------------------
    def _candidates(self):
        if self.device not in ("auto", "", None):
            if os.path.isdir(os.path.join(IIO_ROOT, self.device)):
                return [os.path.join(IIO_ROOT, self.device)]
            return []
        return sorted(glob.glob(os.path.join(IIO_ROOT, "iio:device*")))

    def _resolve(self):
        preferred = []
        others = []
        for path in self._candidates():
            if not os.path.exists(os.path.join(path, "in_accel_x_raw")):
                continue
            name = (_read(os.path.join(path, "name")) or "").lower()
            if "mpu" in name or "accel" in name:
                preferred.append((path, name))
            else:
                others.append((path, name))
        chosen = (preferred or others)
        if chosen:
            self.path, self.name = chosen[0]
        else:
            self.path, self.name = None, None

    @property
    def available(self):
        return self.path is not None

    @property
    def device_name(self):
        return os.path.basename(self.path) if self.path else None

    # -- 读数 -------------------------------------------------------------
    def raw(self):
        """返回 (x, y, z) 原始值；读不到返回 None。"""
        if not self.path:
            return None
        values = []
        for axis in ("x", "y", "z"):
            value = _read_int(os.path.join(self.path, "in_accel_%s_raw" % axis))
            if value is None:
                return None
            values.append(value)
        return tuple(values)

    def scale(self):
        """单轴 scale（g/LSB），仅用于日志展示。"""
        value = _read(os.path.join(self.path, "in_accel_scale"))
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None


def list_devices():
    """诊断用：列出所有 IIO 设备及其关键通道。"""
    result = []
    for path in sorted(glob.glob(os.path.join(IIO_ROOT, "iio:device*"))):
        info = {
            "path": path,
            "name": _read(os.path.join(path, "name")),
            "has_accel": os.path.exists(os.path.join(path, "in_accel_x_raw")),
            "accel_x_raw": _read(os.path.join(path, "in_accel_x_raw")),
            "mount_matrix": _read(os.path.join(path, "in_mount_matrix")),
        }
        result.append(info)
    return result


def parse_mount_matrix(text):
    """把 in_mount_matrix 文本解析成 9 个数字，失败返回 None。"""
    if not text:
        return None
    numbers = re.findall(r"-?\d+", text)
    if len(numbers) != 9:
        return None
    return [int(n) for n in numbers]
