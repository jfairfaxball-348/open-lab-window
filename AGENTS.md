# AGENTS.md — open-lab-window

Instructions for anyone working in this repository: Claude Code, OpenAI Codex,
other coding agents and human contributors. `CLAUDE.md` only imports this file.

## The contract lives in the Builder

This repository is one of three. The development contract for the whole
project — session workflow, honesty rules, the roadmap and the development
state — is **`AGENTS.md` in the sibling `open-lab-builder` checkout**. Read it
first and follow it. Find the sibling checkout next to this one; do not assume
a path.

- The roadmap (`docs/roadmap/ROADMAP.md`) and state (`docs/roadmap/state.json`)
  are in the Builder and are updated there, including for milestones done here.
- The architecture, the contract schemas and the specifications this code
  reads by (`docs/architecture/CONTRACTS.md`, `OBSERVATION.md`,
  `docs/decisions/ADR-0008-component-communication.md`) are in the Builder.

What follows is only what is specific to this repository.

## Rules for this repository

- **The Window never writes.** Not to a run archive, not to the data root, not
  anywhere else on disk: no cache files, no index, no logs. Every file is
  opened through `open_readonly` in `archive.py`, in mode `rb`. Tests enforce
  this three ways (a scan of the source, a record of every file opened while
  browsing, and a digest of the data root before and after). Do not weaken
  them.
- **The Window never reaches a run.** It has no client code: it opens no
  outgoing connection, starts no process and holds no handle the Simulation
  could notice. A live run is followed only by re-reading `events.jsonl`.
- **GET and HEAD only.** Every other HTTP method is answered 405 before any
  routing happens, and whatever body came with it is discarded unparsed. No
  endpoint has a side effect. Operator controls, if ever wanted, belong to
  the Simulation's supervisor, not here.
- **Loopback only.** The server binds to `127.0.0.1` and has no option to bind
  elsewhere; it refuses requests whose `Host` header is not a loopback name.
  There is no authentication, so this is the only access control.
- **Never import Builder or Simulation code**, and never copy schemas into
  `src/`. Events are validated against the schemas each archive carries under
  `contracts/`. A test enforces this.
- **Everything in an archive is untrusted text.** Model output, tool
  arguments, file names and file contents were produced by an agent. The front
  end inserts them with `textContent` only, never as HTML; the server sends a
  strict Content-Security-Policy; artifact bytes are served only when they are
  recognised raster images.
- **Keep observed behaviour and agent claims visibly apart.** An event's
  `epistemic_status` is always shown, and `agent_claim` must stay visually
  distinct from `observed`. The Window displays the record; it does not
  summarise, interpret or infer what an agent wants, feels or believes.
- **Say what could not be read.** A missing file, an unreadable line or a
  failed check is shown as such, never hidden or guessed around.
- **Paginate.** Event logs can reach millions of lines. No endpoint returns a
  whole log, and the server never holds one in memory (it keeps line offsets
  and types only).
- `tests/fixtures/runs/` holds two real archives written by the Simulation.
  Do not edit them by hand.
- Never commit personal paths, user or host names, credentials or model
  weights. No history rewriting, force pushes or `--no-verify`.

## Commands

| Command | Purpose |
| --- | --- |
| `uv sync` | Create or update the virtual environment |
| `uv run pytest` | Run the tests |
| `uv run ruff check .` | Lint |
| `uv run olw serve [--data-root DIR] [--port N]` | Serve the Window on `http://127.0.0.1:8765/` |

## Code conventions

Python 3.12, standard library first (`jsonschema` is the only runtime
dependency). The front end is plain HTML, CSS and JavaScript in
`src/open_lab_window/static/`, with no build step and no third-party
packages; see ADR-0008 in the Builder for why. Must run on Windows and Linux:
`pathlib`, never a shell string. Every behaviour has a test. Comments explain
constraints and reasons.
