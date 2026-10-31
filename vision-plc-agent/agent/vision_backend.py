"""Vision interface (SAM 3.1 via the Meta Model API) + fake.

The real client lands in milestone 5.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Mask:
    label: str
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels


@dataclass(frozen=True)
class VisionResult:
    masks: tuple[Mask, ...]
    confidence: float


class VisionBackend(ABC):
    @abstractmethod
    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        """Segment ``prompt`` (an I/O-map vision prompt) in a JPEG ``frame``."""


class FakeVisionBackend(VisionBackend):
    """Deterministic fake: one full-frame mask at full confidence."""

    def segment(self, frame: bytes, prompt: str) -> VisionResult:
        return VisionResult(masks=(Mask(label=prompt, bbox=(0, 0, 10, 10)),), confidence=1.0)
