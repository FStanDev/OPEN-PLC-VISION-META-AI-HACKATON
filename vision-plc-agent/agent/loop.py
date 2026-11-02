"""Closed loop: codegen → approve → deploy → physical tests → diagnose → repair.

SPEC loop steps 1-10 (see docs/SPEC.md). Milestone 6 wires the milestones
together: :mod:`agent.codegen` plans and generates, a human approves, we
deploy via :mod:`agent.packager` + :mod:`agent.plc_client`, run the spec's
acceptance tests with :mod:`agent.stimulus` + :mod:`agent.tag_reader` +
:mod:`agent.frames`, measure with :mod:`agent.vision_check`,
diagnose each expectation with :mod:`agent.diagnose`, and repair logic bugs
(max :data:`MAX_LOGIC_FIXES`) or hand back with ``report.json``.

Every run writes ``runs/<timestamp>-<task_id>/``: spec, diagram, ST,
codegen report, calibration, frames, ``report.json`` (with token usage)
and a short ``summary.txt``. The whole loop runs end to end with fakes
only (``--fake``): no Pi, no API, no credits.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent.codegen import (
    MAX_REPAIR_ROUNDS,
    ProgramFiles,
    generate_with_repairs,
    offline_check,
    repair_program,
)
from agent.diagnose import (
    DEFAULT_MIN_VISION_CONFIDENCE,
    Diagnosis,
    Verdict,
    diagnose,
)
from agent.dotenv import load_dotenv
from agent.frames import FrameSource, HttpFrameSource
from agent.io_map import IoMap, load_io_map
from agent.llm_backend import LlmBackend, MetaLlmBackend, ScriptedLlmBackend
from agent.measure import Calibration
from agent.packager import PackagerError
from agent.plc_client import FakePlcClient, PlcClient, PlcError, RestPlcClient
from agent.spec import Expectation, TaskSpec, check_against_io_map, load_task_spec
from agent.stimulus import FakeStimulus, HttpStimulus, Stimulus
from agent.strucpp import StrucppNotFoundError
from agent.tag_reader import FakeTagReader, ModbusTagReader, TagReader, TagValue
from agent.tag_writer import FakeTagWriter, ModbusTagWriter, TagWriter
from agent.vision_backend import MetaSamBackend, VisionBackend
from agent.vision_check import ANGLE_TOLERANCE_DEFAULT, check_signal

MAX_LOGIC_FIXES = 3
POLL_INTERVAL_MS = 50
FAKE_PROGRAM_ZIP = b"fake-program-zip: no packager in --fake mode"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"

SleepFn = Callable[[float], None]
NowMsFn = Callable[[], int]
ApproveFn = Callable[[ProgramFiles, str], bool]
DeployFn = Callable[[ProgramFiles, Path], bytes]
VisionCheckFn = Callable[["VisionCheckRequest"], "VisionOutcome"]
PrepareTestFn = Callable[[int, object], None]


@dataclass(frozen=True)
class VisionCheckRequest:
    """One camera check: what the PLC reported vs what the spec wants."""

    signal: str
    expected: bool | int | float
    tolerance: float | None
    tag_value: TagValue


@dataclass(frozen=True)
class VisionOutcome:
    """Measured camera state plus the tags-vs-camera comparison."""

    entry: dict[str, object]
    tags_match_camera: bool
    confidence: float
    frames: tuple[bytes, ...]


@dataclass(frozen=True)
class ExpectationResult:
    test: str
    signal: str
    expected: bool | int | float
    tag_value: TagValue | None
    tag_match: bool
    vision_entry: dict[str, object]
    tags_match_camera: bool
    vision_confidence: float
    diagnosis: Diagnosis

    def to_dict(self) -> dict[str, object]:
        return {
            "test": self.test,
            "signal": self.signal,
            "expected": self.expected,
            "tag_value": self.tag_value,
            "tag_match": self.tag_match,
            "vision": self.vision_entry,
            "tags_match_camera": self.tags_match_camera,
            "vision_confidence": self.vision_confidence,
            "verdict": self.diagnosis.verdict.value,
            "action": self.diagnosis.action,
        }


@dataclass(frozen=True)
class PhysicalTestResult:
    name: str
    expectations: tuple[ExpectationResult, ...]

    @property
    def passed(self) -> bool:
        return all(e.diagnosis.verdict is Verdict.PASS for e in self.expectations)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expectations": [e.to_dict() for e in self.expectations],
        }


@dataclass
class RunReport:
    task_id: str
    run_id: str
    mode: str
    approved: bool
    deployed: bool
    passed: bool
    overall_verdict: str
    codegen: dict[str, object] = field(default_factory=dict)
    logic_fixes_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tests: list[PhysicalTestResult] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "mode": self.mode,
            "approved": self.approved,
            "deployed": self.deployed,
            "passed": self.passed,
            "verdict": self.overall_verdict,
            "logic_fixes_used": self.logic_fixes_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "codegen": self.codegen,
            "tests": [t.to_dict() for t in self.tests],
            "summary": self.summary,
        }


def values_match(expected: bool | int | float, actual: TagValue, tolerance: float | None) -> bool:
    """Spec value vs tag value; numeric tolerance defaults to exact."""
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    tol = 0.0 if tolerance is None else tolerance
    return abs(float(actual) - float(expected)) <= tol


def deadline_ms(exp: Expectation) -> int:
    """within_ms or at_ms deadline (exactly one is always set)."""
    assert exp.within_ms is not None or exp.at_ms is not None
    return exp.within_ms if exp.within_ms is not None else int(exp.at_ms)


def _default_now_ms() -> int:
    return int(time.monotonic() * 1000)


def real_vision_check(
    request: VisionCheckRequest,
    *,
    io_map: IoMap,
    frames: FrameSource,
    vision: VisionBackend,
    calibration: Calibration,
    angle_tolerance: float = ANGLE_TOLERANCE_DEFAULT,
) -> VisionOutcome:
    """Grab a frame, measure it, compare tags vs camera (SPEC steps 7-8).

    Failures become inconclusive (confidence 0.0), never guesses. The first
    inconclusive measurement re-captures once per the diagnosis table.
    Signals without a vision prompt skip the camera (tags trivially match).
    """
    signal = io_map.signals[request.signal]
    if signal.vision is None:
        entry: dict[str, object] = {"status": "no_vision_prompt"}
        return VisionOutcome(entry, True, 1.0, ())

    grabbed: list[bytes] = []
    entry = _measure_once(request, io_map, frames, vision, calibration, grabbed, angle_tolerance)
    if entry.get("status") != "measured":
        entry = _measure_once(
            request, io_map, frames, vision, calibration, grabbed, angle_tolerance
        )
    confidence = _entry_confidence(entry)
    if entry.get("status") != "measured":
        return VisionOutcome(entry, False, 0.0, tuple(grabbed))
    tags_match = _tags_match_camera(request, entry)
    return VisionOutcome(entry, tags_match, confidence, tuple(grabbed))


def _measure_once(
    request: VisionCheckRequest,
    io_map: IoMap,
    frames: FrameSource,
    vision: VisionBackend,
    calibration: Calibration,
    grabbed: list[bytes],
    angle_tolerance: float,
) -> dict[str, object]:
    signal = io_map.signals[request.signal]
    assert signal.vision is not None
    try:
        frame = frames.grab()
    except Exception as exc:
        return {"status": "inconclusive", "error": f"frame grab failed: {exc}"}
    grabbed.append(frame)
    expect_angle: int | None = None
    if signal.vision.measure == "angle" and isinstance(request.tag_value, (int, float)):
        expect_angle = int(request.tag_value)
    elif signal.vision.measure == "angle" and isinstance(request.expected, (int, float)):
        expect_angle = int(request.expected)
    return check_signal(
        request.signal,
        signal.vision.prompt,
        signal.vision.measure,
        frame,
        vision,
        calibration,
        expect_angle=expect_angle,
        angle_tolerance=request.tolerance if request.tolerance is not None else angle_tolerance,
    )


def _entry_confidence(entry: dict[str, object]) -> float:
    confidence = entry.get("confidence")
    if isinstance(confidence, (int, float)):
        return float(confidence)
    return 0.0


def _tags_match_camera(request: VisionCheckRequest, entry: dict[str, object]) -> bool:
    if entry.get("status") != "measured":
        return False
    if "lit" in entry:
        lit = entry["lit"]
        if not isinstance(lit, bool) or not isinstance(request.tag_value, bool):
            return False
        return lit is request.tag_value
    matched = entry.get("angle_match")
    if isinstance(matched, bool):
        return matched
    # No direct comparison available (e.g. angle without a baseline match):
    # a bare measurement neither confirms nor contradicts the tags.
    return matched is None and entry.get("status") == "measured"


def agreeing_vision_check(request: VisionCheckRequest) -> VisionOutcome:
    """Fake-mode checker: a healthy rig where the camera agrees with tags.

    Used by ``--fake`` plumbing and as a test default. Real disagreements
    are injected by tests via custom :data:`VisionCheckFn` callables.
    """
    if isinstance(request.tag_value, bool):
        entry: dict[str, object] = {
            "status": "measured",
            "lit": request.tag_value,
            "confidence": 1.0,
            "box_source": "fake",
        }
    else:
        entry = {
            "status": "measured",
            "angle": float(request.tag_value)
            if isinstance(request.tag_value, (int, float))
            else 0.0,
            "angle_match": True,
            "confidence": 1.0,
            "box_source": "fake",
        }
    return VisionOutcome(entry, True, 1.0, ())


def poll_tag(
    signal: str,
    expected: bool | int | float,
    tolerance: float | None,
    deadline_at_ms: int,
    tags: TagReader,
    *,
    poll_interval_ms: int = POLL_INTERVAL_MS,
    now_ms: NowMsFn = _default_now_ms,
    sleep_s: SleepFn = time.sleep,
) -> tuple[bool, TagValue | None]:
    """Poll until the tag matches or the deadline expires.

    Returns (matched, last_value). A read error counts as no match but
    keeps polling; when the deadline passes the last error still yields
    (False, None).
    """
    last: TagValue | None = None
    while True:
        try:
            last = tags.read(signal)
        except Exception:
            last = None
        if last is not None and values_match(expected, last, tolerance):
            return True, last
        if now_ms() >= deadline_at_ms:
            return False, last
        sleep_s(poll_interval_ms / 1000.0)


def run_stimulus_timeline(
    test_name: str,
    steps: list[object],
    stimulus: Stimulus,
    t0_ms: int,
    *,
    now_ms: NowMsFn = _default_now_ms,
    sleep_s: SleepFn = time.sleep,
) -> None:
    """Play press/release steps at their at_ms offsets from t0."""
    del test_name
    ordered = sorted(steps, key=lambda s: int(getattr(s, "at_ms")))  # StimulusStep
    for step in ordered:
        at_ms = int(getattr(step, "at_ms"))
        delay = (t0_ms + at_ms - now_ms()) / 1000.0
        if delay > 0:
            sleep_s(delay)
        press = getattr(step, "press")
        if press is not None:
            stimulus.press(str(press))
        else:
            stimulus.release(str(getattr(step, "release")))


def run_one_expectation(
    test_name: str,
    exp: Expectation,
    index: int,
    tags: TagReader,
    vision_check: VisionCheckFn,
    t0_ms: int,
    frames_dir: Path,
    *,
    poll_interval_ms: int = POLL_INTERVAL_MS,
    min_vision_confidence: float = DEFAULT_MIN_VISION_CONFIDENCE,
    now_ms: NowMsFn = _default_now_ms,
    sleep_s: SleepFn = time.sleep,
) -> ExpectationResult:
    """Poll the tag until its deadline, check the camera, diagnose (SPEC 7-9)."""
    dl = deadline_ms(exp)
    matched, tag_value = poll_tag(
        exp.signal,
        exp.value,
        exp.tolerance,
        t0_ms + dl,
        tags,
        poll_interval_ms=poll_interval_ms,
        now_ms=now_ms,
        sleep_s=sleep_s,
    )
    # Frame at the deadline per SPEC (sleep forward if the tag matched early).
    delay = (t0_ms + dl - now_ms()) / 1000.0
    if delay > 0:
        sleep_s(delay)
    outcome = vision_check(
        VisionCheckRequest(
            signal=exp.signal,
            expected=exp.value,
            tolerance=exp.tolerance,
            tag_value=tag_value if tag_value is not None else False,
        )
    )
    for frame_no, frame in enumerate(outcome.frames):
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / f"{test_name}_{index}_{exp.signal}_{frame_no}.jpg").write_bytes(frame)
    entry = dict(outcome.entry)
    entry.setdefault("signal", exp.signal)
    tags_match = outcome.tags_match_camera
    if tag_value is None:
        # No PLC reading at all: never call it a logic bug.
        tags_match = False
    diagnosis = diagnose(
        spec_matches_tags=matched,
        tags_match_camera=tags_match,
        vision_confidence=outcome.confidence,
        min_confidence=min_vision_confidence,
    )
    return ExpectationResult(
        test=test_name,
        signal=exp.signal,
        expected=exp.value,
        tag_value=tag_value,
        tag_match=matched,
        vision_entry=entry,
        tags_match_camera=outcome.tags_match_camera,
        vision_confidence=outcome.confidence,
        diagnosis=diagnosis,
    )


def run_physical_test(
    test_index: int,
    test: object,
    tags: TagReader,
    stimulus: Stimulus,
    vision_check: VisionCheckFn,
    frames_dir: Path,
    *,
    poll_interval_ms: int = POLL_INTERVAL_MS,
    min_vision_confidence: float = DEFAULT_MIN_VISION_CONFIDENCE,
    now_ms: NowMsFn = _default_now_ms,
    sleep_s: SleepFn = time.sleep,
) -> PhysicalTestResult:
    """Play one acceptance test's stimulus, then check each expectation."""
    del test_index
    name = str(getattr(test, "name"))
    steps = list(getattr(test, "stimulus"))
    expectations = list(getattr(test, "expect"))
    t0_ms = now_ms()
    run_stimulus_timeline(name, steps, stimulus, t0_ms, now_ms=now_ms, sleep_s=sleep_s)
    results: list[ExpectationResult] = []
    for index, exp in enumerate(expectations):
        results.append(
            run_one_expectation(
                name,
                exp,
                index,
                tags,
                vision_check,
                t0_ms,
                frames_dir,
                poll_interval_ms=poll_interval_ms,
                min_vision_confidence=min_vision_confidence,
                now_ms=now_ms,
                sleep_s=sleep_s,
            )
        )
    return PhysicalTestResult(name=name, expectations=tuple(results))


