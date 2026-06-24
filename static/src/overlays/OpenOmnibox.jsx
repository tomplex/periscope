// Unified open omnibox — a command-palette over GET /api/open/catalog.
// Opened by the +new button OR ⌘K / ⌘P. classify() ranks cards per keystroke;
// the shared <Palette> handles ↑/↓/↵ nav, grouped sections, hover-sync, and
// the footer hint bar. open cards POST immediately; worktree/pr cards drill in
// (same field) to fill the descriptor. On success: write response.ui into
// prefsSignal via prefs.setUI (the deferRailAdd replacement) and close.
import { signal } from "@preact/signals";
import { useEffect, useState, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";
import { setUI } from "../prefs.js";
import { classify } from "../open/classify.js";

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
  workspace:{ group: "Workspaces",   icon: "▧" },
  command:  { group: "Command",       icon: "⚡" },
};

// Commander console: POST the command, then tail the commander pane's
// transcript via /api/pane/turns until it goes idle (no new turn for ~4s).
// Esc dismisses the console but the command keeps running server-side — the
// poll's `alive` flag tears down the timer on unmount without cancelling work.
function useCommanderConsole(text, onError) {
  const [lines, setLines] = useState([]);   // rendered transcript messages
  const [running, setRunning] = useState(true);
  useEffect(() => {
    let alive = true, pane = null, timer = null, lastGrow = Date.now();
    (async () => {
      const data = await apiCall("command", "/api/command", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!alive) return;
      if (!data) { setRunning(false); onError?.("command failed"); return; }
      pane = data;   // { session, index }
      const poll = async () => {
        if (!alive) return;
        const t = await apiCall("turns",
          `/api/pane/turns?session=${encodeURIComponent(pane.session)}&index=${pane.index}`);
        if (!alive) return;
        // /api/pane/turns returns {messages:[…]} (or {turns:null} before a
        // transcript exists) — read messages, not turns.
        const msgs = (t && t.messages) || [];
        setLines((prev) => {
          if (msgs.length > prev.length) lastGrow = Date.now();
          return msgs;
        });
        // idle: no growth for 4s after at least one turn landed.
        if (msgs.length > 0 && Date.now() - lastGrow > 4000) { setRunning(false); return; }
        timer = setTimeout(poll, 1000);
      };
      poll();
    })();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [text]);
  return { lines, running };
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
  const [console_, setConsole] = useState(null);   // { text } when in commander-console mode
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
    setQuery(""); setDrill(null); setConsole(null); setError("");
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

  // Create a parked workspace (no rail-placement ui payload — it appears as a
  // new top-level group on the next /api/state poll), then close. Name comes
  // from the inline NameDrill, NOT window.prompt — prompt() is a silent no-op
  // in the Tauri WKWebView shell.
  async function post_workspace(repo, name) {
    if (!name) return;
    setBusy(true); setError("");
    const data = await apiCall("create workspace", "/api/workspaces", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_repo: repo || null, name }),
    });
    setBusy(false);
    if (!data) { setError("create workspace failed"); return; }
    close();
  }

  function pick(card) {
    if (card.kind === "open") return post(card.descriptor);
    if (card.kind === "command") return setConsole({ text: card.text });
    if (card.kind === "pr" && !card.needsRepo) return post({ repo: card.pr.repo, pr: card.pr.pr });
    setDrill({ card });   // worktree → branch entry; workspace → name entry; pr w/o repo → repo picker
  }

  // Main palette items = ranked cards, decorated with section + glyph.
  const items = classify(query, catalog).map((c) => ({
    ...c, group: KIND_META[c.kind].group, icon: KIND_META[c.kind].icon, key: c.label,
  }));

  return (
    <div id="open-omnibox" class="open-omnibox-overlay"
         onClick={(e) => { if (e.target.id === "open-omnibox") close(); }}>
      <div class="open-omnibox-card">
        {!drill && !console_ && (
          <Palette
            value={query} onInput={setQuery} placeholder="repo, path, #PR…"
            items={items} onPick={pick} onClose={close}
            empty={query ? "no matches" : "type a repo, path, or #PR…"} />
        )}
        {console_ && (
          <CommanderConsole text={console_.text}
            onClose={() => { setConsole(null); close(); }}
            onError={setError} />
        )}
        {drill && drill.card.kind === "worktree" && (
          <BranchDrill card={drill.card}
            onPick={(branch) => post({ repo: drill.card.repo, branch })}
            onBack={() => setDrill(null)} />
        )}
        {drill && drill.card.kind === "workspace" && (
          <NameDrill card={drill.card}
            onPick={(name) => post_workspace(drill.card.repo, name)}
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

// Commander console render: the command line + a scrollable transcript log +
// a working spinner. Esc dismisses (the command keeps running server-side); the
// nested useEscape pushes onto the LIFO stack so it wins over OpenOmnibox's close.
function CommanderConsole({ text, onClose, onError }) {
  const { lines, running } = useCommanderConsole(text, onError);
  const logRef = useRef(null);
  useEscape(onClose, true);
  // Keep the newest turn in view as the transcript grows.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, running]);
  return (
    <div class="open-omnibox-console">
      <div class="open-omnibox-console-cmd">⚡ {text}</div>
      <div class="open-omnibox-console-log" ref={logRef}>
        {lines.map((t, i) => (
          <div class="open-omnibox-console-line" key={t.uuid || i}>{renderTurn(t)}</div>
        ))}
        {running && <div class="open-omnibox-console-spin">commander working…</div>}
        {!running && lines.length === 0 && (
          <div class="open-omnibox-console-spin">no transcript yet — esc to dismiss</div>
        )}
      </div>
    </div>
  );
}

// Shared command-palette: input + grouped, keyboard-navigable result list +
// footer hints. `items` is a flat ordered list; rows with the same `group`
// render under one header. `onBack` (drill-ins) makes Esc go back instead of
// closing; `onClose` (top level) lets the footer label Esc as "close".
function Palette({ value, onInput, placeholder, items, onPick, onBack, onClose, empty }) {
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

// Name entry for a new workspace. Typing a name surfaces a single "create"
// card; Enter (or click) creates it. Inline so it works in the Tauri shell,
// where window.prompt is a silent no-op.
function NameDrill({ card, onPick, onBack }) {
  const [val, setVal] = useState("");
  const name = val.trim();
  const items = name
    ? [{ key: `__ws:${name}`, group: "New workspace", icon: "▧",
         label: `create workspace "${name}"`, sub: card.repo, value: name }]
    : [];
  return (
    <Palette value={val} onInput={setVal} placeholder={`name this workspace (base: ${card.repo})…`}
             items={items} onPick={(it) => onPick(it.value)} onBack={onBack}
             empty="type a workspace name…" />
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
