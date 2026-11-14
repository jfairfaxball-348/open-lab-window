"""Fixtures: real archives copied into a temporary data root, and a writer for synthetic ones.

``ArchiveWriter`` plays the part of the Simulation's recorder for the tests. It
is test code only; the Window itself has nothing that can write.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from open_lab_window.archive import Library
from open_lab_window.server import make_server

FIXTURE_RUNS = Path(__file__).parent / "fixtures" / "runs"
SMOKE = ("scripted-smoke", "run-20261113t233053z-1056ef")
MODEL_CHECK = ("local-model-check", "run-20261114t024819z-09ad2e")

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082"
)


def canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ArchiveWriter:
    """Writes a run archive the way OBSERVATION.md describes, one event at a time."""

    def __init__(self, data_root: Path, experiment_id: str, run_id: str):
        self.experiment_id = experiment_id
        self.run_id = run_id
        self.directory = data_root / "runs" / experiment_id / run_id
        (self.directory / "artifacts").mkdir(parents=True)
        shutil.copytree(FIXTURE_RUNS / SMOKE[0] / SMOKE[1] / "contracts",
                        self.directory / "contracts")
        self.count = 0
        self.previous: str | None = None
        self.by_type: dict[str, int] = {}
        self.artifacts = 0
        (self.directory / "experiment.json").write_text(json.dumps({
            "schema": "open-lab/experiment-config", "schema_version": "1.0.0",
            "experiment_id": experiment_id, "title": "Synthetic test run",
            "initial_prompt": "You are in a test.",
            "world": {"world_id": "minimal", "version": "0.2.0"},
            "orchestrator": {"provider_id": "scripted", "model_id": "scripted"},
            "limits": {"wall_clock_s": 60},
        }), encoding="utf-8")
        (self.directory / "events.jsonl").write_bytes(b"")

    def event(self, type: str, payload: dict[str, Any], *, status: str = "observed",
              correlation_id: str | None = None, recorder: str = "supervisor",
              **extra: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema": "open-lab/observation-event", "schema_version": "1.0.0",
            "event_id": f"evt-{self.count:06d}", "experiment_id": self.experiment_id,
            "run_id": self.run_id, "seq": self.count,
            "timestamp": f"2026-11-14T03:00:{self.count % 60:02d}.000Z",
            "monotonic_ms": self.count * 1000, "recorder": recorder, "type": type,
            "epistemic_status": status, "payload": payload, **extra,
        }
        if correlation_id:
            event["correlation_id"] = correlation_id
        if self.previous is not None:
            event["prev_event_sha256"] = hashlib.sha256(self.previous.encode()).hexdigest()
        line = canonical(event)
        self.append_raw((line + "\n").encode())
        self.previous = line
        self.count += 1
        self.by_type[type] = self.by_type.get(type, 0) + 1
        return event

    def append_raw(self, data: bytes) -> None:
        with (self.directory / "events.jsonl").open("ab") as handle:
            handle.write(data)

    def artifact(self, path: str, content: bytes, *, media_type: str | None = None,
                 preserved: bool = True, **extra: Any) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self.artifacts += 1
        artifact_id = f"art-{self.artifacts:06d}"
        event = self.event("artifact.created", {
            "artifact_id": artifact_id, "path": path, "sha256": digest,
            "size_bytes": len(content)})
        record = {
            "schema": "open-lab/artifact-metadata", "schema_version": "1.0.0",
            "artifact_id": artifact_id, "experiment_id": self.experiment_id,
            "run_id": self.run_id, "workspace_path": path, "preserved": preserved,
            "sha256": digest, "size_bytes": len(content), "observed_at": event["timestamp"],
            "observed_at_seq": event["seq"], "revision": 1, "producer": {"kind": "tool"}, **extra,
        }
        if media_type:
            record["media_type"] = media_type
        if preserved:
            record["archive_path"] = f"artifacts/sha256/{digest[:2]}/{digest}"
            stored = self.directory / record["archive_path"]
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(content)
        with (self.directory / "artifacts" / "index.jsonl").open("ab") as handle:
            handle.write((canonical(record) + "\n").encode())
        return artifact_id

    def start(self) -> None:
        self.event("run.state_changed", {"to": "created", "reason": "launch"},
                   recorder="experiment_manager")
        self.event("run.state_changed", {"from": "created", "to": "running", "reason": "started"})

    def finish(self, state: str = "completed") -> None:
        self.event("run.state_changed", {"from": "running", "to": state, "reason": "test ended"})
        (self.directory / "summary.json").write_text(json.dumps({
            "experiment_id": self.experiment_id, "run_id": self.run_id, "final_state": state,
            "reason": "test ended", "started_at": "2026-11-14T03:00:00.000Z",
            "ended_at": "2026-11-14T03:00:59.000Z", "event_count": self.count,
            "events_by_type": self.by_type,
            "last_event_sha256": hashlib.sha256(self.previous.encode()).hexdigest(),
        }), encoding="utf-8")


class Client:
    """A minimal HTTP client for the tests. Returns (status, headers, body)."""

    def __init__(self, base: str):
        self.base = base

    def request(self, path: str, method: str = "GET", body: bytes | None = None,
                headers: dict[str, str] | None = None):
        request = urllib.request.Request(self.base + path, method=method, data=body,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def json(self, path: str) -> Any:
        status, _, body = self.request(path)
        assert status == 200, (path, status, body[:300])
        return json.loads(body)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """A data root holding copies of the two real archives."""
    root = tmp_path / "open-lab-data"
    shutil.copytree(FIXTURE_RUNS, root / "runs")
    return root


@pytest.fixture
def writer(data_root: Path):
    def make(experiment_id: str = "synthetic", run_id: str = "run-20261114t030000z-aaaaaa"):
        return ArchiveWriter(data_root, experiment_id, run_id)
    return make


@pytest.fixture
def client(data_root: Path):
    server = make_server(Library(data_root), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_url(run: tuple[str, str]) -> str:
    return f"/api/runs/{run[0]}/{run[1]}"
