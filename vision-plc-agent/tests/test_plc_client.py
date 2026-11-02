"""RestPlcClient tests against a MockTransport fake of the v4 REST API."""

import json
from typing import Any

import httpx
import pytest

from agent.plc_client import PlcError, RestPlcClient

TOKEN = "jwt-token"


def make_transport(state: dict[str, Any], calls: list[tuple[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/login":
            if state.get("login_mode") == "bad-json":
                return httpx.Response(200, text="oops")
            body = json.loads(request.content)
            if (body.get("username"), body.get("password")) == ("admin", "secret"):
                return httpx.Response(200, json={"access_token": TOKEN})
            return httpx.Response(401, json={"detail": "bad credentials"})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"Detail": "unauthorized"})
        if request.url.path == "/api/upload-file":
            state["upload"] = bytes(request.content)
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/compilation-status":
            queue = state["compile"]
            if len(queue) > 1:
                return httpx.Response(200, json={"status": queue.pop(0)})
            return httpx.Response(200, json={"status": queue[0]})
        if request.url.path == "/api/start-plc":
            state["running"] = True
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/stop-plc":
            state["running"] = False
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/status":
            if state.get("status_mode") == "bad-shape":
                return httpx.Response(200, text="running?")
            if state.get("status_mode") == "v4-shape":
                return httpx.Response(
                    200, json={"status": "STATUS:RUNNING", "switchPosition": "run"}
                )
            return httpx.Response(
                200,
                json={"running": state["running"], "compilation_status": state["compile"][-1]},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_client(state: dict[str, Any], calls: list[tuple[str, str]]) -> RestPlcClient:
    return RestPlcClient("https://pi:8443", transport=make_transport(state, calls))


def fresh_state() -> dict[str, Any]:
    return {"compile": ["SUCCESS"], "running": False}


def test_login_returns_token() -> None:
    state, calls = fresh_state(), []
    plc = make_client(state, calls)
    assert plc.login("admin", "secret") == TOKEN
    assert calls == [("POST", "/api/login")]


def test_login_rejects_bad_credentials() -> None:
    plc = make_client(fresh_state(), [])
    with pytest.raises(PlcError, match="HTTP 401"):
        plc.login("admin", "wrong")


def test_login_rejects_unexpected_reply() -> None:
    state = fresh_state()
    state["login_mode"] = "bad-json"
    with pytest.raises(PlcError, match="unexpected reply"):
        make_client(state, []).login("admin", "secret")


def test_methods_require_login() -> None:
    plc = make_client(fresh_state(), [])
    with pytest.raises(PlcError, match="not logged in"):
        plc.upload_program(b"zip")


def test_upload_sends_program_zip_multipart() -> None:
    state, calls = fresh_state(), []
    plc = make_client(state, calls)
    plc.login("admin", "secret")
    plc.upload_program(b"PK\x03\x04zip-bytes")
    assert calls[-1] == ("POST", "/api/upload-file")
    assert b'name="file"' in state["upload"]
    assert b'filename="program.zip"' in state["upload"]
    assert b"PK\x03\x04zip-bytes" in state["upload"]


def test_compilation_status_parses_dict_reply() -> None:
    state, calls = fresh_state(), []
    state["compile"] = ["COMPILING"]
    plc = make_client(state, calls)
    plc.login("admin", "secret")
    assert plc.compilation_status() == "COMPILING"


def test_waiter_polls_until_success() -> None:
    state = fresh_state()
    state["compile"] = ["COMPILING", "COMPILING", "SUCCESS"]
    plc = make_client(state, [])
    plc.login("admin", "secret")
    assert plc.wait_for_compilation(timeout_s=5, poll_s=0.01) == "SUCCESS"


def test_waiter_returns_failed() -> None:
    state = fresh_state()
    state["compile"] = ["FAILED"]
    plc = make_client(state, [])
    plc.login("admin", "secret")
    assert plc.wait_for_compilation(timeout_s=5, poll_s=0.01) == "FAILED"


def test_waiter_times_out() -> None:
    state = fresh_state()
    state["compile"] = ["COMPILING", "COMPILING"]
    plc = make_client(state, [])
    plc.login("admin", "secret")
    with pytest.raises(PlcError, match="timed out"):
        plc.wait_for_compilation(timeout_s=0.05, poll_s=0.01)


def test_start_stop_status_roundtrip() -> None:
    state, calls = fresh_state(), []
    plc = make_client(state, calls)
    plc.login("admin", "secret")
    assert plc.status().running is False
    plc.start()
    status = plc.status()
    assert (status.running, status.compile_state) == (True, "SUCCESS")
    plc.stop()
    assert plc.status().running is False


def test_status_parses_observed_v4_shape() -> None:
    """Bench Pi v4.3.1 answers {"status": "STATUS:RUNNING", "switchPosition": "run"}."""
    state = fresh_state()
    state["status_mode"] = "v4-shape"
    plc = make_client(state, [])
    plc.login("admin", "secret")
    status = plc.status()
    assert status.running is True
    assert status.compile_state == "NONE"  # v4 /status carries no compile info


def test_status_rejects_unexpected_reply() -> None:
    state = fresh_state()
    state["status_mode"] = "bad-shape"
    plc = make_client(state, [])
    plc.login("admin", "secret")
    with pytest.raises(PlcError, match="unexpected reply"):
        plc.status()
