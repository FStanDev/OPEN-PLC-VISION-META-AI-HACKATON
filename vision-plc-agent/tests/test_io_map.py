from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.io_map import IoMap, load_io_map

ROOT = Path(__file__).resolve().parent.parent


def test_loads_project_io_map() -> None:
    io_map = load_io_map(ROOT / "io_map.yaml")
    assert io_map.version == 1
    start = io_map.signals["start_btn"]
    assert (start.iec, start.modbus.table, start.modbus.address) == ("%IX0.0", "discrete_input", 0)
    assert start.gpio is not None and start.gpio.bcm == 5 and start.gpio.active_low
    assert io_map.stimulus["stim_start"].drives == "start_btn"
    assert io_map.safe_state == {
        "lamp_red": False,
        "lamp_green": False,
        "lamp_yellow": False,
        "arm_angle": 0,
    }


def test_servo_signal_is_optional_with_pca9685() -> None:
    arm = load_io_map(ROOT / "io_map.yaml").signals["arm_angle"]
    assert arm.optional
    assert arm.range == (0, 180)
    assert arm.pca9685 is not None and arm.pca9685.channel == 1


def test_default_path_is_project_root() -> None:
    assert load_io_map().version == 1


def test_stimulus_drives_must_be_known_signals() -> None:
    dump = load_io_map(ROOT / "io_map.yaml").model_dump()
    dump["stimulus"]["stim_start"]["drives"] = "nope"
    with pytest.raises(ValidationError, match="unknown signal"):
        IoMap.model_validate(dump)


def test_safe_state_must_be_known_signals() -> None:
    dump = load_io_map(ROOT / "io_map.yaml").model_dump()
    dump["safe_state"]["nope"] = False
    with pytest.raises(ValidationError, match="unknown signal"):
        IoMap.model_validate(dump)
