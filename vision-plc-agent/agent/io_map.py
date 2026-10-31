"""Load and validate ``io_map.yaml`` — the I/O contract (see ``docs/SPEC.md``)."""

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, model_validator


class ModbusRef(BaseModel):
    table: Literal["discrete_input", "coil", "holding_register", "input_register"]
    address: int = Field(ge=0)


class GpioRef(BaseModel):
    bcm: int
    pull: Literal["up", "down"] | None = None
    active_low: bool = False


class Pca9685Ref(BaseModel):
    channel: int = Field(ge=0, le=15)


class VisionRef(BaseModel):
    prompt: str
    measure: Literal["brightness", "angle"]


class Signal(BaseModel):
    direction: Literal["input", "output"]
    type: Literal["BOOL", "INT"]
    iec: str
    modbus: ModbusRef
    gpio: GpioRef | None = None
    pca9685: Pca9685Ref | None = None
    vision: VisionRef | None = None
    range: tuple[int, int] | None = None
    optional: bool = False


class StimulusPin(BaseModel):
    bcm: int
    drives: str


class IoMap(BaseModel):
    version: int
    signals: dict[str, Signal]
    stimulus: dict[str, StimulusPin]
    safe_state: dict[str, bool | int]

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        for name, pin in self.stimulus.items():
            if pin.drives not in self.signals:
                raise ValueError(f"stimulus pin {name!r} drives unknown signal {pin.drives!r}")
        for name in self.safe_state:
            if name not in self.signals:
                raise ValueError(f"safe_state lists unknown signal {name!r}")
        return self


def default_io_map_path() -> Path:
    return Path(__file__).resolve().parent.parent / "io_map.yaml"


def load_io_map(path: str | Path | None = None) -> IoMap:
    """Load and validate the I/O map (defaults to the project-root file)."""
    resolved = Path(path) if path is not None else default_io_map_path()
    with resolved.open("r", encoding="utf-8") as fh:
        return IoMap.model_validate(yaml.safe_load(fh))
