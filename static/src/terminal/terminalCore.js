// Imperative xterm.js + /ws/pane WebSocket lifecycle. Ported VERBATIM from
// static/terminal.js + static/terminal-mount.js — this stays imperative on
// purpose (CLAUDE.md invariants #3/#4 + the reconnect FSM live here). The
// <Terminal> Preact wrapper (Terminal.jsx) is a thin ref+useEffect shell over
// these functions; do NOT "Preact-ify" the xterm/WS logic.
//
// A fresh Terminal is created per mount and disposed on unmount, so each pane
// gets clean state. The WS auto-reconnects on unclean close (e.g. while Claude
// is editing server.py and uvicorn reloads). The xterm instance is preserved
// across reconnects, and the server's initial-paint code re-syncs xterm to
// tmux's current state, so a reload is invisible to the user.
//
// Terminal / FitAddon come from the vendored xterm <script> tags on window
// (not an npm import) — same as the vanilla path.
//
// All terminal state stays private to this module — the rest of the app talks
// to it through the exported functions.

import { openExternal } from "../tauri.js";
import { track } from "../track.js";
import { paneIdQuery } from "../util.js";
import { afterSend, NOTHING_SENT, pinWidth, resizeAsk, xtermDims } from "./sizing.js";
import { terminalTheme } from "./theme.js";

// Scalar Terminal options, exported so the unicode11 regression test can
// construct a vendored Terminal with exactly what production passes.
// allowProposedApi is load-bearing: `term.unicode` (the Unicode 11 width
// activation below) is proposed API, and without the flag the activation
// THROWS — swallowed by its try/catch into a console.warn, leaving emoji
// at width 1 and the mirrored cursor one visible cell ahead of where
// typed text lands (a 🐍 in the shell prompt reproduced it every time).
export const XTERM_OPTIONS = {
  allowProposedApi: true,
  fontFamily: '"IBM Plex Mono", "JetBrains Mono", "Menlo", monospace',
  fontSize: 12,
  cursorBlink: true,
  // Holds the initial paint (up to 10k lines from capture-pane in ws.py)
  // plus everything that streams in afterwards. 20k gives modals room to
  // grow during a long open session without dropping the most-recent
  // pre-modal scrollback off the top.
  scrollback: 20000,
  convertEol: false,
  // Option key → Meta escape prefix, so Option+Backspace becomes the
  // readline word-back-delete sequence (ESC + DEL), Option+Left becomes
  // ESC + b (word back), etc. Claude Code's input box honors these.
  macOptionIsMeta: true,
  cursorStyle: "block",
  fontWeight: "400",
};

let term = null;
let termWs = null;
let termWsTarget = null;            // target the current/pending socket is for
let termIntentionalClose = false;   // suppress reconnect when we close on purpose
let termReconnectTimer = null;
let termReconnectAttempt = 0;
let termReconnectedNotified = false; // only print "reconnecting…" once per outage
let termAgent = null;
// The pane's app-level mouse reporting state, pushed by the server at attach
// (tmux eats the DECSET, so xterm can't observe it — see the wheel handler).
let mouseReportingOn = false;
// Wheel scroll sensitivity. We emit one SGR wheel report per this many lines
// of accumulated (cell-height-normalized) travel — mirroring the old
// wheel→arrow cadence, which normalized by cell height rather than firing per
// DOM event. Higher = coarser/slower. `wheelAccumPx` carries sub-line remainder
// between events so slow trackpad scrolls still register.
const WHEEL_LINES_PER_TICK = 1;
let wheelAccumPx = 0;
let fitAddon = null;
let webglAddon = null;
let searchAddon = null;
let termResizeObserver = null;
let fitDebounce = null;
let fitHeld = false;              // a splitter drag is in progress — see holdTerminalFit
let lastSent = NOTHING_SENT;      // what this mount last asked tmux for (afterSend) — kept across reconnects, reset by stopLiveTerminal

const CODEX_PANEL_BG = "\x1b[48;2;30;30;30m";
const PERISCOPE_TERM_BG = "\x1b[48;2;40;44;52m";
const textEncoder = new TextEncoder();
const codexPanelBgBytes = textEncoder.encode(CODEX_PANEL_BG);
const periscopeTermBgBytes = textEncoder.encode(PERISCOPE_TERM_BG);

