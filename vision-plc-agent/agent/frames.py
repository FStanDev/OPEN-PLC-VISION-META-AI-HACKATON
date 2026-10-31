"""Frame service client interface + fake.

The real HTTP client (C920 stills over Tailscale) lands in milestone 3.
"""

from abc import ABC, abstractmethod

_FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(32) + b"\xff\xd9"


class FrameSource(ABC):
    @abstractmethod
    def grab(self) -> bytes:
        """Grab one still frame as JPEG bytes."""


class FakeFrameSource(FrameSource):
    """Fake returning fixed marker bytes (opaque to callers)."""

    def grab(self) -> bytes:
        return _FAKE_JPEG
