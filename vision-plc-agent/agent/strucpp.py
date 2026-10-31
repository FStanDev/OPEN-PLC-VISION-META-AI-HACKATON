"""Wrapper around the STruC++ CLI (compile, ST unit tests)."""

from dataclasses import dataclass
from pathlib import Path
from shutil import which
from subprocess import TimeoutExpired, run


class StrucppNotFoundError(FileNotFoundError):
    """The strucpp binary is not on PATH (see docs/SPEC.md prerequisites)."""


@dataclass(frozen=True)
class StrucppResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str


def compile_st(
    main: str | Path, out: str | Path, *, strucpp: str = "strucpp", timeout_s: float = 300.0
) -> StrucppResult:
    """Run ``strucpp main.st -o build/main.cpp``; creates the output directory."""
    _require_file(main)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    return _run([str(main), "-o", str(out)], strucpp=strucpp, timeout_s=timeout_s)


def run_st_tests(
    main: str | Path, tests: str | Path, *, strucpp: str = "strucpp", timeout_s: float = 300.0
) -> StrucppResult:
    """Run ``strucpp main.st --test tests.st``."""
    _require_file(main)
    _require_file(tests)
    return _run([str(main), "--test", str(tests)], strucpp=strucpp, timeout_s=timeout_s)


def _require_file(path: str | Path) -> None:
    if not Path(path).is_file():
        raise FileNotFoundError(f"ST file not found: {path}")


def _resolve(strucpp: str) -> str:
    if which(strucpp) is None and not Path(strucpp).is_file():
        raise StrucppNotFoundError(
            f"strucpp binary not found: {strucpp!r} "
            "(install the STruC++ prebuilt binary, see docs/SPEC.md)"
        )
    return strucpp


def _run(args: list[str], *, strucpp: str, timeout_s: float) -> StrucppResult:
    exe = _resolve(strucpp)
    try:
        completed = run([exe, *args], capture_output=True, text=True, timeout=timeout_s)
    except TimeoutExpired as exc:
        return StrucppResult(ok=False, returncode=-1, stdout="", stderr=f"timed out: {exc}")
    return StrucppResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
