"""ModbusTagReader tests with an injected stub client (no network)."""

from pathlib import Path
from typing import Any

import pytest

from agent.io_map import IoMap, load_io_map
from agent.tag_reader import ModbusTagReader, TagReadError, TagValue

ROOT = Path(__file__).resolve().parent.parent


class FakeModbusResult:
    def __init__(self, values: list[TagValue] | None = None, error: bool = False) -> None:
        self.bits = values or []
        self.registers = values or []
        self._error = error

    def isError(self) -> bool:  # pymodbus spelling

        return self._error


class FakeModbusClient:
    """Stub with the ModbusTcpClient surface ModbusTagReader uses."""

    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.fail_connect = False
        self.calls: list[tuple[str, int, int, int]] = []
        self.results: dict[tuple[str, int], FakeModbusResult] = {}
        self.closed = False

    def connect(self) -> None:
        self.connect_calls += 1
        if not self.fail_connect:
            self.connected = True

    def read_coils(self, address: int, *, count: int = 1, device_id: int = 1) -> FakeModbusResult:
        return self._record("read_coils", address, count, device_id)

    def read_discrete_inputs(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> FakeModbusResult:
        return self._record("read_discrete_inputs", address, count, device_id)

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> FakeModbusResult:
        return self._record("read_holding_registers", address, count, device_id)

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> FakeModbusResult:
        return self._record("read_input_registers", address, count, device_id)

    def _record(self, method: str, address: int, count: int, device_id: int) -> FakeModbusResult:
        self.calls.append((method, address, count, device_id))
        return self.results[(method, address)]

    def close(self) -> None:
        self.connected = False
        self.closed = True


def load_test_map() -> IoMap:
    """Project map plus an input_register signal (none exists on the rig yet)."""
    dump = load_io_map(ROOT / "io_map.yaml").model_dump()
    dump["signals"]["plc_temp"] = {
        "direction": "input",
        "type": "INT",
        "iec": "%IW0",
        "modbus": {"table": "input_register", "address": 0},
    }
    return IoMap.model_validate(dump)


def make_reader(stub: FakeModbusClient, **kwargs: Any) -> ModbusTagReader:
    # stub quacks like ModbusTcpClient
    return ModbusTagReader("pi", 502, load_test_map(), client=stub, **kwargs)


def test_coil_read() -> None:
    stub = FakeModbusClient()
    stub.results[("read_coils", 0)] = FakeModbusResult([True])
    assert make_reader(stub).read("lamp_red") is True
    assert stub.calls == [("read_coils", 0, 1, 1)]
    assert stub.connect_calls == 1


def test_discrete_input_read() -> None:
    stub = FakeModbusClient()
    stub.results[("read_discrete_inputs", 0)] = FakeModbusResult([False])
    assert make_reader(stub).read("start_btn") is False
    assert stub.calls == [("read_discrete_inputs", 0, 1, 1)]


def test_holding_register_read() -> None:
    stub = FakeModbusClient()
    stub.results[("read_holding_registers", 0)] = FakeModbusResult([90])
    assert make_reader(stub).read("arm_angle") == 90
    assert stub.calls == [("read_holding_registers", 0, 1, 1)]


def test_input_register_read() -> None:
    stub = FakeModbusClient()
    stub.results[("read_input_registers", 0)] = FakeModbusResult([21])
    assert make_reader(stub).read("plc_temp") == 21
    assert stub.calls == [("read_input_registers", 0, 1, 1)]


def test_custom_device_id() -> None:
    stub = FakeModbusClient()
    stub.results[("read_coils", 0)] = FakeModbusResult([True])
    make_reader(stub, device_id=3).read("lamp_red")
    assert stub.calls == [("read_coils", 0, 1, 3)]


def test_stays_connected_across_reads() -> None:
    stub = FakeModbusClient()
    stub.results[("read_coils", 0)] = FakeModbusResult([True])
    reader = make_reader(stub)
    reader.read("lamp_red")
    reader.read("lamp_red")
    assert stub.connect_calls == 1


def test_connect_failure_raises() -> None:
    stub = FakeModbusClient()
    stub.fail_connect = True
    with pytest.raises(TagReadError, match="connect failed"):
        make_reader(stub).read("lamp_red")


def test_error_result_raises() -> None:
    stub = FakeModbusClient()
    stub.results[("read_coils", 0)] = FakeModbusResult(error=True)
    with pytest.raises(TagReadError, match="lamp_red"):
        make_reader(stub).read("lamp_red")


def test_unknown_signal_raises_key_error() -> None:
    with pytest.raises(KeyError):
        make_reader(FakeModbusClient()).read("nope")


def test_close_closes_client() -> None:
    stub = FakeModbusClient()
    stub.results[("read_coils", 0)] = FakeModbusResult([True])
    reader = make_reader(stub)
    reader.read("lamp_red")
    reader.close()
    assert stub.closed is True
