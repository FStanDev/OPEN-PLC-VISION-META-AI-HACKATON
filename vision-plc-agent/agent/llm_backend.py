"""LLM interface (Muse Spark via the Meta Model API) + fake.

The real OpenAI-compatible client lands in milestone 4.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LlmReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LlmBackend(ABC):
    @abstractmethod
    def generate(self, prompt: str, *, effort: str = "low") -> LlmReply:
        """Complete ``prompt``; ``effort`` is ``"low"`` or ``"high"`` per SPEC."""


class FakeLlmBackend(LlmBackend):
    """Deterministic fake for tests and offline runs."""

    def generate(self, prompt: str, *, effort: str = "low") -> LlmReply:
        return LlmReply(
            text="(* fake ST reply *)",
            input_tokens=len(prompt.split()),
            output_tokens=4,
        )
