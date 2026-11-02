from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from agent.measure import BoxRecord, Calibration, decode_frame
from agent.vision_backend import Mask, ScriptedVisionBackend, VisionError, VisionResult
from agent.vision_check import (
    _best_mask,
    _calibrate_signal,
    _parse_box_overrides,
    _synthetic_frame,
    check_signal,
    main,
)


def _hit(score: float | None = 0.9) -> VisionResult:
    return VisionResult(
        masks=(Mask(label="red LED", bbox=(0, 0, 10, 10), score=score),), confidence=0.9
    )


def _bar_frame() -> bytes:
    """Dark frame with a horizontal white bar (angle ~0 in a full-frame mask)."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.line(img, (10, 50), (90, 50), (255, 255, 255), 6)
    ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return bytes(jpeg)


def _full_frame_hit() -> VisionResult:
    return VisionResult(
        masks=(Mask(label="servo arm", bbox=(0, 0, 100, 100), score=0.9),), confidence=0.9
    )


def test_best_mask_prefers_highest_score() -> None:
    low = Mask(label="x", bbox=(0, 0, 1, 1), score=0.2)
    high = Mask(label="x", bbox=(0, 0, 2, 2), score=0.8)
    assert _best_mask((low, high)) is high
    assert _best_mask((high, low)) is high
    first = Mask(label="x", bbox=(0, 0, 1, 1))
    assert _best_mask((first, low)) is low  # scored beats unscored
    assert _best_mask(()) is None


def test_check_signal_brightness_measured() -> None:
    frame = _synthetic_frame()
    vision = ScriptedVisionBackend([_hit()])
    cal = Calibration(brightness_off={"lamp_red": 0.05}, brightness_on={"lamp_red": 0.5})
    entry = check_signal(
        "lamp_red",
        "red LED",
        "brightness",
        frame,
        vision,
        cal,
        expect_angle=None,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "measured"
    assert entry["lit"] is True
    assert entry["confidence"] == 0.9
    assert vision.prompts == ["red LED"]


def test_check_signal_uncalibrated_and_no_mask() -> None:
    frame = _synthetic_frame()
    entry = check_signal(
        "lamp_red",
        "red LED",
        "brightness",
        frame,
        ScriptedVisionBackend([_hit()]),
        Calibration(),
        expect_angle=None,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "uncalibrated"
    assert "brightness" in entry
    entry = check_signal(
        "lamp_red",
        "red LED",
        "brightness",
        frame,
        ScriptedVisionBackend([VisionResult(masks=(), confidence=0.0)]),
        Calibration(),
        expect_angle=None,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "inconclusive"
    assert entry["error"] == "no mask returned"


def test_check_signal_vision_error_is_inconclusive() -> None:
    class Broken(ScriptedVisionBackend):
        def segment(self, frame: bytes, prompt: str) -> VisionResult:
            raise VisionError("down")

    entry = check_signal(
        "lamp_red",
        "red LED",
        "brightness",
        _synthetic_frame(),
        Broken([]),
        Calibration(),
        expect_angle=None,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "inconclusive"
    assert entry["error"] == "down"


def test_check_signal_angle_match_and_mismatch() -> None:
    frame = _bar_frame()
    cal = Calibration(angles={"arm_angle": {90: 0.0}})
    entry = check_signal(
        "arm_angle",
        "servo arm",
        "angle",
        frame,
        ScriptedVisionBackend([_full_frame_hit()]),
        cal,
        expect_angle=90,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "measured"
    assert entry["angle_match"] is True
    cal = Calibration(angles={"arm_angle": {90: 90.0}})
    entry = check_signal(
        "arm_angle",
        "servo arm",
        "angle",
        frame,
        ScriptedVisionBackend([_full_frame_hit()]),
        cal,
        expect_angle=90,
        angle_tolerance=10.0,
    )
    assert entry["angle_match"] is False


def test_fake_cli_check_reports_uncalibrated(tmp_path) -> None:
    out = tmp_path / "check"
    assert main(["--out", str(out), "--fake"]) == 1
    assert (out / "frame.jpg").is_file()
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert set(report["signals"]) == {"lamp_red", "lamp_green", "lamp_yellow", "arm_angle"}
    assert report["signals"]["lamp_red"]["status"] == "uncalibrated"


def test_calibrate_skips_without_segment_calls() -> None:
    vision = ScriptedVisionBackend([])  # raises if segment is called
    signals: dict[str, dict[str, object]] = {}
    frame = _synthetic_frame()
    ok = _calibrate_signal(
        "arm_angle",
        "angle",
        "servo arm",
        frame,
        decode_frame(frame),
        vision,
        Calibration(),
        True,
        False,
        None,
        {},
        signals,
    )
    assert ok is True
    assert signals["arm_angle"]["status"] == "skipped"
    assert vision.prompts == []


def test_frame_flag_reuses_saved_frame(tmp_path, monkeypatch) -> None:
    class StubBackend:
        instances: list[StubBackend] = []

        @classmethod
        def from_env(cls) -> StubBackend:
            inst = cls()
            cls.instances.append(inst)
            return inst

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def segment(self, frame: bytes, prompt: str) -> VisionResult:
            self.prompts.append(prompt)
            return _hit()

        def close(self) -> None:
            pass

    monkeypatch.setattr("agent.vision_check.MetaSamBackend", StubBackend)
    saved = tmp_path / "saved.jpg"
    saved.write_bytes(_synthetic_frame())
    out = tmp_path / "out"
    assert main(["--out", str(out), "--frame", str(saved)]) == 1  # uncalibrated
    assert (out / "frame.jpg").read_bytes() == _synthetic_frame()
    assert StubBackend.instances[0].prompts == [
        "red LED",
        "green LED",
        "yellow LED",
        "servo arm",
    ]


def test_calibrate_manual_box_skips_segment() -> None:
    vision = ScriptedVisionBackend([])  # raises if segment is called
    signals: dict[str, dict[str, object]] = {}
    frame = _synthetic_frame()
    calibration = Calibration()
    ok = _calibrate_signal(
        "lamp_red",
        "brightness",
        "red LED",
        frame,
        decode_frame(frame),
        vision,
        calibration,
        False,
        True,
        None,
        {"lamp_red": (0, 0, 5, 5)},
        signals,
    )
    assert ok is True
    assert signals["lamp_red"]["box_source"] == "manual"
    assert signals["lamp_red"]["bbox"] == [0, 0, 5, 5]
    assert vision.prompts == []
    box = calibration.boxes["lamp_red"]
    assert box.bbox == (0, 0, 5, 5) and box.source == "manual" and box.score is None


def test_calibrate_live_box_is_stored() -> None:
    vision = ScriptedVisionBackend([_hit(score=0.7)])
    signals: dict[str, dict[str, object]] = {}
    frame = _synthetic_frame()
    calibration = Calibration()
    ok = _calibrate_signal(
        "lamp_red",
        "brightness",
        "red LED",
        frame,
        decode_frame(frame),
        vision,
        calibration,
        False,
        True,
        None,
        {},
        signals,
    )
    assert ok is True
    assert signals["lamp_red"]["box_source"] == "live"
    box = calibration.boxes["lamp_red"]
    assert box.bbox == (0, 0, 10, 10) and box.score == 0.7 and box.source == "live"


def test_check_signal_reuses_stored_box() -> None:
    vision = ScriptedVisionBackend([])  # raises if segment is called
    frame = _synthetic_frame()
    calibration = Calibration(
        brightness_off={"lamp_red": 0.05},
        brightness_on={"lamp_red": 0.5},
        boxes={"lamp_red": BoxRecord(bbox=(0, 0, 10, 10), score=0.7, source="live")},
    )
    entry = check_signal(
        "lamp_red",
        "red LED",
        "brightness",
        frame,
        vision,
        calibration,
        expect_angle=None,
        angle_tolerance=10.0,
    )
    assert entry["status"] == "measured"
    assert entry["lit"] is True
    assert entry["box_source"] == "stored"
    assert entry["confidence"] == 0.7
    assert vision.prompts == []


def test_parse_box_overrides_rejects_bad_format() -> None:
    assert _parse_box_overrides(["lamp_red=1,2,3,4"]) == {"lamp_red": (1, 2, 3, 4)}
    for bad in ["lamp_red", "lamp_red=1,2,3", "lamp_red=a,b,c,d", "=1,2,3,4", "x=3,3,1,1"]:
        with pytest.raises(SystemExit, match="bad --box"):
            _parse_box_overrides([bad])


def test_signals_flag_filters_and_rejects_unknown(tmp_path) -> None:
    out = tmp_path / "sub"
    assert main(["--out", str(out), "--fake", "--signals", "lamp_red"]) == 1
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert set(report["signals"]) == {"lamp_red"}
    with pytest.raises(SystemExit, match="unknown vision signals"):
        main(["--out", str(out), "--fake", "--signals", "lamp_red,nope"])


def test_fake_cli_calibrate_captures_baselines(tmp_path) -> None:
    out = tmp_path / "cal"
    calibration = out / "calibration.json"
    assert (
        main(["--out", str(out), "--fake", "--calibrate-off", "--calibration", str(calibration)])
        == 0
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["signals"]["lamp_red"]["status"] == "half-calibrated"
    assert report["signals"]["arm_angle"]["status"] == "skipped"
    assert (
        main(
            [
                "--out",
                str(out),
                "--fake",
                "--calibrate-angle",
                "90",
                "--calibration",
                str(calibration),
            ]
        )
        == 0
    )
    cal = Calibration.load(calibration)
    assert "lamp_red" in cal.brightness_off
    # Square cue has no true orientation; the plumbing just reports a valid angle.
    assert 0.0 <= cal.angles["arm_angle"][90] < 180.0
