# Third-party dependencies

Every dependency and its licence. Licences verified from installed package
metadata (exact resolved versions are pinned in `uv.lock`).

## Runtime

| Package | Constraint | Licence | Use |
| --- | --- | --- | --- |
| pydantic | >=2.0 | MIT | Task spec + I/O map schemas |
| PyYAML | >=6.0 | MIT | YAML parsing |
| httpx | >=0.28 | BSD-3-Clause | OpenPLC REST client |
| pymodbus | >=3.0 | BSD-3-Clause | Tag readback over Modbus TCP |
| opencv-python-headless | >=4.10,<6 | Apache-2.0 | Frame decode, brightness + angle measurement |
| numpy | >=1.26 | BSD-3-Clause | Pixel stats for vision measurement |

## Pi services (installed on the Pi from `pi/*/requirements.txt`, not in `uv.lock`)

| Package | Constraint | Licence | Use |
| --- | --- | --- | --- |
| gpiozero | >=2.0,<3 | BSD-3-Clause | GPIO in gpio_plugin + stimulus_service |
| lgpio (apt `python3-lgpio`; venv needs `--system-site-packages`) | 0.2.2 (trixie) | Unlicense | gpiozero pin factory on Pi 5 (stimulus_service) |
| smbus2 | >=0.6,<1 | MIT | PCA9685 I2C in gpio_plugin |
| opencv-python-headless | >=4.10,<6 | Apache-2.0 | C920 capture in frame_service |

The Pi venvs also reuse pydantic + PyYAML (licences above) to read `io_map.yaml`.

## Development

| Package | Constraint | Licence | Use |
| --- | --- | --- | --- |
| pytest | >=8.0 | MIT | Tests |
| ruff | >=0.8 | MIT | Lint + format |

## Build

| Package | Licence | Use |
| --- | --- | --- |
| hatchling | MIT | Build backend |

## Toolchain (binaries on PATH, not Python deps)

| Tool | Version | Licence | Use |
| --- | --- | --- | --- |
| STruC++ (`strucpp-linux-arm64.tar.gz` from GitHub releases) | 0.7.0 | GPL-3.0 (binary, used as a tool); generated code covered by the STruC++ Runtime Library Exception | ST compile + unit tests |