def format_repair_evidence(failures: list[ExpectationResult]) -> str:
    """Failing expectations as the repair prompt's verbatim evidence block."""
    lines = ["PHYSICAL TEST FAILURES (logic bug: PLC tags disagree with the spec):"]
    for item in failures:
        lines.append(
            f"- test {item.test!r} signal {item.signal!r}: "
            f"expected {item.expected!r}, PLC tag {item.tag_value!r} "
            f"(verdict {item.diagnosis.verdict.value})"
        )
    lines.append("Fix main.st so the PLC produces the expected values; keep tests.st aligned.")
    return "\n".join(lines)


def prompt_for_approval(files: ProgramFiles, diagram: str) -> bool:
    """CLI approval gate: show a summary, require an explicit 'approve'."""
    print("=== ST for approval ===")
    print(f"diagram: {len(diagram.splitlines())} line(s)")
    print(f"main.st: {len(files.main_st.splitlines())} line(s)")
    print(f"tests.st: {len(files.tests_st.splitlines())} line(s)")
    try:
        answer = input("Type 'approve' to deploy to the PLC: ").strip().lower()
    except EOFError:
        return False
    return answer == "approve"


def deploy_real(
    files: ProgramFiles,
    workdir: Path,
    plc: PlcClient,
    *,
    username: str,
    password: str,
    strucpp: str = "strucpp",
    modbus_host: str = "0.0.0.0",
    modbus_port: int = 5020,
    compile_timeout_s: float = 120.0,
) -> bytes:
    """Package program.zip, upload, wait for SUCCESS, start (SPEC step 5)."""
    from agent.packager import package

    workdir.mkdir(parents=True, exist_ok=True)
    main_st = workdir / "main.st"
    main_st.write_text(files.main_st, encoding="utf-8")
    zip_path = workdir / "program.zip"
    try:
        package(
            main_st,
            zip_path,
            strucpp=strucpp,
            modbus_host=modbus_host,
            modbus_port=modbus_port,
        )
    except (PackagerError, StrucppNotFoundError, FileNotFoundError) as exc:
        raise PlcError(f"packaging failed: {exc}") from exc
    blob = zip_path.read_bytes()
    plc.login(username, password)
    plc.upload_program(blob)
    if isinstance(plc, RestPlcClient):
        state = plc.wait_for_compilation(timeout_s=compile_timeout_s)
        if state != "SUCCESS":
            raise PlcError(f"PLC compilation {state}")
    elif plc.compilation_status() != "SUCCESS":
        raise PlcError(f"PLC compilation {plc.compilation_status()}")
    plc.start()
    return blob


