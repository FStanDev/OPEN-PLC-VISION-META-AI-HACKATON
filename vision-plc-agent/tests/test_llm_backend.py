"""MetaLlmBackend (MockTransport, no network) + ScriptedLlmBackend."""

import json

import httpx
import pytest

from agent.llm_backend import (
    DEFAULT_MODEL,
    LlmError,
    LlmReply,
    MetaLlmBackend,
    ScriptedLlmBackend,
)


def _ok_response(text: str = "hello", in_tok: int = 5, out_tok: int = 7) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok},
        },
    )


def test_meta_posts_openai_compatible_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content.decode())
        return _ok_response()

    llm = MetaLlmBackend(
        base_url="https://api.example.co/v1/",
        api_key="sekret",
        model="muse-spark-1.3",
        transport=httpx.MockTransport(handler),
    )
    reply = llm.generate("write ST", effort="high")
    assert reply == LlmReply(text="hello", input_tokens=5, output_tokens=7)
    assert seen["url"] == "https://api.example.co/v1/chat/completions"
    assert seen["auth"] == "Bearer sekret"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "muse-spark-1.3"
    assert body["messages"] == [{"role": "user", "content": "write ST"}]
    assert body["reasoning_effort"] == "high"


def test_meta_default_effort_is_low() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return _ok_response()

    llm = MetaLlmBackend(base_url="https://x", api_key="k", transport=httpx.MockTransport(handler))
    llm.generate("hi")
    body = seen["body"]
    assert isinstance(body, dict) and body["reasoning_effort"] == "low"


def test_meta_invalid_effort_rejected_without_http_call() -> None:
    calls: list[httpx.Request] = []
    llm = MetaLlmBackend(
        base_url="https://x",
        api_key="k",
        transport=httpx.MockTransport(lambda r: (calls.append(r), _ok_response())[1]),
    )
    with pytest.raises(ValueError, match="effort"):
        llm.generate("hi", effort="medium")
    assert calls == []


def test_meta_http_error_maps_to_llm_error() -> None:
    llm = MetaLlmBackend(
        base_url="https://x",
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")),
    )
    with pytest.raises(LlmError, match="HTTP 401"):
        llm.generate("hi")


def test_meta_bad_payloads_raise() -> None:
    for payload in (
        {},
        {"choices": []},
        {"choices": [{"message": {"content": "  "}}]},
    ):
        llm = MetaLlmBackend(
            base_url="https://x",
            api_key="k",
            transport=httpx.MockTransport(lambda r, p=payload: httpx.Response(200, json=p)),
        )
        with pytest.raises(LlmError, match="reply|completion"):
            llm.generate("hi")


def test_meta_network_error_maps_to_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    llm = MetaLlmBackend(base_url="https://x", api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError, match="no route"):
        llm.generate("hi")


def test_meta_empty_key_rejected() -> None:
    with pytest.raises(LlmError, match="api_key"):
        MetaLlmBackend(base_url="https://x", api_key="")


def test_from_env_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_BASE_URL", "https://m")
    monkeypatch.setenv("META_API_KEY", "k")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm = MetaLlmBackend.from_env()
    assert llm._model == DEFAULT_MODEL == "muse-spark-1.3"
    assert llm._url == "https://m/chat/completions"


def test_from_env_missing_vars_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_API_BASE_URL", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(LlmError, match="META_API_BASE_URL, META_API_KEY"):
        MetaLlmBackend.from_env()


def test_scripted_replays_in_order_and_records_calls() -> None:
    llm = ScriptedLlmBackend(["first", LlmReply("second", 1, 2)])
    assert llm.generate("p1", effort="high").text == "first"
    assert llm.generate("p2") == LlmReply("second", 1, 2)
    assert llm.calls == [("high", "p1"), ("low", "p2")]
    with pytest.raises(LlmError, match="exhausted"):
        llm.generate("p3")


def test_scripted_counts_input_words_for_plain_strings() -> None:
    llm = ScriptedLlmBackend(["x"])
    reply = llm.generate("one two three")
    assert (reply.input_tokens, reply.output_tokens) == (3, 0)