// Codex paints its composer and permission dialogs #1e1e1e with a truecolor
// escape, bypassing xterm's configurable ANSI palette. Remap only that exact
// Codex-owned background at the render edge; every other color passes through.
export function normalizeAgentTerminalData(data, agent) {
  if (agent !== "codex") return data;
  if (typeof data === "string") {
    return data.replaceAll(CODEX_PANEL_BG, PERISCOPE_TERM_BG);
  }
  const input = data instanceof Uint8Array ? data : new Uint8Array(data);
  let output = null;
  for (let i = 0; i <= input.length - codexPanelBgBytes.length; i += 1) {
    let match = true;
    for (let j = 0; j < codexPanelBgBytes.length; j += 1) {
      if (input[i + j] !== codexPanelBgBytes[j]) {
        match = false;
        break;
      }
    }
    if (!match) continue;
    if (!output) output = input.slice();
    output.set(periscopeTermBgBytes, i);
    i += codexPanelBgBytes.length - 1;
  }
  return output || input;
}
// Width is pinned for the session. Navigating panes re-runs startLiveTerminal
// with a FRESH xterm + a fresh fit(); a mount-time measurement that differs by
// a column or two (scrollbar appearing, layout rounding) would otherwise resize
// tmux and reflow scrollback — surfacing as the same block duplicated at two
// wrap widths, even though the user never resized. We snap such wobble back to
// the pinned width and only accept a genuinely different width (real window /
// rail resize, or a different container like the modal) as a new pin.
// containerDims is the last measurement (measureContainer): the pinned width
// and the fitted rows, 0 until something is measured this page. It's what
// every resize and reconnect asks tmux for — never term.cols/term.rows, the
// pane's (paneRows): on a split those are a slice of the window, and
// resize-window applies the ask to the whole window, so echoing them back
// shrank a split window on every reconnect. Nothing measured yet (a pane only
// ever shown hidden): no hint at all. It outlives mounts on purpose — a mount
// into a hidden host can't measure, and hints the last measurement.
let containerDims = { cols: 0, rows: 0 };
// The xterm's ROWS follow the tmux pane (the server's `{type:"size"}`), never
// the container — capped at it, never grown to it (xtermDims). The reconcile
// frame paints rows 1..pane_height, so any extra xterm rows are rows tmux
// never repaints — and xterm.js GROWING rows pulls scrollback down over the
// screen, so they held a frozen older copy of the pane under the live one. A
// pane is shorter than the container both when its window is split (periscope
// sizes the window, the pane gets a slice) and after the container grows in
// height only, which is deliberately not sent (resizeAsk).
// Cols follow the pane the same way, so a left/right split shows the pane's
// width instead of flipping between it and the container's on every fit — but
// uncapped (xtermDims).
let paneCols = 0;                // tmux's pane dims, from the last size message
let paneRows = 0;

// Measure the container into containerDims, holding the pin. False when it
// can't be measured: under a display:none host proposeDimensions gives NaN
// cols, and the last measurement stands.
function measureContainer() {
  let proposed;
  try {
    proposed = fitAddon.proposeDimensions();
  } catch (_) {
    return false;
  }
  if (!proposed || Number.isNaN(proposed.cols) || Number.isNaN(proposed.rows)) return false;
  containerDims = { cols: pinWidth(proposed.cols, containerDims.cols), rows: proposed.rows };
  return true;
}

// Size the xterm from what is known of the pane (size messages) and the
// container (measureContainer), by xtermDims. Never fit(): that grows the
// xterm to the container, and growing rows drags scrollback down over the
// screen (see paneRows).
function syncXtermSize() {
  const next = xtermDims(paneCols, paneRows, containerDims.cols, containerDims.rows);
  if (next.cols > 0 && next.rows > 0 && (term.cols !== next.cols || term.rows !== next.rows)) {
    term.resize(next.cols, next.rows);
  }
}
let containerEl = null;          // set by setTerminalContainer() before startLiveTerminal()
let xtermHost = null;            // persistent parent for term.open(); moved between mounts

// Whether the viewport is at the live bottom. The detail pane polls this to
// show its scroll-to-bottom button only when scrolled up. True when there's no
// terminal (nothing to scroll) or in alt-screen (no scrollback).
// Ask the server for an authoritative repaint of this pane (⌘R with a terminal
// focused). The mirror already self-heals on a timer; this is the manual pull
// for when it has visibly drifted and you don't want to wait for the tick.
// No-op when no socket is up — there is nothing to repaint.
export function requestTerminalReconcile() {
  if (termWs && termWs.readyState === WebSocket.OPEN) {
    termWs.send(JSON.stringify({ type: "reconcile" }));
  }
}

export function isTerminalAtBottom() {
  if (!term) return true;
  const b = term.buffer.active;
  return b.viewportY >= b.baseY;
}

// Mount target for the live xterm. Must be called before startLiveTerminal().
// Consumers: <Modal> and <Detail> (via mountTerminal).
export function setTerminalContainer(el) {
  containerEl = el;
}

let fileLinkCallback = null;

// Register a callback for absolute/relative file path clicks. Callback
// signature: (rawPath: string) => void. .md paths take this route too
// (the preview overlay renders markdown). Parents (Detail.jsx, Modal.jsx)
// register this to open the preview overlay.
export function setTerminalFileCallback(cb) {
  fileLinkCallback = cb;
}

// URL: http/https/ws/wss with no embedded whitespace; conservative on what
// terminates — most CLI URLs don't include trailing punctuation we'd want
// to strip, so we eat as much as possible up to whitespace or quote/paren.
const URL_RE = /\b(?:https?|wss?):\/\/[^\s)"'`<>]+/g;

// Absolute file path: starts with /, followed by path chars. Excludes
// trailing punctuation (.,)";:) — common in prose. Bounded on the left by
// a word boundary or start-of-line.
// Note the look-behind excludes ':' — without it, the regex eats the
// `//host/path` portion of URLs like `http://foo.com/bar.md`, producing
// a spurious overlapping link. URL_RE's match still wins via push order
// today, but tightening the regex removes that order dependency.
const ABS_PATH_RE = /(?<![:\w./-])\/[\w./-]+\b(?::\d+)?/g;

