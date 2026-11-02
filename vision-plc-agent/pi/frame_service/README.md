# frame_service

Logitech C920 capture: `GET /frame` returns one still JPEG (what the vision
step consumes), `GET /preview` is an MJPEG stream for the dashboard/human
check, `GET /health` is a liveness probe.

One reader thread owns the camera and reads it continuously, keeping only the
newest frame. `/frame` waits for a frame read at least `--settle` seconds
(default 0.25) after the request arrived, so it always shows the rig *now*
(reading the camera only on request returned frames from the previous
request, minutes old). `/preview` consumes the same frames, so an open
preview no longer breaks `/frame`.

Exposure, white balance, focus, frame-rate hunting and gain are locked to
manual at startup: cv2 properties first, then `v4l2-ctl` (needs the
`v4l-utils` apt package) as the authority. Both current kernel control names
(`auto_exposure`, `exposure_time_absolute`, `exposure_dynamic_framerate`) and
legacy ones are tried; the effective values are printed as `[frames] camera:`.
Gain is fixed (`--gain`, default 128) so brightness is the same after every
restart. Every step is best-effort and refusals go to stderr.
A failed grab (no fresh frame within 3 s) is a 502 — the service never
substitutes a stock or stale image.

## Run on the Pi

Plug in the C920, then:

```bash
cd ~/Developer/vision-plc-agent
python3 -m venv .venvs/frames
.venvs/frames/bin/pip install -r pi/frame_service/requirements.txt
# --device defaults to /dev/v4l/by-id/*C920*-video-index0 (its /dev/videoN moves)
.venvs/frames/bin/python pi/frame_service/service.py
```

Smoke test from the laptop:

```bash
curl http://<pi>:8082/health
curl -o /tmp/frame.jpg http://<pi>:8082/frame && file /tmp/frame.jpg
# preview in a browser: http://<pi>:8082/preview
```

If the picture is too dark or too bright, tune live while watching the
preview, then restart the service with the values you liked (changing them
invalidates `calibration.json` — recalibrate off/on afterwards):

```bash
DEV=$(ls /dev/v4l/by-id/*C920*-video-index0)
v4l2-ctl -d $DEV --set-ctrl exposure_time_absolute=250,gain=160
v4l2-ctl -d $DEV --get-ctrl auto_exposure,exposure_time_absolute,exposure_dynamic_framerate,gain,focus_absolute
# then: service.py --exposure 250 --gain 160
```

Lower gain/exposure makes lit LEDs stand out more against the room, as long
as SAM can still see the parts.
