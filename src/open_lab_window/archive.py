"""Read run archives under ``<data root>/runs`` without ever writing to them.

The layout and the log format are specified in the Builder's
``docs/architecture/OBSERVATION.md``. This module is the Window's own reader:
it shares no code with the Simulation that writes the archives.

Two constraints shape everything here:

* **Nothing is written.** Every file is opened through :func:`open_readonly`.
  There is no cache on disk; what is remembered about a log (line offsets and
  event types) lives in memory and is rebuilt on restart.
* **A log may be huge, and may be growing.** A log is never loaded whole. Only
  complete lines (ending in a line feed) are ever read, so a line the recorder
  is in the middle of writing is not shown until it is finished.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

# The contracts' slug rule. Checking it before touching the disk also means a
# request can never name a path outside the runs directory.
SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

TERMINAL_STATES = frozenset({"completed", "terminated", "failed"})

MAX_PAGE = 500
DEFAULT_PAGE = 100
TEXT_PREVIEW_BYTES = 256 * 1024
STDERR_PREVIEW_BYTES = 64 * 1024
MAX_REPORTED_PROBLEMS = 50

# Raster formats a browser can show, recognised by their leading bytes rather
# than by the file name an agent chose.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


class NotFound(Exception):
    """The requested run, artifact or file is not in the archive."""


class BadRequest(Exception):
    """The request's parameters are not usable."""


def data_root() -> Path:
    """``$OPEN_LAB_DATA``, or ``open-lab-data`` in the home directory."""
    configured = os.environ.get("OPEN_LAB_DATA")
    return Path(configured) if configured else Path.home() / "open-lab-data"


def open_readonly(path: Path) -> BinaryIO:
    """The only way this package opens a file. The mode is fixed."""
    return open(path, "rb")


def _read_bytes(path: Path, limit: int | None = None) -> bytes:
    with open_readonly(path) as handle:
        return handle.read() if limit is None else handle.read(limit)


def _read_json(path: Path) -> Any | None:
    """A JSON document, or ``None`` if the file is missing or not (yet) valid JSON."""
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (OSError, ValueError):
        return None


def _complete_lines(path: Path) -> list[bytes]:
    """The complete lines of a small line-oriented file; a trailing partial line is dropped."""
    try:
        data = _read_bytes(path)
    except OSError:
        return []
    end = data.rfind(b"\n")
    return [line for line in data[: end + 1].split(b"\n") if line] if end >= 0 else []


