"""strucpp wrapper tests with a fake binary (no STruC++ needed)."""

from pathlib import Path

import pytest

from agent.strucpp import StrucppNotFoundError, compile_st, run_st_tests

FAKE_STRUCPP = """#!/usr/bin/env python3
import os
import sys

print("args:", " ".join(sys.argv[1:]))
print("fake-stderr", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_STRUCPP_EXIT", "0")))
"""


@pytest.fixture
def fake_strucpp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "strucpp"
    exe.write_text(FAKE_STRUCPP, encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_STRUCPP_EXIT", "0")
    (tmp_path / "main.st").write_text("PROGRAM Main END_PROGRAM\n", encoding="utf-8")
    (tmp_path / "tests.st").write_text("TEST t END_TEST\n", encoding="utf-8")
    return exe


def test_compile_passes_main_and_output(
    fake_strucpp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = compile_st("main.st", "build/main.cpp", strucpp=str(fake_strucpp))
    assert result.ok and result.returncode == 0
    assert result.stdout.strip() == "args: main.st -o build/main.cpp"
    assert result.stderr.strip() == "fake-stderr"
    assert (tmp_path / "build").is_dir()


def test_compile_reports_failure(
    fake_strucpp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_STRUCPP_EXIT", "2")
    result = compile_st("main.st", "build/main.cpp", strucpp=str(fake_strucpp))
    assert (result.ok, result.returncode) == (False, 2)


def test_run_tests_passes_test_flag(
    fake_strucpp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_st_tests("main.st", "tests.st", strucpp=str(fake_strucpp))
    assert result.ok
    assert result.stdout.strip() == "args: main.st --test tests.st"


def test_missing_binary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.st").write_text("PROGRAM Main END_PROGRAM\n", encoding="utf-8")
    with pytest.raises(StrucppNotFoundError, match="not found"):
        compile_st("main.st", "build/main.cpp", strucpp=str(tmp_path / "nope"))


def test_missing_input_files_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="main.st"):
        compile_st("main.st", "out.cpp", strucpp="definitely-not-a-binary")
    Path("main.st").write_text("PROGRAM Main END_PROGRAM\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="tests.st"):
        run_st_tests("main.st", "tests.st", strucpp="definitely-not-a-binary")