def force_safe_state(
    io_map: IoMap, plc: PlcClient, writer: TagWriter, signals: list[str] | None = None
) -> None:
    """Stop the PLC, then drive outputs to io_map.safe_state over Modbus.

    The plugin holds last outputs when the PLC stops via the API, so the
    agent re-asserts the safe state itself (M6 closes the M3 known limit).
    Unknown safe-state entries are skipped; a best effort is still better
    than leaving lamps on.
    """
    try:
        plc.stop()
    finally:
        wanted = signals if signals is not None else list(io_map.safe_state)
        for name in wanted:
            if name not in io_map.safe_state or name not in io_map.signals:
                continue
            if io_map.signals[name].direction != "output":
                continue
            try:
                writer.write(name, io_map.safe_state[name])
            except Exception:
                continue


def _overall_verdict(tests: list[PhysicalTestResult]) -> str:
    verdicts = {e.diagnosis.verdict for t in tests for e in t.expectations}
    if not verdicts or verdicts == {Verdict.PASS}:
        return Verdict.PASS.value
    if Verdict.PHYSICAL_FAULT in verdicts:
        return Verdict.PHYSICAL_FAULT.value
    if Verdict.UNCLEAR in verdicts:
        return Verdict.UNCLEAR.value
    if Verdict.VISION_INCONCLUSIVE in verdicts:
        return Verdict.VISION_INCONCLUSIVE.value
    return Verdict.LOGIC_BUG.value


