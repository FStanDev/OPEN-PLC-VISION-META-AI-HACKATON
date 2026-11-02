"""PLC tag writer over Modbus TCP + fake (safe-state forcing, SPEC step 10).

The GPIO plugin cannot see PLC run state, so stopping the PLC via the API
leaves last lamp values on the pins until the agent forces
``io_map.safe_state`` over Modbus after stop (see pi/gpio_plugin/README.md).
"""

from abc import ABC, abstractmethod

from pymodbus.client import ModbusTcpClient

from agent.io_map import IoMap
from agent.tag_reader import TagValue


class TagWriteError(IOError):
    """Modbus transport or mapping error writing a tag."""


class TagWriter(ABC):
    @abstractmethod
    def write(self, signal: str, value: TagValue) -> None:
        """Write one I/O-map output signal by name."""


class FakeTagWriter(TagWriter):
    """Dict-backed fake recording writes; reads back what was written."""

    def __init__(self, tags: dict[str, TagValue] | None = None) -> None:
        self._tags = dict(tags or {})
        self.writes: list[tuple[str, TagValue]] = []

    def write(self, signal: str, value: TagValue) -> None:
        self.writes.append((signal, value))
        self._tags[signal] = value

    def read(self, signal: str) -> TagValue:
        return self._tags[signal]


class ModbusTagWriter(TagWriter):
    """Real writer over Modbus TCP (coils and holding registers only).

    Args:
        host: Pi IP address or resolvable hostname.
        port: Modbus TCP port (check the Editor project; often 502).
        io_map: signal → table/address mapping.
        device_id: Modbus unit id.
        client: injectable ModbusTcpClient (tests use a stub).
    """

    def __init__(
        self,
        host: str,
        port: int,
        io_map: IoMap,
        *,
        device_id: int = 1,
        client: ModbusTcpClient | None = None,
    ) -> None:
        self._io_map = io_map
        self._device_id = device_id
        self._client = client if client is not None else ModbusTcpClient(host, port=port)

    def write(self, signal: str, value: TagValue) -> None:
        spec = self._io_map.signals[signal]  # KeyError on unknown signals, like the fake
        if spec.direction != "output":
            raise TagWriteError(f"refusing to write input signal {signal!r}")
        try:
            if not self._client.connected:
                self._client.connect()
                if not self._client.connected:
                    raise TagWriteError(f"modbus connect failed writing {signal!r}")
            table, address = spec.modbus.table, spec.modbus.address
            if table == "coil":
                if not isinstance(value, bool):
                    raise TagWriteError(f"coil {signal!r} needs a bool, got {value!r}")
                result = self._client.write_coils(address, [value], device_id=self._device_id)
            elif table == "holding_register":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TagWriteError(f"register {signal!r} needs a number, got {value!r}")
                result = self._client.write_registers(
                    address, [int(value)], device_id=self._device_id
                )
            else:
                raise TagWriteError(f"unsupported modbus table {table!r} for {signal!r}")
        except TagWriteError:
            raise
        except Exception as exc:
            raise TagWriteError(f"modbus write of {signal!r} failed: {exc}") from exc
        if result.isError():
            raise TagWriteError(f"modbus write of {signal!r} failed: {result!r}")

    def close(self) -> None:
        self._client.close()
