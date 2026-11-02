"""Muse Spark code generation: Mermaid plan, ST + tests, STruC++ repair loop.

SPEC loop steps 1-3: plan the behaviour, generate one ``PROGRAM Main`` plus
an ST test file, then compile and test offline, feeding errors back to the
model verbatim (at most :data:`MAX_REPAIR_ROUNDS` attempts in total).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

from agent.dotenv import load_dotenv as _load_dotenv
from agent.io_map import IoMap, load_io_map
from agent.llm_backend import LlmBackend, LlmReply, MetaLlmBackend, ScriptedLlmBackend
from agent.spec import TaskSpec, check_against_io_map, load_task_spec
from agent.strucpp import StrucppResult, compile_st, run_st_tests

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FEWSHOT_DIR = Path(__file__).resolve().parent.parent / "specs" / "start_lamp_arm"
PLAN_EFFORT = "high"  # first generation gets full reasoning (SPEC: model usage)
CODEGEN_EFFORT = "high"
REPAIR_EFFORT = "low"  # small fixes stay cheap
MAX_REPAIR_ROUNDS = 3
REPORT_TAIL_CHARS = 1500


class CodegenError(RuntimeError):
    """A model reply was unusable (no code blocks) or a prompt is broken."""


@dataclass(frozen=True)
class ProgramFiles:
    main_st: str
    tests_st: str


@dataclass(frozen=True)
class OfflineCheck:
    """One compile + test pass; ``tests`` is None when compile failed."""

    passed: bool
    compile: StrucppResult
    tests: StrucppResult | None

    @property
    def errors_verbatim(self) -> str:
        """Failing step outputs, exactly as STruC++ printed them."""
        failing = [
            result for result in (self.compile, self.tests) if result is not None and not result.ok
        ]
        return "\n".join(
            f"--- returncode {r.returncode} ---\n{r.stdout}{r.stderr}" for r in failing
        )


@dataclass(frozen=True)
class CodegenReport:
    task_id: str
    passed: bool
    diagram: str
    files: ProgramFiles
    checks: tuple[OfflineCheck, ...]
    input_tokens: int
    output_tokens: int

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "attempts": len(self.checks),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "checks": [
                {
                    "compile_ok": c.compile.ok,
                    "compile_tail": _tail(c.compile.stdout + c.compile.stderr),
                    "tests_ok": c.tests.ok if c.tests else None,
                    "tests_tail": _tail(c.tests.stdout + c.tests.stderr) if c.tests else "",
                }
                for c in self.checks
            ],
        }


def load_prompt(name: str) -> Template:
    """Load ``agent/prompts/<name>`` as a ``string.Template``."""
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise CodegenError(f"prompt not found: {path}")
    return Template(path.read_text(encoding="utf-8"))


def render_spec_yaml(spec: TaskSpec) -> str:
    return yaml.safe_dump(spec.model_dump(), sort_keys=False).strip() + "\n"


def render_io_table(spec: TaskSpec, io_map: IoMap) -> str:
    """One line per spec signal: name, type, direction, IEC, Modbus, safe value."""
    lines = []
    for name in spec.signals:
        signal = io_map.signals[name]
        safe = io_map.safe_state.get(name, "?")
        lines.append(
            f"{name}: {signal.type} {signal.direction} {signal.iec} "
            f"({signal.modbus.table} {signal.modbus.address}) safe={safe}"
        )
    return "\n".join(lines)


def render_safe_state(spec: TaskSpec, io_map: IoMap) -> str:
    return ", ".join(f"{n}={io_map.safe_state.get(n, '?')}" for n in spec.signals)


def extract_fenced(text: str, info: str) -> list[str]:
    """Contents of every `````info `` fence in order (info matched exactly)."""
    blocks: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].strip() == f"```{info}":
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                body.append(lines[i])
                i += 1
            blocks.append("".join(body).strip() + "\n")
        i += 1
    return blocks


