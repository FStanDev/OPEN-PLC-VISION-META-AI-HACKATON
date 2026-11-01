# stimulus_service

Tiny HTTP service (stdlib only) that presses the bench buttons for
acceptance tests: `POST /press {"button": "start_btn"}` drives the mapped
stimulus pin LOW, `POST /release` lets go (high impedance). `GET /health`
lists the known buttons. Buttons resolve through `io_map.yaml` `stimulus`
(`drives:`), unknown names are a 400, never a traceback.

## Run on the Pi

```bash
cd ~/Developer/vision-plc-agent
sudo apt install -y python3-lgpio   # gpiozero's Pi 5 pin factory (no pip wheel)
python3 -m venv --system-site-packages .venvs/stimulus
.venvs/stimulus/bin/pip install -r pi/stimulus_service/requirements.txt
PI_BIND=<tailscale-ip> .venvs/stimulus/bin/python pi/stimulus_service/service.py
```

Smoke test from the laptop (replace the host):

```bash
curl http://<pi>:8081/health
curl -X POST http://<pi>:8081/press -d '{"button":"start_btn"}'
curl -X POST http://<pi>:8081/release -d '{"button":"start_btn"}'
```

## Loopback check (no scope needed)

With the GPIO plugin running and a program that maps `%IX0.0` to `%QX0.0`,
pressing `start_btn` here must light `lamp_red` — or read discrete input 0
over Modbus while pressing.
