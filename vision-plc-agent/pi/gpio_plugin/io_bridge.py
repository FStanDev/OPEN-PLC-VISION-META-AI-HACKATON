"""Sync between OpenPLC I/O buffers and bench hardware.

``IoBridge`` is the testable core of the GPIO plugin: it is built from
``io_map.yaml`` (via :mod:`agent.io_map`), talks to the PLC through the
``SafeBufferAccess`` method subset below (duck-typed, so tests inject a
fake), and talks to hardware through :mod:`pi.gpio` / :mod:`pi.pca9685`
backends. It imports nothing Pi-specific itself.
"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from agent.io_map import IoMap
from pi.gpio import GpioBackend
from pi.pca9685 import PwmBackend

_IEC_RE = re.compile(r"%([IQ])([XBWDL]?)(\d+)(?:\.(\d+))?\Z")


class BridgeConfigError(ValueError):
    """The I/O map cannot be realised on this hardware."""


class BufferError(RuntimeError):
    """A SafeBufferAccess call reported failure."""


@dataclass(frozen=True)
class IecAddress:
    area: Literal["I", "Q"]  # input image / output image
    size: Literal["X", "B", "W", "D", "L"]  # bit, byte, word, dword, lword
    byte: int
    bit: int | None


def parse_iec(iec: str) -> IecAddress:
    """Parse ``%IX0.0`` / ``%QW0`` style addresses (``%I0.0`` == ``%IX0.0``)."""
    match = _IEC_RE.match(iec)
    if match is None:
        raise BridgeConfigError(f"bad IEC address: {iec!r}")
    area, size, byte_s, bit_s = match.groups()
    size = size or "X"
    if size == "X" and bit_s is None:
        raise BridgeConfigError(f"bit address needs .bit: {iec!r}")
    if size != "X" and bit_s is not None:
        raise BridgeConfigError(f"byte address takes no .bit: {iec!r}")
    if size not in ("X", "W"):
        raise BridgeConfigError(f"unsupported size {size!r} in {iec!r} (need X or W)")
    return IecAddress(area, size, int(byte_s), int(bit_s) if bit_s is not None else None)  # type: ignore[arg-type]


class BufferAccess(Protocol):
    """The SafeBufferAccess subset the bridge uses.

    All calls return ``(value, status)`` with ``status == "Success"`` on
    success (the runtime's convention, see ``shared/buffer_accessor.py``).
    """

    def write_bool_input(
        self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True
    ) -> tuple[bool, str]: ...
    def read_bool_output(
        self, buffer_idx: int, bit_idx: int, thread_safe: bool = True
    ) -> tuple[bool, str]: ...
    def read_int_output(self, buffer_idx: int, thread_safe: bool = True) -> tuple[int, str]: ...


@dataclass(frozen=True)
class _Binding:
    name: str
    kind: Literal["input_bool", "output_bool", "output_int"]
    byte: int
    bit: int
    bcm: int | None = None
    active_low: bool = False
    pwm_channel: int | None = None
    range: tuple[int, int] = (0, 180)


class IoBridge:
    """One sync step between PLC buffers and hardware (see module docstring)."""

    def __init__(self, io_map: IoMap, gpio: GpioBackend, pwm: PwmBackend | None = None) -> None:
        self._gpio = gpio
        self._pwm = pwm
        self._io_map = io_map
        self.bindings: list[_Binding] = []
        self.skipped: list[str] = []
        for name, signal in io_map.signals.items():
            addr = parse_iec(signal.iec)
            if (addr.area == "I") != (signal.direction == "input"):
                raise BridgeConfigError(
                    f"{name}: direction {signal.direction!r} disagrees with {signal.iec!r}"
                )
            if addr.size == "X" and signal.type != "BOOL":
                raise BridgeConfigError(f"{name}: {signal.iec!r} needs type BOOL")
            if addr.size == "W" and signal.type != "INT":
                raise BridgeConfigError(f"{name}: {signal.iec!r} needs type INT")
            assert addr.bit is not None or addr.size == "W"
            if signal.direction == "input":
                if signal.gpio is None:
                    raise BridgeConfigError(f"{name}: input needs a gpio pin")
                gpio.setup_input(signal.gpio.bcm, pull_up=signal.gpio.pull != "down")
                self.bindings.append(
                    _Binding(
                        name,
                        "input_bool",
                        addr.byte,
                        addr.bit or 0,
                        bcm=signal.gpio.bcm,
                        active_low=signal.gpio.active_low,
                    )
                )
            elif signal.gpio is not None:
                gpio.setup_output(signal.gpio.bcm)
                self.bindings.append(
                    _Binding(name, "output_bool", addr.byte, addr.bit or 0, bcm=signal.gpio.bcm)
                )
            elif signal.pca9685 is not None:
                if pwm is None:
                    if not signal.optional:
                        raise BridgeConfigError(f"{name}: servo needs a PWM backend")
                    self.skipped.append(name)
                    continue
                self.bindings.append(
                    _Binding(
                        name,
                        "output_int",
                        addr.byte,
                        0,
                        pwm_channel=signal.pca9685.channel,
                        range=signal.range or (0, 180),
                    )
                )
            else:
                raise BridgeConfigError(f"{name}: output needs gpio or pca9685")

    def sync(self, buffers: BufferAccess) -> None:
        """Copy physical inputs into the image, image outputs to hardware."""
        for binding in self.bindings:
            if binding.kind == "input_bool":
                assert binding.bcm is not None
                level = self._gpio.read(binding.bcm)
                logical = (not level) if binding.active_low else level
                ok, status = buffers.write_bool_input(binding.byte, binding.bit, logical)
                if not ok:
                    raise BufferError(f"write {binding.name}: {status}")
            elif binding.kind == "output_bool":
                assert binding.bcm is not None
                value, status = buffers.read_bool_output(binding.byte, binding.bit)
                if status != "Success":
                    raise BufferError(f"read {binding.name}: {status}")
                self._gpio.write(binding.bcm, value)
            else:
                assert binding.pwm_channel is not None and self._pwm is not None
                raw, status = buffers.read_int_output(binding.byte)
                if status != "Success":
                    raise BufferError(f"read {binding.name}: {status}")
                signed = raw - 0x10000 if raw >= 0x8000 else raw  # ST INT is signed
                lo, hi = binding.range
                self._pwm.set_angle(binding.pwm_channel, max(lo, min(hi, signed)))

    def close(self) -> None:
        """Release the GPIO and PWM backends."""
        self._gpio.close()
        if self._pwm is not None:
            self._pwm.close()

    def apply_safe_state(self) -> None:
        """Drive every bound output to ``io_map.safe_state`` (PLC/plugin stop)."""
        for binding in self.bindings:
            if binding.kind == "output_bool":
                assert binding.bcm is not None
                safe = bool(self._io_map.safe_state.get(binding.name, False))
                self._gpio.write(binding.bcm, safe)
            elif binding.kind == "output_int":
                assert binding.pwm_channel is not None and self._pwm is not None
                self._pwm.set_angle(
                    binding.pwm_channel, int(self._io_map.safe_state.get(binding.name, 0))
                )
