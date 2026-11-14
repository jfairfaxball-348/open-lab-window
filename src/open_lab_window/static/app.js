// The Window's front end. No framework and no build step.
//
// Two rules hold throughout:
//  * Everything from an archive is untrusted text written by an agent or about
//    one. It reaches the page only through textContent (see `h`), never as HTML.
//  * The only requests made are GETs to this server. There is nothing to send.
"use strict";

const view = document.getElementById("view");
const PAGE = 100;
const POLL_MS = 2000;
const MAX_ROWS = 1000; // While following, the page keeps only the newest rows.
let pollTimer = null;
let generation = 0; // Bumped on every navigation so late responses are dropped.

// -- helpers ---------------------------------------------------------------

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "checked" || name === "disabled" || name === "open") node[name] = !!value;
    else node.setAttribute(name, value);
  }
  fill(node, children);
  return node;
}

// Replace a node's content. Strings become text nodes; nothing is parsed as HTML.
function fill(node, ...children) {
  node.replaceChildren();
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

async function api(path) {
  const response = await fetch(path, { method: "GET", cache: "no-store" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function runPath(e, r) {
  return `/api/runs/${encodeURIComponent(e)}/${encodeURIComponent(r)}`;
}

function show(...nodes) {
  fill(view, nodes);
}

function fail(error) {
  show(h("p", { class: "notice error" }, `Could not load: ${error.message}`),
    h("p", {}, h("a", { href: "#/" }, "Back to the run list")));
}

function time(stamp) {
  if (!stamp) return "—";
  return stamp.replace("T", " ").replace(/\.\d+Z$/, "Z");
}

function ago(stamp) {
  const then = Date.parse(stamp);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function bytes(n) {
  if (typeof n !== "number") return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function json(value) {
  return JSON.stringify(value, null, 2);
}

function stateBadge(run) {
  if (run.finished) {
    return h("span", { class: `badge state-${run.state}` }, run.state || "unknown");
  }
  // Without summary.json the archive cannot say whether the run is still
  // going or was interrupted, so neither is asserted.
  return h("span", {
    class: "badge state-live",
    title: "No summary.json yet: the run is in progress, or it was interrupted before it could end.",
  }, `${run.state || "no events"} · not finished`);
}

function epistemic(event) {
  const status = event.epistemic_status;
  if (status === "agent_claim") {
    return h("span", { class: "ep ep-claim", title: "The model asserted this. Only the fact that it was said is evidence." }, "agent claim");
  }
  if (status === "derived") {
    return h("span", { class: "ep ep-derived", title: "Computed by infrastructure from other events." },
      `derived from ${(event.derived_from || []).join(", ")}`);
  }
  if (status === "observed") {
    return h("span", { class: "ep ep-observed", title: "Infrastructure witnessed this directly." }, "observed");
  }
  return h("span", { class: "ep ep-unknown" }, status || "no status");
}

// -- run list --------------------------------------------------------------

async function runList(mine) {
  const { runs } = await api("/api/runs");
  if (mine !== generation) return;
  const info = await api("/api/info");
  if (mine !== generation) return;
  if (!runs.length) {
    show(h("h1", {}, "Runs"), h("p", { class: "notice" }, info.runs_directory_exists
      ? "The data root has no run archives yet."
      : "There is no runs directory in the data root. Nothing has been recorded there."));
    return;
  }
  const rows = runs.map((run) => h("tr", {},
    h("td", {}, h("a", { href: `#/run/${run.experiment_id}/${run.run_id}` }, run.run_id),
      h("div", { class: "muted" }, run.experiment_id)),
    h("td", {}, run.title || "—"),
    h("td", {}, stateBadge(run), run.reason ? h("div", { class: "muted" }, run.reason) : null),
    h("td", {}, time(run.started_at)),
    h("td", {}, run.finished ? time(run.ended_at) : h("span", { class: "muted" }, `last event ${ago(run.last_event_at)}`)),
    h("td", { class: "num" }, run.event_count ?? "—"),
    h("td", {}, run.orchestrator ? run.orchestrator.model_id : "—"),
    h("td", {}, run.world ? `${run.world.world_id} ${run.world.version}` : "—")));
  show(
    h("h1", {}, "Runs"),
    h("p", { class: "muted" }, `${runs.length} run archive${runs.length === 1 ? "" : "s"}, newest first.`),
    h("div", { class: "scroll" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Run", "Experiment title", "State", "Started (UTC)", "Ended (UTC)", "Events", "Model", "World"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows))));
  // New runs appear without a reload.
  pollTimer = setTimeout(() => { if (mine === generation) runList(mine).catch(() => {}); }, 5000);
}

// -- run pages -------------------------------------------------------------

const TABS = [["summary", "Summary"], ["timeline", "Timeline"], ["tools", "Tool calls"], ["artifacts", "Artifacts"]];

function runHeader(detail, tab) {
  const run = detail.run;
  const base = `#/run/${run.experiment_id}/${run.run_id}`;
  return [
    h("p", { class: "crumbs" }, h("a", { href: "#/" }, "Runs"), " / ", run.experiment_id),
    h("h1", {}, run.run_id, " ", stateBadge(run)),
    h("p", { class: "muted" },
      run.title || "", run.title ? " · " : "",
      `started ${time(run.started_at)}`,
      run.finished ? ` · ended ${time(run.ended_at)}` : ` · last event ${time(run.last_event_at)} (${ago(run.last_event_at)})`,
      run.reason ? ` · ${run.reason}` : ""),
    h("nav", { class: "tabs" }, TABS.map(([id, label]) =>
      h("a", { href: `${base}/${id}`, class: id === tab ? "active" : "" }, label,
        id === "timeline" ? ` (${detail.events.count})` : id === "tools" ? ` (${detail.events.tool_calls})`
          : id === "artifacts" ? ` (${detail.artifact_versions})` : ""))),
  ];
}

function field(label, value) {
  return h("div", { class: "field" }, h("dt", {}, label), h("dd", {}, value ?? "—"));
}

function block(title, value, note) {
  if (value === null || value === undefined) {
    return h("details", { class: "doc" }, h("summary", {}, title, h("span", { class: "muted" }, " — not in this archive")));
  }
  return h("details", { class: "doc" }, h("summary", {}, title, note ? h("span", { class: "muted" }, ` — ${note}`) : null),
    h("pre", {}, typeof value === "string" ? value : json(value)));
}

async function summaryTab(e, r, mine) {
  const detail = await api(runPath(e, r));
  if (mine !== generation) return;
  const x = detail.experiment || {};
  const s = detail.summary;
  const model = detail.model;
  const check = h("div", { class: "check" });
  const button = h("button", {
    type: "button",
    onclick: async () => {
      button.disabled = true;
      fill(check, h("span", { class: "muted" }, "Checking…"));
      try {
        fill(check, integrityReport(await api(`${runPath(e, r)}/integrity`)));
      } catch (error) {
        fill(check, h("span", { class: "error" }, error.message));
      }
      button.disabled = false;
    },
  }, "Check the event log");

  show(
    runHeader(detail, "summary"),
    h("section", {},
      h("h2", {}, "Initial prompt"),
      h("p", { class: "muted" }, "The single prompt the agent was launched with (frozen configuration)."),
      h("pre", { class: "prompt" }, x.initial_prompt ?? "(experiment.json is missing or unreadable)")),
    h("section", {},
      h("h2", {}, "Run"),
      h("dl", { class: "fields" },
        field("Experiment", x.experiment_id || e),
        field("Research question", x.research_question),
        field("Final state", s ? s.final_state : "not finished (no summary.json)"),
        field("Reason", detail.run.reason),
        field("Events recorded", detail.events.count),
        field("Artifact versions", detail.artifact_versions),
        field("World", x.world ? `${x.world.world_id} ${x.world.version}` : null),
        field("World digest", s && s.world ? h("code", {}, s.world.content_digest) : null),
        field("Orchestrator", x.orchestrator ? `${x.orchestrator.model_id} via ${x.orchestrator.provider_id}` : null),
        field("Model served", model ? `${model.provider_ref || model.model_id}${model.real_model === false ? " (scripted stand-in, not a model)" : ""}` : null),
        field("Limits", x.limits ? Object.entries(x.limits).map(([k, v]) => `${k}: ${v}`).join(", ") : null),
        field("Permissions", x.permissions ? Object.entries(x.permissions).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(" ") : v}`).join(", ") : null))),
    h("section", {},
      h("h2", {}, "Events by type"),
      h("div", { class: "chips" }, Object.entries(detail.events.by_type).map(([type, n]) =>
        h("a", { class: "chip", href: `#/run/${e}/${r}/timeline?types=${encodeURIComponent(type)}` }, `${type} `, h("b", {}, n))))),
    h("section", {},
      h("h2", {}, "Record integrity"),
      h("p", { class: "muted" }, "Re-reads the whole event log and checks it against the schemas bundled in this archive, the sequence numbers and the hash chain."),
      button, check),
    h("section", {},
      h("h2", {}, "Frozen documents"),
      block("experiment.json — frozen experiment configuration", detail.experiment),
      block("summary.json — written by the supervisor when the run ended", detail.summary),
      block("model.json — the model that drove the run", detail.model),
      block("world-release.json — frozen world manifest", detail.world_release),
      block("runtime.stderr.log", detail.runtime_stderr ? (detail.runtime_stderr.text || "(empty)") : null,
        detail.runtime_stderr ? `${bytes(detail.runtime_stderr.size_bytes)}${detail.runtime_stderr.truncated ? ", beginning only" : ""}` : null),
      block("Bundled contracts", detail.contracts.length ? detail.contracts.map((c) => `${c.file}  ${c.version ?? "?"}`).join("\n") : null)));
}

function integrityReport(report) {
  const lines = [
    `${report.events_checked} complete event line${report.events_checked === 1 ? "" : "s"} checked.`,
    report.schema_checked ? "Validated against the archive's bundled observation-event schema."
      : "The archive has no bundled observation-event schema, so events were NOT validated against it.",
    report.summary_agrees === null ? "No summary.json to compare with (run not finished)."
      : report.summary_agrees ? "summary.json agrees with the log (event count and last digest)."
        : "summary.json does NOT agree with the log.",
    report.partial_last_line ? "The last line is incomplete (still being written) and was not checked." : null,
    report.scope,
  ].filter(Boolean);
  return h("div", { class: report.problem_count ? "notice error" : "notice ok" },
    h("strong", {}, report.problem_count ? `${report.problem_count} problem${report.problem_count === 1 ? "" : "s"} found` : "No problems found"),
    h("ul", {}, lines.map((line) => h("li", {}, line))),
    report.problems.length ? h("pre", {}, report.problems.join("\n")) : null);
}

// -- timeline --------------------------------------------------------------

function eventBody(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "run.state_changed":
      return h("div", {}, h("b", {}, `${p.from || "(start)"} → ${p.to}`), p.reason ? ` — ${p.reason}` : "");
    case "agent.output":
      return h("div", {}, h("div", { class: "label" }, "Model output, verbatim", p.truncated ? " (truncated by the recorder)" : ""),
        p.text ? h("pre", { class: "said" }, p.text) : h("span", { class: "muted" }, "(empty text; see any tool calls that follow)"));
    case "agent.statement":
      return h("div", {}, h("div", { class: "label" }, `Stated ${p.kind || "statement"} — said by the model, not verified`,
        typeof p.source_seq === "number" ? ` · from event ${p.source_seq}` : ""),
        h("pre", { class: "said" }, p.text ?? ""));
    case "model.call": {
      const extra = Object.entries(p).filter(([k]) => !["model_id", "purpose", "input_tokens", "output_tokens", "duration_ms"].includes(k));
      return h("div", {}, h("b", {}, p.model_id), p.purpose ? ` (${p.purpose})` : "",
        typeof p.input_tokens === "number" ? ` · ${p.input_tokens} tokens in` : "",
        typeof p.output_tokens === "number" ? ` · ${p.output_tokens} out` : "",
        typeof p.duration_ms === "number" ? ` · ${p.duration_ms} ms` : "",
        extra.length ? h("pre", {}, json(Object.fromEntries(extra))) : null);
    }
    case "tool.call":
      return h("div", {}, h("b", {}, p.capability_id), p.capability_version ? ` ${p.capability_version}` : "",
        h("span", { class: "muted" }, ` · ${event.correlation_id}`), h("pre", {}, json(p.arguments)));
    case "tool.result":
      return h("div", {}, h("span", { class: `badge result-${p.status}` }, p.status),
        typeof p.duration_ms === "number" ? ` ${p.duration_ms} ms` : "",
        h("span", { class: "muted" }, ` · ${event.correlation_id}`),
        p.error ? h("pre", { class: "errtext" }, p.error) : null,
        p.result !== undefined ? h("pre", {}, json(p.result)) : null);
    case "artifact.created": case "artifact.modified": case "artifact.deleted":
      return h("div", {}, h("b", {}, p.path), ` · ${p.artifact_id}`,
        typeof p.size_bytes === "number" ? ` · ${bytes(p.size_bytes)}` : "",
        p.sha256 ? h("span", { class: "muted" }, ` · sha256 ${p.sha256.slice(0, 12)}…`) : null);
    case "error":
      return h("div", {}, h("b", {}, p.origin), `: ${p.message}`, p.recoverable === true ? " (recoverable)" : "");
    case "supervisor.action":
      return h("div", {}, h("b", {}, p.action), p.detail ? ` — ${p.detail}` : "");
    default:
      if (event.unreadable) return h("div", {}, h("span", { class: "error" }, "This line of the log is not a JSON object."), h("pre", {}, event.raw));
      return h("pre", {}, json(p));
  }
}

function eventRow(event) {
  const claim = event.epistemic_status === "agent_claim";
  const typeClass = String(event.type || "").replace(/[^a-z]/g, "-");
  return h("article", { class: `event t-${typeClass}${claim ? " claim" : ""}` },
    h("div", { class: "meta" },
      h("span", { class: "seq" }, `#${event.seq}`),
      h("span", { class: "type" }, event.type),
      epistemic(event),
      h("span", { class: "muted" }, time(event.timestamp).slice(11),
        typeof event.monotonic_ms === "number" ? ` · +${(event.monotonic_ms / 1000).toFixed(3)} s` : ""),
      h("span", { class: "muted" }, event.actor ? `actor: ${event.actor.kind}` : "", event.recorder ? ` · recorded by ${event.recorder}` : "")),
    h("div", { class: "body" }, eventBody(event)),
    h("details", { class: "raw" }, h("summary", {}, "Recorded event"), h("pre", {}, json(event))));
}

async function timelineTab(e, r, query, mine) {
  const detail = await api(runPath(e, r));
  if (mine !== generation) return;
  const allTypes = Object.keys(detail.events.by_type);
  let selected = query.get("types") ? new Set(query.get("types").split(",")) : null;
  let after = -1;
  const list = h("div", { class: "events" });
  const status = h("p", { class: "muted" });
  const more = h("button", { type: "button", onclick: () => load(false) }, `Load ${PAGE} more`);
  const follow = h("input", { type: "checkbox", checked: !detail.run.finished });
  let finished = detail.run.finished;
  let total = detail.events.count;
  let shown = 0;
  let busy = false;
  let earlier = false;

  function typesParam() {
    return selected ? `&types=${encodeURIComponent([...selected].join(","))}` : "";
  }

  async function load(tail) {
    if (busy) return;
    busy = true;
    try {
      const page = await api(`${runPath(e, r)}/events?limit=${PAGE}${tail ? "&tail=1" : `&after=${after}`}${typesParam()}`);
      if (mine !== generation) return;
      if (tail) { fill(list); shown = 0; }
      for (const event of page.events) list.append(eventRow(event));
      shown += page.events.length;
      let dropped = false;
      while (list.childElementCount > MAX_ROWS) { list.firstElementChild.remove(); shown -= 1; dropped = true; }
      if (dropped || page.has_earlier) earlier = true;
      after = page.next_after;
      total = page.total;
      if (page.finished && !finished) {
        // The run ended while we watched: reload so the header shows how.
        finished = true;
        route();
        return;
      }
      more.hidden = !page.has_more;
      status.textContent = `${shown} shown${selected ? " (filtered)" : ""} of ${total} recorded`
        + (earlier ? " — earlier events are not shown; use “From the start”." : "")
        + (finished ? "." : follow.checked ? " — following the log as it grows." : " — the log may still be growing.");
      if (follow.checked && page.events.length && !page.has_more) {
        list.lastElementChild.scrollIntoView({ block: "nearest" });
      }
    } catch (error) {
      status.textContent = `Could not read events: ${error.message}`;
    } finally {
      busy = false;
    }
  }

  function restart(tail) {
    after = -1;
    shown = 0;
    earlier = false;
    fill(list);
    load(tail);
  }

  function tick() {
    if (mine !== generation) return;
    // Following only ever asks for lines after the last one seen.
    if (!finished && follow.checked && more.hidden) load(false);
    pollTimer = setTimeout(tick, POLL_MS);
  }

  const filters = h("fieldset", { class: "filters" }, h("legend", {}, "Event types"),
    allTypes.map((type) => h("label", {},
      h("input", {
        type: "checkbox", checked: !selected || selected.has(type),
        onchange: (ev) => {
          if (!selected) selected = new Set(allTypes);
          if (ev.target.checked) selected.add(type); else selected.delete(type);
          restart(false);
        },
      }), ` ${type} `, h("span", { class: "muted" }, detail.events.by_type[type]))));

  show(
    runHeader(detail, "timeline"),
    h("div", { class: "toolbar" },
      filters,
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => restart(false) }, "From the start"),
        h("button", { type: "button", onclick: () => restart(true) }, `Latest ${PAGE}`),
        finished ? null : h("label", { title: "Re-reads the event log every two seconds. Nothing is sent to the run." }, follow, " Follow live"))),
    h("p", { class: "legend" }, epistemic({ epistemic_status: "observed" }), " witnessed by infrastructure ",
      epistemic({ epistemic_status: "agent_claim" }), " asserted by the model; only the saying is evidence ",
      h("span", { class: "ep ep-derived" }, "derived"), " computed from other events"),
    list, more, status);
  more.hidden = true;
  // A run still being recorded opens at its newest events, so that following
  // shows what is happening now; a finished run opens at its beginning.
  await load(!finished);
  if (!finished) pollTimer = setTimeout(tick, POLL_MS);
}

// -- tool calls ------------------------------------------------------------

async function toolsTab(e, r, mine) {
  const detail = await api(runPath(e, r));
  if (mine !== generation) return;
  const list = h("div", { class: "pairs" });
  const status = h("p", { class: "muted" });
  let offset = 0;
  const more = h("button", { type: "button", onclick: () => load() }, `Load ${PAGE} more`);

  async function load() {
    const page = await api(`${runPath(e, r)}/tools?limit=${PAGE}&offset=${offset}`);
    if (mine !== generation) return;
    for (const pair of page.pairs) {
      const call = pair.call || {};
      const result = pair.result;
      const cp = call.payload || {};
      const rp = result ? result.payload || {} : {};
      list.append(h("article", { class: "pair" },
        h("div", { class: "half" },
          h("div", { class: "meta" }, h("span", { class: "seq" }, `#${call.seq}`), h("span", { class: "type" }, "tool.call"), epistemic(call),
            h("span", { class: "muted" }, time(call.timestamp).slice(11), ` · ${pair.correlation_id}`)),
          h("div", {}, h("b", {}, cp.capability_id), cp.capability_version ? ` ${cp.capability_version}` : ""),
          h("pre", {}, json(cp.arguments))),
        result
          ? h("div", { class: "half" },
            h("div", { class: "meta" }, h("span", { class: "seq" }, `#${result.seq}`), h("span", { class: "type" }, "tool.result"), epistemic(result),
              h("span", { class: "muted" }, time(result.timestamp).slice(11), typeof rp.duration_ms === "number" ? ` · ${rp.duration_ms} ms` : "")),
            h("div", {}, h("span", { class: `badge result-${rp.status}` }, rp.status)),
            rp.error ? h("pre", { class: "errtext" }, rp.error) : null,
            rp.result !== undefined ? h("pre", {}, json(rp.result)) : null)
          : h("div", { class: "half missing" }, page.finished
            ? "No result was recorded for this call."
            : "No result recorded yet.")));
    }
    offset += page.pairs.length;
    more.hidden = offset >= page.total;
    status.textContent = page.total ? `${offset} of ${page.total} tool calls shown, in the order they were made.` : "No tool calls were recorded in this run.";
  }

  show(runHeader(detail, "tools"), list, more, status);
  more.hidden = true;
  await load();
}

// -- artifacts -------------------------------------------------------------

async function artifactsTab(e, r, mine) {
  const detail = await api(runPath(e, r));
  if (mine !== generation) return;
  const preview = h("section", { class: "preview" });
  const body = h("tbody", {});
  const status = h("p", { class: "muted" });
  let offset = 0;
  const more = h("button", { type: "button", onclick: () => load() }, `Load ${PAGE} more`);

  async function open(record) {
    fill(preview, h("p", { class: "muted" }, "Loading preview…"));
    try {
      const p = await api(`${runPath(e, r)}/artifacts/${encodeURIComponent(record.artifact_id)}/preview`);
      if (mine !== generation) return;
      const content = p.kind === "image"
        ? h("img", { alt: `Preserved content of ${record.workspace_path}`, src: `${runPath(e, r)}/artifacts/${encodeURIComponent(record.artifact_id)}/image` })
        : p.kind === "text"
          ? [h("pre", { class: "filetext" }, p.text || "(empty file)"), p.truncated ? h("p", { class: "muted" }, "Preview shows the first 256 KB only.") : null]
          : h("p", { class: "notice" }, p.reason);
      fill(preview,
        h("h2", {}, record.workspace_path, h("span", { class: "muted" }, ` · ${record.artifact_id} · revision ${record.revision ?? "?"}`)),
        record.agent_description ? h("p", { class: "claimtext" }, h("span", { class: "ep ep-claim" }, "agent claim"), ` The agent described this file as: “${record.agent_description}”`) : null,
        content,
        h("details", { class: "raw" }, h("summary", {}, "Artifact metadata record"), h("pre", {}, json(record))));
      preview.scrollIntoView({ block: "nearest" });
    } catch (error) {
      fill(preview, h("p", { class: "notice error" }, error.message));
    }
  }

  async function load() {
    const page = await api(`${runPath(e, r)}/artifacts?limit=${PAGE}&offset=${offset}`);
    if (mine !== generation) return;
    for (const a of page.artifacts) {
      const producer = a.producer || {};
      body.append(h("tr", {},
        h("td", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); open(a); } }, a.workspace_path)),
        h("td", { class: "num" }, a.revision ?? "—"),
        h("td", { class: "num" }, bytes(a.size_bytes)),
        h("td", {}, a.media_type || "—"),
        h("td", {}, `${producer.kind || "—"}${producer.capability_id ? ` (${producer.capability_id})` : ""}`),
        h("td", {}, `#${a.observed_at_seq} · ${time(a.observed_at).slice(11)}`),
        h("td", {}, a.preserved === false ? "not preserved" : h("code", {}, `${String(a.sha256).slice(0, 12)}…`))));
    }
    offset += page.artifacts.length;
    more.hidden = offset >= page.total;
    status.textContent = page.total
      ? `${offset} of ${page.total} artifact versions shown, in the order they were observed. Every version of a file is listed.`
      : "No files were observed in the workspace during this run.";
  }

  show(runHeader(detail, "artifacts"),
    h("div", { class: "scroll" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Workspace path", "Rev.", "Size", "Media type (by name)", "Producer", "Observed at", "Content"].map((t) => h("th", {}, t)))),
      body)),
    more, status, preview);
  more.hidden = true;
  await load();
}

// -- routing ---------------------------------------------------------------

function route() {
  clearTimeout(pollTimer);
  const mine = ++generation;
  const [path, queryString] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const query = new URLSearchParams(queryString || "");
  let job;
  if (parts[0] === "run" && parts.length >= 3) {
    const [, e, r, tab] = parts;
    if (tab === "timeline") job = timelineTab(e, r, query, mine);
    else if (tab === "tools") job = toolsTab(e, r, mine);
    else if (tab === "artifacts") job = artifactsTab(e, r, mine);
    else job = summaryTab(e, r, mine);
  } else {
    job = runList(mine);
  }
  job.catch((error) => { if (mine === generation) fail(error); });
}

window.addEventListener("hashchange", route);
route();
