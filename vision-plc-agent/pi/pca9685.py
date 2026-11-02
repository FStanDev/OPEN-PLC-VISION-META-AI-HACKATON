"""Minimal PCA9685 servo driver over I2C (Pi only, servo optional).

Direct register pokes through an injectable bus object (``smbus2.SMBus`` on
the Pi, a fake in tests) — no heavyweight Adafruit stack needed for one
channel. Standard 50 Hz, 1.0–2.0 ms pulse for 0–180 degrees.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from time import sleep
from typing import Protocol

I2C_BUS = 1
I2C_ADDRESS = 0x40

_MODE1 = 0x00
_PRESCALE = 0xFE
_LED0_ON_L = 0x06

_MODE1_RESTART = 0x80
_MODE1_SLEEP = 0x10


class I2cBus(Protocol):
    def write_byte_data(self, address: int, register: int, value: int) -> None: ...
    def read_byte_data(self, address: int, register: int) -> int: ...
    def close(self) -> None: ...


class PwmBackend(ABC):
    @abstractmethod
    def set_angle(self, channel: int, degrees: int) -> None:
        """Drive ``channel`` to ``degrees`` (clamped to 0–180)."""

    @abstractmethod
    def close(self) -> None:
        """Release the bus."""


class FakePwm(PwmBackend):
    """In-memory PWM: records the last angle per channel."""

    def __init__(self) -> None:
        self.angles: dict[int, int] = {}

    def set_angle(self, channel: int, degrees: int) -> None:
        self.angles[channel] = max(0, min(180, degrees))

    def close(self) -> None:
        pass


class Pca9685(PwmBackend):
    """Real PCA9685 driver.

    Args:
        bus_number: I2C bus (default 1; check ``i2cdetect -l`` on the Pi).
        address: chip address (default 0x40).
        bus_factory: injectable for tests; defaults to ``smbus2.SMBus``.
    """

    def __init__(
        self,
        bus_number: int = I2C_BUS,
        address: int = I2C_ADDRESS,
        bus_factory: Callable[[int], I2cBus] | None = None,
    ) -> None:
        if bus_factory is None:
            try:
                import smbus2
            except ImportError as exc:
                raise RuntimeError(
                    "smbus2 is not installed; on the Pi run: "
                    "pip install -r pi/gpio_plugin/requirements.txt"
                ) from exc
            bus_factory = smbus2.SMBus
        self._address = address
        self._bus = bus_factory(bus_number)
        self._configure()

    def set_angle(self, channel: int, degrees: int) -> None:
        if not 0 <= channel <= 15:
            raise ValueError(f"PCA9685 channel out of range: {channel}")
        clamped = max(0, min(180, degrees))
        pulse_us = 1000.0 + (1000.0 * clamped / 180.0)  # 1.0–2.0 ms
        ticks = round(pulse_us * 4096 / 20_000)  # 50 Hz period
        base = _LED0_ON_L + 4 * channel
        self._bus.write_byte_data(self._address, base, 0)
        self._bus.write_byte_data(self._address, base + 1, 0)
        self._bus.write_byte_data(self._address, base + 2, ticks & 0xFF)
        self._bus.write_byte_data(self._address, base + 3, ticks >> 8)

    def close(self) -> None:
        self._bus.close()

    def _configure(self) -> None:
        prescale = round(25_000_000 / (4096 * 50)) - 1  # 50 Hz
        mode1 = self._bus.read_byte_data(self._address, _MODE1)
        self._bus.write_byte_data(self._address, _MODE1, (mode1 & ~_MODE1_RESTART) | _MODE1_SLEEP)
        self._bus.write_byte_data(self._address, _PRESCALE, prescale)
        # Clear SLEEP explicitly (never reuse the entry snapshot: a SLEEP bit
        # latched by an earlier session would otherwise be resurrected here).
        awake = mode1 & ~_MODE1_SLEEP
        self._bus.write_byte_data(self._address, _MODE1, awake)
        sleep(0.005)
        self._bus.write_byte_data(self._address, _MODE1, awake | _MODE1_RESTART)
        if self._bus.read_byte_data(self._address, _MODE1) & _MODE1_SLEEP:
            raise RuntimeError("PCA9685 would not leave SLEEP mode; outputs stay dark")