// Relative path with a known extension. The ext list intentionally errs
// toward false negatives — Cmd+click on garbage in scrollback is worse
// than a missed click. Expand cautiously when concrete misses surface.
// Same ':' exclusion as ABS_PATH_RE for the same URL-overlap reason.
//
// Two forms:
//  - `./foo/bar.py` or `../foo/bar.py` (explicit `./` or `../` prefix)
//  - `src/foo/bar.py` (bare path REQUIRING at least one '/' in the body,
//    so `foo.py` alone is NOT clickable but `path/to/foo.py` is). Claude
//    commonly emits bare relative paths in `Read("src/foo.py")` etc; the
//    `/`-required guard keeps stray "version 1.2.3.json" prose unclicked.
const REL_PATH_RE = /(?<![:\w./-])(?:\.{1,2}\/[\w./-]+|[\w.-]+(?:\/[\w.-]+)+)\.(?:md|py|ts|tsx|js|jsx|json|html|css|rs|go|toml|yaml|yml|sh|sql|txt|rb)\b(?::\d+)?/g;

// One routing link provider. xterm supports N providers but a single
// routing provider keeps the regex passes minimal (one per row) and routing
// decisions in one place. Precedence:
//   1. URL — handed to urlLinkCallback if registered, else openExternal
//      (browser: new tab; Tauri: OS browser via plugin-opener).
//   2. File path (any ext in REL_PATH_RE, plus any absolute path) —
//      handed to fileLinkCallback. The preview overlay routes by
//      extension (md → rendered markdown, html → iframe, else source).
// Custom URL click handler. Installed on the xterm container's mousedown
// and mouseup in capture phase. Bypasses xterm's link-manager activate
// path, which races a TUI mouse-reporting redraw on Claude's pane —
// Claude redraws on receiving the forwarded mousedown, xterm rerenders
// the row, the link manager clears _currentLink, and mouseup fires with
// no link to activate. The hover decoration still works (it doesn't
// depend on the click path), so users see the URL is clickable.
//
// We do NOT preventDefault — selection / mouse-reporting / scrolling
// all continue to work. The URL just also opens.
let urlMouseDownHandler = null;
let urlMouseUpHandler = null;
let pendingUrlClick = null;     // { url, x, y } across mousedown→mouseup

function urlAtClick(e, t) {
  if (!t) return null;
  // Use .xterm-screen (the character grid) rather than the outer container,
  // so the scrollbar / padding don't skew the pixel→cell math.
  const screen = t.element?.querySelector(".xterm-screen");
  if (!screen) return null;
  const rect = screen.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const charW = rect.width / t.cols;
  const charH = rect.height / t.rows;
  const cx = Math.floor((e.clientX - rect.left) / charW);
  const cy = Math.floor((e.clientY - rect.top) / charH);
  if (cx < 0 || cx >= t.cols || cy < 0 || cy >= t.rows) return null;
  const buf = t.buffer.active;
  const line = buf.getLine(buf.viewportY + cy);
  if (!line) return null;
  const text = line.translateToString(true);
  for (const m of text.matchAll(URL_RE)) {
    const s = m.index;
    const e2 = s + m[0].length;
    if (cx >= s && cx < e2) return m[0];
  }
  return null;
}

// Pixel → 1-based (col,row) on the .xterm-screen grid, for SGR mouse coords,
// plus the cell height so the wheel handler can normalize pixel deltas into
// lines. Same screen-relative math as urlAtClick (padding/scrollbar-safe),
// clamped into range so a report always lands on a valid cell.
function wheelCell(e, t) {
  const screen = t.element?.querySelector(".xterm-screen");
  if (!screen) return null;
  const rect = screen.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const cellH = rect.height / t.rows;
  const col = Math.floor((e.clientX - rect.left) / (rect.width / t.cols)) + 1;
  const row = Math.floor((e.clientY - rect.top) / cellH) + 1;
  return {
    col: Math.min(Math.max(col, 1), t.cols),
    row: Math.min(Math.max(row, 1), t.rows),
    cellH,
    pageH: rect.height,
  };
}

function installUrlClickHandler(t) {
  const el = t.element;
  if (!el) return;
  urlMouseDownHandler = (e) => {
    if (e.button !== 0) return;            // left click only
    const url = urlAtClick(e, t);
    if (!url) return;
    pendingUrlClick = { url, x: e.clientX, y: e.clientY };
  };
  urlMouseUpHandler = (e) => {
    if (e.button !== 0 || !pendingUrlClick) return;
    // Require small movement so drag-to-select doesn't trigger an open.
    const dx = Math.abs(e.clientX - pendingUrlClick.x);
    const dy = Math.abs(e.clientY - pendingUrlClick.y);
    const { url } = pendingUrlClick;
    pendingUrlClick = null;
    if (dx > 4 || dy > 4) return;
    const ok = openExternal(url);
    if (!ok) {
      t.writeln(`\r\n\x1b[31m[periscope: failed to open ${url}]\x1b[0m`);
    }
  };
  // No uninstall counterpart: these bind to term.element, which lives inside
  // the parked host for the page's lifetime, so they're installed exactly once
  // alongside the terminal itself (see ensureTerminal).
  el.addEventListener("mousedown", urlMouseDownHandler, true);
  el.addEventListener("mouseup", urlMouseUpHandler, true);
}

