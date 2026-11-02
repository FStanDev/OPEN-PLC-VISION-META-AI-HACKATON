"""Dashboard: one web page over run evidence (milestone 7).

Stdlib-only HTTP server: no new dependencies. Serves
:file:`dashboard/index.html` plus a small JSON API over ``runs/<id>/``
artefacts: report, spec, ST snapshots with diffs, frames, a live camera
preview, and the SPEC step-4 approve button.

Run from the project root::

    uv run python -m dashboard.server --runs-dir runs --port 8000

then open http://127.0.0.1:8000/ . Approval flow: start the loop with
``--wait-approval 300``, open the dashboard, review the pending run and
click Approve.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import threading
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INDEX_PATH = Path(__file__).resolve().parent / "index.html"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
FRAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(jpg|jpeg|png)")
PREVIEW_TIMEOUT_S = 3.0


def _placeholder_png() -> bytes:
    """A 1x1 PNG served when no camera and no run frames exist."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x20\x26\x2e")  # filter 0 + one dark-slate pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PLACEHOLDER_PNG = _placeholder_png()


def snapshot_dirs(run_dir: Path) -> list[tuple[str, str, Path]]:
    """Ordered (id, label, dir) snapshots holding main.st/tests.st."""
    found: list[tuple[str, str, Path]] = []
    attempts = run_dir / "codegen" / "attempts"
    if attempts.is_dir():
        for child in sorted(attempts.iterdir(), key=lambda p: p.name):
            if child.is_dir() and (child / "main.st").is_file():
                num = child.name.removeprefix("attempt")
                found.append((f"codegen-{num}", f"codegen attempt {num}", child))
    for child in sorted(run_dir.iterdir(), key=lambda p: p.name):
        if child.is_dir() and child.name.startswith("repair") and (child / "main.st").is_file():
            found.append((child.name, f"physical {child.name}", child))
    if (run_dir / "main.st").is_file():
        found.append(("final", "final", run_dir))
    return found


def read_text_limited(path: Path, limit: int = 200_000) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) > limit:
        text = text[:limit] + f"\n… truncated ({len(text)} chars total)"
    return text


