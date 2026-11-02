"""LED brightness and servo angle from masks, plus calibration (SPEC steps 6/8).

Thresholds always come from a per-session :class:`Calibration` — never from
hard-coded expected values. Anything that cannot be measured honestly
(no mask, degenerate crop, missing baseline) reports ``None`` so the caller
can mark the expectation inconclusive instead of faking a value.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

GRAY_ZONE_FRACTION = 0.15  # "too close to the threshold to call" band
MIN_ARM_AREA_FRACTION = 0.01  # smallest believable arm blob, vs the crop area
BRIGHTNESS_PERCENTILE = 95  # LED hot spot must cover >= 5% of its box


class MeasureError(RuntimeError):
    """A frame could not be decoded or a crop could not be measured."""


@dataclass(frozen=True)
class BrightnessBaseline:
    off: float  # mean brightness (0..1) with the lamp off
    on: float  # mean brightness (0..1) with the lamp on

    @property
    def threshold(self) -> float:
        return (self.off + self.on) / 2

    @property
    def margin(self) -> float:
        return abs(self.on - self.off) / 2


@dataclass(frozen=True)
class BoxRecord:
    """Measurement region for one signal: a SAM box kept from calibration
    (``source="live"``) or a human-labelled region (``source="manual"``)."""

    bbox: tuple[int, int, int, int]  # half-open x0, y0, x1, y1
    score: float | None = None
    source: str = "live"


@dataclass
class Calibration:
    """Per-session baselines, keyed by I/O-map signal name.

    Off/on brightness accumulate independently so a half-finished
    calibration (only ``off`` captured so far) still persists.
    """

    brightness_off: dict[str, float] = field(default_factory=dict)
    brightness_on: dict[str, float] = field(default_factory=dict)
    angles: dict[str, dict[int, float]] = field(default_factory=dict)
    boxes: dict[str, BoxRecord] = field(default_factory=dict)

    def baseline(self, name: str) -> BrightnessBaseline | None:
        """Complete off/on baseline, or None while either side is missing."""
        if name in self.brightness_off and name in self.brightness_on:
            return BrightnessBaseline(off=self.brightness_off[name], on=self.brightness_on[name])
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "brightness_off": dict(self.brightness_off),
            "brightness_on": dict(self.brightness_on),
            "angles": {
                name: {str(deg): value for deg, value in table.items()}
                for name, table in self.angles.items()
            },
            "boxes": {
                name: {
                    "bbox": list(box.bbox),
                    "score": box.score,
                    "source": box.source,
                }
                for name, box in self.boxes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Calibration:
        def floats(raw: object) -> dict[str, float]:
            if not isinstance(raw, dict):
                return {}
            return {str(name): float(value) for name, value in raw.items()}

        angles: dict[str, dict[int, float]] = {}
        raw_angles = data.get("angles", {})
        if isinstance(raw_angles, dict):
            for name, table in raw_angles.items():
                if isinstance(table, dict):
                    angles[str(name)] = {int(deg): float(value) for deg, value in table.items()}
        boxes: dict[str, BoxRecord] = {}
        raw_boxes = data.get("boxes", {})
        if isinstance(raw_boxes, dict):
            for name, entry in raw_boxes.items():
                if not isinstance(entry, dict):
                    continue
                raw_bbox = entry.get("bbox")
                if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                    continue
                x0, y0, x1, y1 = (int(v) for v in raw_bbox)
                score = entry.get("score")
                boxes[str(name)] = BoxRecord(
                    bbox=(x0, y0, x1, y1),
                    score=None if score is None else float(score),
                    source=str(entry.get("source", "live")),
                )
        return cls(
            brightness_off=floats(data.get("brightness_off")),
            brightness_on=floats(data.get("brightness_on")),
            angles=angles,
            boxes=boxes,
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Calibration:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def decode_frame(frame: bytes) -> np.ndarray:
    """Decode JPEG/PNG bytes to a BGR image."""
    if not frame:
        raise MeasureError("empty frame")
    img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise MeasureError(f"could not decode {len(frame)} frame bytes")
    return img


def crop_for_mask(img: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Crop ``img`` to the half-open ``bbox`` (x0, y0, x1, y1), clamped."""
    height, width = img.shape[:2]
    x0 = min(max(bbox[0], 0), width)
    y0 = min(max(bbox[1], 0), height)
    x1 = min(max(bbox[2], 0), width)
    y1 = min(max(bbox[3], 0), height)
    if x1 <= x0 or y1 <= y0:
        raise MeasureError(f"mask bbox {bbox} is empty inside a {width}x{height} frame")
    return img[y0:y1, x0:x1]


def brightness(img: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
    """High-percentile grayscale inside ``bbox`` as 0..1.

    A lit LED is a small hot spot in a box that is mostly legs, breadboard
    and background; a box mean dilutes it (bench: ~10% of a manual box).
    The ``BRIGHTNESS_PERCENTILE`` value tracks the hot spot while ignoring a
    few specular pixels.
    """
    crop = crop_for_mask(img, bbox)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(np.percentile(gray, BRIGHTNESS_PERCENTILE)) / 255.0


def classify_brightness(value: float, baseline: BrightnessBaseline | None) -> bool | None:
    """Lit (True), unlit (False) or inconclusive (None) vs the baseline.

    ``None`` means: no baseline, a degenerate baseline (on <= off), or the
    reading falls in the gray zone around the threshold.
    """
    if baseline is None or baseline.on <= baseline.off:
        return None
    if abs(value - baseline.threshold) < GRAY_ZONE_FRACTION * baseline.margin:
        return None
    return value > baseline.threshold


def arm_angle(img: np.ndarray, bbox: tuple[int, int, int, int]) -> float | None:
    """Principal-axis angle (0..180 deg, image plane) of the arm blob in ``bbox``.

    Otsu-thresholds the crop and takes the largest bright blob's orientation.
    ``None`` means no believable blob was found.
    """
    crop = crop_for_mask(img, bbox)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(biggest) < MIN_ARM_AREA_FRACTION * crop.shape[0] * crop.shape[1]:
        return None
    moments = cv2.moments(biggest)
    denom = moments["mu20"] - moments["mu02"]
    angle = 0.5 * math.degrees(math.atan2(2 * moments["mu11"], denom))
    return angle % 180.0


def match_angle(
    measured: float | None,
    commanded: int,
    baselines: dict[int, float] | None,
    tolerance: float,
) -> bool | None:
    """True/False/inconclusive (None) vs the calibration baseline at ``commanded``.

    ``None`` means no measurement or no baseline for that commanded angle —
    never a guess.
    """
    if measured is None or baselines is None or commanded not in baselines:
        return None
    return abs(measured - baselines[commanded]) <= tolerance
