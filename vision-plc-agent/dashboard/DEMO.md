# M7 demo rehearsal

Maps the SPEC demo script (under 3 minutes) to commands. Rehearse with
`--fake` first (no Pi, no credits), then on the real rig.

## 0. Start the dashboard

```bash
cd vision-plc-agent
uv run python -m dashboard.server --runs-dir runs --port 8000
# open http://127.0.0.1:8000/
```

With the Pi reachable, add `--frames-url $FRAMES_URL` (or export
`FRAMES_URL`) for the live camera tile; otherwise it shows the newest
frame from the last run.

## 1. Show the rig, state the task

Task file: `specs/start_lamp_arm.yaml`. Show it in the dashboard
(spec tab) or `cat` it in the terminal.

## 2. Agent shows diagram + ST, you approve

```bash
uv run python -m agent.loop specs/start_lamp_arm.yaml --fake \
  --wait-approval 300 --out /tmp/demo-run-1
```

The loop writes `pending_approval.json` and waits. The dashboard banner
turns yellow: review diagram + `main.st`, click **Approve & deploy**.
The run continues and the timeline fills in green.

## 3. Deployment + physical test, all green

Watch the dashboard: verdict chip flips to `pass`, the timeline shows
each expectation (expected / tag / camera), frames land in the strip —
click one to see the SAM mask box overlaid.

## 4. Fault injection: pull an LED leg

1. Pull the red LED leg (or cover it).
2. Re-run the same command with `--out /tmp/demo-run-2`, approve again.
3. Expect `physical_fault`: "Logic correct, `%QX0.0` is TRUE, lamp not
   lit, check wiring at that output." Tags agree with the spec, the
   camera disagrees — the loop stops instead of "fixing" good code.

Re-seat the LED before the next step.

## 5. Spec change: 3 s → 5 s, agent repairs

Edit a copy of the spec (`lamp_red value: false at_ms: 3500` →
`at_ms: 5500`), run with `--wait-approval`, approve. The first physical
test reports `logic_bug` (PLC still does 3 s), the agent repairs the ST,
re-deploys, re-verifies. Show the **ST diffs** panel: `codegen-1` →
`final` is the repair.

## 6. Close on the evidence report

`runs/<id>/report.json` + `summary.txt`: verdict, token usage, frames.
That directory is the submission artefact (demo run excepted,
`runs/` is git-ignored).

## If the rig misbehaves on camera

- Dashboard works fully offline from recorded runs: `--frames-url ""`
  falls back to last-run frames, so a dead camera never blanks the page.
- Keep one known-green run dir aside (`cp -r`) to show while debugging.
- Cut order per SPEC: servo, then dashboard polish — never the core
  loop (spec → ST → deploy → camera check → diagnosis).
