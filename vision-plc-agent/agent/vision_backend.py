"""Vision interface: SAM 3.1 via the Meta Model API Responses endpoint + fakes.

Image requests are unary ``POST {base}/responses`` calls carrying the noun
phrase and the frame inline as a data URL; the reply's ``output_text`` holds
one ``<Nf>`` line per frame with box + mask records. We parse the boxes only
(the mask payloads stay opaque) and measure brightness/angle inside the box
(see :mod:`agent.measure`).
"""

from __future__ import annotations

import base64
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self

import httpx

DEFAULT_VISION_MODEL = "sam-3.1"

_FRAME_RE = re.compile(r"<(?P<frame>\d+)f(?:;[^>]*)?>")
_SEGMENT_RE = re.compile(r"(?P<id>\d+)(?P<tokens>(?:<\|[^|]*\|>)+)")
_BOX_RE = re.compile(r"<\|box;(?P<body>[^|]*)\|>")


@dataclass(frozen=True)
class Mask:
    label: str
    bbox: tuple[int, int, int, int]  # half-open x0, y0, x1, y1 in pixels
    score: float | None = None  # per-record confidence (None when not reported)


@dataclass(frozen=True)
class VisionResult:
    masks: tuple[Mask, ...]
    confidence: float


class VisionError(RuntimeError):
    """Transport, auth, API or parse error talking to the vision model."""


class VisionBackend(ABC):
    @abstractmethod
    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        """Segment ``prompt`` (an I/O-map vision prompt) in a JPEG ``frame``."""


class FakeVisionBackend(VisionBackend):
    """Deterministic fake: one full-frame mask at full confidence."""

    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        return VisionResult(masks=(Mask(label=prompt, bbox=(0, 0, 10, 10)),), confidence=1.0)


class ScriptedVisionBackend(VisionBackend):
    """Canned :class:`VisionResult` objects in call order; records prompts.

    Raises :class:`VisionError` when exhausted.
    """

    def __init__(self, results: list[VisionResult]) -> None:
        self._results = list(results)
        self.prompts: list[str] = []

    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        self.prompts.append(prompt)
        if not self._results:
            raise VisionError(f"script exhausted after {len(self.prompts)} call(s)")
        return self._results.pop(0)


def parse_output_text(text: str, label: str) -> tuple[Mask, ...]:
    """Parse SAM ``output_text`` into box masks, skipping malformed segments.

    Coordinates on the wire are inclusive; bboxes come out half-open
    (``right = x2 + 1``). An empty lane (no matches) yields no masks.
    """
    masks: list[Mask] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        frame_match = _FRAME_RE.match(line)
        if frame_match is None:
            continue
        for segment in _SEGMENT_RE.finditer(line, frame_match.end()):
            box_match = _BOX_RE.search(segment.group("tokens"))
            if box_match is None:
                continue
            mask = _parse_box(box_match.group("body"), label)
            if mask is not None:
                masks.append(mask)
    return tuple(masks)


def _parse_box(body: str, label: str) -> Mask | None:
    fields: dict[str, str] = {}
    for field in body.split(";"):
        field = field.strip()
        if not field or "=" not in field:
            continue
        key, _, value = field.partition("=")
        fields[key.strip()] = value.strip()
    try:
        x1 = int(fields["x1"])
        y1 = int(fields["y1"])
        x2 = int(fields["x2"])
        y2 = int(fields["y2"])
        width = int(fields["w"])
        height = int(fields["h"])
    except (KeyError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    if not (0 <= x1 <= x2 < width and 0 <= y1 <= y2 < height):
        return None
    score: float | None = None
    if "c" in fields:
        try:
            candidate = float(fields["c"])
        except ValueError:
            candidate = float("nan")
        if 0.0 <= candidate <= 1.0:
            score = candidate
    return Mask(label=label, bbox=(x1, y1, x2 + 1, y2 + 1), score=score)


def _sniff_mime(frame: bytes) -> str:
    if frame.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if frame.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise VisionError(f"frame is neither JPEG nor PNG (first bytes {frame[:8]!r})")


class MetaSamBackend(VisionBackend):
    """Real client for SAM 3.1 over the Meta Model API ``/responses`` endpoint.

    Args:
        base_url: e.g. ``"https://api.meta.ai/v1"`` (``/responses`` appended).
        api_key: Bearer [REDACTED]
        model: e.g. ``"sam-3.1"``.
        transport: injectable httpx transport (tests use MockTransport).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_VISION_MODEL,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise VisionError("empty api_key (set META_API_KEY)")
        self._url = base_url.rstrip("/") + "/responses"
        self._key = api_key
        self._model = model
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_env(
        cls, *, timeout_s: float = 120.0, transport: httpx.BaseTransport | None = None
    ) -> Self:
        """Build from ``META_API_BASE_URL`` / ``META_API_KEY`` / ``VISION_MODEL``."""
        base_url = os.environ.get("META_API_BASE_URL", "").strip()
        api_key = os.environ.get("META_API_KEY", "").strip()
        model = os.environ.get("VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL
        missing = [
            name
            for name, value in (("META_API_BASE_URL", base_url), ("META_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise VisionError(f"missing env vars: {', '.join(missing)} (see .env.example)")
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_s=timeout_s,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        if not prompt.strip():
            raise VisionError("empty prompt")
        mime = _sniff_mime(frame)
        payload = base64.b64encode(frame).decode("ascii")
        try:
            response = self._client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Accept": "application/json",
                },
                json={
                    "model": self._model,
                    "stream": False,
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt.strip()},
                                {
                                    "type": "input_image",
                                    "image_url": f"data:{mime};base64,{payload}",
                                },
                            ],
                        }
                    ],
                    "metadata": {"include_confidence": "true"},
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VisionError(
                f"responses failed: HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionError(f"responses failed: {exc}") from exc
        text = _output_text(response)
        masks = parse_output_text(text, prompt.strip())
        scores = [m.score for m in masks if m.score is not None]
        if not masks:
            return VisionResult(masks=(), confidence=0.0)
        return VisionResult(masks=masks, confidence=min(scores) if scores else 1.0)


def _output_text(response: httpx.Response) -> str:
    """Concatenated ``output_text`` of a completed unary ``/responses`` reply."""
    try:
        data = response.json()
    except ValueError as exc:
        raise VisionError(f"invalid JSON in reply {response.text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise VisionError(f"unexpected reply {response.text[:200]!r}")
    if data.get("status") != "completed":
        raise VisionError(f"run not completed ({data.get('status')!r}): {response.text[:200]!r}")
    texts: list[str] = []
    output = data.get("output", [])
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    texts.append(part["text"])
    return "\n".join(texts)
