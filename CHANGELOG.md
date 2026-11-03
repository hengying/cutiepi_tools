# Changelog

All notable changes to this project are documented here.

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
- On-screen strings and console messages are in Chinese; the documentation is in
  English.
