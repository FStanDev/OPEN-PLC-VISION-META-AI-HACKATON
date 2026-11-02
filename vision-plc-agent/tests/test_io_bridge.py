import pytest

from agent.io_map import IoMap, load_io_map
from pi.gpio import FakeGpio
from pi.gpio_plugin.io_bridge import (
    BridgeConfigError,
    BufferError,
    IecAddress,
    IoBridge,
    parse_iec,
)
from pi.pca9685 import FakePwm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("%IX0.0", IecAddress("I", "X", 0, 0)),
        ("%I0.1", IecAddress("I", "X", 0, 1)),
        ("%QX0.2", IecAddress("Q", "X", 0, 2)),
        ("%QW0", IecAddress("Q", "W", 0, None)),
        ("%IW3", IecAddress("I", "W", 3, None)),
    ],
)
def test_parse_iec_valid(text: str, expected: IecAddress) -> None:
    assert parse_iec(text) == expected


@pytest.mark.parametrize("text", ["%IX0", "%QB0.0", "%ID0", "%MW0", "QX0.0", "%QX0.x"])
def test_parse_iec_invalid(text: str) -> None:
    with pytest.raises(BridgeConfigError):
        parse_iec(text)


class FakeBuffers:
    """Duck-typed SafeBufferAccess over dicts, with error injection.

    Success status is ``"Success"`` (not ``""``): that is the runtime's real
    ``(value, status)`` convention (see ``shared/buffer_accessor.py``).
    """

    def __init__(self) -> None:
        self.bool_inputs: dict[tuple[int, int], bool] = {}
        self.bool_outputs: dict[tuple[int, int], bool] = {}
        self.int_outputs: dict[int, int] = {}
        self.fail: str | None = None

    def write_bool_input(
        self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True
    ) -> tuple[bool, str]:
        if self.fail:
            return False, self.fail
        self.bool_inputs[(buffer_idx, bit_idx)] = value
        return True, "Success"

    def read_bool_output(
        self, buffer_idx: int, bit_idx: int, thread_safe: bool = True
    ) -> tuple[bool, str]:
        if self.fail:
            return False, self.fail
        return self.bool_outputs.get((buffer_idx, bit_idx), False), "Success"

    def read_int_output(self, buffer_idx: int, thread_safe: bool = True) -> tuple[int, str]:
        if self.fail:
            return 0, self.fail
        return self.int_outputs.get(buffer_idx, 0), "Success"


def _bridge(gpio: FakeGpio, pwm: FakePwm | None = None) -> IoBridge:
    return IoBridge(load_io_map(), gpio, FakePwm() if pwm is None else pwm)


def test_sync_inputs_respects_active_low() -> None:
    gpio = FakeGpio()
    bridge = _bridge(gpio)
    gpio.set_level(5, True)  # start idle: pull-up HIGH
    gpio.set_level(6, False)  # stop pressed: LOW
    buffers = FakeBuffers()
    bridge.sync(buffers)
    assert buffers.bool_inputs == {(0, 0): False, (0, 1): True}


def test_sync_outputs_drive_lamps() -> None:
    gpio = FakeGpio()
    bridge = _bridge(gpio)
    buffers = FakeBuffers()
    buffers.bool_outputs = {(0, 0): True, (0, 1): False, (0, 2): True}
    bridge.sync(buffers)
    assert gpio.levels[17] is True
    assert gpio.levels[27] is False
    assert gpio.levels[22] is True


def test_sync_clamps_servo_and_treats_int_as_signed() -> None:
    gpio = FakeGpio()
    pwm = FakePwm()
    bridge = _bridge(gpio, pwm)
    buffers = FakeBuffers()
    buffers.int_outputs[0] = 90
    bridge.sync(buffers)
    assert pwm.angles[2] == 90  # io_map arm_angle channel
    buffers.int_outputs[0] = 1000
    bridge.sync(buffers)
    assert pwm.angles[2] == 180
    buffers.int_outputs[0] = 0xFFFF  # ST INT -1 on the wire
    bridge.sync(buffers)
    assert pwm.angles[2] == 0


def test_sync_propagates_buffer_errors() -> None:
    bridge = _bridge(FakeGpio())
    buffers = FakeBuffers()
    buffers.fail = "boom"
    with pytest.raises(BufferError, match="boom"):
        bridge.sync(buffers)


def test_safe_state_turns_everything_off() -> None:
    gpio = FakeGpio()
    pwm = FakePwm()
    bridge = _bridge(gpio, pwm)
    buffers = FakeBuffers()
    buffers.bool_outputs = {(0, 0): True, (0, 1): True, (0, 2): True}
    buffers.int_outputs[0] = 90
    bridge.sync(buffers)
    bridge.apply_safe_state()
    assert gpio.levels[17] is False
    assert gpio.levels[27] is False
    assert gpio.levels[22] is False
    assert pwm.angles[2] == 0


def test_optional_servo_skipped_without_pwm() -> None:
    bridge = IoBridge(load_io_map(), FakeGpio(), None)
    assert bridge.skipped == ["arm_angle"]
    assert len(bridge.bindings) == 5
    bridge.sync(FakeBuffers())  # must not raise


def _custom_map(signals: dict) -> IoMap:
    return IoMap.model_validate(
        {
            "version": 1,
            "signals": signals,
            "stimulus": {},
            "safe_state": {},
        }
    )


def test_direction_must_match_iec_area() -> None:
    io_map = _custom_map(
        {
            "oops": {
                "direction": "output",
                "type": "BOOL",
                "iec": "%IX0.5",
                "modbus": {"table": "coil", "address": 5},
                "gpio": {"bcm": 17},
            }
        }
    )
    with pytest.raises(BridgeConfigError, match="disagrees"):
        IoBridge(io_map, FakeGpio())


def test_required_servo_without_pwm_raises() -> None:
    io_map = _custom_map(
        {
            "arm": {
                "direction": "output",
                "type": "INT",
                "iec": "%QW0",
                "modbus": {"table": "holding_register", "address": 0},
                "pca9685": {"channel": 0},
            }
        }
    )
    with pytest.raises(BridgeConfigError, match="PWM backend"):
        IoBridge(io_map, FakeGpio(), None)
