# First prompt for Muse Code

Paste this as your first message after `cd vision-plc-agent && muse`.

---

Read AGENTS.md and docs/SPEC.md fully. Don't write code yet.

1. Summarise the project back to me in 5 bullet points, so I know you understood it.
2. List anything in the spec that is ambiguous or that you can't verify (especially
   OpenPLC Runtime v4 program.zip layout and the Python plugin API).
3. Propose a plan for milestone 1 only: "Scaffold + fakes". It should deliver:
   - `pyproject.toml` (uv, ruff, pytest), `LICENSE` (MIT), `THIRD_PARTY.md`
   - the folder layout from the spec, with empty modules
   - `agent/io_map.py` loading `io_map.yaml` with Pydantic
   - `agent/spec.py`: Pydantic model for the task YAML, plus `specs/start_lamp_arm.yaml`
     (LED-only version first, no servo)
   - the interfaces and fake implementations for `llm_backend`, `vision_backend`,
     `plc_client`, `tag_reader`, `stimulus`, `frames`
   - `agent/diagnose.py` implementing the diagnosis table, with unit tests
   - `uv run pytest` green

Wait for my OK before writing files.

---

## Next milestones (one prompt each, after the previous is committed)

2. **Real clients:** `plc_client` (OpenPLC REST: login, upload, compile status,
   start/stop), `tag_reader` (pymodbus), `strucpp.py` wrapper. Test against the Pi.
3. **Pi services:** `pi/gpio_plugin` (LEDs + buttons first), `pi/stimulus_service`,
   `pi/frame_service` (v4l2 lock, `/frame.jpg`, `/preview.mjpg`).
4. **Packager:** build `program.zip` from ST, matching `reference/program.zip`
   from the Editor. If not possible, fall back to semi-automatic deploy (see spec risks).
5. **Codegen:** Muse Spark ST + ST test generation, strucpp feedback loop (max 3).
6. **Vision:** SAM 3.1 masks, calibration, LED brightness (servo angle if wired).
7. **Closed loop + report.json**, then **dashboard**, then demo rehearsal.
