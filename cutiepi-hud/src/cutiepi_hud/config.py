"""配置读取。

优先级（后面的覆盖前面的）：

    内置默认值
    /etc/cutiepi-hud.conf                 系统默认（install.sh 安装）
    ~/.config/cutiepi-hud/config.ini      个人覆盖（calibrate 会写这里）

所有键都有内置默认值，配置文件缺项/写错不会导致启动失败。
"""

import configparser
import os

SYSTEM_CONF = "/etc/cutiepi-hud.conf"


def user_conf_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "cutiepi-hud", "config.ini")


# (section, key) -> (默认值, 类型)
DEFAULTS = {
    ("display", "output"): ("auto", str),
    ("display", "backend"): ("auto", str),
    # CutiePi 实测：不需要 swap_lr（角度值本身就是标定出来的真实 transform）
    ("display", "swap_lr"): (False, bool),
    ("display", "touch_matrix"): (True, bool),
    ("orientation", "enabled"): (True, bool),
    ("orientation", "poll_ms"): (250, int),
    ("orientation", "threshold"): (0.35, float),
    ("orientation", "flat_threshold"): (0.75, float),
    ("orientation", "confirm"): (3, int),
    ("orientation", "swap_xy"): (False, bool),
    ("orientation", "invert_x"): (False, bool),
    ("orientation", "invert_y"): (False, bool),
    ("orientation", "normal_axis"): ("y-", str),
    ("orientation", "left_up_axis"): ("x-", str),
    ("orientation", "bottom_up_axis"): ("y+", str),
    ("orientation", "right_up_axis"): ("x+", str),
    ("orientation", "device"): ("auto", str),
    # 角度含义：画面需要顺时针转多少度才能正过来。
    # 下面是 2026-11-03 在 CutiePi CM4 真机上标定的结果：
    #   竖屏(摄像头朝上)=180，倒竖屏=0，左边缘朝上=270，右边缘朝上=90
    ("orientation", "deg_normal"): (180, int),
    ("orientation", "deg_left_up"): (270, int),
    ("orientation", "deg_bottom_up"): (0, int),
    ("orientation", "deg_right_up"): (90, int),
    ("mcu", "port"): ("auto", str),
    ("mcu", "baud"): (115200, int),
    ("mcu", "smoothing"): (15, int),
    ("mcu", "shutdown_confirm"): (True, bool),
    ("mcu", "shutdown_dialog"): (True, bool),
    ("mcu", "shutdown_dialog_timeout"): (15, int),
    # 连按两下电源键直接弹关机框（比按住 6 秒快），窗口单位毫秒
    ("mcu", "double_press_dialog"): (True, bool),
    ("mcu", "double_press_ms"): (700, int),
    # 单按电源键做什么：toggle=息屏/亮屏（默认），dialog=直接弹关机框，none=什么都不做
    ("mcu", "short_press_action"): ("toggle", str),
    ("mcu", "screen_toggle"): (True, bool),
    ("hud", "enabled"): (True, bool),
    # tray = 显示在系统状态栏托盘里（推荐，默认）
    # overlay = 独立角标窗口（备用：面板没有托盘时）
    # both = 两个都开
    # off = 不显示
    ("hud", "mode"): ("tray", str),
    ("hud", "icon_size"): (32, int),
    # auto = 按 GTK 主题前景色；light = 浅色面板(画深色图标)；dark = 深色面板(画浅色图标)
    ("hud", "icon_color"): ("auto", str),
    # number = 图标就是电量数字（默认）；battery = 电池图形
    ("hud", "icon_style"): ("number", str),
    # 状态栏上是否显示亮度图标（点一下弹 1..15 的滑块）
    ("hud", "brightness_icon"): (True, bool),
    ("hud", "overlay"): ("/opt/cutiepi-hud/hud-overlay", str),
    ("hud", "margin_top"): (40, int),
    ("hud", "margin_right"): (8, int),
}

