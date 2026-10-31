import pytest

from agent.frames import FakeFrameSource
from agent.llm_backend import FakeLlmBackend, LlmReply
from agent.plc_client import FakePlcClient
from agent.stimulus import FakeStimulus, StimulusEvent
from agent.tag_reader import FakeTagReader
from agent.vision_backend import FakeVisionBackend


def test_fake_llm_returns_reply_with_token_counts() -> None:
    reply = FakeLlmBackend().generate("write ST", effort="low")
    assert isinstance(reply, LlmReply)
    assert reply.text
    assert reply.input_tokens == 2


def test_fake_vision_returns_full_confidence_mask() -> None:
    result = FakeVisionBackend().segment(b"frame", "red LED")
    assert result.confidence == 1.0
    assert [m.label for m in result.masks] == ["red LED"]


def test_fake_plc_client_deploy_cycle() -> None:
    plc = FakePlcClient()
    assert plc.login("u", "p") == "fake-token"
    assert plc.status().running is False
    plc.upload_program(b"zip")
    assert plc.compilation_status() == "SUCCESS"
    assert plc.uploads == [b"zip"]
    plc.start()
    assert plc.status().running is True
    plc.stop()
    assert plc.status().running is False


def test_fake_tag_reader_read_write() -> None:
    tags = FakeTagReader({"lamp_red": True})
    assert tags.read("lamp_red") is True
    tags.write("arm_angle", 90)
    assert tags.read("arm_angle") == 90
    with pytest.raises(KeyError):
        tags.read("nope")


def test_fake_stimulus_records_timeline() -> None:
    stim = FakeStimulus()
    stim.press("start_btn")
    stim.release("start_btn")
    assert stim.events == [
        StimulusEvent("press", "start_btn"),
        StimulusEvent("release", "start_btn"),
    ]


def test_fake_frame_source_returns_bytes() -> None:
    frame = FakeFrameSource().grab()
    assert isinstance(frame, bytes) and frame.startswith(b"\xff\xd8")
