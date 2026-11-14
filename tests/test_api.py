"""The JSON API over real archives (written by the Simulation) and synthetic ones."""

from __future__ import annotations

import json

from conftest import MODEL_CHECK, PNG, SMOKE, run_url


def test_run_list_shows_every_archive_newest_first(client, writer):
    live = writer()
    live.start()
    runs = client.json("/api/runs")["runs"]
    assert [run["run_id"] for run in runs] == [live.run_id, MODEL_CHECK[1], SMOKE[1]]
    by_id = {run["run_id"]: run for run in runs}
    smoke = by_id[SMOKE[1]]
    assert smoke["finished"] is True and smoke["state"] == "completed"
    assert smoke["event_count"] == 20
    assert smoke["title"] == "Scripted pipeline check (no model)"
    assert by_id[live.run_id]["finished"] is False
    assert by_id[live.run_id]["state"] == "running"
    assert by_id[live.run_id]["event_count"] == 2


def test_run_summary_carries_the_frozen_configuration(client):
    detail = client.json(run_url(MODEL_CHECK))
    assert detail["experiment"]["initial_prompt"].startswith("You are inside a small")
    assert detail["experiment"]["limits"] == {"wall_clock_s": 600, "max_steps": 20}
    assert detail["summary"]["final_state"] == "completed"
    assert detail["model"]["provider_ref"] == "llama3.2:3b"
    assert detail["world_release"]["schema"] == "open-lab/world-release"
    assert detail["runtime_stderr"] == {"size_bytes": 0, "text": "", "truncated": False}
    assert detail["events"] == {
        "count": 12,
        "by_type": {"agent.output": 2, "model.call": 2, "run.state_changed": 4,
                    "tool.call": 2, "tool.result": 2},
        "tool_calls": 2,
    }
    # Counted from the log itself, and it agrees with what the supervisor wrote.
    assert detail["events"]["by_type"] == detail["summary"]["events_by_type"]
    assert {"file": "v1/observation-event.schema.json", "version": "1.0.0"} in detail["contracts"]


def test_timeline_is_paginated_in_recorded_order(client):
    first = client.json(run_url(SMOKE) + "/events?limit=8")
    assert [event["seq"] for event in first["events"]] == list(range(8))
    assert first["total"] == 20 and first["has_more"] is True and first["finished"] is True
    rest = client.json(run_url(SMOKE) + f"/events?limit=500&after={first['next_after']}")
    assert [event["seq"] for event in rest["events"]] == list(range(8, 20))
    assert rest["has_more"] is False
    # Events are returned exactly as recorded.
    assert rest["events"][-1]["payload"]["to"] == "completed"
    assert rest["events"][-1]["epistemic_status"] == "observed"


def test_timeline_type_filter(client):
    page = client.json(run_url(SMOKE) + "/events?types=tool.call,tool.result&limit=3")
    assert [event["type"] for event in page["events"]] == ["tool.call", "tool.result", "tool.call"]
    assert page["has_more"] is True
    page = client.json(run_url(SMOKE) + f"/events?types=tool.call,tool.result"
                       f"&after={page['next_after']}")
    assert len(page["events"]) == 5 and page["has_more"] is False
    assert client.json(run_url(SMOKE) + "/events?types=agent.statement")["events"] == []


def test_timeline_tail_returns_the_latest_events(client):
    page = client.json(run_url(SMOKE) + "/events?tail=1&limit=3")
    assert [event["seq"] for event in page["events"]] == [17, 18, 19]
    assert page["has_earlier"] is True and page["next_after"] == 19


def test_page_size_is_capped_and_validated(client, writer):
    big = writer()
    big.start()
    for index in range(1200):
        big.event("agent.output", {"text": f"line {index}"}, recorder="agent_runtime")
    page = client.json(run_url((big.experiment_id, big.run_id)) + "/events?limit=100000")
    assert len(page["events"]) == 500 and page["total"] == 1202 and page["has_more"] is True
    last = client.json(run_url((big.experiment_id, big.run_id)) + "/events?after=1195")
    assert [event["seq"] for event in last["events"]] == list(range(1196, 1202))
    assert client.request(run_url(SMOKE) + "/events?limit=0")[0] == 400
    assert client.request(run_url(SMOKE) + "/events?after=abc")[0] == 400


def test_tool_calls_are_paired_with_their_results(client):
    tools = client.json(run_url(SMOKE) + "/tools")
    assert tools["total"] == 4
    pairs = tools["pairs"]
    assert [pair["call"]["payload"]["capability_id"] for pair in pairs] == [
        "text.stats", "math.evaluate", "math.evaluate", "no.such.tool"]
    for pair in pairs:
        assert pair["call"]["correlation_id"] == pair["result"]["correlation_id"]
        assert pair["call"]["seq"] < pair["result"]["seq"]
    assert pairs[0]["result"]["payload"]["result"] == {"characters": 19, "lines": 2, "words": 3}
    assert pairs[2]["result"]["payload"]["status"] == "error"
    assert "division by zero" in pairs[2]["result"]["payload"]["error"]
    page = client.json(run_url(SMOKE) + "/tools?offset=3&limit=1")
    assert [pair["correlation_id"] for pair in page["pairs"]] == ["call-000004"]


