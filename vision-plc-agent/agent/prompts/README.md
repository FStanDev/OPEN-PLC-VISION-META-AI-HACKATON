# Prompts

Versioned `string.Template` prompts for milestone 4 (code generation).
`$placeholders` are substituted by `agent/codegen.py`; ST and Mermaid never
contain `$`, so no escaping is needed.

| File | Used by | Placeholders |
| --- | --- | --- |
| `plan_v2.txt` | `plan_diagram` | `spec_yaml`, `io_table`, `safe_state` |
| `codegen_v2.txt` | `generate_program` | `spec_yaml`, `io_table`, `diagram`, `fewshot_main`, `fewshot_tests` |
| `repair_v2.txt` | `repair_program` | `round`, `max_rounds`, `spec_yaml`, `io_table`, `main_st`, `tests_st`, `errors` |

v2 (2026-11-02): stop still forces the safe state while held (AGENTS.md
safety rule 3); a timed indicator may run after stop is RELEASED (TP on
F_TRIG), and the plan marks any request it cannot meet with `%% NOT DONE:`.
The `_v1` files are kept for older runs.

Few-shot examples are NOT copied here: codegen loads the known-good,
strucpp-tested `specs/start_lamp_arm/main.st` + `tests.st` directly, so there
is a single source of truth. Bump the `_vN` suffix when a prompt changes
meaning; keep the old file if any committed test or run depends on it.
