# Changelog

All notable changes to this project are documented here.

## v1.4.2 — 2026-11-23

### Added

- **Rotation lock in the system tray.** A third icon toggles between *follow gravity*
  (black circular arrow) and *locked* (grey arrow with a black slash over it, i.e. the
  classic "disabled" symbol). While locked the orientation is frozen; if something else
  changes the output transform the daemon corrects it within 5 seconds. Switching back
  to *allowed* applies the current posture immediately instead of waiting for movement.
- The lock flag and the last applied angle are stored in the user config
  (`[rotation] locked` / `last_angle`). The angle is written on **every** successful
  rotation (plus once at exit), so a power cut does not lose it.
- On login the autostart entry runs `cutiepi-hud rotate-apply-saved` before starting
  the daemon, so the first frame already shows the orientation you left with.
- New commands: `rotate-lock [on|off|toggle|status]`, `rotate-apply-saved`.
- Toggling the icon shows a **2.5-second desktop notification** ("rotation disabled" /
  "rotation enabled"). Tray `ToolTip` stays permanently empty: on a touch screen the
  panel never gets a pointer-leave event, so a tooltip bubble would stay on screen
  forever and keep showing stale text. A self-drawn bubble (`toast.py`) is the fallback
  when no notification daemon is running.
- `write_user_overrides()` now takes a file lock and replaces the config atomically, so
  the daemon writing `last_angle` cannot race with `calibrate`.

### Changed

- New configuration keys: `hud/rotation_icon`, `rotation/locked`, `rotation/last_angle`.
- Tests: 275 → 326 checks.

## v1.3.1 — 2026-11-03

### Added

- Chinese README (`README.zh-CN.md`), with a language switcher at the top of both files.

## v1.3.0 — 2026-11-03

First public release.

### Added

- **Auto-rotation** for all four orientations (MPU6050 via IIO sysfs,
  `wlr-randr` on Wayland, `xrandr` + touch matrix on X11), with hysteresis,
  a flat-lying fallback and a startup baseline.
- **Battery + charging indicator in the system tray** using the
  StatusNotifierItem protocol; digits are drawn into the icon (5×7 bitmap font),
  green while charging, tooltip `Battery 87% · Charging`.
- **Brightness tray icon**: sun icon whose size follows the level, click opens a
  1–15 slider; the value is written straight to
  `/sys/class/backlight/*/brightness`.
- **Power button handling**:
  - short press toggles the backlight (`cutiepi-screen-on/off` wrappers included),
  - double press (or the MCU's 6-second request) opens a full-screen shutdown
    confirmation dialog that auto-cancels after 15 s,
  - the MCU's power-off request is ACKed with *cancel* first, so the decision
    stays on the Pi and the MCU never hard-cuts power behind our back.
- **Clean power-off**: a `systemd` shutdown hook sends the MCU cutoff frame, so a
  normal `poweroff` / `halt` / menu shutdown really disconnects the battery.
- **Calibration commands**: `calibrate` (accelerometer axes → posture) and
  `rotate-calibrate` (posture → rotation, with a `--full` mode), plus
  `rotate-flip` for a one-shot 180° correction.
- **Diagnostics**: `diagnose`, `battery`, `monitor`, `tray-test`, `demo`,
  `screen`, `brightness`, `power-dialog`.
- **Installer / uninstaller** handling dependencies, udev rules, the serial getty,
  `sudoers`, autostart, the systemd user unit, logrotate and `avoid_warnings=2`.
- **275 pure-logic unit tests** (`cutiepi-hud/tests/run_tests.py`), runnable on any
  machine without a tablet.

### Notes

- The MCU only reports the power button at 6 seconds; a shorter hold cannot be
  detected from the Pi side (the button is wired to the microcontroller).
- Default rotation angles were measured on a CutiePi CM4; other panel mountings
  may need `rotate-flip` or `rotate-calibrate`.
- On-screen strings and console messages are in Chinese; the documentation is
  bilingual (`README.md` English, `README.zh-CN.md` Chinese).