def extract_mermaid(text: str) -> str:
    """The `````mermaid `` block, or raw text starting with ``stateDiagram``."""
    blocks = extract_fenced(text, "mermaid")
    if blocks:
        return blocks[0]
    if text.strip().startswith("stateDiagram"):
        return text.strip() + "\n"
    raise CodegenError("reply contains no ```mermaid block")


def extract_program_files(text: str) -> ProgramFiles:
    """First two `````st `` blocks: ``main.st``, then ``tests.st``."""
    blocks = extract_fenced(text, "st")
    if len(blocks) < 2:
        raise CodegenError(f"reply contains {len(blocks)} ```st blocks, need 2")
    return ProgramFiles(main_st=blocks[0], tests_st=blocks[1])


def _fewshot(name: str) -> str:
    path = FEWSHOT_DIR / name
    if not path.is_file():
        raise CodegenError(f"few-shot example missing: {path}")
    return path.read_text(encoding="utf-8")


def plan_diagram(spec: TaskSpec, io_map: IoMap, llm: LlmBackend) -> tuple[str, LlmReply]:
    """Step 1: Mermaid behaviour plan for the spec."""
    prompt = load_prompt("plan_v1.txt").substitute(
        spec_yaml=render_spec_yaml(spec),
        io_table=render_io_table(spec, io_map),
        safe_state=render_safe_state(spec, io_map),
    )
    reply = llm.generate(prompt, effort=PLAN_EFFORT)
    return extract_mermaid(reply.text), reply


def generate_program(
    spec: TaskSpec, io_map: IoMap, diagram: str, llm: LlmBackend
) -> tuple[ProgramFiles, LlmReply]:
    """Step 2: ST program + ST tests from spec, I/O map and plan."""
    prompt = load_prompt("codegen_v1.txt").substitute(
        spec_yaml=render_spec_yaml(spec),
        io_table=render_io_table(spec, io_map),
        diagram=diagram,
        fewshot_main=_fewshot("main.st"),
        fewshot_tests=_fewshot("tests.st"),
    )
    reply = llm.generate(prompt, effort=CODEGEN_EFFORT)
    return extract_program_files(reply.text), reply


def repair_program(
    spec: TaskSpec,
    io_map: IoMap,
    files: ProgramFiles,
    errors: str,
    llm: LlmBackend,
    *,
    round: int,
    max_rounds: int,
) -> tuple[ProgramFiles, LlmReply]:
    """Step 3: fix ``files`` given verbatim STruC++ errors (cheap effort)."""
    prompt = load_prompt("repair_v1.txt").substitute(
        round=round,
        max_rounds=max_rounds,
        spec_yaml=render_spec_yaml(spec),
        io_table=render_io_table(spec, io_map),
        main_st=files.main_st,
        tests_st=files.tests_st,
        errors=errors,
    )
    reply = llm.generate(prompt, effort=REPAIR_EFFORT)
    return extract_program_files(reply.text), reply


def offline_check(
    files: ProgramFiles, workdir: str | Path, *, strucpp: str = "strucpp"
) -> OfflineCheck:
    """Write ``main.st`` / ``tests.st`` and run compile + ST tests.

    Tests only run when compilation succeeds.
    """
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    main = root / "main.st"
    tests = root / "tests.st"
    main.write_text(files.main_st, encoding="utf-8")
    tests.write_text(files.tests_st, encoding="utf-8")
    compile_result = compile_st(main, root / "build" / "main.cpp", strucpp=strucpp)
    test_result: StrucppResult | None = None
    if compile_result.ok:
        test_result = run_st_tests(main, tests, strucpp=strucpp)
    return OfflineCheck(
        passed=compile_result.ok and test_result is not None and test_result.ok,
        compile=compile_result,
        tests=test_result,
    )


