# Dashboard

One web page over run evidence (milestone 7): spec, ST code and diffs,
live camera with mask boxes, test timeline and verdict — plus the SPEC
step-4 approve button.

Stdlib only (`http.server`, no new dependencies): the sandbox has no
network access to install FastAPI, and the page must work regardless.
The JSON API mirrors the shape a FastAPI port would serve.

```bash
uv run python -m dashboard.server --runs-dir runs --port 8000
# open http://127.0.0.1:8000/
```

Pair with a waiting loop:

```bash
uv run python -m agent.loop specs/start_lamp_arm.yaml --fake \
  --wait-approval 300 --out /tmp/demo-run
```

Or use the **New task** form in the page: type a description, pick
signals, click Generate & run. With the tests box left empty the
dashboard writes a *brief* (`specs/<task_id>.yaml` without `tests`) and
the run drafts the acceptance tests with Muse Spark first: they appear
in an editable **Acceptance tests** panel; edit if needed and click
**Approve tests & generate code** (or Reject). The server validates the
edit, fills the tests into `specs/<task_id>.yaml`, and the loop goes on
to generate the ST; then the usual **Approve & deploy** gate. Brief runs
wait for both gates in fake mode too, so the flow can be rehearsed
without the Pi. Paste YAML into the tests box to skip drafting. One run
at a time: starting while a run is active returns 409.

API: `GET /api/runs`, `GET /api/pending`, `GET /api/runs/<id>`,
`GET /api/runs/<id>/diff?from=<snap>&to=<snap>`,
`GET /api/runs/<id>/frames/<name>`, `POST /api/runs/<id>/approve`,
`POST /api/runs/<id>/approve-tests` (`{tests_yaml, by}`),
`POST /api/runs/<id>/reject-tests`,
`GET /api/preview.jpg` (live frame service → newest run frame →
placeholder), `GET /api/io-map`, `POST /api/tasks`,
`POST /api/runs/start`. See [DEMO.md](DEMO.md) for the rehearsal script.
