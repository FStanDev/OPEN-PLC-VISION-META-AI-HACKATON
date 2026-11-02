"""Acceptance-test drafting (loop step 0): validator, repair loop, gates. Fakes only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.acceptance import (
    PENDING_TESTS_FILE,
    TESTS_APPROVED_FILE,
    TESTS_REJECTED_FILE,
    check_tests,
    draft_acceptance_tests,
    extract_tests_yaml,
    fake_tests_yaml,
    render_draft_prompt,
    wait_for_tests_approval,
)
from agent.io_map import load_io_map
from agent.llm_backend import LlmReply, ScriptedLlmBackend
from agent.spec import TaskBrief, TaskSpec, load_task_file

IO_MAP = load_io_map()
BRIEF = TaskBrief(
    task_id="green_red_on",
    description="When start is pressed, latch the green lamp on. "
    "Stop turns everything off leaving red lamp on for 3 seconds.",
    signals=["start_btn", "stop_btn", "lamp_red", "lamp_green"],
)
GOOD_TESTS = """\
# ASSUMPTION: the red lamp starts when stop is released.
- name: start_latches_green
  stimulus: [{at_ms: 0, press: start_btn}, {at_ms: 200, release: start_btn}]
  expect:
    - {signal: lamp_green, value: true, within_ms: 500}
- name: stop_shows_red_then_safe
  stimulus:
    - {at_ms: 0, press: start_btn}
    - {at_ms: 200, release: start_btn}
    - {at_ms: 1000, press: stop_btn}
    - {at_ms: 1200, release: stop_btn}
  expect:
    - {signal: lamp_green, value: false, within_ms: 1500}
    - {signal: lamp_red, value: true, within_ms: 1700}
    - {signal: lamp_red, value: false, at_ms: 5000}
"""
EARLY_DEADLINE = """\
- name: stop_too_early
  stimulus:
    - {at_ms: 0, press: start_btn}
    - {at_ms: 200, release: start_btn}
    - {at_ms: 1000, press: stop_btn}
    - {at_ms: 1200, release: stop_btn}
  expect:
    - {signal: lamp_green, value: true, within_ms: 500}
"""


def test_good_tests_validate_clean() -> None:
    check = check_tests(BRIEF, GOOD_TESTS, IO_MAP)
    assert check.spec is not None and check.problems == ()
    assert [t.name for t in check.spec.tests] == ["start_latches_green", "stop_shows_red_then_safe"]


def test_mapping_with_tests_key_is_accepted() -> None:
    wrapped = "tests:\n" + "".join("  " + line + "\n" for line in GOOD_TESTS.splitlines())
    assert check_tests(BRIEF, wrapped, IO_MAP).spec is not None


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- name: [unclosed", "does not parse"),
        ("{}", "non-empty YAML list"),
        ("- {name: t, expect: [{signal: lamp_red, value: true}]}", "within_ms"),
        ("- {name: t, expect: [{signal: lamp_yellow, value: true, within_ms: 500}]}", "undeclared"),
        ("- {name: t, expect: [{signal: lamp_red, value: 3, within_ms: 500}]}", "does not fit"),
    ],
)
def test_hard_errors_block_the_spec(text: str, needle: str) -> None:
    check = check_tests(BRIEF, text, IO_MAP)
    assert check.spec is None
    assert any(needle in e for e in check.errors), check.errors


def test_timing_warnings() -> None:
    brief = TaskBrief(
        task_id="t", description="d", signals=["start_btn", "stop_btn", "lamp_red", "arm_angle"]
    )
    text = """\
- name: dup
  stimulus: [{at_ms: 0, press: start_btn}]
  expect:
    - {signal: lamp_red, value: true, within_ms: 900}
    - {signal: lamp_red, value: false, within_ms: 600}
    - {signal: arm_angle, value: 90, within_ms: 1500}
    - {signal: arm_angle, value: 400, tolerance: 10, within_ms: 1600}
    - {signal: start_btn, value: true, within_ms: 1700}
- name: dup
  stimulus: [{at_ms: 100, release: stop_btn}]
  expect: [{signal: lamp_red, value: false, within_ms: 900}]
