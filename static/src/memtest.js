// Self-driving pane-switch sweeps for the 2026-07 webview memory-leak
// bisection. DORMANT in normal use: boot() probes /memtest.json once and this
// module does nothing more unless that file exists (it is deliberately NOT
// committed — the investigating session drops it into static/ to arm a sweep
// and deletes it afterwards). While armed, the file is re-polled for a nonce
// change, so successive sweeps need no reload; progress and completion are
// reported through track() → /api/events → the ui_events table, which the
// driving session reads back out of periscope.db.
//
// Why this exists: the leak reproduces only in the real WKWebView (Playwright
// WebKit plateaus — see the 2026-07-28 handoff), and nothing outside the page
// can drive the rail there. Selection is flipped exactly the way a rail click
// does it (railSelection.value = "pane:<pid>") EXCEPT prefs.setLastSelected,
// so a sweep never clobbers the user's persisted selection.
//
// Config shape (all optional but nonce):
//   { "nonce": 1, "switches": 20, "settleMs": 2000, "mode": "terminal",
//     "kind": "claude", "blockWs": false, "pids": ["@12", "@34"] }
// blockWs mocks window.WebSocket for /ws/pane only — the terminal mounts and
// fits, but no socket connects and no initial paint arrives. That isolates
// the mount/DOM path from the data path.
import { getDetailMode, setDetailMode, setLastSelected } from "./prefs.js";
import { closeFileTab, openFileTab, railSelection, setActiveTab, windows } from "./store.js";
import { track } from "./track.js";

const POLL_MS = 3000;

let lastNonce = null;
let running = false;
let wsPatched = false;
let blockPaneWs = false;

// Wrap window.WebSocket so /ws/pane construction returns an inert stub
// (readyState stays CONNECTING → terminalCore never sends, never reconnects).
// /ws/state and everything else pass through untouched.
function patchWs() {
  if (wsPatched) return;
  wsPatched = true;
  const Real = window.WebSocket;
  function stub() {
    return {
      readyState: 0,
      binaryType: "arraybuffer",
      send() {},
      close() {},
      onopen: null,
      onmessage: null,
      onerror: null,
      onclose: null,
    };
  }
  function Wrapped(url, protos) {
    if (blockPaneWs && String(url).includes("/ws/pane")) return stub();
    return protos === undefined ? new Real(url) : new Real(url, protos);
  }
  Wrapped.CONNECTING = Real.CONNECTING;
  Wrapped.OPEN = Real.OPEN;
  Wrapped.CLOSING = Real.CLOSING;
  Wrapped.CLOSED = Real.CLOSED;
  Wrapped.prototype = Real.prototype;
  window.WebSocket = Wrapped;
}

