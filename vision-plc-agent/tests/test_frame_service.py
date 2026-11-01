import httpx
import pytest

from agent.frames import FrameError, HttpFrameSource
from pi.frame_service.service import FrameGrabber, GrabError, serve

_JPEG = b"\xff\xd8\xff\xe0" + bytes(64) + b"\xff\xd9"


class ScriptGrabber(FrameGrabber):
    def __init__(self, payload: bytes | None = _JPEG) -> None:
        self.payload = payload
        self.calls = 0

    def grab_jpeg(self) -> bytes:
        self.calls += 1
        if self.payload is None:
            raise GrabError("no camera")
        return self.payload


@pytest.fixture()
def live():
    grabber = ScriptGrabber()
    server = serve(grabber, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with HttpFrameSource(f"http://127.0.0.1:{port}") as source:
            yield source, grabber, port
    finally:
        server.shutdown()
        server.server_close()


def test_grab_returns_frame_bytes(live) -> None:
    source, grabber, _port = live
    assert source.grab() == _JPEG
    assert grabber.calls == 1


def test_camera_failure_is_a_clean_error() -> None:
    server = serve(ScriptGrabber(None), "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with HttpFrameSource(f"http://127.0.0.1:{port}") as source:
            with pytest.raises(FrameError, match="502"):
                source.grab()
    finally:
        server.shutdown()
        server.server_close()


def test_preview_streams_multipart_jpeg(live) -> None:
    _source, _grabber, port = live
    with httpx.Client() as client, client.stream("GET", f"http://127.0.0.1:{port}/preview") as r:
        assert r.status_code == 200
        chunk = b""
        for part in r.iter_bytes(1024):
            chunk += part
            if _JPEG in chunk:
                break
    assert b"--frame" in chunk
