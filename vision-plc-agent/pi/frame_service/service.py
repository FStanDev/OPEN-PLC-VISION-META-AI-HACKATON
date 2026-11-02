"""C920 frame service: still JPEGs for vision + MJPEG preview for humans.

Endpoints: ``GET /frame`` (one JPEG), ``GET /preview`` (multipart MJPEG
stream), ``GET /health``. Capture sits behind :class:`FrameGrabber` so
tests inject a fake; the real :class:`Cv2Grabber` needs
``opencv-python-headless`` on the Pi. A failed grab is a 502 with a JSON
error — never an empty frame, never a stock image, never a stale frame.

One reader thread owns the camera and reads it continuously, keeping only
the newest frame (:class:`ThreadedGrabber`). That matters twice over:

* ``/frame`` must show the rig *now*. V4L2 queues a few buffers; a camera
  read only on request hands back a frame captured right after the
  previous request (bench: a "lamp on" still showed a wristwatch 16 min
  behind). ``/frame`` therefore waits for a frame read after the request
  arrived plus a settle delay that covers the camera's pipeline latency.
* ``cv2.VideoCapture`` is not thread-safe. ``/preview`` and ``/frame`` used
  to call ``read()`` concurrently and ``/frame`` failed with a 502 while a
  preview was open. Now both only consume the reader thread's frames.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_PORT = 8082
DEFAULT_EXPOSURE = 200  # exposure_time_absolute, 100 us units (20 ms)
DEFAULT_GAIN = 128  # fixed so brightness is the same after every restart
DEFAULT_SETTLE_S = 0.25  # camera pipeline latency budget for a fresh frame
DEFAULT_TIMEOUT_S = 3.0
WB_TEMPERATURE = 4500
_BOUNDARY = "frame"


def v4l2_lock_controls(exposure: int, gain: int | None) -> list[tuple[tuple[str, ...], int]]:
    """Controls to lock, each with its accepted spellings (newest kernel name first).

    Order matters: exposure can only be set once auto exposure is off.
    ``exposure_dynamic_framerate`` (old name ``exposure_auto_priority``) is the
    C920's low-light frame-rate hunting switch. Manual WB needs an explicit
    temperature or colors drift with the driver's leftover value.
    """
    controls: list[tuple[tuple[str, ...], int]] = [
        (("auto_exposure", "exposure_auto"), 1),  # 1 = manual
        (("exposure_dynamic_framerate", "exposure_auto_priority"), 0),
        (("exposure_time_absolute", "exposure_absolute"), exposure),
        (("white_balance_automatic", "white_balance_temperature_auto"), 0),
        (("white_balance_temperature",), WB_TEMPERATURE),
        (("focus_automatic_continuous", "focus_auto"), 0),
    ]
    if gain is not None:
        controls.append((("gain",), gain))
    return controls


def _warn(message: str) -> None:
    sys.stderr.write(f"[frames] WARNING: {message}\n")


def lock_camera(
    device: str,
    capture: Any,
    cv2: Any,
    *,
    exposure: int = DEFAULT_EXPOSURE,
    gain: int | None = DEFAULT_GAIN,
    runner: Callable[..., Any] | None = None,
) -> None:
    """Lock the camera to fully manual operation (best effort).

    cv2 properties first (fallback when v4l2-ctl is missing), then v4l2-ctl
    last so it is authoritative. ``gain=None`` holds whatever gain the camera
    currently has instead of setting one. Nothing here raises: every failure
    is a stderr warning, because a half-locked camera still beats no camera.
    """
    run = runner if runner is not None else subprocess.run
    wanted = {
        cv2.CAP_PROP_AUTO_EXPOSURE: 1,  # manual (raw V4L2 menu value)
        cv2.CAP_PROP_EXPOSURE: exposure,
        cv2.CAP_PROP_AUTO_WB: 0,
        cv2.CAP_PROP_WB_TEMPERATURE: WB_TEMPERATURE,
        cv2.CAP_PROP_AUTOFOCUS: 0,
    }
    if gain is not None:
        wanted[cv2.CAP_PROP_GAIN] = gain
    for prop, value in wanted.items():
        ok = capture.set(prop, value)
        actual = capture.get(prop)
        if not ok or actual != value:
            _warn(f"camera refused prop {prop}={value} (reads back {actual})")
    if gain is None:
        held = capture.get(cv2.CAP_PROP_GAIN)
        if held is None or held < 0:
            _warn("gain read-back unsupported; brightness may drift between frames")
        else:
            capture.set(cv2.CAP_PROP_GAIN, held)
            if capture.get(cv2.CAP_PROP_GAIN) != held:
                _warn(f"gain would not hold at {held}")
    _v4l2_lock(device, run, v4l2_lock_controls(exposure, gain))


def _v4l2_lock(
    device: str,
    runner: Callable[..., Any],
    controls: list[tuple[tuple[str, ...], int]],
) -> None:
    applied: list[str] = []
    for names, value in controls:
        last_error = ""
        for name in names:
            try:
                done = runner(
                    ["v4l2-ctl", "-d", device, "--set-ctrl", f"{name}={value}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except FileNotFoundError:
                _warn("v4l2-ctl not found (apt install v4l-utils); cv2-only lock")
                return
            except Exception as exc:
                _warn(f"v4l2-ctl failed: {exc}")
                return
            if done.returncode == 0:
                applied.append(name)
                break
            detail = str(getattr(done, "stderr", "") or "").strip().splitlines()
            last_error = detail[0][:120] if detail else f"rc={done.returncode}"
        else:
            _warn(f"v4l2-ctl rejected {'/'.join(names)}={value}: {last_error}")
    if applied:
        _log_effective(device, runner, applied)


def _log_effective(device: str, runner: Callable[..., Any], names: list[str]) -> None:
    """Print what the driver actually holds, so a bad lock is visible at startup."""
    try:
        done = runner(
            ["v4l2-ctl", "-d", device, "--get-ctrl", ",".join(names)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return
    text = str(getattr(done, "stdout", "") or "").strip()
    if text:
        sys.stderr.write("[frames] camera: " + " ".join(text.split()) + "\n")


class GrabError(RuntimeError):
    """The camera produced no (fresh) frame."""


class FrameGrabber(ABC):
    @abstractmethod
    def grab_jpeg(self) -> bytes:
        """One JPEG of a frame captured after this call started."""

    def preview_jpegs(self) -> Iterator[bytes]:
        """Endless JPEG stream for the human preview."""
        while True:
            yield self.grab_jpeg()

    def close(self) -> None:
        """Release the camera (optional)."""


class ThreadedGrabber(FrameGrabber):
    """Single reader thread drains the camera and keeps only the newest frame.

    Args:
        read_frame: blocking camera read; returns a frame or None on failure.
            Only the reader thread ever calls it.
        encode: frame -> JPEG bytes (done on demand, not per frame).
        settle_s: a frame for ``grab_jpeg`` must be read at least this long
            after the call started (covers sensor/USB pipeline latency).
        timeout_s: give up (GrabError) if no such frame arrives in time.
    """

    def __init__(
        self,
        read_frame: Callable[[], Any],
        encode: Callable[[Any], bytes],
        *,
        settle_s: float = DEFAULT_SETTLE_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read_frame
        self._encode = encode
        self._settle = settle_s
        self._timeout = timeout_s
        self._clock = clock
        self._cond = threading.Condition()
        self._frame: Any = None
        self._stamp = float("-inf")
        self._seq = 0
        self._error = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="frame-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._read()
            except Exception as exc:  # a driver hiccup must not kill the reader
                frame = None
                error = f"camera read raised: {exc}"
            else:
                error = "camera read failed"
            with self._cond:
                if frame is None:
                    self._error = error
                else:
                    self._frame = frame
                    self._stamp = self._clock()
                    self._seq += 1
                    self._error = ""
                self._cond.notify_all()
            if frame is None:
                self._stop.wait(0.1)  # unplugged camera: retry without spinning

    def _wait(self, ready: Callable[[], bool], what: str) -> tuple[int, Any]:
        with self._cond:
            if not self._cond.wait_for(lambda: ready() or self._stop.is_set(), self._timeout):
                cause = f" ({self._error})" if self._error else ""
                raise GrabError(f"no {what} frame within {self._timeout:g}s{cause}")
            if self._stop.is_set():
                raise GrabError("grabber closed")
            return self._seq, self._frame

    def grab_jpeg(self) -> bytes:
        not_before = self._clock() + self._settle
        _seq, frame = self._wait(lambda: self._stamp >= not_before, "fresh")
        return self._encode(frame)

    def preview_jpegs(self) -> Iterator[bytes]:
        seen = -1
        while True:
            seen, frame = self._wait(lambda: self._seq != seen, "new")
            yield self._encode(frame)

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2.0)


class Cv2Grabber(ThreadedGrabber):
    """Real grabber on top of OpenCV (Pi only)."""

    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 1280,
        height: int = 720,
        *,
        exposure: int = DEFAULT_EXPOSURE,
        gain: int | None = DEFAULT_GAIN,
        settle_s: float = DEFAULT_SETTLE_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "opencv is not installed; on the Pi run: "
                "pip install -r pi/frame_service/requirements.txt"
            ) from exc
        self._cv2 = cv2
        capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not capture.isOpened():
            raise GrabError(f"cannot open camera {device}")
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # least queueing latency
        lock_camera(device, capture, cv2, exposure=exposure, gain=gain)
        self._capture = capture
        super().__init__(self._read_one, self._encode_jpeg, settle_s=settle_s, timeout_s=timeout_s)

    def _read_one(self) -> Any:
        ok, frame = self._capture.read()
        return frame if ok else None

    def _encode_jpeg(self, frame: Any) -> bytes:
        ok, jpeg = self._cv2.imencode(".jpg", frame)
        if not ok:
            raise GrabError("JPEG encode failed")
        return bytes(jpeg)

    def close(self) -> None:
        super().close()
        self._capture.release()


def default_device() -> str:
    """The C920's stable by-id node if present (its /dev/videoN moves), else video0."""
    found = sorted(glob.glob("/dev/v4l/by-id/*C920*-video-index0"))
    return found[0] if found else "/dev/video0"


