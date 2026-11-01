"""Frame service client interface + fake + real HTTP client."""

from abc import ABC, abstractmethod
from typing import Self

import httpx

_FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(32) + b"\xff\xd9"


class FrameSource(ABC):
    @abstractmethod
    def grab(self) -> bytes:
        """Grab one still frame as JPEG bytes."""


class FakeFrameSource(FrameSource):
    """Fake returning fixed marker bytes (opaque to callers)."""

    def grab(self) -> bytes:
        return _FAKE_JPEG


class FrameError(RuntimeError):
    """Transport, camera or API error grabbing a frame."""


class HttpFrameSource(FrameSource):
    """Real client for the Pi frame service (``FRAMES_URL``).

    Args:
        base_url: e.g. ``"http://raspberrypi:8082"``.
        transport: injectable httpx transport (tests use MockTransport).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def grab(self) -> bytes:
        try:
            response = self._client.get(f"{self._base}/frame")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FrameError(
                f"grab failed: HTTP {exc.response.status_code} ({exc.response.text[:200]})"
            ) from exc
        except httpx.HTTPError as exc:
            raise FrameError(f"grab failed: {exc}") from exc
        if not response.content:
            raise FrameError("grab failed: empty frame")
        return response.content
