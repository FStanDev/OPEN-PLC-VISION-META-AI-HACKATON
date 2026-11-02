"""Codegen tests with scripted LLMs and fake strucpp binaries (no API, no ST)."""

import os
from pathlib import Path

import pytest

from agent.codegen import (
    CodegenError,
    ProgramFiles,
    _load_dotenv,
    extract_mermaid,
    extract_program_files,
    generate_with_repairs,
    load_prompt,
    offline_check,
    render_io_table,
)
from agent.io_map import IoMap, load_io_map
from agent.llm_backend import ScriptedLlmBackend
from agent.spec import TaskSpec, check_against_io_map, load_task_spec

ROOT = Path(__file__).resolve().parent.parent

FAKE_PASS = """#!/usr/bin/env python3
import sys
print("ok:", " ".join(sys.argv[1:]))
"""

FAIL_IF_MARKER = """#!/usr/bin/env python3
# Exits 1 when any input .st file contains the marker; else prints ok.
import sys
from pathlib import Path
marker = "SYNTAX_BAD"
for arg in sys.argv[1:]:
    p = Path(arg)
    if p.suffix == ".st" and p.is_file() and marker in p.read_text():
        print(f"error: {marker} in {arg}")
        print("details on stderr", file=sys.stderr)
        sys.exit(1)
print("ok:", " ".join(sys.argv[1:]))
"""