def _handler(grabber: FrameGrabber) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FrameService/2"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(f"[frames] {self.address_string()} {fmt % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True})
            elif self.path == "/frame":
                self._frame()
            elif self.path == "/preview":
                self._preview()
            else:
                self._json(404, {"error": "unknown path"})

        def _frame(self) -> None:
            try:
                jpeg = grabber.grab_jpeg()
            except GrabError as exc:
                self._json(502, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpeg)

        def _preview(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for jpeg in grabber.preview_jpegs():
                    self.wfile.write(
                        f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                        + jpeg
                        + b"\r\n"
                    )
            except (GrabError, BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(
    grabber: FrameGrabber,
    bind: str,
    port: int,
    *,
    _server_cls: type[ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    server = _server_cls((bind, port), _handler(grabber))
    server.daemon_threads = True  # open previews must not block shutdown
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C920 frame service (Pi).")
    parser.add_argument("--bind", default=os.environ.get("PI_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--device", default=None, help="camera node (default: C920 by-id link, else /dev/video0)"
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--exposure",
        type=int,
        default=DEFAULT_EXPOSURE,
        help="manual exposure_time_absolute in 100 us units",
    )
    parser.add_argument(
        "--gain", type=int, default=DEFAULT_GAIN, help="fixed gain 0-255; -1 holds current gain"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_S,
        help="seconds after a /frame request before a frame counts as fresh",
    )
    args = parser.parse_args(argv)

    if "PI_BIND" not in os.environ:
        print(
            "[frames] WARNING: PI_BIND unset, binding 0.0.0.0 — "
            "set it to the Pi's Tailscale IP in production.",
            flush=True,
        )
    device = args.device or default_device()
    grabber = Cv2Grabber(
        device,
        args.width,
        args.height,
        exposure=args.exposure,
        gain=None if args.gain < 0 else args.gain,
        settle_s=args.settle,
    )
    server = ThreadingHTTPServer((args.bind, args.port), _handler(grabber))
    server.daemon_threads = True
    print(f"[frames] listening on {args.bind}:{args.port} ({device})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        grabber.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
