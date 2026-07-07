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
import { terminalTheme } from "./theme.js";

let term = null;
let termWs = null;
let termWsTarget = null;            // target the current/pending socket is for
let termIntentionalClose = false;   // suppress reconnect when we close on purpose
let termReconnectTimer = null;
let termReconnectAttempt = 0;
let termReconnectedNotified = false; // only print "reconnecting…" once per outage
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
let lastSentCols = 0;            // dims of the most recent resize message sent to the server
let _lastSentRows = 0;            //   — used to suppress redundant resizes during initial mount
// Width is pinned for the session. Navigating panes re-runs startLiveTerminal
// with a FRESH xterm + a fresh fit(); a mount-time measurement that differs by
// a column or two (scrollbar appearing, layout rounding) would otherwise resize
// tmux and reflow scrollback — surfacing as the same block duplicated at two
// wrap widths, even though the user never resized. We snap such wobble back to
// the pinned width and only accept a genuinely different width (real window /
// rail resize, or a different container like the modal) as a new pin.
let pinnedCols = 0;
const WIDTH_PIN_TOLERANCE = 4;   // cols; absorbs scrollbar/rounding wobble, not a real resize
let containerEl = null;          // set by setTerminalContainer() before startLiveTerminal()

// Whether the viewport is at the live bottom. The detail pane polls this to
// show its scroll-to-bottom button only when scrolled up. True when there's no
// terminal (nothing to scroll) or in alt-screen (no scrollback).
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
let urlClickListenerEl = null;
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
  el.addEventListener("mousedown", urlMouseDownHandler, true);
  el.addEventListener("mouseup", urlMouseUpHandler, true);
  urlClickListenerEl = el;
}

function uninstallUrlClickHandler() {
  if (urlClickListenerEl) {
    if (urlMouseDownHandler) urlClickListenerEl.removeEventListener("mousedown", urlMouseDownHandler, true);
    if (urlMouseUpHandler) urlClickListenerEl.removeEventListener("mouseup", urlMouseUpHandler, true);
  }
  urlClickListenerEl = null;
  urlMouseDownHandler = null;
  urlMouseUpHandler = null;
  pendingUrlClick = null;
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

export function startLiveTerminal(target) {
  track("terminal.open", { target });
  // Fresh xterm.js instance per mount. Dispose any leftover from a prior
  // session before creating a new one.
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
  containerEl.innerHTML = "";

  term = new Terminal({
    fontFamily: '"SF Mono", "JetBrains Mono", "Menlo", monospace',
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
    theme: terminalTheme,
    cursorStyle: "block",
    fontWeight: "400",
    // CSS ligatures aren't a runtime xterm setting in the vendored bundle,
    // but the parent container's CSS sets font-feature-settings so JetBrains
    // Mono / SF Mono show their ligatures when present.
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
  term.open(containerEl);
  term.focus();

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

  // Try WebGL renderer; fall back to canvas on init failure (older Chromes,
  // headless contexts, GPU-disabled environments). The addon writes to its
  // own canvas inside xterm's element tree, so failure is silent on success
  // paths but we log it once for diagnosis.
  try {
    webglAddon = new WebglAddon.WebglAddon();
    webglAddon.onContextLoss(() => {
      try { webglAddon?.dispose(); } catch (_) {}
      webglAddon = null;
    });
    term.loadAddon(webglAddon);
  } catch (e) {
    console.warn("[periscope] WebGL terminal renderer unavailable; falling back to canvas:", e);
  }

  // Search addon: powers the Cmd+F bar (UI in a later task). Exported as
  // searchNext/searchPrev/clearSearch below.
  searchAddon = new SearchAddon.SearchAddon();
  term.loadAddon(searchAddon);

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

  // Synchronous initial fit. Reading the container's layout (via fit())
  // right after term.open() forces a sync layout flush, so we get real
  // dims here — and we pass them to the WS as a connect-time hint so the
  // server resizes tmux BEFORE capture-pane. Without that, the initial
  // blob is at tmux's current pane width (often a real terminal also
  // attached at 200+ cols) and xterm has to reflow it down, mangling
  // box-drawing TUIs for the first frame. If fit can't measure for any
  // reason, leave the hint at zero and the server uses tmux's view.
  let initialCols = 0;
  let initialRows = 0;
  try {
    fitAddon.fit();
    // Reuse the session's pinned width unless this is a genuinely different
    // size (first mount, or a real resize beyond the wobble tolerance). This
    // is what stops navigation from reflowing tmux at slightly-different
    // widths. Height always follows the fit — row changes don't reflow.
    if (pinnedCols <= 0 || Math.abs(term.cols - pinnedCols) > WIDTH_PIN_TOLERANCE) {
      pinnedCols = term.cols;
    } else if (term.cols !== pinnedCols) {
      term.resize(pinnedCols, term.rows);
    }
    initialCols = pinnedCols;
    initialRows = term.rows;
  } catch (_) {}

  // ResizeObserver: refit + tell tmux when the modal/window changes size.
  // Debounced so a window-drag doesn't spam tmux with subprocess calls.
  termResizeObserver = new ResizeObserver(scheduleFit);
  termResizeObserver.observe(containerEl);

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

  termWsTarget = target;
  termIntentionalClose = false;
  termReconnectAttempt = 0;
  termReconnectedNotified = false;
  // Seed the "last sent" memo with the dims we're about to give the server
  // via the WS query params. Without this, the first ResizeObserver fire
  // would send a redundant resize message.
  lastSentCols = initialCols;
  _lastSentRows = initialRows;
  connectTerminalWs(target, initialCols, initialRows);
}

function connectTerminalWs(target, hintCols = 0, hintRows = 0) {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  let url = `${wsProto}://${location.host}/ws/pane?${paneIdQuery(target)}`;
  if (hintCols > 0 && hintRows > 0) {
    url += `&cols=${hintCols}&rows=${hintRows}`;
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
          term.resize(msg.cols, msg.rows);
        } else if (msg.type === "mouse") {
          mouseReportingOn = !!msg.on;
        }
      } catch (_) {
        // Not JSON; treat as data
        term.write(event.data);
      }
    } else {
      // Binary terminal bytes
      term.write(new Uint8Array(event.data));
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
      // Re-send current xterm dims so the reconnected server resizes tmux
      // before its initial paint — same reasoning as the first connect.
      connectTerminalWs(target, term.cols, term.rows);
    }, delay);
  };
}