def _summarise(report: RunReport, spec: TaskSpec) -> str:
    total = sum(len(t.expectations) for t in report.tests)
    passed = sum(
        1 for t in report.tests for e in t.expectations if e.diagnosis.verdict is Verdict.PASS
    )
    lines = [
        f"task {spec.task_id}: {report.overall_verdict} ({passed}/{total} expectations pass)",
        f"mode={report.mode} approved={report.approved} deployed={report.deployed} "
        f"logic_fixes={report.logic_fixes_used} "
        f"tokens={report.input_tokens} in/{report.output_tokens} out",
    ]
    for test in report.tests:
        for exp in test.expectations:
            if exp.diagnosis.verdict is not Verdict.PASS:
                lines.append(
                    f"- {test.name}/{exp.signal}: expected {exp.expected!r}, "
                    f"tag {exp.tag_value!r}, {exp.diagnosis.verdict.value} "
                    f"({exp.diagnosis.action})"
                )
    if report.overall_verdict == Verdict.PHYSICAL_FAULT.value:
        lines.append("Check wiring/power at the reported output; PLC logic is correct.")
    elif report.overall_verdict == Verdict.LOGIC_BUG.value:
        lines.append("ST repaired and re-tested; inspect the diff if fixes ran out.")
    elif report.overall_verdict in (Verdict.UNCLEAR.value, Verdict.VISION_INCONCLUSIVE.value):
        lines.append("Handing back to the human; see report.json and frames/.")
    return "\n".join(lines) + "\n"


