"""Task spec schema (``specs/*.yaml``) — the format from ``docs/SPEC.md``."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, model_validator

from agent.io_map import IoMap


class StimulusStep(BaseModel):
    at_ms: int = Field(ge=0)
    press: str | None = None
    release: str | None = None

    @model_validator(mode="after")
    def _exactly_one_action(self) -> Self:
        if (self.press is None) == (self.release is None):
            raise ValueError("stimulus step needs exactly one of 'press' / 'release'")
        return self


class Expectation(BaseModel):
    signal: str
    value: bool | int | float
    within_ms: int | None = Field(default=None, ge=0)
    at_ms: int | None = Field(default=None, ge=0)
    tolerance: float | None = Field(default=None, ge=0)
    tolerance_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_one_deadline(self) -> Self:
        if (self.within_ms is None) == (self.at_ms is None):
            raise ValueError("expectation needs exactly one of 'within_ms' / 'at_ms'")
        return self


class AcceptanceTest(BaseModel):
    name: str
    stimulus: list[StimulusStep] = Field(default_factory=list)
    expect: list[Expectation] = Field(min_length=1)


class TaskBrief(BaseModel):
    """A task without acceptance tests yet: the agent drafts them (step 0)."""

    task_id: str
    description: str
    signals: list[str] = Field(min_length=1)


class TaskSpec(TaskBrief):
    tests: list[AcceptanceTest] = Field(min_length=1)

    def brief(self) -> TaskBrief:
        return TaskBrief(task_id=self.task_id, description=self.description, signals=self.signals)


def load_task_spec(path: str | Path) -> TaskSpec:
    """Load and validate one task spec file."""
    with Path(path).open("r", encoding="utf-8") as fh:
        return TaskSpec.model_validate(yaml.safe_load(fh))


def load_task_file(path: str | Path) -> TaskSpec | TaskBrief:
    """A full spec, or a brief when ``tests`` is missing or empty."""
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if isinstance(data, dict) and not data.get("tests"):
        data = {k: v for k, v in data.items() if k != "tests"}
        return TaskBrief.model_validate(data)
    return TaskSpec.model_validate(data)


def check_brief_against_io_map(brief: TaskBrief, io_map: IoMap) -> None:
    """Raise ValueError if the brief names signals outside the I/O map."""
    unknown = [name for name in brief.signals if name not in io_map.signals]
    if unknown:
        raise ValueError(
            "task does not match io_map: " + "; ".join(f"unknown signal {n!r}" for n in unknown)
        )


def check_against_io_map(spec: TaskSpec, io_map: IoMap) -> None:
    """Raise ValueError if the spec references signals outside the I/O map."""
    problems: list[str] = []
    for name in spec.signals:
        if name not in io_map.signals:
            problems.append(f"unknown signal {name!r}")
    declared = set(spec.signals)
    for test in spec.tests:
        for step in test.stimulus:
            target = step.press if step.press is not None else step.release
            if target is None or target not in declared:
                problems.append(f"test {test.name!r}: stimulus on undeclared signal {target!r}")
            elif io_map.signals[target].direction != "input":
                problems.append(f"test {test.name!r}: stimulus on non-input {target!r}")
        for exp in test.expect:
            if exp.signal not in declared:
                problems.append(f"test {test.name!r}: expect on undeclared {exp.signal!r}")
            elif exp.signal in io_map.signals and not _value_fits(exp.value, io_map, exp.signal):
                problems.append(
                    f"test {test.name!r}: value {exp.value!r} does not fit {exp.signal!r}"
                )
    if problems:
        raise ValueError("task spec does not match io_map: " + "; ".join(problems))


def _value_fits(value: bool | int | float, io_map: IoMap, signal: str) -> bool:
    if io_map.signals[signal].type == "BOOL":
        return isinstance(value, bool)
    return isinstance(value, (int, float)) and not isinstance(value, bool)
