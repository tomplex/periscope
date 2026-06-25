// Unified open omnibox — a command-palette over GET /api/open/catalog.
// Opened by the +new button OR ⌘K / ⌘P. classify() ranks cards per keystroke;
// the shared <Palette> handles ↑/↓/↵ nav, grouped sections, hover-sync, and
// the footer hint bar. open cards POST immediately; worktree/pr cards drill in
// (same field) to fill the descriptor. On success: write response.ui into
// prefsSignal via prefs.setUI (the deferRailAdd replacement) and close.
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { classify } from "../open/classify.js";
import { setUI } from "../prefs.js";
import { track } from "../track.js";
import { apiCall, relTime } from "../util.js";

const open = signal(false);
function openOmnibox() { open.value = true; track("overlay.open", { which: "open" }); }
function close() { open.value = false; }

// kind → (section header, row glyph). Drives the grouped palette rendering.
const KIND_META = {
  open:     { group: "Open",         icon: "▸" },
  worktree: { group: "New worktree", icon: "✦" },
  pr:       { group: "Review PR",    icon: "◆" },
  branch:   { group: "Branches",     icon: "⎇" },
  newbranch:{ group: "Branches",     icon: "+" },
  repo:     { group: "Repositories", icon: "◳" },
  track:    { group: "New track",    icon: "▧" },
  command:  { group: "Command",       icon: "⚡" },
};

const JOBS_POLL_MS = 3000;

// Poll the server-backed job list while the jobs view is open. The list lives
// server-side (the `commands` table); closing/reopening the omnibox reads the
// same rows. Returns the freshest jobs array; the poll tears down on unmount.
function useJobList(active) {
  const [jobs, setJobs] = useState([]);
  useEffect(() => {
    if (!active) return;
    let alive = true, timer = null;
    const poll = async () => {
      const data = await apiCall("jobs", "/api/command/jobs");
      if (!alive) return;
      if (Array.isArray(data)) setJobs(data);
      timer = setTimeout(poll, JOBS_POLL_MS);
    };
    poll();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [active]);
  return jobs;
}

// Fetch a job's transcript on demand. Dispatch is prod-only, so in dev (and for
// a just-dispatched job) /jobs/{id}/turns 404s with "no transcript yet" — apiCall
// surfaces that as a toast and returns null, which we render as a pending state
// rather than crashing. Returns { messages, pending }.
function useJobTurns(jobId) {
  const [messages, setMessages] = useState(null);   // null = not yet loaded
  useEffect(() => {
    if (!jobId) { setMessages(null); return; }
    let alive = true;
    setMessages(null);
    (async () => {
      const data = await apiCall("job-turns", `/api/command/jobs/${encodeURIComponent(jobId)}/turns`);
      if (!alive) return;
      setMessages((data?.messages) || []);
    })();
    return () => { alive = false; };
  }, [jobId]);
  return { messages, pending: messages === null };
}

// One-line summary of a transcript message (the /api/pane/turns shape:
// {role, text, tool_uses?}). Assistant tool calls render as `Name(arg)`; prose
// turns render their first non-empty line; system compact markers as a divider.
function renderTurn(t) {
  if (t.role === "system") return "— context compacted —";
  const role = t.role === "user" ? "❯" : "⏺";
  const tools = t.tool_uses || [];
  if (tools.length) {
    const tu = tools[0];
    const inp = tu.input || {};
    const arg = inp.command || inp.file_path || inp.path || inp.pattern
      || inp.description || inp.query || inp.url || "";
    const extra = tools.length > 1 ? ` +${tools.length - 1}` : "";
    return `${role} ${tu.name || "tool"}${arg ? `(${arg})` : ""}${extra}`;
  }
  const firstLine = String(t.text || "").split("\n").find((l) => l.trim()) || "";
  return `${role} ${firstLine}`;
}

export function OpenOmnibox() {
  useEscape(close, open.value);
  const [catalog, setCatalog] = useState({ repos: [], worktrees: [] });
  const [query, setQuery] = useState("");
  const [drill, setDrill] = useState(null);   // { card } when drilling into worktree/pr
  const [jobsView, setJobsView] = useState(false);   // true when showing the commander job list
  const [selectedJob, setSelectedJob] = useState(null);   // job id whose transcript is open
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Window bridge + global ⌘K/⌘P summon. Registered once; persists while mounted.
  useEffect(() => {
    window.__periscopeOpenOmnibox = openOmnibox;
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "p")) {
        e.preventDefault();
        open.value = !open.value;
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (window.__periscopeOpenOmnibox === openOmnibox) delete window.__periscopeOpenOmnibox;
    };
  }, []);

  useEffect(() => {
    if (!open.value) return;
    setQuery(""); setDrill(null); setSelectedJob(null); setError("");
    (async () => {
      const data = await apiCall("open catalog", "/api/open/catalog");
      if (data) setCatalog(data);
    })();
  }, [open.value]);

  if (!open.value) return null;

  async function post(descriptor) {
    setBusy(true); setError("");
    const data = await apiCall("open", "/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(descriptor),
    });
    setBusy(false);
    if (!data) { setError("open failed"); return; }
    setUI(data.ui);     // synchronous rail placement — no deferRailAdd
    close();
  }

  // Create an empty track (no rail-placement ui payload — it appears as a new
  // top-level group once a tab is tagged into it; an empty track has no live
  // window so it won't render until then). Name comes from the inline NameDrill,
  // NOT window.prompt — prompt() is a silent no-op in the Tauri WKWebView shell.
  async function post_track(repo, name) {
    if (!name) return;
    setBusy(true); setError("");
    const data = await apiCall("create track", "/api/tracks", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: repo || null, name }),
    });
    setBusy(false);
    if (!data) { setError("create track failed"); return; }
    close();
  }

  // Dispatch a free-text command as a fresh `claude --bg` commander, then switch
  // to the job-list view and select the new job. The job runs server-side (the
  // `commands` table) — closing the omnibox doesn't cancel it.
  async function runCommand(text) {
    setBusy(true); setError("");
    const data = await apiCall("command", "/api/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    setBusy(false);
    if (!data) { setError("command failed"); return; }
    setSelectedJob(null);
    setJobsView(true);
  }

  function pick(card) {
    if (card.kind === "open") return post(card.descriptor);
    if (card.kind === "command") return runCommand(card.text);
    if (card.kind === "pr" && !card.needsRepo) return post({ repo: card.pr.repo, pr: card.pr.pr });
    setDrill({ card });   // worktree → branch entry; track → name entry; pr w/o repo → repo picker
  }

  // Main palette items = ranked cards, decorated with section + glyph.
  const items = classify(query, catalog).map((c) => ({
    ...c, group: KIND_META[c.kind].group, icon: KIND_META[c.kind].icon, key: c.label,
  }));

  return (
    <div id="open-omnibox" class="open-omnibox-overlay"
         onClick={(e) => { if (e.target.id === "open-omnibox") close(); }}>
      <div class="open-omnibox-card">
        {!drill && !jobsView && (
          <Palette
            value={query} onInput={setQuery} placeholder="repo, path, #PR…"
            items={items} onPick={pick} onClose={close}
            empty={query ? "no matches" : "type a repo, path, or #PR…"} />
        )}
        {jobsView && (
          <JobList
            selectedJob={selectedJob}
            onSelect={setSelectedJob}
            onBack={selectedJob ? () => setSelectedJob(null)
                                : () => { setJobsView(false); setQuery(""); }}
            onClose={close} />
        )}
        {drill && drill.card.kind === "worktree" && (
          <BranchDrill card={drill.card}
            onPick={(branch) => post({ repo: drill.card.repo, branch })}
            onBack={() => setDrill(null)} />
        )}
        {drill && drill.card.kind === "track" && (
          <NameDrill card={drill.card}
            onPick={(name) => post_track(drill.card.repo, name)}
            onBack={() => setDrill(null)} />
        )}
        {drill && drill.card.kind === "pr" && (
          <RepoDrill repos={catalog.repos}
            onPick={(repo) => post({ repo, pr: drill.card.pr.pr })}
            onBack={() => setDrill(null)} />
        )}
        {error && <div class="open-omnibox-error">{error}</div>}
        {busy && <div class="open-omnibox-busy">opening…</div>}
      </div>
    </div>
  );
}

