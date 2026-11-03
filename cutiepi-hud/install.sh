#!/bin/bash
# CutiePi HUD 安装脚本
#
#   sudo ./install.sh                 正常安装（状态栏托盘方式，会尝试用 apt 装依赖）
#   sudo ./install.sh --no-deps       不装依赖（离线/已装好时）
#   sudo ./install.sh --no-cmdline    不改 /boot/firmware/cmdline.txt
#   sudo ./install.sh --with-overlay  额外编译"独立角标"备用程序（只在面板没有托盘时用）
#
# 只做这些事：装依赖、拷程序、装配置/autostart/udev/sudoers/关机钩子。
# 每一处改动都会打印出来，卸载用 uninstall.sh。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/opt/cutiepi-hud"
LAUNCHER="/usr/local/bin/cutiepi-hud"
CONF="/etc/cutiepi-hud.conf"
UDEV_RULE="/etc/udev/rules.d/90-cutiepi-hud-backlight.rules"
UDEV_SERIAL="/etc/udev/rules.d/99-cutiepi-hud-serial.rules"
AUTOSTART="/etc/xdg/autostart/cutiepi-hud.desktop"
USER_UNIT="/etc/systemd/user/cutiepi-hud.service"
SHUTDOWN_HOOK="/usr/lib/systemd/system-shutdown/cutiepi-hud-cutoff"
SUDOERS_FILE="/etc/sudoers.d/cutiepi-hud"

WITH_DEPS=1
FIX_CMDLINE=1
WITH_OVERLAY=0
TARGET_USER="${TARGET_USER:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps) WITH_DEPS=0 ;;
        --no-cmdline) FIX_CMDLINE=0 ;;
        --with-overlay) WITH_OVERLAY=1 ;;
        --user) shift; TARGET_USER="$1" ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            exit 2
            ;;
    esac
    shift
done

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 基本检查
[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo ./install.sh"

if [ -z "$TARGET_USER" ]; then
    TARGET_USER="${SUDO_USER:-}"
fi
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    die "请从普通用户账号用 sudo 运行（例如 sudo ./install.sh），或用 --user 用户名 指定"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -n "$TARGET_HOME" ] && [ -d "$TARGET_HOME" ] || die "找不到用户 $TARGET_USER 的家目录"

log "安装用户：$TARGET_USER（$TARGET_HOME）"
log "源目录：$SRC"

# ---------------------------------------------------------------- 依赖
if [ "$WITH_DEPS" -eq 1 ]; then
    log "安装系统依赖（已装的会跳过）"
    apt-get install -y --no-install-recommends \
        python3 python3-gi gir1.2-glib-2.0 python3-cairo \
        || warn "基础依赖安装失败，程序可能跑不起来（需要 python3-gi）"
    apt-get install -y --no-install-recommends wlr-randr \
        || warn "wlr-randr 没装上：Wayland 下自动旋转会不可用（可稍后 sudo apt install wlr-randr）"
    if [ "$WITH_OVERLAY" -eq 1 ]; then
        apt-get install -y --no-install-recommends gcc pkg-config libgtk-3-dev libgtk-layer-shell-dev \
            || warn "叠加层编译依赖安装失败"
    fi
else
    log "按参数要求跳过依赖安装"
fi

# 先确认 PyGObject 可用（状态栏图标靠它发 D-Bus）
if ! python3 -c "import gi" >/dev/null 2>&1; then
    warn "python3 -c 'import gi' 失败：状态栏图标需要 python3-gi"
    warn "  sudo apt install python3-gi gir1.2-glib-2.0"
fi

# ---------------------------------------------------------------- 拷贝程序
log "拷贝程序到 $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/lib"
rm -rf "$PREFIX/lib/cutiepi_hud"
cp -r "$SRC/src/cutiepi_hud" "$PREFIX/lib/cutiepi_hud"
find "$PREFIX/lib" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX "$PREFIX/lib"

install -d -m 755 /usr/local/bin
install -m 755 "$SRC/src/cutiepi-hud" "$LAUNCHER"
install -m 755 "$SRC/src/cutiepi-screen-off" /usr/local/bin/cutiepi-screen-off
install -m 755 "$SRC/src/cutiepi-screen-on" /usr/local/bin/cutiepi-screen-on
log "已安装启动器 $LAUNCHER"
log "已安装息屏/亮屏命令 /usr/local/bin/cutiepi-screen-off、cutiepi-screen-on"

# ---------------------------------------------------------------- 备用叠加层（可选）
if [ "$WITH_OVERLAY" -eq 1 ]; then
    log "编译备用叠加层（独立角标，默认不启用）"
    if ! sh "$SRC/src/overlay/build.sh" "$PREFIX/hud-overlay"; then
        warn "叠加层编译失败（不影响状态栏图标）"
        warn "  sudo apt install gcc pkg-config libgtk-3-dev libgtk-layer-shell-dev"
        warn "  sudo sh $SRC/src/overlay/build.sh $PREFIX/hud-overlay"
    fi
