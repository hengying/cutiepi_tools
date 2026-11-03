"""把屏幕旋转应用到当前的显示服务器。

支持两种桌面：

* Wayland（labwc / wayfire / sway 等 wlroots 系）→ wlr-randr --transform
* X11 → xrandr --rotate，并同步把触摸屏的坐标变换矩阵改掉

角度含义：程序内部用“把画面顺时针转多少度”表示姿态（与 wlr-randr 的
transform 数值一致）；X11 这边换算成 left/right/inverted。

如果真机上左右方向反了，把配置里的 display/swap_lr 改成 true 即可。
"""

import os
import re
import shutil
import subprocess

from . import util

TIMEOUT = 10

# X11：xrandr 的 rotate 名字 -> 触摸屏坐标变换矩阵（参照 X.Org/Arch Wiki 的标准值）
X11_TOUCH_MATRIX = {
    "normal": "1 0 0 0 1 0 0 0 1",
    "left": "0 -1 1 1 0 0 0 0 1",
    "right": "0 1 0 -1 0 1 0 0 1",
    "inverted": "-1 0 1 0 -1 1 0 0 1",
}


def _run(command, timeout=TIMEOUT):
    """执行命令，返回 (returncode, stdout, stderr)。命令不存在时 returncode 为 127。"""
    if not command or not shutil.which(command[0]):
        return 127, "", "%s: 未安装" % command[0]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "", "%s: 超时" % command[0]
    except OSError as exc:
        return 126, "", "%s: %s" % (command[0], exc)


def runtime_dir():
    return util.runtime_dir() or "/tmp"


def wayland_socket_present():
    display = os.environ.get("WAYLAND_DISPLAY")
    runtime = runtime_dir()
    if display:
        if os.path.isabs(display):
            return os.path.exists(display)
        return os.path.exists(os.path.join(runtime, display))
    for name in ("wayland-0", "wayland-1", "wayland-2"):
        if os.path.exists(os.path.join(runtime, name)):
            return True
    return False


def x11_socket_present():
    display = os.environ.get("DISPLAY", "")
    match = re.match(r"^:(\d+)", display)
    if match:
        return os.path.exists("/tmp/.X11-unix/X%s" % match.group(1))
    return os.path.exists("/tmp/.X11-unix/X0")


