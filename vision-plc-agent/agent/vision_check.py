"""Vision check: grab a frame, segment the I/O-map prompts, measure, report.

Live mode talks to the Pi frame service and SAM 3.1; ``--fake`` runs the
same plumbing on a synthetic frame with canned segmentation. Calibration
modes capture baselines into ``calibration.json`` — thresholds always come
from there, never from hard-coded values.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np

from agent.dotenv import load_dotenv
from agent.frames import FakeFrameSource, FrameSource, HttpFrameSource
from agent.io_map import IoMap, load_io_map
from agent.measure import (
    BoxRecord,
    Calibration,
    MeasureError,
    arm_angle,
    brightness,
    classify_brightness,
    decode_frame,
    match_angle,
)
from agent.vision_backend import (
    FakeVisionBackend,
    Mask,
    MetaSamBackend,
    VisionBackend,
    VisionError,
)

ANGLE_TOLERANCE_DEFAULT = 10.0


def _best_mask(masks: tuple[Mask, ...]) -> Mask | None:
    if not masks:
        return None
    return max(masks, key=lambda m: (m.score is not None, m.score or 0.0))


def _synthetic_frame() -> bytes:
    """Decodable fake frame: dark field, bright 10x10 cue where the fake mask is."""
    img = np.full((100, 100, 3), 20, dtype=np.uint8)
    img[0:10, 0:10] = 255
    ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise MeasureError("synthetic frame encode failed")
    return bytes(jpeg)


def check_signal(
    name: str,
    prompt: str,
    kind: str,
    frame: bytes,
    vision: VisionBackend,
    calibration: Calibration,
    *,
    expect_angle: int | None,
    angle_tolerance: float,
) -> dict[str, object]:
    """Measure one signal; failures become inconclusive entries.

    A stored calibration box is reused without calling SAM (the camera is
    fixed after calibration); otherwise the prompt is segmented live.
    """
    entry: dict[str, object] = {"prompt": prompt, "measure": kind, "status": "inconclusive"}
    stored = calibration.boxes.get(name)
    if stored is not None:
        mask = Mask(label=prompt, bbox=stored.bbox, score=stored.score)
        entry["confidence"] = stored.score if stored.score is not None else 1.0
        entry["box_source"] = "manual" if stored.source == "manual" else "stored"
    else:
        try:
            result = vision.segment(frame, prompt)
        except VisionError as exc:
            entry["error"] = str(exc)
            return entry
        entry["confidence"] = result.confidence
        mask = _best_mask(result.masks)
        if mask is None:
            entry["error"] = "no mask returned"
            return entry
        entry["box_source"] = "live"
    entry["bbox"] = list(mask.bbox)
    entry["score"] = mask.score
    try:
        img = decode_frame(frame)
        if kind == "brightness":
            value = brightness(img, mask.bbox)
            entry["brightness"] = round(value, 4)
            baseline = calibration.baseline(name)
            if baseline is None:
                entry["status"] = "uncalibrated"
            else:
                lit = classify_brightness(value, baseline)
                entry["lit"] = lit
                entry["status"] = "measured" if lit is not None else "inconclusive"
        else:
            value = arm_angle(img, mask.bbox)
            entry["angle"] = None if value is None else round(value, 2)
            baselines = calibration.angles.get(name)
            if value is None or baselines is None:
                entry["status"] = "uncalibrated" if value is not None else "inconclusive"
            elif expect_angle is None:
                entry["status"] = "measured"
            else:
                matched = match_angle(value, expect_angle, baselines, angle_tolerance)
                entry["angle_match"] = matched
                entry["status"] = "measured" if matched is not None else "inconclusive"
    except MeasureError as exc:
        entry["error"] = str(exc)
    return entry


def _parse_box_overrides(raw: list[str]) -> dict[str, tuple[int, int, int, int]]:
    """Parse ``--box NAME=x0,y0,x1,y1`` flags (half-open pixels)."""
    overrides: dict[str, tuple[int, int, int, int]] = {}
    for item in raw:
        try:
            name, _, coords = item.partition("=")
            x0, y0, x1, y1 = (int(v) for v in coords.split(","))
        except ValueError:
            raise SystemExit(f"bad --box {item!r}: want NAME=x0,y0,x1,y1")
        if not name or x1 <= x0 or y1 <= y0:
            raise SystemExit(f"bad --box {item!r}: want NAME=x0,y0,x1,y1")
        overrides[name] = (x0, y0, x1, y1)
    return overrides


def _calibrate_signal(
    name: str,
    kind: str,
    prompt: str,
    frame: bytes,
    img: np.ndarray,
    vision: VisionBackend,
    calibration: Calibration,
    record_off: bool,
    record_on: bool,
    record_angle: int | None,
    overrides: dict[str, tuple[int, int, int, int]],
    signals: dict[str, dict[str, object]],
) -> bool:
    """Capture one baseline sample; failures become inconclusive entries."""
    wanted = (kind == "brightness" and (record_off or record_on)) or (
        kind == "angle" and record_angle is not None
    )
    if not wanted:
        signals[name] = {"status": "skipped", "error": "no matching calibrate flag for " + kind}
        return True
    if name in overrides:
        mask = Mask(label=prompt, bbox=overrides[name])
        box_source = "manual"
    else:
        try:
            result = vision.segment(frame, prompt)
        except VisionError as exc:
            signals[name] = {"status": "inconclusive", "error": str(exc)}
            return False
        found = _best_mask(result.masks)
        if found is None:
            signals[name] = {"status": "inconclusive", "error": "no mask returned"}
            return False
        mask = found
        box_source = "live"
    try:
        if kind == "brightness":
            value = brightness(img, mask.bbox)
            if record_off:
                calibration.brightness_off[name] = value
            if record_on:
                calibration.brightness_on[name] = value
            calibration.boxes[name] = BoxRecord(bbox=mask.bbox, score=mask.score, source=box_source)
            done = calibration.baseline(name) is not None
            signals[name] = {
                "status": "calibrated" if done else "half-calibrated",
                "brightness": round(value, 4),
                "bbox": list(mask.bbox),
                "box_source": box_source,
            }
            return True
        value = arm_angle(img, mask.bbox)
        if value is None:
            signals[name] = {"status": "inconclusive", "error": "no arm blob found"}
            return False
        assert record_angle is not None
        calibration.angles.setdefault(name, {})[record_angle] = value
        calibration.boxes[name] = BoxRecord(bbox=mask.bbox, score=mask.score, source=box_source)
        signals[name] = {
            "status": "calibrated",
            "angle": round(value, 2),
            "bbox": list(mask.bbox),
            "box_source": box_source,
        }
        return True
    except MeasureError as exc:
        signals[name] = {"status": "inconclusive", "error": str(exc)}
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Segment a rig frame and measure it.")
    parser.add_argument("--out", required=True, help="output dir for frame/calibration/report")
    parser.add_argument("--frames-url", default=None, help="frame service base URL")
    parser.add_argument("--frame", default=None, help="reuse a saved frame instead of grabbing")
    parser.add_argument("--fake", action="store_true", help="synthetic frame + fake vision")
    parser.add_argument("--io-map", default=None, help="io_map.yaml path (default: project file)")
    parser.add_argument("--calibration", default=None, help="calibration.json path")
    parser.add_argument("--calibrate-off", action="store_true", help="record brightness as off")
    parser.add_argument("--calibrate-on", action="store_true", help="record brightness as lamp-on")
    parser.add_argument(
        "--calibrate-angle",
        type=int,
        default=None,
        help="record angle baseline at this commanded angle",
    )
    parser.add_argument(
        "--box",
        action="append",
        default=[],
        help="manual region NAME=x0,y0,x1,y1 (repeatable, calibration only)",
    )
    parser.add_argument(
        "--expect-angle", type=int, default=None, help="commanded arm angle to match against"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=ANGLE_TOLERANCE_DEFAULT,
        help="angle match tolerance in degrees",
    )
    parser.add_argument(
        "--signals",
        default=None,
        help="comma-separated signal subset (default: all vision signals)",
    )
    args = parser.parse_args(argv)

    io_map: IoMap = load_io_map(args.io_map)
    targets = {n: s for n, s in io_map.signals.items() if s.vision is not None}
    if args.signals:
        wanted = {n.strip() for n in args.signals.split(",") if n.strip()}
        unknown = wanted - set(targets)
        if unknown:
            raise SystemExit(f"unknown vision signals: {sorted(unknown)}")
        targets = {n: s for n, s in targets.items() if n in wanted}
    if not targets:
        raise SystemExit("no signals with vision prompts in the I/O map")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    calibration_path = Path(args.calibration) if args.calibration else out / "calibration.json"
    calibration = (
        Calibration.load(calibration_path) if calibration_path.is_file() else Calibration()
    )

    frames: FrameSource
    vision: VisionBackend
    if args.fake:
        frame = _synthetic_frame()
        frames = FakeFrameSource()
        vision = FakeVisionBackend()
    else:
        load_dotenv()
        vision = MetaSamBackend.from_env()
        if args.frame:
            frame = Path(args.frame).read_bytes()
            frames = FakeFrameSource()
        else:
            base_url = args.frames_url or os.environ.get("FRAMES_URL", "").strip()
            if not base_url:
                raise SystemExit("set FRAMES_URL in .env or pass --frames-url")
            frames = HttpFrameSource(base_url)
            frame = frames.grab()
    try:
        (out / "frame.jpg").write_bytes(frame)
        try:
            img = decode_frame(frame)
        except MeasureError as exc:
            print(f"vision: cannot decode frame: {exc}")
            return 1
        calibrating = args.calibrate_off or args.calibrate_on or args.calibrate_angle is not None

        signals: dict[str, dict[str, object]] = {}
        report: dict[str, object] = {"signals": signals}
        success = True
        for name, signal in targets.items():
            assert signal.vision is not None
            kind = signal.vision.measure
            if calibrating:
                success = (
                    _calibrate_signal(
                        name,
                        kind,
                        signal.vision.prompt,
                        frame,
                        img,
                        vision,
                        calibration,
                        args.calibrate_off,
                        args.calibrate_on,
                        args.calibrate_angle,
                        _parse_box_overrides(args.box),
                        signals,
                    )
                    and success
                )
            else:
                entry = check_signal(
                    name,
                    signal.vision.prompt,
                    kind,
                    frame,
                    vision,
                    calibration,
                    expect_angle=args.expect_angle,
                    angle_tolerance=args.tolerance,
                )
                signals[name] = entry
                if entry["status"] != "measured":
                    success = False

        if calibrating:
            calibration.save(calibration_path)
            report["calibration"] = str(calibration_path)
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        states = ", ".join(f"{n}={e.get('status')}" for n, e in signals.items())
        print(f"vision: {states} -> {out}")
        return 0 if success else 1
    finally:
        if isinstance(frames, HttpFrameSource):
            frames.close()
        if isinstance(vision, MetaSamBackend):
            vision.close()


if __name__ == "__main__":
    raise SystemExit(main())