async function sweep(cfg) {
  running = true;
  const settle = cfg.settleMs || 2000;
  const n = cfg.switches || 20;
  const wantClaude = (cfg.kind || "claude") === "claude";
  const pool = (windows.value || []).filter((w) => !!w.is_claude === wantClaude && w.pid);
  // poolSize > 2 cycles through that many distinct panes round-robin — the
  // manual reproduction switches among MANY panes, and per-unique-pane
  // allocations (transcript hosts, preview tabs) only show up that way.
  const take = Math.max(2, cfg.poolSize || (cfg.pids?.length ?? 2));
  const pids = cfg.pids?.length >= 2 ? cfg.pids : pool.slice(0, take).map((w) => w.pid);
  const origTitle = document.title;
  if (pids.length < 2) {
    track("memtest.abort", { nonce: cfg.nonce, reason: "need 2 pids" });
    running = false;
    return;
  }
  const origSel = railSelection.value;
  const origModes = pids.map((p) => getDetailMode(p));
  const mode = cfg.mode || "terminal";
  // realClick keeps each pane's natural detail mode — fidelity to manual use.
  if (!cfg.realClick) for (const p of pids) setDetailMode(p, mode);
  blockPaneWs = !!cfg.blockWs;
  if (blockPaneWs) patchWs();
  const rail = document.getElementById("rail") || document.querySelector(".rail, #left");
  track("memtest.start", {
    nonce: cfg.nonce, n, mode, blockWs: blockPaneWs, pids,
    realClick: !!cfg.realClick, scrollRail: !!cfg.scrollRail, hoverStorm: !!cfg.hoverStorm,
    railScroll: rail ? { sh: rail.scrollHeight, ch: rail.clientHeight } : null,
    noWebgl: (() => { try { return localStorage.getItem("periscope.noWebgl") === "1"; } catch (_) { return null; } })(),
  });
  // tabPath arm: after each pane lands, open (first visit) or front the HTML
  // preview tab, then toggle back to the terminal — the manual repro's
  // "clicking between the term and an HTML file tab" pattern. Kept-mounted
  // iframe hosts get display-toggled on every subsequent pane switch.
  const tabPath = cfg.tabPath || null;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  // realClick arm: dispatch actual clicks on the rendered rail rows in
  // up-then-down order — the full click path (setLastSelected → prefs PATCH →
  // prefs-signal re-render), which the signal-flip path deliberately skips.
  // Rows are re-queried per click; the rail re-renders on every poll.
  let clickIdx = 0;
  let clickDir = 1;
  function realClickStep() {
    const rows = document.querySelectorAll(".rail-row.pane-row");
    if (rows.length < 2) return;
    const span = Math.min(rows.length, Math.max(2, cfg.poolSize || rows.length));
    if (clickIdx >= span) clickIdx = span - 1;
    const row = rows[clickIdx];
    // scrollRail: bring the row into view the way a human reaches it — real
    // smooth scrolling of the rail, churning scroll tiles. element.click()
    // alone never scrolls, which is the one thing no previous arm did.
    if (cfg.scrollRail) row.scrollIntoView({ behavior: "smooth", block: "center" });
    // hoverStorm: sweep mouseover/mouseout across every row between clicks —
    // closest synthetic stand-in for the pointer traversing the rail.
    if (cfg.hoverStorm) {
      for (const r of rows) {
        r.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        r.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
      }
    }
    row.click();
    clickIdx += clickDir;
    if (clickIdx >= span - 1 || clickIdx <= 0) clickDir = -clickDir;
  }
  for (let i = 1; i <= n; i++) {
    const pid = pids[i % pids.length];
    if (cfg.realClick) realClickStep();
    else railSelection.value = `pane:${pid}`;
    document.title = `memtest ${i}/${n}`;
    await wait(tabPath ? settle / 3 : settle);
    if (tabPath) {
      openFileTab({ path: tabPath }); // needs activeTarget — set by the render above
      await wait(settle / 3);
      setActiveTab(pid, "pane");
      await wait(settle / 3);
    }
    if (i % 5 === 0) track("memtest.progress", { nonce: cfg.nonce, i });
  }
  if (tabPath) for (const p of pids) closeFileTab(p, tabPath);
  blockPaneWs = false;
  if (!cfg.realClick) for (const [j, p] of pids.entries()) setDetailMode(p, origModes[j] || "terminal");
  railSelection.value = origSel;
  // realClick's row handlers rewrote last_selected in prefs; put it back.
  if (cfg.realClick && origSel?.startsWith("pane:")) {
    setLastSelected({ kind: "pane", pid: origSel.slice("pane:".length) });
  }
  document.title = origTitle;
  track("memtest.done", { nonce: cfg.nonce, n, mode });
  running = false;
}

async function readConfig() {
  try {
    const res = await fetch("/memtest.json", { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;
  }
}

// One probe at boot; only an armed instance (file present) keeps polling, so
// a normal prod boot costs a single 404 and nothing recurring.
export async function startMemtest() {
  const first = await readConfig();
  if (first === null) return;
  lastNonce = first.nonce ?? null;
  setInterval(async () => {
    if (running) return;
    const cfg = await readConfig();
    if (!cfg || cfg.nonce == null || cfg.nonce === lastNonce) return;
    lastNonce = cfg.nonce;
    sweep(cfg);
  }, POLL_MS);
}
