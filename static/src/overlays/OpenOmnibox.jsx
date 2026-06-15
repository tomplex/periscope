// Unified open omnibox. Opened by the +new button via window.__periscopeOpenOmnibox.
// Loads GET /api/open/catalog once on open; classify() ranks cards per keystroke.
// open cards POST immediately; worktree/pr cards drill in (same field) to fill
// the descriptor, then POST. On success: write response.ui into prefsSignal via
// prefs.setUI (the deferRailAdd replacement) and close.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";
import { setUI } from "../prefs.js";
import { classify } from "../open/classify.js";

const open = signal(false);
function openOmnibox() { open.value = true; track("overlay.open", { which: "open" }); }
function close() { open.value = false; }

export function OpenOmnibox() {
  useEscape(close, open.value);
  const [catalog, setCatalog] = useState({ repos: [], worktrees: [] });
  const [query, setQuery] = useState("");
  const [drill, setDrill] = useState(null);   // { card } when drilling into worktree/pr
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    window.__periscopeOpenOmnibox = openOmnibox;
    return () => { if (window.__periscopeOpenOmnibox === openOmnibox) delete window.__periscopeOpenOmnibox; };
  }, []);

  useEffect(() => {
    if (!open.value) return;
    setQuery(""); setDrill(null); setError("");
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

  function pick(card) {
    if (card.kind === "open") return post(card.descriptor);
    if (card.kind === "pr" && !card.needsRepo) return post({ repo: card.pr.repo, pr: card.pr.pr });
    setDrill({ card });   // worktree → branch entry; pr w/o repo → repo picker
  }

  const cards = drill ? [] : classify(query, catalog);

  return (
    <div id="open-omnibox" class="open-omnibox-overlay"
         onClick={(e) => { if (e.target.id === "open-omnibox") close(); }}>
      <div class="open-omnibox-card">
        {!drill && (
          <>
            <input class="open-omnibox-input" autofocus placeholder="repo, path, #PR…"
                   value={query} onInput={(e) => setQuery(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && cards[0]) pick(cards[0]); }} />
            <div class="open-omnibox-list">
              {cards.map((c) => (
                <button key={c.label} class={`open-omnibox-row kind-${c.kind}`} onClick={() => pick(c)}>
                  {c.label}
                </button>
              ))}
            </div>
          </>
        )}
        {drill && drill.card.kind === "worktree" && (
          <BranchDrill card={drill.card} onPick={(branch) =>
            post({ repo: drill.card.repo, branch })} onBack={() => setDrill(null)} />
        )}
        {drill && drill.card.kind === "pr" && (
          <RepoDrill repos={catalog.repos} onPick={(repo) =>
            post({ repo, pr: drill.card.pr.pr })} onBack={() => setDrill(null)} />
        )}
        {error && <div class="open-omnibox-error">{error}</div>}
        {busy && <div class="open-omnibox-busy">opening…</div>}
      </div>
    </div>
  );
}

function BranchDrill({ card, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (card.branches || []).filter((b) => b.includes(val));
  return (
    <>
      <input class="open-omnibox-input" autofocus placeholder={`branch in ${card.repo}…`}
             value={val} onInput={(e) => setVal(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") onPick(val || matches[0]); }} />
      <div class="open-omnibox-list">
        {matches.map((b) => (
          <button key={b} class="open-omnibox-row" onClick={() => onPick(b)}>{b}</button>
        ))}
        {val && !matches.includes(val) && (
          <button class="open-omnibox-row kind-new" onClick={() => onPick(val)}>
            new branch "{val}"
          </button>
        )}
      </div>
      <button class="open-omnibox-back" onClick={onBack}>← back</button>
    </>
  );
}

function RepoDrill({ repos, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (repos || []).filter((r) => r.label.includes(val) || r.repo.includes(val));
  return (
    <>
      <input class="open-omnibox-input" autofocus placeholder="repo for this PR…"
             value={val} onInput={(e) => setVal(e.target.value)} />
      <div class="open-omnibox-list">
        {matches.map((r) => (
          <button key={r.repo} class="open-omnibox-row" onClick={() => onPick(r.repo)}>{r.label}</button>
        ))}
      </div>
      <button class="open-omnibox-back" onClick={onBack}>← back</button>
    </>
  );
}
