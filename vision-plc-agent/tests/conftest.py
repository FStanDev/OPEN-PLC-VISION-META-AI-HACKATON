import pytest


@pytest.fixture(autouse=True)
def _localhost_bypasses_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live localhost servers must not go through any configured proxy."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
