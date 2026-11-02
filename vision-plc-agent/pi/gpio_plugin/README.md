# gpio_plugin

OpenPLC Runtime v4 Python plugin: copies physical button levels into `%IX`
and `%QX`/`%QW` image values out to the lamps (GPIO17/27/22) and the servo
(PCA9685 channel 2). Driven entirely by `io_map.yaml` — no pins in code.

Design notes:

- Python plugins get no per-scan hook (`cycle_start`/`cycle_end` are
  native-only), so `start_loop` runs a 10 ms polling thread.
- `stop_loop`/`cleanup` drive hardware to `io_map.safe_state`.
- The servo is optional: if I2C fails, the plugin logs loudly and runs lamps
  + buttons only.
- Negative `%QW0` values (ST `INT` is signed) clamp to 0; anything over 180
  clamps to 180.

## Install on the Pi

```bash
cd ~/Developer/vision-plc-agent   # wherever you cloned this repo
python3 -m venv .venvs/gpio_plugin
.venvs/gpio_plugin/bin/pip install -r pi/gpio_plugin/requirements.txt
# let the plugin import the runtime's SafeBufferAccess helpers:
echo /home/stan/Developer/openplc-runtime/core/src/drivers/plugins/python \
  > .venvs/gpio_plugin/lib/python3.13/site-packages/openplc-shared.pth
```

Register in `/home/stan/Developer/openplc-runtime/plugins.conf` (one line,
adjust the checkout path):

```text
gpio_plugin,/home/stan/Developer/vision-plc-agent/pi/gpio_plugin/gpio_plugin.py,1,0,,/home/stan/Developer/vision-plc-agent/.venvs/gpio_plugin
```

(Fields: name, path, enabled, type 0=Python, config unused, venv.)

```bash
sudo systemctl restart openplc-runtime.service
journalctl -u openplc-runtime.service --since '2 min ago' | grep gpio_plugin
```

Expect `live: 6 bindings, poll 10 ms` (5 without the servo). If `%QW0` stays
dark, check the bus number with `i2cdetect -l` — it is `I2C_BUS` in
`pi/pca9685.py`.

## Known limitation

The plugin cannot see PLC run state (the runtime exposes no query to
plugins), so stopping the PLC via the API leaves last lamp values on the
pins until the agent forces the safe state over Modbus after stop (agent
loop, milestone 6). `stop_btn` safe-off while running is guaranteed by the
generated ST itself, in one scan.
