"""Closed-loop tests with fakes only (no Pi, no API, no strucpp binary)."""

import json
from pathlib import Path

import pytest

from agent.diagnose import Verdict
from agent.io_map import load_io_map
from agent.llm_backend import ScriptedLlmBackend
from agent.loop import (
    VisionCheckRequest,
    VisionOutcome,
    agreeing_vision_check,
    deadline_ms,
    deploy_real,
    force_safe_state,
    format_repair_evidence,
    main,
    poll_tag,
    run_loop,
    run_one_expectation,
    run_physical_test,
    run_stimulus_timeline,
    values_match,
    with_pi_host,
)
from agent.measure import Calibration
from agent.plc_client import FakePlcClient, PlcError
from agent.spec import (
    AcceptanceTest,
    Expectation,
    StimulusStep,
    TaskSpec,
    check_against_io_map,
    load_task_spec,
)
from agent.stimulus import FakeStimulus
from agent.tag_reader import FakeTagReader, TagValue
from agent.tag_writer import FakeTagWriter

ROOT = Path(__file__).resolve().parent.parent

FAKE_PASS = """#!/usr/bin/env python3
import sys
print("ok:", " ".join(sys.argv[1:]))
"""

FAIL_ALWAYS = """#!/usr/bin/env python3
import sys
print("error: bad ST", file=sys.stderr)
sys.exit(1)
"""


