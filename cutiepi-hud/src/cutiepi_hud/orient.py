"""根据重力加速度判断平板摆放姿态。

数据来自 MPU6050 的原始加速度（/sys/bus/iio/devices/iio:deviceX/in_accel_*_raw），
单位不重要：所有阈值都按“占合向量长度的比例”判断，因此不依赖 ROM 里的 scale。

各姿态用“重力落在哪根轴的正/负方向”来描述，例如 'y-'。
默认映射是猜测值，真机第一次使用请跑：

    cutiepi-hud calibrate

它会引导你把平板摆成 4 个姿态，直接把正确的映射写进用户配置。
"""

POSTURES = ("normal", "left-up", "bottom-up", "right-up")

AXIS_KEYS = ("x+", "x-", "y+", "y-", "z+", "z-")

# 默认猜测：竖直握持时重力沿 Y 负方向（与官方 cutiepi-shell 的起始状态一致）
DEFAULT_MAPPING = {
    "y+": "bottom-up",
    "y-": "normal",
    "x+": "right-up",
    "x-": "left-up",
}


def axis_key(x, y, z):
    """返回合向量落在哪根轴上，例如 'y-'。"""
    ax, ay, az = abs(x), abs(y), abs(z)
    if ax >= ay and ax >= az:
        return "x+" if x >= 0 else "x-"
    if ay >= az:
        return "y+" if y >= 0 else "y-"
    return "z+" if z >= 0 else "z-"


def magnitude(x, y, z):
    return (x * x + y * y + z * z) ** 0.5


def dominant_key(x, y, z):
    """合向量主轴；合向量太小（自由落体/没数据）时返回 None。"""
    mag = magnitude(x, y, z)
    if mag < 1e-6:
        return None, 0.0
    key = axis_key(x, y, z)
    if key.startswith("z"):
        share = abs(z) / mag
    elif key.startswith("x"):
        share = abs(x) / mag
    else:
        share = abs(y) / mag
    return key, share


class OrientationTracker(object):
    """把连续的加速度采样变成稳定的姿态事件。

    * threshold：主轴分量占合向量的最小比例，低于它认为“姿态不明显”，不切换；
    * flat_threshold：Z 轴占比超过它认为平板是平放（朝上/朝下），保持原姿态；
    * confirm：需要连续多少次采样一致才切换，避免抖动。
    """

    def __init__(
        self,
        mapping=None,
        threshold=0.35,
        flat_threshold=0.75,
        confirm=3,
    ):
        self.mapping = dict(DEFAULT_MAPPING if mapping is None else mapping)
        self.threshold = float(threshold)
        self.flat_threshold = float(flat_threshold)
        self.confirm = max(1, int(confirm))
        self._posture = None
        self._candidate = None
        self._count = 0

    # -- 状态查询 ---------------------------------------------------------
    @property
    def posture(self):
        return self._posture

    def set_posture(self, posture):
        """直接设定当前姿态（例如从配置文件恢复上次的值）。"""
        self._posture = posture if posture in POSTURES else None
        self._candidate = None
        self._count = 0

    # -- 主逻辑 -----------------------------------------------------------
    def update(self, x, y, z):
        """喂一个采样，返回新姿态字符串；没有变化/不明确时返回 None。"""
        mag = magnitude(x, y, z)
        if mag <= 1e-6:
            return None

        key = axis_key(x, y, z)
        share = max(abs(x), abs(y), abs(z)) / mag

        # 平放（屏幕朝上/朝下）时不做旋转，保持上一次的姿态
        if key.startswith("z") and share >= self.flat_threshold:
            self._candidate = None
            self._count = 0
            return None

        if share < self.threshold:
            self._candidate = None
            self._count = 0
            return None

        posture = self.mapping.get(key)
        if posture is None:
            return None

        if posture == self._posture:
            self._candidate = None
            self._count = 0
            return None

        if posture == self._candidate:
            self._count += 1
        else:
            self._candidate = posture
            self._count = 1

        if self._count >= self.confirm:
            self._posture = posture
            self._candidate = None
            self._count = 0
            return posture
        return None

    def describe(self, x, y, z):
        """调试用：给出当前采样 + 判定解释。"""
        key, share = dominant_key(x, y, z)
        return {
            "x": x,
            "y": y,
            "z": z,
            "magnitude": magnitude(x, y, z),
            "axis": key,
            "share": share,
            "posture": self.mapping.get(key) if key else None,
        }


def mapping_from_vectors(vectors):
    """由校准得到的 4 个重力向量生成 轴 -> 姿态 映射。

    vectors: {'normal': (x, y, z), 'left-up': ..., 'bottom-up': ..., 'right-up': ...}

    返回 (mapping, error)。error 非空表示校准结果不可用（例如两个姿态落在同一根轴上）。
    """
    mapping = {}
    for posture, vector in vectors.items():
        if posture not in POSTURES:
            return None, "未知姿态: %s" % posture
        key = axis_key(*vector)
        if key.startswith("z"):
            return None, "姿态 %s 的重力落在 Z 轴，说明采样时平板是平放的，请竖直握持后重试" % posture
        if key in mapping:
            return None, (
                "姿态 %s 与 %s 都落在 %s 轴上（加速度计可能装反或采样失败），"
                "请确认每个姿态都真的转过 90 度后重试" % (posture, mapping[key], key)
            )
        mapping[key] = posture

    missing = [p for p in POSTURES if p not in mapping.values()]
    if missing:
        return None, "缺少姿态: %s" % ", ".join(missing)
    return mapping, None
