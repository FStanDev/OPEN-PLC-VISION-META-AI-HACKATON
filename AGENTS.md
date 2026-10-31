# AGENTS.md — vision-plc-agent

Project rules for coding agents (Muse Code, Claude Code). Read this file fully
before every task. The full project spec is `docs/SPEC.md`; read the section you
need before starting a milestone. If this file and a prompt conflict, stop and ask.

## What we are building

An AI agent that commissions a PLC like an engineer: it writes IEC 61131-3
Structured Text (ST) from a task spec, deploys it to OpenPLC Runtime v4 on a
Raspberry Pi, runs acceptance tests, and checks the physical result with a
camera. It tells logic bugs apart from wiring or hardware faults.

Hackathon: Meta Global AI Developer Hackathon. Submission due 4 Nov 2026, 21:00 ET.
One developer, limited evening hours: prefer the simplest thing that works.

## Hard constraints (never break these)

1. Open source only: MIT, Apache-2.0, BSD, MPL, or LGPL used as a library.
   Our code is MIT. Add every new dependency and its licence to `THIRD_PARTY.md`.
2. No proprietary or employer material of any kind. Build only from public sources.
3. The product's AI is Meta models via the Meta Model API (OpenAI-compatible):
   `muse-spark-1.3` for reasoning and ST code, SAM 3.1 for vision.
4. Hobby bench rig only: Pi, LEDs, optional servo, buttons, Logitech C920.

## Architecture

- **Laptop** (`agent/`, `dashboard/`, `tests/`): Python agent loop, STruC++ CLI,
  dashboard. Talks to the Pi over Tailscale.
- **Pi** (`pi/`): OpenPLC Runtime v4 (native install, REST API on 8443, Modbus TCP),
  `pi/gpio_plugin` (OpenPLC Python plugin: I/O image to GPIO + PCA9685),
  `pi/stimulus_service` (presses buttons via GPIO26/GPIO13),
  `pi/frame_service` (C920 still frames + MJPEG preview).
- **Pi access:** `ssh raspberrypi` (SSH config over Tailscale). Agent reaches all
  Pi services at the Pi's Tailscale address.
- Loop: spec → ST + ST tests → `strucpp` compile and test → human approve →
  package and deploy → stimulus → read tags (Modbus) + grab frames → SAM 3.1
  masks → measure → diagnose → repair (max 3) or hand back.

## I/O map (the contract — source of truth is `io_map.yaml`)

| Signal | IEC | Modbus | Pi pin (BCM) |
| --- | --- | --- | --- |
| start_btn | %IX0.0 | discrete input 0 | GPIO5, pull-up, active low |
| stop_btn | %IX0.1 | discrete input 1 | GPIO6, pull-up, active low |
| lamp_red | %QX0.0 | coil 0 | GPIO17 |
| lamp_green | %QX0.1 | coil 1 | GPIO27 |
| lamp_yellow | %QX0.2 | coil 2 | GPIO22 |
| arm_angle | %QW0 (0–180 deg) | holding register 0 | PCA9685 ch 0 (optional) |
| stim_start | test harness only | — | GPIO26 (1 kΩ to GPIO5) |
| stim_stop | test harness only | — | GPIO13 (1 kΩ to GPIO6) |

Change `io_map.yaml` first, then code. Never hard-code pins or addresses elsewhere.

## Diagnosis rules

| Spec vs tags | Tags vs camera | Verdict | Action |
| --- | --- | --- | --- |
| match | match | pass | record evidence |
| mismatch | camera agrees with tags | logic bug | fix ST, retry |
| match | mismatch | physical fault | stop, report address + observed state |
| mismatch | mismatch | unclear | stop, ask the human |
| any | low confidence | vision inconclusive | re-capture once, then report |

## Safety rules

1. Never deploy without explicit human approval (dashboard button or CLI prompt).
2. Clamp `arm_angle` to 0–180 in the GPIO plugin; force outputs off when the PLC stops.
3. Every generated program: `stop_btn` drives all outputs safe (lamps off, arm 0) in one scan.
4. Touch only pins in `io_map.yaml`. Nothing runs as root except the OpenPLC runtime.
5. Secrets only in `.env` (never committed). Bind Pi services to the Tailscale address.
6. Never fake results: failed measurement = "inconclusive". No hard-coded expected
   values in vision code.

## Conventions

**Python:** 3.11+, `uv` for envs, type hints, `ruff` (lint + format), `pytest`.
Every hardware, network and model call sits behind an interface with a fake:
`llm_backend`, `vision_backend`, `plc_client`, `tag_reader`, `stimulus`, `frames`.
The whole loop must run end to end with fakes only (no Pi, no API, no credits).

**Generated ST:** one `PROGRAM Main`; located vars named as in the I/O map
(`start_btn AT %IX0.0 : BOOL;`); standard FBs only (TON, TOF, TP, R_TRIG, F_TRIG,
SR, RS, CTU); edge-detect buttons with R_TRIG; English comments; every program
ships with an ST test file that passes `strucpp main.st --test tests.st`.

**Runs:** every run writes `runs/<timestamp>-<task_id>/` (spec, prompts, model
replies, ST, logs, frames, `report.json` with token usage).

**Git:** small commits, clear messages, no secrets, no large binaries.

## Commands

```bash
uv sync                                   # install
uv run pytest                             # tests (fakes, no hardware)
uv run ruff check . && uv run ruff format .
strucpp specs/<task>/main.st -o build/main.cpp
strucpp specs/<task>/main.st --test specs/<task>/tests.st
uv run python -m agent.loop specs/<task>.yaml --fake      # full loop, fakes
uv run python -m agent.loop specs/<task>.yaml             # real rig
```

## How to work

- One milestone at a time (see `docs/SPEC.md`, "Build plan"). Finish it, test it,
  commit, then stop and summarise what changed and what is next.
- Ask before adding a dependency, changing the I/O map, or touching `pi/` code
  that drives hardware.
- Unknown OpenPLC v4 details (program.zip layout, plugin API): read the reference
  in `reference/` or the upstream docs; do not guess. Flag it if still unknown.
- Keep credits low: small focused prompts to Muse Spark, low reasoning effort for
  small fixes, fakes in tests.