function registerRoutingLinkProvider(t) {
  t.registerLinkProvider({
    provideLinks(rowNumber, callback) {
      const line = t.buffer.active.getLine(rowNumber - 1)?.translateToString(true);
      if (!line) return callback(undefined);
      const links = [];

      function pushMatch(re, kind) {
        for (const m of line.matchAll(re)) {
          const text = m[0];
          const start = m.index + 1;       // xterm columns are 1-indexed
          const end = start + text.length - 1;
          links.push({
            text,
            range: {
              start: { x: start, y: rowNumber },
              end:   { x: end,   y: rowNumber },
            },
            activate(event, linkText) {
              // URLs activate on a plain click — opening a wrong tab is
              // mild, and the friction of "Cmd is required even for a
              // URL" was a real pain point. File paths still demand
              // Cmd/Ctrl: the preview overlay is more invasive than a
              // browser tab, and scrollback often has incidental
              // path-shaped text.
              if (kind === "url") {
                // URL activation is handled by the custom mousedown/mouseup
                // pair installed below (installUrlClickHandler). xterm's
                // own activate path races a TUI mouse-reporting redraw
                // that clears _currentLink mid-click, so we can't rely on
                // it. Keeping URL matches in the link provider gives us
                // the hover decoration (underline + pointer cursor) for
                // free; the actual open lives in the custom handler.
                return;
              }
              if (!event.metaKey && !event.ctrlKey) return;
              if (kind === "file" && fileLinkCallback) {
                fileLinkCallback(linkText);
              }
            },
            hover() {},
            leave() {},
          });
        }
      }

      // Order matters: URLs first so http://foo.md isn't claimed as a
      // file. ABS before REL so /abs/foo.html doesn't double-match.
      pushMatch(URL_RE, "url");
      pushMatch(ABS_PATH_RE, "file");
      pushMatch(REL_PATH_RE, "file");

      callback(links);
    },
  });
}

// xterm's WebglAddon.dispose() deletes its GL objects and removes the canvas
// but never loses the CONTEXT (xtermjs/xterm.js#6068, open; PR #6069 is the
// one-line upstream fix — our vendored bundle has zero occurrences of
// loseContext). The orphaned context then survives until GC, which WebKit
// defers indefinitely: measured at ~1.8MB of GPU memory and one graphics VM
// region leaked per terminal mount, reaching 4.8GB over four days in the Tauri
// shell (`footprint -p <WebContent pid>`, "Owned physical footprint (unmapped)
// (graphics)"). It survives a page reload — the surfaces are IOKit-owned.
// This also matters for correctness, not just memory: WebKit hard-caps live
// contexts at 16 and evicts the least-recently-ACTIVE one unrecoverably, so
// enough zombies will blank a terminal that's still on screen.
// Reaches through private internals (no public accessor exists) — optional
// chaining throughout so an upgrade that moves them degrades to the old
// leaky-but-working behavior instead of throwing. Grab `gl` BEFORE dispose:
// the renderer is torn down by it.
// Caveat: WebKit #218305 reports loseContext() may still not fully release.
// Verify with footprint, don't assume.
// localStorage can throw in restricted contexts; a failed read means "keep the
// default renderer", never a broken terminal.
function noWebglRenderer() {
  try { return localStorage.getItem("periscope.noWebgl") === "1"; } catch (_) { return false; }
}

function disposeWebglAddon() {
  if (!webglAddon) return;
  const gl = webglAddon._renderer?._gl;
  try { webglAddon.dispose(); } catch (_) {}
  try { gl?.getExtension("WEBGL_lose_context")?.loseContext(); } catch (_) {}
  webglAddon = null;
}

