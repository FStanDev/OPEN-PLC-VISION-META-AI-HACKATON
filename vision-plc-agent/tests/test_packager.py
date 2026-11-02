"""Packager tests with a fake strucpp binary (no STruC++ needed)."""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from agent.packager import (
    GPIO_PLUGIN_CONF,
    PackagerError,
    build_program_st,
    modbus_slave_conf,
    package,
)
from agent.strucpp import StrucppNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_STRUCPP = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

if os.environ.get("FAKE_STRUCPP_EXIT", "0") != "0":
    print("fake compile error", file=sys.stderr)
    sys.exit(int(os.environ["FAKE_STRUCPP_EXIT"]))
out = Path(sys.argv[sys.argv.index("-o") + 1])
out.write_text("// canned\\nclass Configuration_CONFIG0 {};\\n", encoding="utf-8")
out.with_suffix(".hpp").write_text("// canned header\\n", encoding="utf-8")
"""

BODY_ST = "PROGRAM Main\nVAR x : BOOL; END_VAR\nEND_PROGRAM\n"


@pytest.fixture
def fake_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "strucpp"
    exe.write_text(FAKE_STRUCPP, encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_STRUCPP_EXIT", "0")
    include = tmp_path / "include"
    include.mkdir()
    (include / "iec_std_lib.hpp").write_text("// std lib\n", encoding="utf-8")
    (include / "iec_var.hpp").write_text("// var\n", encoding="utf-8")
    (tmp_path / "main.st").write_text(BODY_ST, encoding="utf-8")
    return exe


def test_appends_configuration_with_program_name() -> None:
    program_st = build_program_st(BODY_ST)
    assert "CONFIGURATION Config0" in program_st
    assert "TASK Task0(INTERVAL := T#20ms, PRIORITY := 1);" in program_st
    assert "PROGRAM Inst0 WITH Task0 : Main;" in program_st


def test_uses_first_program_name_and_interval() -> None:
    program_st = build_program_st("PROGRAM Line1\nEND_PROGRAM\n", interval_ms=50)
    assert "PROGRAM Inst0 WITH Task0 : Line1;" in program_st
    assert "T#50ms" in program_st


def test_leaves_existing_configuration_alone() -> None:
    body = BODY_ST + "\nCONFIGURATION Mine\nEND_CONFIGURATION\n"
    assert build_program_st(body) == body


def test_defaults_to_main_without_program() -> None:
    assert "PROGRAM Inst0 WITH Task0 : Main;" in build_program_st("(* empty *)\n")


def test_modbus_conf_schema() -> None:
    conf = json.loads(modbus_slave_conf("1.2.3.4", 5020))
    assert conf["network_configuration"] == {"host": "1.2.3.4", "port": 5020}
    assert conf["buffer_mapping"]["coils"]["qx_bits"] == 8192


def test_gpio_conf_matches_plugin_file() -> None:
    """Uploads overwrite the plugin config; identical bytes keep git clean."""
    repo_file = REPO_ROOT / "pi" / "gpio_plugin" / "gpio_plugin.json"
    assert GPIO_PLUGIN_CONF == repo_file.read_text(encoding="utf-8")


def test_package_layout(fake_toolchain: Path, tmp_path: Path) -> None:
    out = package(
        tmp_path / "main.st",
        tmp_path / "program.zip",
        strucpp=str(fake_toolchain),
        runtime_include=tmp_path / "include",
        modbus_host="0.0.0.0",
        modbus_port=5020,
    )
    assert out == tmp_path / "program.zip"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "program.st" in names
        assert "generated.cpp" in names
        assert "generated.hpp" in names
        assert "generated_debug.cpp" in names
        assert "defines.h" in names
        assert "c_blocks.h" in names
        assert "strucpp_runtime/include/iec_std_lib.hpp" in names
        assert "strucpp_runtime/include/iec_var.hpp" in names
        assert "conf/gpio_plugin.json" in names
        assert "conf/modbus_slave.json" in names

        program_st = zf.read("program.st").decode("utf-8")
        assert "CONFIGURATION Config0" in program_st
        md5 = hashlib.md5(zf.read("program.st")).hexdigest()
        assert zf.read("defines.h").decode("utf-8") == (
            f'#pragma once\n// Program MD5\n#define PROGRAM_MD5 "{md5}"\n'
        )
        assert zf.read("conf/gpio_plugin.json").decode("utf-8") == GPIO_PLUGIN_CONF
        modbus = json.loads(zf.read("conf/modbus_slave.json"))
        assert modbus["network_configuration"] == {"host": "0.0.0.0", "port": 5020}


def test_package_extra_conf(fake_toolchain: Path, tmp_path: Path) -> None:
    package(
        tmp_path / "main.st",
        tmp_path / "program.zip",
        strucpp=str(fake_toolchain),
        runtime_include=tmp_path / "include",
        extra_conf={"opcua.json": "{}"},
    )
    with zipfile.ZipFile(tmp_path / "program.zip") as zf:
        assert "conf/opcua.json" in zf.namelist()


def test_package_strucpp_failure(
    fake_toolchain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STRUCPP_EXIT", "2")
    with pytest.raises(PackagerError, match="fake compile error"):
        package(
            tmp_path / "main.st",
            tmp_path / "program.zip",
            strucpp=str(fake_toolchain),
            runtime_include=tmp_path / "include",
        )


def test_package_missing_headers(fake_toolchain: Path, tmp_path: Path) -> None:
    with pytest.raises(PackagerError, match="runtime headers"):
        package(
            tmp_path / "main.st",
            tmp_path / "program.zip",
            strucpp=str(fake_toolchain),
            runtime_include=tmp_path / "nope",
        )


def test_package_missing_binary(tmp_path: Path) -> None:
    (tmp_path / "main.st").write_text(BODY_ST, encoding="utf-8")
    with pytest.raises(StrucppNotFoundError):
        package(tmp_path / "main.st", tmp_path / "program.zip", strucpp="no-such-strucpp")


def test_package_missing_st(fake_toolchain: Path, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        package(
            tmp_path / "missing.st",
            tmp_path / "program.zip",
            strucpp=str(fake_toolchain),
            runtime_include=tmp_path / "include",
        )


@pytest.mark.skipif(shutil.which("strucpp") is None, reason="needs the STruC++ binary")
def test_package_real_strucpp_configuration(tmp_path: Path) -> None:
    """End to end with the real compiler: the template yields Configuration_CONFIG0."""
    package(
        REPO_ROOT / "specs" / "start_lamp_arm" / "main.st",
        tmp_path / "program.zip",
    )
    with zipfile.ZipFile(tmp_path / "program.zip") as zf:
        generated = zf.read("generated.cpp").decode("utf-8")
        assert "class Configuration_CONFIG0" in zf.read("generated.hpp").decode("utf-8")
        assert "Configuration_CONFIG0::Configuration_CONFIG0()" in generated
        assert 'TaskInstance("TASK0", 20000000LL' in generated
