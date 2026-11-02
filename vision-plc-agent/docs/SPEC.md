# Vision-verified PLC agent — project spec

This is the single source of truth for the project and for every coding agent working on it. Read it fully before writing code; when it conflicts with a prompt, ask.

## Purpose and constraints

We are building an AI agent that commissions a PLC the way an engineer does: it writes the control logic, deploys it, runs acceptance tests, and checks the physical result with a camera. It tells logic bugs apart from wiring or hardware faults.

**One-line pitch:** "An agent that writes PLC logic, runs the tests and uses vision to verify the physical result, telling code bugs apart from wiring faults."

**Context**

- Event: Meta Global AI Developer Hackathon, remote. Build window 26 Oct to 4 Nov 2026; submission due 4 Nov, 21:00 ET (03:00 on 5 Nov in Madrid).
- Track: Indie developer (judged on creativity, technical ambition and whether it works).
- Theme: "Push the Frontier" (give a machine a new capability; models meeting the physical world).
- Builder: one person, mostly evenings on weekdays.

**Hard constraints (agents must respect these)**

1. Only open-source components with permissive or weak-copyleft licences (MIT, Apache-2.0, BSD, MPL, LGPL used as libraries). Our own code is MIT.
2. No proprietary or employer material of any kind: no vendor SDKs, no code, documents, tag names, naming conventions or screenshots from any employer. Everything is built from public sources.
3. Meta models are the AI core: Muse Spark 1.3 for reasoning and code, SAM 3.1 for vision, via the Meta Model API.
4. The rig is a hobby bench setup: Raspberry Pi, LEDs, a servo, buttons, a webcam. It never controls real industrial equipment or mains voltage.

## Scope

The MVP is one closed loop on one bench rig: plain-language task in, verified PLC program and evidence report out.

**MVP (must ship by 4 Nov)**

1. Task spec: a YAML file (written by hand or drafted by the agent from a sentence) with I/O map, sequence and acceptance tests.
2. Code generation: Muse Spark writes IEC 61131-3 Structured Text; STruC++ compile errors and ST unit-test failures feed back automatically.
3. Deployment: the agent packages and uploads the program to OpenPLC Runtime v4 on the Pi, after a human clicks "approve".
4. Test execution: the agent drives inputs, records PLC tags over Modbus TCP and grabs camera frames at checkpoints.
5. Vision: SAM 3.1 segments the LEDs and the servo arm; we measure LED on/off and servo angle.
6. Diagnosis: three-way comparison of spec, tags and camera, classifying each failure as a logic bug or a physical fault, with a retry loop for logic bugs.
7. Dashboard: one web page showing spec, ST code and diffs, live camera with masks, test timeline and verdict.
8. Demo video (under 3 minutes) and README.

**Stretch (only after MVP works end to end)**

- OPC UA readback instead of Modbus, with the PLC variables exposed as a small information model.
- Muse Spark as a second visual opinion on frames ("is the red lamp lit?").
- Sentence-to-YAML spec drafting with a human review step.
- A second process scenario (traffic light or a pick-and-place arm sequence).
- Voice input for the task.

**Non-goals**

- Ladder or FBD generation (ST only).
- Safety-rated control, SIL, or any real plant.
- Multi-PLC or fieldbus (EtherCAT, PROFINET).
- Training or fine-tuning models.

## System architecture

The laptop runs the agent; the Pi runs the PLC; the agent judges every test from two independent sources, PLC tags and camera frames.

```text
                 ┌──────────────────────────────────────────┐
                 │ Meta Model API                           │
                 │  Muse Spark 1.3: spec, ST code, repairs  │
                 │  SAM 3.1: masks and tracks for vision    │
                 └───────────────────┬──────────────────────┘
                                     │
┌─ Laptop (Python) ─────────────┐    │     ┌─ Raspberry Pi ───────────────────────┐
│  Agent loop  ◄────────────────┼────┘     │                                      │
│  (spec, codegen, deploy,      │◄────────►│  OpenPLC Runtime v4                  │
│   test, diagnose)             │ REST 8443│   REST :8443 deploy, scan cycle      │
│      │                        │ Modbus   │   Modbus TCP tags for readback       │
│  STruC++ CLI                  │          │        │                             │
│  (compile, ST unit tests)     │          │  GPIO plugin (I/O image to pins,     │
│                               │─────────►│   PCA9685 servo)                     │
│  Dashboard                    │ HTTP     │  Stimulus and frame services         │
│  (approve deploy, evidence)   │◄─────────│   (buttons via GPIO26/13, C920)      │
│                               │ frames   │        │                             │
│  Vision and measurement       │          └────────┼─────────────────────────────┘
│  (LED brightness, servo angle)│                   ▼
└───────────────────────────────┘          Bench rig: 3 LEDs, servo, 2 buttons
                                           C920 webcam on the Pi (frames over HTTP)    
```

