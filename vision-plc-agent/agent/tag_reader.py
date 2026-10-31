"""PLC tag reader over Modbus TCP + fake.

Table mapping per SPEC: coils ↔ %QX, discrete inputs ↔ %IX,
holding registers ↔ %QW, input registers ↔ %IW.
"""

from abc import ABC, abstractmethod

from pymodbus.client import ModbusTcpClient

from agent.io_map import IoMap

TagValue = bool | int | float


class TagReadError(IOError):
    """Modbus transport or mapping error reading a tag."""


class TagReader(ABC):
    @abstractmethod
    def read(self, signal: str) -> TagValue:
        """Read one I/O-map signal by name."""


class FakeTagReader(TagReader):
    """Dict-backed fake; unknown signals raise KeyError."""

    def __init__(self, tags: dict[str, TagValue] | None = None) -> None:
        self._tags = dict(tags or {})

    def read(self, signal: str) -> TagValue:
        return self._tags[signal]

    def write(self, signal: str, value: TagValue) -> None:
        """Script a tag value (test helper)."""
        self._tags[signal] = value


class ModbusTagReader(TagReader):
    """Real reader over Modbus TCP.

    Args:
        host: Pi Tailscale name or address.
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

    def read(self, signal: str) -> TagValue:
        spec = self._io_map.signals[signal]  # KeyError on unknown signals, like the fake
        try:
            if not self._client.connected:
                self._client.connect()
                if not self._client.connected:
                    raise TagReadError(f"modbus connect failed reading {signal!r}")
            table, address = spec.modbus.table, spec.modbus.address
            if table == "coil":
                result = self._client.read_coils(address, count=1, device_id=self._device_id)
                values: list[TagValue] = list(result.bits)
            elif table == "discrete_input":
                result = self._client.read_discrete_inputs(
                    address, count=1, device_id=self._device_id
                )
                values = list(result.bits)
            elif table == "holding_register":
                result = self._client.read_holding_registers(
                    address, count=1, device_id=self._device_id
                )
                values = list(result.registers)
            elif table == "input_register":
                result = self._client.read_input_registers(
                    address, count=1, device_id=self._device_id
                )
                values = list(result.registers)
            else:
                raise TagReadError(f"unsupported modbus table {table!r} for {signal!r}")
        except TagReadError:
            raise
        except Exception as exc:
            raise TagReadError(f"modbus read of {signal!r} failed: {exc}") from exc
        if result.isError() or not values:
            raise TagReadError(f"modbus read of {signal!r} failed: {result!r}")
        return values[0]

    def close(self) -> None:
        self._client.close()