def _write_exe(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _code_reply(main_st: str = "MAIN", tests_st: str = "TESTS") -> str:
    return f"```st\n{main_st}\n```\n```st\n{tests_st}\n```\n"


def _llm_for_pass(extra_repairs: int = 0) -> ScriptedLlmBackend:
    plan = "```mermaid\nstateDiagram-v2\n```"
    code = _code_reply()
    return ScriptedLlmBackend([plan, code] + [code] * extra_repairs)


def _single_spec() -> TaskSpec:
    spec = TaskSpec(
        task_id="t",
        description="d",
        signals=["start_btn", "stop_btn", "lamp_red"],
        tests=[
            AcceptanceTest(
                name="t1",
                stimulus=[StimulusStep(at_ms=0, press="start_btn")],
                expect=[Expectation(signal="lamp_red", value=True, within_ms=100)],
            )
        ],
    )
    check_against_io_map(spec, load_io_map())
    return spec


class FakeClock:
    """Manual clock: sleep advances time instantly (no real delay)."""

    def __init__(self) -> None:
        self.t_ms = 1000

    def now_ms(self) -> int:
        return self.t_ms

    def sleep_s(self, seconds: float) -> None:
        self.t_ms += int(seconds * 1000)


def test_values_match_bool_and_numeric() -> None:
    assert values_match(True, True, None) is True
    assert values_match(True, False, None) is False
    assert values_match(90, 90, None) is True
    assert values_match(90, 95, 10.0) is True
    assert values_match(90, 95, None) is False
    assert values_match(90, True, 10.0) is False
    assert values_match(True, 1, None) is False


def test_deadline_ms_prefers_within() -> None:
    assert deadline_ms(Expectation(signal="s", value=True, within_ms=100)) == 100
    assert deadline_ms(Expectation(signal="s", value=True, at_ms=3500)) == 3500


def test_poll_tag_matches_immediately() -> None:
    clock = FakeClock()
    matched, value = poll_tag(
        "lamp_red",
        True,
        None,
        clock.now_ms() + 100,
        FakeTagReader({"lamp_red": True}),
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert (matched, value) == (True, True)
    assert clock.t_ms == 1000  # no sleep needed


def test_poll_tag_times_out() -> None:
    clock = FakeClock()
    matched, value = poll_tag(
        "lamp_red",
        True,
        None,
        clock.now_ms() + 100,
        FakeTagReader({"lamp_red": False}),
        poll_interval_ms=50,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert (matched, value) == (False, False)
    assert clock.t_ms >= 1100


def test_poll_tag_read_errors_yield_none() -> None:
    clock = FakeClock()

    class Broken(FakeTagReader):
        def read(self, signal: str) -> TagValue:
            raise KeyError(signal)

    matched, value = poll_tag(
        "lamp_red",
        True,
        None,
        clock.now_ms() + 60,
        Broken({}),
        poll_interval_ms=50,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert (matched, value) == (False, None)


def test_stimulus_timeline_plays_in_order() -> None:
    clock = FakeClock()
    stim = FakeStimulus()
    steps = [
        StimulusStep(at_ms=200, release="start_btn"),
        StimulusStep(at_ms=0, press="start_btn"),
    ]
    run_stimulus_timeline(
        "t", steps, stim, clock.now_ms(), now_ms=clock.now_ms, sleep_s=clock.sleep_s
    )
    assert [(e.action, e.button) for e in stim.events] == [
        ("press", "start_btn"),
        ("release", "start_btn"),
    ]
    assert clock.t_ms == 1200


def test_agreeing_vision_check_matches_tags() -> None:
    out = agreeing_vision_check(VisionCheckRequest("lamp_red", True, None, True))
    assert out.tags_match_camera is True and out.confidence == 1.0
    assert out.entry["lit"] is True
    out = agreeing_vision_check(VisionCheckRequest("arm_angle", 90, 10.0, 90))
    assert out.tags_match_camera is True and out.entry["angle_match"] is True


def _expectation(**overrides: object) -> Expectation:
    base: dict[str, object] = {"signal": "lamp_red", "value": True, "within_ms": 100}
    base.update(overrides)
    return Expectation.model_validate(base)


def test_run_one_expectation_pass(tmp_path: Path) -> None:
    clock = FakeClock()
    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": True}),
        agreeing_vision_check,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.tag_match is True
    assert result.diagnosis.verdict is Verdict.PASS


def test_run_one_expectation_logic_bug(tmp_path: Path) -> None:
    clock = FakeClock()
    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": False}),
        agreeing_vision_check,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    # Tags say off, camera agrees with tags: the ST is wrong.
    assert result.tag_match is False
    assert result.tags_match_camera is True
    assert result.diagnosis.verdict is Verdict.LOGIC_BUG


def _disagreeing_vision(request: VisionCheckRequest) -> VisionOutcome:
    entry: dict[str, object] = {
        "status": "measured",
        "lit": not request.tag_value,
        "confidence": 1.0,
        "box_source": "fake",
    }
    return VisionOutcome(entry, False, 1.0, ())


def test_run_one_expectation_physical_fault(tmp_path: Path) -> None:
    clock = FakeClock()
    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": True}),
        _disagreeing_vision,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.tag_match is True
    assert result.diagnosis.verdict is Verdict.PHYSICAL_FAULT


def test_run_one_expectation_unclear(tmp_path: Path) -> None:
    clock = FakeClock()
    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": False}),
        _disagreeing_vision,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.diagnosis.verdict is Verdict.UNCLEAR


def _low_confidence_vision(request: VisionCheckRequest) -> VisionOutcome:
    entry: dict[str, object] = {"status": "inconclusive", "confidence": 0.1}
    return VisionOutcome(entry, False, 0.1, ())


def test_run_one_expectation_vision_inconclusive(tmp_path: Path) -> None:
    clock = FakeClock()
    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": True}),
        _low_confidence_vision,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.diagnosis.verdict is Verdict.VISION_INCONCLUSIVE


def test_run_one_expectation_tag_error_is_unclear(tmp_path: Path) -> None:
    clock = FakeClock()

    class Broken(FakeTagReader):
        def read(self, signal: str) -> TagValue:
            raise KeyError(signal)

    result = run_one_expectation(
        "t1",
        _expectation(),
        0,
        Broken({}),
        agreeing_vision_check,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.tag_value is None
    assert result.diagnosis.verdict is Verdict.UNCLEAR


def test_run_physical_test_plays_stimulus_and_checks(tmp_path: Path) -> None:
    clock = FakeClock()
    stim = FakeStimulus()
    spec = _single_spec()
    result = run_physical_test(
        0,
        spec.tests[0],
        FakeTagReader({"lamp_red": True}),
        stim,
        agreeing_vision_check,
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    assert result.passed is True
    assert [(e.action, e.button) for e in stim.events] == [("press", "start_btn")]


def test_format_repair_evidence_names_failures(tmp_path: Path) -> None:
    clock = FakeClock()
    failure = run_one_expectation(
        "t1",
        _expectation(),
        0,
        FakeTagReader({"lamp_red": False}),
        agreeing_vision_check,
        clock.now_ms(),
        tmp_path,
        now_ms=clock.now_ms,
        sleep_s=clock.sleep_s,
    )
    evidence = format_repair_evidence([failure])
    assert "t1" in evidence and "lamp_red" in evidence
    assert "True" in evidence and "False" in evidence


def test_force_safe_state_stops_and_writes_safe() -> None:
    io_map = load_io_map()
    plc = FakePlcClient()
    plc.login("u", "p")
    plc.start()
    writer = FakeTagWriter()
    force_safe_state(io_map, plc, writer, ["start_btn", "lamp_red", "arm_angle", "nope"])
    assert plc.status().running is False
    assert ("lamp_red", False) in writer.writes
    assert ("arm_angle", 0) in writer.writes
    assert all(name != "start_btn" for name, _ in writer.writes)  # inputs skipped


def _fake_deploy(plc: FakePlcClient):  # type: ignore[no-untyped-def]
    def _deploy(files, workdir):  # type: ignore[no-untyped-def]
        workdir.mkdir(parents=True, exist_ok=True)
        plc.login("u", "p")
        plc.upload_program(b"zip")
        plc.start()
        return b"zip"

    return _deploy


def test_run_loop_full_pass(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc, writer, stim = FakePlcClient(), FakeTagWriter(), FakeStimulus()
    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(),
        plc=plc,
        tags=FakeTagReader({"lamp_red": True}),
        writer=writer,
        stimulus=stim,
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        approve_fn=lambda _f, _d: True,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
        calibration=Calibration(),
    )
    assert report.passed is True and report.overall_verdict == "pass"
    assert report.approved and report.deployed and report.logic_fixes_used == 0
    assert plc.uploads == [b"zip"]
    assert plc.status().running is False  # safe-state forced after the run
    assert ("lamp_red", False) in writer.writes
    run_dir = tmp_path / "run"
    for name in (
        "spec.yaml",
        "diagram.mmd",
        "main.st",
        "tests.st",
        "codegen_report.json",
        "calibration.json",
        "report.json",
        "summary.txt",
    ):
        assert (run_dir / name).is_file(), name
    stored = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert stored["verdict"] == "pass" and stored["input_tokens"] > 0


def test_run_loop_writes_progress_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.loop as loop_mod

    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc, writer, stim = FakePlcClient(), FakeTagWriter(), FakeStimulus()
    run_dir = tmp_path / "run"
    seen: dict[str, object] = {}
    real_codegen = loop_mod.generate_with_repairs

    def _spy_codegen(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        seen["generating"] = json.loads((run_dir / "status.json").read_text())["stage"]
        return real_codegen(*args, **kwargs)

    monkeypatch.setattr(loop_mod, "generate_with_repairs", _spy_codegen)

    def _approve(files: object, diagram: str) -> bool:
        seen["at_approval"] = json.loads((run_dir / "status.json").read_text())["stage"]
        seen["early_code"] = (run_dir / "main.st").is_file()
        return True

    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=run_dir,
        llm=_llm_for_pass(),
        plc=plc,
        tags=FakeTagReader({"lamp_red": True}),
        writer=writer,
        stimulus=stim,
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        approve_fn=_approve,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
        calibration=Calibration(),
    )
    assert report.passed
    assert seen["generating"] == "generating"
    assert seen["at_approval"] == "awaiting_approval"
    assert seen["early_code"] is True
    final = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert final["stage"] == "done" and final["verdict"] == "pass"


def test_run_loop_not_approved_skips_deploy(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc = FakePlcClient()
    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(),
        plc=plc,
        tags=FakeTagReader({"lamp_red": True}),
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        approve_fn=lambda _f, _d: False,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.overall_verdict == "not_approved" and not report.deployed
    assert plc.uploads == []


def test_run_loop_codegen_failed_skips_deploy(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAIL_ALWAYS)
    clock = FakeClock()
    plc = FakePlcClient()
    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(extra_repairs=3),
        plc=plc,
        tags=FakeTagReader({"lamp_red": True}),
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        max_offline_rounds=2,
        approve_fn=lambda _f, _d: True,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.overall_verdict == "codegen_failed" and not report.deployed
    assert plc.uploads == []


def test_run_loop_deploy_failed(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()

    def _boom(files, workdir):  # type: ignore[no-untyped-def]
        raise PlcError("upload down")

    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(),
        plc=FakePlcClient(),
        tags=FakeTagReader({"lamp_red": True}),
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        approve_fn=lambda _f, _d: True,
        deploy_fn=_boom,
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.overall_verdict == "deploy_failed" and not report.deployed


class AttemptTags(FakeTagReader):
    """Buggy on deploy 0 (lamp stuck off), fixed from deploy 1 on."""

    def __init__(self) -> None:
        super().__init__({"lamp_red": False})
        self.attempt = 0

    def read(self, signal: str) -> TagValue:
        if signal == "lamp_red" and self.attempt >= 1:
            return True
        return super().read(signal)


def test_run_loop_repairs_logic_bug(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc = FakePlcClient()
    tags = AttemptTags()

    def _deploy(files, workdir):  # type: ignore[no-untyped-def]
        tags.attempt = len(plc.uploads)  # 0 on first deploy, 1 on re-deploy
        return _fake_deploy(plc)(files, workdir)

    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(extra_repairs=1),
        plc=plc,
        tags=tags,
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        approve_fn=lambda _f, _d: True,
        deploy_fn=_deploy,
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.passed is True and report.logic_fixes_used == 1
    assert len(plc.uploads) == 2
    assert report.codegen["physical_repairs"][0]["offline_passed"] is True


def test_run_loop_gives_up_after_max_fixes(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc = FakePlcClient()
    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(extra_repairs=3),
        plc=plc,
        tags=FakeTagReader({"lamp_red": False}),  # never fixed
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=agreeing_vision_check,
        strucpp=str(exe),
        max_logic_fixes=2,
        approve_fn=lambda _f, _d: True,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.overall_verdict == "logic_bug" and report.logic_fixes_used == 2
    assert len(plc.uploads) == 3  # initial + 2 re-deploys


def test_run_loop_physical_fault_stops_without_repair(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    clock = FakeClock()
    plc = FakePlcClient()
    report = run_loop(
        _single_spec(),
        load_io_map(),
        run_dir=tmp_path / "run",
        llm=_llm_for_pass(),
        plc=plc,
        tags=FakeTagReader({"lamp_red": True}),
        writer=FakeTagWriter(),
        stimulus=FakeStimulus(),
        vision_check=_disagreeing_vision,
        strucpp=str(exe),
        approve_fn=lambda _f, _d: True,
        deploy_fn=_fake_deploy(plc),
        sleep_s=clock.sleep_s,
        now_ms=clock.now_ms,
    )
    assert report.overall_verdict == "physical_fault"
    assert report.logic_fixes_used == 0 and len(plc.uploads) == 1
    assert "wiring" in report.summary


def test_deploy_real_rejects_failed_packaging(tmp_path: Path) -> None:
    from agent.codegen import ProgramFiles

    exe = _write_exe(tmp_path / "strucpp", FAIL_ALWAYS)
    try:
        deploy_real(
            ProgramFiles(main_st="bad", tests_st="bad"),
            tmp_path / "work",
            FakePlcClient(),
            username="u",
            password="p",
            strucpp=str(exe),
        )
    except PlcError as exc:
        assert "packaging failed" in str(exc)
    else:
        raise AssertionError("expected PlcError")


def test_main_fake_end_to_end(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    out = tmp_path / "run"
    code = main(
        [
            str(ROOT / "specs" / "start_lamp_arm.yaml"),
            "--fake",
            "--out",
            str(out),
            "--strucpp",
            str(exe),
        ]
    )
    assert code == 0
    stored = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert stored["verdict"] == "pass" and stored["mode"] == "fake"
    assert stored["deployed"] is True and len(stored["tests"]) == 2


def test_main_fake_loads_example_spec(tmp_path: Path) -> None:
    spec = load_task_spec(ROOT / "specs" / "start_lamp_arm.yaml")
    assert spec.task_id == "start_lamp_arm"


def test_with_pi_host_swaps_host_keeps_scheme_port_path() -> None:
    assert with_pi_host("https://raspberrypi:8443", "10.0.0.7", "") == "https://10.0.0.7:8443"
    assert with_pi_host("http://old/x", "pi.lan", "") == "http://pi.lan/x"
    assert with_pi_host("", "10.0.0.7", "http://{host}:8082") == "http://10.0.0.7:8082"


def test_with_pi_host_rejects_bad_host() -> None:
    for bad in ("", "a b", "x/y", "h:80", "-x"):
        with pytest.raises(ValueError):
            with_pi_host("http://old:1", bad, "")