def generate_with_repairs(
    spec: TaskSpec,
    io_map: IoMap,
    llm: LlmBackend,
    *,
    workdir: str | Path,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    strucpp: str = "strucpp",
    diagram: str | None = None,
) -> CodegenReport:
    """Plan, generate, then repair until the offline check passes or rounds run out."""
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
    replies: list[LlmReply] = []
    if diagram is None:
        diagram, plan_reply = plan_diagram(spec, io_map, llm)
        replies.append(plan_reply)
    files, reply = generate_program(spec, io_map, diagram, llm)
    replies.append(reply)
    root = Path(workdir)
    checks = [offline_check(files, root, strucpp=strucpp)]
    _snapshot_attempt(root, 1, files)
    attempt = 1
    while not checks[-1].passed and attempt < max_rounds:
        attempt += 1
        files, reply = repair_program(
            spec,
            io_map,
            files,
            checks[-1].errors_verbatim,
            llm,
            round=attempt,
            max_rounds=max_rounds,
        )
        replies.append(reply)
        checks.append(offline_check(files, root, strucpp=strucpp))
        _snapshot_attempt(root, attempt, files)
    return CodegenReport(
        task_id=spec.task_id,
        passed=checks[-1].passed,
        diagram=diagram,
        files=files,
        checks=tuple(checks),
        input_tokens=sum(r.input_tokens for r in replies),
        output_tokens=sum(r.output_tokens for r in replies),
    )


def _snapshot_attempt(workdir: Path, attempt: int, files: ProgramFiles) -> None:
    """Keep per-attempt ST so the dashboard can diff repairs (M7).

    ``offline_check`` rewrites ``main.st`` in place; without snapshots the
    intermediate attempts are lost. Best effort: a snapshot failure must
    never fail codegen.
    """
    try:
        snap = workdir / "attempts" / f"attempt{attempt}"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "main.st").write_text(files.main_st, encoding="utf-8")
        (snap / "tests.st").write_text(files.tests_st, encoding="utf-8")
    except OSError:
        pass


def _tail(text: str) -> str:
    text = text.strip()
    return text[-REPORT_TAIL_CHARS:] if len(text) > REPORT_TAIL_CHARS else text


def _fake_llm_for_plumbing() -> ScriptedLlmBackend:
    """Scripted replies from the checked-in example: exercises plumbing, not quality."""
    plan = "```mermaid\nstateDiagram-v2\n    [*] --> safe\n    safe --> [*]\n```\n"
    code = f"```st\n{_fewshot('main.st')}```\n```st\n{_fewshot('tests.st')}```\n"
    return ScriptedLlmBackend([plan, code])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate ST + tests for a task spec.")
    parser.add_argument("spec", help="task spec YAML, e.g. specs/start_lamp_arm.yaml")
    parser.add_argument("--out", required=True, help="output dir for diagram/main/tests/report")
    parser.add_argument("--fake", action="store_true", help="no API: scripted example replies")
    parser.add_argument("--max-rounds", type=int, default=MAX_REPAIR_ROUNDS)
    parser.add_argument("--strucpp", default="strucpp")
    parser.add_argument("--io-map", default=None, help="io_map.yaml path (default: project file)")
    parser.add_argument("--diagram", default=None, help="reuse a diagram.mmd instead of planning")
    args = parser.parse_args(argv)

    spec = load_task_spec(args.spec)
    io_map = load_io_map(args.io_map)
    check_against_io_map(spec, io_map)
    llm: LlmBackend
    if args.fake:
        llm = _fake_llm_for_plumbing()
    else:
        _load_dotenv()
        llm = MetaLlmBackend.from_env()
    diagram = Path(args.diagram).read_text(encoding="utf-8") if args.diagram else None
    out = Path(args.out)
    report = generate_with_repairs(
        spec,
        io_map,
        llm,
        workdir=out,
        max_rounds=args.max_rounds,
        strucpp=args.strucpp,
        diagram=diagram,
    )
    (out / "diagram.mmd").write_text(report.diagram, encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{spec.task_id}: {'PASS' if report.passed else 'FAIL'} "
        f"after {len(report.checks)} attempt(s); "
        f"tokens {report.input_tokens} in / {report.output_tokens} out -> {out}"
    )
    if isinstance(llm, MetaLlmBackend):
        llm.close()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
