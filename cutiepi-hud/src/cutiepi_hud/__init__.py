"""CutiePi HUD —— 屏幕自动旋转 + 状态栏电池/亮度指示。

模块划分：
    config         读取 /etc/cutiepi-hud.conf 与 ~/.config/cutiepi-hud/config.ini
    mcu            MCU 串口协议（电量 / 充电 / 按键）
    orient         加速度计 -> 平板姿态
    iio            从 IIO sysfs 读取加速度
    rotate         把旋转角度应用到 Wayland / X11
    backlight      背光开关（息屏）与亮度
    tray           状态栏图标（SNI 托盘协议 / X11 托盘），电池与亮度共用
    brightness_tray 亮度图标进程（每个托盘图标要独立进程）
    brightness_popup 亮度滑块弹窗
    dialog         关机确认对话框
    state          把运行时状态写到文件（备用角标读它）
    session        等待并准备桌面会话环境变量
    overlay        备用角标子进程（面板没有托盘时用）
    cli            命令行入口
"""

__version__ = "1.3.0"
