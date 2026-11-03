"""串口收发（CTypes 无关，纯 os + termios，避免依赖 pyserial）。

MCU 挂在 ttyS0（mini UART，GPIO14/15）。不同镜像上可能叫 serial0 / ttyAMA1，
所以提供端口自动探测：打开每个候选口，听 3 秒，谁能解析出合法帧就用谁。
"""

import glob
import os
import select
import time

from . import mcu

CANDIDATE_PORTS = (
    "/dev/serial0",
    "/dev/ttyS0",
    "/dev/ttyAMA1",
    "/dev/ttyAMA0",
    "/dev/ttyAMA10",
)

BAUD_CONSTANTS = {}


def _load_termios():
    try:
        import termios  # noqa: F401  (仅 Linux 有)
    except ImportError:  # pragma: no cover - 只在开发机 (Windows) 触发
        return None
    import termios as _termios

    for name in dir(_termios):
        if name.startswith("B") and name[1:].isdigit():
            BAUD_CONSTANTS[int(name[1:])] = getattr(_termios, name)
    return _termios


_TERMIOS = _load_termios()


def candidate_ports():
    """返回可能存在的串口列表（含 /dev/serial* 软链）。"""
    ports = []
    for path in CANDIDATE_PORTS:
        if os.path.exists(path) and path not in ports:
            ports.append(path)
    for path in sorted(glob.glob("/dev/serial*")):
        if path not in ports:
            ports.append(path)
    return ports


def open_port(path, baud=115200):
    """打开并配置串口，返回 fd；失败抛 OSError。"""
    if _TERMIOS is None:
        raise OSError("当前平台没有 termios，无法打开串口")
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = _TERMIOS.tcgetattr(fd)
        attrs[0] = 0  # iflag: 关掉软件流控 / CR 转换
        attrs[1] = 0  # oflag
        attrs[2] = _TERMIOS.CS8 | _TERMIOS.CREAD | _TERMIOS.CLOCAL
        if hasattr(_TERMIOS, "CRTSCTS"):
            attrs[2] &= ~_TERMIOS.CRTSCTS
        attrs[3] = 0  # lflag: raw
        speed = BAUD_CONSTANTS.get(int(baud))
        if speed is None:
            raise OSError("不支持的波特率: %s" % baud)
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][_TERMIOS.VMIN] = 0
        attrs[6][_TERMIOS.VTIME] = 0
        _TERMIOS.tcsetattr(fd, _TERMIOS.TCSANOW, attrs)
        _TERMIOS.tcflush(fd, _TERMIOS.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


def write_frame(fd, frame):
    try:
        os.write(fd, frame)
        return True
    except OSError:
        return False


class SerialReader(object):
    """把串口字节流变成 Frame 序列（阻塞 100ms 级别，适合放独立线程）。"""

    def __init__(self, path, baud=115200, logger=None):
        self.path = path
        self.baud = int(baud)
        self.log = logger or (lambda message: None)
        self.fd = None
        self.parser = mcu.FrameParser()

    def open(self):
        self.fd = open_port(self.path, self.baud)
        self.parser.reset()
        return self

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()

    def read(self, timeout=0.2):
        """返回本次读到的 Frame 列表（可能为空）。"""
        if self.fd is None:
            return []
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return []
        if not ready:
            return []
        try:
            data = os.read(self.fd, 256)
        except OSError:
            return []
        if not data:
            return []
        return self.parser.feed(data)


def detect_port(listen_seconds=3.0, baud=115200, logger=None):
    """逐个候选口试探，返回 (path, 观察到的帧列表)。找不到返回 (None, [])。"""
    log = logger or (lambda message: None)
    for path in candidate_ports():
        try:
            reader = SerialReader(path, baud).open()
        except OSError as exc:
            log("  %s 打不开：%s" % (path, exc))
            continue
        frames = []
        # 打开后先发一次版本查询：MCU 会立刻回一帧，比干等被动上报快得多
        write_frame(reader.fd, mcu.ack_frame(mcu.ACK_VERSION))
        deadline = time.time() + float(listen_seconds)
        try:
            while time.time() < deadline and not frames:
                frames.extend(reader.read(timeout=0.2))
        finally:
            reader.close()
        if frames:
            log("  %s 收到合法帧：%s" % (path, ", ".join(sorted({str(f.msg_type) for f in frames}))))
            return path, frames
        log("  %s 3 秒内没有合法帧" % path)
    return None, []


def probe_ports(baud=115200):
    """诊断用：逐个候选端口说明权限和能否打开。

    返回 [(path, mode_int|None, gid|None, error_str)]
    """
    results = []
    for path in candidate_ports():
        try:
            stat_info = os.stat(path)
        except OSError as exc:
            results.append((path, None, None, str(exc)))
            continue
        mode = stat_info.st_mode & 0o777
        try:
            fd = open_port(path, baud)
        except OSError as exc:
            results.append((path, mode, stat_info.st_gid, str(exc)))
            continue
        try:
            os.close(fd)
        except OSError:
            pass
        results.append((path, mode, stat_info.st_gid, ""))
    return results