else
    log "跳过备用叠加层（状态栏图标不需要编译；需要独立角标时加 --with-overlay）"
fi

# ---------------------------------------------------------------- 配置文件
if [ -f "$CONF" ]; then
    log "保留已有配置 $CONF"
else
    install -m 644 "$SRC/src/cutiepi-hud.conf" "$CONF"
    log "已写入默认配置 $CONF"
fi

# ---------------------------------------------------------------- 文档
install -d -m 755 "$PREFIX/doc"
if [ -f "$SRC/README.md" ]; then
    install -m 644 "$SRC/README.md" "$PREFIX/README.md"
fi
if [ -d "$SRC/docs" ]; then
    install -d -m 755 "$PREFIX/docs"
    install -m 644 "$SRC"/docs/* "$PREFIX/docs/" 2>/dev/null || true
    log "已安装文档与截图到 $PREFIX/docs"
fi

# ---------------------------------------------------------------- udev / autostart / unit / 钩子
install -m 644 "$SRC/udev/90-cutiepi-hud-backlight.rules" "$UDEV_RULE"
log "已安装 udev 规则 $UDEV_RULE"
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=backlight 2>/dev/null || true

# /dev/ttyS0 在树莓派 OS 13 上是 600 root:root，普通用户读不了 MCU
install -m 644 "$SRC/udev/99-cutiepi-hud-serial.rules" "$UDEV_SERIAL"
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=tty 2>/dev/null || true
if [ -e /dev/ttyS0 ]; then
    chgrp dialout /dev/ttyS0 2>/dev/null || true
    chmod 660 /dev/ttyS0 2>/dev/null || true
    log "已安装 udev 规则 $UDEV_SERIAL，并立即把 /dev/ttyS0 改成 dialout 可读写"
else
    log "已安装 udev 规则 $UDEV_SERIAL（/dev/ttyS0 当前不存在，重启后生效）"
fi

install -m 644 "$SRC/autostart/cutiepi-hud.desktop" "$AUTOSTART"
log "已安装开机自启 $AUTOSTART"

install -d -m 755 /etc/systemd/user
install -m 644 "$SRC/systemd/cutiepi-hud.service" "$USER_UNIT"
log "已安装 systemd 用户单元 $USER_UNIT"
# 让已在运行的桌面会话立刻看到新单元，并启用它（这样 systemctl --user restart 能管到）
TARGET_UID="$(id -u "$TARGET_USER" 2>/dev/null || echo "")"
if [ -n "$TARGET_UID" ] && [ -S "/run/user/$TARGET_UID/bus" ]; then
    sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    if sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        systemctl --user enable cutiepi-hud >/dev/null 2>&1; then
        log "已启用用户服务（登录后自动启动，可用 systemctl --user restart cutiepi-hud 控制）"
    fi
fi

install -d -m 755 "$(dirname "$SHUTDOWN_HOOK")"
install -m 755 "$SRC/shutdown-hook/cutiepi-hud-cutoff" "$SHUTDOWN_HOOK"
log "已安装关机钩子 $SHUTDOWN_HOOK（关机时通知 MCU 断电）"

# ---------------------------------------------------------------- sudoers
if [ -f "$SRC/sudoers/cutiepi-hud" ]; then
    sed "s/__USER__/$TARGET_USER/g" "$SRC/sudoers/cutiepi-hud" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    if visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
        log "已安装 sudoers 规则 $SUDOERS_FILE（长按电源键 6 秒可关机）"
    else
        rm -f "$SUDOERS_FILE"
        warn "sudoers 规则校验失败，已删除；长按电源键将不会执行关机"
    fi
fi

# ---------------------------------------------------------------- 用户组
log "把 $TARGET_USER 加入 dialout / video / input / render 组"
usermod -aG dialout,video,input,render "$TARGET_USER" || warn "加组失败，请手动检查"

# ---------------------------------------------------------------- 串口控制台
if [ "$FIX_CMDLINE" -eq 1 ]; then
    CMDLINE=""
    for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
        if [ -f "$candidate" ]; then
            CMDLINE="$candidate"
            break
        fi
    done
    if [ -n "$CMDLINE" ]; then
        if grep -q 'console=serial0' "$CMDLINE" 2>/dev/null; then
            cp -n "$CMDLINE" "$CMDLINE.cutiepi-bak" || true
            sed -i 's/console=serial0,[0-9]* //g; s/console=serial0,[0-9]*$//g' "$CMDLINE"
            log "已从 $CMDLINE 移除串口控制台（备份：$CMDLINE.cutiepi-bak）"
            warn "这一项要重启后才生效"
        else
            log "cmdline.txt 里没有串口控制台占用（OK）"
        fi
    else
        warn "没找到 cmdline.txt，请自行确认没有 console=serial0"
    fi
else
    log "按参数要求跳过 cmdline.txt 修改"
fi

# 串口登录程序（getty）必须先停掉：它会占用 MCU 的串口，
# 把 MCU 每秒上报的电量帧吃掉，并且会把 /dev/ttyS0 的权限改回 600 root:tty。
if systemctl disable --now serial-getty@ttyS0.service >/dev/null 2>&1; then
    log "已停用 serial-getty@ttyS0（它会抢 MCU 串口）"
fi
if systemctl mask serial-getty@ttyS0.service >/dev/null 2>&1; then
    log "已屏蔽 serial-getty@ttyS0，防止开机又被拉起来"
fi
udevadm trigger --sysname-match=ttyS0 2>/dev/null || true
sleep 1
if [ -e /dev/ttyS0 ]; then
    chgrp dialout /dev/ttyS0 2>/dev/null || true
    chmod 660 /dev/ttyS0 2>/dev/null || true
    log "  /dev/ttyS0 现在是：$(ls -l /dev/ttyS0 | awk '{print $1, $3, $4}')"
fi

# ---------------------------------------------------------------- 欠压提示
# 拔掉充电器瞬间电池→升压会掉一下电压，树莓派固件会弹 "Low voltage warning"。
# CutiePi 官方镜像用 avoid_warnings=2 关掉这个提示（只关显示，不影响实际保护）。
CONFIG_TXT="/boot/firmware/config.txt"
if [ ! -f "$CONFIG_TXT" ]; then
    CONFIG_TXT="/boot/config.txt"
fi
if [ -f "$CONFIG_TXT" ]; then
    if grep -q '^avoid_warnings' "$CONFIG_TXT"; then
        log "config.txt 已设置 avoid_warnings"
    else
        printf 'avoid_warnings=2\n' >> "$CONFIG_TXT"
        log "已给 $CONFIG_TXT 加上 avoid_warnings=2（关掉欠压提示，重启后生效）"
    fi
fi

# ---------------------------------------------------------------- 日志轮转
# 守护进程日志写在 ~/cutiepi-hud.log（systemd 单元的 StandardOutput=append），
# 这里加一条轮转规则，免得长期运行越滚越大。
if [ -d /etc/logrotate.d ]; then
    TARGET_HOME_FOR_LOG="$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6)"
    if [ -n "$TARGET_HOME_FOR_LOG" ]; then
        cat > /etc/logrotate.d/cutiepi-hud <<EOF
$TARGET_HOME_FOR_LOG/cutiepi-hud.log {
    weekly
    rotate 2
    maxsize 1M
    missingok
    notifempty
    copytruncate
}
EOF
        log "已安装日志轮转规则 /etc/logrotate.d/cutiepi-hud"
    fi
fi

# ---------------------------------------------------------------- 自检
echo
log "自检"
if "$LAUNCHER" --version >/dev/null 2>&1; then
    log "  Python 程序可执行：$("$LAUNCHER" --version)"
else
    warn "  Python 程序执行失败，请手动运行：$LAUNCHER --version"
fi
if su - "$TARGET_USER" -c "python3 -c 'import gi'" >/dev/null 2>&1; then
    log "  PyGObject 可用（状态栏图标就靠它）"
else
    warn "  PyGObject 不可用：状态栏图标无法显示（sudo apt install python3-gi）"
fi
if [ -x "$PREFIX/hud-overlay" ]; then
    log "  备用叠加层已编译：$PREFIX/hud-overlay"
else
    log "  备用叠加层未编译（正常，状态栏方式不需要）"
fi
[ -x "$(command -v wlr-randr 2>/dev/null)" ] && log "  wlr-randr：已安装" || warn "  wlr-randr：未安装（Wayland 自动旋转需要它）"
[ -x "$(command -v xrandr 2>/dev/null)" ] && log "  xrandr：已安装" || true

cat <<EOF

$(printf '\033[1;32m==>\033[0m') 安装完成。接下来的测试请用【平板上的普通用户】执行（不要 sudo）：

  1) 生成诊断报告
       cutiepi-hud diagnose

  2) 先确认状态栏能把电池图标显示出来（不接 MCU，纯演示）
       cutiepi-hud tray-test
     → 任务栏右侧图标区应该出现电池图标，悬停显示「电池 15%」之类，
       图标会从 15% 涨到 100% 再回来，50% 以下会带充电闪电。
       如果任务栏没有出现图标，请把这条命令的输出连同 diagnose 一起发我。

  3) 确认能转屏（不依赖传感器）
       cutiepi-hud test-rotate 90
       cutiepi-hud test-rotate 0

  4) 确认加速度计能被读到（把平板慢慢转一圈，看"主轴/姿态"是否跟着变）
       cutiepi-hud monitor

  5) 确认能读到电池（插拔充电器各看一次）
       cutiepi-hud battery

  6) 正式跑起来（前台运行，Ctrl+C 退出）
       cutiepi-hud run

  7) 没问题就重启；登录后任务栏里应该有电池图标，转动平板屏幕会自动转

注意：第 1 次装机建议先跑一次姿态校准（约 20 秒）
       cutiepi-hud calibrate

日志查看：journalctl --user -f -u cutiepi-hud   （用 systemd 启动时）
         或者直接看前台输出

EOF
