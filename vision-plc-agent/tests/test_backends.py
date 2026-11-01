import pytest

from pi.gpio import FakeGpio
from pi.pca9685 import Pca9685


def test_fake_gpio_input_defaults_to_pull_level() -> None:
    gpio = FakeGpio()
    gpio.setup_input(5, pull_up=True)
    gpio.setup_input(6, pull_up=False)
    assert gpio.read(5) is True
    assert gpio.read(6) is False


def test_fake_gpio_write_records_and_misuse_raises() -> None:
    gpio = FakeGpio()
    gpio.setup_output(17)
    gpio.write(17, True)
    assert gpio.writes == [(17, True)]
    with pytest.raises(RuntimeError):
        gpio.write(5, True)  # never set up
    with pytest.raises(RuntimeError):
        gpio.read(17)  # an output


def test_fake_gpio_high_impedance_returns_to_input() -> None:
    gpio = FakeGpio()
    gpio.setup_output(26)
    gpio.write(26, False)
    gpio.set_high_impedance(26)
    assert gpio.modes[26] == "input"
    assert gpio.read(26) is False  # last driven level, now floating


class FakeBus:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, int]] = []
        self.regs = {0x00: 0x00}

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.writes.append((address, register, value))
        self.regs[register] = value

    def read_byte_data(self, address: int, register: int) -> int:
        return self.regs.get(register, 0)

    def close(self) -> None:
        pass


def _pwm(bus: FakeBus) -> Pca9685:
    return Pca9685(bus_factory=lambda _n: bus)


def test_pca9685_configures_50hz() -> None:
    bus = FakeBus()
    _pwm(bus)
    assert (0x40, 0x00, 0x10) in bus.writes  # sleep
    assert (0x40, 0xFE, 121) in bus.writes  # prescale for 50 Hz
    assert bus.writes[-1] == (0x40, 0x00, 0x80)  # restart


def test_pca9685_angle_to_pulse() -> None:
    bus = FakeBus()
    pwm = _pwm(bus)
    pwm.set_angle(0, 0)
    assert bus.writes[-2:] == [(0x40, 0x08, 205), (0x40, 0x09, 0)]  # 1.0 ms
    pwm.set_angle(0, 180)
    assert bus.writes[-2:] == [(0x40, 0x08, 154), (0x40, 0x09, 1)]  # 2.0 ms
    pwm.set_angle(0, 90)
    assert bus.writes[-2:] == [(0x40, 0x08, 51), (0x40, 0x09, 1)]  # 1.5 ms


def test_pca9685_clamps_and_validates_channel() -> None:
    bus = FakeBus()
    pwm = _pwm(bus)
    pwm.set_angle(0, -5)
    assert bus.writes[-2:] == [(0x40, 0x08, 205), (0x40, 0x09, 0)]
    pwm.set_angle(0, 999)
    assert bus.writes[-2:] == [(0x40, 0x08, 154), (0x40, 0x09, 1)]
    with pytest.raises(ValueError):
        pwm.set_angle(16, 90)