// The commander job list — server-backed (the `commands` table), polled while
// open. A selected row drills into that job's read-only transcript. Esc pushes
// onto the LIFO stack via useEscape: from a transcript it goes back to the list,
// from the list it leaves the jobs view (onBack carries the right target).
function JobList({ selectedJob, onSelect, onBack }) {
  const jobs = useJobList(!selectedJob);   // pause list polling while a transcript is open
  useEscape(onBack, true);

  if (selectedJob) {
    return <JobTranscript jobId={selectedJob} onBack={onBack} />;
  }

  return (
    <div class="open-omnibox-jobs">
      <div class="open-omnibox-jobs-head">Commands</div>
      <div class="open-omnibox-list">
        {jobs.length === 0 && (
          <div class="open-omnibox-empty">no commands yet</div>
        )}
        {jobs.map((j) => (
          <button class="open-omnibox-row" key={j.id} onClick={() => onSelect(j.id)}>
            <span class={`open-omnibox-jobdot${j.status === "running" ? " is-running" : ""}`}>
              {j.status === "running" ? "●" : "✓"}
            </span>
            <span class="open-omnibox-label">{j.text}</span>
            <span class="open-omnibox-sub">{relTime(j.started_at)}</span>
          </button>
        ))}
      </div>
      <div class="open-omnibox-footer">
        <span><kbd>esc</kbd> back</span>
      </div>
    </div>
  );
}

