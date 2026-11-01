"""Stimulus service client interface + fake + real HTTP client."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Self

import httpx


@dataclass(frozen=True)
class StimulusEvent:
    action: Literal["press", "release"]
    button: str


class Stimulus(ABC):
    @abstractmethod
    def press(self, button: str) -> None:
        """Drive the stimulus pin LOW (electrical press)."""

    @abstractmethod
    def release(self, button: str) -> None:
        """Return the stimulus pin to high impedance."""


@dataclass
class FakeStimulus(Stimulus):
    """Fake recording the stimulus timeline."""

    events: list[StimulusEvent] = field(default_factory=list)

    def press(self, button: str) -> None:
        self.events.append(StimulusEvent("press", button))

    def release(self, button: str) -> None:
        self.events.append(StimulusEvent("release", button))


class StimulusError(RuntimeError):
    """Transport or API error talking to the stimulus service."""


class HttpStimulus(Stimulus):
    """Real client for the Pi stimulus service (``STIMULUS_URL``).

    Args:
        base_url: e.g. ``"http://raspberrypi:8081"``.
        transport: injectable httpx transport (tests use MockTransport).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
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

    def press(self, button: str) -> None:
        self._post("/press", button)

    def release(self, button: str) -> None:
        self._post("/release", button)

    def _post(self, path: str, button: str) -> None:
        try:
            response = self._client.post(f"{self._base}{path}", json={"button": button})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            raise StimulusError(
                f"{path} {button!r} failed: HTTP {exc.response.status_code} ({detail})"
            ) from exc
        except httpx.HTTPError as exc:
            raise StimulusError(f"{path} {button!r} failed: {exc}") from exc


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.text[:200]))
    except ValueError:
        return response.text[:200]
