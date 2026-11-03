"""守护进程主体：串口线程 + 姿态线程 + 状态文件 + 叠加层管理。"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import backlight, iio, mcu, orient, rotate, serialport, util
from . import session as session_module
from .overlay import OverlayProcess
from .brightness_tray import BrightnessTrayProcess
from .state import StateFile, default_path
from .tray import TrayProvider

try:  # 只在 Linux 上可用；Windows 上跑单元测试时不会走到这里
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class RuntimeState(object):
    """线程间共享的运行时状态。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = {
            "battery_percent": -1,
            "battery_mv": None,
            "battery_present": False,
            "charging": False,
            "posture": None,
            "rotation": None,
            "mcu_port": None,
            "mcu_ok": False,
        }

    def update(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                if key in self.values:
                    self.values[key] = value

    def snapshot(self):
        with self.lock:
            return dict(self.values)


class SingleInstanceLock(object):
    """防止 autostart 与 systemd 两条路径同时起来两个实例抢串口。"""

    def __init__(self, path=None):
        runtime = util.runtime_dir() or "/tmp"
        self.path = path or os.path.join(runtime, "cutiepi-hud", "lock")
        self._handle = None

    def acquire(self):
        if fcntl is None:
            return True
        try:
            directory = os.path.dirname(self.path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, mode=0o700)
            self._handle = open(self.path, "w")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle.write(str(os.getpid()))
            self._handle.flush()
            return True
        except (IOError, OSError):
            return False

    def release(self):
        if self._handle is not None:
            try:
                self._handle.close()
            except (IOError, OSError):
                pass
            self._handle = None


# ---------------------------------------------------------------------------
# MCU 串口线程
# ---------------------------------------------------------------------------
class McuWorker(threading.Thread):
    """读 MCU 上报的电量/充电/按键，并把需要的 ACK 写回去。"""

    def __init__(self, config, runtime, stop_event, logger=None):
        super(McuWorker, self).__init__(name="mcu", daemon=True)
        self.cfg = config
        self.runtime = runtime
        self.stop = stop_event
        self.log = logger or logging.info
        self.filter = mcu.BatteryFilter(config.as_int("mcu", "smoothing"))
        self._detect_warned = False
        self._screen_off = False
        self._dialog_open = False
        self._shutdown_lock = threading.Lock()
        self._powered_off_requested = False
        self._ignore_warned = False
        self._last_button_value = None
        self._last_button_time = 0.0
        self._last_short_time = None
        self._last_charging = None

    # -- 线程主体 ---------------------------------------------------------
    def run(self):
        baud = self.cfg.as_int("mcu", "baud")
        configured = self.cfg.get("mcu", "port")
        while not self.stop.is_set():
            port = configured
            if not port or str(port).lower() == "auto":
                self.log("正在探测 MCU 串口（3 秒）…")
                port, frames = serialport.detect_port(3.0, baud, logger=self.log)
                if not port:
                    if not self._detect_warned:
                        self.log(
                            "没找到 MCU 串口。请确认 /boot/firmware/cmdline.txt 里没有 "
                            "console=serial0，并且用户已在 dialout 组里。"
                        )
                        self._detect_warned = True
                    self.stop.wait(10)
                    continue
            self._session(port, baud)
            if not self.stop.is_set():
                self.stop.wait(3)

    def _session(self, port, baud):
        try:
            reader = serialport.SerialReader(port, baud, self.log).open()
        except OSError as exc:
            self.log("打开串口 %s 失败：%s" % (port, exc))
            return
        self.log("已打开 MCU 串口 %s @ %d" % (port, baud))
        self.runtime.update(mcu_port=port, mcu_ok=True)
        self.filter.reset()
        # 主动要一次固件版本：既确认链路是双向通的，也能在日志里留下 MCU 版本
        serialport.write_frame(reader.fd, mcu.ack_frame(mcu.ACK_VERSION))
        last_frame_at = time.time()
        try:
            while not self.stop.is_set():
                frames = reader.read(0.25)
                if frames:
                    last_frame_at = time.time()
                    for frame in frames:
                        self._handle_frame(frame, reader)
                elif time.time() - last_frame_at > 30:
                    self.log("串口 30 秒没有数据，重新打开")
                    return
        finally:
            self.runtime.update(mcu_ok=False)
            reader.close()

    # -- 单帧处理 ---------------------------------------------------------
    # 注意：方法名不能叫 _handle —— threading.Thread 自己有个 _handle 属性，
    # 会被覆盖掉，第一帧数据到达时就会 TypeError。（已踩过这个坑）
    def _handle_frame(self, frame, reader):
        if frame.msg_type == mcu.MSG_BATTERY:
            mv = frame.value
            smoothed = self.filter.feed(mv)
            percent = mcu.mv_to_percent(smoothed)
            self.runtime.update(
                battery_mv=mv,
                battery_percent=-1 if percent is None else percent,
                battery_present=percent is not None,
            )
            return

        if frame.msg_type == mcu.MSG_CHARGE:
            charging = frame.value == mcu.CHARGE_ON
            self.runtime.update(charging=charging)
            ack = mcu.ACK_CHARGE_ON if charging else mcu.ACK_CHARGE_OFF
            serialport.write_frame(reader.fd, mcu.ack_frame(ack))
            # MCU 每秒都发一次；只有状态变化才写日志，避免日志越滚越大
            if charging != self._last_charging:
                self._last_charging = charging
                self.log("充电状态：%s" % ("充电中" if charging else "未充电"))
            return

        if frame.msg_type == mcu.MSG_BUTTON:
            self._handle_button(frame.value, reader)
            return

        if frame.msg_type == mcu.MSG_VERSION:
            self.log("MCU 版本：%s" % frame.payload.decode("ascii", "replace"))
            return

    def _handle_button(self, value, reader):
        # MCU 在按住期间会反复发同一个值，这里做个限流，避免日志被刷爆
        now = time.time()
        if value != self._last_button_value or now - self._last_button_time > 1.0:
            self.log("按键事件：%d" % value)
        self._last_button_value = value
        self._last_button_time = now

        if value == mcu.BUTTON_SHORT:
            serialport.write_frame(reader.fd, mcu.ack_frame(mcu.ACK_BUTTON))
            # 短按说明用户又操作了一次，允许下一次长按重新触发关机流程
            self._powered_off_requested = False

            # 连按两下 -> 直接弹关机确认框（比按住 6 秒快得多）
            window = self.cfg.as_int("mcu", "double_press_ms") / 1000.0
            is_double = (
                self._last_short_time is not None and (now - self._last_short_time) <= window
            )
            self._last_short_time = now

            action = self.cfg.get("mcu", "short_press_action")
            if action == "dialog":
                # 单按即弹关机框（这时"双击"就没意义了）
                self.log("短按电源键 -> 弹出关机确认框")
                self._ask_shutdown()
                return

            if is_double and self.cfg.as_bool("mcu", "double_press_dialog"):
                self._last_short_time = None
                self.log("连按两下电源键，弹出关机确认框")
                self._ask_shutdown()
                return

            if action != "none" and self.cfg.as_bool("mcu", "screen_toggle"):
                self._toggle_screen()
            return

        if value == mcu.BUTTON_POWER_OFF:
            self._request_shutdown(reader)
            return

    def _ask_shutdown(self):
        """弹出关机确认框（同一时间只允许一个）。"""
        with self._shutdown_lock:
            if self._dialog_open:
                return False
            self._dialog_open = True
        self.log("弹出关机确认框")
        thread = threading.Thread(
            target=self._confirm_then_poweroff, name="shutdown-dialog", daemon=True
        )
        thread.start()
        return True

    def _toggle_screen(self):
        # 读真实状态再取反，避免内部标志和实际背光不一致（比如别处把屏幕点亮了）
        current = backlight.get_power()
        if current is None:
            current = not self._screen_off
        target = not current
        if backlight.set_power(target):
            self._screen_off = not target
            self.log("背光已%s" % ("打开" if target else "关闭"))
        else:
            self.log("背光控制不可用（缺少 udev 规则或权限），已忽略短按")

    def _request_shutdown(self, reader):
        """6 秒长按：先回"取消"避免 MCU 自己切电，再弹框确认或直接关机。

        注意：MCU 在按住期间会**反复**发这个请求（每几十毫秒一次），
        所以这里必须保证"每次按压只处理一次"，否则第二个事件会把对话框
        顶掉直接关机（真机上踩过）。
        """
        # 每次请求都回 FC（取消），把决定权留在 Pi 这边
        serialport.write_frame(reader.fd, mcu.ack_frame(mcu.ACK_CANCEL_SHUTDOWN))

        with self._shutdown_lock:
            if self._dialog_open:
                return  # 已经弹着确认框了，重复请求忽略
            if not self.cfg.as_bool("mcu", "shutdown_confirm"):
                if not self._ignore_warned:
                    self.log("收到 6 秒长按关机请求，但 shutdown_confirm=false，已忽略")
                    self._ignore_warned = True
                return
            if self._powered_off_requested:
                return  # 本次按压已经处理过

            if self.cfg.as_bool("mcu", "shutdown_dialog"):
                # 先同步占位，避免连续事件各弹一个框
                self._dialog_open = True
                spawn_dialog = True
            else:
                self._powered_off_requested = True
                spawn_dialog = False

        if spawn_dialog:
            self.log("收到 6 秒长按关机请求，弹出确认对话框")
            threading.Thread(
                target=self._confirm_then_poweroff, name="shutdown-dialog", daemon=True
            ).start()
            return

        self.log("收到 6 秒长按关机请求，正在关机…")
        code = self._poweroff()
        if code == 0:
            self.log("已执行关机命令")
        else:
            self.log("关机命令执行失败（code=%s），请检查 sudoers 规则" % code)

    def _confirm_then_poweroff(self):
        """弹确认框（独立进程），确认后才关机。"""
        code = 1
        timeout = self.cfg.as_int("mcu", "shutdown_dialog_timeout")
        try:
            # 息屏时长按也要能看到对话框
            if backlight.get_power() is False:
                backlight.set_power(True)
            # 用脚本路径启动（不依赖 PYTHONPATH），对话框进程只用到 gi/gtk
            dialog_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialog.py")
            command = [
                sys.executable,
                dialog_script,
                "shutdown",
                "--timeout",
                str(timeout),
            ]
            self.log("启动关机确认框：%s" % " ".join(command))
            try:
                code = subprocess.call(command, env=dict(os.environ))
                self.log("确认框退出码：%s" % code)
            except OSError as exc:
                # 弹不出来就什么都不做（绝对不能未经确认就断电）
                self.log("对话框启动失败：%s，已取消本次关机" % exc)
                code = 1
        finally:
            with self._shutdown_lock:
                self._dialog_open = False

        if code == 0:
            with self._shutdown_lock:
                self._powered_off_requested = True
            self.log("用户确认关机，正在关机…")
            rc = self._poweroff()
            if rc != 0:
                self.log("关机命令执行失败（code=%s）" % rc)
        else:
            # 取消/超时：允许下一次长按或双击重新弹框
            with self._shutdown_lock:
                self._powered_off_requested = False
            self.log("本次关机已取消（未确认或对话框异常）")

    @staticmethod
    def _poweroff():
        commands = (
            ["sudo", "-n", "systemctl", "poweroff"],
            ["systemctl", "poweroff"],
        )
        for command in commands:
            try:
                return subprocess.call(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError:
                continue
        return 1


# ---------------------------------------------------------------------------
# 姿态 / 旋转线程
# ---------------------------------------------------------------------------
class OrientationWorker(threading.Thread):
    def __init__(self, config, runtime, stop_event, logger=None, dry_run=False):
        super(OrientationWorker, self).__init__(name="orientation", daemon=True)
        self.cfg = config
        self.runtime = runtime
        self.stop = stop_event
        self.log = logger or logging.info
        self.dry_run = dry_run
        self.rotator = rotate.Rotator(
            output=config.get("display", "output"),
            backend=config.get("display", "backend"),
            swap_lr=config.as_bool("display", "swap_lr"),
            touch_matrix=config.as_bool("display", "touch_matrix"),
            dry_run=dry_run,
        )
        self.tracker = orient.OrientationTracker(
            mapping=config.orientation_mapping,
            threshold=config.as_float("orientation", "threshold"),
            flat_threshold=config.as_float("orientation", "flat_threshold"),
            confirm=config.as_int("orientation", "confirm"),
        )
        self._last_error = None

    def run(self):
        if not self.cfg.as_bool("orientation", "enabled"):
            self.log("自动旋转已在配置中关闭")
            return

        backend, output = self.rotator.detect()
        if backend:
            self.log("旋转后端：%s，输出：%s" % (backend, output))
        else:
            self.log(
                "暂时没有可用的旋转后端（%s）。Wayland 需要 wlr-randr，X11 需要 xrandr。"
                % (self.rotator.last_error or "未检测到显示服务器")
            )

        poll = max(0.05, self.cfg.as_int("orientation", "poll_ms") / 1000.0)
        accel = iio.IioAccelerometer(self.cfg.get("orientation", "device"))
        warned = False
        zero_streak = 0
        warned_zero = False
        started_at = time.time()
        baseline_applied = False

        while not self.stop.is_set():
            if not accel.available:
                if not warned:
                    self.log("没找到加速度计（IIO 设备）。检查 config.txt 里的 mpu6050-i2c5 叠加层")
                    warned = True
                self.stop.wait(5)
                accel = iio.IioAccelerometer(self.cfg.get("orientation", "device"))
                continue

            raw = accel.raw()
            if raw is None:
                self.stop.wait(poll)
                continue

            if raw == (0, 0, 0):
                zero_streak += 1
                if zero_streak == 20 and not warned_zero:
                    self.log(
                        "加速度计连续读到 0：内核驱动可能不支持直接读 sysfs，"
                        "请把 `cutiepi-hud diagnose` 输出发我（可临时用 iio-sensor-proxy 验证）"
                    )
                    warned_zero = True
            else:
                zero_streak = 0

            x, y, z = self._transform(*raw)
            posture = self.tracker.update(x, y, z)
            if posture:
                degrees = self.cfg.degree_for(posture)
                self.runtime.update(posture=posture, rotation=degrees)
                if degrees is None:
                    self.log("姿态 %s 没有配置角度，已跳过" % posture)
                elif backend is None:
                    backend, output = self.rotator.detect(force=True)
                    if backend is None:
                        self.log("姿态 %s 需要旋转，但没有可用的旋转后端" % posture)
                    else:
                        self._apply(posture, degrees)
                        baseline_applied = True
                else:
                    self._apply(posture, degrees)
                    baseline_applied = True
            elif not baseline_applied and time.time() - started_at > 3.0 and backend:
                # 开机时平板如果是平放的（重力在 Z 轴），判断不出横竖，
                # 这里按"竖屏"兜底，避免屏幕停在合成器默认方向（这台机器上是上下颠倒的）。
                degrees = self.cfg.degree_for("normal")
                if degrees is not None:
                    self.log("平板平放，先按竖屏方向兜底：%d°" % degrees)
                    self._apply("normal", degrees)
                baseline_applied = True

            self.stop.wait(poll)

    def _apply(self, posture, degrees):
        if self.rotator.apply(degrees):
            # 日志里把"实际下发的 transform"也打出来：swap_lr 会交换 90/270
            if self.rotator.backend == "wayland":
                actual = self.rotator.transform_wayland(degrees)
            else:
                actual = self.rotator.rotate_name_x11(degrees)
            self.log("姿态 %s -> 旋转 %d°（实际下发 %s）" % (posture, degrees, actual))
            self._last_error = None
        else:
            message = self.rotator.last_error or "未知错误"
            if message != self._last_error:
                self.log("旋转失败：%s" % message)
                self._last_error = message

    def _transform(self, x, y, z):
        cfg = self.cfg
        if cfg.as_bool("orientation", "swap_xy"):
            x, y = y, x
        if cfg.as_bool("orientation", "invert_x"):
            x = -x
        if cfg.as_bool("orientation", "invert_y"):
            y = -y
        return x, y, z


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run(config, args, logger=None):
    log = logger or logging.info

    lock = SingleInstanceLock(getattr(args, "lock_file", None))
    if not lock.acquire():
        log("已经有一个 cutiepi-hud 实例在运行，本次退出")
        return 0

    state_path = getattr(args, "state_file", None) or default_path()
    state_file = StateFile(state_path)
    runtime = RuntimeState()
    stop_event = threading.Event()
    # 先落一份状态文件：这样即使后面等桌面/托盘花时间，diagnose 也能看到内容
    state_file.write(runtime.snapshot(), force=True)

    def _on_signal(signum, _frame):
        log("收到信号 %d，准备退出" % signum)
        stop_event.set()

    for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, signame):
            try:
                signal.signal(getattr(signal, signame), _on_signal)
            except (ValueError, OSError):
                pass

    log("状态文件：%s" % state_path)

    # 先启动 MCU 线程：串口不依赖桌面，电量可以立刻开始采集
    workers = []
    if not getattr(args, "no_mcu", False):
        worker = McuWorker(config, runtime, stop_event, log)
        worker.start()
        workers.append(worker)

    info = session_module.ensure_display_env(
        timeout=getattr(args, "display_timeout", 90), logger=log
    )
    if info.get("backend") is None:
        log("没有找到桌面会话；如果这是开机自启，稍后会自动重试")

    # 电量显示：默认走系统状态栏托盘；overlay 只是面板没有托盘时的备用方案
    hud_mode = "off"
    if config.as_bool("hud", "enabled") and not getattr(args, "no_hud", False):
        hud_mode = (config.get("hud", "mode") or "tray").strip().lower()
    if hud_mode not in ("tray", "overlay", "both", "off"):
        log("未知的 hud/mode=%r，改用 tray" % hud_mode)
        hud_mode = "tray"
    log("电量显示方式：%s" % hud_mode)

    tray = None
    if hud_mode in ("tray", "both"):
        tray = TrayProvider(
            logger=log,
            icon_size=config.as_int("hud", "icon_size"),
            icon_color=config.get("hud", "icon_color"),
            icon_style=config.get("hud", "icon_style"),
        )
        tray.start()
        if tray.backend is None:
            log(
                "提示：当前桌面没有可用的托盘（%s）。如果任务栏有托盘图标区，"
                "请把结果发我；也可以用 hud/mode = overlay 先显示独立角标。" % tray.status
            )

    overlay = None
    if hud_mode in ("overlay", "both"):
        overlay = OverlayProcess(
            config.get("hud", "overlay"),
            state_path,
            margin_top=config.as_int("hud", "margin_top"),
            margin_right=config.as_int("hud", "margin_right"),
            logger=log,
        )
        overlay.ensure()

    # 状态栏亮度图标（独立进程：每个托盘图标要有自己的 SNI 连接）
    brightness_tray = None
    if config.as_bool("hud", "brightness_icon") and not getattr(args, "no_hud", False):
        brightness_tray = BrightnessTrayProcess(logger=log)
        brightness_tray.ensure()
        log("亮度图标：%s" % ("已启动" if brightness_tray.running else "稍后重试"))

    if not getattr(args, "no_rotate", False):
        worker = OrientationWorker(
            config, runtime, stop_event, log, dry_run=getattr(args, "dry_run", False)
        )
        worker.start()
        workers.append(worker)

    state_file.write(runtime.snapshot(), force=True)
    log("cutiepi-hud 已启动（Ctrl+C 退出）")

    try:
        while not stop_event.is_set():
            snapshot = runtime.snapshot()
            state_file.write(snapshot)
            if tray is not None:
                tray.update(
                    snapshot.get("battery_percent"),
                    snapshot.get("charging", False),
                    snapshot.get("battery_present", False),
                )
            if overlay is not None:
                overlay.ensure()
            if brightness_tray is not None:
                brightness_tray.ensure()
            stop_event.wait(1.0)
    finally:
        stop_event.set()
        if tray is not None:
            tray.stop()
        if overlay is not None:
            overlay.stop()
        if brightness_tray is not None:
            brightness_tray.stop()
        for worker in workers:
            worker.join(timeout=3)
        lock.release()
        log("cutiepi-hud 已退出")
    return 0
