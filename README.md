# The Open Laboratory — Window

A read-only browser view of recorded runs: what an agent was launched with,
every event the infrastructure recorded, the tools it called and what they
returned, and the files it left behind.

This is one of three independent repositories. The **World Builder**
(`open-lab-builder`) engineers the world and owns the architecture, contracts
and roadmap. The **Simulation** (`open-lab-simulation`) runs experiments and
writes run archives. The Window only reads those archives. They share contract
schemas and a data directory, never code.

## Status

**S004 (basic Window) is complete.** What exists and works:

- `olw serve` — a local server on `http://127.0.0.1:8765/` over
  `<data root>/runs`.
- **Run list**: every archive, newest first, with state, times, event count,
  model and world. Refreshes by itself.
- **Run summary**: the initial prompt and the rest of the frozen experiment
  configuration, `summary.json`, `model.json`, the world manifest, the
  runtime's stderr, the bundled contracts, event counts by type.
- **Timeline**: events in recorded order, paginated, with type filters. Each
  event shows its `epistemic_status`; an `agent_claim` is drawn differently
  from an `observed` event (own colour, dashed border, label).
  Text the runtime itself gave the model (an `environment.message`, such as
  the continuation sent after a reply without a tool call) is labelled as
  coming from the runtime, not the model.
- **Tool calls**: each `tool.call` beside the `tool.result` with the same
  correlation id, or a note that no result was recorded.
- **Artifacts**: every observed version of every file, with a text preview
  (first 256 KB) or an image preview (PNG, JPEG, GIF, BMP, WebP, recognised by
  content, not by name).
- **Live tail**: a run without `summary.json` is followed by re-reading its
  event log every two seconds.
- **Record check**: re-reads an event log and checks it against the schemas
  bundled in that archive, the sequence numbers and the hash chain.

What does **not** exist:

- No comparison of runs, no search, no statistics, no export (S010).
- No control of runs, by design, ever.
- No authentication: the server is reachable from this machine only.
- The record check covers the event log, not artifact contents or the world
  digest. `ols verify` in the Simulation checks a whole archive.
- A run with no `summary.json` is shown as "not finished". The archive alone
  cannot say whether it is still running or was interrupted; the Window shows
  the last recorded state and when the last event was written.
- Only contract major version 1 is understood.
- No real archive contains an artifact yet (the minimal world has no
  capability that writes files), so the artifact views have only been
  exercised on synthetic archives written by the tests.

## Read-only, and how that is kept

The Window must not be able to influence a run, even by accident
(ADR-0008 and OBSERVATION.md in the Builder).

- Every file is opened by one function, in mode `rb`. The Window writes
  nothing anywhere: no cache, no index, no log file.
- The server answers GET and HEAD. Every other method gets 405 before routing,
  and its body is discarded unparsed.
- It binds to `127.0.0.1` only, with no option to change that, and refuses
  requests whose `Host` header is not a loopback name.
- It has no client code: no outgoing connections and no child processes. It
  cannot contact the Simulation or a model server.
- Archive content is agent-produced and is treated as untrusted text: it is
  never inserted as HTML, the page runs under a strict Content-Security-Policy,
  and stored files are served only when their bytes are a raster image.

The tests check these: a scan of the source for anything that could write or
connect, a record of every file opened while every page is requested, a digest
of the data root before and after, and requests with every other HTTP method.

## Use

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run olw serve
```

Then open `http://127.0.0.1:8765/`. The data root is `$OPEN_LAB_DATA`, or
`open-lab-data` in the home directory; `--data-root DIR` overrides it and
`--port N` changes the port.

```bash
uv run pytest
```

```bash
uv run ruff check .
```

## Layout

| Path | Purpose |
| --- | --- |
| `src/open_lab_window/archive.py` | Read-only archive reader: runs, events, tool pairs, artifacts, record check |
| `src/open_lab_window/server.py` | Loopback HTTP server, GET and HEAD only |
| `src/open_lab_window/cli.py` | `olw serve` |
| `src/open_lab_window/static/` | The front end: plain HTML, CSS and JavaScript, no build step |
| `tests/fixtures/runs/` | Two real archives written by the Simulation (a scripted run and a local-model run) |

## JSON endpoints

All GET. `{e}` is the experiment id and `{r}` the run id.

| Path | Returns |
| --- | --- |
| `/api/runs` | Every run, newest first |
| `/api/runs/{e}/{r}` | Summary, frozen documents, event counts |
| `/api/runs/{e}/{r}/events?after=N&limit=N&types=a,b&tail=1` | A page of events (at most 500). Pass `next_after` back as `after` to continue or to follow a growing log |
| `/api/runs/{e}/{r}/tools?offset=N&limit=N` | Tool calls paired with results |
| `/api/runs/{e}/{r}/artifacts?offset=N&limit=N` | Artifact metadata records |
| `/api/runs/{e}/{r}/artifacts/{id}/preview` | Text, or a note that it is an image or cannot be shown |
| `/api/runs/{e}/{r}/artifacts/{id}/image` | The stored bytes, only if they are a raster image |
| `/api/runs/{e}/{r}/integrity` | Result of checking the event log |

Development rules are in [AGENTS.md](AGENTS.md).
