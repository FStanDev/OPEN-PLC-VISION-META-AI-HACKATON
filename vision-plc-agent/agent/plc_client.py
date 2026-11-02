"""OpenPLC Runtime v4 REST client + fake.

Endpoints per SPEC (``https://<pi>:8443/api``, self-signed TLS, JWT auth):
login, upload-file, compilation-status, start/stop-plc, status.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Self

import httpx


class PlcError(RuntimeError):
    """Transport, auth or API error talking to OpenPLC."""


@dataclass(frozen=True)
class PlcStatus:
    running: bool
    compile_state: str  # "NONE", "COMPILING", "SUCCESS" or "FAILED"


class PlcClient(ABC):
    @abstractmethod
    def login(self, username: str, password: str) -> str:
        """Authenticate; return the bearer token."""

    @abstractmethod
    def upload_program(self, program_zip: bytes) -> None:
        """Upload ``program.zip`` (multipart field ``file``)."""

    @abstractmethod
    def compilation_status(self) -> str:
        """Poll until ``SUCCESS`` or ``FAILED``."""

    @abstractmethod
    def start(self) -> None:
        """Start the PLC."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the PLC."""

    @abstractmethod
    def status(self) -> PlcStatus:
        """Current run state."""


@dataclass
class FakePlcClient(PlcClient):
    """In-memory fake recording uploads and run state."""

    uploads: list[bytes] = field(default_factory=list)
    _token: str | None = None
    _compile_state: str = "NONE"
    _running: bool = False

    def login(self, username: str, password: str) -> str:
        self._token = "fake-token"
        return self._token

    def upload_program(self, program_zip: bytes) -> None:
        self.uploads.append(program_zip)
        self._compile_state = "SUCCESS"

    def compilation_status(self) -> str:
        return self._compile_state

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def status(self) -> PlcStatus:
        return PlcStatus(running=self._running, compile_state=self._compile_state)


TERMINAL_COMPILE_STATES = ("SUCCESS", "FAILED")


class RestPlcClient(PlcClient):
    """Real client for ``https://<pi>:8443/api``.

    Args:
        base_url: e.g. ``"https://raspberrypi:8443"`` (``/api`` is appended).
        verify_tls: CA bundle path, or False for the runtime's self-signed cert.
        transport: injectable httpx transport (tests use MockTransport).
    """

    def __init__(
        self,
        base_url: str,
        *,
        verify_tls: bool | str = False,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api = base_url.rstrip("/") + "/api"
        self._client = httpx.Client(verify=verify_tls, timeout=timeout_s, transport=transport)
        self._token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def login(self, username: str, password: str) -> str:
        try:
            response = self._client.post(
                f"{self._api}/login", json={"username": username, "password": password}
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise PlcError(f"login failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise PlcError(f"login failed: {exc}") from exc
        try:
            token = response.json()["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PlcError(f"login failed: unexpected reply {response.text[:200]!r}") from exc
        if not token:
            raise PlcError("login failed: empty access token")
        self._token = token
        return token

    def upload_program(self, program_zip: bytes) -> None:
        self._request(
            "POST",
            "/upload-file",
            files={"file": ("program.zip", program_zip, "application/zip")},
        )

    def compilation_status(self) -> str:
        return _parse_status(self._request("GET", "/compilation-status"))

    def wait_for_compilation(self, *, timeout_s: float = 120.0, poll_s: float = 2.0) -> str:
        """Poll until SUCCESS or FAILED; raise PlcError on timeout."""
        deadline = monotonic() + timeout_s
        while True:
            state = self.compilation_status()
            if state in TERMINAL_COMPILE_STATES:
                return state
            if monotonic() >= deadline:
                raise PlcError(f"compilation timed out after {timeout_s}s (last: {state})")
            sleep(poll_s)

    def start(self) -> None:
        self._request("GET", "/start-plc")

    def stop(self) -> None:
        self._request("GET", "/stop-plc")

    def status(self) -> PlcStatus:
        response = self._request("GET", "/status")
        try:
            data = response.json()
        except ValueError as exc:
            raise PlcError(f"status: unexpected reply {response.text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise PlcError(f"status: unexpected reply {response.text[:200]!r}")
        # Observed v4.3.1 shape: {"status": "STATUS:RUNNING", "switchPosition": "run"}.
        status_text = str(data.get("status", "")).lower()
        if status_text.startswith("status:"):
            status_text = status_text[len("status:") :]
        # The runtime state wins; switchPosition is only the requested mode
        # (it stays "run" after a stop, observed 2026-11-02).
        if "running" in data:
            running = bool(data["running"])
        elif status_text:
            running = status_text in {"running", "started", "on"}
        else:
            running = str(data.get("switchPosition", "")).lower() in {"run", "running"}
        compile_state = str(data.get("compilation_status", data.get("compile_state", "NONE")))
        return PlcStatus(running=running, compile_state=compile_state)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            raise PlcError("not logged in: call login() first")
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(
                method, f"{self._api}{path}", headers=self._headers(), **kwargs
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise PlcError(
                f"{method} {path} failed: HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise PlcError(f"{method} {path} failed: {exc}") from exc
        return response


def _parse_status(response: httpx.Response) -> str:
    """Normalize /compilation-status: ``{"status": ...}`` dict or plain text."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("status", "compilation_status", "state"):
            if data.get(key):
                return str(data[key]).upper()
    text = response.text.strip().strip('"').upper()
    if text and not text.startswith("{"):
        return text
    raise PlcError(f"compilation-status: unexpected reply {response.text[:200]!r}")