LOG_ARGS = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path
Path(os.environ["CALL_LOG"]).open("a").write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_STRUCPP_EXIT", "0")))
"""

GOOD_CODE = """intro text
```st
PROGRAM Main END_PROGRAM
```
middle ```mermaid
stateDiagram-v2
```
```st
TEST 't' END_TEST
```
"""


def _write_exe(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def spec_and_map() -> tuple[TaskSpec, IoMap]:
    spec = load_task_spec(ROOT / "specs" / "start_lamp_arm.yaml")
    io_map = load_io_map()
    check_against_io_map(spec, io_map)
    return spec, io_map


def test_extract_program_files_in_order_ignoring_other_fences() -> None:
    files = extract_program_files(GOOD_CODE)
    assert files == ProgramFiles(
        main_st="PROGRAM Main END_PROGRAM\n", tests_st="TEST 't' END_TEST\n"
    )


def test_extract_program_files_needs_two_blocks() -> None:
    with pytest.raises(CodegenError, match="need 2"):
        extract_program_files("```st\nonly one\n```\n")


def test_extract_mermaid_from_fence_or_raw() -> None:
    assert extract_mermaid("x\n```mermaid\nstateDiagram-v2\n```\n") == "stateDiagram-v2\n"
    assert extract_mermaid("stateDiagram-v2\n  [*] --> a\n") == "stateDiagram-v2\n  [*] --> a\n"
    with pytest.raises(CodegenError, match="mermaid"):
        extract_mermaid("no diagram here")


def test_render_io_table_only_spec_signals(spec_and_map: tuple[TaskSpec, IoMap]) -> None:
    spec, io_map = spec_and_map
    table = render_io_table(spec, io_map)
    assert "start_btn: BOOL input %IX0.0 (discrete_input 0)" in table
    assert "arm_angle: INT output %QW0 (holding_register 0)" in table
    assert "lamp_green" not in table  # not in this spec


def test_all_prompts_render() -> None:
    plan = load_prompt("plan_v1.txt").substitute(spec_yaml="s", io_table="t", safe_state="z")
    codegen = load_prompt("codegen_v1.txt").substitute(
        spec_yaml="s", io_table="t", diagram="d", fewshot_main="m", fewshot_tests="u"
    )
    repair = load_prompt("repair_v1.txt").substitute(
        round=2,
        max_rounds=3,
        spec_yaml="s",
        io_table="t",
        main_st="m",
        tests_st="u",
        errors="e",
    )
    for rendered in (plan, codegen, repair):
        assert "$" not in rendered
    assert "stateDiagram-v2" in plan
    assert "PROGRAM Main" in codegen and "ADVANCE_TIME" in codegen
    assert "attempt 2 of 3" in repair


def test_missing_prompt_raises() -> None:
    with pytest.raises(CodegenError, match="not found"):
        load_prompt("nope_v9.txt")


def test_offline_check_writes_files_and_passes(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    files = ProgramFiles(main_st="PROGRAM Main END_PROGRAM\n", tests_st="TEST x\n")
    check = offline_check(files, tmp_path / "work", strucpp=str(exe))
    assert check.passed and check.tests is not None and check.tests.ok
    assert (tmp_path / "work" / "main.st").read_text() == files.main_st
    assert (tmp_path / "work" / "tests.st").read_text() == files.tests_st


def test_offline_check_compile_failure_skips_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = _write_exe(tmp_path / "strucpp", LOG_ARGS)
    monkeypatch.setenv("CALL_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_STRUCPP_EXIT", "1")
    files = ProgramFiles(main_st="bad", tests_st="bad")
    check = offline_check(files, tmp_path / "work", strucpp=str(exe))
    assert not check.passed and check.tests is None
    calls = (tmp_path / "calls.log").read_text().strip().splitlines()
    assert len(calls) == 1 and "--test" not in calls[0]


def test_offline_check_reports_test_failure_verbatim(tmp_path: Path) -> None:
    exe = _write_exe(tmp_path / "strucpp", FAIL_IF_MARKER)
    files = ProgramFiles(main_st="PROGRAM Main END_PROGRAM\n", tests_st="SYNTAX_BAD\n")
    check = offline_check(files, tmp_path / "work", strucpp=str(exe))
    assert not check.passed and check.tests is not None and not check.tests.ok
    assert "SYNTAX_BAD" in check.errors_verbatim
    assert "details on stderr" in check.errors_verbatim


def _code_reply(main: str, tests: str) -> str:
    return f"```st\n{main}\n```\n```st\n{tests}\n```\n"


def test_generate_with_repairs_passes_first_try(
    tmp_path: Path, spec_and_map: tuple[TaskSpec, IoMap]
) -> None:
    spec, io_map = spec_and_map
    exe = _write_exe(tmp_path / "strucpp", FAKE_PASS)
    llm = ScriptedLlmBackend(["```mermaid\nstateDiagram-v2\n```", _code_reply("MAIN", "TESTS")])
    report = generate_with_repairs(spec, io_map, llm, workdir=tmp_path / "work", strucpp=str(exe))
    assert report.passed and len(report.checks) == 1
    assert report.files == ProgramFiles(main_st="MAIN\n", tests_st="TESTS\n")
    assert report.diagram == "stateDiagram-v2\n"
    assert [effort for effort, _ in llm.calls] == ["high", "high"]
    assert report.input_tokens > 0
    as_dict = report.to_dict()
    assert as_dict["passed"] is True and as_dict["attempts"] == 1


def test_generate_with_repairs_feeds_verbatim_errors_back(
    tmp_path: Path, spec_and_map: tuple[TaskSpec, IoMap]
) -> None:
    spec, io_map = spec_and_map
    exe = _write_exe(tmp_path / "strucpp", FAIL_IF_MARKER)
    llm = ScriptedLlmBackend(
        [
            "```mermaid\nstateDiagram-v2\n```",
            _code_reply("MAIN", "SYNTAX_BAD"),
            _code_reply("MAIN", "FIXED"),
        ]
    )
    report = generate_with_repairs(spec, io_map, llm, workdir=tmp_path / "work", strucpp=str(exe))
    assert report.passed and len(report.checks) == 2
    assert [effort for effort, _ in llm.calls] == ["high", "high", "low"]
    repair_prompt = llm.calls[2][1]
    assert "SYNTAX_BAD" in repair_prompt and "details on stderr" in repair_prompt
    assert "attempt 2 of 3" in repair_prompt


def test_generate_with_repairs_gives_up_after_max_rounds(
    tmp_path: Path, spec_and_map: tuple[TaskSpec, IoMap]
) -> None:
    spec, io_map = spec_and_map
    exe = _write_exe(tmp_path / "strucpp", FAIL_IF_MARKER)
    llm = ScriptedLlmBackend(
        ["```mermaid\nstateDiagram-v2\n```"] + [_code_reply("MAIN", "SYNTAX_BAD")] * 3
    )
    report = generate_with_repairs(
        spec,
        io_map,
        llm,
        workdir=tmp_path / "work",
        max_rounds=3,
        strucpp=str(exe),
    )
    assert not report.passed and len(report.checks) == 3
    assert len(llm.calls) == 4  # plan + 3 codegen attempts


def test_load_dotenv_strips_inline_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "# comment\n"
        "META_API_BASE_URL=https://api.meta.ai/v1   # confirm against docs\n"
        "QUOTED='a#b'\n"
        "EMPTY=\n"
        "NOEQUALS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for var in ("META_API_BASE_URL", "QUOTED", "EMPTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("META_API_KEY", "keep-me")
    (tmp_path / ".env").write_text(
        (tmp_path / ".env").read_text() + "META_API_KEY=from-file\n", encoding="utf-8"
    )
    _load_dotenv()
    assert os.environ["META_API_BASE_URL"] == "https://api.meta.ai/v1"
    assert os.environ["QUOTED"] == "a#b"
    assert os.environ["EMPTY"] == ""
    assert os.environ["META_API_KEY"] == "keep-me"  # never clobbers real env
    assert "NOEQUALS" not in os.environ


def test_generate_with_repairs_rejects_bad_max_rounds(
    tmp_path: Path, spec_and_map: tuple[TaskSpec, IoMap]
) -> None:
    spec, io_map = spec_and_map
    with pytest.raises(ValueError, match="max_rounds"):
        generate_with_repairs(
            spec,
            io_map,
            ScriptedLlmBackend([]),
            workdir=tmp_path,
            max_rounds=0,
        )


def test_generate_with_repairs_snapshots_each_attempt(
    tmp_path: Path, spec_and_map: tuple[TaskSpec, IoMap]
) -> None:
    spec, io_map = spec_and_map
    exe = _write_exe(tmp_path / "strucpp", FAIL_IF_MARKER)
    llm = ScriptedLlmBackend(
        [
            "```mermaid\nstateDiagram-v2\n```",
            _code_reply("MAIN", "SYNTAX_BAD"),
            _code_reply("MAIN", "FIXED"),
        ]
    )
    report = generate_with_repairs(spec, io_map, llm, workdir=tmp_path / "work", strucpp=str(exe))
    assert report.passed and len(report.checks) == 2
    attempts = tmp_path / "work" / "attempts"
    assert (attempts / "attempt1" / "tests.st").read_text() == "SYNTAX_BAD\n"
    assert (attempts / "attempt2" / "tests.st").read_text() == "FIXED\n"
    assert (attempts / "attempt2" / "main.st").read_text() == "MAIN\n"
