import cv2
import numpy as np
import pytest

from agent.measure import (
    BoxRecord,
    BrightnessBaseline,
    Calibration,
    MeasureError,
    arm_angle,
    brightness,
    classify_brightness,
    decode_frame,
    match_angle,
)


def _encode(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return bytes(buf)


def test_decode_roundtrip_and_failures() -> None:
    img = np.full((8, 6, 3), 128, dtype=np.uint8)
    back = decode_frame(_encode(img))
    assert back.shape == (8, 6, 3)
    assert abs(float(np.mean(back)) - 128) < 1.0
    with pytest.raises(MeasureError, match="empty frame"):
        decode_frame(b"")
    with pytest.raises(MeasureError, match="could not decode"):
        decode_frame(b"not an image" * 10)


def test_brightness_solid_and_clamped() -> None:
    img = decode_frame(_encode(np.full((10, 10, 3), 255, dtype=np.uint8)))
    assert brightness(img, (0, 0, 10, 10)) == pytest.approx(1.0)
    assert brightness(img, (-50, -50, 500, 500)) == pytest.approx(1.0)
    dark = decode_frame(_encode(np.zeros((10, 10, 3), dtype=np.uint8)))
    assert brightness(dark, (0, 0, 10, 10)) == pytest.approx(0.0)
    with pytest.raises(MeasureError, match="empty"):
        brightness(img, (5, 5, 5, 9))  # zero width
    with pytest.raises(MeasureError, match="empty"):
        brightness(img, (50, 50, 60, 60))  # fully outside


def test_brightness_tracks_small_hot_spot_not_box_mean() -> None:
    """A lit LED covering ~10% of a loose box must still read near its own level."""
    raw = np.full((60, 60, 3), 40, dtype=np.uint8)
    lit = raw.copy()
    lit[25:44, 25:44] = 255  # ~10% of the box
    off = brightness(decode_frame(_encode(raw)), (0, 0, 60, 60))
    on = brightness(decode_frame(_encode(lit)), (0, 0, 60, 60))
    assert on - off > 0.5  # a box mean would move only ~0.08


def test_classify_lit_unlit_and_gray_zone() -> None:
    base = BrightnessBaseline(off=0.1, on=0.9)
    assert classify_brightness(0.8, base) is True
    assert classify_brightness(0.2, base) is False
    assert classify_brightness(0.5, base) is None  # on the threshold
    assert classify_brightness(0.8, None) is None
    assert classify_brightness(0.8, BrightnessBaseline(off=0.5, on=0.5)) is None
    assert classify_brightness(0.8, BrightnessBaseline(off=0.9, on=0.1)) is None


def _bar(angle_deg: int) -> np.ndarray:
    """White bar across a black 100x100 field at ``angle_deg`` from horizontal."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    theta = np.radians(angle_deg)
    dx, dy = int(40 * np.cos(theta)), int(40 * np.sin(theta))
    cv2.line(img, (50 - dx, 50 - dy), (50 + dx, 50 + dy), (255, 255, 255), 6)
    return decode_frame(_encode(img))


def test_arm_angle_cardinal_bars() -> None:
    assert arm_angle(_bar(0), (0, 0, 100, 100)) == pytest.approx(0.0, abs=3.0)
    assert arm_angle(_bar(90), (0, 0, 100, 100)) == pytest.approx(90.0, abs=3.0)


def test_arm_angle_diagonal_bar() -> None:
    assert arm_angle(_bar(135), (0, 0, 100, 100)) == pytest.approx(135.0, abs=5.0)


def test_arm_angle_blank_or_tiny_is_none() -> None:
    blank = decode_frame(_encode(np.zeros((100, 100, 3), dtype=np.uint8)))
    assert arm_angle(blank, (0, 0, 100, 100)) is None
    tiny = np.zeros((100, 100, 3), dtype=np.uint8)
    tiny[50, 50] = 255  # one pixel: far below the area fraction
    assert arm_angle(decode_frame(_encode(tiny)), (0, 0, 100, 100)) is None


def test_match_angle_needs_measurement_and_baseline() -> None:
    assert match_angle(92.0, 90, {90: 90.0}, 10.0) is True
    assert match_angle(70.0, 90, {90: 90.0}, 10.0) is False
    assert match_angle(None, 90, {90: 90.0}, 10.0) is None
    assert match_angle(92.0, 90, None, 10.0) is None
    assert match_angle(92.0, 0, {90: 90.0}, 10.0) is None


def test_calibration_roundtrip_and_baseline(tmp_path) -> None:
    cal = Calibration(
        brightness_off={"lamp_red": 0.1},
        brightness_on={"lamp_red": 0.9},
        angles={"arm_angle": {90: 88.5}},
        boxes={"lamp_red": BoxRecord(bbox=(1, 2, 3, 4), score=0.6, source="live")},
    )
    assert cal.baseline("lamp_red") == BrightnessBaseline(off=0.1, on=0.9)
    assert cal.baseline("lamp_green") is None
    assert Calibration.from_dict(cal.to_dict()) == cal
    path = tmp_path / "cal" / "calibration.json"
    cal.save(path)
    assert Calibration.load(path) == cal
    half = Calibration(brightness_off={"lamp_red": 0.1})
    assert half.baseline("lamp_red") is None
    assert Calibration.from_dict({}) == Calibration()
