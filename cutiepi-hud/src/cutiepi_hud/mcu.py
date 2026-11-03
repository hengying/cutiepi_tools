"""CutiePi MCU 串口协议。

协议来源：002_资料/cutiepi-middleware-master/README.md
报文格式（7 字节起）：

    | 0x5A | 0xA5 | msg type | len_lo | len_hi | payload ... | crc |

* 长度字段为小端 16 位；
* crc = 除 crc 自身以外所有字节之和的低 8 位；
* 电量事件的 payload 是 2 字节小端整数，单位 mV，每秒上报一次；
* 充电事件收到后必须回 ACK，否则 MCU 会一直重发。

本模块只做纯逻辑（不打开串口），方便在电脑上跑单元测试。
"""

from collections import deque

HEADER0 = 0x5A
HEADER1 = 0xA5

# --- 消息类型 ---
MSG_BUTTON = 1
MSG_BATTERY = 2
MSG_CHARGE = 3
MSG_VERSION = 4
MSG_CUTOFF = 5

# --- 充电状态 ---
CHARGE_ON = 4
CHARGE_OFF = 5

# --- 按键事件 ---
BUTTON_SHORT = 1          # 短按：适合用来开关背光
BUTTON_POWER_ON = 2       # 开机（3 秒长按）
BUTTON_POWER_OFF = 3      # 关机请求（6 秒长按），需要 Pi 回 0xF3 / 0xFC

# --- 需要回给 MCU 的 ACK ---
ACK_BUTTON = 0xF1
ACK_SHUTDOWN = 0xF3
ACK_CANCEL_SHUTDOWN = 0xFC
ACK_CHARGE_ON = 0xF4
ACK_CHARGE_OFF = 0xF5
ACK_VERSION = 0xF6
ACK_CUTOFF = 0xF7

MAX_PAYLOAD = 64


def build_frame(msg_type, payload=b""):
    """按协议打包一帧。"""
    payload = bytes(payload)
    body = bytes(
        (
            HEADER0,
            HEADER1,
            msg_type & 0xFF,
            len(payload) & 0xFF,
            (len(payload) >> 8) & 0xFF,
        )
    ) + payload
    return body + bytes((sum(body) & 0xFF,))


def ack_frame(ack_value):
    """1 字节 payload 的简单应答帧（0xF1 / 0xF3 / 0xFC / 0xF4 / 0xF5 / 0xF6 / 0xF7）。"""
    return build_frame(1, bytes((ack_value & 0xFF,)))


def cutoff_frame():
    """关机时通知 MCU 切断电池供电（对应 002_资料 里的 cutoff 程序）。"""
    return build_frame(MSG_CUTOFF, bytes((ACK_CUTOFF,)))


class Frame(object):
    """一帧解析结果。"""

    __slots__ = ("msg_type", "payload")

    def __init__(self, msg_type, payload):
        self.msg_type = int(msg_type)
        self.payload = bytes(payload)

    @property
    def value(self):
        """payload 按小端拼成的整数（电量 mV、充电状态、按键值都走这里）。"""
        result = 0
        for index, byte in enumerate(self.payload):
            result |= byte << (8 * index)
        return result

    def __repr__(self):  # pragma: no cover - 仅调试用
        return "Frame(type=%d, payload=%s, value=%d)" % (
            self.msg_type,
            self.payload.hex(),
            self.value,
        )


class FrameParser(object):
    """把串口字节流切成合法报文，容忍噪声、半包和校验错。"""

    def __init__(self, max_payload=MAX_PAYLOAD):
        self._buf = bytearray()
        self._max_payload = max_payload

    def reset(self):
        self._buf.clear()

    def feed(self, data):
        """喂入字节，返回本次能完整解析出来的 Frame 列表。"""
        frames = []
        self._buf.extend(data)

        while True:
            start = self._buf.find(bytes((HEADER0, HEADER1)))
            if start < 0:
                # 没有帧头：只保留可能是半个帧头的最后一个字节
                if self._buf and self._buf[-1] == HEADER0:
                    del self._buf[: len(self._buf) - 1]
                else:
                    del self._buf[:]
                break
            if start > 0:
                del self._buf[:start]

            if len(self._buf) < 5:
                break

            length = self._buf[3] | (self._buf[4] << 8)
            if length > self._max_payload:
                # 长度不合理，丢掉这个帧头继续找下一个
                del self._buf[0]
                continue

            total = 5 + length + 1
            if len(self._buf) < total:
                break

            body = bytes(self._buf[: 5 + length])
            checksum = self._buf[total - 1]
            if (sum(body) & 0xFF) != checksum:
                del self._buf[0]
                continue

            frames.append(Frame(body[2], body[5:]))
            del self._buf[:total]

        return frames


# ---------------------------------------------------------------------------
# 电量：mV -> 百分比
# ---------------------------------------------------------------------------
# 曲线取自官方 cutiepi-shell（opt/cutiepi-shell/shell.qml 里的 batteryAttributes），
# 保持与官方固件一致的读数，避免用户觉得“和以前不一样”。
BATTERY_CURVE = (
    (3.00, 0),
    (3.48, 5),
    (3.49, 10),
    (3.51, 15),
    (3.53, 20),
    (3.55, 25),
    (3.56, 30),
    (3.59, 35),
    (3.60, 40),
    (3.61, 45),
    (3.63, 50),
    (3.65, 55),
    (3.67, 60),
    (3.69, 65),
    (3.73, 70),
    (3.76, 75),
    (3.80, 80),
    (3.82, 85),
    (3.86, 90),
    (3.88, 95),
    (3.89, 100),
)

MIN_VALID_MV = 2000  # 低于这个值基本是没插电池 / 读到的噪声


def mv_to_percent(mv):
    """把毫伏换算成 0~100 的百分比（曲线内线性插值）。"""
    if mv is None or mv < MIN_VALID_MV:
        return None
    volts = mv / 1000.0
    if volts <= BATTERY_CURVE[0][0]:
        return 0
    if volts >= BATTERY_CURVE[-1][0]:
        return 100
    for (v0, p0), (v1, p1) in zip(BATTERY_CURVE, BATTERY_CURVE[1:]):
        if volts <= v1:
            if v1 == v0:
                return int(p1)
            percent = p0 + (p1 - p0) * (volts - v0) / (v1 - v0)
            return int(round(percent))
    return 100


class BatteryFilter(object):
    """电压滑动平均，抑制 ADC 抖动（对应官方 shell 的 15 点均值）。"""

    def __init__(self, samples=15):
        self._samples = max(1, int(samples))
        self._window = deque(maxlen=self._samples)

    def reset(self):
        self._window.clear()

    def feed(self, mv):
        """喂入一个毫伏采样，返回平滑后的毫伏值（无效值直接返回 None）。"""
        if mv is None or mv < MIN_VALID_MV:
            return None
        self._window.append(int(mv))
        return int(round(sum(self._window) / float(len(self._window))))

    def percent(self, mv=None):
        """返回平滑后的百分比；没有数据时返回 None。"""
        if mv is not None:
            smoothed = self.feed(mv)
        else:
            if not self._window:
                return None
            smoothed = int(round(sum(self._window) / float(len(self._window))))
        return mv_to_percent(smoothed)