def run_loop(
    spec: TaskSpec,
    io_map: IoMap,
    *,
    run_dir: Path,
    llm: LlmBackend,
    plc: PlcClient,
    tags: TagReader,
    writer: TagWriter,
    stimulus: Stimulus,
    vision_check: VisionCheckFn,
    strucpp: str = "strucpp",
    max_offline_rounds: int = MAX_REPAIR_ROUNDS,
    max_logic_fixes: int = MAX_LOGIC_FIXES,
    poll_interval_ms: int = POLL_INTERVAL_MS,
    min_vision_confidence: float = DEFAULT_MIN_VISION_CONFIDENCE,
    mode: str = "real",
    approve_fn: ApproveFn | None = None,
    deploy_fn: DeployFn | None = None,
    prepare_test_fn: PrepareTestFn | None = None,
    sleep_s: SleepFn = time.sleep,
    now_ms: NowMsFn | None = None,
    plc_username: str = "",
    plc_password: str = "",
    modbus_host: str = "0.0.0.0",
    modbus_port: int = 5020,
    calibration: Calibration | None = None,
) -> RunReport:
    """Run SPEC steps 1-10 with at most ``max_logic_fixes`` physical repairs."""
    clock: NowMsFn = now_ms if now_ms is not None else _default_now_ms
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = run_dir / "frames"
    run_id = run_dir.name
    report = RunReport(
        task_id=spec.task_id,
        run_id=run_id,
        mode=mode,
        approved=False,
        deployed=False,
        passed=False,
        overall_verdict="codegen_failed",
    )

    def _deploy(files: ProgramFiles) -> bytes:
        if deploy_fn is not None:
            return deploy_fn(files, run_dir / "deploy")
        return deploy_real(
            files,
            run_dir / "deploy",
            plc,
            username=plc_username,
            password=plc_password,
            strucpp=strucpp,
            modbus_host=modbus_host,
            modbus_port=modbus_port,
        )

    # Steps 1-3: plan, generate, offline check (M4).
    codegen = generate_with_repairs(
        spec,
        io_map,
        llm,
        workdir=run_dir / "codegen",
        max_rounds=max_offline_rounds,
        strucpp=strucpp,
    )
    report.input_tokens += codegen.input_tokens
    report.output_tokens += codegen.output_tokens
    report.codegen = codegen.to_dict()
    files = codegen.files
    diagram = codegen.diagram
    (run_dir / "diagram.mmd").write_text(diagram, encoding="utf-8")
    if not codegen.passed:
        report.summary = _summarise(report, spec)
        _write_run_files(run_dir, spec, files, report, calibration)
        return report

    # Step 4: human approval before any deploy.
    approver = approve_fn if approve_fn is not None else prompt_for_approval
    report.approved = bool(approver(files, diagram))
    if not report.approved:
        report.overall_verdict = "not_approved"
        report.summary = _summarise(report, spec)
        _write_run_files(run_dir, spec, files, report, calibration)
        return report

    # Step 5: deploy.
    try:
        _deploy(files)
    except (PlcError, PackagerError, StrucppNotFoundError, OSError) as exc:
        report.overall_verdict = "deploy_failed"
        report.summary = f"deploy failed: {exc}\n" + _summarise(report, spec)
        _write_run_files(run_dir, spec, files, report, calibration)
        return report
    report.deployed = True

    # Steps 6-10: calibrate once (caller-supplied), test, diagnose, repair.
    physical_repairs: list[dict[str, object]] = []
    try:
        while True:
            tests: list[PhysicalTestResult] = []
            for index, test in enumerate(spec.tests):
                if prepare_test_fn is not None:
                    prepare_test_fn(index, test)
                tests.append(
                    run_physical_test(
                        index,
                        test,
                        tags,
                        stimulus,
                        vision_check,
                        frames_dir,
                        poll_interval_ms=poll_interval_ms,
                        min_vision_confidence=min_vision_confidence,
                        now_ms=clock,
                        sleep_s=sleep_s,
                    )
                )
            report.tests = tests
            report.overall_verdict = _overall_verdict(tests)
            report.passed = report.overall_verdict == Verdict.PASS.value
            logic_failures = [
                e for t in tests for e in t.expectations if e.diagnosis.verdict is Verdict.LOGIC_BUG
            ]
            if not logic_failures or report.logic_fixes_used >= max_logic_fixes:
                break
            # A physical fault / unclear / inconclusive anywhere stops the loop;
            # only pure logic bugs (camera agrees with tags) are repaired.
            blocking = [
                e
                for t in tests
                for e in t.expectations
                if e.diagnosis.verdict
                in (Verdict.PHYSICAL_FAULT, Verdict.UNCLEAR, Verdict.VISION_INCONCLUSIVE)
            ]
            if blocking:
                break
            report.logic_fixes_used += 1
            evidence = format_repair_evidence(logic_failures)
            files, reply = repair_program(
                spec,
                io_map,
                files,
                evidence,
                llm,
                round=report.logic_fixes_used,
                max_rounds=max_logic_fixes,
            )
            report.input_tokens += reply.input_tokens
            report.output_tokens += reply.output_tokens
            check = offline_check(
                files, run_dir / f"repair{report.logic_fixes_used}", strucpp=strucpp
            )
            physical_repairs.append(
                {
                    "fix": report.logic_fixes_used,
                    "offline_passed": check.passed,
                    "errors_tail": check.errors_verbatim[-500:],
                }
            )
            if not check.passed:
                evidence += "\n\nOFFLINE CHECK STILL FAILS:\n" + check.errors_verbatim
                continue  # next round repairs the offline errors too
            try:
                _deploy(files)
            except (PlcError, PackagerError, StrucppNotFoundError, OSError) as exc:
                report.overall_verdict = "deploy_failed"
                report.summary = f"re-deploy failed: {exc}\n" + _summarise(report, spec)
                _write_run_files(run_dir, spec, files, report, calibration)
                return report
    finally:
        try:
            force_safe_state(io_map, plc, writer, spec.signals)
        except Exception:
            pass
    report.codegen["physical_repairs"] = physical_repairs
    report.summary = _summarise(report, spec)
    _write_run_files(run_dir, spec, files, report, calibration)
    return report