function scheduleFit() {
  if (fitDebounce) clearTimeout(fitDebounce);
  fitDebounce = setTimeout(() => {
    fitDebounce = null;
    if (!term || !fitAddon) return;
    try {
      fitAddon.fit();
    } catch (_) {
      return;
    }
    // Hold the pinned width: a fit that wobbled by a column or two (the bug —
    // mount/layout transients) snaps back so tmux never reflows; a genuinely
    // different width (real resize beyond tolerance) becomes the new pin.
    if (pinnedCols > 0 && term.cols !== pinnedCols) {
      if (Math.abs(term.cols - pinnedCols) > WIDTH_PIN_TOLERANCE) {
        pinnedCols = term.cols;
      } else {
        term.resize(pinnedCols, term.rows);
      }
    }
    // Only send a resize message when the dims actually changed since the
    // last one we sent, AND only when width changed — height-only changes
    // are dropped. Two reasons:
    //   1. The ResizeObserver's initial observation fires shortly after
    //      mount and produces a no-op resize (cols/rows unchanged from the
    //      WS-connect hint), which tmux silently honors but the cycle of
    //      "fit → send → tmux resizes → output reflows" can produce visible
    //      width churn in scrollback.
    //   2. Height-only resizes (modal drag, sidebar splitter, browser window
    //      vertical resize) don't reflow content — but tmux resize-window
    //      still raises SIGWINCH and Claude's TUI re-renders its frame on
    //      every SIGWINCH. That re-render mangles scrollback (characters
    //      from earlier frames overlapping the latest content — the same
    //      failure class as the width-reflow corruption noted in
    //      `routes/ws.py::set_pane_size`). Suppressing row-only sends costs
    //      "modal may show empty rows at the bottom until the next real
    //      width change re-syncs height," which is strictly better than
    //      scrambled chat history.
    if (term.cols === lastSentCols) return;
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      lastSentCols = term.cols;
      _lastSentRows = term.rows;
      termWs.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }, 80);
}

export function stopLiveTerminal() {
  // Suppress the reconnect path before closing — otherwise onclose would
  // schedule a retry against a target whose modal we've just torn down.
  termIntentionalClose = true;
  uninstallUrlClickHandler();
  termWsTarget = null;
  if (termReconnectTimer) {
    clearTimeout(termReconnectTimer);
    termReconnectTimer = null;
  }
  lastSentCols = 0;
  _lastSentRows = 0;
  if (fitDebounce) {
    clearTimeout(fitDebounce);
    fitDebounce = null;
  }
  if (termResizeObserver) {
    try { termResizeObserver.disconnect(); } catch (_) {}
    termResizeObserver = null;
  }
  if (webglAddon) {
    try { webglAddon.dispose(); } catch (_) {}
    webglAddon = null;
  }
  if (searchAddon) {
    try { searchAddon.dispose(); } catch (_) {}
    searchAddon = null;
  }
  fitAddon = null;
  if (termWs) {
    try { termWs.close(); } catch (_) {}
    termWs = null;
  }
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
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
// startLiveTerminal called fit() synchronously (common when re-mounting into a
// container that just became visible via class toggle).
export function refitTerminal() {
  if (!term || !fitAddon) return;
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