def read_json(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_summary(run_dir: Path) -> dict[str, object]:
    report = read_json(run_dir / "report.json")
    summary: dict[str, object] = {
        "run_id": run_dir.name,
        "pending_approval": (run_dir / "pending_approval.json").is_file()
        and not (run_dir / "approved.json").is_file(),
        "approved": (run_dir / "approved.json").is_file(),
    }
    if report is None:
        summary.update(
            {"task_id": None, "verdict": "awaiting_approval", "passed": False, "tests": []}
        )
        pending = read_json(run_dir / "pending_approval.json")
        if isinstance(pending, dict):
            summary["task_id"] = pending.get("task_id")
        return summary
    tests = report.get("tests")
    summary.update(
        {
            "task_id": report.get("task_id"),
            "verdict": report.get("verdict"),
            "passed": report.get("passed"),
            "approved": report.get("approved"),
            "deployed": report.get("deployed"),
            "mode": report.get("mode"),
            "tests": [
                {"name": t.get("name"), "passed": t.get("passed")}
                for t in tests
                if isinstance(t, dict)
            ]
            if isinstance(tests, list)
            else [],
        }
    )
    return summary


def run_detail(run_dir: Path) -> dict[str, object]:
    detail: dict[str, object] = run_summary(run_dir)
    detail["report"] = read_json(run_dir / "report.json")
    detail["pending"] = read_json(run_dir / "pending_approval.json")
    detail["approval"] = read_json(run_dir / "approved.json")
    detail["spec_yaml"] = read_text_limited(run_dir / "spec.yaml")
    detail["diagram"] = read_text_limited(run_dir / "diagram.mmd")
    detail["summary_txt"] = read_text_limited(run_dir / "summary.txt")
    detail["codegen_report"] = read_json(run_dir / "codegen_report.json")
    snapshots = []
    for snap_id, label, snap_dir in snapshot_dirs(run_dir):
        snapshots.append(
            {
                "id": snap_id,
                "label": label,
                "main_st": read_text_limited(snap_dir / "main.st"),
                "tests_st": read_text_limited(snap_dir / "tests.st"),
            }
        )
    detail["snapshots"] = snapshots
    frames_dir = run_dir / "frames"
    frames = (
        sorted(p.name for p in frames_dir.iterdir() if FRAME_RE.fullmatch(p.name))
        if frames_dir.is_dir()
        else []
    )
    detail["frames"] = frames
    return detail


def unified_diff(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


class DashboardState:
    def __init__(self, runs_dir: Path, frames_url: str | None) -> None:
        self.runs_dir = runs_dir
        self.frames_url = (frames_url or "").rstrip("/")
        self._lock = threading.Lock()

    def run_dir(self, run_id: str) -> Path | None:
        if not RUN_ID_RE.fullmatch(run_id):
            return None
        candidate = self.runs_dir / run_id
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.runs_dir.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_dir() else None

    def list_runs(self) -> list[dict[str, object]]:
        if not self.runs_dir.is_dir():
            return []
        runs = [p for p in self.runs_dir.iterdir() if p.is_dir()]
        runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [run_summary(p) for p in runs]

    def approve(self, run_dir: Path, by: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            if not (run_dir / "pending_approval.json").is_file():
                return 409, {"error": "run is not awaiting approval"}
            approved_path = run_dir / "approved.json"
            if approved_path.is_file():
                return 200, {"approved": True, "already": True}
            if (run_dir / "report.json").is_file():
                return 409, {"error": "run already finished"}
            record = {
                "approved": True,
                "at": datetime.now(UTC).isoformat(),
                "by": by or "dashboard",
            }
            approved_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            return 200, {"approved": True, "record": record}

    def newest_frame(self) -> bytes | None:
        newest: Path | None = None
        newest_mtime = -1.0
        if self.runs_dir.is_dir():
            for frames_dir in self.runs_dir.glob("*/frames"):
                if not frames_dir.is_dir():
                    continue
                for frame in frames_dir.iterdir():
                    if not FRAME_RE.fullmatch(frame.name):
                        continue
                    try:
                        mtime = frame.stat().st_mtime
                    except OSError:
                        continue
                    if mtime > newest_mtime:
                        newest, newest_mtime = frame, mtime
        if newest is None:
            return None
        try:
            return newest.read_bytes()
        except OSError:
            return None

    def live_frame(self) -> tuple[bytes, str, str]:
        """Best camera bytes: live frame service, newest run frame, placeholder."""
        if self.frames_url:
            try:
                with urllib.request.urlopen(
                    f"{self.frames_url}/frame", timeout=PREVIEW_TIMEOUT_S
                ) as response:
                    blob = response.read(5_000_000)
                if blob:
                    return blob, "image/jpeg", "live"
            except Exception:
                pass
        cached = self.newest_frame()
        if cached is not None:
            return cached, "image/jpeg", "last-run"
        return PLACEHOLDER_PNG, "image/png", "none"


class DashboardHandler(BaseHTTPRequestHandler):
    state: DashboardState

    server_version = "VisionPlcDashboard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep demo output clean; errors still raise

    def _send_json(self, status: int, payload: object) -> None:
        blob = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _send_bytes(
        self, blob: bytes, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            try:
                self._send_bytes(INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send_json(500, {"error": "index.html missing"})
            return
        if path == "/api/runs":
            self._send_json(200, {"runs": self.state.list_runs()})
            return
        if path == "/api/pending":
            pending = [r for r in self.state.list_runs() if r.get("pending_approval")]
            self._send_json(200, {"pending": pending})
            return
        if path == "/api/preview.jpg":
            blob, content_type, source = self.state.live_frame()
            self._send_bytes(blob, content_type, {"X-Frame-Source": source})
            return
        match = re.fullmatch(r"/api/runs/([^/]+)", path)
        if match:
            run_dir = self.state.run_dir(match.group(1))
            if run_dir is None:
                self._send_json(404, {"error": "unknown run"})
                return
            self._send_json(200, run_detail(run_dir))
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/diff", path)
        if match:
            run_dir = self.state.run_dir(match.group(1))
            if run_dir is None:
                self._send_json(404, {"error": "unknown run"})
                return
            self._diff(run_dir, parse_qs(parsed.query))
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/frames/([^/]+)", path)
        if match:
            run_dir = self.state.run_dir(match.group(1))
            name = match.group(2)
            if run_dir is None or not FRAME_RE.fullmatch(name):
                self._send_json(404, {"error": "unknown frame"})
                return
            frame_path = run_dir / "frames" / name
            try:
                blob = frame_path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "unknown frame"})
                return
            content = "image/png" if name.endswith(".png") else "image/jpeg"
            self._send_bytes(blob, content)
            return
        self._send_json(404, {"error": "not found"})

    def _diff(self, run_dir: Path, query: dict[str, list[str]]) -> None:
        by_id = {snap_id: snap_dir for snap_id, _, snap_dir in snapshot_dirs(run_dir)}
        old_id = (query.get("from") or [""])[0]
        new_id = (query.get("to") or [""])[0]
        if old_id not in by_id or new_id not in by_id:
            self._send_json(400, {"error": f"unknown snapshot; have: {sorted(by_id)}"})
            return
        diffs = {}
        for filename in ("main.st", "tests.st"):
            old = read_text_limited(by_id[old_id] / filename) or ""
            new = read_text_limited(by_id[new_id] / filename) or ""
            diffs[filename] = unified_diff(old, new, filename)
        self._send_json(200, {"from": old_id, "to": new_id, "diffs": diffs})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/runs/([^/]+)/approve", parsed.path)
        if not match:
            self._send_json(404, {"error": "not found"})
            return
        run_dir = self.state.run_dir(match.group(1))
        if run_dir is None:
            self._send_json(404, {"error": "unknown run"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        by = ""
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
                if isinstance(body, dict) and isinstance(body.get("by"), str):
                    by = body["by"][:64]
            except (ValueError, UnicodeDecodeError):
                pass
        status, payload = self.state.approve(run_dir, by)
        self._send_json(status, payload)


def create_server(
    runs_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    frames_url: str | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard server."""
    state = DashboardState(Path(runs_dir), frames_url)

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.state = state
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the vision-plc-agent dashboard.")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--frames-url",
        default=os.environ.get("FRAMES_URL", ""),
        help="Pi frame service base URL (default: $FRAMES_URL, else last run frame)",
    )
    args = parser.parse_args(argv)
    server = create_server(
        args.runs_dir, host=args.host, port=args.port, frames_url=args.frames_url or None
    )
    print(f"dashboard: http://{args.host}:{args.port}/  (runs: {args.runs_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