class EventLog:
    """An in-memory index of one ``events.jsonl``: where each line starts and what it is.

    The index grows as the file does; ``refresh`` reads only the bytes added
    since the last call. Event bodies are read from disk on demand.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.offsets: list[int] = []
        self.types: list[str] = []
        self.by_type: dict[str, int] = {}
        self.calls: list[tuple[int, str]] = []
        self.results: dict[str, int] = {}
        self.end = 0
        self.last_state: str | None = None
        self.last_state_reason: str | None = None
        self.first_timestamp: str | None = None
        self.last_timestamp: str | None = None
        self.unreadable = 0

    def refresh(self) -> None:
        with self._lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                self._reset()
                return
            if size < self.end:
                # Not an append: the file was replaced. Start again rather than
                # present a mixture of two logs.
                self._reset()
            if size == self.end:
                return
            with open_readonly(self.path) as handle:
                handle.seek(self.end)
                position = self.end
                pending = b""
                while chunk := handle.read(1 << 20):
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        self._add(position, line)
                        position += len(line) + 1
                self.end = position

    def _add(self, offset: int, line: bytes) -> None:
        seq = len(self.offsets)
        self.offsets.append(offset)
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except ValueError:
            self.types.append("(unreadable)")
            self.by_type["(unreadable)"] = self.by_type.get("(unreadable)", 0) + 1
            self.unreadable += 1
            return
        kind = event.get("type") if isinstance(event.get("type"), str) else "(untyped)"
        self.types.append(kind)
        self.by_type[kind] = self.by_type.get(kind, 0) + 1
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            self.first_timestamp = self.first_timestamp or timestamp
            self.last_timestamp = timestamp
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        correlation = event.get("correlation_id")
        if kind == "tool.call" and isinstance(correlation, str):
            self.calls.append((seq, correlation))
        elif kind == "tool.result" and isinstance(correlation, str):
            self.results.setdefault(correlation, seq)
        elif kind == "run.state_changed" and isinstance(payload.get("to"), str):
            self.last_state = payload["to"]
            reason = payload.get("reason")
            self.last_state_reason = reason if isinstance(reason, str) else None

    @property
    def count(self) -> int:
        return len(self.offsets)

    def read(self, seqs: list[int]) -> list[dict[str, Any]]:
        """The events on the given lines. A line that is not a JSON object is reported as such."""
        if not seqs:
            return []
        with self._lock:
            spans = [
                (
                    seq,
                    self.offsets[seq],
                    (self.offsets[seq + 1] if seq + 1 < len(self.offsets) else self.end) - 1,
                )
                for seq in seqs
                if 0 <= seq < len(self.offsets)
            ]
        events: list[dict[str, Any]] = []
        with open_readonly(self.path) as handle:
            for seq, start, stop in spans:
                handle.seek(start)
                line = handle.read(stop - start)
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("not an object")
                except ValueError:
                    event = {
                        "seq": seq,
                        "type": "(unreadable)",
                        "unreadable": True,
                        "raw": line[:2000].decode("utf-8", errors="replace"),
                    }
                events.append(event)
        return events


class Library:
    """Every run archive under one data root."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else data_root()
        self.runs_root = self.root / "runs"
        self._logs: dict[tuple[str, str], EventLog] = {}
        self._lock = threading.Lock()

    # -- locating ---------------------------------------------------------

    def _run_dir(self, experiment_id: str, run_id: str) -> Path:
        if not (SLUG.match(experiment_id) and SLUG.match(run_id)):
            raise NotFound("no such run")
        directory = self.runs_root / experiment_id / run_id
        if not directory.is_dir():
            raise NotFound("no such run")
        # A link placed inside the runs directory must not lead the Window
        # to read somewhere else.
        if not directory.resolve().is_relative_to(self.runs_root.resolve()):
            raise NotFound("no such run")
        return directory

    def _log(self, experiment_id: str, run_id: str) -> EventLog:
        directory = self._run_dir(experiment_id, run_id)
        with self._lock:
            log = self._logs.get((experiment_id, run_id))
            if log is None:
                log = self._logs[(experiment_id, run_id)] = EventLog(directory / "events.jsonl")
        log.refresh()
        return log

    # -- runs -------------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        if not self.runs_root.is_dir():
            return runs
        for experiment in sorted(self.runs_root.iterdir()):
            if not (experiment.is_dir() and SLUG.match(experiment.name)):
                continue
            for run in sorted(experiment.iterdir()):
                if run.is_dir() and SLUG.match(run.name):
                    try:
                        runs.append(self._overview(experiment.name, run.name))
                    except NotFound:
                        continue
        # Run identifiers begin with their launch time, so this is newest first.
        runs.sort(key=lambda run: (run["started_at"] or "", run["run_id"]), reverse=True)
        return runs

    def _overview(self, experiment_id: str, run_id: str) -> dict[str, Any]:
        directory = self._run_dir(experiment_id, run_id)
        summary = _read_json(directory / "summary.json")
        summary = summary if isinstance(summary, dict) else None
        experiment = _read_json(directory / "experiment.json")
        experiment = experiment if isinstance(experiment, dict) else {}
        overview: dict[str, Any] = {
            "experiment_id": experiment_id,
            "run_id": run_id,
            "title": experiment.get("title"),
            "world": experiment.get("world"),
            "orchestrator": experiment.get("orchestrator"),
        }
        if summary is not None:
            overview.update(
                # The Simulation writes summary.json once, after the last event.
                finished=True,
                state=summary.get("final_state"),
                reason=summary.get("reason"),
                started_at=summary.get("started_at"),
                ended_at=summary.get("ended_at"),
                last_event_at=summary.get("ended_at"),
                event_count=summary.get("event_count"),
            )
        else:
            # No summary: the run is still going, or it was interrupted before
            # it could end. The archive alone cannot tell these apart, so the
            # Window reports what the log says and when it last grew.
            log = self._log(experiment_id, run_id)
            overview.update(
                finished=False,
                state=log.last_state,
                reason=log.last_state_reason,
                started_at=log.first_timestamp,
                ended_at=None,
                last_event_at=log.last_timestamp,
                event_count=log.count,
            )
        return overview

    def run_detail(self, experiment_id: str, run_id: str) -> dict[str, Any]:
        directory = self._run_dir(experiment_id, run_id)
        log = self._log(experiment_id, run_id)
        contracts = []
        for path in sorted((directory / "contracts").glob("v*/*.schema.json")):
            schema = _read_json(path)
            contracts.append(
                {
                    "file": f"{path.parent.name}/{path.name}",
                    "version": schema.get("x-schema-version") if isinstance(schema, dict) else None,
                }
            )
        stderr_path = directory / "runtime.stderr.log"
        stderr = None
        if stderr_path.is_file():
            size = stderr_path.stat().st_size
            stderr = {
                "size_bytes": size,
                "text": _read_bytes(stderr_path, STDERR_PREVIEW_BYTES).decode(
                    "utf-8", errors="replace"
                ),
                "truncated": size > STDERR_PREVIEW_BYTES,
            }
        return {
            "run": self._overview(experiment_id, run_id),
            "summary": _read_json(directory / "summary.json"),
            "experiment": _read_json(directory / "experiment.json"),
            "world_release": _read_json(directory / "world-release.json"),
            "model": _read_json(directory / "model.json"),
            "runtime_stderr": stderr,
            "contracts": contracts,
            "events": {
                "count": log.count,
                "by_type": dict(sorted(log.by_type.items())),
                "tool_calls": len(log.calls),
            },
            "artifact_versions": len(self._artifact_index(directory)),
        }

    # -- events -----------------------------------------------------------

    def events(
        self,
        experiment_id: str,
        run_id: str,
        *,
        after: int = -1,
        limit: int = DEFAULT_PAGE,
        types: frozenset[str] | None = None,
        tail: bool = False,
    ) -> dict[str, Any]:
        """One page of the timeline, in recorded order.

        ``after`` is the last sequence number already seen; pass ``next_after``
        back to continue, which is also how a growing log is followed. With
        ``tail`` the page is the last ``limit`` matching events instead.
        """
        limit = _page_size(limit)
        log = self._log(experiment_id, run_id)
        count = log.count
        kinds = log.types

        def wanted(seq: int) -> bool:
            return types is None or kinds[seq] in types

        if tail:
            chosen: list[int] = []
            seq = count - 1
            while seq >= 0 and len(chosen) < limit:
                if wanted(seq):
                    chosen.append(seq)
                seq -= 1
            chosen.reverse()
            earlier = any(wanted(index) for index in range(seq, -1, -1))
            return {
                "events": log.read(chosen),
                "total": count,
                "next_after": count - 1,
                "has_more": False,
                "has_earlier": earlier,
                "finished": self._finished(experiment_id, run_id),
            }

        chosen = []
        seq = max(after, -1) + 1
        while seq < count and len(chosen) < limit:
            if wanted(seq):
                chosen.append(seq)
            seq += 1
        # ``seq`` is now the first line not yet examined.
        return {
            "events": log.read(chosen),
            "total": count,
            "next_after": seq - 1,
            "has_more": any(wanted(index) for index in range(seq, count)),
            "has_earlier": False,
            "finished": self._finished(experiment_id, run_id),
        }

    def _finished(self, experiment_id: str, run_id: str) -> bool:
        return (self._run_dir(experiment_id, run_id) / "summary.json").is_file()

    def tool_calls(
        self, experiment_id: str, run_id: str, *, offset: int = 0, limit: int = DEFAULT_PAGE
    ) -> dict[str, Any]:
        """Tool calls in order, each with the result that carries the same correlation id."""
        limit = _page_size(limit)
        log = self._log(experiment_id, run_id)
        page = log.calls[max(offset, 0) : max(offset, 0) + limit]
        result_seqs = [log.results[correlation] for _, correlation in page
                       if correlation in log.results]
        calls = {event.get("seq"): event for event in log.read([seq for seq, _ in page])}
        results = {event.get("seq"): event for event in log.read(result_seqs)}
        return {
            "pairs": [
                {
                    "correlation_id": correlation,
                    "call": calls.get(seq),
                    # ``None`` means no result is in the record (yet): the call
                    # may be running, or the run ended before it returned.
                    "result": results.get(log.results.get(correlation, -1)),
                }
                for seq, correlation in page
            ],
            "total": len(log.calls),
            "offset": max(offset, 0),
            "finished": self._finished(experiment_id, run_id),
        }

    # -- artifacts --------------------------------------------------------

    def _artifact_index(self, directory: Path) -> list[dict[str, Any]]:
        records = []
        for line in _complete_lines(directory / "artifacts" / "index.jsonl"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def artifacts(
        self, experiment_id: str, run_id: str, *, offset: int = 0, limit: int = DEFAULT_PAGE
    ) -> dict[str, Any]:
        limit = _page_size(limit)
        records = self._artifact_index(self._run_dir(experiment_id, run_id))
        start = max(offset, 0)
        return {
            "artifacts": records[start : start + limit],
            "total": len(records),
            "offset": start,
        }

    def _artifact_file(
        self, experiment_id: str, run_id: str, artifact_id: str
    ) -> tuple[dict[str, Any], Path | None]:
        directory = self._run_dir(experiment_id, run_id)
        for record in self._artifact_index(directory):
            if record.get("artifact_id") == artifact_id:
                break
        else:
            raise NotFound("no such artifact")
        digest = record.get("sha256")
        if record.get("preserved") is False or not (
            isinstance(digest, str) and SHA256.match(digest)
        ):
            return record, None
        # The location is derived from the digest, never taken from the
        # record, so a damaged record cannot point outside the store.
        stored = directory / "artifacts" / "sha256" / digest[:2] / digest
        if record.get("archive_path") != f"artifacts/sha256/{digest[:2]}/{digest}":
            return record, None
        return record, stored if stored.is_file() else None

    def artifact_preview(self, experiment_id: str, run_id: str, artifact_id: str) -> dict[str, Any]:
        """What the Window can show of an artifact: an image, text, or the reason for neither."""
        record, stored = self._artifact_file(experiment_id, run_id, artifact_id)
        if stored is None:
            reason = (
                "The content was not preserved (too large)."
                if record.get("preserved") is False
                else "The preserved content is missing from the archive."
            )
            return {"artifact": record, "kind": "none", "reason": reason}
        head = _read_bytes(stored, TEXT_PREVIEW_BYTES + 4)
        media_type = _image_type(head)
        if media_type:
            return {"artifact": record, "kind": "image", "media_type": media_type}
        text = _as_text(head[:TEXT_PREVIEW_BYTES])
        if text is None:
            return {
                "artifact": record,
                "kind": "none",
                "reason": "Not text and not an image format the Window displays.",
            }
        return {
            "artifact": record,
            "kind": "text",
            "text": text,
            "truncated": stored.stat().st_size > TEXT_PREVIEW_BYTES,
        }

    def artifact_image(self, experiment_id: str, run_id: str, artifact_id: str) -> tuple[str, Path]:
        """The stored file and its media type, only if its bytes are a raster image."""
        _, stored = self._artifact_file(experiment_id, run_id, artifact_id)
        media_type = _image_type(_read_bytes(stored, 16)) if stored else None
        if stored is None or media_type is None:
            raise NotFound("no image content for this artifact")
        return media_type, stored

    # -- integrity --------------------------------------------------------

    def integrity(self, experiment_id: str, run_id: str) -> dict[str, Any]:
        """Check the event log against the archive's own contracts and its hash chain.

        The rules are those of OBSERVATION.md: every complete line is canonical
        JSON, validates, carries ``seq`` equal to its line number and the
        digest of the line before it.
        """
        directory = self._run_dir(experiment_id, run_id)
        validator = _event_validator(directory / "contracts")
        problems: list[str] = []
        problem_count = 0

        def report(message: str) -> None:
            nonlocal problem_count
            problem_count += 1
            if len(problems) < MAX_REPORTED_PROBLEMS:
                problems.append(message)

        count = 0
        previous: bytes | None = None
        partial = False
        try:
            handle = open_readonly(directory / "events.jsonl")
        except OSError:
            handle = None
            report("events.jsonl is missing")
        if handle is not None:
            with handle:
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        partial = True
                        break
                    line = raw[:-1]
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("not an object")
                    except ValueError:
                        report(f"line {count}: not a JSON object")
                    else:
                        if validator is not None:
                            error = next(iter(validator.iter_errors(event)), None)
                            if error is not None:
                                where = "".join(f"/{part}" for part in error.absolute_path)
                                report(f"line {count}{where or ''}: {error.message}"[:300])
                        if event.get("seq") != count:
                            found = event.get("seq")
                            report(f"line {count}: expected seq {count}, found {found!r}")
                        if event.get("experiment_id") != experiment_id or (
                            event.get("run_id") != run_id
                        ):
                            report(f"line {count}: belongs to a different run")
                        expected = (
                            None if previous is None else hashlib.sha256(previous).hexdigest()
                        )
                        if event.get("prev_event_sha256") != expected:
                            report(f"line {count}: hash chain broken")
                        canonical = json.dumps(
                            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                        if canonical != line:
                            report(f"line {count}: not in canonical form")
                    previous = line
                    count += 1

        last_digest = hashlib.sha256(previous).hexdigest() if previous is not None else None
        summary = _read_json(directory / "summary.json")
        summary_agrees: bool | None = None
        if isinstance(summary, dict):
            summary_agrees = (
                summary.get("event_count") == count
                and summary.get("last_event_sha256") == last_digest
            )
            if not summary_agrees:
                report("summary.json does not match the event log (count or last digest)")
        return {
            "events_checked": count,
            "schema_checked": validator is not None,
            "problem_count": problem_count,
            "problems": problems,
            "intact": problem_count == 0 and (validator is not None),
            "last_event_sha256": last_digest,
            "summary_agrees": summary_agrees,
            # A line still being written is normal while a run is in progress.
            "partial_last_line": partial,
            "scope": "Event log only. Artifact contents and the world digest are not "
            "re-checked here; the Simulation's `ols verify` does that.",
        }


def _page_size(limit: int) -> int:
    if limit < 1:
        raise BadRequest("limit must be at least 1")
    return min(limit, MAX_PAGE)


def _image_type(head: bytes) -> str | None:
    for signature, media_type in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _as_text(data: bytes) -> str | None:
    """The bytes as UTF-8 text, or ``None`` if they are not text."""
    if b"\x00" in data:
        return None
    # The preview may have cut a multi-byte character; that alone is not an error.
    for trim in range(4):
        try:
            return data[: len(data) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _event_validator(contracts_dir: Path) -> Draft202012Validator | None:
    """A validator for observation events built from the archive's own schemas, if it has them."""
    registry = Registry()
    event_schema = None
    for path in sorted((contracts_dir / "v1").glob("*.schema.json")):
        schema = _read_json(path)
        if not (isinstance(schema, dict) and isinstance(schema.get("$id"), str)):
            continue
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        if path.name == "observation-event.schema.json":
            event_schema = schema
    if event_schema is None:
        return None
    return Draft202012Validator(event_schema, registry=registry)
