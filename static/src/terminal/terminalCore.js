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

import { targetQuery } from "../util.js";
import { terminalTheme } from "./theme.js";

let term = null;
let termWs = null;
let termWsTarget = null;            // target the current/pending socket is for
let termIntentionalClose = false;   // suppress reconnect when we close on purpose
let termReconnectTimer = null;
let termReconnectAttempt = 0;
let termReconnectedNotified = false; // only print "reconnecting…" once per outage
let fitAddon = null;
let webglAddon = null;
let searchAddon = null;
let termResizeObserver = null;
let fitDebounce = null;
let lastSentCols = 0;            // dims of the most recent resize message sent to the server
let lastSentRows = 0;            //   — used to suppress redundant resizes during initial mount
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
let linkClickCallback = null;    // set by setTerminalLinkCallback()

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

// Register a callback invoked when an .md link in the terminal is clicked.
// Callback signature: (rawPath: string) => void
// rawPath includes any trailing ":42" line suffix; callees parse it themselves
// (see addLgtmDocFromTerminal in the modal for the original handler).
export function setTerminalLinkCallback(cb) {
  linkClickCallback = cb;
}

let fileLinkCallback = null;
let urlLinkCallback = null;

// Register a callback for absolute/relative file path clicks (no .md
// special case). Callback signature: (rawPath: string) => void.
// Parents (Detail.jsx) register this to open the preview overlay.
export function setTerminalFileCallback(cb) {
  fileLinkCallback = cb;
}

// Register a callback for URL clicks. Callback signature: (url: string)
// => void. Default unwired — terminalCore handles URLs via window.open
// directly when no callback is registered (see registerRoutingLinkProvider).
export function setTerminalUrlCallback(cb) {
  urlLinkCallback = cb;
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

// .md special-case for LGTM routing (legacy behavior preserved). Same
// shape as the prior MD_PATH_RE but renamed for clarity.
const MD_PATH_RE = /(?<![\w./-])[\w./~-]*[\w-]+\.md(?::\d+)?(?!\w)/g;

// One routing link provider. xterm supports N providers but a single
// routing provider keeps the regex passes minimal (one per row) and routing
// decisions in one place. Precedence:
//   1. URL — always handled by urlLinkCallback if registered, else
//      window.open with noopener.
//   2. .md AND a markdown handler is registered (LGTM session for this
//      cwd) → markdown handler.
//   3. File path — handled by fileLinkCallback if registered (Phase 3 wires
//      this to the preview overlay). Until then it's a no-op.
function registerRoutingLinkProvider(t) {
  t.registerLinkProvider({
    provideLinks(rowNumber, callback) {
      const line = t.buffer.active.getLine(rowNumber - 1)?.translateToString(true);
      if (!line) return callback(undefined);
      const links = [];

      function pushMatch(re, kind) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(line)) !== null) {
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
              // Cmd/Ctrl is required for ALL routes — reading scrollback
              // can't accidentally trigger a file open or URL.
              if (!event.metaKey && !event.ctrlKey) return;
              if (kind === "url") {
                if (urlLinkCallback) urlLinkCallback(linkText);
                else window.open(linkText, "_blank", "noopener");
                return;
              }
              if (kind === "md" && linkClickCallback) {
                linkClickCallback(linkText);
                return;
              }
              if (kind === "file" && fileLinkCallback) {
                fileLinkCallback(linkText);
              }
            },
            hover() {},
            leave() {},
          });
        }
      }

      // Order matters: URLs first so http://foo.md isn't claimed as .md.
      pushMatch(URL_RE, "url");
      pushMatch(MD_PATH_RE, "md");
      pushMatch(ABS_PATH_RE, "file");
      pushMatch(REL_PATH_RE, "file");

      callback(links);
    },
  });
}

export function startLiveTerminal(target) {
  // Fresh xterm.js instance per mount. Dispose any leftover from a prior
  // session before creating a new one.
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
  containerEl.innerHTML = "";

  term = new Terminal({
    fontFamily: '"SF Mono", "JetBrains Mono", "Menlo", monospace',
    fontSize: 13,
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
  });
  term.open(containerEl);
  term.focus();

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
  lastSentRows = initialRows;
  connectTerminalWs(target, initialCols, initialRows);
}

function connectTerminalWs(target, hintCols = 0, hintRows = 0) {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  let url = `${wsProto}://${location.host}/ws/pane?${targetQuery(target)}`;
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

  ws.onclose = (e) => {
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
    // last one we sent. Without this guard, the ResizeObserver's initial
    // observation fires shortly after mount and produces a no-op resize
    // (cols/rows unchanged from the WS-connect hint), which tmux silently
    // honors but the cycle of "fit → send → tmux resizes → output reflows"
    // can produce visible width churn in scrollback. Suppressing redundant
    // sends keeps tmux at the size set at connect-time unless the user
    // actually resized the browser / sidebar / etc.
    if (term.cols === lastSentCols && term.rows === lastSentRows) return;
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      lastSentCols = term.cols;
      lastSentRows = term.rows;
      termWs.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }, 80);
}

export function stopLiveTerminal() {
  // Suppress the reconnect path before closing — otherwise onclose would
  // schedule a retry against a target whose modal we've just torn down.
  termIntentionalClose = true;
  termWsTarget = null;
  if (termReconnectTimer) {
    clearTimeout(termReconnectTimer);
    termReconnectTimer = null;
  }
  lastSentCols = 0;
  lastSentRows = 0;
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
 * @param {(rawPath: string) => void} [opts.onMdLink]
 * @param {(event: ClipboardEvent) => void} [opts.onPaste] — capture-phase paste hook
 */
export function mountTerminal(container, target, opts = {}) {
  unmountTerminal();  // tear down any previous mount
  setTerminalContainer(container);
  setTerminalLinkCallback(opts.onMdLink || null);
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
  setTerminalLinkCallback(null);
}