class Rotator(object):
    def __init__(self, output="auto", backend="auto", swap_lr=False, touch_matrix=True, dry_run=False):
        self.requested_output = output or "auto"
        self.requested_backend = backend or "auto"
        self.swap_lr = bool(swap_lr)
        self.touch_matrix = bool(touch_matrix)
        self.dry_run = bool(dry_run)
        self.backend = None
        self.output = None
        self.last_degrees = None
        self.last_error = None

    # -- 探测 -------------------------------------------------------------
    def detect(self, force=False):
        """探测后端与输出名，返回 (backend, output)。失败时 backend 为 None。"""
        if self.backend and self.output and not force:
            return self.backend, self.output

        backend = self.requested_backend
        if backend == "auto":
            if wayland_socket_present() and shutil.which("wlr-randr"):
                backend = "wayland"
            elif x11_socket_present() and shutil.which("xrandr"):
                backend = "x11"
            elif shutil.which("wlr-randr"):
                backend = "wayland"
            elif shutil.which("xrandr"):
                backend = "x11"
            else:
                backend = None

        if backend == "wayland":
            output = self._output_wayland()
        elif backend == "x11":
            output = self._output_x11()
        else:
            output = None

        self.backend = backend if output else None
        self.output = output
        return self.backend, self.output

    def _pick(self, names):
        if self.requested_output not in ("auto", "", None) and self.requested_output in names:
            return self.requested_output
        for name in names:
            if name.upper().startswith("DSI"):
                return name
        return names[0] if names else None

    def _output_wayland(self):
        code, out, err = _run(["wlr-randr"])
        if code != 0:
            self.last_error = "wlr-randr 执行失败: %s" % (err.strip() or code)
            return None
        names = []
        for line in out.splitlines():
            if not line or line[0].isspace():
                continue
            match = re.match(r"^(\S+)", line)
            if match:
                names.append(match.group(1))
        if not names:
            self.last_error = "wlr-randr 没有报告任何输出（合成器可能不支持 wlr-output-management）"
            return None
        return self._pick(names)

    def _output_x11(self):
        code, out, err = _run(["xrandr", "--query"])
        if code != 0:
            self.last_error = "xrandr --query 执行失败: %s" % (err.strip() or code)
            return None
        names = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "connected":
                names.append(parts[0])
        if not names:
            self.last_error = "xrandr 没有报告任何已连接的输出"
            return None
        return self._pick(names)

    # -- 应用 -------------------------------------------------------------
    def transform_wayland(self, degrees):
        degrees = int(degrees) % 360
        if degrees in (90, 270) and self.swap_lr:
            degrees = 270 if degrees == 90 else 90
        return {0: "normal", 90: "90", 180: "180", 270: "270"}.get(degrees, "normal")

    def rotate_name_x11(self, degrees):
        degrees = int(degrees) % 360
        if degrees in (90, 270) and self.swap_lr:
            degrees = 270 if degrees == 90 else 90
        return {0: "normal", 90: "right", 180: "inverted", 270: "left"}.get(degrees, "normal")

    def apply(self, degrees):
        """应用角度，返回 True/False。"""
        backend, output = self.detect()
        if not backend:
            self.last_error = self.last_error or "找不到可用的显示服务器（没有 Wayland 也没有 X11）"
            return False

        if self.dry_run:
            self.last_degrees = int(degrees) % 360
            return True

        if backend == "wayland":
            ok = self._apply_wayland(output, degrees)
            rotate_name = None
        else:
            rotate_name = self.rotate_name_x11(degrees)
            ok = self._apply_x11(output, rotate_name)

        if ok:
            self.last_degrees = int(degrees) % 360
            self.last_error = None
            if backend == "x11" and self.touch_matrix:
                self._apply_touch_matrix(rotate_name)
        return ok

    def _apply_wayland(self, output, degrees):
        transform = self.transform_wayland(degrees)
        code, out, err = _run(["wlr-randr", "--output", output, "--transform", transform])
        if code != 0:
            self.last_error = "wlr-randr --transform %s 失败: %s" % (transform, err.strip() or code)
            return False
        return True

    def _apply_x11(self, output, rotate_name):
        code, out, err = _run(["xrandr", "--output", output, "--rotate", rotate_name])
        if code != 0:
            self.last_error = "xrandr --rotate %s 失败: %s" % (rotate_name, err.strip() or code)
            return False
        return True

    def _apply_touch_matrix(self, rotate_name):
        """X11 下把触摸屏的坐标变换矩阵改成与屏幕一致。Wayland 由合成器自动处理。"""
        matrix = X11_TOUCH_MATRIX.get(rotate_name)
        if not matrix:
            return False
        code, out, _err = _run(["xinput", "list", "--short"])
        if code != 0:
            return False
        device = None
        for line in out.splitlines():
            lower = line.lower()
            if "touch" in lower or "goodix" in lower:
                match = re.search(r"\bid=(\d+)", line)
                if match:
                    device = match.group(1)
                    break
        if not device:
            return False
        code, _out, err = _run(
            ["xinput", "set-prop", device, "Coordinate Transformation Matrix"] + matrix.split()
        )
        if code != 0:
            self.last_error = "设置触摸矩阵失败: %s" % (err.strip() or code)
        return code == 0

    # -- 查询 -------------------------------------------------------------
    def current_transform(self):
        """返回当前输出的 transform 文本（用于诊断）。"""
        backend, output = self.detect()
        if backend == "wayland":
            code, out, _err = _run(["wlr-randr"])
            if code == 0:
                in_output = False
                for line in out.splitlines():
                    if line and not line[0].isspace():
                        in_output = line.split()[0] == output
                        continue
                    if in_output and "Transform:" in line:
                        return line.split(":", 1)[1].strip()
            return None
        if backend == "x11":
            code, out, _err = _run(["xrandr", "--query"])
            if code == 0:
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == output and parts[1] == "connected":
                        # 形如：DSI-1 connected primary 800x1280+0+0 left (normal left ...)
                        if parts[3] in X11_TOUCH_MATRIX:
                            return parts[3]
            return None
        return None