The agent deploys over REST and reads tags over Modbus TCP, the stimulus service presses buttons electrically, and the C920 on the Pi serves frames over HTTP to the vision module, which calls SAM 3.1. Everything works from anywhere on the private network. The diagnosis step compares the two paths against the spec.

## Hardware bill of materials

Already available: Raspberry Pi (64-bit, Pi 4 or Pi 5), breadboard, jumper wires, LEDs, Logitech C920. The MVP runs on LEDs alone if the servo parts are missing.

| Item | Qty | Need | Why |
| --- | --- | --- | --- |
| Resistors 220–330 Ω | 10 | Must | LED current limiting at 3.3 V |
| Resistors 1 kΩ | 5 | Must | Stimulus lines in parallel with the buttons |
| Tactile push buttons | 2 | Must | Start and stop |
| Tripod or clamp for the C920 | 1 | Must | The camera must not move after calibration |
| SG90 or MG90S servo | 1–2 | Should | The moving actuator; optional for the MVP |
| PCA9685 16-channel PWM board (I2C) | 1 | Should | Jitter-free servo pulses |
| 5 V 2–3 A supply + barrel-to-screw adapter | 1 | Should | Servo power separate from the Pi; join grounds |
| Female-to-male jumper wires | 10 | Should | Pi header to breadboard and PCA9685 |

## Wiring and I/O map

This table is the contract between hardware, PLC code, the GPIO plugin, Modbus and vision. Agents must use these names and addresses exactly; change `io_map.yaml` first, never the code alone.

| Signal | IEC address | Modbus | Pi pin (BCM / physical) | Wiring | Vision prompt and measure |
| --- | --- | --- | --- | --- | --- |
| `start_btn` | `%IX0.0` | Discrete input 0 | GPIO5 / 29 | Button to GND, internal pull-up, active low | — |
| `stop_btn` | `%IX0.1` | Discrete input 1 | GPIO6 / 31 | Button to GND, internal pull-up, active low | — |
| `lamp_red` | `%QX0.0` | Coil 0 | GPIO17 / 11 | 330 Ω, LED, GND | "red LED" → brightness |
| `lamp_green` | `%QX0.1` | Coil 1 | GPIO27 / 13 | 330 Ω, LED, GND | "green LED" → brightness |
| `lamp_yellow` | `%QX0.2` | Coil 2 | GPIO22 / 15 | 330 Ω, LED, GND | "yellow LED" → brightness |
| `arm_angle` | `%QW0` (0–180, degrees) | Holding register 0 | PCA9685 channel 2 | Servo signal; servo power from external 5 V | "servo arm" → angle |
| `stim_start` | — (test harness only) | — | GPIO26 / 37 | 1 kΩ to GPIO5 node | — |
| `stim_stop` | — (test harness only) | — | GPIO13 / 33 | 1 kΩ to GPIO6 node | — |

**PCA9685 connections:** SDA to GPIO2 (pin 3), SCL to GPIO3 (pin 5), VCC to 3.3 V (pin 1), GND to pin 6. Its V+ terminal takes the external 5 V supply. Join the external supply's ground to the Pi's ground.

**How the agent "presses" a button.** The stimulus pins sit in parallel with the real buttons through 1 kΩ. To press, the harness drives the pin LOW; to release, it switches the pin back to input (high impedance). The physical buttons keep working, and the PLC sees a real electrical input, not a software force.

**Fault injection for the demo:** pull one LED leg, unplug the servo signal wire, or cover an LED. Each produces "tags correct, camera disagrees", which the agent must classify as a physical fault.

**Pin ownership:** the OpenPLC GPIO plugin owns the PLC pins (inputs, LEDs, I2C). The stimulus service owns GPIO26 and GPIO13 only. Nothing else touches GPIO.

