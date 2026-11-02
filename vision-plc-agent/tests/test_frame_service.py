import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from agent.frames import FrameError, HttpFrameSource
from pi.frame_service.service import (
    FrameGrabber,
    GrabError,
    ThreadedGrabber,
    lock_camera,
    serve,
)

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
    return [argv[-1] for argv in calls if "--set-ctrl" in argv]


def test_lock_sets_fixed_gain_and_disables_frame_rate_hunting() -> None:
    _ok_runner.calls.clear()
    capture = FakeCapture({_FAKE_CV2.CAP_PROP_GAIN: 255.0})
    lock_camera("/dev/video0", capture, _FAKE_CV2, exposure=150, gain=64, runner=_ok_runner)
    pairs = _v4l2_pairs(_ok_runner.calls)
    # Current kernel spellings are tried first (bench Pi rejects the old ones).
    assert "auto_exposure=1" in pairs
    assert "exposure_dynamic_framerate=0" in pairs  # the low-light hunting switch
    assert "exposure_time_absolute=150" in pairs
    assert "gain=64" in pairs
    assert pairs.index("auto_exposure=1") < pairs.index("exposure_time_absolute=150")
    for argv in _ok_runner.calls:
        assert argv[0] == "v4l2-ctl" and argv[1:3] == ["-d", "/dev/video0"]
    assert (_FAKE_CV2.CAP_PROP_AUTO_EXPOSURE, 1) in capture.sets
    assert (_FAKE_CV2.CAP_PROP_GAIN, 64) in capture.sets
    assert capture.props[_FAKE_CV2.CAP_PROP_GAIN] == 64


def test_lock_holds_current_gain_when_none() -> None:
    _ok_runner.calls.clear()
    capture = FakeCapture({_FAKE_CV2.CAP_PROP_GAIN: 42.0})
    lock_camera("/dev/video0", capture, _FAKE_CV2, gain=None, runner=_ok_runner)
    assert (_FAKE_CV2.CAP_PROP_GAIN, 42.0) in capture.sets  # current value held
    assert not any(p.startswith("gain=") for p in _v4l2_pairs(_ok_runner.calls))


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


def test_lock_falls_back_to_legacy_control_names(capsys) -> None:
    """Older kernels only know exposure_auto / exposure_absolute / ..."""
    calls: list = []
    legacy = {"exposure_auto", "exposure_auto_priority", "exposure_absolute", "focus_auto"}
    legacy |= {"white_balance_temperature_auto", "white_balance_temperature", "gain"}

    def old_kernel(argv, **kwargs):
        calls.append(argv[-1])
        if "--set-ctrl" in argv and argv[-1].split("=")[0] not in legacy:
            return SimpleNamespace(returncode=1, stderr="unknown control")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    lock_camera("/dev/video0", FakeCapture(), _FAKE_CV2, runner=old_kernel)
    assert "exposure_auto=1" in calls
    assert "exposure_auto_priority=0" in calls
    assert "exposure_absolute=200" in calls
    assert "WARNING" not in capsys.readouterr().err  # every control found a spelling


def test_lock_warns_per_rejected_control_and_continues(capsys) -> None:
    calls: list = []

    def refuse_gain(argv, **kwargs):
        calls.append(argv[-1])
        if argv[-1].startswith("gain="):
            return SimpleNamespace(returncode=1, stderr="unknown control 'gain'")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    lock_camera("/dev/video0", FakeCapture(), _FAKE_CV2, runner=refuse_gain)
    err = capsys.readouterr().err
    assert "rejected gain=128" in err
    assert "focus_automatic_continuous=0" in calls  # other controls still applied


def test_lock_warns_when_gain_unreadable(capsys) -> None:
    _ok_runner.calls.clear()
    capture = FakeCapture()  # get(GAIN) -> -1.0: unsupported
    lock_camera("/dev/video0", capture, _FAKE_CV2, gain=None, runner=_ok_runner)
    assert "gain" in capsys.readouterr().err.lower()


class LaggyCamera:
    """Fake UVC camera: frames leave the pipeline ``lag`` reads after capture,
    and a concurrent ``read()`` is a hard error (cv2.VideoCapture is not
    thread-safe)."""

    def __init__(self, lag: int = 4, period_s: float = 0.005) -> None:
        self.scene = "off"
        self.fail = False
        self._pipeline = ["off"] * lag
        self._period = period_s
        self._busy = threading.Lock()
        self.reentered = False

    def read(self) -> str | None:
        if not self._busy.acquire(blocking=False):
            self.reentered = True
            raise RuntimeError("concurrent read")
        try:
            time.sleep(self._period)
            if self.fail:
                return None
            self._pipeline.append(self.scene)
            return self._pipeline.pop(0)
        finally:
            self._busy.release()


def _encode(frame: str) -> bytes:
    return frame.encode()


def test_grab_returns_frame_captured_after_the_request() -> None:
    camera = LaggyCamera()
    grabber = ThreadedGrabber(camera.read, _encode, settle_s=0.1, timeout_s=2.0)
    try:
        assert grabber.grab_jpeg() == b"off"
        time.sleep(0.2)
        camera.scene = "on"  # the LED switches, then the agent asks for a frame
        assert grabber.grab_jpeg() == b"on"
        camera.scene = "off"
        assert grabber.grab_jpeg() == b"off"
    finally:
        grabber.close()


def test_preview_and_frame_share_one_reader() -> None:
    camera = LaggyCamera()
    grabber = ThreadedGrabber(camera.read, _encode, settle_s=0.02, timeout_s=2.0)
    server = serve(grabber, "127.0.0.1", 0)
    port = server.server_address[1]
    stop = threading.Event()
    streamed: list[int] = []

    def watch_preview() -> None:
        with (
            httpx.Client() as client,
            client.stream("GET", f"http://127.0.0.1:{port}/preview") as r,
        ):
            for part in r.iter_bytes(64):
                streamed.append(len(part))
                if stop.is_set():
                    return

    viewers = [threading.Thread(target=watch_preview) for _ in range(2)]
    try:
        for viewer in viewers:
            viewer.start()
        with HttpFrameSource(f"http://127.0.0.1:{port}") as source:
            for _ in range(5):
                assert source.grab() == b"off"
    finally:
        stop.set()
        for viewer in viewers:
            viewer.join(timeout=3)
        server.shutdown()
        server.server_close()
        grabber.close()
    assert streamed  # previews kept flowing while /frame was served
    assert not camera.reentered


def test_dead_camera_times_out_with_cause() -> None:
    camera = LaggyCamera()
    camera.fail = True
    grabber = ThreadedGrabber(camera.read, _encode, settle_s=0.0, timeout_s=0.3)
    try:
        with pytest.raises(GrabError, match="camera read failed"):
            grabber.grab_jpeg()
    finally:
        grabber.close()


def test_close_unblocks_waiters() -> None:
    camera = LaggyCamera()
    camera.fail = True
    grabber = ThreadedGrabber(camera.read, _encode, settle_s=0.0, timeout_s=5.0)
    errors: list[Exception] = []

    def wait() -> None:
        try:
            grabber.grab_jpeg()
        except GrabError as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.1)
    grabber.close()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert errors and "closed" in str(errors[0])
