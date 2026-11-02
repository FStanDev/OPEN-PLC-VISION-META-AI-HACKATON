"""LLM interface (Muse Spark via the Meta Model API) + fakes.

The Meta Model API is OpenAI-compatible, so the real client is a small
``httpx`` POST to ``/chat/completions`` (no extra dependency).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self

import httpx

DEFAULT_MODEL = "muse-spark-1.3"
EFFORTS = ("low", "high")


@dataclass(frozen=True)
class LlmReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LlmError(RuntimeError):
    """Transport, auth or API error talking to the LLM."""


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


class ScriptedLlmBackend(LlmBackend):
    """Canned replies in call order; records ``(effort, prompt)`` per call.

    Accepts plain strings (input tokens counted, output tokens 0) or
    full :class:`LlmReply` objects. Raises :class:`LlmError` when exhausted.
    """

    def __init__(self, replies: list[str | LlmReply]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, *, effort: str = "low") -> LlmReply:
        self.calls.append((effort, prompt))
        if not self._replies:
            raise LlmError(f"script exhausted after {len(self.calls)} call(s)")
        reply = self._replies.pop(0)
        if isinstance(reply, str):
            return LlmReply(text=reply, input_tokens=len(prompt.split()))
        return reply


class MetaLlmBackend(LlmBackend):
    """Real client for the Meta Model API (OpenAI-compatible ``/chat/completions``).

    Args:
        base_url: e.g. ``"https://api.meta.ai/v1"`` (``/chat/completions`` appended).
        api_key: Bearer [REDACTED]
        model: e.g. ``"muse-spark-1.3"``.
        transport: injectable httpx transport (tests use MockTransport).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise LlmError("empty api_key (set META_API_KEY)")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_env(
        cls, *, timeout_s: float = 180.0, transport: httpx.BaseTransport | None = None
    ) -> Self:
        """Build from ``META_API_BASE_URL`` / ``META_API_KEY`` / ``LLM_MODEL``."""
        base_url = os.environ.get("META_API_BASE_URL", "").strip()
        api_key = os.environ.get("META_API_KEY", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL
        missing = [
            name
            for name, value in (("META_API_BASE_URL", base_url), ("META_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise LlmError(f"missing env vars: {', '.join(missing)} (see .env.example)")
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

    def generate(self, prompt: str, *, effort: str = "low") -> LlmReply:
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, got {effort!r}")
        try:
            response = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "reasoning_effort": effort,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlmError(
                f"chat completions failed: HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"chat completions failed: {exc}") from exc
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"unexpected reply {response.text[:200]!r}") from exc
        if not isinstance(text, str) or not text.strip():
            raise LlmError(f"empty completion in reply {response.text[:200]!r}")
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        return LlmReply(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
