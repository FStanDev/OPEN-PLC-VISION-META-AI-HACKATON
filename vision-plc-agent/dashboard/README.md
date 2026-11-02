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

API: `GET /api/runs`, `GET /api/pending`, `GET /api/runs/<id>`,
`GET /api/runs/<id>/diff?from=<snap>&to=<snap>`,
`GET /api/runs/<id>/frames/<name>`, `POST /api/runs/<id>/approve`,
`GET /api/preview.jpg` (live frame service → newest run frame →
placeholder). See [DEMO.md](DEMO.md) for the rehearsal script.
