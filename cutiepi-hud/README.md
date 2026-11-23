# cutiepi-hud

The installable tool. See the [top-level README](../README.md) for the full story,
screenshots and configuration reference.

## Before installing (fresh OS)

A freshly installed Raspberry Pi OS still needs the **display** and **gyro** overlays
enabled — they are `config.txt` lines, the complete file is in
[`../configs/config.txt`](../configs/config.txt):

```ini
dtoverlay=cutiepi-panel             # DSI panel
dtoverlay=i2c5,pins_10_11
dtoverlay=mpu6050-i2c5,interrupt=27 # gyro / accelerometer
```

If the desktop looks wrong afterwards (a phantom extra output, taskbar in the wrong
place), right-click the desktop, open the display settings, disable the `HDMI2`
output and reboot. More detail: [`../after_OS_installed.md`](../after_OS_installed.md).

## Install

```bash
sudo ./install.sh          # dependencies, udev, sudoers, systemd unit, hook
sudo reboot

cutiepi-hud diagnose       # check everything
cutiepi-hud tray-test      # show a demo battery icon in the panel
```

## Everyday commands

```bash
# rotation
cutiepi-hud rotate-lock              # print state + last angle
cutiepi-hud rotate-lock on|off       # freeze / follow gravity
cutiepi-hud rotate-apply-saved       # re-apply the saved angle (autostart does this)

# tray helpers
cutiepi-screen-off / cutiepi-screen-on     # backlight off / on
cutiepi-hud brightness [1-15|+1|-1|popup]  # backlight level, or the slider
```

## What is inside

| Path | Contents |
|---|---|
| `src/cutiepi_hud/` | Python modules: `daemon`, `mcu`, `orient`, `iio`, `rotate`, `lockstate`, `backlight`, `tray`, `rotation_tray`, `brightness_tray`, `brightness_popup`, `dialog`, `toast`, `notify`, `trayprocess`, `session`, `state`, `config`, `cli` |
| `src/cutiepi-hud` | launcher installed to `/usr/local/bin/cutiepi-hud` |
| `src/cutiepi-screen-on`, `src/cutiepi-screen-off` | two-line wrappers for scripting |
| `src/cutiepi-hud.conf` | default configuration, installed to `/etc/cutiepi-hud.conf` |
| `src/overlay/` | optional C fallback indicator (`--with-overlay`), only needed when the panel has no tray area |
| `systemd/` | user unit (`cutiepi-hud.service`), started at login, logs to `~/cutiepi-hud.log` |
| `autostart/` | desktop autostart entry: applies the saved rotation, then starts the unit |
| `udev/` | backlight permissions (video group) and `/dev/ttyS0` (dialout group) |
| `sudoers/` | passwordless `systemctl poweroff` for the power button |
| `shutdown-hook/` | sends the MCU cutoff frame at the end of a shutdown, plus the initramfs hook that keeps it runnable after the root switch |
| `tests/` | `run_tests.py`: 326 pure-logic checks, no tablet required |

## Notes

* Code comments and console messages are in Chinese; the documentation is English.
* The installer keeps an existing `/etc/cutiepi-hud.conf`. When upgrading from an
  older copy, compare it with `src/cutiepi-hud.conf` — the defaults carry the
  measured rotation angles for this hardware.
* After editing any configuration file: `systemctl --user restart cutiepi-hud`.
* Tray icons intentionally publish no `ToolTip`: on a touch screen the panel never
  sees a pointer-leave event, so the tooltip bubble would stay on screen forever.
  State changes are reported with a 2.5-second desktop notification instead.
