"""Dashboard server tests: stdlib HTTP against fixture runs (no Pi, no API)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent.codegen import ProgramFiles
from agent.io_map import load_io_map
from agent.loop import wait_for_dashboard_approval
from agent.spec import check_against_io_map, load_task_spec
from dashboard.server import PLACEHOLDER_PNG, create_server

ROOT = Path(__file__).resolve().parent.parent
FRAME_BYTES = b"\xff\xd8fake-jpeg-bytes\xff\xd9"


def _write_report(run: Path) -> None:
    (run / "report.json").write_text(
        json.dumps(
            {
                "task_id": "start_lamp_arm",
                "run_id": run.name,
                "mode": "fake",
                "approved": True,
                "deployed": True,
                "passed": True,
                "verdict": "pass",
                "logic_fixes_used": 0,
                "input_tokens": 10,
                "output_tokens": 5,
                "codegen": {"passed": True, "attempts": 2},
                "tests": [
                    {
                        "name": "start_latches_lamp",
                        "passed": True,
                        "expectations": [
                            {
                                "test": "start_latches_lamp",
                                "signal": "lamp_red",
                                "expected": True,
                                "tag_value": True,
                                "tag_match": True,
                                "vision": {
                                    "status": "measured",
                                    "lit": True,
                                    "confidence": 1.0,
                                    "bbox": [10, 20, 30, 40],
                                    "signal": "lamp_red",
                                },
                                "tags_match_camera": True,
                                "vision_confidence": 1.0,
                                "verdict": "pass",
                                "action": "record evidence",
                            }
                        ],
                    }
                ],
                "summary": "task start_lamp_arm: pass (1/1 expectations pass)\n",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _make_run(runs: Path, name: str = "20261102T120000Z-start_lamp_arm") -> Path:
    run = runs / name
    (run / "frames").mkdir(parents=True)
    _write_report(run)
    (run / "spec.yaml").write_text("task_id: start_lamp_arm\n", encoding="utf-8")
    (run / "diagram.mmd").write_text("stateDiagram-v2\n", encoding="utf-8")
    (run / "summary.txt").write_text("pass (1/1)\n", encoding="utf-8")
    (run / "main.st").write_text("PROGRAM Main v3 END_PROGRAM\n", encoding="utf-8")
    (run / "tests.st").write_text("TESTS v3\n", encoding="utf-8")
    attempt1 = run / "codegen" / "attempts" / "attempt1"
    attempt1.mkdir(parents=True)
    (attempt1 / "main.st").write_text("PROGRAM Main v1 END_PROGRAM\n", encoding="utf-8")
    (attempt1 / "tests.st").write_text("TESTS v1\n", encoding="utf-8")
    attempt2 = run / "codegen" / "attempts" / "attempt2"
    attempt2.mkdir(parents=True)
    (attempt2 / "main.st").write_text("PROGRAM Main v2 END_PROGRAM\n", encoding="utf-8")
    (attempt2 / "tests.st").write_text("TESTS v2\n", encoding="utf-8")
    repair1 = run / "repair1"
    repair1.mkdir()
    (repair1 / "main.st").write_text("PROGRAM Main v3 END_PROGRAM\n", encoding="utf-8")
    (repair1 / "tests.st").write_text("TESTS v3\n", encoding="utf-8")
    (run / "frames" / "start_latches_lamp_0_lamp_red_0.jpg").write_bytes(FRAME_BYTES)
    return run


def _make_pending(runs: Path) -> Path:
    run = runs / "20261102T130000Z-lamp_seq"
    run.mkdir(parents=True)
    (run / "pending_approval.json").write_text(
        json.dumps(
            {
                "run_id": run.name,
                "task_id": "lamp_seq",
                "at": "2026-11-02T13:00:00+00:00",
                "spec": {"task_id": "lamp_seq"},
                "diagram": "stateDiagram-v2\n",
                "main_st": "PROGRAM Main END_PROGRAM\n",
                "tests_st": "TESTS\n",
            }
        ),
        encoding="utf-8",
    )
    return run


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    _make_run(runs)
    _make_pending(runs)
    return runs


@pytest.fixture
def base_url(runs_dir: Path):
    server = create_server(runs_dir, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _get_json(base_url: str, path: str) -> object:
    with urllib.request.urlopen(base_url + path, timeout=5) as response:
        assert response.headers.get_content_type() == "application/json"
        return json.loads(response.read().decode("utf-8"))


def _get_bytes(base_url: str, path: str) -> tuple[bytes, str, dict[str, str]]:
    with urllib.request.urlopen(base_url + path, timeout=5) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.read(), response.headers.get_content_type(), headers


def _post_json(base_url: str, path: str, body: object) -> tuple[int, object]:
    blob = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path, data=blob, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_index_serves_single_page(base_url: str) -> None:
    blob, content_type, _ = _get_bytes(base_url, "/")
    assert content_type == "text/html"
    text = blob.decode("utf-8")
    assert "Vision PLC Agent" in text
    assert "approveBtn" in text and "frameView" in text


def test_runs_list_finished_and_pending(base_url: str) -> None:
    payload = _get_json(base_url, "/api/runs")
    assert isinstance(payload, dict)
    by_id = {r["run_id"]: r for r in payload["runs"]}
    finished = by_id["20261102T120000Z-start_lamp_arm"]
    assert finished["verdict"] == "pass" and finished["passed"] is True
    assert finished["pending_approval"] is False
    pending = by_id["20261102T130000Z-lamp_seq"]
    assert pending["pending_approval"] is True
    assert pending["verdict"] == "awaiting_approval"


def test_pending_lists_only_awaiting(base_url: str) -> None:
    payload = _get_json(base_url, "/api/pending")
    assert isinstance(payload, dict)
    assert [r["run_id"] for r in payload["pending"]] == ["20261102T130000Z-lamp_seq"]


def test_run_detail_has_snapshots_and_frames(base_url: str) -> None:
    detail = _get_json(base_url, "/api/runs/20261102T120000Z-start_lamp_arm")
    assert isinstance(detail, dict)
    assert detail["spec_yaml"] == "task_id: start_lamp_arm\n"
    assert [s["id"] for s in detail["snapshots"]] == [
        "codegen-1",
        "codegen-2",
        "repair1",
        "final",
    ]
    assert detail["snapshots"][0]["main_st"] == "PROGRAM Main v1 END_PROGRAM\n"
    assert detail["frames"] == ["start_latches_lamp_0_lamp_red_0.jpg"]
    vision = detail["report"]["tests"][0]["expectations"][0]["vision"]
    assert vision["bbox"] == [10, 20, 30, 40]


def test_run_detail_pending_without_report(base_url: str) -> None:
    detail = _get_json(base_url, "/api/runs/20261102T130000Z-lamp_seq")
    assert isinstance(detail, dict)
    assert detail["report"] is None
    assert detail["pending"]["task_id"] == "lamp_seq"
    assert detail["pending"]["main_st"].startswith("PROGRAM Main")


def test_unknown_run_is_404(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(base_url + "/api/runs/no-such-run", timeout=5)
    assert exc_info.value.code == 404


def test_diff_between_snapshots(base_url: str) -> None:
    diff = _get_json(
        base_url, "/api/runs/20261102T120000Z-start_lamp_arm/diff?from=codegen-1&to=final"
    )
    assert isinstance(diff, dict)
    assert "-PROGRAM Main v1 END_PROGRAM" in diff["diffs"]["main.st"]
    assert "+PROGRAM Main v3 END_PROGRAM" in diff["diffs"]["main.st"]


def test_diff_rejects_unknown_snapshot(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            base_url + "/api/runs/20261102T120000Z-start_lamp_arm/diff?from=codegen-1&to=../../x",
            timeout=5,
        )
    assert exc_info.value.code == 400


def test_frame_served_and_traversal_rejected(base_url: str) -> None:
    run = "20261102T120000Z-start_lamp_arm"
    frame = "start_latches_lamp_0_lamp_red_0.jpg"
    blob, content_type, _ = _get_bytes(base_url, f"/api/runs/{run}/frames/{frame}")
    assert content_type == "image/jpeg" and blob == FRAME_BYTES
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            base_url + "/api/runs/20261102T120000Z-start_lamp_arm/frames/nope.jpg", timeout=5
        )
    assert exc_info.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(base_url + "/api/runs/../secret/frames/x.jpg", timeout=5)
    assert exc_info.value.code == 404


def test_approve_flow_writes_record(base_url: str, runs_dir: Path) -> None:
    status, first = _post_json(
        base_url, "/api/runs/20261102T130000Z-lamp_seq/approve", {"by": "tester"}
    )
    assert status == 200 and first["approved"] is True
    record = json.loads((runs_dir / "20261102T130000Z-lamp_seq" / "approved.json").read_text())
    assert record["by"] == "tester"
    status, second = _post_json(base_url, "/api/runs/20261102T130000Z-lamp_seq/approve", {})
    assert status == 200 and second["already"] is True
    pending = _get_json(base_url, "/api/pending")
    assert isinstance(pending, dict) and pending["pending"] == []


def test_approve_finished_run_conflicts(base_url: str) -> None:
    status, payload = _post_json(base_url, "/api/runs/20261102T120000Z-start_lamp_arm/approve", {})
    assert status == 409
    assert isinstance(payload, dict) and "error" in payload


def test_preview_falls_back_to_last_run_frame(base_url: str) -> None:
    blob, content_type, headers = _get_bytes(base_url, "/api/preview.jpg")
    assert content_type == "image/jpeg" and blob == FRAME_BYTES
    assert headers.get("x-frame-source") == "last-run"


def test_preview_without_frames_serves_placeholder(tmp_path: Path) -> None:
    empty = tmp_path / "empty-runs"
    empty.mkdir()
    server = create_server(empty, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        blob, content_type, headers = _get_bytes(base, "/api/preview.jpg")
        assert content_type == "image/png" and blob == PLACEHOLDER_PNG
        assert headers.get("x-frame-source") == "none"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_preview_proxies_live_frame_service(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class StubFrames(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            assert self.path == "/frame"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FRAME_BYTES)))
            self.end_headers()
            self.wfile.write(FRAME_BYTES)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubFrames)
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    runs = tmp_path / "runs"
    runs.mkdir()
    server = create_server(
        runs,
        host="127.0.0.1",
        port=0,
        frames_url=f"http://127.0.0.1:{stub.server_address[1]}",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        blob, content_type, headers = _get_bytes(base, "/api/preview.jpg")
        assert content_type == "image/jpeg" and blob == FRAME_BYTES
        assert headers.get("x-frame-source") == "live"
    finally:
        server.shutdown()
        stub.shutdown()
        server.server_close()
        stub.server_close()


def test_wait_for_dashboard_approval_reads_click(tmp_path: Path) -> None:
    spec = load_task_spec(ROOT / "specs" / "start_lamp_arm.yaml")
    check_against_io_map(spec, load_io_map())
    run_dir = tmp_path / "run"
    (run_dir).mkdir()
    (run_dir / "approved.json").write_text('{"approved": true}\n', encoding="utf-8")
    waiter = wait_for_dashboard_approval(run_dir, spec, timeout_s=5)
    assert waiter(ProgramFiles(main_st="m", tests_st="t"), "diagram") is True
    pending = json.loads((run_dir / "pending_approval.json").read_text())
    assert pending["task_id"] == "start_lamp_arm"
    assert pending["main_st"] == "m" and pending["diagram"] == "diagram"


def test_wait_for_dashboard_approval_times_out(tmp_path: Path) -> None:
    spec = load_task_spec(ROOT / "specs" / "start_lamp_arm.yaml")
    waiter = wait_for_dashboard_approval(
        tmp_path / "run", spec, timeout_s=0, poll_s=0, sleep_fn=lambda s: None
    )
    assert waiter(ProgramFiles(main_st="m", tests_st="t"), "diagram") is False
