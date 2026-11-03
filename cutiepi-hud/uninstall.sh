#!/bin/bash
# CutiePi HUD 卸载脚本：sudo ./uninstall.sh
#
# 会删掉 install.sh 装的所有东西；/etc/cutiepi-hud.conf 和用户的
# ~/.config/cutiepi-hud/config.ini 默认保留（加 --purge 一起删）。
set -uo pipefail

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
LOGROTATE="/etc/logrotate.d/cutiepi-hud"
SCREEN_OFF="/usr/local/bin/cutiepi-screen-off"
SCREEN_ON="/usr/local/bin/cutiepi-screen-on"

PURGE=0
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        --user) shift; TARGET_USER="$1" ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行：sudo ./uninstall.sh" >&2
    exit 1
fi

if [ -n "${TARGET_USER:-}" ] && [ "$TARGET_USER" != "root" ]; then
    UID_NUM="$(id -u "$TARGET_USER" 2>/dev/null || true)"
    if [ -n "$UID_NUM" ]; then
        log "停止 $TARGET_USER 的用户服务"
        sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$UID_NUM" \
            systemctl --user disable --now cutiepi-hud 2>/dev/null || true
        pkill -u "$TARGET_USER" -f 'cutiepi_hud' 2>/dev/null || true
        pkill -u "$TARGET_USER" -f 'hud-overlay' 2>/dev/null || true
    fi
fi

for path in "$LAUNCHER" "$SCREEN_OFF" "$SCREEN_ON" "$AUTOSTART" "$USER_UNIT" "$UDEV_RULE" \
            "$UDEV_SERIAL" "$SHUTDOWN_HOOK" "$SUDOERS_FILE" "$LOGROTATE"; do
    if [ -e "$path" ]; then
        rm -f "$path"
        log "已删除 $path"
    fi
done

if [ -d "$PREFIX" ]; then
    rm -rf "$PREFIX"
    log "已删除 $PREFIX"
fi

if [ "$PURGE" -eq 1 ]; then
    rm -f "$CONF"
    log "已删除 $CONF"
    if [ -n "${TARGET_USER:-}" ] && [ "$TARGET_USER" != "root" ]; then
        TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
        rm -rf "$TARGET_HOME/.config/cutiepi-hud"
        log "已删除 $TARGET_HOME/.config/cutiepi-hud"
        rm -f "$TARGET_HOME/cutiepi-hud.log"
        log "已删除 $TARGET_HOME/cutiepi-hud.log"
    fi
else
    log "保留配置文件 $CONF（加 --purge 可一起删除）"
fi

udevadm control --reload-rules 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

log "卸载完成。用户组（dialout 等）未改动，属正常设置，可保留。"