**Camera:** the C920 plugs into a Pi USB port, on a tripod in front of the rig. A frame service on the Pi locks exposure, focus and white balance with `v4l2-ctl` at start-up and serves still frames (and an MJPEG preview for the dashboard) over HTTP.

## Software prerequisites

The agent runs on the laptop in Python; the Pi runs OpenPLC natively plus two small services.

**Accounts and access**

- Meta Model API key with access to `muse-spark-1.3` and SAM 3.1
- Partner credits as fallback for open-weights models (Baseten, Modal or Fireworks)
- Public GitHub repository, MIT licence
- Hackathon Discord, for office hours

**Laptop (agent, compiler, dashboard)**

- Python 3.11+ with `uv`
- [STruC++](https://github.com/Autonomy-Logic/STruCpp) prebuilt binary on `PATH`, plus `g++` with C++17 for `--build`
- [OpenPLC Editor v4](https://github.com/Autonomy-Logic/openplc-editor/releases), matched to the runtime version
- Docker, to run a second OpenPLC runtime on the laptop for tests without the Pi
- Git, VS Code

**Raspberry Pi**

- Raspberry Pi OS 64-bit, SSH and I2C enabled
- [OpenPLC Runtime v4](https://github.com/Autonomy-Logic/openplc-runtime), native install, pinned to v4.3.1 (verified against the bench Pi on 31 Oct 2026; REST: login, version, status, compilation-status)
- Plugin venv (`venvs/gpio_rig`) with `gpiozero`, `lgpio`, Adafruit `servokit`
- `pi/.venv` for the stimulus and frame services (`lgpio`, `fastapi`, `uvicorn`, `opencv-python-headless`)

**Remote development:** personal devices on the home LAN only, SSH keys only, VS Code Remote-SSH, tmux. The agent on the laptop reaches the Pi's IP address: REST on 8443, Modbus TCP, the stimulus service and the frame service.

**Python libraries (agent side)**

| Library | Use | Licence |
| --- | --- | --- |
| `openai` SDK | Meta Model API is OpenAI-compatible (`base_url` = Meta's endpoint) | Apache-2.0 |
| `httpx` | OpenPLC REST client | BSD-3 |
| `pymodbus` | Tag readback over Modbus TCP | BSD-3 |
| `asyncua` (stretch) | Tag readback over OPC UA | LGPL-3.0, used as a library |
| `opencv-python`, `numpy` | Frame handling, mask measurements | Apache-2.0 / BSD-3 |
| `pydantic`, `PyYAML` | Task spec schema and parsing | MIT |
| `fastapi`, `uvicorn` | Dashboard backend, live updates | MIT / BSD-3 |
| `pytest` | Tests | MIT |

Record every dependency and its licence in `THIRD_PARTY.md` when you add it.

## OpenPLC Runtime v4 facts agents need

- Headless; no web UI. Controlled through a REST API on `https://<pi>:8443/api`, self-signed TLS, JWT auth.
- `POST /api/login` with `{"username","password"}` returns `access_token`; send `Authorization: Bearer <token>`.
- `POST /api/upload-file` (multipart, field `file` = `program.zip`); poll `GET /api/compilation-status` until `SUCCESS` or `FAILED` (returns logs); then `GET /api/start-plc`. Also `GET /api/stop-plc`, `GET /api/status`, `GET /api/runtime-logs`, `GET /api/version`.
- The `program.zip` layout is generated by the Editor (STruC++ pipeline producing C++ sources). It is not publicly specified: reproduce it from `reference/program.zip`. If that proves impossible, fall back to semi-automatic deploy (see risks).
- Python plugins implement `init(runtime_args_capsule)`, optional `start_loop`, `stop_loop`, `cleanup`; use `shared.safe_extract_runtime_args_from_capsule` and `SafeBufferAccess` for I/O buffers. Registered in `plugins.conf` as `name,path,enabled,type(0=Python),config_path,venv_path`.
- Modbus slave mapping: coils ↔ `%QX`, discrete inputs ↔ `%IX`, holding registers ↔ `%QW`, input registers ↔ `%IW`.
- There is no built-in Raspberry Pi GPIO driver in v4: our `pi/gpio_plugin` provides it.

## Auxiliary tools

| Need | Tool | How we use it |
| --- | --- | --- |
| Logic diagrams in the repo | Mermaid | Each task spec gets a `stateDiagram-v2` written by the agent |
| Graphical PLC logic and live debug | OpenPLC Editor v4 | Sketch logic, force variables, produce the reference `program.zip` |
| ST compile, unit tests, REPL | STruC++ CLI | Agent inner loop; manual checks |
| Wiring drawings | draw.io | Wiring diagram for the README and demo |
| Manual Modbus checks | `mbpoll` or QModMaster | Read coils and registers by hand |
| Camera control | `v4l2-ctl` | Lock exposure, white balance and focus |
| Network capture | Wireshark | Debug Modbus traffic |
| Demo recording | OBS Studio | Screen plus rig camera |

## Agent loop specification

Every run goes spec → code → offline tests → human approval → deploy → physical tests → diagnosis, with at most 3 automatic code fixes before handing back to the human.

### Task spec format

One YAML file per task in `specs/`, validated by a Pydantic model. Signal names and addresses come from `io_map.yaml`.

```yaml
task_id: start_lamp_arm
description: >
  When start is pressed, turn the red lamp on for 3 s and move the arm to 90 degrees.
  Stop returns the arm to 0 and turns everything off.
signals: [start_btn, stop_btn, lamp_red, arm_angle]
tests:
  - name: start_sequence
    stimulus:
      - {at_ms: 0,   press: start_btn}
      - {at_ms: 200, release: start_btn}
    expect:
      - {signal: lamp_red,  value: true,  within_ms: 500}
      - {signal: arm_angle, value: 90, tolerance: 10, within_ms: 1500}
      - {signal: lamp_red,  value: false, at_ms: 3500, tolerance_ms: 400}
  - name: stop_resets
    stimulus:
      - {at_ms: 0, press: start_btn}
      - {at_ms: 200, release: start_btn}
      - {at_ms: 1000, press: stop_btn}
      - {at_ms: 1200, release: stop_btn}
    expect:
      - {signal: arm_angle, value: 0, tolerance: 10, within_ms: 2500}
      - {signal: lamp_red,  value: false, within_ms: 1500}
```

The `tests` list may be left out: such a *brief* (task_id, description,
signals) makes the loop start at step 0.

### The loop, step by step

0. **Draft acceptance tests** (briefs only). Muse Spark turns the description into timed stimulus + expectations; `agent/acceptance.py` validates them against the schema, the I/O map and the harness timing rules (up to 3 rounds), and the human approves or edits them (dashboard panel or CLI prompt) before any code is written.
1. **Plan.** Muse Spark reads the spec and writes a Mermaid `stateDiagram-v2` of the intended behaviour, saved next to the spec.
2. **Generate.** Muse Spark writes one ST `PROGRAM` using only the signals in the spec, plus an ST test file with `TEST` blocks derived from the YAML tests (timers handled with `ADVANCE_TIME`).
3. **Offline check.** Run `strucpp main.st -o build/main.cpp` and `strucpp main.st --test tests.st`. Errors go back to the model verbatim; up to 3 rounds.
4. **Approve.** The dashboard shows the code diff and the diagram; nothing is deployed until the human clicks approve.
5. **Deploy.** Package `program.zip`, `POST /api/upload-file`, poll `/api/compilation-status` until `SUCCESS`, then `/api/start-plc`.
6. **Calibrate** (once per session, before tests): drive each lamp off and on and the arm to 0, 90 and 180; record per-mask brightness and angle baselines.
7. **Run physical tests.** The harness plays the stimulus timeline through the stimulus service, polls Modbus every 50 ms, and grabs camera frames at each `expect` deadline (plus one frame every 250 ms for the video).
8. **Measure.** SAM 3.1 segments the prompts from the I/O map; we compute LED brightness inside each mask and the arm angle from the mask's main axis, compared with the calibration.
9. **Diagnose** each expectation with the table below; write `runs/<id>/report.json` and a short readable summary.
10. **Repair or hand back.** A logic bug returns to step 2 with the failing evidence (max 3 total fixes per run). A physical fault stops the loop and tells the human what to check.

### Diagnosis table

| Spec vs tags | Tags vs camera | Verdict | Agent action |
| --- | --- | --- | --- |
| Match | Match | Pass | Record evidence |
| Mismatch | (camera agrees with tags) | Logic bug | Fix the ST code and retry |
| Match | Mismatch | Physical fault (wiring, dead LED, stalled servo, power) | Stop; report the address and the observed state |
| Mismatch | Mismatch | Unclear | Stop; ask the human; never loop |
| Any | Camera low confidence | Vision inconclusive | Re-capture once, then report |

### Model usage

| Job | Model | Notes |
| --- | --- | --- |
| Spec reasoning, ST and test generation, repairs, report text | `muse-spark-1.3` via Meta Model API | OpenAI-compatible client; low reasoning effort for small fixes, higher for first generation |
| Segmentation and tracking of LEDs and arm | SAM 3.1 via Meta Model API | One call returns detections, masks and tracks from a short text prompt |
| Second visual opinion (stretch) | `muse-spark-1.3` with the frame attached | Yes/no question per lamp; never overrides measurements |
| Fallback | SAM 3.1 open weights locally, or open-weights LLMs via partner credits | Behind the same interface (`vision_backend`, `llm_backend`) |

Prompts live in `agent/prompts/` as versioned text files, with a few-shot set of correct OpenPLC-style ST programs.

### Development tools vs product AI

The product's AI (what runs inside the loop and what judges see) is always Meta models. The coding assistant used to build the repo is a separate choice: Muse Code first, then Claude Code if credits run out and the rules allow it. Product calls get priority on credits; fakes keep tests free.

## Rules for agents

**Safety and integrity**

1. Never deploy to the PLC without explicit human approval.
2. Servo limits are enforced twice: clamp `arm_angle` to 0–180 in the GPIO plugin, independent of PLC code. The plugin also forces all outputs off when the PLC is stopped.
3. Every generated program has a stop path: `stop_btn` drives all outputs to their safe state (lamps off, arm 0) within one scan.
4. Never touch GPIO pins outside the I/O map; never run anything as root except the runtime itself.
5. Keep the rig on a private network (home LAN only); no port forwarding; personal devices only; credentials in `.env`, never in git.
6. Never fake results: if a measurement fails, report it as inconclusive. No hard-coded expected values in vision code.
7. No employer or proprietary material, ever.

**Generated ST conventions**

- One `PROGRAM Main` per task; located variables with the names from the I/O map (`start_btn AT %IX0.0 : BOOL;`).
- Standard function blocks only (`TON`, `TOF`, `TP`, `R_TRIG`, `F_TRIG`, `SR`, `RS`, `CTU`); no pointers, no dynamic memory.
- Edge-detect buttons with `R_TRIG`; never act on a level twice.
- Comments in English; each state of the sequence commented.
- Every program ships with an ST test file that passes `strucpp --test` before deployment.
- ST test files use single-quoted names (`TEST 'lamp on'`): STruC++ v0.7.0 rejects the double-quoted form shown in its own docs.

**Python conventions**

- Python 3.11, type hints, `ruff`, `pytest`.
- Hardware, network and model calls behind interfaces (`plc_client`, `tag_reader`, `stimulus`, `frames`, `vision_backend`, `llm_backend`) with fakes, so the loop runs end to end with no Pi and no API.
- All runs write to `runs/<timestamp>-<task_id>/`: spec, prompts, model replies, ST, logs, frames, `report.json` (including token usage).
- Small commits; no secrets; no large binaries in git.

## Repository layout

```text
vision-plc-agent/
├── AGENTS.md              # agent rules (short); points here
├── README.md              # pitch, demo GIF, setup
├── LICENSE                # MIT
├── THIRD_PARTY.md         # every dependency and its licence
├── io_map.yaml            # the I/O map, machine-readable
├── docs/SPEC.md           # this spec
├── specs/                 # task YAML files + generated Mermaid diagrams
├── agent/
│   ├── loop.py            # orchestrates the 10 steps
│   ├── llm_backend.py     # Meta Model API client (OpenAI-compatible) + fake
│   ├── codegen.py         # prompts, ST + test generation, repairs
│   ├── strucpp.py         # wraps the STruC++ CLI
│   ├── packager.py        # builds program.zip for Runtime v4
│   ├── plc_client.py      # OpenPLC REST: login, upload, status, start/stop
│   ├── tag_reader.py      # Modbus TCP (OPC UA later)
│   ├── stimulus.py        # client for the Pi stimulus service
│   ├── frames.py          # client for the Pi frame service
│   ├── vision_backend.py  # SAM 3.1 calls + fake
│   ├── measure.py         # brightness and angle from masks, calibration
│   ├── diagnose.py        # the diagnosis table
│   └── prompts/           # versioned prompts + few-shot ST examples
├── pi/
│   ├── gpio_plugin/       # OpenPLC Runtime v4 Python plugin (GPIO + PCA9685)
│   ├── stimulus_service/  # tiny HTTP service driving GPIO26 and GPIO13
│   └── frame_service/     # C920 capture: v4l2 lock, still frames, MJPEG preview
├── dashboard/             # FastAPI + one HTML page
├── reference/             # program.zip from the Editor, for the packager
├── tests/                 # pytest, using the fake backends
└── runs/                  # run artefacts (git-ignored except the demo run)
```

## Build plan, demo and risks

The two riskiest pieces, the `program.zip` packager and the GPIO plugin, are spiked first.

### Build plan (to 4 Nov)

1. **Scaffold + fakes.** Layout, `io_map`, spec schema, interfaces with fakes, diagnosis table with tests.
2. **Spikes.** Inspect an Editor-made `program.zip` and swap in hand-written ST: does it compile and run? Minimal Python plugin toggling one LED. One Muse Spark and one SAM 3.1 call.
3. **Plumbing.** GPIO plugin, stimulus service, frame service, `plc_client`, `tag_reader`, `packager`. A hand-written program deploys and passes a scripted test with no AI.
4. **Code generation.** Muse Spark ST and test generation, STruC++ feedback loop, Mermaid diagrams.
5. **Vision.** SAM 3.1 masks, calibration, LED brightness; servo angle if wired.
6. **Closed loop.** Diagnosis, repair loop, `report.json`.
7. **Dashboard and demo.** Dashboard page, fault-injection rehearsal, video.
8. **4 Nov — buffer.** README, `THIRD_PARTY.md`, submit before 21:00 ET (03:00 Madrid, 5 Nov).

If time runs short, cut in this order: servo, OPC UA, dashboard polish. Never cut the core loop: spec, ST, deploy, camera check, diagnosis.

### Demo script (under 3 minutes)

1. Show the rig and type the task in plain language.
2. The agent shows the state diagram and ST; you approve.
3. Deployment and the physical test run, camera view with masks, all green.
4. Pull an LED leg and re-run: "Logic correct, `%QX0.0` is TRUE, lamp not lit, check wiring at that output."
5. Change the spec (lamp 5 s instead of 3 s): the agent detects the mismatch, fixes the code, re-verifies.
6. Close on the evidence report and the open-source stack.

### Risks and fallbacks

| Risk | Likelihood | Fallback |
| --- | --- | --- |
| `program.zip` cannot be built outside the Editor | Medium | Agent writes ST into an Editor project; human clicks upload (semi-automatic deploy); everything else stays automatic |
| Python GPIO plugin API harder than expected | Medium | External I/O bridge process using `lgpio` and Modbus |
| Meta Model API not reachable from Spain | Medium | Hackathon-provisioned access; else open-weights LLM via partner credits and SAM 3.1 weights locally |
| LED glare or lighting changes break vision | Medium | Lock exposure, black background, desk lamp, diffused LEDs; recalibrate per session |
| Servo jitter or brown-outs | Low | PCA9685, separate 5 V supply, joined grounds, spare servo |
| Generated ST has subtle timing bugs | Medium | ST unit tests with `ADVANCE_TIME`; few-shot examples; max 3 repairs |
| Runtime or Editor update breaks the API | Low | Pin versions; no updates during the sprint |
| Not enough evening hours | High | Protect the MVP list; drop stretch goals first; demo one scenario |

## Sources

- [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon)
- [OpenPLC Runtime v4](https://github.com/Autonomy-Logic/openplc-runtime), [Editor integration and REST API](https://github.com/Autonomy-Logic/openplc-runtime/blob/main/docs/EDITOR_INTEGRATION.md), [plugin system](https://github.com/Autonomy-Logic/openplc-runtime/blob/main/core/src/drivers/README.md)
- [STruC++ compiler](https://github.com/Autonomy-Logic/STruCpp)
- [Forum: no Raspberry Pi driver in v4](https://openplc.discussion.community/post/openplc-v4-and-raspberry-pi-13755921)