def test_a_call_without_a_result_is_shown_as_such(client, writer):
    run = writer()
    run.start()
    run.event("tool.call", {"capability_id": "text.stats", "arguments": {"text": "x"}},
              correlation_id="call-000001", recorder="tool_host")
    pair = client.json(run_url((run.experiment_id, run.run_id)) + "/tools")["pairs"][0]
    assert pair["call"]["payload"]["capability_id"] == "text.stats"
    assert pair["result"] is None


def test_agent_claims_keep_their_status(client, writer):
    run = writer()
    run.start()
    run.event("agent.output", {"text": "My goal is to keep this world running."},
              recorder="agent_runtime")
    run.event("agent.statement", {"kind": "goal", "text": "keep this world running",
                                  "source_seq": 2}, status="agent_claim", recorder="agent_runtime")
    events = client.json(run_url((run.experiment_id, run.run_id)) + "/events")["events"]
    assert [(event["type"], event["epistemic_status"]) for event in events[2:]] == [
        ("agent.output", "observed"), ("agent.statement", "agent_claim")]
    integrity = client.json(run_url((run.experiment_id, run.run_id)) + "/integrity")
    assert integrity["problem_count"] == 0 and integrity["schema_checked"] is True


def test_artifact_list_and_previews(client, writer):
    run = writer()
    run.start()
    note = run.artifact("notes/plan.md", "# Plan\nfirst line — ünïcode\n".encode(),
                        media_type="text/markdown", agent_description="my plan")
    image = run.artifact("out/pixel.png", PNG, media_type="image/png")
    binary = run.artifact("out/blob.dat", b"\x00\x01\x02\xff")
    big = run.artifact("out/huge.bin", b"x" * 10, preserved=False)
    fake = run.artifact("out/evil.png", b"<script>alert(1)</script>", media_type="image/png")
    run.finish()
    base = run_url((run.experiment_id, run.run_id))

    listing = client.json(base + "/artifacts")
    assert listing["total"] == 5
    assert [a["workspace_path"] for a in listing["artifacts"]][:2] == [
        "notes/plan.md", "out/pixel.png"]
    assert client.json(base + "/artifacts?offset=4&limit=1")["artifacts"][0]["artifact_id"] == fake
    assert client.json(base)["artifact_versions"] == 5

    text = client.json(f"{base}/artifacts/{note}/preview")
    assert text["kind"] == "text" and text["text"].endswith("ünïcode\n")
    assert text["truncated"] is False
    assert text["artifact"]["agent_description"] == "my plan"

    assert client.json(f"{base}/artifacts/{image}/preview")["kind"] == "image"
    status, headers, body = client.request(f"{base}/artifacts/{image}/image")
    assert status == 200 and body == PNG
    assert headers["Content-Type"] == "image/png"
    assert headers["X-Content-Type-Options"] == "nosniff"

    assert client.json(f"{base}/artifacts/{binary}/preview")["kind"] == "none"
    assert "not preserved" in client.json(f"{base}/artifacts/{big}/preview")["reason"]
    # Bytes are served as an image only if they are one, whatever the file is called.
    assert client.json(f"{base}/artifacts/{fake}/preview")["kind"] == "text"
    assert client.request(f"{base}/artifacts/{fake}/image")[0] == 404
    assert client.request(f"{base}/artifacts/{note}/image")[0] == 404
    assert client.request(f"{base}/artifacts/art-999999/preview")[0] == 404


def test_real_archive_without_artifacts(client):
    assert client.json(run_url(SMOKE) + "/artifacts") == {"artifacts": [], "total": 0, "offset": 0}


def test_long_text_preview_is_truncated(client, writer):
    run = writer()
    run.start()
    artifact = run.artifact("long.txt", ("é" * 200_000).encode())
    base = run_url((run.experiment_id, run.run_id))
    preview = client.json(f"{base}/artifacts/{artifact}/preview")
    assert preview["kind"] == "text" and preview["truncated"] is True
    assert 100_000 < len(preview["text"]) <= 131_072 and set(preview["text"]) == {"é"}


