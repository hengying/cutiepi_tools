#!/bin/sh
# 编译右上角叠加层。
#   用法：sh build.sh [输出文件名]
# 依赖：gcc、pkg-config、libgtk-3-dev；可选的 libgtk-layer-shell-dev
set -e

cd "$(dirname "$0")"

CC=${CC:-cc}
OUT=${1:-hud-overlay}

if ! command -v "$CC" >/dev/null 2>&1; then
    echo "错误：找不到 C 编译器（sudo apt install gcc）" >&2
    exit 3
fi
if ! command -v pkg-config >/dev/null 2>&1; then
    echo "错误：找不到 pkg-config（sudo apt install pkg-config）" >&2
    exit 3
fi
if ! pkg-config --exists gtk+-3.0; then
    echo "错误：缺少 GTK3 开发包（sudo apt install libgtk-3-dev）" >&2
    exit 3
fi

DEFS=""
LIBS="$(pkg-config --cflags --libs gtk+-3.0)"

if pkg-config --exists gtk-layer-shell-0; then
    DEFS="-DHAVE_LAYER_SHELL"
    LIBS="$LIBS $(pkg-config --cflags --libs gtk-layer-shell-0)"
    echo "[build] gtk-layer-shell: 已找到 —— Wayland 下可精确固定在右上角"
else
    echo "[build] gtk-layer-shell: 未找到 —— Wayland 下位置将由合成器决定"
    echo "[build]   需要正确位置请先装：sudo apt install libgtk-layer-shell-dev"
fi

# shellcheck disable=SC2086
$CC -O2 -Wall -Wextra $DEFS -o "$OUT" hud-overlay.c $LIBS -lm

echo "[build] 已生成 $(pwd)/$OUT"
