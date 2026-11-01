from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.io_map import load_io_map
from agent.spec import (
    AcceptanceTest,
    Expectation,
    StimulusStep,
    TaskSpec,
    check_against_io_map,
    load_task_spec,
)

ROOT = Path(__file__).resolve().parent.parent


def _task(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "task_id": "t",
        "description": "d",
        "signals": ["start_btn", "stop_btn", "lamp_red"],
        "tests": [
            AcceptanceTest(
                name="t",
                stimulus=[StimulusStep(at_ms=0, press="start_btn")],
                expect=[Expectation(signal="lamp_red", value=True, within_ms=100)],
            )
        ],
    }
    base.update(overrides)
    return TaskSpec.model_validate(base)


def test_loads_start_lamp_arm_spec() -> None:
    spec = load_task_spec(ROOT / "specs" / "start_lamp_arm.yaml")
    assert spec.task_id == "start_lamp_arm"
    assert spec.signals == ["start_btn", "stop_btn", "lamp_red", "arm_angle"]
    assert [t.name for t in spec.tests] == ["start_latches_lamp", "stop_clears_lamp"]
    by_name = {t.name: t for t in spec.tests}
    assert ("arm_angle", 90) in [(e.signal, e.value) for e in by_name["start_latches_lamp"].expect]
    assert ("arm_angle", 0) in [(e.signal, e.value) for e in by_name["stop_clears_lamp"].expect]
    check_against_io_map(spec, load_io_map())  # must not raise


def test_unknown_signal_rejected() -> None:
    spec = _task(signals=["nope"])
    with pytest.raises(ValueError, match="unknown signal"):
        check_against_io_map(spec, load_io_map())


def test_stimulus_needs_exactly_one_action() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        StimulusStep(at_ms=0)
    with pytest.raises(ValidationError, match="exactly one"):
        StimulusStep(at_ms=0, press="start_btn", release="start_btn")


def test_expectation_needs_exactly_one_deadline() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        Expectation(signal="lamp_red", value=True)
    with pytest.raises(ValidationError, match="exactly one"):
        Expectation(signal="lamp_red", value=True, within_ms=100, at_ms=200)


def test_stimulus_on_output_rejected() -> None:
    spec = _task(
        tests=[
            AcceptanceTest(
                name="t",
                stimulus=[StimulusStep(at_ms=0, press="lamp_red")],
                expect=[Expectation(signal="lamp_red", value=True, within_ms=100)],
            )
        ]
    )
    with pytest.raises(ValueError, match="non-input"):
        check_against_io_map(spec, load_io_map())


def test_bool_signal_rejects_numeric_value() -> None:
    spec = _task(
        tests=[
            AcceptanceTest(
                name="t",
                expect=[Expectation(signal="lamp_red", value=1, within_ms=100)],
            )
        ]
    )
    with pytest.raises(ValueError, match="does not fit"):
        check_against_io_map(spec, load_io_map())


def test_int_signal_rejects_bool_value() -> None:
    spec = _task(
        signals=["start_btn", "arm_angle"],
        tests=[
            AcceptanceTest(
                name="t",
                stimulus=[StimulusStep(at_ms=0, press="start_btn")],
                expect=[Expectation(signal="arm_angle", value=True, within_ms=100)],
            )
        ],
    )
    with pytest.raises(ValueError, match="does not fit"):
        check_against_io_map(spec, load_io_map())
