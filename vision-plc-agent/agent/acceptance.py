"""Muse Spark acceptance-test drafting: plain-English task -> YAML tests.

Loop step 0, before planning. A task can arrive as a *brief* (task_id,
description, signals, no ``tests``). Muse Spark drafts the ``tests:`` list;
each reply is checked against the spec schema, the I/O map and the harness
timing rules, and problems go back to the model verbatim (at most
:data:`MAX_DRAFT_ROUNDS` attempts). A human then approves the tests, editing
them if needed, before any code is generated: the tests are the contract the
generated ST is judged against.

Approval gates: :func:`prompt_for_tests_approval` (CLI) and
:func:`wait_for_tests_approval` (dashboard: ``pending_tests.json`` ->
``tests_approved.json`` or ``tests_rejected.json``).
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent.codegen import extract_fenced, load_prompt, render_io_table
from agent.dotenv import load_dotenv
from agent.io_map import IoMap, load_io_map
from agent.llm_backend import LlmBackend, LlmReply, MetaLlmBackend, ScriptedLlmBackend
from agent.spec import (
    AcceptanceTest,
    TaskBrief,
    TaskSpec,
    check_against_io_map,
    check_brief_against_io_map,
    load_task_spec,
)

EXAMPLE_SPEC = Path(__file__).resolve().parent.parent / "specs" / "start_lamp_arm.yaml"
DRAFT_EFFORT = "high"  # first draft gets full reasoning, like codegen
REPAIR_EFFORT = "low"
MAX_DRAFT_ROUNDS = 3
SETTLE_MS = 300  # minimum gap between the last stimulus step and any deadline
PRESS_MS = 200  # press -> release, as in the checked-in specs
PENDING_TESTS_FILE = "pending_tests.json"
TESTS_APPROVED_FILE = "tests_approved.json"
TESTS_REJECTED_FILE = "tests_rejected.json"
APPROVAL_POLL_S = 1.0


@dataclass(frozen=True)
class TestsCheck:
    """Validator verdict for one tests YAML text.

    ``errors`` make the YAML unusable (parse, schema, I/O map); ``warnings``
    break harness timing rules: the test would run but likely misjudge the
    PLC. The drafter repairs both; approval only requires no errors.
    """

    spec: TaskSpec | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def problems(self) -> tuple[str, ...]:
        return self.errors + self.warnings


@dataclass(frozen=True)
class AcceptanceDraft:
    brief: TaskBrief
    tests_yaml: str
    check: TestsCheck
    attempts: int
    input_tokens: int
    output_tokens: int

    @property
    def passed(self) -> bool:
        return self.check.spec is not None and not self.check.problems

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.brief.task_id,
            "passed": self.passed,
            "attempts": self.attempts,
            "errors": list(self.check.errors),
            "warnings": list(self.check.warnings),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tests_yaml": self.tests_yaml,
        }


# Approval gate: returns the approved tests YAML (maybe human-edited) or None.
TestsApproveFn = Callable[[AcceptanceDraft], str | None]
SleepFn = Callable[[float], None]


def _flow(data: object) -> str:
    return yaml.safe_dump(data, default_flow_style=True, sort_keys=False, width=10_000).strip()


def render_tests_yaml(tests: list[AcceptanceTest]) -> str:
    """Compact YAML list in the style of ``specs/*.yaml`` (one step per line)."""
    lines: list[str] = []
    for test in tests:
        lines.append("- " + yaml.safe_dump({"name": test.name}, sort_keys=False).strip())
        if test.stimulus:
            lines.append("  stimulus:")
            lines += ["    - " + _flow(s.model_dump(exclude_none=True)) for s in test.stimulus]
        else:
            lines.append("  stimulus: []")
        lines.append("  expect:")
        lines += ["    - " + _flow(e.model_dump(exclude_none=True)) for e in test.expect]
    return "\n".join(lines) + "\n"


def pressable_inputs(brief: TaskBrief, io_map: IoMap) -> list[str]:
    """Spec inputs the stimulus service can drive."""
    driven = {pin.drives for pin in io_map.stimulus.values()}
    return [n for n in brief.signals if n in driven and io_map.signals[n].direction == "input"]


def extract_tests_yaml(text: str) -> str:
    """The `````yaml `` (or ``yml``) block, else the raw reply."""
    for info in ("yaml", "yml"):
        blocks = extract_fenced(text, info)
        if blocks:
            return blocks[0]
    return text.strip() + "\n"


def _format_validation(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg")))
    return out


def check_tests(brief: TaskBrief, tests_yaml: str, io_map: IoMap) -> TestsCheck:
    """Parse ``tests_yaml`` (a list, or a mapping with ``tests:``) for ``brief``."""
    try:
        data = yaml.safe_load(tests_yaml)
    except yaml.YAMLError as exc:
        return TestsCheck(None, (f"YAML does not parse: {exc}",), ())
    if isinstance(data, dict) and "tests" in data:
        data = data["tests"]
    if not isinstance(data, list) or not data:
        return TestsCheck(None, ("want a non-empty YAML list of tests",), ())
    try:
        spec = TaskSpec.model_validate({**brief.model_dump(), "tests": data})
    except ValidationError as exc:
        return TestsCheck(None, tuple("tests." + m for m in _format_validation(exc)), ())
    try:
        check_against_io_map(spec, io_map)
    except ValueError as exc:
        detail = str(exc).split(": ", 1)[-1]
        return TestsCheck(None, tuple(detail.split("; ")), ())
    return TestsCheck(spec, (), tuple(timing_warnings(spec, io_map)))


def timing_warnings(spec: TaskSpec, io_map: IoMap) -> list[str]:
    """Harness rules a schema-valid test can still break (see the prompt)."""
    warnings: list[str] = []
    driven = {pin.drives for pin in io_map.stimulus.values()}
    seen_names: set[str] = set()
    for test in spec.tests:
        where = f"test {test.name!r}"
        if test.name in seen_names:
            warnings.append(f"{where}: duplicate test name")
        seen_names.add(test.name)
        held: dict[str, int] = {}
        for step in sorted(test.stimulus, key=lambda s: s.at_ms):
            if step.press is not None:
                if step.press not in driven:
                    warnings.append(f"{where}: the harness cannot press {step.press!r}")
                if step.press in held:
                    warnings.append(f"{where}: {step.press} pressed twice without a release")
                held[step.press] = step.at_ms
            else:
                assert step.release is not None
                if held.pop(step.release, None) is None:
                    warnings.append(
                        f"{where}: release of {step.release} at {step.at_ms} ms without a press"
                    )
        for name in held:
            warnings.append(f"{where}: {name} is pressed but never released")
        last_step = max((s.at_ms for s in test.stimulus), default=0)
        previous = -1
        for exp in test.expect:
            deadline = exp.within_ms if exp.within_ms is not None else exp.at_ms
            assert deadline is not None
            signal = io_map.signals[exp.signal]
            label = f"{where}: {exp.signal}={exp.value!r} @ {deadline} ms"
            if test.stimulus and deadline < last_step + SETTLE_MS:
                warnings.append(
                    f"{label}: deadline must be >= {last_step + SETTLE_MS} ms (last stimulus "
                    f"step at {last_step} ms + {SETTLE_MS} ms); expectations are only checked "
                    "after the whole stimulus has played"
                )
            if deadline < previous:
                warnings.append(f"{label}: deadline earlier than the previous expectation's")
            previous = max(previous, deadline)
            if signal.direction != "output":
                warnings.append(f"{label}: expectations must be on outputs")
            if signal.type == "INT":
                if exp.tolerance is None:
                    warnings.append(f"{label}: INT expectation needs a tolerance")
                if signal.range is not None and not (
                    signal.range[0] <= float(exp.value) <= signal.range[1]
                ):
                    warnings.append(f"{label}: value outside range {list(signal.range)}")
    return warnings


def _example() -> tuple[str, str]:
    spec = load_task_spec(EXAMPLE_SPEC)
    return " ".join(spec.description.split()), render_tests_yaml(spec.tests)


def render_draft_prompt(brief: TaskBrief, io_map: IoMap) -> str:
    example_description, example_tests = _example()
    pressable = pressable_inputs(brief, io_map)
    return load_prompt("acceptance_v1.txt").substitute(
        task_id=brief.task_id,
        description=" ".join(brief.description.split()),
        io_table=render_io_table(brief, io_map),
        safe_state=", ".join(
            f"{n}={io_map.safe_state[n]}" for n in brief.signals if n in io_map.safe_state
        ),
        pressable=", ".join(pressable) or "(none: tests can only check outputs at rest)",
        example_description=example_description,
        example_tests=example_tests,
    )


def render_repair_prompt(
    brief: TaskBrief, io_map: IoMap, tests_yaml: str, problems: tuple[str, ...], *, round: int
) -> str:
    return load_prompt("acceptance_repair_v1.txt").substitute(
        round=round,
        max_rounds=MAX_DRAFT_ROUNDS,
        task_id=brief.task_id,
        description=" ".join(brief.description.split()),
        io_table=render_io_table(brief, io_map),
        pressable=", ".join(pressable_inputs(brief, io_map)) or "(none)",
        tests_yaml=tests_yaml if tests_yaml.endswith("\n") else tests_yaml + "\n",
        problems="\n".join(f"- {p}" for p in problems),
    )


def _record(workdir: Path | None, attempt: int, prompt: str, reply: LlmReply) -> None:
    """Prompts + replies per attempt (AGENTS.md: runs keep model I/O). Best effort."""
    if workdir is None:
        return
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / f"prompt{attempt}.txt").write_text(prompt, encoding="utf-8")
        (workdir / f"reply{attempt}.txt").write_text(reply.text, encoding="utf-8")
    except OSError:
        pass


def draft_acceptance_tests(
    brief: TaskBrief,
    io_map: IoMap,
    llm: LlmBackend,
    *,
    workdir: str | Path | None = None,
    max_rounds: int = MAX_DRAFT_ROUNDS,
) -> AcceptanceDraft:
    """Draft, validate, repair until clean or rounds run out.

    A draft that still has problems is returned anyway: the human sees the
    problems next to the YAML and can fix it before approving.
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
    check_brief_against_io_map(brief, io_map)
    root = Path(workdir) if workdir is not None else None
    prompt = render_draft_prompt(brief, io_map)
    reply = llm.generate(prompt, effort=DRAFT_EFFORT)
    _record(root, 1, prompt, reply)
    replies = [reply]
    tests_yaml = extract_tests_yaml(reply.text)
    check = check_tests(brief, tests_yaml, io_map)
    attempt = 1
    while check.problems and attempt < max_rounds:
        attempt += 1
        prompt = render_repair_prompt(brief, io_map, tests_yaml, check.problems, round=attempt)
        reply = llm.generate(prompt, effort=REPAIR_EFFORT)
        _record(root, attempt, prompt, reply)
        replies.append(reply)
        tests_yaml = extract_tests_yaml(reply.text)
        check = check_tests(brief, tests_yaml, io_map)
    draft = AcceptanceDraft(
        brief=brief,
        tests_yaml=tests_yaml,
        check=check,
        attempts=attempt,
        input_tokens=sum(r.input_tokens for r in replies),
        output_tokens=sum(r.output_tokens for r in replies),
    )
    if root is not None:
        try:
            (root / "tests.yaml").write_text(tests_yaml, encoding="utf-8")
            (root / "report.json").write_text(
                json.dumps(draft.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
    return draft


def fake_tests_yaml(brief: TaskBrief, io_map: IoMap) -> str:
    """Deterministic draft for ``--fake``: exercises plumbing, not quality.

    Press the first pressable input and expect every output away from its
    safe value; if ``stop_btn`` is in the task, also check stop -> safe.
    """
    outputs = [n for n in brief.signals if io_map.signals[n].direction == "output"]
    if not outputs:
        raise ValueError("task has no outputs to test")
    inputs = pressable_inputs(brief, io_map)
    go = next((n for n in inputs if n != "stop_btn"), inputs[0] if inputs else None)

    def active(name: str) -> object:
        signal = io_map.signals[name]
        if signal.type == "BOOL":
            return {"signal": name, "value": True, "within_ms": 800}
        top = signal.range[1] if signal.range else 90
        return {"signal": name, "value": top // 2, "tolerance": 10, "within_ms": 1500}

    def safe(name: str, at: int) -> object:
        value = io_map.safe_state.get(name, False)
        out: dict[str, object] = {"signal": name, "value": value, "within_ms": at}
        if io_map.signals[name].type == "INT":
            out["tolerance"] = 10
        return out

    press = [{"at_ms": 0, "press": go}, {"at_ms": PRESS_MS, "release": go}] if go else []
    tests: list[dict[str, object]] = [
        {
            "name": "fake_press_drives_outputs",
            "stimulus": press,
            "expect": [active(n) for n in outputs],
        }
    ]
    if "stop_btn" in inputs and go and go != "stop_btn":
        stop = [
            {"at_ms": 1000, "press": "stop_btn"},
            {"at_ms": 1000 + PRESS_MS, "release": "stop_btn"},
        ]
        tests.append(
            {
                "name": "fake_stop_is_safe",
                "stimulus": press + stop,
                "expect": [safe(n, 2000) for n in outputs],
            }
        )
    parsed = [AcceptanceTest.model_validate(t) for t in tests]
    return (
        "# ASSUMPTION: fake mode, drafted without a model (plumbing only).\n"
        + render_tests_yaml(parsed)
    )


def fake_llm_for_brief(brief: TaskBrief, io_map: IoMap) -> ScriptedLlmBackend:
    return ScriptedLlmBackend([f"```yaml\n{fake_tests_yaml(brief, io_map)}```\n"])


def prompt_for_tests_approval(draft: AcceptanceDraft) -> str | None:
    """CLI gate: show the drafted tests, require an explicit 'approve'."""
    print("=== acceptance tests for approval ===")
    print(draft.tests_yaml.rstrip())
    for problem in draft.check.problems:
        print(f"! {problem}")
    if draft.check.spec is None:
        print("tests do not validate; fix the spec file by hand.")
        return None
    try:
        answer = input("Type 'approve' to accept these tests: ").strip().lower()
    except EOFError:
        return None
    return draft.tests_yaml if answer == "approve" else None


def write_pending_tests(run_dir: Path, draft: AcceptanceDraft) -> Path:
    """Publish the draft for the dashboard's tests review panel."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    pending = {
        "run_id": run_dir.name,
        "task_id": draft.brief.task_id,
        "at": datetime.now(UTC).isoformat(),
        "brief": draft.brief.model_dump(),
        "tests_yaml": draft.tests_yaml,
        "errors": list(draft.check.errors),
        "warnings": list(draft.check.warnings),
        "attempts": draft.attempts,
    }
    path = run_dir / PENDING_TESTS_FILE
    path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    return path


def wait_for_tests_approval(
    run_dir: Path,
    timeout_s: float,
    *,
    poll_s: float = APPROVAL_POLL_S,
    sleep_fn: SleepFn = time.sleep,
) -> TestsApproveFn:
    """Gate that waits for the dashboard's Approve tests / Reject buttons.

    The dashboard validates edits before writing ``tests_approved.json``
    (``{"tests_yaml": ..., "by": ..., "at": ...}``); a timeout or a
    ``tests_rejected.json`` means not approved.
    """

    def _wait(draft: AcceptanceDraft) -> str | None:
        write_pending_tests(run_dir, draft)
        approved = Path(run_dir) / TESTS_APPROVED_FILE
        rejected = Path(run_dir) / TESTS_REJECTED_FILE
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if approved.is_file():
                try:
                    record = json.loads(approved.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    record = None
                text = record.get("tests_yaml") if isinstance(record, dict) else None
                return text if isinstance(text, str) else None
            if rejected.is_file() or time.monotonic() >= deadline:
                return None
            sleep_fn(min(poll_s, max(0.0, deadline - time.monotonic())))

    return _wait


def auto_approve_tests(draft: AcceptanceDraft) -> str | None:
    """Fake/explicit-flag gate: accept a draft that validates."""
    return draft.tests_yaml if draft.check.spec is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft acceptance tests for a task brief.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--description", required=True, help="the task in plain English")
    parser.add_argument("--signals", required=True, help="comma-separated io_map signal names")
    parser.add_argument("--out", required=True, help="spec YAML to write, e.g. specs/x.yaml")
    parser.add_argument("--fake", action="store_true", help="no API: deterministic draft")
    parser.add_argument("--io-map", default=None)
    parser.add_argument("--max-rounds", type=int, default=MAX_DRAFT_ROUNDS)
    args = parser.parse_args(argv)
    brief = TaskBrief(
        task_id=args.task_id,
        description=args.description,
        signals=[s.strip() for s in args.signals.split(",") if s.strip()],
    )
    io_map = load_io_map(args.io_map)
    llm: LlmBackend
    if args.fake:
        llm = fake_llm_for_brief(brief, io_map)
    else:
        load_dotenv()
        llm = MetaLlmBackend.from_env()
    out = Path(args.out)
    try:
        draft = draft_acceptance_tests(brief, io_map, llm, max_rounds=args.max_rounds)
    finally:
        if isinstance(llm, MetaLlmBackend):
            llm.close()
    for problem in draft.check.problems:
        print(f"! {problem}")
    print(f"tokens {draft.input_tokens} in / {draft.output_tokens} out")
    approved = prompt_for_tests_approval(draft)
    if approved is None:
        print("not approved; nothing written")
        return 1
    spec = check_tests(brief, approved, io_map).spec
    assert spec is not None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(spec.model_dump(exclude_none=True), sort_keys=False), "utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
