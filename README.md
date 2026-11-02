# vision-plc-agent

**An agent that writes PLC logic, runs the tests and uses a camera to verify the
physical result, telling code bugs apart from wiring faults.**

Entry for the Meta Global AI Developer Hackathon (Indie developer track, theme
"Push the Frontier"). Built by one person on a hobby bench
rig; it never controls real industrial equipment or mains voltage.

## What it does

The agent commissions a PLC the way an engineer does:

1. **Spec:** a task written as YAML (I/O, sequence, acceptance tests).
2. **Code:** Muse Spark writes IEC 61131-3 Structured Text plus ST unit tests.
   STruC++ compile errors and test failures are fed back automatically.
3. **Approve:** a human approves before anything is deployed.
4. **Deploy:** the program is packaged and uploaded to OpenPLC Runtime v4 on a
   Raspberry Pi.
5. **Test:** the agent presses the buttons, reads PLC tags over Modbus TCP and
   grabs camera frames.
6. **See:** SAM 3.1 segments the LEDs (and optional servo arm) so lamp state can be
   measured.
7. **Diagnose:** spec, tags and camera are compared three ways:

| Spec vs tags | Tags vs camera | Verdict | Action |
| --- | --- | --- | --- |
| match | match | pass | record evidence |
| mismatch | camera agrees with tags | logic bug | fix ST, retry (max 3) |
| match | mismatch | physical fault | stop, report address + observed state |
| mismatch | mismatch | unclear | stop, ask the human |
| any | low confidence | vision inconclusive | re-capture once, then report |

Example: "Logic correct, `%QX0.0` is TRUE, lamp not lit, check wiring at that output."

## AI models

Meta models through the Meta Model API (OpenAI-compatible):
`muse-spark-1.3` for reasoning and ST code, SAM 3.1 for vision.

## Architecture

```text
Laptop                                  Raspberry Pi (local network)
agent/      Python agent loop           OpenPLC Runtime v4 (REST 8443, Modbus TCP)
dashboard/  web UI                      pi/gpio_plugin       I/O image -> GPIO (+ PCA9685)
tests/      pytest, fakes only          pi/stimulus_service  presses buttons via GPIO
                                        pi/frame_service     C920 stills + MJPEG preview
```

Every hardware, network and model call sits behind an interface with a fake
(`llm_backend`, `vision_backend`, `plc_client`, `tag_reader`, `stimulus`, `frames`),
so the whole loop runs without a Pi, an API key or credits.

The I/O contract (pins, IEC addresses, Modbus addresses) lives in
[`vision-plc-agent/io_map.yaml`](vision-plc-agent/io_map.yaml).

## Repository layout

| Path | Contents |
| --- | --- |
| [`vision-plc-agent/`](vision-plc-agent/) | The project: Python package, Pi services, tests, task specs |
| [`vision-plc-agent/docs/SPEC.md`](vision-plc-agent/docs/SPEC.md) | Full project spec (source of truth) |
| [`vision-plc-agent/AGENTS.md`](vision-plc-agent/AGENTS.md) | Rules for coding agents working on the repo |
| [`vision-plc-agent/THIRD_PARTY.md`](vision-plc-agent/THIRD_PARTY.md) | Every dependency and its licence |
| `vision-plc-agent-starter/` | Original starter kit the project was bootstrapped from |
| `AGENTS.md`, `KICKOFF_PROMPT.md`, `docs/` | Original kickoff material |

Note on history: Git was re-initialised at the project root on 31 Oct 2026; the
first commit message was later reworded to explain this.

## Getting started

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
cd vision-plc-agent
uv sync
uv run pytest                      # unit tests, fakes only, no hardware
uv run ruff check . && uv run ruff format .
```

Configuration for the real rig goes in `.env` (copy `.env.example`; never commit it).

Running the agent loop (planned interface: `agent/loop.py` is still a stub until
the closed-loop milestone):

```bash
uv run python -m agent.loop specs/start_lamp_arm.yaml --fake   # fakes only
uv run python -m agent.loop specs/start_lamp_arm.yaml          # real rig
```

Compiling and testing generated ST needs the
[STruC++](https://github.com/Autonomy-Logic/STruCpp) CLI (`strucpp`) on your `PATH`:

```bash
strucpp specs/<task>/main.st --test specs/<task>/tests.st
```

## Status

Work in progress for the 4 Nov 2026 deadline.

- [x] Milestone 1: scaffold, interfaces and fakes, diagnosis table with tests
- [x] Milestone 2: real clients (`plc_client`, `tag_reader`) and the `strucpp` wrapper
- [ ] Pi services (GPIO plugin, stimulus, frames)
- [ ] `program.zip` packager
- [ ] Muse Spark code generation and repair loop
- [ ] SAM 3.1 vision and measurement
- [ ] Closed loop, `report.json`, dashboard, demo

## Safety

- Nothing is deployed without explicit human approval.
- `stop_btn` drives every output to a safe state (lamps off, arm 0) within one scan.
- The GPIO plugin clamps `arm_angle` to 0-180 and forces outputs off when the PLC stops.
- A failed measurement is reported as "inconclusive", never faked.

## Licence

SPDX-License-Identifier: `MIT` ([LICENSE](LICENSE)). All dependencies are open source with permissive or weak-copyleft
licences; see [`vision-plc-agent/THIRD_PARTY.md`](vision-plc-agent/THIRD_PARTY.md).