def test_in_progress_run_is_followed_by_polling(client, writer):
    run = writer()
    run.start()
    base = run_url((run.experiment_id, run.run_id))
    detail = client.json(base)
    assert detail["run"]["finished"] is False and detail["summary"] is None
    assert detail["run"]["state"] == "running"

    page = client.json(base + "/events")
    assert [event["seq"] for event in page["events"]] == [0, 1] and page["finished"] is False
    cursor = page["next_after"]
    assert client.json(base + f"/events?after={cursor}")["events"] == []

    run.event("agent.output", {"text": "hello"}, recorder="agent_runtime")
    # A line the recorder has only half written is not shown...
    half = b'{"seq":3,"type":"agent.outp'
    run.append_raw(half)
    page = client.json(base + f"/events?after={cursor}")
    assert [event["seq"] for event in page["events"]] == [2] and page["total"] == 3
    assert client.json(base + "/integrity")["partial_last_line"] is True
    # ...until it is complete. (Finish that line as garbage, then go on.)
    run.append_raw(b'ut\n')
    page = client.json(base + f"/events?after={page['next_after']}")
    assert page["events"][0]["unreadable"] is True and page["total"] == 4

    assert client.json("/api/runs")["runs"][0]["event_count"] == 4


def test_run_that_ends_while_watched(client, writer):
    run = writer()
    run.start()
    base = run_url((run.experiment_id, run.run_id))
    cursor = client.json(base + "/events")["next_after"]
    run.finish("terminated")
    page = client.json(base + f"/events?after={cursor}")
    assert page["finished"] is True and page["events"][-1]["payload"]["to"] == "terminated"
    detail = client.json(base)
    assert detail["run"]["finished"] is True and detail["run"]["state"] == "terminated"
    assert client.json(base + "/integrity")["intact"] is True


def test_integrity_of_real_archives(client):
    for run in (SMOKE, MODEL_CHECK):
        report = client.json(run_url(run) + "/integrity")
        assert report["intact"] is True and report["problems"] == []
        assert report["schema_checked"] is True and report["summary_agrees"] is True
        assert report["last_event_sha256"] == client.json(run_url(run))["summary"][
            "last_event_sha256"]
    assert client.json(run_url(SMOKE) + "/integrity")["events_checked"] == 20


def test_integrity_detects_an_edited_log(client, data_root):
    log = data_root / "runs" / SMOKE[0] / SMOKE[1] / "events.jsonl"
    lines = log.read_bytes().split(b"\n")
    event = json.loads(lines[4])
    event["payload"]["text"] = "Something else entirely."
    lines[4] = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    log.write_bytes(b"\n".join(lines))
    report = client.json(run_url(SMOKE) + "/integrity")
    assert report["intact"] is False
    assert "line 5: hash chain broken" in report["problems"]
    # The Window still shows the log; it reports the damage rather than hiding the run.
    assert client.json(run_url(SMOKE) + "/events?after=3&limit=1")["events"][0]["payload"][
        "text"] == "Something else entirely."


def test_integrity_reports_schema_violations(client, writer):
    run = writer()
    run.start()
    # An observation that claims to be an agent's claim is not allowed by the contract.
    run.event("agent.output", {"text": "x"}, status="agent_claim", recorder="agent_runtime")
    report = client.json(run_url((run.experiment_id, run.run_id)) + "/integrity")
    assert report["problem_count"] == 1 and report["problems"][0].startswith("line 2")


def test_unknown_runs_and_paths_are_not_found(client):
    for path in ("/api/runs/nope/run-1", "/api/runs/scripted-smoke/nope/events",
                 "/api/nothing", "/static/missing.js", "/api/runs/scripted-smoke"):
        assert client.request(path)[0] == 404, path


def test_requests_cannot_leave_the_runs_directory(client, data_root):
    (data_root / "secret.json").write_text('{"secret": true}', encoding="utf-8")
    for path in ("/api/runs/../secret.json", "/api/runs/%2e%2e/%2e%2e", "/api/runs/..%2f../x",
                 "/api/runs/scripted-smoke/..", "/api/runs/scripted-smoke/%2e%2e/events",
                 "/static/../archive.py", "/static/%2e%2e/archive.py", "/../pyproject.toml"):
        status, _, body = client.request(path)
        assert status == 404, path
        assert b"secret" not in body


def test_empty_data_root(tmp_path):
    from open_lab_window.archive import Library
    assert Library(tmp_path / "missing").list_runs() == []


def test_front_end_is_served_with_a_strict_policy(client):
    status, headers, body = client.request("/")
    assert status == 200 and b"<title>Open Laboratory" in body
    assert headers["Content-Type"].startswith("text/html")
    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "unsafe" not in policy
    assert headers["Cache-Control"] == "no-store"
    for path, kind in (("/static/app.js", "text/javascript"), ("/static/style.css", "text/css")):
        status, headers, _ = client.request(path)
        assert status == 200 and headers["Content-Type"].startswith(kind)


def test_front_end_never_builds_html_from_archive_text():
    from open_lab_window import server
    script = (server._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                      "POST", "PUT", "DELETE", "XMLHttpRequest", "WebSocket", "sendBeacon"):
        assert forbidden not in script, forbidden
    # Content reaches the page through one helper that makes text nodes of strings
    # and skips empty values (a bare replaceChildren would print "null").
    assert script.count("replaceChildren") == 1 and "createTextNode" in script
    page = (server._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "http://" not in page and "https://" not in page