POSTURE_KEYS = {
    "normal": "normal_axis",
    "left-up": "left_up_axis",
    "bottom-up": "bottom_up_axis",
    "right-up": "right_up_axis",
}

POSTURE_DEG_KEYS = {
    "normal": "deg_normal",
    "left-up": "deg_left_up",
    "bottom-up": "deg_bottom_up",
    "right-up": "deg_right_up",
}

_TRUE = ("1", "true", "yes", "on", "y")
_FALSE = ("0", "false", "no", "off", "n")


def _convert(value, kind):
    if kind is bool:
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError("不是布尔值: %r" % value)
    if kind is int:
        return int(float(str(value).strip()))
    if kind is float:
        return float(str(value).strip())
    return str(value).strip()


class Config(object):
    """带类型的配置对象。用 cfg.get('display', 'swap_lr') 或便捷属性访问。"""

    def __init__(self, values=None, sources=None, warnings=None):
        self._values = {}
        for (section, key), (default, _kind) in DEFAULTS.items():
            self._values[(section, key)] = default
        if values:
            self._values.update(values)
        self.sources = sources or []
        self.warnings = warnings or []

    # -- 通用取值 ---------------------------------------------------------
    def get(self, section, key):
        return self._values.get((section, key))

    def as_bool(self, section, key):
        return bool(self.get(section, key))

    def as_int(self, section, key):
        return int(self.get(section, key))

    def as_float(self, section, key):
        return float(self.get(section, key))

    # -- 语义化快捷方式 ---------------------------------------------------
    @property
    def orientation_mapping(self):
        return {
            str(self.get("orientation", POSTURE_KEYS[posture])).strip(): posture
            for posture in POSTURE_KEYS
        }

    @property
    def posture_degrees(self):
        return {
            posture: self.as_int("orientation", POSTURE_DEG_KEYS[posture])
            for posture in POSTURE_DEG_KEYS
        }

    def degree_for(self, posture):
        key = POSTURE_DEG_KEYS.get(posture)
        if not key:
            return None
        return self.as_int("orientation", key)

    def describe(self):
        lines = []
        for section in ("display", "orientation", "mcu", "hud"):
            for (sec, key), (_default, _kind) in sorted(DEFAULTS.items()):
                if sec == section:
                    lines.append("%-14s %-16s = %s" % (section, key, self.get(sec, key)))
        return "\n".join(lines)


def load_config(system_path=SYSTEM_CONF, user_path=None, verbose=False):
    """读取系统配置 + 用户配置，返回 Config。"""
    user_path = user_path if user_path is not None else user_conf_path()
    values = {}
    sources = []
    warnings = []
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str

    for path in (system_path, user_path):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r") as handle:
                parser.read_file(handle)
            sources.append(path)
        except (configparser.Error, IOError, OSError) as exc:
            warnings.append("读取配置 %s 失败：%s" % (path, exc))

    for section in parser.sections():
        for key, value in parser.items(section):
            section = section.strip().lower()
            key = key.strip().lower()
            spec = DEFAULTS.get((section, key))
            if spec is None:
                if verbose:
                    warnings.append("忽略未知配置项 %s/%s" % (section, key))
                continue
            try:
                values[(section, key)] = _convert(value, spec[1])
            except (ValueError, TypeError) as exc:
                warnings.append("配置项 %s/%s 无效（%s），使用默认值" % (section, key, exc))

    return Config(values, sources, warnings)


def write_user_overrides(overrides, user_path=None):
    """把 {section: {key: value}} 写进用户配置文件（保留其它已有项）。"""
    user_path = user_path if user_path is not None else user_conf_path()
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if os.path.exists(user_path):
        try:
            with open(user_path, "r") as handle:
                parser.read_file(handle)
        except (configparser.Error, IOError, OSError):
            pass

    for section, items in overrides.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in items.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            parser.set(section, key, str(value))

    directory = os.path.dirname(user_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(user_path, "w") as handle:
        parser.write(handle)
    return user_path