// Read-only transcript for one job, rendered as one-line turn summaries (the
// same shape /api/pane/turns / /jobs/{id}/turns return). Dispatch is prod-only,
// so a fresh job (or any job in dev) has no JSONL yet — the route 404s and we
// render a pending state instead of crashing.
function JobTranscript({ jobId, onBack }) {
  const { messages, pending } = useJobTurns(jobId);
  return (
    <div class="open-omnibox-console">
      <div class="open-omnibox-console-cmd">
        <button class="open-omnibox-jobback" onClick={onBack}>← back</button>
      </div>
      <div class="open-omnibox-console-log">
        {pending && (
          <div class="open-omnibox-console-spin">no transcript yet — the commander is still starting…</div>
        )}
        {!pending && messages.length === 0 && (
          <div class="open-omnibox-console-spin">no transcript yet</div>
        )}
        {!pending && messages.map((t, i) => (
          <div class="open-omnibox-console-line" key={t.uuid || i}>{renderTurn(t)}</div>
        ))}
      </div>
    </div>
  );
}

// Shared command-palette: input + grouped, keyboard-navigable result list +
// footer hints. `items` is a flat ordered list; rows with the same `group`
// render under one header. `onBack` (drill-ins) makes Esc go back instead of
// closing; `onClose` (top level) lets the footer label Esc as "close".
function Palette({ value, onInput, placeholder, items, onPick, onBack, empty }) {
  const [sel, setSel] = useState(0);
  const listRef = useRef(null);
  const inputRef = useRef(null);
  const n = items.length;
  const active = Math.min(sel, Math.max(0, n - 1));

  // Take focus on mount — `autofocus` doesn't re-fire when the input is
  // conditionally re-mounted (open toggle, drill-in). rAF defers past the
  // overlay paint so the focus sticks.
  useEffect(() => {
    const id = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, []);

  // Reset highlight to the top whenever the query changes.
  useEffect(() => { setSel(0); }, [value]);

  // Keep the active row in view as it moves.
  useEffect(() => {
    listRef.current?.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
  }, [active, n]);

  function onKey(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel(Math.min(active + 1, n - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel(Math.max(active - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (items[active]) onPick(items[active]); }
    else if (e.key === "Escape" && onBack) { e.preventDefault(); e.stopPropagation(); onBack(); }
  }

  let lastGroup = null;
  return (
    <>
      <input class="open-omnibox-input" ref={inputRef} placeholder={placeholder}
             value={value} onInput={(e) => onInput(e.target.value)} onKeyDown={onKey} />
      <div class="open-omnibox-list" ref={listRef}>
        {n === 0 && <div class="open-omnibox-empty">{empty}</div>}
        {items.map((it, i) => {
          const header = it.group && it.group !== lastGroup
            ? <div class="open-omnibox-group" key={`g:${it.group}`}>{it.group}</div> : null;
          lastGroup = it.group;
          return (
            <>
              {header}
              <button key={it.key || i}
                      class={`open-omnibox-row${i === active ? " is-active" : ""}`}
                      onMouseMove={() => setSel(i)} onClick={() => onPick(it)}>
                {it.icon && <span class="open-omnibox-icon">{it.icon}</span>}
                <span class="open-omnibox-label">{it.label}</span>
                {it.sub && <span class="open-omnibox-sub">{it.sub}</span>}
              </button>
            </>
          );
        })}
      </div>
      <div class="open-omnibox-footer">
        <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
        <span><kbd>↵</kbd> {onBack ? "select" : "open"}</span>
        <span><kbd>esc</kbd> {onBack ? "back" : "close"}</span>
      </div>
    </>
  );
}

function BranchDrill({ card, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (card.branches || []).filter((b) => b.toLowerCase().includes(val.toLowerCase()));
  const items = matches.map((b) => ({ key: b, group: "Branches", icon: "⎇", label: b, value: b }));
  if (val && !matches.includes(val)) {
    items.push({ key: `__new:${val}`, group: "Branches", icon: "+",
                 label: `new branch "${val}"`, value: val });
  }
  return (
    <Palette value={val} onInput={setVal} placeholder={`branch in ${card.repo}…`}
             items={items} onPick={(it) => onPick(it.value)} onBack={onBack}
             empty="type a new branch name…" />
  );
}

// Name entry for a new track. Typing a name surfaces a single "create" card;
// Enter (or click) creates it. Inline so it works in the Tauri shell, where
// window.prompt is a silent no-op.
function NameDrill({ card, onPick, onBack }) {
  const [val, setVal] = useState("");
  const name = val.trim();
  const items = name
    ? [{ key: `__tk:${name}`, group: "New track", icon: "▧",
         label: `create track "${name}"`, sub: card.repo, value: name }]
    : [];
  return (
    <Palette value={val} onInput={setVal} placeholder={`name this track (repo: ${card.repo})…`}
             items={items} onPick={(it) => onPick(it.value)} onBack={onBack}
             empty="type a track name…" />
  );
}

function RepoDrill({ repos, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (repos || []).filter(
    (r) => r.label.toLowerCase().includes(val.toLowerCase()) || r.repo.toLowerCase().includes(val.toLowerCase()));
  const items = matches.map((r) => ({ key: r.repo, group: "Repositories", icon: "◳",
                                      label: r.label, sub: r.repo, value: r.repo }));
  return (
    <Palette value={val} onInput={setVal} placeholder="repo for this PR…"
             items={items} onPick={(it) => onPick(it.value)} onBack={onBack}
             empty="no matching repo" />
  );
}
