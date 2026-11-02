"""ModbusTagWriter tests with an injected stub client (no network)."""

import pytest

from agent.io_map import load_io_map
from agent.tag_writer import FakeTagWriter, ModbusTagWriter, TagWriteError


class FakeModbusResult:
    def __init__(self, error: bool = False) -> None:
        self._error = error

    def isError(self) -> bool:  # pymodbus spelling
        return self._error


class FakeModbusClient:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.fail_connect = False
        self.calls: list[tuple[str, int, list[object], int]] = []
        self.error = False
        self.closed = False

    def connect(self) -> None:
        self.connect_calls += 1
        if not self.fail_connect:
            self.connected = True

    def write_coils(
        self, address: int, values: list[bool], *, device_id: int = 1
    ) -> FakeModbusResult:
        self.calls.append(("write_coils", address, list(values), device_id))
        return FakeModbusResult(self.error)

    def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> FakeModbusResult:
        self.calls.append(("write_registers", address, list(values), device_id))
        return FakeModbusResult(self.error)

    def close(self) -> None:
        self.connected = False
        self.closed = True


def test_fake_writer_records_and_reads_back() -> None:
    writer = FakeTagWriter()
    writer.write("lamp_red", True)
    writer.write("arm_angle", 90)
    assert writer.writes == [("lamp_red", True), ("arm_angle", 90)]
    assert writer.read("lamp_red") is True


def test_coil_write() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    writer.write("lamp_red", True)
    assert stub.calls == [("write_coils", 0, [True], 1)]
    assert stub.connect_calls == 1


def test_holding_register_write() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    writer.write("arm_angle", 90)
    assert stub.calls == [("write_registers", 0, [90], 1)]


def test_register_write_casts_float_to_int() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    writer.write("arm_angle", 90.0)
    assert stub.calls == [("write_registers", 0, [90], 1)]


def test_refuses_input_signal() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    with pytest.raises(TagWriteError, match="input signal"):
        writer.write("start_btn", True)


def test_rejects_wrong_value_type() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    with pytest.raises(TagWriteError, match="needs a bool"):
        writer.write("lamp_red", 1)
    with pytest.raises(TagWriteError, match="needs a number"):
        writer.write("arm_angle", True)


def test_unknown_signal_raises_key_error() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        writer.write("nope", True)


def test_connect_failure_raises() -> None:
    stub = FakeModbusClient()
    stub.fail_connect = True
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    with pytest.raises(TagWriteError, match="connect failed"):
        writer.write("lamp_red", True)


def test_error_result_raises() -> None:
    stub = FakeModbusClient()
    stub.error = True
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    with pytest.raises(TagWriteError, match="lamp_red"):
        writer.write("lamp_red", True)


def test_close_closes_client() -> None:
    stub = FakeModbusClient()
    writer = ModbusTagWriter("pi", 502, load_io_map(), client=stub)  # type: ignore[arg-type]
    writer.write("lamp_red", False)
    writer.close()
    assert stub.closed is True
