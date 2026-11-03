# cutiepi-hud

The installable tool. See the [top-level README](../README.md) for the full story,
screenshots and configuration reference.

```bash
sudo ./install.sh          # dependencies, udev, sudoers, systemd unit, hook
sudo reboot

cutiepi-hud diagnose       # check everything
cutiepi-hud tray-test      # show a demo battery icon in the panel
```

## What is inside

| Path | Contents |
|---|---|
| `src/cutiepi_hud/` | Python modules: `daemon`, `mcu`, `orient`, `iio`, `rotate`, `backlight`, `tray`, `brightness_tray`, `brightness_popup`, `dialog`, `session`, `state`, `config`, `cli` |
| `src/cutiepi-hud` | launcher installed to `/usr/local/bin/cutiepi-hud` |
| `src/cutiepi-screen-on`, `src/cutiepi-screen-off` | two-line wrappers for scripting |
| `src/cutiepi-hud.conf` | default configuration, installed to `/etc/cutiepi-hud.conf` |
| `src/overlay/` | optional C fallback indicator (`--with-overlay`), only needed when the panel has no tray area |
| `systemd/` | user unit (`cutiepi-hud.service`), started at login, logs to `~/cutiepi-hud.log` |
| `autostart/` | desktop autostart entry that simply starts the unit (belt and braces) |
| `udev/` | backlight permissions (video group) and `/dev/ttyS0` (dialout group) |
| `sudoers/` | passwordless `systemctl poweroff` for the power button |
| `shutdown-hook/` | sends the MCU cutoff frame at the end of a shutdown |
| `tests/` | `run_tests.py`: 275 pure-logic checks, no tablet required |

## Notes

* Code comments and console messages are in Chinese; the documentation is English.
* The installer keeps an existing `/etc/cutiepi-hud.conf`. When upgrading from an
  older copy, compare it with `src/cutiepi-hud.conf` — the defaults carry the
  measured rotation angles for this hardware.
* After editing any configuration file: `systemctl --user restart cutiepi-hud`.
