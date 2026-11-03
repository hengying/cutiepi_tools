"""命令行入口。

    cutiepi-hud                  等价于 run
    cutiepi-hud run              启动守护进程（自动旋转 + 状态栏电池）
    cutiepi-hud monitor          只打印姿态和加速度，不做任何旋转（用来确认传感器）
    cutiepi-hud calibrate        引导校准 4 个姿态（第一次装机建议跑一次）
    cutiepi-hud test-rotate 90   直接测试某个旋转角度
    cutiepi-hud battery          只读一次电池信息
    cutiepi-hud tray-test        只测试状态栏电池图标（不接 MCU）
    cutiepi-hud demo             用假数据看状态栏图标
    cutiepi-hud diagnose         输出环境诊断报告
"""

import argparse
import logging
import os
import subprocess
import sys
import time

from . import __version__, config as config_module, iio, mcu, orient, session, util
from . import state as state_module
from . import serialport

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def _setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    return logging.info


def _load(args):
    cfg = config_module.load_config(user_path=getattr(args, "user_config", None))
    for warning in cfg.warnings:
        logging.warning(warning)
    return cfg


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args):
    from . import daemon

    cfg = _load(args)
    log = logging.info
    if not cfg.sources:
        log("没有找到配置文件，全部使用内置默认值")
    else:
        log("配置来源：%s" % ", ".join(cfg.sources))
    return daemon.run(cfg, args, logger=log)


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------
def cmd_monitor(args):
    cfg = _load(args)
    log = logging.info
    seconds = args.seconds or 0
    mapping = cfg.orientation_mapping
    tracker = orient.OrientationTracker(
        mapping=mapping,
        threshold=cfg.as_float("orientation", "threshold"),
        flat_threshold=cfg.as_float("orientation", "flat_threshold"),
        confirm=cfg.as_int("orientation", "confirm"),
    )
    accel = iio.IioAccelerometer(cfg.get("orientation", "device"))

    if not accel.available:
        log("没有找到 IIO 加速度计设备，检查 config.txt 里的 dtoverlay=mpu6050-i2c5")
        return 2

    log("加速度计：%s（%s）" % (accel.device_name, accel.name))
    log("轴 -> 姿态映射：%s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(mapping.items())))
    log("把平板绕着屏幕法线慢慢转一圈，观察下面的输出；Ctrl+C 结束")

    deadline = time.time() + seconds if seconds else None
    last_print = 0.0
    status = None
    zero_streak = 0
    try:
        while deadline is None or time.time() < deadline:
            raw = accel.raw()
            if raw is None:
                time.sleep(0.2)
                continue
            if raw == (0, 0, 0):
                zero_streak += 1
                if zero_streak == 10:
                    log(
                        "读数一直是 0：内核的 mpu6050 驱动可能不支持直接读 sysfs，"
                        "请把 diagnose 输出发我（见 README 6.3）"
                    )
            else:
                zero_streak = 0
            x, y, z = raw
            if cfg.as_bool("orientation", "swap_xy"):
                x, y = y, x
            if cfg.as_bool("orientation", "invert_x"):
                x = -x
            if cfg.as_bool("orientation", "invert_y"):
                y = -y
            new = tracker.update(x, y, z)
            if new:
                status = new
            info = tracker.describe(x, y, z)
            now = time.time()
            if now - last_print >= 0.5:
                last_print = now
                sys.stdout.write(
                    "x=%+8.1f y=%+8.1f z=%+8.1f |g|=%7.1f  主轴=%-2s 占比=%.2f  "
                    "即时=%-9s 稳定=%-9s 角度=%s\n"
                    % (
                        info["x"],
                        info["y"],
                        info["z"],
                        info["magnitude"],
                        info["axis"] or "--",
                        info["share"],
                        info["posture"] or "--",
                        status or "--",
                        cfg.degree_for(status) if status else "--",
                    )
                )
                sys.stdout.flush()
            time.sleep(max(0.05, cfg.as_int("orientation", "poll_ms") / 1000.0))
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------
CALIBRATION_STEPS = (
    ("normal", "竖屏正对自己：摄像头在上，屏幕正对你（面板原生方向）"),
    ("left-up", "把平板顺时针转 90 度：左边缘朝上"),
    ("bottom-up", "再顺时针转 90 度：上下颠倒"),
    ("right-up", "再顺时针转 90 度：右边缘朝上"),
)


def _sample_mean(accel, samples=15, delay=0.1):
    values = []
    for _ in range(samples):
        raw = accel.raw()
        if raw:
            values.append(raw)
        time.sleep(delay)
    if not values:
        return None
    count = float(len(values))
    return (
        sum(v[0] for v in values) / count,
        sum(v[1] for v in values) / count,
        sum(v[2] for v in values) / count,
    )


def posture_axis_overrides(mapping):
    """把 {轴: 姿态} 转成写进配置的 {normal_axis: 轴, ...}。

    orient.mapping_from_vectors() 返回的是「轴 -> 姿态」，写配置时要反过来用，
    这里单独抽出来，方便单测（之前这里写反过，真机上 KeyError）。
    """
    overrides = {}
    for axis, posture in mapping.items():
        key = config_module.POSTURE_KEYS.get(posture)
        if key is None:
            continue
        overrides[key] = axis
    return {"orientation": overrides}


def cmd_calibrate(args):
    cfg = _load(args)
    log = logging.info
    accel = iio.IioAccelerometer(cfg.get("orientation", "device"))
    if not accel.available:
        log("没有找到加速度计，无法校准")
        return 2

    log("校准：每个姿态把平板放稳、静止 1 秒，然后按回车")
    vectors = {}
    for posture, description in CALIBRATION_STEPS:
        print("")
        print(">>> %s" % description)
        try:
            input("    摆好后按回车…")
        except EOFError:
            return 1
        vector = _sample_mean(accel)
        if vector is None:
            log("读不到加速度数据，重试")
            return 2
        vectors[posture] = vector
        print(
            "    采到：x=%+.1f y=%+.1f z=%+.1f  ->  主轴 %s"
            % (vector[0], vector[1], vector[2], orient.axis_key(*vector))
        )

    mapping, error = orient.mapping_from_vectors(vectors)
    if error:
        log("校准失败：%s" % error)
        return 1

    overrides = posture_axis_overrides(mapping)
    for posture in ("normal", "left-up", "bottom-up", "right-up"):
        overrides["orientation"][config_module.POSTURE_DEG_KEYS[posture]] = cfg.as_int(
            "orientation", config_module.POSTURE_DEG_KEYS[posture]
        )
    path = config_module.write_user_overrides(overrides, getattr(args, "user_config", None))
    print("")
    print("已写入：%s" % path)
    print("轴 -> 姿态：%s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(mapping.items())))
    print("重启服务生效：systemctl --user restart cutiepi-hud  （或重新登录）")
    return 0


# ---------------------------------------------------------------------------
# brightness：读/设亮度，或弹出滑块
# ---------------------------------------------------------------------------
def cmd_brightness(args):
    from . import backlight

    current = backlight.get_brightness()
    max_level = backlight.max_brightness()
    target = (args.value or "").strip()

    if target in ("popup", "ui", "slider"):
        from . import brightness_popup

        result = brightness_popup.run_popup(
            level=current,
            max_level=max_level,
            on_change=lambda value: backlight.set_brightness(value, max_level=max_level),
        )
        if result is None:
            return 2
        print("亮度：%d/%d" % (result, max_level))
        return 0

    if not target:
        print("%s/%s" % (current, max_level))
        return 0 if current is not None else 2

    try:
        if target.startswith(("+", "-")) and current is not None:
            new_level = current + int(target)
        else:
            new_level = int(target)
    except ValueError:
        print("用法：cutiepi-hud brightness [1-15|+1|-1|popup]", file=sys.stderr)
        return 2

    if backlight.set_brightness(new_level, max_level=max_level):
        print("亮度：%s/%s（原 %s）" % (backlight.get_brightness(), max_level, current))
        return 0
    print("设置亮度失败（检查 udev 规则 / video 组权限）", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# screen：息屏 / 亮屏（也装成 cutiepi-screen-off / cutiepi-screen-on 两个命令）
# ---------------------------------------------------------------------------
def cmd_screen(args):
    from . import backlight

    action = (args.action or "status").lower()
    current = backlight.get_power()

    if action == "status":
        if current is None:
            print("unknown")
            return 2
        print("on" if current else "off")
        return 0

    if action == "on":
        target = True
    elif action == "off":
        target = False
    elif action == "toggle":
        target = not current if current is not None else True
    else:
        print("用法：cutiepi-hud screen on|off|toggle|status", file=sys.stderr)
        return 2

    if backlight.set_power(target):
        print("屏幕已%s" % ("打开" if target else "关闭"))
        return 0
    print(
        "背光控制失败（设备 %s）。检查 udev 规则/权限："
        "sudo ./install.sh 会装 90-cutiepi-hud-backlight.rules" % (backlight.find_backlight() or "未找到"),
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# power-dialog：单独测试确认对话框
# ---------------------------------------------------------------------------
def cmd_power_dialog(args):
    from . import dialog

    timeout = getattr(args, "timeout", 15)
    code = dialog.run_dialog(
        "关机", "长按电源键 —— 要关闭平板吗？", "关机", "取消", timeout
    )
    print("结果：%s" % ("确认" if code == dialog.CONFIRM else "取消"))
    return 0 if code == dialog.CONFIRM else 1


# ---------------------------------------------------------------------------
# rotate-calibrate：交互标定「每个姿态应该用哪个旋转角度」
# ---------------------------------------------------------------------------
def _stop_daemon(log=None):
    """停掉后台守护进程：systemd 单元 + 锁文件里的 PID（两种启动方式都覆盖）。

    踩过的坑：手工 nohup 起的那个实例不受 systemctl 管，标定时会跟屏幕抢旋转。
    """
    import subprocess

    stopped = False
    try:
        subprocess.call(
            ["systemctl", "--user", "stop", "cutiepi-hud"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass

    lock = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % util.uid()),
        "cutiepi-hud",
        "lock",
    )
    for attempt in range(2):
        try:
            with open(lock, "r") as handle:
                pid = int((handle.read() or "0").strip() or 0)
        except (IOError, OSError, ValueError):
            pid = 0
        if pid <= 0:
            break
        try:
            os.kill(pid, 15)
            stopped = True
            time.sleep(1.5)
        except OSError:
            break

    # 兜底：按命令行匹配（只匹配 `run` 子命令，不会误伤本进程）
    try:
        subprocess.call(
            ["pkill", "-f", "python3 -m cutiepi_hud run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return stopped


def _start_daemon():
    import subprocess

    try:
        subprocess.call(
            ["systemctl", "--user", "daemon-reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.call(
            ["systemctl", "--user", "restart", "cutiepi-hud"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def _posture_from_accel(accel, cfg, seconds=4.0):
    """采样几秒，返回当前姿态（用配置里的轴->姿态映射）。"""
    tracker = orient.OrientationTracker(
        mapping=cfg.orientation_mapping,
        threshold=cfg.as_float("orientation", "threshold"),
        flat_threshold=cfg.as_float("orientation", "flat_threshold"),
        confirm=2,
    )
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        raw = accel.raw()
        if raw:
            x, y, z = raw
            if cfg.as_bool("orientation", "swap_xy"):
                x, y = y, x
            if cfg.as_bool("orientation", "invert_x"):
                x = -x
            if cfg.as_bool("orientation", "invert_y"):
                y = -y
            found = tracker.update(x, y, z)
            if found:
                last = found
                break
        time.sleep(0.1)
    return last or tracker.posture


def cmd_rotate_flip(args):
    """把四个姿态的角度整体 +180°（等价于上下、左右同时对调）。"""
    cfg = _load(args)
    overrides = {"orientation": {}}
    print("调整（整体 +180°）：")
    for posture in ("normal", "left-up", "bottom-up", "right-up"):
        key = config_module.POSTURE_DEG_KEYS[posture]
        current = cfg.as_int("orientation", key)
        new_value = (current + 180) % 360
        overrides["orientation"][key] = new_value
        print("  %-10s %3d° -> %3d°" % (posture, current, new_value))
    path = config_module.write_user_overrides(overrides, getattr(args, "user_config", None))
    print("")
    print("已写入：%s" % path)
    if _start_daemon():
        print("服务已重启，请转一圈确认。")
    return 0


def _cycle_angles_and_ask(rotator, seconds=4.0):
    """依次下发 0/90/180/270，问用户哪一个是正的；返回 (角度, 是否取消)。"""
    print("   开始依次旋转（注意看屏幕，每个停 %.0f 秒）…" % seconds)
    for angle in (0, 90, 180, 270):
        rotator.apply(angle)
        print("     -> 现在是 %3d 度" % angle)
        sys.stdout.flush()
        time.sleep(seconds)
    while True:
        try:
            answer = input("   刚才哪个角度画面是正的？(0/90/180/270，q 放弃): ").strip()
        except EOFError:
            return None, True
        if answer in ("0", "90", "180", "270"):
            return int(answer), False
        if answer.lower() in ("q", "quit", "exit"):
            return None, True
        print("   请输入 0 / 90 / 180 / 270")


def cmd_rotate_calibrate(args):
    """标定「姿态 -> 屏幕旋转角度」。

    默认（快速）：只在你当前摆的姿势下标定一次，另外三个方向按
    「设备顺时针转 90°、画面就逆时针转 90°」的物理关系自动推导。
    加 --full 则四个姿势逐个标定（更慢，但完全不依赖推导）。
    """
    from .rotate import Rotator

    cfg = _load(args)
    log = logging.info

    accel = iio.IioAccelerometer(cfg.get("orientation", "device"))
    if not accel.available:
        log("没有找到加速度计，无法判断姿态")
        return 2

    rotator = Rotator(
        output=cfg.get("display", "output"),
        backend=cfg.get("display", "backend"),
        swap_lr=False,  # 标定期间一律下发原始角度
        touch_matrix=cfg.as_bool("display", "touch_matrix"),
    )
    backend, output = rotator.detect()
    if not backend:
        log("没有可用的旋转后端：%s" % (rotator.last_error or "未检测到显示服务器"))
        return 2
    print("显示后端：%s   输出：%s" % (backend, output))

    order = ("normal", "left-up", "bottom-up", "right-up")

    # 停掉后台服务，免得它跟标定抢屏幕（systemd 单元 + 手工 nohup 的实例都停）
    _stop_daemon(log)

    chosen = {}
    try:
        if getattr(args, "full", False):
            steps = (
                ("normal", "竖屏正对自己，摄像头朝上（面板原生方向）"),
                ("left-up", "从上一步顺时针转 90 度：左边缘朝上"),
                ("bottom-up", "再顺时针转 90 度：上下颠倒"),
                ("right-up", "再顺时针转 90 度：右边缘朝上"),
            )
            print("")
            print("四个姿势逐个标定：每个姿势我会把屏幕转到 0/90/180/270，")
            print("你只要记住哪一个角度画面是正的，然后输入那个数字。")
            for index, (label, hint) in enumerate(steps):
                print("")
                print("第 %d/4 步：%s" % (index + 1, hint))
                try:
                    input("   摆好后按回车…")
                except EOFError:
                    return 1
                posture = _posture_from_accel(accel, cfg) or label
                print("   检测到姿态：%s" % posture)
                angle, cancelled = _cycle_angles_and_ask(rotator)
                if cancelled:
                    print("已取消，配置未修改")
                    return 1
                chosen[posture] = angle
            deg = dict(chosen)
        else:
            print("")
            print("把平板摆成你平时用的姿势（横着拿就行），拿稳别动。")
            try:
                input("   摆好后按回车…")
            except EOFError:
                return 1
            posture = _posture_from_accel(accel, cfg)
            if posture not in order:
                log("识别不出当前姿态（读到 %r），请先跑 cutiepi-hud monitor 看看" % posture)
                return 2
            index = order.index(posture)
            print("   检测到姿态：%s（第 %d 个方向）" % (posture, index + 1))
            angle, cancelled = _cycle_angles_and_ask(rotator)
            if cancelled:
                print("已取消，配置未修改")
                return 1
            # 设备顺时针转 90° => 画面要逆时针转 90°（即角度 -90）
            base = (angle + 90 * index) % 360  # 竖屏(normal)应有的角度
            deg = {
                "normal": base,
                "left-up": (base - 90) % 360,
                "bottom-up": (base - 180) % 360,
                "right-up": (base - 270) % 360,
            }
            print("")
            print("按物理关系推导出四个方向：")
            for posture_name in order:
                print("  %-10s %3d°" % (posture_name, deg[posture_name]))
    finally:
        _start_daemon()

    overrides = {"display": {"swap_lr": "false"}, "orientation": {}}
    for posture, angle in deg.items():
        key = config_module.POSTURE_DEG_KEYS.get(posture)
        if key:
            overrides["orientation"][key] = angle
    path = config_module.write_user_overrides(overrides, getattr(args, "user_config", None))

    print("")
    print("已写入：%s" % path)
    print("标定结果（姿态 -> 画面顺时针角度）：")
    for posture in order:
        print("  %-10s %s" % (posture, deg.get(posture, "（未变）")))
    print("")
    print("服务已重启，请把平板转一圈确认方向是否都对了。")
    return 0


# ---------------------------------------------------------------------------
# test-rotate
# ---------------------------------------------------------------------------
def cmd_test_rotate(args):
    cfg = _load(args)
    from .rotate import Rotator

    # --raw：跳过 swap_lr，直接下发 0/90/180/270（标定方向时用）
    rotator = Rotator(
        output=cfg.get("display", "output"),
        backend=cfg.get("display", "backend"),
        swap_lr=False if getattr(args, "raw", False) else cfg.as_bool("display", "swap_lr"),
        touch_matrix=cfg.as_bool("display", "touch_matrix"),
    )
    backend, output = rotator.detect()
    print("后端：%s   输出：%s" % (backend, output))
    if not backend:
        print("错误：%s" % (rotator.last_error or "没有可用的显示服务器"))
        return 2

    angles = args.angles or [0, 90, 180, 270]
    for angle in angles:
        ok = rotator.apply(angle)
        print(
            "旋转 %4d° -> %s   当前 transform=%s"
            % (angle, "OK" if ok else "失败: %s" % rotator.last_error, rotator.current_transform())
        )
        sys.stdout.flush()
        if len(angles) > 1:
            time.sleep(args.delay)
    return 0


# ---------------------------------------------------------------------------
# battery
# ---------------------------------------------------------------------------
def cmd_battery(args):
    cfg = _load(args)
    log = logging.info
    baud = cfg.as_int("mcu", "baud")
    port = cfg.get("mcu", "port")
    if not port or str(port).lower() == "auto":
        log("正在探测 MCU 串口（3 秒）…")
        port, _frames = serialport.detect_port(3.0, baud, logger=log)
        if not port:
            log("没有找到 MCU 串口")
            return 2

    battery_filter = mcu.BatteryFilter(cfg.as_int("mcu", "smoothing"))
    deadline = time.time() + args.seconds
    try:
        reader = serialport.SerialReader(port, baud, log).open()
    except OSError as exc:
        log("打开 %s 失败：%s" % (port, exc))
        return 2

    print("读取 %s，%d 秒内收到的帧：" % (port, args.seconds))
    try:
        while time.time() < deadline:
            for frame in reader.read(0.2):
                if frame.msg_type == mcu.MSG_BATTERY:
                    smoothed = battery_filter.feed(frame.value)
                    print(
                        "  电量: %d mV（平滑 %s mV）-> %s%%"
                        % (frame.value, smoothed, mcu.mv_to_percent(smoothed))
                    )
                elif frame.msg_type == mcu.MSG_CHARGE:
                    print(
                        "  充电: %s (value=%d)"
                        % ("充电中" if frame.value == mcu.CHARGE_ON else "未充电", frame.value)
                    )
                    ack = mcu.ACK_CHARGE_ON if frame.value == mcu.CHARGE_ON else mcu.ACK_CHARGE_OFF
                    serialport.write_frame(reader.fd, mcu.ack_frame(ack))
                elif frame.msg_type == mcu.MSG_BUTTON:
                    print("  按键: %d" % frame.value)
                elif frame.msg_type == mcu.MSG_VERSION:
                    print("  版本: %s" % frame.payload)
                else:
                    print("  其它: %r" % frame)
    finally:
        reader.close()
    return 0


# ---------------------------------------------------------------------------
# tray-test
# ---------------------------------------------------------------------------
def cmd_tray_test(args):
    cfg = _load(args)
    log = logging.info
    from . import tray as tray_module

    session.ensure_display_env(timeout=15, logger=log)
    info = tray_module.probe_watcher()
    print("[托盘探测]")
    print("  SNI watcher：%s" % ("有" if info["has_watcher"] else "没有"))
    print("  host 已注册：%s" % ("是" if info["host_registered"] else "否"))
    if info["items"]:
        print("  已注册条目：%s" % ", ".join(info["items"]))
    if info["error"]:
        print("  错误：%s" % info["error"])

    tray = tray_module.TrayProvider(
        logger=log,
        icon_size=cfg.as_int("hud", "icon_size"),
        icon_color=cfg.get("hud", "icon_color"),
        icon_style=cfg.get("hud", "icon_style"),
    )
    tray.start()
    print("  后端：%s" % (tray.backend or "无"))
    print("  状态：%s" % tray.status)
    if tray.backend is None:
        print("")
        print("系统里没有可用的托盘，状态栏图标无法显示。请把上面的输出发我。")
        return 2

    print("")
    print("请看看任务栏（右上角图标区）是否出现电池图标；接下来 %d 秒会循环变化。" % args.seconds)
    percent = 15
    direction = 10
    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            charging = percent < 50
            tray.update(percent, charging, True)
            title, text = tray_module.build_tooltip(percent, charging, True)
            print("  -> %s（%s）" % (title, text))
            sys.stdout.flush()
            percent += direction
            if percent >= 100:
                percent = 100
                direction = -10
            elif percent <= 10:
                percent = 10
                direction = 10
            time.sleep(1.0)
        tray.update(-1, False, False)
        print("  -> 无电池数据（图标应变灰）")
        time.sleep(2.0)
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
    return 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def cmd_demo(args):
    cfg = _load(args)
    log = logging.info
    from .overlay import OverlayProcess
    from .tray import TrayProvider

    session.ensure_display_env(timeout=20, logger=log)
    mode = (cfg.get("hud", "mode") or "tray").strip().lower()
    if getattr(args, "mode", None):
        mode = args.mode
    path = getattr(args, "state_file", None) or state_module.default_path()
    state_file = state_module.StateFile(path)

    tray = None
    overlay = None
    if mode in ("tray", "both"):
        tray = TrayProvider(
            logger=log,
            icon_size=cfg.as_int("hud", "icon_size"),
            icon_color=cfg.get("hud", "icon_color"),
            icon_style=cfg.get("hud", "icon_style"),
        )
        tray.start()
        log("托盘后端：%s（%s）" % (tray.backend or "无", tray.status))
    if mode in ("overlay", "both"):
        overlay = OverlayProcess(
            cfg.get("hud", "overlay"),
            path,
            margin_top=cfg.as_int("hud", "margin_top"),
            margin_right=cfg.as_int("hud", "margin_right"),
            logger=log,
        )
        overlay.ensure()

    values = {
        "battery_percent": args.percent,
        "battery_mv": int(args.percent / 100.0 * 3.89 * 1000),
        "battery_present": True,
        "charging": args.charging,
        "posture": "normal",
        "rotation": 0,
        "mcu_port": "demo",
        "mcu_ok": True,
    }
    state_file.write(values, force=True)
    if tray is not None:
        tray.update(args.percent, args.charging, True)
    log("演示模式：%d 秒后自动退出" % args.seconds)
    deadline = time.time() + args.seconds
    charging = args.charging
    last_flip = time.time()
    try:
        while time.time() < deadline:
            if time.time() - last_flip > 5 and not args.fixed:
                charging = not charging
                last_flip = time.time()
                values["charging"] = charging
                state_file.write(values, force=True)
                if tray is not None:
                    tray.update(args.percent, charging, True)
                if overlay is not None:
                    overlay.ensure()
                log("切换演示状态：%s" % ("充电中" if charging else "未充电"))
            if overlay is not None:
                overlay.ensure()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if tray is not None:
            tray.stop()
        if overlay is not None:
            overlay.stop()
    return 0


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------
def _print_boot_config(lines):
    paths = ("/boot/firmware/config.txt", "/boot/config.txt")
    found = None
    for path in paths:
        if os.path.exists(path):
            found = path
            break
    if not found:
        lines.append("  ! 找不到 config.txt（路径不同？）")
        return
    lines.append("  配置文件：%s" % found)
    wanted = ("dtoverlay=cutiepi-panel", "dtoverlay=mpu6050-i2c5", "enable_uart=1", "uart1")
    try:
        with open(found, "r") as handle:
            text = handle.read()
    except (IOError, OSError) as exc:
        lines.append("  ! 读取失败：%s" % exc)
        return
    for item in wanted:
        lines.append("  %s %s" % ("OK  " if item in text else "缺少", item))


def _print_cmdline(lines):
    paths = ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt")
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as handle:
                    text = handle.read().strip()
            except (IOError, OSError) as exc:
                lines.append("  配置文件：%s（读取失败：%s）" % (path, exc))
                return
            lines.append("  配置文件：%s" % path)
            if "console=serial0" in text or "console=ttyS0" in text or "console=ttyAMA0" in text:
                lines.append("  ! cmdline.txt 里还开着串口控制台，会抢占 MCU 的串口")
            else:
                lines.append("  OK   没有串口控制台占用")
            lines.append("  内容：%s" % text)
            return
    lines.append("  ! 找不到 cmdline.txt")


def cmd_diagnose(args):
    cfg = _load(args)
    lines = []
    add = lines.append

    add("=== cutiepi-hud %s 诊断报告 ===" % __version__)
    add("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("用户：uid=%d  home=%s" % (util.uid(), os.path.expanduser("~")))
    add("")

    add("[系统]")
    for path in ("/etc/os-release",):
        try:
            with open(path, "r") as handle:
                for line in handle:
                    if line.startswith("PRETTY_NAME"):
                        add("  %s" % line.strip())
        except (IOError, OSError):
            pass
    try:
        with open("/proc/device-tree/model", "r") as handle:
            add("  机型：%s" % handle.read().strip("\x00\n "))
    except (IOError, OSError):
        add("  机型：未知")
    try:
        add("  内核：%s" % os.uname().release)
    except AttributeError:
        pass
    add("")

    add("[桌面会话]")
    for name in (
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        add("  %-24s = %s" % (name, os.environ.get(name, "(未设置)")))
    info = session.probe()
    add("  探测到 Wayland：%s" % (info["wayland"] or "无"))
    add("  探测到 X11    ：%s" % (info["x11"] or "无"))
    add("  会话总线      ：%s" % (session.session_bus_address() or "无"))
    add("")

    add("[显示旋转]")
    from .rotate import Rotator

    rotator = Rotator(
        output=cfg.get("display", "output"),
        backend=cfg.get("display", "backend"),
        swap_lr=cfg.as_bool("display", "swap_lr"),
        touch_matrix=cfg.as_bool("display", "touch_matrix"),
    )
    backend, output = rotator.detect()
    add("  后端：%s   输出：%s" % (backend or "无", output or "无"))
    if rotator.last_error:
        add("  错误：%s" % rotator.last_error)
    add("  当前 transform：%s" % (rotator.current_transform() or "未知"))
    add("  wlr-randr：%s" % ("已安装" if _which("wlr-randr") else "未安装"))
    add("  xrandr   ：%s" % ("已安装" if _which("xrandr") else "未安装"))
    add("")

    add("[加速度计]")
    accel = iio.IioAccelerometer(cfg.get("orientation", "device"))
    add("  设备：%s" % (accel.device_name or "未找到"))
    if accel.available:
        add("  名称：%s" % accel.name)
        for _ in range(3):
            add("  采样：%s" % (accel.raw(),))
            time.sleep(0.1)
        add("  mount matrix：%s" % (iio.parse_mount_matrix(
            _read_file(os.path.join(accel.path, "in_mount_matrix"))) or "无"))
    for device in iio.list_devices():
        add(
            "  - %-22s name=%-12s accel=%s"
            % (os.path.basename(device["path"]), device["name"] or "-", device["has_accel"])
        )
    add("  轴 -> 姿态映射：%s" % ", ".join(
        "%s=%s" % (k, v) for k, v in sorted(cfg.orientation_mapping.items())))
    add("")

    add("[启动配置]")
    _print_boot_config(lines)
    add("")

    add("[MCU 串口]")
    _print_cmdline(lines)
    groups = []
    try:
        import grp

        groups = [grp.getgrgid(gid).gr_name for gid in os.getgroups()]
    except (ImportError, AttributeError, KeyError, OSError):
        groups = ["(无法读取)"]
    add("  当前用户组：%s" % " ".join(groups))

    locked_ports = []
    for path, mode, gid, error in serialport.probe_ports(cfg.as_int("mcu", "baud")):
        group_name = str(gid)
        if gid is not None:
            try:
                import grp

                group_name = grp.getgrgid(gid).gr_name
            except (ImportError, KeyError):
                pass
        mode_text = "?" if mode is None else "%o" % mode
        if error:
            add("  %s  权限=%s 组=%s  → 打不开：%s" % (path, mode_text, group_name, error))
            if "ermission" in error or "denied" in error:
                locked_ports.append(path)
        else:
            add("  %s  权限=%s 组=%s  → 可以打开" % (path, mode_text, group_name))

    if locked_ports:
        add("")
        add("  ! %s 打不开（权限不足）：树莓派 OS 13 上 /dev/ttyS0 默认是 600 root:root" %
            ", ".join(locked_ports))
        add("    修复办法（二选一）：")
        add("      sudo ./install.sh                        # 会装 udev 规则并立即修正权限")
        add("      sudo chgrp dialout /dev/ttyS0 && sudo chmod 660 /dev/ttyS0 && sudo usermod -aG dialout $USER")
        add("    （改完要重新登录，或者重启）")

    port, frames = serialport.detect_port(2.0, cfg.as_int("mcu", "baud"), logger=None)
    add("  自动探测结果：%s%s" % (port or "没有可用端口",
                                 "（收到帧）" if frames else ""))
    for competitor in ("cutiepi-mcuproxy", "cutiepi-shell"):
        if _pgrep(competitor):
            add("  ! 发现 %s 在运行：它会抢占串口 / 覆盖屏幕，建议停掉（见 README 6.9）" % competitor)
    add("")

    add("[状态栏托盘]")
    add("  配置的显示方式：hud/mode = %s" % cfg.get("hud", "mode"))
    panels = _panel_processes()
    add("  运行中的面板/合成器：%s" % (", ".join(panels) if panels else "没找到"))
    from . import tray as tray_module

    probe = tray_module.probe_watcher()
    if probe["error"]:
        add("  ! %s" % probe["error"])
    add("  使用的会话总线：%s" % (probe["bus"] or "无"))
    add("  SNI watcher：%s" % ("有" if probe["has_watcher"] else "没有"))
    add("  SNI host 已注册：%s" % ("是" if probe["host_registered"] else "否"))
    add("  已注册的托盘条目：%s" % (", ".join(probe["items"]) or "无"))
    if probe["has_watcher"] and not probe["host_registered"]:
        add("  ! watcher 在但没有 host：任务栏可能关掉了托盘图标区")
    add("  图标颜色设置：hud/icon_color = %s" % cfg.get("hud", "icon_color"))
    theme = tray_module.probe_theme()
    if theme["fg"]:
        add(
            "  主题：%s（prefer-dark=%s）前景色=%.2f %.2f %.2f 背景色=%s"
            % (
                theme["name"] or "?",
                theme["dark"],
                theme["fg"][0],
                theme["fg"][1],
                theme["fg"][2],
                ("%.2f %.2f %.2f" % theme["bg"]) if theme["bg"] else "?",
            )
        )
    else:
        add("  ! 主题探测失败：图标用默认灰（可用 hud/icon_color = light/dark 手工指定）")
    add("")

    add("[叠加层（备用方案）]")
    binary = cfg.get("hud", "overlay")
    add("  程序：%s %s" % (binary, "存在" if os.path.exists(binary) else "不存在（默认不编译）"))
    if os.path.exists(binary):
        try:
            linked = subprocess.check_output(
                ["ldd", binary], stderr=subprocess.DEVNULL, universal_newlines=True
            )
            add(
                "  layer-shell 支持：%s"
                % ("是" if "layer-shell" in linked else "否（Wayland 下位置由合成器决定）")
            )
        except (OSError, subprocess.SubprocessError):
            pass
    add("  当前进程：%s" % ("在运行" if _pgrep("hud-overlay") else "未运行"))
    add("  守护进程：%s" % ("在运行" if _pgrep("cutiepi_hud run") else "未运行"))
    add("  状态文件：%s" % (state_module.default_path(),))
    add("  状态内容：%s" % (state_module.StateFile().read() or "空"))
    add("")

    add("[配置]")
    add("  来源：%s" % (", ".join(cfg.sources) or "内置默认值"))
    if cfg.warnings:
        for warning in cfg.warnings:
            add("  ! %s" % warning)
    for line in cfg.describe().splitlines():
        add("  " + line)
    add("")
    add("=== 报告结束 ===")

    report = "\n".join(lines)
    print(report)
    if getattr(args, "save", None):
        try:
            with open(args.save, "w") as handle:
                handle.write(report + "\n")
            print("\n报告已保存到 %s" % args.save)
        except (IOError, OSError) as exc:
            print("\n保存失败：%s" % exc)
    return 0


def _panel_processes():
    """列出正在运行的面板/合成器进程名（诊断用）。"""
    wanted = (
        "wf-panel-pi",
        "lxpanel",
        "waybar",
        "xfce4-panel",
        "tint2",
        "labwc",
        "wayfire",
        "openbox",
        "mutter",
        "plasmashell",
    )
    found = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % entry, "r") as handle:
                    name = handle.read().strip()
            except (IOError, OSError):
                continue
            if name in wanted and name not in found:
                found.append(name)
    except OSError:
        pass
    return sorted(found)


def _which(name):
    import shutil

    return shutil.which(name)


def _read_file(path):
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return None


def _pgrep(name):
    """按 cmdline 子串找进程（跳过当前进程自己）。"""
    try:
        own_pid = os.getpid()
    except AttributeError:
        own_pid = -1
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            if int(entry) == own_pid:
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "r") as handle:
                    cmdline = handle.read()
            except (IOError, OSError):
                continue
            if name in cmdline:
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def build_parser():
    # 让 -v / --user-config 写在子命令前后都能用。
    # 用 SUPPRESS 作默认值：没写就不覆盖另一个解析器已经设好的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出调试日志"
    )
    common.add_argument(
        "--user-config", default=argparse.SUPPRESS, help="指定用户配置文件路径"
    )

    parser = argparse.ArgumentParser(
        prog="cutiepi-hud",
        description="CutiePi 平板：屏幕自动旋转 + 状态栏电池指示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
        epilog=(
            "常用：\n"
            "  cutiepi-hud run              启动守护进程\n"
            "  cutiepi-hud diagnose         生成诊断报告\n"
            "  cutiepi-hud calibrate        校准姿态\n"
            "  cutiepi-hud test-rotate 90   测试旋转\n"
            "  cutiepi-hud tray-test        测试状态栏电池图标\n"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="启动守护进程", parents=[common])
    run_parser.add_argument("--no-hud", action="store_true", help="不显示电量叠加层")
    run_parser.add_argument("--no-mcu", action="store_true", help="不读 MCU 串口")
    run_parser.add_argument("--no-rotate", action="store_true", help="不自动旋转")
    run_parser.add_argument("--dry-run", action="store_true", help="只打印旋转动作，不真的执行")
    run_parser.add_argument("--state-file", default=None, help="状态文件路径")
    run_parser.add_argument("--lock-file", default=None, help="单实例锁文件路径")
    run_parser.add_argument("--display-timeout", type=float, default=90.0, help="等待桌面会话的秒数")

    monitor_parser = sub.add_parser("monitor", help="打印姿态与加速度", parents=[common])
    monitor_parser.add_argument("seconds", nargs="?", type=float, default=0, help="持续秒数，0 表示一直跑")

    sub.add_parser("calibrate", help="校准 4 个姿态（加速度计的方向）", parents=[common])
    rotate_cal_parser = sub.add_parser(
        "rotate-calibrate",
        help="标定 4 个姿态对应的屏幕旋转角度（屏幕方向不对时用）",
        parents=[common],
    )
    rotate_cal_parser.add_argument(
        "--full", action="store_true", help="四个姿势逐个标定（默认只标定当前姿势并推导其余）"
    )
    sub.add_parser(
        "rotate-flip",
        help="四个方向整体旋转 180°（上下左右同时对调，方向完全相反时用）",
        parents=[common],
    )

    screen_parser = sub.add_parser(
        "screen",
        help="息屏/亮屏（on|off|toggle|status，可给其它程序调用）",
        parents=[common],
    )
    screen_parser.add_argument(
        "action", nargs="?", default="toggle", choices=("on", "off", "toggle", "status")
    )

    brightness_parser = sub.add_parser(
        "brightness", help="查看/设置屏幕亮度（1-15），或弹滑块", parents=[common]
    )
    brightness_parser.add_argument(
        "value", nargs="?", default="", help="1-15 / +1 / -1 / popup，留空则打印当前值"
    )

    power_dialog_parser = sub.add_parser(
        "power-dialog", help="单独弹一次关机确认对话框（测试用）", parents=[common]
    )
    power_dialog_parser.add_argument("--timeout", type=float, default=15.0)

    rotate_parser = sub.add_parser("test-rotate", help="测试旋转", parents=[common])
    rotate_parser.add_argument("angles", nargs="*", type=int, help="要测试的角度，默认 0 90 180 270")
    rotate_parser.add_argument("--delay", type=float, default=3.0, help="每个角度停留秒数")
    rotate_parser.add_argument(
        "--raw", action="store_true", help="不套用 swap_lr，直接下发原始角度（标定方向用）"
    )

    battery_parser = sub.add_parser("battery", help="读一次电池信息", parents=[common])
    battery_parser.add_argument("--seconds", type=float, default=6.0)

    demo_parser = sub.add_parser("demo", help="用假数据看效果", parents=[common])
    demo_parser.add_argument("--percent", type=int, default=87)
    demo_parser.add_argument("--charging", action="store_true")
    demo_parser.add_argument("--fixed", action="store_true", help="不自动切换充电状态")
    demo_parser.add_argument("--seconds", type=int, default=30)
    demo_parser.add_argument("--mode", choices=("tray", "overlay", "both"), default=None)
    demo_parser.add_argument("--state-file", default=None)

    tray_parser = sub.add_parser(
        "tray-test", help="只测试状态栏图标（不需要 MCU）", parents=[common]
    )
    tray_parser.add_argument("--seconds", type=int, default=40)

    diag_parser = sub.add_parser("diagnose", help="输出诊断报告", parents=[common])
    diag_parser.add_argument("--save", default=None, help="把报告另存为文件")

    return parser


HANDLERS = {
    "run": cmd_run,
    "monitor": cmd_monitor,
    "calibrate": cmd_calibrate,
    "rotate-calibrate": cmd_rotate_calibrate,
    "rotate-flip": cmd_rotate_flip,
    "screen": cmd_screen,
    "brightness": cmd_brightness,
    "power-dialog": cmd_power_dialog,
    "test-rotate": cmd_test_rotate,
    "battery": cmd_battery,
    "demo": cmd_demo,
    "tray-test": cmd_tray_test,
    "diagnose": cmd_diagnose,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    command = args.command or "run"
    if not args.command:
        # 没有子命令时按 run 处理，需要把 run 的默认值补齐
        for action in ("no_hud", "no_mcu", "no_rotate", "dry_run"):
            setattr(args, action, False)
        args.state_file = None
        args.lock_file = None
        args.display_timeout = 90.0
    handler = HANDLERS.get(command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
