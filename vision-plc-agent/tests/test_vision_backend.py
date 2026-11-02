import json

import httpx
import pytest

from agent.vision_backend import (
    DEFAULT_VISION_MODEL,
    Mask,
    MetaSamBackend,
    ScriptedVisionBackend,
    VisionError,
    VisionResult,
    parse_output_text,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)
JPEG = b"\xff\xd8\xff\xe0" + bytes(16)

BOX = "<|box;x1=10;y1=20;x2=14;y2=24;w=100;h=80|>"
BOX_C = "<|box;x1=10;y1=20;x2=14;y2=24;w=100;h=80;c=0.75|>"
MASK = "<|mask;x=0;y=0;data=5,5,!!!!!(QO(0lu8?|>"


def test_parse_single_record_half_open_bbox() -> None:
    (mask,) = parse_output_text(f"<0f>0{BOX}{MASK}", "red LED")
    assert mask == Mask(label="red LED", bbox=(10, 20, 15, 25), score=None)


def test_parse_confidence_and_multi_digit_ids() -> None:
    masks = parse_output_text(f"<0f>10{BOX_C}{MASK},2{BOX}{MASK}", "red LED")
    assert [m.score for m in masks] == [0.75, None]
    assert all(m.bbox == (10, 20, 15, 25) for m in masks)


def test_parse_accepts_any_token_order_and_frame_fields() -> None:
    (mask,) = parse_output_text(f"<0f;example=1>0{MASK}{BOX}", "arm")
    assert mask.bbox == (10, 20, 15, 25)


def test_parse_skips_malformed_but_keeps_good() -> None:
    text = "\n".join(
        [
            "<0f>",  # bare header
            "not a sam line",
            "<0f>0<|box;x1=10;y1=20;w=100;h=80|>?",  # missing fields
            "<0f>0<|box;x1=10;y1=20;x2=500;y2=24;w=100;h=80|>",  # outside w/h
            f"<0f>0{BOX}{MASK}",
        ]
    )
    assert parse_output_text(text, "red LED") == (
        Mask(label="red LED", bbox=(10, 20, 15, 25), score=None),
    )


def test_parse_ignores_bad_confidence() -> None:
    for bad in ("c=2", "c=-1", "c=abc", "c="):
        box = f"<|box;x1=10;y1=20;x2=14;y2=24;w=100;h=80;{bad}|>"
        (mask,) = parse_output_text(f"<0f>0{box}{MASK}", "x")
        assert mask.score is None


def test_parse_empty_lane_is_no_masks() -> None:
    assert parse_output_text("", "red LED") == ()
    assert parse_output_text("\n  \n", "red LED") == ()


def _responses_reply(text: str, status: str = "completed") -> dict:
    return {
        "status": status,
        "output": [
            {"id": "msg-1", "content": [{"type": "output_text", "text": text}]},
        ],
    }


def _backend(handler) -> MetaSamBackend:
    return MetaSamBackend(
        base_url="https://api.meta.ai/v1",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )


def test_segment_request_shape_and_reply() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_responses_reply(f"<0f>0{BOX_C}{MASK}"))

    result = _backend(handler).segment(JPEG, "red LED")
    assert seen["url"] == "https://api.meta.ai/v1/responses"
    assert seen["auth"] == "Bearer k"
    body = seen["body"]
    assert body["model"] == DEFAULT_VISION_MODEL
    assert body["stream"] is False
    assert body["metadata"] == {"include_confidence": "true"}
    (message,) = body["input"]
    assert message["role"] == "user"
    text_part, image_part = message["content"]
    assert text_part == {"type": "input_text", "text": "red LED"}
    assert image_part["type"] == "input_image"
    assert image_part["image_url"].startswith("data:image/jpeg;base64,")
    assert result == VisionResult(
        masks=(Mask(label="red LED", bbox=(10, 20, 15, 25), score=0.75),),
        confidence=0.75,
    )


def test_segment_png_mime_and_min_confidence() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        line = f"<0f>0{BOX_C}{MASK},1{BOX}{MASK}"
        return httpx.Response(200, json=_responses_reply(line))

    result = _backend(handler).segment(PNG, "green LED")
    image_part = seen["body"]["input"][0]["content"][1]
    assert image_part["image_url"].startswith("data:image/png;base64,")
    assert len(result.masks) == 2
    assert result.confidence == 0.75  # min of the reported scores


def test_segment_no_masks_is_zero_confidence() -> None:
    backend = _backend(lambda request: httpx.Response(200, json=_responses_reply("")))
    assert backend.segment(JPEG, "red LED") == VisionResult(masks=(), confidence=0.0)


def test_segment_rejects_bad_frames_and_prompts() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_responses_reply(""))

    backend = _backend(handler)
    with pytest.raises(VisionError, match="neither JPEG nor PNG"):
        backend.segment(b"nope", "red LED")
    with pytest.raises(VisionError, match="empty prompt"):
        backend.segment(JPEG, "  ")
    assert calls == []


def test_segment_errors_become_vision_error() -> None:
    def status(text: str, code: int):
        return _backend(lambda request: httpx.Response(code, text=text))

    with pytest.raises(VisionError, match="HTTP 500"):
        status("boom", 500).segment(JPEG, "red LED")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(VisionError, match="responses failed"):
        _backend(fail).segment(JPEG, "red LED")

    bad_json = _backend(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(VisionError, match="invalid JSON"):
        bad_json.segment(JPEG, "red LED")

    failed = _backend(
        lambda request: httpx.Response(200, json=_responses_reply("", status="failed"))
    )
    with pytest.raises(VisionError, match="not completed"):
        failed.segment(JPEG, "red LED")


def test_from_env_and_empty_key(monkeypatch) -> None:
    monkeypatch.setenv("META_API_BASE_URL", "https://x/v1")
    monkeypatch.setenv("META_API_KEY", "k")
    monkeypatch.delenv("VISION_MODEL", raising=False)
    backend = MetaSamBackend.from_env()
    assert backend._url == "https://x/v1/responses"
    assert backend._model == "sam-3.1"
    monkeypatch.setenv("VISION_MODEL", "sam-9")
    assert MetaSamBackend.from_env()._model == "sam-9"
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(VisionError, match="META_API_KEY"):
        MetaSamBackend.from_env()
    with pytest.raises(VisionError, match="empty api_key"):
        MetaSamBackend(base_url="https://x/v1", api_key="")


def test_scripted_backend_order_and_exhaustion() -> None:
    result = VisionResult(masks=(), confidence=0.0)
    scripted = ScriptedVisionBackend([result])
    assert scripted.segment(b"f", "red LED") is result
    assert scripted.prompts == ["red LED"]
    with pytest.raises(VisionError, match="exhausted"):
        scripted.segment(b"f", "green LED")
