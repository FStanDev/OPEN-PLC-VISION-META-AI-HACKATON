from types import SimpleNamespace

import httpx
import pytest

from agent.frames import FrameError, HttpFrameSource
from pi.frame_service.service import FrameGrabber, GrabError, lock_camera, serve

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


class FakeCapture:
    """Duck-typed cv2.VideoCapture: dict-backed props, scriptable refuses."""

    def __init__(self, props: dict[int, float] | None = None) -> None:
        self.props = dict(props or {})
        self.sets: list[tuple[int, float]] = []

    def set(self, prop: int, value: float) -> bool:
        self.sets.append((prop, value))
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.props.get(prop, -1.0)


_FAKE_CV2 = SimpleNamespace(
    CAP_PROP_AUTO_EXPOSURE=100,
    CAP_PROP_EXPOSURE=101,
    CAP_PROP_AUTO_WB=102,
    CAP_PROP_AUTOFOCUS=103,
    CAP_PROP_GAIN=104,
    CAP_PROP_WB_TEMPERATURE=105,
)


def _ok_runner(argv, **kwargs):
    _ok_runner.calls.append(argv)
    return SimpleNamespace(returncode=0, stderr="")


_ok_runner.calls = []


def _v4l2_pairs(calls: list) -> list[str]:
    return [argv[-1] for argv in calls]


def test_lock_disables_auto_priority_and_gain_then_holds_gain() -> None:
    _ok_runner.calls.clear()
    capture = FakeCapture({_FAKE_CV2.CAP_PROP_GAIN: 42.0})
    lock_camera("/dev/video0", capture, _FAKE_CV2, runner=_ok_runner)
    pairs = _v4l2_pairs(_ok_runner.calls)
    assert "exposure_auto_priority=0" in pairs  # the low-light hunting switch
    assert "gain_automatic=0" in pairs
    assert "exposure_auto=1" in pairs
    for argv in _ok_runner.calls:
        assert argv[0] == "v4l2-ctl" and argv[1:3] == ["-d", "/dev/video0"]
    assert (_FAKE_CV2.CAP_PROP_AUTO_EXPOSURE, 1) in capture.sets
    assert (_FAKE_CV2.CAP_PROP_GAIN, 42.0) in capture.sets  # current value held


def test_lock_sets_white_balance_temperature() -> None:
    """Manual WB needs an explicit temperature or colors drift (bench: orange cast)."""
    _ok_runner.calls.clear()
    capture = FakeCapture({_FAKE_CV2.CAP_PROP_GAIN: 42.0})
    lock_camera("/dev/video0", capture, _FAKE_CV2, runner=_ok_runner)
    assert "white_balance_temperature=4500" in _v4l2_pairs(_ok_runner.calls)
    assert (_FAKE_CV2.CAP_PROP_WB_TEMPERATURE, 4500) in capture.sets


def test_lock_survives_missing_v4l2_with_warning(capsys) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError("no v4l2-ctl")

    capture = FakeCapture({_FAKE_CV2.CAP_PROP_GAIN: 42.0})
    lock_camera("/dev/video0", capture, _FAKE_CV2, runner=missing)  # must not raise
    assert "v4l2-ctl" in capsys.readouterr().err
    assert (_FAKE_CV2.CAP_PROP_AUTO_EXPOSURE, 1) in capture.sets  # cv2 path still runs


def test_lock_warns_per_rejected_control_and_continues(capsys) -> None:
    calls: list = []

    def refuse_gain(argv, **kwargs):
        calls.append(argv[-1])
        if argv[-1].startswith("gain_"):
            return SimpleNamespace(returncode=1, stderr="unknown control 'gain_automatic'")
        return SimpleNamespace(returncode=0, stderr="")

    lock_camera("/dev/video0", FakeCapture(), _FAKE_CV2, runner=refuse_gain)
    err = capsys.readouterr().err
    assert "gain_automatic" in err
    assert "exposure_auto_priority=0" in calls  # later controls still attempted


def test_lock_warns_when_gain_unreadable(capsys) -> None:
    _ok_runner.calls.clear()
    capture = FakeCapture()  # get(GAIN) -> -1.0: unsupported
    lock_camera("/dev/video0", capture, _FAKE_CV2, runner=_ok_runner)
    assert "gain" in capsys.readouterr().err.lower()