"""
    check = check_tests(brief, text, IO_MAP)
    assert check.spec is not None  # warnings never block
    joined = "\n".join(check.warnings)
    for needle in (
        "never released",
        "earlier than the previous",
        "needs a tolerance",
        "outside range",
        "must be on outputs",
        "duplicate test name",
        "without a press",
    ):
        assert needle in joined, needle
    early = check_tests(BRIEF, EARLY_DEADLINE, IO_MAP)
    assert any(">= 1500 ms" in w for w in early.warnings), early.warnings


def test_extract_tests_yaml_prefers_fence() -> None:
    assert extract_tests_yaml("intro\n```yaml\n- a\n```\nbye") == "- a\n"
    assert extract_tests_yaml("```yml\n- b\n```") == "- b\n"
    assert extract_tests_yaml("- raw\n") == "- raw\n"


def test_prompt_is_fully_substituted() -> None:
    prompt = render_draft_prompt(BRIEF, IO_MAP)
    assert "$" not in prompt
    assert "leaving red lamp on for 3 seconds" in prompt
    assert "INPUTS THE HARNESS CAN PRESS: start_btn, stop_btn" in prompt
    assert "lamp_red=False, lamp_green=False" in prompt  # outputs only
    assert "start_lamp_arm" not in prompt and "move the arm to 90" in prompt  # few-shot


def test_draft_repairs_warnings_then_records(tmp_path: Path) -> None:
    llm = ScriptedLlmBackend(
        [
            LlmReply(f"```yaml\n{EARLY_DEADLINE}```", input_tokens=100, output_tokens=40),
            LlmReply(f"```yaml\n{GOOD_TESTS}```", input_tokens=60, output_tokens=50),
        ]
    )
    draft = draft_acceptance_tests(BRIEF, IO_MAP, llm, workdir=tmp_path)
    assert draft.passed and draft.attempts == 2
    assert (draft.input_tokens, draft.output_tokens) == (160, 90)
    assert [effort for effort, _ in llm.calls] == ["high", "low"]
    repair_prompt = llm.calls[1][1]
    assert ">= 1500 ms" in repair_prompt and "stop_too_early" in repair_prompt
    assert "$" not in repair_prompt
    assert (tmp_path / "prompt1.txt").is_file() and (tmp_path / "reply2.txt").is_file()
    assert (tmp_path / "tests.yaml").read_text(encoding="utf-8") == GOOD_TESTS
    assert json.loads((tmp_path / "report.json").read_text())["passed"] is True


def test_draft_gives_up_after_max_rounds_and_keeps_problems() -> None:
    llm = ScriptedLlmBackend(["no yaml here"] * 3)
    draft = draft_acceptance_tests(BRIEF, IO_MAP, llm, max_rounds=3)
    assert not draft.passed and draft.attempts == 3 and len(llm.calls) == 3
    assert draft.check.spec is None and draft.check.errors


def test_draft_rejects_unknown_signal() -> None:
    bad = TaskBrief(task_id="t", description="d", signals=["lamp_blue"])
    with pytest.raises(ValueError, match="unknown signal"):
        draft_acceptance_tests(bad, IO_MAP, ScriptedLlmBackend([]))


@pytest.mark.parametrize(
    "signals",
    [
        ["start_btn", "stop_btn", "lamp_red", "lamp_green"],
        ["start_btn", "stop_btn", "lamp_red", "arm_angle"],
        ["stop_btn", "lamp_yellow"],
        ["lamp_red"],
    ],
)
def test_fake_draft_is_clean(signals: list[str]) -> None:
    brief = TaskBrief(task_id="t", description="d", signals=signals)
    check = check_tests(brief, fake_tests_yaml(brief, IO_MAP), IO_MAP)
    assert check.spec is not None and check.problems == (), check.problems


def _draft(tmp_path: Path):
    return draft_acceptance_tests(
        BRIEF, IO_MAP, ScriptedLlmBackend([f"```yaml\n{GOOD_TESTS}```"]), workdir=tmp_path / "a"
    )


def test_wait_for_tests_approval_returns_edited_yaml(tmp_path: Path) -> None:
    draft = _draft(tmp_path)

    def approve_on_first_sleep(_: float) -> None:
        pending = json.loads((tmp_path / PENDING_TESTS_FILE).read_text(encoding="utf-8"))
        assert pending["tests_yaml"] == GOOD_TESTS and pending["brief"]["task_id"] == "green_red_on"
        (tmp_path / TESTS_APPROVED_FILE).write_text(json.dumps({"tests_yaml": "- edited\n"}))

    gate = wait_for_tests_approval(tmp_path, 5, sleep_fn=approve_on_first_sleep)
    assert gate(draft) == "- edited\n"


def test_wait_for_tests_approval_reject_and_timeout(tmp_path: Path) -> None:
    draft = _draft(tmp_path)
    (tmp_path / TESTS_REJECTED_FILE).write_text("{}")
    assert wait_for_tests_approval(tmp_path, 5, sleep_fn=lambda _: None)(draft) is None
    (tmp_path / TESTS_REJECTED_FILE).unlink()
    assert wait_for_tests_approval(tmp_path, 0, sleep_fn=lambda _: None)(draft) is None


def test_load_task_file_brief_or_spec(tmp_path: Path) -> None:
    brief_path = tmp_path / "b.yaml"
    brief_path.write_text("task_id: b\ndescription: d\nsignals: [lamp_red]\ntests: []\n")
    assert type(load_task_file(brief_path)) is TaskBrief
    spec = load_task_file(Path(__file__).resolve().parent.parent / "specs" / "start_lamp_arm.yaml")
    assert isinstance(spec, TaskSpec)
