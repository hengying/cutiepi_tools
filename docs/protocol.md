# Technical notes

Everything in this file was established on a real CutiePi CM4 tablet by
observing the hardware — it is the reference the code is built on.

## 1. MCU serial protocol

The tablet's microcontroller talks to the Pi over `/dev/serial0` at **115200 8N1**,
raw bytes (no shell, no getty — a login prompt on `ttyS0` will silently eat the
frames and reset the port permissions, which is why the installer masks
`serial-getty@ttyS0`).

### Frame format

```
5A A5 | type | len_lo len_hi | payload[len] | crc
```

* `5A A5` — magic
* `type` — message type (see below)
* `len` — payload length, little endian
* `crc` — `sum(all bytes) & 0xFF`

### Message types

| type | direction | payload | meaning |
|---|---|---|---|
| `1` | MCU → Pi | 1 byte | button event: `1` short press, `2` power-on (3 s hold, only when off), `3` power-off request (6 s hold) |
| `2` | MCU → Pi | 2 bytes | battery voltage in mV, little endian |
| `3` | MCU → Pi | 1 byte | charge state: `4` charging, `5` not charging |
| `4` | MCU → Pi | ASCII | firmware version string, e.g. `Ver0.1 SN:123456` |
| `5` | Pi → MCU | `01 00` | **cut power** (used at the very end of a shutdown) |

### ACK frames (Pi → MCU, always `type = 1`)

| ACK | Sent when |
|---|---|
| `F1` | after any button event |
| `F3` | shutdown confirmed |
| `FC` | shutdown cancelled (also used to keep the decision on the Pi side) |
| `F4` / `F5` | after a charge-on / charge-off report |
| `F6` | after the version report |
| `F7` | cutoff — the frame that actually disconnects the battery |

### Cadence and button behaviour (measured)

* Battery + charge frames arrive **once per second** (jitter < 5 ms) once the
  serial getty is gone.
* While the power button is held, the MCU **repeats** the `type=1 value=3` frame
  every few tens of milliseconds. Handling code must therefore make the whole
  shutdown flow idempotent per press, or the second frame will cancel the dialog
  and shut down immediately.
* **6 s** — power-off *request*; the Pi decides.
* **10 s** — the MCU cuts power **in hardware**, no message is sent. Software
  cannot prevent this.
* The button is wired to the MCU only; there is no Pi GPIO to poll, so no earlier
  press detection is possible.

## 2. Battery voltage → percentage

The curve is the same one the official `cutiepi-shell` uses, so readings match
what users were used to. Values are linearly interpolated between the points and
smoothed with a 15-sample moving average.

| Voltage (V) | Percent | Voltage (V) | Percent |
|---|---|---|---|
| 3.00 (floor) | 0 | 3.63 | 50 |
| 3.48 | 5 | 3.65 | 55 |
| 3.49 | 10 | 3.67 | 60 |
| 3.51 | 15 | 3.69 | 65 |
| 3.53 | 20 | 3.73 | 70 |
| 3.55 | 25 | 3.76 | 75 |
| 3.56 | 30 | 3.80 | 80 |
| 3.59 | 35 | 3.82 | 85 |
| 3.60 | 40 | 3.86 | 90 |
| 3.61 | 45 | 3.88 | 95 |
| | | 3.89 | 100 |

Readings below 2.0 V are treated as "no battery / noise".

## 3. Posture → rotation

The accelerometer is an MPU6050 exposed through IIO (`iio:device0`,
`in_accel_*_raw`). The dominant axis and its sign identify the posture:

| Axis | Posture | Physical meaning | Rotation applied |
|---|---|---|---|
| `y-` | `normal` | portrait, camera edge up | 180° |
| `x-` | `left-up` | landscape, left edge up | 270° |
| `y+` | `bottom-up` | portrait upside down | 0° |
| `x+` | `right-up` | landscape, right edge up | 90° |

Notes:

* The angles above were measured on the reference unit; the panel is mounted
  180° from the naive assumption, which is why `normal` is `180°` and not `0°`.
  On a unit that boots the other way up, `cutiepi-hud rotate-flip` fixes all
  four directions at once, and `rotate-calibrate` measures them interactively.
* Rotating the tablet 90° clockwise must rotate the **content** 90°
  counter-clockwise; that relation is what `rotate-calibrate` relies on to derive
  three angles from a single measurement.
* When the tablet lies flat, gravity is on the Z axis and no orientation can be
  inferred; the daemon applies the portrait angle as a baseline instead of
  leaving the compositor's default (which is upside down on this hardware).
* The touchscreen follows the output transform automatically on Wayland
  (`wlr-randr --transform`), so pointer positions stay under the finger.

## 4. System tray icons (SNI)

The panel (`wf-panel-pi`) is a StatusNotifierItem host. What actually works:

* Export the item on a **private D-Bus connection per icon**, at path
  `/StatusNotifierItem`, interface `org.kde.StatusNotifierItem`. Two icons in one
  process collide on the object path, which is why the brightness icon lives in
  its own process.
* `IconName` may be empty; the panel then uses `IconPixmap`, which must be
  **network byte order ARGB32** (`a(iiay)` with `(width, height, bytes)`), rows
  top to bottom, no padding.
* `ToolTip` is `(sa(iiay)ss)` — `(icon, pixmaps, title, text)`.
* The panel picks the foreground colour from the GTK theme; for a light panel the
  icons must be drawn dark. `hud/icon_color = auto` probes the theme once and
  caches the result.
* Registration must be retried until `org.kde.StatusNotifierWatcher` exists — at
  login the daemon usually starts before the panel does. Watch
  `NameOwnerChanged` to re-register when the panel restarts.
* `RegisteredStatusNotifierItems` lists entries either as `bus-name` or
  `bus-name/object-path`; both mean "registered", so compare accordingly.

## 5. Why a shutdown hook

A normal `systemctl poweroff` (or `halt`, or the desktop menu) otherwise leaves the
MCU powered: the tablet appears off, but the battery is still connected and the MCU
keeps drawing current. The official image solved this with a `cutoff` helper run
from a shutdown hook; this project does the same:

```
/usr/lib/systemd/system-shutdown/cutiepi-hud-cutoff
```

systemd calls it as the very last step, with `poweroff`, `reboot`, `halt` or
`kexec` as `$1`. The script sends the `type=5` frame three times (`F7`) — and skips
`reboot` on purpose, because a reboot must not disconnect the battery.

`CUTIEPI_HUD_CUTOFF_DRYRUN=1` prints the frame and the ports it would use instead of
sending it, which is handy for checking the plumbing without powering anything off.