// ONE xterm instance per page load, reused for every pane, living in a host
// div we move between mount containers instead of rebuilding.
//
// It used to be a fresh Terminal + WebglAddon per mount, which leaked a WebGL
// context on every pane switch (see disposeWebglAddon) — and WebKit caps live
// contexts at 16, evicting the least-recently-active one unrecoverably, so
// enough churn blanks a terminal that's still on screen. Reusing the instance
// means we create exactly one context, which is the only fix that doesn't
// depend on WebKit honouring loseContext() (bugs.webkit.org #218305).
//
// Re-parenting MUST move the element xterm already owns: term.open(newParent)
// silently no-ops after the first call (xtermjs/xterm.js#4978 — it ignores the
// argument and returns). Same imperative parked-host trick <Detail> uses to
// keep LGTM iframes from reloading on reconciliation.
//
// Nothing disposes this; it intentionally lives for the page's lifetime.
function ensureTerminal() {
  if (term) return;
  xtermHost = document.createElement("div");
  xtermHost.className = "xterm-host";

  term = new Terminal({
    ...XTERM_OPTIONS,
    theme: terminalTheme,
    // No ligatures: IBM Plex Mono ships none, and they aren't a runtime xterm
    // setting in the vendored bundle. .detail-xterm's font-feature-settings
    // only matter if a ligature face goes back first in fontFamily.
    //
    // OSC 8 hyperlinks (Claude Code wraps file paths in file:// links).
    // Without a handler, xterm's fallback confirm()s then window.open()s
    // the raw URI — in the Tauri shell that escapes to LaunchServices and
    // the .md opens in whatever app owns the extension (Warp, as it
    // happens) instead of a periscope preview tab. Route file:// into the
    // same Cmd+click preview path as regex-matched paths; everything else
    // through openExternal like plain URLs.
    linkHandler: {
      allowNonHttpProtocols: true,
      activate(event, uri) {
        if (uri.startsWith("file://")) {
          if (!event.metaKey && !event.ctrlKey) return;
          if (!fileLinkCallback) return;
          let path = uri.slice("file://".length);
          if (path.startsWith("localhost/")) path = path.slice("localhost".length);
          try { path = decodeURIComponent(path); } catch (_) {}
          fileLinkCallback(path);
          return;
        }
        openExternal(uri);
      },
    },
  });
  term.open(xtermHost);
  // IBM Plex Mono is a webfont: xterm measures its cell on open, often against
  // the Menlo fallback, and never re-measures on its own. Bouncing the family
  // forces a fresh measure once the real face lands (the options setter drops
  // a same-value write, so one assignment is a no-op); the refit goes through
  // the width pin, so a sub-column difference never reaches tmux. Both faces:
  // the bounce rebuilds the WebGL glyph atlas, and bold glyphs rasterised
  // before the 700 file lands would otherwise stay in the fallback for good.
  if (document.fonts?.load) {
    const face = `${XTERM_OPTIONS.fontSize}px "IBM Plex Mono"`;
    Promise.all([document.fonts.load(face), document.fonts.load(`bold ${face}`)]).then(() => {
      if (!term) return;
      term.options.fontFamily = "monospace";
      term.options.fontFamily = XTERM_OPTIONS.fontFamily;
      scheduleFit();
    }, () => {});
  }

  // Mouse wheel. tmux consumes the app's mouse-mode DECSET (it's a tmux pane
  // flag, `mouse_any_flag`), so the xterm mirror never sees `\e[?1003h` and
  // can't forward wheel as mouse events — it converts them to arrow keys,
  // which Claude reads as prompt-history navigation (scrolling walks back
  // through sent prompts). When the server reports the pane has mouse
  // reporting on, synthesize SGR wheel reports at the hovered cell and send
  // them through the input channel so the app scrolls its own transcript —
  // the scrollback the user actually wants. Otherwise (plain shell, no app
  // mouse) let xterm scroll its local scrollback.
  term.attachCustomWheelEventHandler((ev) => {
    if (!mouseReportingOn || !ev.deltaY) return true;
    const cell = wheelCell(ev, term);
    if (!cell) return true;
    ev.preventDefault();                   // always own the event once we're here
    // Normalize the delta to pixels (line-/page-mode wheels → px), then to
    // lines via cell height, accumulating remainder so speed matches the old
    // per-line cadence instead of firing once per DOM event.
    let px = ev.deltaY;
    if (ev.deltaMode === 1) px *= cell.cellH;       // DOM_DELTA_LINE
    else if (ev.deltaMode === 2) px *= cell.pageH;  // DOM_DELTA_PAGE
    if ((px < 0) !== (wheelAccumPx < 0)) wheelAccumPx = 0;  // reversal → drop stale remainder
    wheelAccumPx += px;
    const step = cell.cellH * WHEEL_LINES_PER_TICK;
    let ticks = Math.trunc(wheelAccumPx / step);
    if (!ticks) return false;
    wheelAccumPx -= ticks * step;
    ticks = Math.max(-8, Math.min(8, ticks));        // cap a fling to a sane burst
    const btn = ticks < 0 ? 64 : 65;                 // SGR wheel-up / wheel-down
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(`\x1b[<${btn};${cell.col};${cell.row}M`.repeat(Math.abs(ticks)));
    }
    return false;                          // suppress xterm's arrow/scroll fallback
  });

  // Try the WebGL renderer. On init failure (GPU-disabled environments, or
  // Safari <16 — the addon's constructor throws there) xterm keeps its DEFAULT
  // DOM renderer: there is no automatic canvas fallback, and we don't vendor
  // @xterm/addon-canvas. Same on dispose — WebglAddon's teardown explicitly
  // reinstalls the DOM renderer. Failure is silent on the success path, so we
  // log it once for diagnosis.
  // Escape hatch for bisecting GPU-memory growth without a rebuild:
  //   localStorage.setItem("periscope.noWebgl", "1")  // then reload
  // GPU-backed memory ("Owned physical footprint (unmapped) (graphics)" in
  // `footprint -p <WebContent pid>`) grew ~8MB per pane switch even after the
  // instance became single-use, so the renderer needs to be removable from the
  // picture to tell atlas/texture churn apart from everything else.
  if (!noWebglRenderer()) {
    try {
      webglAddon = new WebglAddon.WebglAddon();
      webglAddon.onContextLoss(() => { disposeWebglAddon(); });
      term.loadAddon(webglAddon);
    } catch (e) {
      console.warn("[periscope] WebGL terminal renderer unavailable; using the DOM renderer:", e);
    }
  } else {
    console.warn("[periscope] WebGL renderer disabled via localStorage periscope.noWebgl");
  }

  // Search addon: powers the Cmd+F bar (UI in a later task). Exported as
  // searchNext/searchPrev/clearSearch below.
  searchAddon = new SearchAddon.SearchAddon();
  term.loadAddon(searchAddon);

  // Unicode 11 widths. xterm's BUILT-IN provider is Unicode 6, whose
  // wcwidth falls through to 1 for everything above the BMP except the CJK
  // extension planes — so every emoji is one cell wide. tmux uses a modern
  // wcwidth and calls them two. Each emoji on a line therefore drifted the
  // mirrored cursor one column left of where tmux (and the shell) think it
  // is: with a 🐍 in the prompt, the block cursor rendered one cell past
  // where typed text actually landed, permanently. Must match tmux, or the
  // grid coordinates we're handed refer to a different screen than the one
  // we drew.
  try {
    term.loadAddon(new Unicode11Addon.Unicode11Addon());
    term.unicode.activeVersion = "11";
  } catch (e) {
    console.warn("[periscope] Unicode 11 widths unavailable; emoji will offset the cursor:", e);
  }

  // Cmd+click on a `.md` path → add it as a document to the LGTM
  // session for this pane's repo. Path is resolved against the pane's
  // cwd server-side. Plain click on the underlined path does nothing
  // (no modifier = no action) — the Cmd requirement keeps incidental
  // clicks during scrollback reading from triggering adds.
  //
  // The link provider runs per-rendered-row on demand. Underline-on-
  // hover comes for free from xterm's default link styling.
  registerRoutingLinkProvider(term);
  installUrlClickHandler(term);

  // Fit xterm to the modal container's actual pixel size (so we never clip
  // the bottom rows) and ask tmux to resize the underlying pane to match.
  // Without this, xterm renders at tmux's pane size (often taller than the
  // modal) and the bottom is clipped by overflow:hidden.
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  // The browser intercepts Cmd+key combos before xterm sees them. Translate
  // the common ones into readline-style control sequences and forward them
  // to the pane ourselves. Returning false from the handler tells xterm to
  // skip its own processing (which would otherwise be nothing for these).
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;

    // Esc handling:
    //   Shift+Esc — passthrough: send a real \x1b to the pane (vim / Claude
    //     get an Escape) and stop the event so the modal stays open.
    //   plain Esc — let it bubble to the useEscape stack so the modal
    //     closes; return false so xterm doesn't ALSO emit \x1b to the pane
    //     in the background.
    //   Ctrl+[ also works as a passthrough Esc via xterm's standard ANSI
    //     mapping — no special handling needed here.
    if (e.key === "Escape" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      if (termWs && termWs.readyState === WebSocket.OPEN) termWs.send("\x1b");
      return false;
    }
    if (e.key === "Escape" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      return false;
    }

    // Shift+Enter — insert a newline in Claude's input instead of submitting.
    // xterm.js collapses Shift+Enter to a bare \r (identical to Enter), so
    // Claude can't distinguish them. Send meta-return (ESC + CR) — the exact
    // bytes Option+Enter already produces, which Claude treats as a newline.
    if (e.key === "Enter" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      if (termWs && termWs.readyState === WebSocket.OPEN) termWs.send("\x1b\r");
      return false;
    }

    if (!e.metaKey) return true;
    const sendCtrl = (seq) => {
      e.preventDefault();
      if (termWs && termWs.readyState === WebSocket.OPEN) termWs.send(seq);
    };
    switch (e.key) {
      // Cmd+Backspace = clear input box. Ctrl+U (kill-line-backward) doesn't
      // reliably trigger in Ink-based TUIs like Claude Code, so we just flood
      // backspaces — every input library handles \x7f. 200 is plenty for any
      // realistic input length; extras hit empty input as no-ops.
      case "Backspace": sendCtrl("\x7f".repeat(200)); return false;
      case "Delete":    sendCtrl("\x0b"); return false;  // Cmd+Delete   = kill line forward (Ctrl+K)
      case "ArrowLeft": sendCtrl("\x01"); return false;  // Cmd+Left     = beginning of line (Ctrl+A)
      case "ArrowRight":sendCtrl("\x05"); return false;  // Cmd+Right    = end of line       (Ctrl+E)
      // Cmd+R is periscope's refresh chord (keys.js), never the pane's. Return
      // false so xterm neither consumes it nor emits bytes, and it reaches the
      // window listener — same passthrough contract Escape already uses.
      case "r":
      case "R": return false;
      default: return true;  // Cmd+C/V/etc fall through to xterm's clipboard handling
    }
  });

  // Keystroke forwarding lives on the terminal, not the socket — it survives
  // across reconnects and always sends to whichever socket is current.
  term.onData((data) => {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(data);
    }
  });

  // BEL (\x07) — flash the container border. Triggered by `printf '\a'` and
  // many CLI tools' notification hooks.
  term.onBell(() => {
    if (containerEl) {
      containerEl.classList.add("bell-pulse");
      setTimeout(() => containerEl.classList.remove("bell-pulse"), 400);
    }
  });
}

