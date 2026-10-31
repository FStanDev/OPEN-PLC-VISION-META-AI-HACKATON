"""Stimulus service client interface + fake.

The real HTTP client (driving GPIO26/GPIO13 on the Pi) lands in milestone 3.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


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
