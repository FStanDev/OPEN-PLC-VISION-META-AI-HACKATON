# vision-plc-agent

An agent that writes PLC logic, runs the tests and uses vision to verify the
physical result, telling code bugs apart from wiring faults.

Meta Global AI Developer Hackathon entry. Rules for agents: `AGENTS.md`;
full spec: `docs/SPEC.md`.

## Setup

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Code generation (M4)

```bash
uv run python -m agent.codegen specs/start_lamp_arm.yaml --out build/codegen/demo --fake  # no API
uv run python -m agent.codegen specs/start_lamp_arm.yaml --out build/codegen/demo          # real model, needs META_API_KEY in .env
```

## Status

Milestone 4: code generation (Muse Spark ST + tests, STruC++ repair loop).