export function startLiveTerminal(target) {
  track("terminal.open", { target });
  const reused = term !== null;
  ensureTerminal();
  // Move the parked host into this mount's container. Preact hands us a fresh
  // empty div per mount, so this re-parents rather than duplicates.
  if (containerEl && xtermHost.parentElement !== containerEl) {
    containerEl.appendChild(xtermHost);
  }
  if (reused) {
    // Drop the previous pane's grid, scrollback and input modes. The server's
    // capture-pane blob repaints this pane on connect, exactly as it did for a
    // freshly-created instance — pane switches never preserved scrollback.
    try { term.reset(); } catch (_) {}
    mouseReportingOn = false;
    wheelAccumPx = 0;
  }
  term.focus();

  // Synchronous initial measure. Reading the container's layout (via
  // proposeDimensions()) forces a sync layout flush, so we get real dims here
  // — and we pass them to the WS as a connect-time hint so the server resizes
  // tmux BEFORE capture-pane. Without that, the initial blob is at tmux's
  // current pane width (often a real terminal also attached at 200+ cols) and
  // xterm has to reflow it down, mangling box-drawing TUIs for the first
  // frame. The pin (pinWidth) stops navigation from reflowing tmux at
  // slightly-different widths. A hidden host (the pane left in transcript
  // mode, another tab showing) can't be measured, so the hint is the
  // container's LAST measurement: without one, tmux kept the window's old
  // width and the capture came in wider than the pin. Nothing measured yet
  // this page: no hint, and tmux's view stands. No pane dims yet
  // (stopLiveTerminal cleared them): the xterm takes the container's until
  // the handshake's size message.
  measureContainer();
  syncXtermSize();

  // ResizeObserver: refit + tell tmux when the modal/window changes size.
  // Debounced so a window-drag doesn't spam tmux with subprocess calls.
  // Re-created per mount because it watches the mount's container, not the
  // parked host.
  if (termResizeObserver) {
    try { termResizeObserver.disconnect(); } catch (_) {}
  }
  termResizeObserver = new ResizeObserver(scheduleFit);
  if (containerEl) termResizeObserver.observe(containerEl);

  termWsTarget = target;
  termIntentionalClose = false;
  termReconnectAttempt = 0;
  termReconnectedNotified = false;
  connectTerminalWs(target, containerDims.cols, containerDims.rows);
}

