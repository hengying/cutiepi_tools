#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯逻辑单元测试（不需要树莓派、不需要串口、不需要图形界面）。

    python3 tests/run_tests.py

覆盖：MCU 帧协议、电量曲线、姿态状态机、配置读取、状态文件、旋转参数换算。
"""

import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from cutiepi_hud import config as config_module  # noqa: E402
from cutiepi_hud import mcu, orient  # noqa: E402
from cutiepi_hud import rotate as rotate_module  # noqa: E402
from cutiepi_hud import state as state_module  # noqa: E402

TEMP_ROOT = os.path.join(os.path.dirname(HERE), ".test-tmp")

FAILURES = []
CHECKS = [0]


def temp_dir(name):
    """在工程目录下准备一个干净的可写临时目录（不依赖系统 temp 权限）。

    如果当前环境不允许建目录（例如受限沙箱），返回 None，相关用例跳过。
    """
    path = os.path.join(TEMP_ROOT, name)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".probe")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.unlink(probe)
    except OSError:
        return None
    return path


def check(condition, message):
    CHECKS[0] += 1
    if not condition:
        FAILURES.append(message)
        print("  FAIL  %s" % message)


def eq(actual, expected, message):
    check(actual == expected, "%s（实际 %r，期望 %r）" % (message, actual, expected))


# ---------------------------------------------------------------------------
def test_frames():
    print("[mcu] 组帧")
    frame = mcu.build_frame(1, b"\xf1")
    eq(frame, bytes((0x5A, 0xA5, 0x01, 0x01, 0x00, 0xF1, 0xF2)), "ACK 帧字节")
    eq(mcu.ack_frame(mcu.ACK_CHARGE_ON), mcu.build_frame(1, b"\xf4"), "充电 ACK")
    eq(mcu.cutoff_frame()[2], mcu.MSG_CUTOFF, "断电帧类型")
    eq(mcu.cutoff_frame()[5], 0xF7, "断电帧 payload")

    # 校验和
    body = frame[:-1]
    eq(frame[-1], sum(body) & 0xFF, "校验和 = 前面所有字节之和")


def test_parser():
    print("[mcu] 解帧")
    parser = mcu.FrameParser()

    battery = mcu.build_frame(2, bytes((0x2E, 0x0F)))  # 3886 mV
    frames = parser.feed(battery)
    eq(len(frames), 1, "单帧解析数量")
    eq(frames[0].msg_type, mcu.MSG_BATTERY, "消息类型")
    eq(frames[0].value, 3886, "电量小端解析")

    # 噪声前缀
    parser.reset()
    frames = parser.feed(b"\x00\x11\x22" + battery)
    eq(len(frames), 1, "带噪声前缀仍能解析")

    # 半包：分两次喂
    parser.reset()
    eq(parser.feed(battery[:3]), [], "半包不应产出帧")
    frames = parser.feed(battery[3:])
    eq(len(frames), 1, "补齐后应解析出一帧")

    # 校验错 -> 丢弃并继续找下一帧
    parser.reset()
    broken = bytearray(battery)
    broken[-1] = (broken[-1] + 1) & 0xFF
    frames = parser.feed(bytes(broken) + battery)
    eq(len(frames), 1, "校验错的帧被丢弃，后续正常帧仍能解析")

    # 多个帧连在一起
    parser.reset()
    frames = parser.feed(mcu.build_frame(3, b"\x04") + battery + mcu.build_frame(1, b"\x01"))
    eq([f.msg_type for f in frames], [3, 2, 1], "连续三帧顺序")

    # 长度字段异常也不能崩
    parser.reset()
    parser.feed(bytes((0x5A, 0xA5, 0x02, 0xFF, 0xFF, 0x00)))
    eq(parser.feed(bytes((0x5A, 0xA5))), [], "异常长度后仍可工作")


def test_battery_curve():
    print("[mcu] 电量换算")
    eq(mcu.mv_to_percent(3890), 100, "3.89V -> 100%")
    eq(mcu.mv_to_percent(4200), 100, "超过曲线顶端 -> 100%")
    eq(mcu.mv_to_percent(3480), 5, "3.48V -> 5%")
    eq(mcu.mv_to_percent(3000), 0, "3.00V -> 0%")
    eq(mcu.mv_to_percent(2500), 0, "低于 3.00V -> 0%")
    eq(mcu.mv_to_percent(1900), None, "明显无效值 -> None")
    eq(mcu.mv_to_percent(None), None, "None -> None")
    middle = mcu.mv_to_percent(3805)
    check(middle is not None and 78 <= middle <= 82, "3.805V 应落在 80%% 附近（实际 %r）" % middle)

    battery_filter = mcu.BatteryFilter(samples=4)
    eq(battery_filter.percent(4000), 100, "首次采样")
    # 突然掉到 3.5V，均值应当被平滑而不是立刻变成 35%
    smoothed = battery_filter.percent(3500)
    check(smoothed is not None and smoothed > 35, "滑动平均应高于 35%%（实际 %r）" % smoothed)


def test_orientation():
    print("[orient] 姿态判定")
    eq(orient.axis_key(0, -16000, 200), "y-", "主轴 y-")
    eq(orient.axis_key(0, 16000, 200), "y+", "主轴 y+")
    eq(orient.axis_key(-16000, 0, 100), "x-", "主轴 x-")
    eq(orient.axis_key(16000, 0, 100), "x+", "主轴 x+")
    eq(orient.axis_key(100, 200, 16000), "z+", "主轴 z+")

    mapping = {"y-": "normal", "y+": "bottom-up", "x-": "left-up", "x+": "right-up"}
    tracker = orient.OrientationTracker(mapping=mapping, confirm=3)
    eq(tracker.update(0, -16000, 100), None, "第 1 次采样不切换")
    eq(tracker.update(0, -16000, 100), None, "第 2 次采样不切换")
    eq(tracker.update(0, -16000, 100), "normal", "第 3 次采样切换为 normal")
    eq(tracker.update(0, -16000, 100), None, "已经稳定后不再重复上报")

    # 连续 3 次一致才切换
    eq(tracker.update(-16000, 0, 100), None, "转向 left-up 第 1 次")
    eq(tracker.update(-16000, 0, 100), None, "转向 left-up 第 2 次")
    eq(tracker.update(-16000, 0, 100), "left-up", "转向 left-up 第 3 次")

    # 中间夹一次抖动会清零计数
    eq(tracker.update(16000, 0, 100), None, "抖到 right-up 1")
    eq(tracker.update(0, -16000, 100), None, "抖回 normal 1")
    eq(tracker.update(0, -16000, 100), None, "抖回 normal 2")
    eq(tracker.posture, "left-up", "尚未达到 confirm 次数，姿态不变")

    # 平放（屏幕朝上）保持当前姿态
    tracker.set_posture("normal")
    eq(tracker.update(100, 200, 16000), None, "平放不切换")
    eq(tracker.posture, "normal", "平放后仍是 normal")

    # 姿态不明显时不切换
    tracker.set_posture("normal")
    eq(tracker.update(5000, -6000, 5000), None, "45 度斜放不切换")
    eq(tracker.posture, "normal", "斜放保持原姿态")

    # 自定义映射生效
    flipped = orient.OrientationTracker(mapping={"y+": "normal"}, confirm=1)
    eq(flipped.update(0, 16000, 0), "normal", "自定义映射 y+ -> normal")


def test_calibration():
    print("[orient] 校准结果 -> 映射")
    vectors = {
        "normal": (0.0, -16000.0, 100.0),
        "left-up": (-16000.0, 0.0, 100.0),
        "bottom-up": (0.0, 16000.0, 100.0),
        "right-up": (16000.0, 0.0, 100.0),
    }
    mapping, error = orient.mapping_from_vectors(vectors)
    eq(error, None, "正常校准无错误")
    eq(mapping, {"y-": "normal", "x-": "left-up", "y+": "bottom-up", "x+": "right-up"},
       "校准映射内容")

    bad = dict(vectors)
    bad["right-up"] = (-16000.0, 0.0, 100.0)  # 与 left-up 落在同一根轴（x-）
    _mapping, error = orient.mapping_from_vectors(bad)
    check(error is not None, "两个姿态落在同一根轴时应报错")

    flat = dict(vectors)
    flat["normal"] = (0.0, 0.0, 16000.0)
    _mapping, error = orient.mapping_from_vectors(flat)
    check(error is not None, "平放采样应报错")

    # 回归：calibrate 写配置时要把「轴 -> 姿态」反过来（真机上这里 KeyError 过）
    from cutiepi_hud import cli

    overrides = cli.posture_axis_overrides(mapping)
    eq(
        overrides["orientation"],
        {
            "normal_axis": "y-",
            "left_up_axis": "x-",
            "bottom_up_axis": "y+",
            "right_up_axis": "x+",
        },
        "写入配置的键名/取值",
    )


def test_config():
    print("[config] 读取与覆盖")
    tmp = temp_dir("config")
    if tmp is None:
        print("  跳过（当前环境不能创建临时文件）")
        return
    system = os.path.join(tmp, "system.conf")
    user = os.path.join(tmp, "user.ini")
    with open(system, "w") as handle:
        handle.write(
            "[display]\nswap_lr = true\n\n[orientation]\nconfirm = 5\nunknown_key = 1\n"
        )
    with open(user, "w") as handle:
        handle.write("[orientation]\nconfirm = 2\ndeg_left_up = 90\n")

    cfg = config_module.load_config(system_path=system, user_path=user)
    eq(cfg.as_bool("display", "swap_lr"), True, "系统配置生效")
    eq(cfg.as_int("orientation", "confirm"), 2, "用户配置覆盖系统配置")
    eq(cfg.as_int("orientation", "deg_left_up"), 90, "角度覆盖")
    eq(cfg.as_int("orientation", "deg_right_up"), 90, "未覆盖项保持默认")
    eq(cfg.get("mcu", "port"), "auto", "缺省项使用默认值")

    # 写回用户配置（calibrate 用）
    config_module.write_user_overrides(
        {"orientation": {"left_up_axis": "x+", "confirm": 4}}, user_path=user
    )
    cfg2 = config_module.load_config(system_path=system, user_path=user)
    eq(cfg2.get("orientation", "left_up_axis"), "x+", "写回后能读出来")
    eq(cfg2.as_int("orientation", "confirm"), 4, "写回覆盖旧值")
    eq(cfg2.as_int("orientation", "deg_left_up"), 90, "写回保留其它项")

    bad = os.path.join(tmp, "bad.conf")
    with open(bad, "w") as handle:
        handle.write("[display]\nswap_lr = 也许\n")
    cfg3 = config_module.load_config(system_path=bad, user_path="/nonexistent/x.ini")
    eq(cfg3.as_bool("display", "swap_lr"), False, "非法布尔值回落到默认值")
    check(len(cfg3.warnings) >= 1, "非法值应产生警告")


def test_degrees():
    print("[config] 姿态角度")
    cfg = config_module.load_config(user_path="/nonexistent/x.ini")
    # 2026-11-03 在 CutiePi 真机上标定出的实测值
    eq(cfg.degree_for("normal"), 180, "normal=180")
    eq(cfg.degree_for("left-up"), 270, "left-up=270")
    eq(cfg.degree_for("bottom-up"), 0, "bottom-up=0")
    eq(cfg.degree_for("right-up"), 90, "right-up=90")
    eq(cfg.degree_for("不存在"), None, "未知姿态返回 None")
    eq(len(cfg.orientation_mapping), 4, "映射覆盖 4 个姿态")
    eq(cfg.as_bool("display", "swap_lr"), False, "本机不需要 swap_lr")


def test_rotation_params():
    print("[rotate] 角度 -> 命令参数")
    x11 = rotate_module.Rotator(backend="x11", swap_lr=False)
    eq(x11.rotate_name_x11(0), "normal", "X11 0 度")
    eq(x11.rotate_name_x11(90), "right", "X11 90 度 -> right")
    eq(x11.rotate_name_x11(180), "inverted", "X11 180 度")
    eq(x11.rotate_name_x11(270), "left", "X11 270 度 -> left")
    eq(x11.rotate_name_x11(360), "normal", "X11 360 度回绕")

    wayland = rotate_module.Rotator(backend="wayland", swap_lr=False)
    eq(wayland.transform_wayland(0), "normal", "Wayland 0 度")
    eq(wayland.transform_wayland(90), "90", "Wayland 90 度")
    eq(wayland.transform_wayland(270), "270", "Wayland 270 度")

    swapped = rotate_module.Rotator(backend="x11", swap_lr=True)
    eq(swapped.rotate_name_x11(90), "left", "swap_lr 后 90 度变 left")
    eq(swapped.rotate_name_x11(270), "right", "swap_lr 后 270 度变 right")
    eq(swapped.rotate_name_x11(180), "inverted", "swap_lr 不影响 180 度")

    # X11 触摸矩阵齐全
    for name in ("normal", "left", "right", "inverted"):
        check(name in rotate_module.X11_TOUCH_MATRIX, "缺触摸矩阵：%s" % name)
        eq(len(rotate_module.X11_TOUCH_MATRIX[name].split()), 9, "%s 触摸矩阵应为 9 个数字" % name)


def test_state_file():
    print("[state] 状态文件")
    tmp = temp_dir("state")
    if tmp is None:
        print("  跳过（当前环境不能创建临时文件）")
        return
    path = os.path.join(tmp, "state")
    writer = state_module.StateFile(path)
    values = {
        "battery_percent": 87,
        "battery_mv": 3850,
        "battery_present": True,
        "charging": True,
        "posture": "normal",
        "rotation": 0,
        "mcu_ok": True,
    }
    check(writer.write(values), "首次写入应返回 True")
    check(not writer.write(values), "内容不变时不应重写")
    read_back = writer.read()
    eq(read_back["battery_percent"], "87", "读回电量")
    eq(read_back["charging"], "1", "布尔值写成 1")
    eq(read_back["posture"], "normal", "读回姿态")
    eq(len(read_back), 7, "键数量")

    values["battery_percent"] = 86
    check(writer.write(values), "内容变化应写入")
    eq(writer.read()["battery_percent"], "86", "更新后读回")


def test_mcu_worker():
    print("[daemon] MCU 帧 -> 运行时状态")
    from cutiepi_hud import daemon as daemon_module

    cfg = config_module.Config({("mcu", "shutdown_confirm"): False})
    runtime = daemon_module.RuntimeState()
    worker = daemon_module.McuWorker(cfg, runtime, threading.Event(), logger=lambda _m: None)

    # 回归测试：方法名不能叫 _handle —— 会覆盖 threading.Thread._handle，
    # 真机上表现为"第一帧数据到达就 TypeError"
    check(
        callable(getattr(worker, "_handle_frame", None)),
        "McuWorker 应该有 _handle_frame 方法",
    )
    check(
        getattr(worker, "_handle", None) is not getattr(worker, "_handle_frame", None),
        "_handle 不能被我们的方法覆盖（那是 Thread 自己的属性）",
    )

    sent = []
    original = daemon_module.serialport.write_frame
    daemon_module.serialport.write_frame = lambda _fd, frame: sent.append(frame)

    class FakeReader(object):
        fd = None

    reader = FakeReader()
    try:
        # 电量：3886 mV
        worker._handle_frame(mcu.Frame(mcu.MSG_BATTERY, bytes((0x2E, 0x0F))), reader)
        snapshot = runtime.snapshot()
        eq(snapshot["battery_mv"], 3886, "电量原值进入运行时状态")
        eq(snapshot["battery_present"], True, "有效电量应标记 present")
        check(
            snapshot["battery_percent"] is not None and 90 <= snapshot["battery_percent"] <= 100,
            "3.886V 应在 90~100%%（实际 %r）" % snapshot["battery_percent"],
        )

        # 无效电量（断开电池）
        worker._handle_frame(mcu.Frame(mcu.MSG_BATTERY, bytes((0x00, 0x00))), reader)
        eq(runtime.snapshot()["battery_present"], False, "0 mV 应标记为没有电池")

        # 充电事件：充电中 -> 回 ACK 0xF4
        sent[:] = []
        worker._handle_frame(mcu.Frame(mcu.MSG_CHARGE, b"\x04"), reader)
        eq(runtime.snapshot()["charging"], True, "充电中")
        eq(sent, [mcu.build_frame(1, b"\xf4")], "充电 ACK 内容")

        # 未充电 -> 回 ACK 0xF5
        sent[:] = []
        worker._handle_frame(mcu.Frame(mcu.MSG_CHARGE, b"\x05"), reader)
        eq(runtime.snapshot()["charging"], False, "未充电")
        eq(sent, [mcu.build_frame(1, b"\xf5")], "断电 ACK 内容")

        # 短按 -> ACK 0xF1
        sent[:] = []
        worker._handle_frame(mcu.Frame(mcu.MSG_BUTTON, b"\x01"), reader)
        eq(sent, [mcu.build_frame(1, b"\xf1")], "短按 ACK")

        # 关机请求但 shutdown_confirm=false -> 回取消 0xFC
        sent[:] = []
        worker._handle_frame(mcu.Frame(mcu.MSG_BUTTON, b"\x03"), reader)
        eq(sent, [mcu.build_frame(1, b"\xfc")], "取消关机 ACK")
    finally:
        daemon_module.serialport.write_frame = original


def test_tray_icon():
    print("[tray] 状态栏图标渲染")
    from cutiepi_hud import tray

    size = 32
    pixels = tray.render_icon(87, False, True, size, foreground=(0.1, 0.1, 0.1))
    eq(len(pixels), size * size, "像素数量")
    ok = True
    for value in pixels:
        if len(value) != 4 or any(not (0 <= channel <= 255) for channel in value):
            ok = False
            break
    check(ok, "每个像素都是 0~255 的 RGBA")

    visible = sum(1 for _r, _g, _b, a in pixels if a > 0)
    check(visible > 100, "图标应该有可见像素（实际 %d）" % visible)

    # 颜色规则
    eq(tray.fill_color(80, False), tray.DEFAULT_FOREGROUND, "正常电量跟随前景色")
    eq(tray.fill_color(80, True), tray.COLOR_CHARGING, "充电中绿色")
    eq(tray.fill_color(10, False), tray.COLOR_CRITICAL, "≤10% 红色")
    eq(tray.fill_color(20, False), tray.COLOR_LOW, "≤20% 橙色")
    eq(tray.fill_color(-1, False, present=False), None, "没有电池时不填充")
    eq(tray.fill_color(80, False, True, (0.2, 0.2, 0.2)), (0.2, 0.2, 0.2), "可指定前景色")

    def opaque(pix):
        # 填充是半透明的，阈值取 100 才能把"有内容的像素"都算进来
        return sum(1 for _r, _g, _b, a in pix if a > 100)

    full = opaque(tray.render_icon(100, False, True, size))
    low = opaque(tray.render_icon(20, False, True, size))
    check(full > low, "100%% 的可见面积应多于 20%%（%d vs %d）" % (full, low))

    absent = opaque(tray.render_icon(-1, False, False, size))
    check(absent < low, "没电池时应该更“空”（%d vs %d）" % (absent, low))

    # 前景色必须生效：浅色主题用深图标，深色主题用浅图标
    dark_fg = tray.render_icon(80, False, True, size, foreground=(0.05, 0.05, 0.05))
    light_fg = tray.render_icon(80, False, True, size, foreground=(0.95, 0.95, 0.95))
    dark_mass = sum(sum(pixel[:3]) for pixel in dark_fg)
    light_mass = sum(sum(pixel[:3]) for pixel in light_fg)
    check(light_mass > dark_mass * 3, "前景色应明显影响图标亮度")

    # 充电闪电会让像素分布不同
    no_charge = tray.render_icon(60, False, True, size)
    charging = tray.render_icon(60, True, True, size)
    check(no_charge != charging, "充电状态应改变图标")

    # 字节序
    one = [(1, 2, 3, 4)]
    eq(tray.to_argb_bytes(one), bytes((4, 1, 2, 3)), "SNI 用 ARGB")
    eq(tray.to_rgba_bytes(one), bytes((1, 2, 3, 4)), "GdkPixbuf 用 RGBA")
    eq(len(tray.to_argb_bytes(pixels)), size * size * 4, "字节数")

    # 手工指定颜色（面板底色和主题不一致时的逃生口）
    eq(
        tray.resolve_foreground("light"),
        tray.FOREGROUND_FOR_LIGHT_PANEL,
        "light = 浅色面板用深图标",
    )
    eq(
        tray.resolve_foreground("dark"),
        tray.FOREGROUND_FOR_DARK_PANEL,
        "dark = 深色面板用浅图标",
    )
    eq(tray.resolve_foreground("auto", (0.3, 0.3, 0.3)), (0.3, 0.3, 0.3), "auto 用探测值")
    eq(tray.resolve_foreground("auto", None), tray.DEFAULT_FOREGROUND, "auto 探测失败用默认")


def test_tray_tooltip():
    print("[tray] 悬停提示文本")
    from cutiepi_hud import tray

    eq(tray.build_tooltip(87, False, True), ("电池 87%", "使用电池"), "未充电")
    eq(tray.build_tooltip(87, True, True), ("电池 87%", "充电中"), "充电中")
    title, text = tray.build_tooltip(-1, False, False)
    eq(title, "电池", "没有数据时的标题")
    check("读不到" in text, "没有数据时的说明")

    # 属性类型字符串必须和 SNI 规范一致
    check('name="IconPixmap" type="a(iiay)"' in tray.SNI_XML, "IconPixmap 类型")
    check('name="ToolTip" type="(sa(iiay)ss)"' in tray.SNI_XML, "ToolTip 类型")
    check("<signal name=\"NewIcon\"/>" in tray.SNI_XML, "NewIcon 信号")
    eq(tray.WATCHER_NAME, "org.kde.StatusNotifierWatcher", "watcher 名字")
    eq(tray.SNI_IFACE, "org.kde.StatusNotifierItem", "SNI 接口名")


def test_tray_number_icon():
    print("[tray] 数字图标")
    from cutiepi_hud import tray

    size = 32
    pixels = tray.render_number_icon(87, False, True, size, foreground=(0.1, 0.1, 0.1))
    eq(len(pixels), size * size, "像素数量")
    visible = sum(1 for _r, _g, _b, a in pixels if a > 128)
    check(visible > 40, "87 应该有可见笔画（实际 %d）" % visible)

    # 数字越多，笔画面积不一定单调，但 100 必须也能画出来并且不超出边界
    full = tray.render_number_icon(100, False, True, size)
    eq(len(full), size * size, "100 的像素数量")
    check(sum(1 for p in full if p[3] > 128) > 60, "100 应该有可见笔画")

    # 每个数字都在字体表里
    for digit in "0123456789":
        check(digit in tray.FONT_5X7, "字体缺少数字 %s" % digit)
        eq(len(tray.FONT_5X7[digit]), 7, "字形 %s 应该是 7 行" % digit)
        for row in tray.FONT_5X7[digit]:
            eq(len(row), 5, "字形 %s 每行应为 5 列" % digit)

    # 颜色：充电绿、低电量红/橙、正常前景色
    def main_color(pix):
        counts = {}
        for red, green, blue, alpha in pix:
            if alpha > 128:
                counts[(red, green, blue)] = counts.get((red, green, blue), 0) + 1
        return max(counts.items(), key=lambda item: item[1])[0] if counts else None

    charging = main_color(tray.render_number_icon(50, True, True, size))
    check(charging is not None and charging[1] > charging[0], "充电时数字偏绿（实际 %r）" % (charging,))
    critical = main_color(tray.render_number_icon(5, False, True, size))
    check(critical is not None and critical[0] > critical[1], "≤10%% 时数字偏红（实际 %r）" % (critical,))
    normal = main_color(tray.render_number_icon(80, False, True, size, foreground=(0.1, 0.1, 0.1)))
    eq(normal, (26, 26, 26), "正常电量用前景色")

    # 没数据时显示 --，而且不是前景色
    none_pix = tray.render_number_icon(-1, False, False, size)
    check(sum(1 for p in none_pix if p[3] > 128) > 20, "没数据时也要有笔画")

    # 数字变化必须改变图标
    check(
        tray.render_number_icon(87, False, True, size) != tray.render_number_icon(88, False, True, size),
        "不同电量应该画出不同图标",
    )


def test_button_screen_toggle():
    print("[daemon] 短按电源键 -> 息屏/亮屏")
    from cutiepi_hud import daemon as daemon_module

    calls = []
    state = {"on": True}

    def fake_set_power(on, path=None):
        calls.append(bool(on))
        state["on"] = bool(on)
        return True

    def fake_get_power(path=None):
        return state["on"]

    original_set = daemon_module.backlight.set_power
    original_get = daemon_module.backlight.get_power
    daemon_module.backlight.set_power = fake_set_power
    daemon_module.backlight.get_power = fake_get_power
    try:
        for enabled in (True, False):
            cfg = config_module.Config({("mcu", "screen_toggle"): enabled})
            runtime = daemon_module.RuntimeState()
            worker = daemon_module.McuWorker(cfg, runtime, threading.Event(), logger=lambda _m: None)
            sent = []

            class FakeReader(object):
                fd = None

            original_write = daemon_module.serialport.write_frame
            daemon_module.serialport.write_frame = lambda _fd, frame: sent.append(frame)
            try:
                if enabled:
                    # 第一次：开 -> 关；第二次：关 -> 开
                    worker._handle_frame(mcu.Frame(mcu.MSG_BUTTON, b"\x01"), FakeReader())
                    eq(calls[-1], False, "第一次短按应关屏")
                    eq(sent[-1], mcu.build_frame(1, b"\xf1"), "短按要回 ACK 0xF1")
                    worker._handle_frame(mcu.Frame(mcu.MSG_BUTTON, b"\x01"), FakeReader())
                    eq(calls[-1], True, "第二次短按应亮屏")
                else:
                    before = len(calls)
                    worker._handle_frame(mcu.Frame(mcu.MSG_BUTTON, b"\x01"), FakeReader())
                    eq(len(calls), before, "screen_toggle=false 时不应动背光")
                    eq(sent[-1], mcu.build_frame(1, b"\xf1"), "仍然要回 ACK")
            finally:
                daemon_module.serialport.write_frame = original_write
    finally:
        daemon_module.backlight.set_power = original_set
        daemon_module.backlight.get_power = original_get


def test_screen_command():
    print("[cli] 息屏/亮屏命令")
    from cutiepi_hud import backlight as backlight_module
    from cutiepi_hud import cli as cli_module

    state = {"on": True}
    calls = []

    def fake_set(on, path=None):
        calls.append(bool(on))
        state["on"] = bool(on)
        return True

    def fake_get(path=None):
        return state["on"]

    original_set = backlight_module.set_power
    original_get = backlight_module.get_power
    backlight_module.set_power = fake_set
    backlight_module.get_power = fake_get
    try:
        class Args(object):
            pass

        args = Args()
        args.action = "off"
        eq(cli_module.cmd_screen(args), 0, "screen off 退出码 0")
        eq(state["on"], False, "screen off 生效")

        args.action = "on"
        eq(cli_module.cmd_screen(args), 0, "screen on 退出码 0")
        eq(state["on"], True, "screen on 生效")

        args.action = "toggle"
        cli_module.cmd_screen(args)
        eq(state["on"], False, "toggle 取反（亮->灭）")
        cli_module.cmd_screen(args)
        eq(state["on"], True, "toggle 取反（灭->亮）")

        args.action = "status"
        eq(cli_module.cmd_screen(args), 0, "status 退出码 0")
    finally:
        backlight_module.set_power = original_set
        backlight_module.get_power = original_get


def test_shutdown_dialog_flow():
    print("[daemon] 长按关机：先取消 MCU 请求，再弹框确认")
    from cutiepi_hud import daemon as daemon_module

    class FakeReader(object):
        fd = None

    # 1) 先回 FC（取消），再把决定权交给对话框
    cfg = config_module.Config(
        {
            ("mcu", "shutdown_confirm"): True,
            ("mcu", "shutdown_dialog"): False,
        }
    )
    worker = daemon_module.McuWorker(cfg, daemon_module.RuntimeState(), threading.Event(),
                                     logger=lambda _m: None)
    powered = []
    worker._poweroff = lambda: (powered.append(1), 0)[1]
    sent = []
    original_write = daemon_module.serialport.write_frame
    original_call = daemon_module.subprocess.call
    daemon_module.serialport.write_frame = lambda _fd, frame: sent.append(frame)
    try:
        worker._request_shutdown(FakeReader())
        eq(sent, [mcu.build_frame(1, b"\xfc")], "关机请求要先回 FC 取消，避免 MCU 硬断电")
        eq(len(powered), 1, "shutdown_dialog=false 时应直接关机")

        # 2) 对话框：确认 -> 关机；取消 -> 不关机
        powered.clear()
        daemon_module.subprocess.call = lambda *_a, **_k: 0
        worker._confirm_then_poweroff()
        eq(len(powered), 1, "对话框确认后应关机")

        powered.clear()
        daemon_module.subprocess.call = lambda *_a, **_k: 1
        worker._confirm_then_poweroff()
        eq(len(powered), 0, "对话框取消后不应关机")

        # 3) 重复的 6 秒请求（MCU 按住时会连续发）只能触发一次
        powered.clear()
        daemon_module.subprocess.call = lambda *_a, **_k: 1  # 对话框立即返回"取消"
        worker2 = daemon_module.McuWorker(
            config_module.Config({("mcu", "shutdown_dialog"): True}),
            daemon_module.RuntimeState(),
            threading.Event(),
            logger=lambda _m: None,
        )
        worker2._poweroff = lambda: (powered.append(1), 0)[1]
        sent[:] = []
        worker2._request_shutdown(FakeReader())
        eq(len(powered), 0, "弹框时不能直接关机")

        # 模拟"框已经开着"时又来了一堆重复请求：不能再弹、更不能关机
        worker2._dialog_open = True
        for _ in range(5):
            worker2._request_shutdown(FakeReader())
        eq(len(powered), 0, "重复请求不能触发关机")
        worker2._dialog_open = False

        # 4) shutdown_confirm=false：只回 ACK，不弹框也不关机
        cfg_off = config_module.Config({("mcu", "shutdown_confirm"): False})
        worker_off = daemon_module.McuWorker(
            cfg_off, daemon_module.RuntimeState(), threading.Event(), logger=lambda _m: None
        )
        worker_off._poweroff = lambda: (powered.append(1), 0)[1]
        sent[:] = []
        worker_off._request_shutdown(FakeReader())
        eq(sent, [mcu.build_frame(1, b"\xfc")], "关闭功能时只回 FC")
        eq(len(powered), 0, "关闭功能时不关机")
    finally:
        daemon_module.serialport.write_frame = original_write
        daemon_module.subprocess.call = original_call


def test_double_press_dialog():
    print("[daemon] 连按两下电源键 -> 弹关机框")
    from cutiepi_hud import daemon as daemon_module

    class FakeReader(object):
        fd = None

    cfg = config_module.Config(
        {
            ("mcu", "screen_toggle"): True,
            ("mcu", "double_press_dialog"): True,
            ("mcu", "double_press_ms"): 700,
        }
    )
    worker = daemon_module.McuWorker(cfg, daemon_module.RuntimeState(), threading.Event(),
                                     logger=lambda _m: None)
    dialogs = []
    toggles = []
    worker._ask_shutdown = lambda: (dialogs.append(time.time()), True)[1]
    worker._toggle_screen = lambda: toggles.append(time.time())

    original_write = daemon_module.serialport.write_frame
    daemon_module.serialport.write_frame = lambda _fd, frame: None
    try:
        # 单次短按 -> 只息屏
        worker._handle_button(1, FakeReader())
        eq(len(toggles), 1, "单次短按应切换背光")
        eq(len(dialogs), 0, "单次短按不该弹框")

        # 间隔 0.3s 再按一次 -> 双击，弹框（且不再切换背光）
        worker._last_short_time = time.time() - 0.3
        toggles.clear()
        worker._handle_button(1, FakeReader())
        eq(len(dialogs), 1, "连按两下应弹框")
        eq(len(toggles), 0, "双击时不应再切背光")

        # 间隔很久再按 -> 又回到单次短按
        dialogs.clear()
        worker._last_short_time = time.time() - 5
        worker._handle_button(1, FakeReader())
        eq(len(dialogs), 0, "间隔久了不算双击")
    finally:
        daemon_module.serialport.write_frame = original_write


def test_repeated_shutdown_requests():
    print("[daemon] 重复的 6 秒请求只处理一次，取消后能再次触发")
    from cutiepi_hud import daemon as daemon_module

    class FakeReader(object):
        fd = None

    cfg = config_module.Config({("mcu", "shutdown_dialog"): True})
    worker = daemon_module.McuWorker(cfg, daemon_module.RuntimeState(), threading.Event(),
                                     logger=lambda _m: None)
    powered = []
    worker._poweroff = lambda: (powered.append(1), 0)[1]
    dialogs = []

    def fake_confirm():
        """模拟"弹框 -> 用户取消"：结束后必须清掉两个标记。"""
        dialogs.append(1)
        with worker._shutdown_lock:
            worker._dialog_open = False
            worker._powered_off_requested = False

    worker._confirm_then_poweroff = fake_confirm

    original_write = daemon_module.serialport.write_frame
    daemon_module.serialport.write_frame = lambda _fd, frame: None
    try:
        # 1) 框开着（_dialog_open=True）时的重复请求：不能再弹、不能关机
        worker._dialog_open = True
        for _ in range(5):
            worker._request_shutdown(FakeReader())
        eq(len(dialogs), 0, "框开着时重复请求不应再弹框")
        eq(len(powered), 0, "框开着时重复请求不应关机")
        worker._dialog_open = False

        # 2) 取消之后（标记都已清零）再长按，应能再次弹框（这就是真机上第二个 bug）
        worker._request_shutdown(FakeReader())
        time.sleep(0.2)
        eq(len(dialogs), 1, "取消后再次长按应重新弹框")

        # 3) 再来一次，仍应弹框（而不是被静默忽略）
        worker._request_shutdown(FakeReader())
        time.sleep(0.2)
        eq(len(dialogs), 2, "第二次长按也应弹框")
        eq(len(powered), 0, "两次都没确认，不应关机")
    finally:
        daemon_module.serialport.write_frame = original_write


def test_brightness():
    print("[hud] 亮度：等级换算 / 太阳图标")
    from cutiepi_hud import backlight as backlight_module
    from cutiepi_hud import tray as tray_module

    eq(backlight_module.clamp_level(0, 15), 1, "亮度下限 1")
    eq(backlight_module.clamp_level(99, 15), 15, "亮度上限 15")
    eq(backlight_module.clamp_level("7", 15), 7, "字符串也能转")
    eq(backlight_module.clamp_level("abc", 15), None, "非法值返回 None")
    eq(backlight_module.clamp_level(7.6, 15), 8, "四舍五入")

    low = tray_module.render_sun_icon(1, 15, 32)
    high = tray_module.render_sun_icon(15, 15, 32)
    eq(len(low), 32 * 32, "太阳图标尺寸")
    check(
        sum(1 for px in high if px[3] > 0) > sum(1 for px in low if px[3] > 0),
        "亮度越高，太阳越大",
    )

    eq(tray_module.build_sun_tooltip(8, 15)[0], "亮度 8/15", "亮度提示标题")
    eq(tray_module.build_sun_tooltip(None, 15)[0], "亮度", "读不到亮度时的提示")

    provider = tray_module.TrayProvider(
        kind="brightness", foreground=(0.0, 0.0, 0.0), icon_size=32
    )
    provider.update_brightness(5, 15)
    eq(provider.brightness_snapshot()["level"], 5, "亮度状态保存")
    eq(len(provider._pixels()), 32 * 32, "亮度和电池图标互不干扰")


def cleanup():
    """跑完清掉临时目录（某些沙箱环境删不掉，忽略即可）。"""
    if os.path.isdir(TEMP_ROOT):
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def main():
    test_frames()
    test_parser()
    test_battery_curve()
    test_orientation()
    test_calibration()
    test_config()
    test_degrees()
    test_rotation_params()
    test_state_file()
    test_mcu_worker()
    test_button_screen_toggle()
    test_screen_command()
    test_shutdown_dialog_flow()
    test_double_press_dialog()
    test_repeated_shutdown_requests()
    test_brightness()
    test_tray_icon()
    test_tray_number_icon()
    test_tray_tooltip()
    cleanup()

    print("")
    if FAILURES:
        print("失败 %d / %d 项：" % (len(FAILURES), CHECKS[0]))
        for failure in FAILURES:
            print("  - %s" % failure)
        return 1
    print("全部通过：%d 项检查" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
