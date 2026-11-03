# configs

## `config.txt`

The complete `/boot/firmware/config.txt` of a working CutiePi CM4 tablet running
Raspberry Pi OS 13 (trixie). It is the stock file plus the CutiePi panel/IMU/MCU
overlays; lines that matter for this project are marked `# [cutiepi-hud]`.

To use it:

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
sudo cp config.txt /boot/firmware/config.txt
sudo reboot
```

Keep your own backlight/audio/camera tweaks if you have any — the file is a
reference, not a mandatory replacement.

### The five lines that matter

| Line | Why |
|---|---|
| `dtparam=i2c_arm=on` | I2C bus used by the panel and the IMU |
| `dtoverlay=cutiepi-panel` | enables the DSI panel (800×1280) |
| `dtoverlay=i2c5,pins_10_11` | second I2C bus for the accelerometer |
| `dtoverlay=mpu6050-i2c5,interrupt=27` | MPU6050 accelerometer → `/sys/bus/iio/...` |
| `enable_uart=1` + `dtoverlay=uart1` | UART for the MCU → `/dev/serial0` |
| `avoid_warnings=2` | silences the under-voltage popup on charger removal |

Without `mpu6050-i2c5` the rotation thread reports "no accelerometer"; without
`enable_uart`/`uart1` there is no `/dev/serial0` and therefore no battery reading.

## `../cutiepi-hud/src/cutiepi-hud.conf`

The fully commented default configuration of the daemon (installed to
`/etc/cutiepi-hud.conf`). Copy the keys you want to change into
`~/.config/cutiepi-hud/config.ini` instead of editing the system file, so an
upgrade of the package does not fight your edits.