function connectTerminalWs(target, hintCols = 0, hintRows = 0) {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  let url = `${wsProto}://${location.host}/ws/pane?${paneIdQuery(target)}`;
  if (hintCols > 0 && hintRows > 0) {
    url += `&cols=${hintCols}&rows=${hintRows}`;
    // Seed the "last sent" memo with the hint, the handshake's ask (the server
    // may floor a shared window's height below it: routes/ws.py).
    // Without this, the first ResizeObserver fire would send a redundant
    // resize message. From the live memo, not NOTHING_SENT: a reconnect made
    // while shrunk hints the shrunk height, and forgetting the shrink left
    // the window short once the container grew back. A mount starts from
    // NOTHING_SENT anyway (stopLiveTerminal), so a pane switched to while
    // short never regrows — that would need memory per window.
    lastSent = afterSend(lastSent, { cols: hintCols, rows: hintRows });
  }
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  termWs = ws;

  ws.onopen = () => {
    if (termReconnectAttempt > 0 && term) {
      // Server's initial paint will clear the screen and repaint tmux's
      // current state — the reconnect notice goes to scrollback above that.
      term.writeln("\r\n\x1b[32m[periscope: reconnected]\x1b[0m");
    }
    termReconnectAttempt = 0;
    termReconnectedNotified = false;
  };

  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      // Control message (JSON) — currently only `{type:"size", cols, rows}`.
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "size") {
          paneCols = msg.cols;
          paneRows = msg.rows;
          // Through the fit's rule (xtermDims): the pane can outgrow this
          // container without our asking (another viewer's resize).
          syncXtermSize();
          // And through the fit's ask (resizeAsk), which asks a pane announced
          // taller than the container back down. Nothing else would: the
          // container didn't change, so no fit runs. Size messages come only
          // when the pane's dims change, so the extra measure costs nothing.
          scheduleFit();
        } else if (msg.type === "mouse") {
          mouseReportingOn = !!msg.on;
        }
      } catch (_) {
        // Not JSON; treat as data
        term.write(normalizeAgentTerminalData(event.data, termAgent));
      }
    } else {
      // Binary terminal bytes
      term.write(normalizeAgentTerminalData(new Uint8Array(event.data), termAgent));
    }
  };

  // onerror always precedes onclose; let onclose drive the reconnect so we
  // don't double-schedule.
  ws.onerror = () => {};

  ws.onclose = (_e) => {
    // Ignore stale closes from a socket we've already replaced or torn down.
    if (ws !== termWs) return;
    if (termIntentionalClose) return;
    // Modal closed or switched panes since this socket opened — drop quietly.
    if (!term || termWsTarget !== target) return;

    if (!termReconnectedNotified) {
      term.writeln("\r\n\x1b[33m[periscope: reconnecting…]\x1b[0m");
      termReconnectedNotified = true;
    }
    // Backoff: 250, 500, 1000, 2000, then steady 4000ms.
    const delays = [250, 500, 1000, 2000];
    const delay = delays[Math.min(termReconnectAttempt, delays.length - 1)] || 4000;
    termReconnectAttempt += 1;
    if (termReconnectTimer) clearTimeout(termReconnectTimer);
    termReconnectTimer = setTimeout(() => {
      termReconnectTimer = null;
      if (!term || termWsTarget !== target || termIntentionalClose) return;
      // Re-send current dims so the reconnected server resizes tmux before
      // its initial paint — same reasoning as the first connect. The
      // container's, not term's: those are the pane's (containerDims).
      connectTerminalWs(target, containerDims.cols, containerDims.rows);
    }, delay);
  };
}

function scheduleFit() {
  if (fitDebounce) clearTimeout(fitDebounce);
  fitDebounce = setTimeout(() => {
    fitDebounce = null;
    if (!term || !fitAddon || fitHeld) return;
    // Hold the pinned width: a fit that wobbled by a column or two (the bug —
    // mount/layout transients) snaps back so tmux never reflows; a genuinely
    // different width (real resize beyond tolerance) becomes the new pin.
    // The xterm takes the pane's dims, rows capped at the container (xtermDims).
    if (!measureContainer()) return;
    syncXtermSize();
    // What to ask tmux for — a width change, rarely a height — and why:
    // resizeAsk. From the container's dims (containerDims): term.cols is the
    // pane's, which on a left/right split never equals what we last sent.
    const ask = resizeAsk(containerDims, lastSent, paneRows);
    if (!ask) return;
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      lastSent = afterSend(lastSent, ask);
      // The server's reconcile answers with the pane's real size. No cols: a
      // height-only ask, the window's width kept (resizeAsk).
      termWs.send(JSON.stringify({ type: "resize", ...ask }));
    }
  }, 80);
}

