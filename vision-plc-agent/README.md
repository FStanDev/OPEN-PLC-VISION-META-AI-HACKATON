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

## Status

Milestone 1: scaffold + fakes.
