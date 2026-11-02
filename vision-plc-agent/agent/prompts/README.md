# Prompts

Versioned `string.Template` prompts for milestone 4 (code generation).
`$placeholders` are substituted by `agent/codegen.py`; ST and Mermaid never
contain `$`, so no escaping is needed.

| File | Used by | Placeholders |
| --- | --- | --- |
| `plan_v2.txt` | `plan_diagram` | `spec_yaml`, `io_table`, `safe_state` |
| `codegen_v2.txt` | `generate_program` | `spec_yaml`, `io_table`, `diagram`, `fewshot_main`, `fewshot_tests` |
| `repair_v2.txt` | `repair_program` | `round`, `max_rounds`, `spec_yaml`, `io_table`, `main_st`, `tests_st`, `errors` |
| `acceptance_v1.txt` | `agent.acceptance.render_draft_prompt` | `task_id`, `description`, `io_table`, `safe_state`, `pressable`, `example_description`, `example_tests` |
| `acceptance_repair_v1.txt` | `agent.acceptance.render_repair_prompt` | `round`, `max_rounds`, `task_id`, `description`, `io_table`, `pressable`, `tests_yaml`, `problems` |

Acceptance prompts (loop step 0) draft the `tests:` list of a task spec from
a brief (description + signals). They spell out how the harness plays a test
(whole stimulus first, then expectations in order, frame at each deadline);
`agent/acceptance.py` checks the same rules and feeds violations back.
The few-shot is `specs/start_lamp_arm.yaml`.

v2 (2026-11-02): stop still forces the safe state while held (AGENTS.md
safety rule 3); a timed indicator may run after stop is RELEASED (TP on
F_TRIG), and the plan marks any request it cannot meet with `%% NOT DONE:`.
The `_v1` files are kept for older runs.

Few-shot examples are NOT copied here: codegen loads the known-good,
strucpp-tested `specs/start_lamp_arm/main.st` + `tests.st` directly, so there
is a single source of truth. Bump the `_vN` suffix when a prompt changes
meaning; keep the old file if any committed test or run depends on it.