// Detach from the current pane WITHOUT destroying the xterm instance: the
// socket, the resize observer and the pending timers are per-mount, but the
// terminal, its addons and the parked host outlive every mount (see
// ensureTerminal — recreating them is what leaked a WebGL context per switch).
// The url-click handlers are bound to term.element, which is inside the parked
// host, so they persist with the instance rather than being reinstalled.
export function stopLiveTerminal() {
  // Suppress the reconnect path before closing — otherwise onclose would
  // schedule a retry against a target whose modal we've just torn down.
  termIntentionalClose = true;
  termWsTarget = null;
  if (termReconnectTimer) {
    clearTimeout(termReconnectTimer);
    termReconnectTimer = null;
  }
  lastSent = NOTHING_SENT;
  paneCols = 0;
  paneRows = 0;
  if (fitDebounce) {
    clearTimeout(fitDebounce);
    fitDebounce = null;
  }
  if (termResizeObserver) {
    try { termResizeObserver.disconnect(); } catch (_) {}
    termResizeObserver = null;
  }
  if (termWs) {
    try { termWs.close(); } catch (_) {}
    termWs = null;
  }
  // Park the host outside the DOM so Preact tearing down the mount container
  // can't take it with it. It goes back in on the next startLiveTerminal().
  if (xtermHost?.parentElement) xtermHost.remove();
}

// Modal/Detail use this to surface image-paste errors inline in the terminal.
export function writeTerminalLine(line) {
  if (term) term.writeln(line);
}

// Jump the viewport to the live bottom. Used on mount, on switching back to
// terminal mode, and by the detail pane's scroll-to-bottom button — xterm's
// own scrollbar is fiddly to drag to the very bottom.
export function scrollTerminalToBottom() {
  if (term) {
    try { term.scrollToBottom(); } catch (_) {}
  }
}

// Re-run FitAddon. Used by mountTerminal after a deferred frame to catch the
// case where the container's layout wasn't fully computed when
// startLiveTerminal measured it synchronously (common when re-mounting into a
// container that just became visible via class toggle).
export function refitTerminal() {
  if (!term || !fitAddon) return;
  scheduleFit();
}

// Held while a splitter is dragged (split/ResizeHandle.jsx). The container
// changes width on every pointermove, and each fit that landed would resize
// the tmux pane mid-drag — a burst of SIGWINCHes, each one a Claude redraw:
// the scrollback-mangling redraw resizeAsk's comment describes. The xterm
// keeps its size (clipped, or short of the edge) until release, then fits once.
export function holdTerminalFit() {
  fitHeld = true;
}

export function releaseTerminalFit() {
  fitHeld = false;
  scheduleFit();
}

// Search API used by the Cmd+F bar in <Terminal>.
export function searchNext(query, opts = {}) {
  return searchAddon ? searchAddon.findNext(query, opts) : false;
}
export function searchPrev(query, opts = {}) {
  return searchAddon ? searchAddon.findPrevious(query, opts) : false;
}
export function clearSearch() {
  if (searchAddon) searchAddon.clearDecorations();
}

// --- mount helper (ported from static/terminal-mount.js) ---
//
// One xterm instance lives in the app at a time. mountTerminal() retargets it
// onto a new container; unmountTerminal() tears it down. The <Terminal>
// wrapper drives these from its mount/unmount effect.

let activePasteHandler = null;
let activeContainer = null;

/**
 * Mount the live terminal for `target` into `container`.
 * @param {HTMLElement} container
 * @param {string} target — tmux target spec (e.g. "session:0.0")
 * @param {Object} opts
 * @param {(event: ClipboardEvent) => void} [opts.onPaste] — capture-phase paste hook
 */
export function mountTerminal(container, target, opts = {}) {
  unmountTerminal();  // tear down any previous mount
  setTerminalContainer(container);
  termAgent = opts.agent || null;
  if (opts.onPaste) {
    activePasteHandler = opts.onPaste;
    container.addEventListener("paste", activePasteHandler, true);
  }
  activeContainer = container;
  startLiveTerminal(target);
  // Defensive refit on the next animation frame: when a view-switch just
  // un-hid the container, layout queries during startLiveTerminal can
  // race the browser's layout pass and produce stale cols/rows. Asking
  // the browser to refit on the next frame catches that case. Two rAFs
  // because the first one fires before the post-paint relayout settles
  // when transitioning from `display: none` → visible.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    refitTerminal();
    scrollTerminalToBottom();
  }));
  // The initial capture-pane paint streams in over the socket after mount;
  // pin to the bottom once it's likely landed so a shell pane opens at its
  // prompt rather than scrolled up in scrollback.
  setTimeout(scrollTerminalToBottom, 300);
}

export function unmountTerminal() {
  stopLiveTerminal();
  if (activeContainer && activePasteHandler) {
    activeContainer.removeEventListener("paste", activePasteHandler, true);
  }
  activePasteHandler = null;
  activeContainer = null;
  setTerminalContainer(null);
}
