"""C920 frame service: still JPEGs for vision + MJPEG preview for humans.

Endpoints: ``GET /frame`` (one JPEG), ``GET /preview`` (multipart MJPEG
stream), ``GET /health``. Capture sits behind :class:`FrameGrabber` so
tests inject a fake; the real :class:`Cv2Grabber` needs
``opencv-python-headless`` on the Pi. A failed grab is a 502 with a JSON
error — never an empty frame, never a stock image.
"""

import argparse
import json
import os
import sys
import threading
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8082
_BOUNDARY = "frame"


class GrabError(RuntimeError):
    """The camera produced no frame."""


class FrameGrabber(ABC):
    @abstractmethod
    def grab_jpeg(self) -> bytes:
        """Capture one frame, encoded as JPEG bytes."""

    def close(self) -> None:
        """Release the camera (optional)."""


class Cv2Grabber(FrameGrabber):
    """Real grabber on top of OpenCV (Pi only)."""

    def __init__(self, device: str = "/dev/video0", width: int = 1280, height: int = 720) -> None:
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
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        capture.set(cv2.CAP_PROP_FOURCC, fourcc)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._lock_exposure(capture)
        self._capture = capture

    def _lock_exposure(self, capture) -> None:
        """Best-effort manual exposure/focus/WB; the C920 may refuse some."""
        cv2 = self._cv2
        wanted = {
            cv2.CAP_PROP_AUTO_EXPOSURE: 1,  # manual
            cv2.CAP_PROP_EXPOSURE: 200,
            cv2.CAP_PROP_AUTO_WB: 0,
            cv2.CAP_PROP_AUTOFOCUS: 0,
        }
        for prop, value in wanted.items():
            if not capture.set(prop, value):
                sys.stderr.write(f"[frames] WARNING: camera refused prop {prop}={value}\n")

    def grab_jpeg(self) -> bytes:
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise GrabError("camera read failed")
        ok, jpeg = self._cv2.imencode(".jpg", frame)
        if not ok:
            raise GrabError("JPEG encode failed")
        return bytes(jpeg)

    def close(self) -> None:
        self._capture.release()


def _handler(grabber: FrameGrabber) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FrameService/1"
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
            self.end_headers()
            self.wfile.write(jpeg)

        def _preview(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY}")
            self.end_headers()
            try:
                while True:
                    jpeg = grabber.grab_jpeg()
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C920 frame service (Pi).")
    parser.add_argument("--bind", default=os.environ.get("PI_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args(argv)

    if "PI_BIND" not in os.environ:
        print(
            "[frames] WARNING: PI_BIND unset, binding 0.0.0.0 — "
            "set it to the Pi's Tailscale IP in production.",
            flush=True,
        )
    grabber = Cv2Grabber(args.device, args.width, args.height)
    server = ThreadingHTTPServer((args.bind, args.port), _handler(grabber))
    print(f"[frames] listening on {args.bind}:{args.port} ({args.device})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        grabber.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