def _write_run_files(
    run_dir: Path,
    spec: TaskSpec,
    files: ProgramFiles,
    report: RunReport,
    calibration: Calibration | None,
) -> None:
    import yaml

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spec.yaml").write_text(
        yaml.safe_dump(spec.model_dump(), sort_keys=False), encoding="utf-8"
    )
    (run_dir / "main.st").write_text(files.main_st, encoding="utf-8")
    (run_dir / "tests.st").write_text(files.tests_st, encoding="utf-8")
    (run_dir / "codegen_report.json").write_text(
        json.dumps(report.codegen, indent=2) + "\n", encoding="utf-8"
    )
    if calibration is not None:
        calibration.save(run_dir / "calibration.json")
    (run_dir / "report.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "summary.txt").write_text(report.summary, encoding="utf-8")


def _fake_llm_for_loop() -> ScriptedLlmBackend:
    """Scripted plan + ST replies (checked-in example) for --fake plumbing."""
    fewshot = Path(__file__).resolve().parent.parent / "specs" / "start_lamp_arm"
    plan = "```mermaid\nstateDiagram-v2\n    [*] --> safe\n    safe --> [*]\n```\n"
    main_st = (fewshot / "main.st").read_text(encoding="utf-8")
    tests_st = (fewshot / "tests.st").read_text(encoding="utf-8")
    code = f"```st\n{main_st}```\n```st\n{tests_st}```\n"
    # Plan + initial + one spare per physical repair.
    return ScriptedLlmBackend([plan, code] + [code] * MAX_LOGIC_FIXES)


def _fake_tags_for_spec(spec: TaskSpec) -> FakeTagReader:
    """Initial fake tags; per-test values are set by the prepare hook."""
    values: dict[str, TagValue] = {}
    for test in spec.tests:
        for exp in test.expect:
            values.setdefault(exp.signal, exp.value)
    return FakeTagReader(values)


def _prepare_fake_test(tags: FakeTagReader, index: int, test: object) -> None:
    """Drive fake tags to the current test's expectations (a correct PLC)."""
    del index
    for exp in getattr(test, "expect"):
        tags.write(str(getattr(exp, "signal")), getattr(exp, "value"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the closed PLC commissioning loop.")
    parser.add_argument("spec", help="task spec YAML, e.g. specs/start_lamp_arm.yaml")
    parser.add_argument("--fake", action="store_true", help="fakes only: no Pi, no API, no credits")
    parser.add_argument(
        "--approve", action="store_true", help="explicit deploy approval (no prompt)"
    )
    parser.add_argument(
        "--runs-dir", default="runs", help="parent dir for runs/<timestamp>-<task_id>"
    )
    parser.add_argument("--out", default=None, help="exact run dir (overrides --runs-dir)")
    parser.add_argument("--io-map", default=None, help="io_map.yaml path (default: project file)")
    parser.add_argument("--calibration", default=None, help="calibration.json path (real mode)")
    parser.add_argument("--strucpp", default="strucpp")
    parser.add_argument("--max-fixes", type=int, default=MAX_LOGIC_FIXES)
    parser.add_argument("--max-offline-rounds", type=int, default=MAX_REPAIR_ROUNDS)
    parser.add_argument("--poll-ms", type=int, default=POLL_INTERVAL_MS)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_VISION_CONFIDENCE)
    parser.add_argument(
        "--modbus-port", type=int, default=None, help="default: $MODBUS_PORT or 5020"
    )
    args = parser.parse_args(argv)

    spec = load_task_spec(args.spec)
    io_map = load_io_map(args.io_map)
    check_against_io_map(spec, io_map)
    if args.out:
        run_dir = Path(args.out)
    else:
        stamp = datetime.now(UTC).strftime(RUN_ID_FORMAT)
        run_dir = Path(args.runs_dir) / f"{stamp}-{spec.task_id}"

    if args.fake:
        llm: LlmBackend = _fake_llm_for_loop()
        plc: PlcClient = FakePlcClient()
        fake_tags = _fake_tags_for_spec(spec)
        tags: TagReader = fake_tags
        writer: TagWriter = FakeTagWriter()
        stimulus: Stimulus = FakeStimulus()
        vision_check: VisionCheckFn = agreeing_vision_check
        calibration: Calibration | None = Calibration()

        def _fake_deploy(files: ProgramFiles, workdir: Path) -> bytes:
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "program.zip").write_bytes(FAKE_PROGRAM_ZIP)
            assert isinstance(plc, FakePlcClient)
            plc.login("fake", "fake")
            plc.upload_program(FAKE_PROGRAM_ZIP)
            plc.start()
            return FAKE_PROGRAM_ZIP

        # --fake uploads dummy bytes to an in-memory PLC: no hardware, so the
        # explicit --approve prompt is skipped (real mode still requires it).
        print("fake mode: auto-approved (no real PLC)")
        report = run_loop(
            spec,
            io_map,
            run_dir=run_dir,
            llm=llm,
            plc=plc,
            tags=tags,
            writer=writer,
            stimulus=stimulus,
            vision_check=vision_check,
            strucpp=args.strucpp,
            max_offline_rounds=args.max_offline_rounds,
            max_logic_fixes=args.max_fixes,
            poll_interval_ms=args.poll_ms,
            min_vision_confidence=args.min_confidence,
            mode="fake",
            approve_fn=lambda _f, _d: True,
            deploy_fn=_fake_deploy,
            prepare_test_fn=lambda index, test: _prepare_fake_test(fake_tags, index, test),
            calibration=calibration,
        )
        print(f"{spec.task_id}: {report.overall_verdict} -> {run_dir}")
        print(report.summary)
        return 0 if report.passed else 1

    load_dotenv()
    modbus_port = args.modbus_port
    if modbus_port is None:
        try:
            modbus_port = int(os.environ.get("MODBUS_PORT", "5020"))
        except ValueError:
            raise SystemExit("bad MODBUS_PORT: want an int")
    openplc_url = os.environ.get("OPENPLC_URL", "").strip()
    openplc_user = os.environ.get("OPENPLC_USER", "").strip()
    openplc_password = os.environ.get("OPENPLC_PASSWORD", "").strip()
    pi_host = os.environ.get("PI_HOST", "").strip()
    stimulus_url = os.environ.get("STIMULUS_URL", "").strip()
    frames_url = os.environ.get("FRAMES_URL", "").strip()
    missing = [
        name
        for name, value in (
            ("OPENPLC_URL", openplc_url),
            ("OPENPLC_USER", openplc_user),
            ("OPENPLC_PASSWORD", openplc_password),
            ("PI_HOST", pi_host),
            ("STIMULUS_URL", stimulus_url),
            ("FRAMES_URL", frames_url),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"missing env vars: {', '.join(missing)} (see .env.example)")
    if args.calibration:
        calibration = Calibration.load(args.calibration)
    else:
        default_cal = run_dir / "calibration.json"
        calibration = Calibration.load(default_cal) if default_cal.is_file() else Calibration()
    real_llm = MetaLlmBackend.from_env()
    real_plc = RestPlcClient(openplc_url)
    real_tags = ModbusTagReader(pi_host, modbus_port, io_map)
    real_writer = ModbusTagWriter(pi_host, modbus_port, io_map)
    real_stimulus = HttpStimulus(stimulus_url)
    real_frames = HttpFrameSource(frames_url)
    real_vision = MetaSamBackend.from_env()

    def _real_vision_check(request: VisionCheckRequest) -> VisionOutcome:
        assert calibration is not None
        return real_vision_check(
            request,
            io_map=io_map,
            frames=real_frames,
            vision=real_vision,
            calibration=calibration,
        )

    try:
        report = run_loop(
            spec,
            io_map,
            run_dir=run_dir,
            llm=real_llm,
            plc=real_plc,
            tags=real_tags,
            writer=real_writer,
            stimulus=real_stimulus,
            vision_check=_real_vision_check,
            strucpp=args.strucpp,
            max_offline_rounds=args.max_offline_rounds,
            max_logic_fixes=args.max_fixes,
            poll_interval_ms=args.poll_ms,
            min_vision_confidence=args.min_confidence,
            mode="real",
            approve_fn=(lambda _f, _d: True) if args.approve else None,
            deploy_fn=None,
            plc_username=openplc_user,
            plc_password=openplc_password,
            modbus_port=modbus_port,
            calibration=calibration,
        )
    finally:
        real_llm.close()
        real_plc.close()
        real_tags.close()
        real_writer.close()
        real_stimulus.close()
        real_frames.close()
        real_vision.close()
    print(f"{spec.task_id}: {report.overall_verdict} -> {run_dir}")
    print(report.summary)
    return 0 if report.passed else 1


def run() -> None:
    """Backwards-compatible stub entry (use main() / run_loop())."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
