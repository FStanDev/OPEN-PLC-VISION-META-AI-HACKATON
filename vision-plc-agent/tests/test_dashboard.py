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
from dashboard.server import PLACEHOLDER_PNG, DashboardState, create_server

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


@pytest.fixture
def task_server(tmp_path: Path):
    runs = tmp_path / "task-runs"
    runs.mkdir()
    specs = tmp_path / "task-specs"
    specs.mkdir()
    server = create_server(runs, host="127.0.0.1", port=0, specs_dir=specs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", specs, runs
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


VALID_TESTS_YAML = """\
- name: start_latches_lamp
  stimulus:
    - {at_ms: 0, press: start_btn}
    - {at_ms: 200, release: start_btn}
  expect:
    - {signal: lamp_red, value: true, within_ms: 500}
    - {signal: arm_angle, value: 90, tolerance: 10, within_ms: 1500}
"""


def _valid_task(task_id: str = "smoke_task") -> dict[str, object]:
    return {
        "task_id": task_id,
        "description": "Latch the red lamp and move the arm.",
        "signals": ["start_btn", "stop_btn", "lamp_red", "arm_angle"],
        "tests": VALID_TESTS_YAML,
    }


def _wait_for_report(base: str, run_id: str, timeout_s: float = 120.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            detail = _get_json(base, f"/api/runs/{run_id}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            time.sleep(0.5)
            continue
        assert isinstance(detail, dict)
        if detail.get("report"):
            return detail
        time.sleep(1.0)
    raise AssertionError(f"run {run_id} produced no report within {timeout_s}s")


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


def test_generating_run_shows_stage(base_url: str, runs_dir: Path) -> None:
    run = runs_dir / "20261102T140000Z-gen"
    run.mkdir()
    (run / "status.json").write_text(
        json.dumps({"run_id": run.name, "stage": "generating", "at": "2026-11-02T14:00:00"}),
        encoding="utf-8",
    )
    payload = _get_json(base_url, "/api/runs")
    assert isinstance(payload, dict)
    by_id = {r["run_id"]: r for r in payload["runs"]}
    assert by_id[run.name]["stage"] == "generating"
    assert by_id[run.name]["verdict"] == "generating"
    detail = _get_json(base_url, f"/api/runs/{run.name}")
    assert isinstance(detail, dict)
    assert detail["status"]["stage"] == "generating"
    assert detail["report"] is None


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


def test_io_map_lists_signals(base_url: str) -> None:
    payload = _get_json(base_url, "/api/io-map")
    assert isinstance(payload, dict)
    by_name = {s["name"]: s for s in payload["signals"]}
    assert by_name["lamp_red"]["iec"] == "%QX0.0"
    assert by_name["start_btn"]["direction"] == "input"
    assert payload["safe_state"]["lamp_red"] is False


def test_create_task_writes_validated_spec(task_server: tuple) -> None:
    base, specs, _ = task_server
    status, payload = _post_json(base, "/api/tasks", _valid_task())
    assert status == 200
    assert isinstance(payload, dict) and payload["task_id"] == "smoke_task"
    spec = load_task_spec(specs / "smoke_task.yaml")
    assert spec.task_id == "smoke_task" and len(spec.tests) == 1
    assert spec.tests[0].expect[1].value == 90


def test_create_task_rejects_bad_specs(task_server: tuple) -> None:
    base, _, _ = task_server
    bad_id = _valid_task(task_id="../evil")
    assert _post_json(base, "/api/tasks", bad_id)[0] == 400
    bad_signal = _valid_task()
    bad_signal["signals"] = ["no_such_signal"]
    status, payload = _post_json(base, "/api/tasks", bad_signal)
    assert status == 400 and "unknown signal" in str(payload)
    bad_yaml = _valid_task()
    bad_yaml["tests"] = "- name: [unclosed"
    assert _post_json(base, "/api/tasks", bad_yaml)[0] == 400
    bad_deadline = _valid_task()
    bad_deadline["tests"] = "- {name: t, expect: [{signal: lamp_red, value: true}]}"
    status, payload = _post_json(base, "/api/tasks", bad_deadline)
    assert status == 400 and "within_ms" in str(payload)


def test_create_task_conflict_needs_overwrite(task_server: tuple) -> None:
    base, _, _ = task_server
    assert _post_json(base, "/api/tasks", _valid_task())[0] == 200
    assert _post_json(base, "/api/tasks", _valid_task())[0] == 409
    retry = _valid_task()
    retry["overwrite"] = True
    assert _post_json(base, "/api/tasks", retry)[0] == 200


def test_tasks_without_specs_dir_is_500(base_url: str) -> None:
    assert _post_json(base_url, "/api/tasks", _valid_task())[0] == 500
    assert _post_json(base_url, "/api/runs/start", {"task_id": "x"})[0] == 500


def test_start_run_fake_completes(task_server: tuple) -> None:
    base, _, _ = task_server
    assert _post_json(base, "/api/tasks", _valid_task())[0] == 200
    status, payload = _post_json(
        base, "/api/runs/start", {"task_id": "smoke_task", "mode": "fake", "run_id": "demo1"}
    )
    assert status == 200
    assert isinstance(payload, dict) and payload["run_id"] == "demo1"
    detail = _wait_for_report(base, "demo1")
    report = detail["report"]
    assert report["verdict"] == "pass" and report["passed"] is True
    assert detail["status"]["stage"] == "done"
    assert "PROGRAM Main" in detail["snapshots"][-1]["main_st"]


def test_start_run_guards_busy_and_unknown(task_server: tuple) -> None:
    base, _, _ = task_server
    assert _post_json(base, "/api/tasks", _valid_task())[0] == 200
    assert _post_json(base, "/api/tasks", _valid_task(task_id="second"))[0] == 200
    first = _post_json(
        base, "/api/runs/start", {"task_id": "smoke_task", "mode": "fake", "run_id": "busy1"}
    )
    assert first[0] == 200
    try:
        status, payload = _post_json(
            base, "/api/runs/start", {"task_id": "second", "mode": "fake", "run_id": "busy2"}
        )
        assert status == 409 and "still active" in str(payload)
        assert _post_json(base, "/api/runs/start", {"task_id": "nope"})[0] == 404
        bad_mode = _post_json(base, "/api/runs/start", {"task_id": "second", "mode": "quantum"})
        assert bad_mode[0] == 400
    finally:
        _wait_for_report(base, "busy1")


def test_config_reports_default_pi_host(tmp_path: Path) -> None:
    server = create_server(tmp_path, host="127.0.0.1", port=0, pi_host="10.0.0.7")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert _get_json(base, "/api/config") == {"pi_host": "10.0.0.7"}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_start_run_real_passes_pi_host_and_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.loop

    seen: list[list[str]] = []
    monkeypatch.setattr(agent.loop, "main", lambda argv: seen.append(argv) or 0)
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "t1.yaml").write_text("task_id: t1\n", encoding="utf-8")
    state = DashboardState(
        tmp_path / "runs",
        "http://raspberrypi:8082",
        specs,
        calibration=tmp_path / "cal.json",
    )
    status, payload = state.start_run(
        {"task_id": "t1", "mode": "real", "pi_host": " 10.0.0.7 ", "run_id": "r1"}
    )
    assert status == 200, payload
    state._jobs["r1"]["thread"].join(timeout=5)  # type: ignore[union-attr]
    argv = seen[0]
    assert argv[argv.index("--pi-host") + 1] == "10.0.0.7"
    assert argv[argv.index("--calibration") + 1] == str(tmp_path / "cal.json")
    assert state.frames_url == "http://10.0.0.7:8082"
    assert state.pi_host == "10.0.0.7"


def test_start_run_rejects_bad_pi_host(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "t1.yaml").write_text("task_id: t1\n", encoding="utf-8")
    state = DashboardState(tmp_path / "runs", None, specs)
    status, _ = state.start_run({"task_id": "t1", "mode": "real", "pi_host": "evil/x"})
    assert status == 400


def test_preview_backs_off_after_camera_failure(tmp_path: Path) -> None:
    state = DashboardState(tmp_path, "http://127.0.0.1:9")  # nothing listens on port 9
    assert state.live_frame()[2] == "none"
    assert state._live_retry_at > 0
    state.frames_url = "http://unused.invalid"
    blob, _, source = state.live_frame()  # within the back-off: no network call
    assert source == "none" and blob == PLACEHOLDER_PNG


def _plc_state(tmp_path: Path):
    from agent.plc_client import FakePlcClient
    from agent.tag_writer import FakeTagWriter

    plc, writer = FakePlcClient(), FakeTagWriter()
    hosts: list[str] = []
    state = DashboardState(tmp_path / "runs", None, pi_host="10.0.0.7")

    def factory(host: str):
        hosts.append(host)
        return plc, writer

    state.plc_factory = factory
    return state, plc, writer, hosts


def test_plc_start_stop_status(tmp_path: Path) -> None:
    state, plc, writer, hosts = _plc_state(tmp_path)
    assert state.plc_control("start", "") == (200, {"running": True, "pi_host": "10.0.0.7"})
    status, payload = state.plc_control("stop", "10.0.0.9")
    assert status == 200 and payload["running"] is False
    assert ("lamp_red", False) in writer.writes and ("arm_angle", 0) in writer.writes
    assert state.plc_control("status", "")[1]["running"] is False
    assert hosts == ["10.0.0.7", "10.0.0.9", "10.0.0.7"]


def test_plc_control_rejects_bad_input_and_reports_errors(tmp_path: Path) -> None:
    state, _, _, _ = _plc_state(tmp_path)
    assert state.plc_control("reboot", "")[0] == 400
    assert state.plc_control("start", "evil/x")[0] == 400

    def broken(host: str):
        raise OSError("no route to host")

    state.plc_factory = broken
    status, payload = state.plc_control("status", "")
    assert status == 502 and "no route" in str(payload["error"])


def _brief_task(task_id: str = "brief_task") -> dict[str, object]:
    return {
        "task_id": task_id,
        "description": "When start is pressed, latch the green lamp on. Stop turns "
        "everything off leaving red lamp on for 3 seconds.",
        "signals": ["start_btn", "stop_btn", "lamp_red", "lamp_green"],
        "tests": "  \n",
    }


def _wait_for(base: str, run_id: str, key: str, timeout_s: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            detail = _get_json(base, f"/api/runs/{run_id}")
        except urllib.error.HTTPError:
            time.sleep(0.3)
            continue
        assert isinstance(detail, dict)
        if detail.get(key):
            return detail
        time.sleep(0.3)
    raise AssertionError(f"run {run_id}: {key} not set within {timeout_s}s")


def test_create_task_without_tests_writes_brief(task_server: tuple) -> None:
    base, specs, _ = task_server
    status, payload = _post_json(base, "/api/tasks", _brief_task())
    assert status == 200 and isinstance(payload, dict) and payload["draft_tests"] is True
    text = (specs / "brief_task.yaml").read_text(encoding="utf-8")
    assert "tests" not in text and "lamp_green" in text
    bad = _brief_task()
    bad["signals"] = ["lamp_blue"]
    assert _post_json(base, "/api/tasks", bad)[0] == 400


def test_brief_run_drafts_tests_then_code_then_deploys(task_server: tuple) -> None:
    base, specs, _ = task_server
    assert _post_json(base, "/api/tasks", _brief_task())[0] == 200
    status, _ = _post_json(
        base, "/api/runs/start", {"task_id": "brief_task", "mode": "fake", "run_id": "draft1"}
    )
    assert status == 200
    detail = _wait_for(base, "draft1", "pending_tests")
    assert detail["verdict"] == "awaiting_tests_approval"
    draft = detail["tests_draft"]
    assert draft["brief"]["task_id"] == "brief_task" and "lamp_green" in draft["tests_yaml"]
    pending = _get_json(base, "/api/pending")
    assert isinstance(pending, dict) and pending["pending"][0]["pending_tests"] is True
    # Code approval is not open yet; a broken edit is refused with the validator's words.
    assert _post_json(base, "/api/runs/draft1/approve", {})[0] == 409
    status, payload = _post_json(
        base, "/api/runs/draft1/approve-tests", {"tests_yaml": "- {name: t, expect: []}"}
    )
    assert status == 400 and "expect" in str(payload)
    status, payload = _post_json(
        base, "/api/runs/draft1/approve-tests", {"tests_yaml": draft["tests_yaml"], "by": "me"}
    )
    assert status == 200 and isinstance(payload, dict) and payload["approved"] is True
    assert _post_json(base, "/api/runs/draft1/approve-tests", {"tests_yaml": "x"})[0] == 409
    assert "tests:" in (specs / "brief_task.yaml").read_text(encoding="utf-8")
    # Then the usual code gate.
    _wait_for(base, "draft1", "pending_approval")
    assert _post_json(base, "/api/runs/draft1/approve", {"by": "me"})[0] == 200
    report = _wait_for_report(base, "draft1")["report"]
    assert report["verdict"] == "pass"
    assert report["codegen"]["acceptance"]["approval"]["by"] == "me"


def test_brief_run_reject_tests_ends_run(task_server: tuple) -> None:
    base, _, _ = task_server
    assert _post_json(base, "/api/tasks", _brief_task("rej_task"))[0] == 200
    assert (
        _post_json(
            base, "/api/runs/start", {"task_id": "rej_task", "mode": "fake", "run_id": "rej1"}
        )[0]
        == 200
    )
    _wait_for(base, "rej1", "pending_tests")
    assert _post_json(base, "/api/runs/rej1/reject-tests", {})[0] == 200
    report = _wait_for_report(base, "rej1", timeout_s=30)["report"]
    assert report["verdict"] == "tests_not_approved" and report["deployed"] is False
    assert _post_json(base, "/api/runs/rej1/reject-tests", {})[0] == 409
