"""GPIO abstraction shared by the Pi-side plugin and services.

``GpioBackend`` is the only GPIO API the plugin/services use; ``FakeGpio``
lets the whole stack run on a laptop with no hardware. The real backend
needs ``gpiozero`` (BSD-3-Clause), installed in the Pi venvs only.
"""

from abc import ABC, abstractmethod


class GpioBackend(ABC):
    """Physical pin access. Levels are raw: True == HIGH, False == LOW."""

    @abstractmethod
    def setup_input(self, bcm: int, *, pull_up: bool) -> None:
        """Configure ``bcm`` as input with the internal pull up (or down)."""

    @abstractmethod
    def setup_output(self, bcm: int) -> None:
        """Configure ``bcm`` as output, initially LOW."""

    @abstractmethod
    def read(self, bcm: int) -> bool:
        """Read the physical level of an input pin."""

    @abstractmethod
    def write(self, bcm: int, value: bool) -> None:
        """Drive an output pin HIGH (True) or LOW (False)."""

    @abstractmethod
    def set_high_impedance(self, bcm: int) -> None:
        """Return ``bcm`` to a floating input (lets go of the node)."""

    @abstractmethod
    def close(self) -> None:
        """Release all pins."""


class FakeGpio(GpioBackend):
    """In-memory GPIO: scriptable input levels, recorded output writes."""

    def __init__(self) -> None:
        self.modes: dict[int, str] = {}
        self.levels: dict[int, bool] = {}
        self.writes: list[tuple[int, bool]] = []

    def set_level(self, bcm: int, level: bool) -> None:
        """Script the physical level a test input pin reads back."""
        self.levels[bcm] = level

    def setup_input(self, bcm: int, *, pull_up: bool) -> None:
        self.modes[bcm] = "input"
        self.levels.setdefault(bcm, pull_up)

    def setup_output(self, bcm: int) -> None:
        self.modes[bcm] = "output"
        self.levels[bcm] = False

    def read(self, bcm: int) -> bool:
        if self.modes.get(bcm) != "input":
            raise RuntimeError(f"pin GPIO{bcm} is not an input")
        return self.levels.get(bcm, True)

    def write(self, bcm: int, value: bool) -> None:
        if self.modes.get(bcm) != "output":
            raise RuntimeError(f"pin GPIO{bcm} is not an output")
        self.levels[bcm] = value
        self.writes.append((bcm, value))

    def set_high_impedance(self, bcm: int) -> None:
        self.modes[bcm] = "input"

    def close(self) -> None:
        self.modes.clear()


class GpioZeroBackend(GpioBackend):
    """Real backend on top of gpiozero (Pi only).

    Prefers the lgpio pin factory (works on Pi 5 / Bookworm where RPi.GPIO
    does not) and falls back to gpiozero's default factory choice.
    """

    def __init__(self) -> None:
        try:
            import gpiozero
        except ImportError as exc:
            raise RuntimeError(
                "gpiozero is not installed; on the Pi run: "
                "pip install -r pi/gpio_plugin/requirements.txt"
            ) from exc
        try:
            from gpiozero.pins.lgpio import LGPIOFactory

            gpiozero.Device.pin_factory = LGPIOFactory()
        except Exception:
            pass  # gpiozero picks the best available factory itself
        self._devices: dict[int, object] = {}

    def setup_input(self, bcm: int, *, pull_up: bool) -> None:
        from gpiozero import DigitalInputDevice

        self._close_pin(bcm)
        self._devices[bcm] = DigitalInputDevice(bcm, pull_up=pull_up, bounce_time=0.02)

    def setup_output(self, bcm: int) -> None:
        from gpiozero import DigitalOutputDevice

        self._close_pin(bcm)
        self._devices[bcm] = DigitalOutputDevice(bcm, initial_value=False)

    def read(self, bcm: int) -> bool:
        device = self._devices.get(bcm)
        if device is None:
            raise RuntimeError(f"pin GPIO{bcm} is not set up")
        return bool(device.pin.state)  # type: ignore[union-attr]

    def write(self, bcm: int, value: bool) -> None:
        device = self._devices.get(bcm)
        if device is None:
            raise RuntimeError(f"pin GPIO{bcm} is not set up")
        if value:
            device.on()  # type: ignore[union-attr]
        else:
            device.off()  # type: ignore[union-attr]

    def set_high_impedance(self, bcm: int) -> None:
        from gpiozero import DigitalInputDevice

        self._close_pin(bcm)
        self._devices[bcm] = DigitalInputDevice(bcm, pull_up=None)  # floating input

    def close(self) -> None:
        for bcm in list(self._devices):
            self._close_pin(bcm)

    def _close_pin(self, bcm: int) -> None:
        device = self._devices.pop(bcm, None)
        if device is not None:
            device.close()  # type: ignore[union-attr]
