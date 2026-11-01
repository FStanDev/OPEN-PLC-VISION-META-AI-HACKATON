"""Tiny HTTP service driving the stimulus pins (GPIO26/GPIO13).

``press`` drives the pin LOW through its 1 kΩ resistor (electrical button
press); ``release`` returns it to high impedance so the button node's own
pull-up takes over. Stdlib HTTP + :mod:`pi.gpio`; pins come from
``io_map.yaml`` (``stimulus.*.drives``), never from code.

Endpoints: ``POST /press {"button": "start_btn"}``, ``POST /release``,
``GET /health``.
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.io_map import IoMap, load_io_map  # noqa: E402
from pi.gpio import FakeGpio, GpioBackend  # noqa: E402

DEFAULT_PORT = 8081


class StimulusService:
    """Button press/release on top of an injectable GPIO backend."""

    def __init__(self, io_map: IoMap, gpio: GpioBackend) -> None:
        self._gpio = gpio
        self._pins = {pin.drives: pin.bcm for pin in io_map.stimulus.values()}

    @property
    def buttons(self) -> list[str]:
        return sorted(self._pins)

    def press(self, button: str) -> None:
        self._gpio.setup_output(self._pin(button))
        self._gpio.write(self._pin(button), False)

    def release(self, button: str) -> None:
        self._gpio.set_high_impedance(self._pin(button))

    def _pin(self, button: str) -> int:
        try:
            return self._pins[button]
        except KeyError:
            raise KeyError(f"unknown button {button!r} (known: {self.buttons})") from None


def _handler(service: StimulusService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "StimulusService/1"

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(f"[stimulus] {self.address_string()} {fmt % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "buttons": service.buttons})
            else:
                self._json(404, {"error": "unknown path"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/press", "/release"):
                self._json(404, {"error": "unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                button = body["button"]
            except (ValueError, KeyError, TypeError):
                self._json(400, {"error": 'body must be {"button": "<name>"}'})
                return
            try:
                service.press(button) if self.path == "/press" else service.release(button)
            except KeyError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True})

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(
    service: StimulusService,
    bind: str,
    port: int,
    *,
    _server_cls: type[ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    server = _server_cls((bind, port), _handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Button stimulus service (Pi).")
    parser.add_argument("--bind", default=os.environ.get("PI_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--io-map", default=None)
    parser.add_argument("--fake-gpio", action="store_true", help="no hardware (debug only)")
    args = parser.parse_args(argv)

    if "PI_BIND" not in os.environ:
        print(
            "[stimulus] WARNING: PI_BIND unset, binding 0.0.0.0 — "
            "set it to the Pi's Tailscale IP in production.",
            flush=True,
        )
    if args.fake_gpio:
        gpio: GpioBackend = FakeGpio()
    else:
        from pi.gpio import GpioZeroBackend

        gpio = GpioZeroBackend()
    service = StimulusService(load_io_map(args.io_map), gpio)
    server = ThreadingHTTPServer((args.bind, args.port), _handler(service))
    print(f"[stimulus] listening on {args.bind}:{args.port} ({service.buttons})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
