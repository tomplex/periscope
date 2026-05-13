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

const modalXtermEl = document.getElementById("modal-xterm");

const DOUBLE_ESC_MS = 300;

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
// Esc-tap state. Single Esc fires onCloseRequested (after a brief debounce
// window so we can distinguish from double-tap). Double Esc within the window
// cancels the close and forwards Esc to the terminal for Claude to interrupt.
let escCloseTimer = null;

export function startLiveTerminal(target, { onCloseRequested }) {
  // Fresh xterm.js instance per modal-open. Dispose any leftover from a prior
  // session before creating a new one.
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
  modalXtermEl.innerHTML = "";

  term = new Terminal({
    fontFamily: '"SF Mono", "JetBrains Mono", "Menlo", monospace',
    fontSize: 12,
    cursorBlink: true,
    scrollback: 5000,
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
  term.open(modalXtermEl);
  term.focus();

  // Fit xterm to the modal container's actual pixel size (so we never clip
  // the bottom rows) and ask tmux to resize the underlying pane to match.
  // Without this, xterm renders at tmux's pane size (often taller than the
  // modal) and the bottom is clipped by overflow:hidden.
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  // ResizeObserver: refit + tell tmux when the modal/window changes size.
  // Debounced so a window-drag doesn't spam tmux with subprocess calls.
  termResizeObserver = new ResizeObserver(scheduleFit);
  termResizeObserver.observe(modalXtermEl);
  requestAnimationFrame(scheduleFit);

  // The browser intercepts Cmd+key combos before xterm sees them. Translate
  // the common ones into readline-style control sequences and forward them
  // to the pane ourselves. Returning false from the handler tells xterm to
  // skip its own processing (which would otherwise be nothing for these).
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;

    // Esc handling: first tap schedules a close; second tap within DOUBLE_ESC_MS
    // cancels the close and lets xterm send Esc to the pane (so Claude sees it
    // for interrupt / cancel).
    if (e.key === "Escape" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
      if (escCloseTimer) {
        clearTimeout(escCloseTimer);
        escCloseTimer = null;
        return true;  // let xterm emit ESC normally
      }
      e.preventDefault();
      escCloseTimer = setTimeout(() => {
        escCloseTimer = null;
        onCloseRequested();
      }, DOUBLE_ESC_MS);
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
  connectTerminalWs(target);
}

function connectTerminalWs(target) {
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${wsProto}://${location.host}/ws/pane?${targetQuery(target)}`);
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
      connectTerminalWs(target);
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
  if (escCloseTimer) {
    clearTimeout(escCloseTimer);
    escCloseTimer = null;
  }
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
