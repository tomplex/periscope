// xterm.js + /ws/pane WebSocket lifecycle. A fresh Terminal is created per
// modal-open and disposed on close, so each pane gets clean state.
//
// The WS auto-reconnects on unclean close (e.g. while Claude is editing
// server.py and uvicorn reloads). The xterm instance is preserved across
// reconnects, and the server's initial-paint code re-syncs xterm to tmux's
// current state, so a reload is invisible to the user.
//
// All terminal state stays private to this module — the rest of the app talks
// to it through the exported functions.

import { targetQuery } from './util.js';

let term = null;
let termWs = null;
let termWsTarget = null;            // target the current/pending socket is for
let termIntentionalClose = false;   // suppress reconnect when we close on purpose
let termReconnectTimer = null;
let termReconnectAttempt = 0;
let termReconnectedNotified = false; // only print "reconnecting…" once per outage
let fitAddon = null;
let termResizeObserver = null;
let fitDebounce = null;
let containerEl = null;          // set by setTerminalContainer() before startLiveTerminal()
let linkClickCallback = null;    // set by setTerminalLinkCallback()

// Mount target for the live xterm. Must be called before startLiveTerminal().
// Consumers: modal.js (passes #modal-xterm) and detail.js (passes #detail-xterm).
export function setTerminalContainer(el) {
  containerEl = el;
}

// Register a callback invoked when an .md link in the terminal is clicked.
// Replaces the previous hard import of addLgtmDocFromTerminal from modal.js.
// Callback signature: (rawPath: string) => void
// rawPath includes any trailing ":42" line suffix; callees parse it themselves
// (see addLgtmDocFromTerminal in modal.js for the original handler).
export function setTerminalLinkCallback(cb) {
  linkClickCallback = cb;
}

// Matches a .md path inside a terminal row. Anchored by negative
// look-around so `data.md_archive` doesn't fool it into matching
// `data.md`, and a leading word/`/`/`.`/`-` doesn't claim more than
// the path's first char. Trailing `:42` line numbers are captured
// so Cmd+click in compiler-style output works.
const MD_PATH_RE = /(?<![\w./-])[\w./~-]*[\w-]+\.md(?::\d+)?(?!\w)/g;

function registerMarkdownLinkProvider(t) {
  t.registerLinkProvider({
    provideLinks(rowNumber, callback) {
      const line = t.buffer.active.getLine(rowNumber - 1)?.translateToString(true);
      if (!line) return callback(undefined);
      const links = [];
      let m;
      MD_PATH_RE.lastIndex = 0;
      while ((m = MD_PATH_RE.exec(line)) !== null) {
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
            // Require a modifier so reading scrollback doesn't accidentally
            // trigger adds. Cmd on Mac, Ctrl elsewhere.
            if (!event.metaKey && !event.ctrlKey) return;
            if (linkClickCallback) linkClickCallback(linkText);
          },
          hover() {},
          leave() {},
        });
      }
      callback(links);
    },
  });
}

export function startLiveTerminal(target) {
  // Fresh xterm.js instance per modal-open. Dispose any leftover from a prior
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
    theme: {
      background: "#282c34",
      foreground: "#e6edf3",
      cursor: "#58a6ff",
      cursorAccent: "#282c34",
      selectionBackground: "rgba(88,166,255,0.35)",
      black: "#1d1f21",        red: "#cc6666",  green: "#b5bd68",
      yellow: "#f0c674",       blue: "#81a2be", magenta: "#b294bb",
      cyan: "#8abeb7",         white: "#c5c8c6",
      brightBlack: "#969896",  brightRed: "#ff7373",
      brightGreen: "#c8e094",  brightYellow: "#ffd47b",
      brightBlue: "#9ec5fe",   brightMagenta: "#d8b6db",
      brightCyan: "#a8e0d8",   brightWhite: "#ffffff",
    },
  });
  term.open(containerEl);
  term.focus();

  // Cmd+click on a `.md` path → add it as a document to the LGTM
  // session for this pane's repo. Path is resolved against the pane's
  // cwd server-side. Plain click on the underlined path does nothing
  // (no modifier = no action) — the Cmd requirement keeps incidental
  // clicks during scrollback reading from triggering adds.
  //
  // The link provider runs per-rendered-row on demand. Underline-on-
  // hover comes for free from xterm's default link styling.
  registerMarkdownLinkProvider(term);

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
    initialCols = term.cols;
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
    //   plain Esc — let it bubble to the overlay.js Esc stack so the modal
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

  termWsTarget = target;
  termIntentionalClose = false;
  termReconnectAttempt = 0;
  termReconnectedNotified = false;
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
    if (termWs && termWs.readyState === WebSocket.OPEN) {
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
  if (fitDebounce) {
    clearTimeout(fitDebounce);
    fitDebounce = null;
  }
  if (termResizeObserver) {
    try { termResizeObserver.disconnect(); } catch (_) {}
    termResizeObserver = null;
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

// Modal uses this to surface image-paste errors inline in the terminal.
export function writeTerminalLine(line) {
  if (term) term.writeln(line);
}
