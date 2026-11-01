# frame_service

Logitech C920 capture: `GET /frame` returns one still JPEG (what the vision
step consumes), `GET /preview` is an MJPEG stream for the dashboard/human
check, `GET /health` is a liveness probe. Exposure, white balance and focus
are locked to manual at startup (best-effort; refusals go to stderr).
A failed grab is a 502 — the service never substitutes a stock image.

## Run on the Pi

Plug in the C920, then:

```bash
cd ~/Developer/vision-plc-agent
python3 -m venv .venvs/frames
.venvs/frames/bin/pip install -r pi/frame_service/requirements.txt
ls /dev/video*   # note which node the C920 got (video0 here)
PI_BIND=<tailscale-ip> .venvs/frames/bin/python pi/frame_service/service.py --device /dev/video0
```

Smoke test from the laptop:

```bash
curl http://<pi>:8082/health
curl -o /tmp/frame.jpg http://<pi>:8082/frame && file /tmp/frame.jpg
# preview in a browser: http://<pi>:8082/preview
```

If the picture is dark or blurry, check the manual settings took:

```bash
v4l2-ctl -d /dev/video0 --list-ctrls | grep -Ei 'exposure|white|focus'
```
