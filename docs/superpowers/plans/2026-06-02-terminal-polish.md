# Terminal Polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship terminal-rendering polish (WebGL, search, theme, visual bell, click dispatcher) plus a file-context shell (files-touched sidebar section, cwd breadcrumb, floating CodeMirror file-preview overlay reachable from terminal Cmd+click, transcript file-path click, or sidebar row click), with a safe-path filesystem API on the server side.

**Architecture:** Two independent halves with three phases.
- **Phase 1** (Half A, client-only): rendering polish in `static/src/terminal/`. WebGL + Search xterm addons vendored as `<script>` tags alongside the existing ones; theme literal extracted; routing link provider replaces single-purpose `.md` provider; Cmd+F bar; visual bell.
- **Phase 2** (Half B, server-only): `periscope/fs.py` (pure `safe_read` / `safe_reveal` + `_for_pane` tmux-resolving wrappers) and `periscope/routes/fs.py`. Pure TDD.
- **Phase 3** (Half B, client): `paneTranscript` + `previewPath` signals in `store.js`; `filesTouched` selector; Sidebar Files section; PaneHeader cwd breadcrumb; CodeMirror 6 preview overlay; three click entry points wired to the same overlay.

**Tech Stack:** Preact + `@preact/signals`, vendored xterm.js (with `@xterm/addon-webgl` + `@xterm/addon-search` added the same way), CodeMirror 6 via npm, FastAPI (Python 3.12), pytest, vitest (new — added in Phase 3 for the pure selector test only).

**Spec:** `docs/superpowers/specs/2026-06-02-terminal-polish-design.md`

---

## Phase 1 — Rendering polish (client-only)

### Task 1: Vendor `@xterm/addon-webgl` + try-with-fallback load

**Files:**
- Create: `static/vendor/addon-webgl.js` (downloaded from xterm.js release)
- Modify: `static/index.html` (add `<script>` tag)
- Modify: `static/src/terminal/terminalCore.js` (try-load addon after `term.open`)

- [ ] **Step 1: Download the addon to vendor/.**

```bash
cd /Users/tom/dev/periscope
curl -L -o static/vendor/addon-webgl.js \
  https://unpkg.com/@xterm/addon-webgl@0.18.0/lib/addon-webgl.js
```

Verify the file is non-empty and exposes `WebglAddon`:

```bash
head -c 200 static/vendor/addon-webgl.js
grep -c "WebglAddon" static/vendor/addon-webgl.js
```

Expected: `head` shows minified UMD wrapper; `grep -c` returns at least `1`.

- [ ] **Step 2: Add the `<script>` tag in `static/index.html`.**

Locate the existing block (around line 29):

```html
<script src="/vendor/xterm.js"></script>
<script src="/vendor/addon-fit.js"></script>
```

Add immediately after:

```html
<script src="/vendor/addon-webgl.js"></script>
```

- [ ] **Step 3: Load the addon in `terminalCore.js` after `term.open(containerEl)`.**

In `static/src/terminal/terminalCore.js`, immediately after `term.focus();` (around line 127), add:

```js
  // Try WebGL renderer; fall back to canvas on init failure (older Chromes,
  // headless contexts, GPU-disabled environments). The addon writes to its
  // own canvas inside xterm's element tree, so failure is silent on success
  // paths but we log it once for diagnosis.
  try {
    const webgl = new WebglAddon.WebglAddon();
    webgl.onContextLoss(() => {
      try { webgl.dispose(); } catch (_) {}
    });
    term.loadAddon(webgl);
  } catch (e) {
    console.warn("[periscope] WebGL terminal renderer unavailable; falling back to canvas:", e);
  }
```

- [ ] **Step 4: Build and smoke-test in the browser.**

```bash
cd /Users/tom/dev/periscope
npm run build
```

Run dev periscope (or restart prod), open a Claude pane in the dashboard, scroll through scrollback rapidly. Expected: no visible regressions; DevTools Network shows `addon-webgl.js` 200; no console errors. Note: visual difference vs canvas is subtle on small terminals; the win shows up under high-throughput rendering.

- [ ] **Step 5: Commit.**

```bash
git add static/vendor/addon-webgl.js static/index.html static/src/terminal/terminalCore.js static/dist/app.js
git commit -m "terminal: vendor @xterm/addon-webgl with canvas fallback"
```

---

### Task 2: Vendor `@xterm/addon-search`

**Files:**
- Create: `static/vendor/addon-search.js`
- Modify: `static/index.html`
- Modify: `static/src/terminal/terminalCore.js` (instantiate but don't wire UI yet — Cmd+F bar lands in Task 5)

- [ ] **Step 1: Download the addon.**

```bash
curl -L -o static/vendor/addon-search.js \
  https://unpkg.com/@xterm/addon-search@0.15.0/lib/addon-search.js
grep -c "SearchAddon" static/vendor/addon-search.js
```

Expected: `>=1`.

- [ ] **Step 2: Add the `<script>` tag in `static/index.html`** after the webgl addon:

```html
<script src="/vendor/addon-search.js"></script>
```

- [ ] **Step 3: Load + expose the addon in `terminalCore.js`.**

Add at the top of the file with the other module-level state (around `let fitAddon = null;`):

```js
let searchAddon = null;
```

Then immediately after the WebGL try/catch from Task 1, add:

```js
  searchAddon = new SearchAddon.SearchAddon();
  term.loadAddon(searchAddon);
```

Export search primitives at the bottom of the file (above the mount helper section, around line 350):

```js
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
```

In `stopLiveTerminal()`, just before `fitAddon = null;`, add:

```js
  searchAddon = null;
```

- [ ] **Step 4: Build, smoke-test in browser.**

```bash
npm run build
```

Expected: no console errors; `addon-search.js` loads 200.

- [ ] **Step 5: Commit.**

```bash
git add static/vendor/addon-search.js static/index.html static/src/terminal/terminalCore.js static/dist/app.js
git commit -m "terminal: vendor @xterm/addon-search (exports searchNext/searchPrev/clearSearch)"
```

---

### Task 3: Extract theme to its own module + tune

**Files:**
- Create: `static/src/terminal/theme.js`
- Modify: `static/src/terminal/terminalCore.js`

- [ ] **Step 1: Create `static/src/terminal/theme.js`.**

```js
// xterm.js theme tokens. One object, used by terminalCore. Refinements vs
// the original inline theme:
//   - cursor color sharpened (was #58a6ff, now #7aa2f7) so the blink reads
//     against #282c34 instead of washing out
//   - selectionBackground darkened slightly so selections over Claude's
//     yellow status line stay legible
//   - brightGreen/Red slightly more saturated for diff readability
// Background unchanged — muscle memory + existing screenshots.

export const terminalTheme = {
  background: "#282c34",
  foreground: "#e6edf3",
  cursor: "#7aa2f7",
  cursorAccent: "#282c34",
  selectionBackground: "rgba(88,166,255,0.28)",
  black: "#1d1f21",        red: "#cc6666",  green: "#b5bd68",
  yellow: "#f0c674",       blue: "#81a2be", magenta: "#b294bb",
  cyan: "#8abeb7",         white: "#c5c8c6",
  brightBlack: "#969896",  brightRed: "#ff7373",
  brightGreen: "#cce29b",  brightYellow: "#ffd47b",
  brightBlue: "#9ec5fe",   brightMagenta: "#d8b6db",
  brightCyan: "#a8e0d8",   brightWhite: "#ffffff",
};
```

- [ ] **Step 2: Replace the inline theme literal in `terminalCore.js`.**

Add an import at the top with the other imports:

```js
import { terminalTheme } from "./theme.js";
```

Find the `new Terminal({...})` call (around line 97). Replace the `theme: { ... }` block with:

```js
    theme: terminalTheme,
```

Also add three new options on the same `Terminal` constructor call:

```js
    cursorStyle: "block",
    fontWeight: "400",
    // CSS ligatures aren't a runtime xterm setting in the vendored bundle,
    // but the parent container's CSS sets font-feature-settings so JetBrains
    // Mono / SF Mono show their ligatures when present.
```

(Note: `cursorBlink: true` is already in the existing literal — keep it.)

- [ ] **Step 3: Add CSS padding + ligature settings.**

Open `static/styles.css` and locate `.detail-xterm` (the host class — search for `detail-xterm`). Add or extend its rules:

```css
.detail-xterm {
  padding: 8px;
  font-feature-settings: "liga" 1, "calt" 1;
}
.modal-xterm {
  padding: 8px;
  font-feature-settings: "liga" 1, "calt" 1;
}
```

(If those classes exist already, merge the rules; don't duplicate selectors.)

- [ ] **Step 4: Build + visual smoke-test.**

```bash
npm run build
```

Open the dashboard, open a Claude pane. Expected: cursor reads as sharper blue; no clipping; padding visible inside the terminal frame.

- [ ] **Step 5: Commit.**

```bash
git add static/src/terminal/theme.js static/src/terminal/terminalCore.js static/styles.css static/dist/app.js
git commit -m "terminal: extract theme module; sharper cursor, padding, ligature opt-in"
```

---

### Task 4: Click dispatcher router (URL handler wired now; file/md routed via setters)

**Files:**
- Modify: `static/src/terminal/terminalCore.js`

This task replaces the single-purpose `registerMarkdownLinkProvider` with one routing link provider. URL handling is wired immediately (no parent registration needed — opens `window.open`). File and `.md` handlers stay setter-based; parents register them in Phase 3.

- [ ] **Step 1: Replace `MD_PATH_RE` with three regexes.**

In `static/src/terminal/terminalCore.js`, find the `MD_PATH_RE` constant (around line 53). Replace that block with:

```js
// URL: http/https/ws/wss with no embedded whitespace; conservative on what
// terminates — most CLI URLs don't include trailing punctuation we'd want
// to strip, so we eat as much as possible up to whitespace or quote/paren.
const URL_RE = /\b(?:https?|wss?):\/\/[^\s)"'`<>]+/g;

// Absolute file path: starts with /, followed by path chars. Excludes
// trailing punctuation (.,)";:) — common in prose. Bounded on the left by
// a word boundary or start-of-line.
const ABS_PATH_RE = /(?<![\w./-])\/[\w./-]+\b(?::\d+)?/g;

// Relative path with a known extension. The ext list intentionally errs
// toward false negatives — Cmd+click on garbage in scrollback is worse
// than a missed click. Expand cautiously when concrete misses surface.
const REL_PATH_RE = /(?<![\w./-])\.{0,2}\/[\w./-]+\.(?:md|py|ts|tsx|js|jsx|json|html|css|rs|go|toml|yaml|yml|sh|sql|txt|rb)\b(?::\d+)?/g;

// .md special-case for LGTM routing (legacy behavior preserved). Same
// shape as the prior MD_PATH_RE but renamed for clarity.
const MD_PATH_RE = /(?<![\w./-])[\w./~-]*[\w-]+\.md(?::\d+)?(?!\w)/g;
```

- [ ] **Step 2: Add three handler setters.**

Find `setTerminalLinkCallback` (around line 44). Add two more setters next to it:

```js
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
```

- [ ] **Step 3: Replace `registerMarkdownLinkProvider` with a routing provider.**

Find `registerMarkdownLinkProvider` (around line 55) and replace the whole function with:

```js
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
```

- [ ] **Step 4: Swap the call site.**

Find the call to `registerMarkdownLinkProvider(term);` (around line 137). Replace with:

```js
  registerRoutingLinkProvider(term);
```

- [ ] **Step 5: Build + verify URL clicks work.**

```bash
npm run build
```

Open the dashboard, open any pane that has a URL in scrollback (any Claude pane mentioning a PR URL will do). Cmd+click — opens in new tab. Expected: no regression on existing `.md → LGTM` behavior in panes where Detail.jsx has registered the LGTM callback (already wired in detail integration paths).

- [ ] **Step 6: Commit.**

```bash
git add static/src/terminal/terminalCore.js static/dist/app.js
git commit -m "terminal: routing link provider (URL/file/md), Cmd+click URLs land in new tab"
```

---

### Task 5: Cmd+F search bar in `<Terminal>`

**Files:**
- Modify: `static/src/terminal/Terminal.jsx`
- Modify: `static/styles.css`

- [ ] **Step 1: Add the search bar UI to `<Terminal>`.**

Replace the contents of `static/src/terminal/Terminal.jsx` with:

```jsx
// Thin ref+useEffect wrapper over the imperative xterm/WS core
// (terminalCore.js). This is the ONLY Preact-aware part of the terminal —
// the xterm instance, WebSocket, reconnect FSM, fit/resize, paste, and
// link handlers all live in the core and stay imperative.
//
// Lifecycle: the empty-deps effect mounts the live terminal ONCE per
// component instance and tears it down on unmount. Call sites KEY this
// component on the pane's pid so re-selecting the same pane preserves
// this instance.
//
// Cmd+F opens a search bar overlay above the terminal. The bar is
// rendered here (Preact) but the actual search work is in terminalCore
// (xterm.js addon-search). Esc closes the bar via useEscape (LIFO).
import { useRef, useEffect, useState, useCallback } from "preact/hooks";
import {
  mountTerminal, unmountTerminal,
  searchNext, searchPrev, clearSearch,
} from "./terminalCore.js";
import { useEscape } from "../hooks/useEscape.js";

export function Terminal({ target, onMdLink, onPaste, class: className = "modal-xterm", id }) {
  const hostRef = useRef(null);
  const inputRef = useRef(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    mountTerminal(hostRef.current, target, { onMdLink, onPaste });
    return unmountTerminal;
  }, []); // empty deps — mount ONCE per component instance (pid-keyed at call site)

  // Cmd+F opens the search bar. Use a window-level listener so the user
  // doesn't have to focus the terminal first.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setSearchOpen(true);
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const close = useCallback(() => {
    setSearchOpen(false);
    setQuery("");
    clearSearch();
  }, []);

  useEscape(close, searchOpen);

  function onSubmit(e) {
    e.preventDefault();
    if (!query) return;
    if (e.shiftKey) searchPrev(query); else searchNext(query);
  }

  return (
    <div class="terminal-wrap">
      {searchOpen && (
        <form class="term-search" onSubmit={onSubmit}>
          <input
            ref={inputRef}
            class="term-search-input"
            value={query}
            placeholder="find in terminal"
            onInput={(e) => { setQuery(e.currentTarget.value); }}
          />
          <button type="button" class="term-search-btn"
                  title="Previous (Shift+Enter)"
                  onClick={() => query && searchPrev(query)}>‹</button>
          <button type="button" class="term-search-btn"
                  title="Next (Enter)"
                  onClick={() => query && searchNext(query)}>›</button>
          <button type="button" class="term-search-btn"
                  title="Close (Esc)"
                  onClick={close}>✕</button>
        </form>
      )}
      <div ref={hostRef} id={id} class={className} />
    </div>
  );
}
```

- [ ] **Step 2: Add CSS for the search bar.**

Append to `static/styles.css`:

```css
.terminal-wrap {
  display: contents;
  position: relative;
}
.term-search {
  position: absolute;
  top: 4px;
  right: 12px;
  z-index: 5;
  display: flex;
  gap: 4px;
  align-items: center;
  background: rgba(28, 30, 36, 0.95);
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  padding: 4px 6px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.25);
}
.term-search-input {
  width: 220px;
  background: transparent;
  border: none;
  outline: none;
  color: var(--fg-1, #e6edf3);
  font: inherit;
  padding: 2px 4px;
}
.term-search-btn {
  background: transparent;
  border: none;
  color: var(--fg-2, #c5c8c6);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
}
.term-search-btn:hover {
  background: rgba(255,255,255,0.06);
}
```

- [ ] **Step 3: Build + browser-verify.**

```bash
npm run build
```

Open a Claude pane in the dashboard. Press Cmd+F → bar appears, input focused. Type a string visible in scrollback → Enter highlights next match; Shift+Enter highlights previous; Esc closes; arrow buttons work.

- [ ] **Step 4: Commit.**

```bash
git add static/src/terminal/Terminal.jsx static/styles.css static/dist/app.js
git commit -m "terminal: Cmd+F search bar (xterm addon-search, Esc closes, ← →)"
```

---

### Task 6: Visual bell + working→idle|done ping

**Files:**
- Modify: `static/src/terminal/terminalCore.js`
- Modify: `static/src/split/Detail.jsx`
- Modify: `static/styles.css`

- [ ] **Step 1: Wire the BEL handler in `terminalCore.js`.**

In `static/src/terminal/terminalCore.js`, find the block where `term.attachCustomKeyEventHandler` is registered (around line 171). Immediately after `term.onData(...)` (around line 215), add:

```js
  // BEL (\x07) — flash the container border. Triggered by `printf '\a'` and
  // many CLI tools' notification hooks.
  term.onBell(() => {
    if (containerEl) {
      containerEl.classList.add("bell-pulse");
      setTimeout(() => containerEl.classList.remove("bell-pulse"), 400);
    }
  });
```

- [ ] **Step 2: Add CSS for the bell pulse.**

Append to `static/styles.css`:

```css
@keyframes terminal-bell-pulse {
  0%   { box-shadow: 0 0 0 2px rgba(255, 213, 79, 0); }
  20%  { box-shadow: 0 0 0 2px rgba(255, 213, 79, 0.85); }
  100% { box-shadow: 0 0 0 2px rgba(255, 213, 79, 0); }
}
.detail-xterm.bell-pulse,
.modal-xterm.bell-pulse {
  animation: terminal-bell-pulse 400ms ease-out;
}
```

- [ ] **Step 3: Add the working→idle|done ping in `<PaneDetail>`.**

Open `static/src/split/Detail.jsx`. Find the `<PaneDetail>` function (around line 209). After the existing `useEffect`s but before the `return`, add:

```jsx
  // Working → (idle | done) transition pulse for Claude panes. The server
  // refines idle → done when there's an unacknowledged completion stamp
  // (window_view.py:120-121), so the visible-to-client transition is
  // almost always working → done. Watching both is correct.
  const prevState = useRef(w.state);
  useEffect(() => {
    const wasWorking = prevState.current === "working";
    const isFinished = w.state === "idle" || w.state === "done";
    if (wasWorking && isFinished && w.is_claude) {
      const el = document.getElementById("detail-xterm");
      if (el) {
        el.classList.add("bell-pulse");
        setTimeout(() => el.classList.remove("bell-pulse"), 400);
      }
    }
    prevState.current = w.state;
  }, [w.state, w.is_claude]);
```

Add `useRef` to the import at the top of the file if it's not there. Check the imports — `useRef, useEffect, useState` should all be present.

- [ ] **Step 4: Build + verify.**

```bash
npm run build
```

In a tmux pane, run `printf '\a'`. Open that pane in periscope. Expected: terminal border pulses yellow briefly.

For the working→idle test: ask any Claude pane to do a short task and watch for the pulse when it finishes.

- [ ] **Step 5: Commit.**

```bash
git add static/src/terminal/terminalCore.js static/src/split/Detail.jsx static/styles.css static/dist/app.js
git commit -m "terminal: visual bell on \\a + working→idle|done pulse for claude panes"
```

---

## Phase 2 — Server filesystem API (TDD)

### Task 7: `tests/test_fs.py` — failing tests for pure `safe_read`

**Files:**
- Create: `tests/test_fs.py`

- [ ] **Step 1: Write the test file.**

```python
"""Pure unit tests for periscope.fs.safe_read / safe_reveal.

The tmux-resolving variants (safe_read_for_pane / safe_reveal_for_pane)
are tested in tests/routes/test_fs.py against route-level fixtures.
"""
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from periscope import fs


def test_safe_read_absolute_inside_cwd(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hi there\n")
    resolved, contents = fs.safe_read(str(tmp_path), str(f))
    assert resolved == str(f.resolve())
    assert contents == "hi there\n"


def test_safe_read_relative_against_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "file.py"
    f.write_text("print('ok')\n")
    resolved, contents = fs.safe_read(str(tmp_path), "sub/file.py")
    assert resolved == str(f.resolve())
    assert contents == "print('ok')\n"


def test_safe_read_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "rc"
    f.write_text("x\n")
    resolved, contents = fs.safe_read(str(tmp_path), "~/rc")
    assert resolved == str(f.resolve())
    assert contents == "x\n"


def test_safe_read_dotdot_escape_blocked(tmp_path):
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret").write_text("nope")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(cwd), "../outside/secret")
    assert exc.value.status_code == 403


def test_safe_read_missing_file(tmp_path):
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), "no-such-file")
    assert exc.value.status_code == 404


def test_safe_read_oversize(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 1024)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), str(f), max_bytes=128)
    assert exc.value.status_code == 413


def test_safe_read_binary_file(tmp_path):
    f = tmp_path / "icon.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), str(f))
    assert exc.value.status_code == 415


def test_safe_read_empty_path(tmp_path):
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), "")
    assert exc.value.status_code == 400


def test_safe_read_prefix_confusion_guard(tmp_path):
    # /tmp/a/cwd as the safe root must NOT permit /tmp/a/cwd-sibling/...
    cwd = tmp_path / "a" / "cwd"
    cwd.mkdir(parents=True)
    sibling = tmp_path / "a" / "cwd-sibling"
    sibling.mkdir()
    (sibling / "secret").write_text("nope")
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(cwd), str(sibling / "secret"))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
cd /Users/tom/dev/periscope
uv run pytest tests/test_fs.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` on `periscope.fs`. All tests fail at collection. That's correct — Task 8 implements the module.

- [ ] **Step 3: Commit the failing test.**

```bash
git add tests/test_fs.py
git commit -m "tests: failing safe_read/safe_reveal tests for periscope.fs"
```

---

### Task 8: Implement `periscope/fs.py` — pure `safe_read` + `safe_reveal`

**Files:**
- Create: `periscope/fs.py`

- [ ] **Step 1: Write the module.**

```python
"""Safe filesystem access for periscope's file-preview overlay + reveal.

Sole filesystem-access seam for the client (invariant): every new route
that touches a file goes through here. Resolves user-supplied paths
against a cwd, refuses anything outside a small set of safe roots, caps
file size, and surfaces clean HTTPException codes.

Tmux-resolving variants (`_for_pane`) are thin wrappers below — kept
separate so unit tests of the pure resolution logic don't have to mock
tmux subprocess calls.
"""
import os
import subprocess
from pathlib import Path

from fastapi import HTTPException


_MAX_BYTES_DEFAULT = 1_000_000


def _safe_roots(cwd: Path) -> list[Path]:
    """Roots a resolved path is allowed to live under.

    - cwd (and descendants)
    - the cwd's git repo root, if any
    - $HOME (~)
    - /tmp, /var/tmp — Tom occasionally pastes build-artifact paths

    Anything else → 403.
    """
    roots = [cwd]
    repo = _git_repo_root(cwd)
    if repo:
        roots.append(repo)
    home = Path(os.path.expanduser("~"))
    if home.exists():
        roots.append(home)
    for extra in ("/tmp", "/var/tmp"):
        p = Path(extra)
        if p.exists():
            roots.append(p)
    return [r.resolve() for r in roots]


def _git_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a .git dir. None if not found."""
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _inside_any(resolved: Path, roots: list[Path]) -> bool:
    """True if `resolved` is `r` or a descendant of any `r` in roots.
    commonpath-based to defeat /foo vs /foobar prefix confusion."""
    rstr = str(resolved)
    for r in roots:
        try:
            if os.path.commonpath([rstr, str(r)]) == str(r):
                return True
        except ValueError:
            continue
    return False


def safe_read(cwd: str, raw_path: str,
              max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, str]:
    """Resolve `raw_path` against `cwd`, enforce safe roots, read as UTF-8.

    Returns (resolved_abs_path, contents).

    Raises HTTPException with:
      400 — empty path.
      403 — resolved path escapes the safe roots.
      404 — file missing.
      413 — file exceeds max_bytes.
      415 — file is not UTF-8 decodable (binary).
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="empty path")

    cwd_p = Path(cwd).resolve()
    if not cwd_p.exists():
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")

    # Strip a trailing ":NN" line suffix if present; we don't read it here
    # but callers' regex may include it.
    candidate = raw_path
    if ":" in candidate and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]

    expanded = os.path.expanduser(candidate)
    if os.path.isabs(expanded):
        target = Path(expanded)
    else:
        target = cwd_p / expanded
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail=f"path not resolvable: {candidate}")

    roots = _safe_roots(cwd_p)
    if not _inside_any(resolved, roots):
        raise HTTPException(status_code=403, detail="path outside safe roots")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no such file: {resolved}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="not a regular file")

    size = resolved.stat().st_size
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({size} > {max_bytes} bytes)",
        )

    blob = resolved.read_bytes()
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="binary file")

    return (str(resolved), text)


def safe_reveal(cwd: str, raw_path: str) -> None:
    """Resolve `raw_path` against `cwd`, enforce safe roots, `open -R`.

    Same gating as safe_read; on success runs macOS Finder reveal.
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="empty path")
    cwd_p = Path(cwd).resolve()
    candidate = raw_path
    if ":" in candidate and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]
    expanded = os.path.expanduser(candidate)
    target = Path(expanded) if os.path.isabs(expanded) else cwd_p / expanded
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail=f"path not resolvable: {candidate}")
    if not _inside_any(resolved, _safe_roots(cwd_p)):
        raise HTTPException(status_code=403, detail="path outside safe roots")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no such file: {resolved}")
    # Best-effort; don't surface non-zero exits as 500 (Finder may be
    # closed, etc.). Logging level is debug because this is user-visible
    # and the failure mode is benign.
    subprocess.run(["open", "-R", str(resolved)], check=False)
```

- [ ] **Step 2: Run the tests, expect pass.**

```bash
uv run pytest tests/test_fs.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit.**

```bash
git add periscope/fs.py
git commit -m "fs: safe_read / safe_reveal — pure path resolver + UTF-8 reader"
```

---

### Task 9: `_for_pane` wrappers + their tests

**Files:**
- Modify: `tests/test_fs.py`
- Modify: `periscope/fs.py`

- [ ] **Step 1: Append wrapper tests to `tests/test_fs.py`.**

Append at the end of the file:

```python
def test_safe_read_for_pane_uses_tmux_cwd(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("ok\n")

    def fake_tmux(*args):
        # display-message resolves the pane's cwd
        if args[:1] == ("display-message",):
            return str(tmp_path) + "\n"
        raise AssertionError(f"unexpected tmux call: {args}")

    monkeypatch.setattr("periscope.fs.tmux", fake_tmux)
    resolved, contents = fs.safe_read_for_pane("sess:1", "x.txt")
    assert contents == "ok\n"
    assert resolved == str(f.resolve())


def test_safe_read_for_pane_404_when_target_unknown(monkeypatch):
    def fake_tmux(*args):
        raise RuntimeError("no such target")

    monkeypatch.setattr("periscope.fs.tmux", fake_tmux)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read_for_pane("sess:1", "x.txt")
    assert exc.value.status_code == 404


def test_safe_reveal_for_pane_invokes_open_R(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("ok\n")
    called = []

    def fake_tmux(*args):
        if args[:1] == ("display-message",):
            return str(tmp_path) + "\n"
        raise AssertionError(f"unexpected tmux call: {args}")

    def fake_run(cmd, **kw):
        called.append(cmd)
    monkeypatch.setattr("periscope.fs.tmux", fake_tmux)
    monkeypatch.setattr("periscope.fs.subprocess.run", fake_run)
    fs.safe_reveal_for_pane("sess:1", "x.txt")
    assert called == [["open", "-R", str(f.resolve())]]
```

- [ ] **Step 2: Run tests, expect failure on missing `safe_read_for_pane`.**

```bash
uv run pytest tests/test_fs.py -v
```

Expected: 3 new tests fail with AttributeError or ImportError.

- [ ] **Step 3: Add the `_for_pane` wrappers to `periscope/fs.py`.**

Add at the top with the other imports:

```python
from periscope.tmux import tmux
```

Append at the end of `periscope/fs.py`:

```python
def _cwd_for_target(target: str) -> str:
    """Resolve the pane's cwd via `tmux display-message`. Same one-shot
    pattern as periscope/turns.py:get_turns_for_pane."""
    try:
        out = tmux(
            "display-message", "-t", target, "-p", "#{pane_current_path}"
        ).strip()
    except Exception:
        raise HTTPException(status_code=404, detail=f"unknown pane: {target}")
    if not out:
        raise HTTPException(status_code=404, detail=f"pane has no cwd: {target}")
    return out


def safe_read_for_pane(target: str, raw_path: str,
                       max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, str]:
    """tmux-resolves cwd from `target`, then calls safe_read."""
    return safe_read(_cwd_for_target(target), raw_path, max_bytes)


def safe_reveal_for_pane(target: str, raw_path: str) -> None:
    """tmux-resolves cwd from `target`, then calls safe_reveal."""
    safe_reveal(_cwd_for_target(target), raw_path)
```

- [ ] **Step 4: Run tests, expect pass.**

```bash
uv run pytest tests/test_fs.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_fs.py periscope/fs.py
git commit -m "fs: _for_pane wrappers tmux-resolve cwd from session:index target"
```

---

### Task 10: `/api/fs/read` + `/api/fs/open` routes

**Files:**
- Create: `tests/routes/test_fs.py`
- Create: `periscope/routes/fs.py`
- Modify: `periscope/app.py` (register the router)

- [ ] **Step 1: Write failing route tests.**

```python
"""Route tests for /api/fs/read and /api/fs/open. Exercises the
HTTP surface; the safe-path logic itself is tested in tests/test_fs.py."""
from fastapi.testclient import TestClient

from periscope.app import app


def test_fs_read_happy(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("print('ok')\n")
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": "f.py"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "print('ok')\n"
    assert body["language"] == "python"
    assert body["path"].endswith("/f.py")


def test_fs_read_blank_path(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": ""})
    assert r.status_code == 400


def test_fs_read_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": "nope.txt"})
    assert r.status_code == 404


def test_fs_read_unknown_pane(monkeypatch):
    def boom(*a):
        raise RuntimeError("no such target")
    monkeypatch.setattr("periscope.fs.tmux", boom)
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 99, "path": "x"})
    assert r.status_code == 404


def test_fs_open_reveal_invokes_subprocess(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("x\n")
    called = []
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    monkeypatch.setattr("periscope.fs.subprocess.run",
                        lambda c, **kw: called.append(c))
    client = TestClient(app)
    r = client.post("/api/fs/open",
                    params={"session": "s", "index": 1,
                            "path": "f.py", "action": "reveal"})
    assert r.status_code == 200
    assert called and called[0][:2] == ["open", "-R"]


def test_fs_open_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.post("/api/fs/open",
                    params={"session": "s", "index": 1,
                            "path": "x", "action": "edit"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run, expect failure.**

```bash
uv run pytest tests/routes/test_fs.py -v
```

Expected: 404 on the routes themselves (router not registered).

- [ ] **Step 3: Implement `periscope/routes/fs.py`.**

```python
"""GET /api/fs/read — read a file relative to a pane's cwd, with safe-path gating.
POST /api/fs/open?action=reveal — macOS reveal-in-Finder.

Both share the tmux-resolving wrappers in periscope.fs."""
import os

from fastapi import APIRouter

from periscope import fs

router = APIRouter()


_LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html", ".htm": "html",
    ".css": "css",
    ".rs": "rust",
    ".go": "go",
    ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell",
    ".sql": "sql",
}


def _language_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _LANGUAGE_BY_EXT.get(ext, "plain")


@router.get("/api/fs/read")
def fs_read(session: str, index: int, path: str):
    target = f"{session}:{index}"
    resolved, content = fs.safe_read_for_pane(target, path)
    return {"path": resolved, "content": content, "language": _language_for(resolved)}


@router.post("/api/fs/open")
def fs_open(session: str, index: int, path: str, action: str = "reveal"):
    if action != "reveal":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    target = f"{session}:{index}"
    fs.safe_reveal_for_pane(target, path)
    return {"ok": True}
```

- [ ] **Step 4: Register the router in `periscope/app.py`.**

In `periscope/app.py`, locate the multi-line import (line 24-27):

```python
from periscope.routes import (
    alerts, auto_rename, channel, healthz, history, pane, paste_image, prefs,
    send, sessions, state, ws,
)
```

Add `fs` to the import list alphabetically:

```python
from periscope.routes import (
    alerts, auto_rename, channel, fs, healthz, history, pane, paste_image, prefs,
    send, sessions, state, ws,
)
```

Then locate the router mount loop (around line 99-104):

```python
for r in (
    alerts, auto_rename, channel, cleanup_routes, healthz, history, lgtm_route,
    pane, paste_image, prefs, projects_routes, send, sessions, settings_routes,
    state, ws,
):
    app.include_router(r.router)
```

Add `fs` to the tuple alphabetically:

```python
for r in (
    alerts, auto_rename, channel, cleanup_routes, fs, healthz, history, lgtm_route,
    pane, paste_image, prefs, projects_routes, send, sessions, settings_routes,
    state, ws,
):
    app.include_router(r.router)
```

- [ ] **Step 5: Run all the new tests + the smoke suite, expect pass.**

```bash
uv run pytest tests/test_fs.py tests/routes/test_fs.py tests/test_smoke.py -v
```

Expected: all green.

- [ ] **Step 6: Commit.**

```bash
git add tests/routes/test_fs.py periscope/routes/fs.py periscope/app.py
git commit -m "routes: GET /api/fs/read + POST /api/fs/open?action=reveal"
```

---

## Phase 3 — File-context shell UI

### Task 11: vitest setup + `paneTranscript` / `previewPath` signals

**Files:**
- Modify: `package.json`
- Create: `vitest.config.js`
- Modify: `static/src/store.js`

- [ ] **Step 1: Add vitest to devDependencies.**

```bash
cd /Users/tom/dev/periscope
npm install --save-dev vitest@^2.0.0
```

Verify `package.json` now lists vitest. Then add a `test` script:

Edit `package.json`'s `"scripts"` block:

```json
  "scripts": {
    "dev": "./dev.sh",
    "build": "vite build",
    "test": "vitest run"
  },
```

- [ ] **Step 2: Create `vitest.config.js`.**

```js
import { defineConfig } from "vitest/config";
import preact from "@preact/preset-vite";

// Test config — covers ONLY pure data transforms (selectors, parsers).
// React/Preact components are verified in the browser per CLAUDE.md
// ("UI work: test in the browser"). Add component tests only for state
// reducers where the browser is a bad oracle.
export default defineConfig({
  plugins: [preact()],
  test: {
    include: ["static/src/**/__tests__/**/*.test.{js,jsx}"],
    environment: "node",
  },
});
```

- [ ] **Step 3: Add the two new signals to `static/src/store.js`.**

Open `static/src/store.js`. Find the existing transcript-related signals (around lines 30-31):

```js
export const transcriptMode = signal({});   // { [pid]: "transcript" | "terminal" }
export const transcriptSeen = signal({});   // { [pid]: true }
```

Add immediately after them:

```js
// Shared transcript messages — written by useTranscriptPoll in
// Transcript.jsx (the kept-mounted instance for each opened Claude pid),
// read by both TranscriptView (own messages) and Sidebar's Files section
// (selected pane's messages). One poll per selected pid; no duplicate
// fetches. Evicted alongside transcript-host pruning in Detail.jsx.
export const paneTranscript = signal({});   // { [pid]: { messages, sessionId } }

// File-preview overlay state. Non-null => overlay is shown for that path.
// Three setters: terminal Cmd+click (via terminalCore link router),
// transcript tool-call chip click, sidebar Files row click. All write
// the same shape: { path, line } where line may be null.
export const previewPath = signal(null);
```

- [ ] **Step 4: Smoke check.**

```bash
npm run build
```

Expected: no build errors. (Tests come next task.)

- [ ] **Step 5: Commit.**

```bash
git add package.json package-lock.json vitest.config.js static/src/store.js static/dist/app.js
git commit -m "store: paneTranscript + previewPath signals; add vitest for pure-selector tests"
```

---

### Task 12: `filesTouched` selector + tests

**Files:**
- Create: `static/src/split/filesTouched.js`
- Create: `static/src/split/__tests__/filesTouched.test.js`

- [ ] **Step 1: Write the failing test.**

```js
import { describe, it, expect } from "vitest";
import { filesTouched } from "../filesTouched.js";

const u = (text) => ({ role: "user", user_text: text });
const a = (toolUses = []) => ({ role: "assistant", tool_uses: toolUses });
const tu = (name, file_path, extra = {}) => ({
  id: Math.random().toString(36),
  name,
  input: { file_path, ...extra },
});

describe("filesTouched", () => {
  it("returns empty for no messages", () => {
    expect(filesTouched([])).toEqual([]);
  });

  it("collapses one Read into a single entry", () => {
    const out = filesTouched([
      u("hi"),
      a([tu("Read", "src/a.ts")]),
    ]);
    expect(out).toEqual([{ path: "src/a.ts", op: "Read" }]);
  });

  it("dedups by path, latest op wins", () => {
    const out = filesTouched([
      a([tu("Read", "src/a.ts")]),
      a([tu("Edit", "src/a.ts")]),
    ]);
    expect(out).toEqual([{ path: "src/a.ts", op: "Edit" }]);
  });

  it("orders most-recent first", () => {
    const out = filesTouched([
      a([tu("Read", "src/a.ts")]),
      a([tu("Write", "src/b.ts")]),
    ]);
    expect(out).toEqual([
      { path: "src/b.ts", op: "Write" },
      { path: "src/a.ts", op: "Read" },
    ]);
  });

  it("recognizes MultiEdit and NotebookEdit", () => {
    const out = filesTouched([
      a([tu("MultiEdit", "src/a.ts")]),
      a([tu("NotebookEdit", "nb.ipynb", { notebook_path: "nb.ipynb" })]),
    ]);
    expect(out.map((x) => x.path)).toEqual(["nb.ipynb", "src/a.ts"]);
  });

  it("ignores non-file tools (Bash, Grep, Glob)", () => {
    const out = filesTouched([
      a([tu("Bash", undefined, { command: "rm foo.txt" })]),
      a([tu("Grep", undefined, { pattern: "TODO" })]),
    ]);
    expect(out).toEqual([]);
  });

  it("ignores tool_uses lacking file_path", () => {
    const out = filesTouched([
      a([{ id: "x", name: "Read", input: {} }]),
    ]);
    expect(out).toEqual([]);
  });
});
```

- [ ] **Step 2: Run, expect failure.**

```bash
npm test
```

Expected: import error on `filesTouched`.

- [ ] **Step 3: Implement the selector.**

```js
// Pure selector: collapses /api/pane/turns messages into a per-path
// ordered list, most-recent op first. Used by Sidebar's Files section.
//
// Tool scope is intentional: Read / Edit / Write / MultiEdit / NotebookEdit
// — the tools whose `input.file_path` cleanly names the touched path.
// Bash 'rm' / 'mv' detection would be brittle (false positives in long
// commands) and Claude has no formal Delete tool; deletes via Bash are
// not shown (accepted v1 limitation, see design spec).
const FILE_TOOLS = new Set([
  "Read", "Edit", "Write", "MultiEdit", "NotebookEdit",
]);

export function filesTouched(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  // Walk newest-to-oldest so the first occurrence we see for any path is
  // its latest op. Track seen paths so we don't override.
  const seen = new Map();
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const tus = m && m.tool_uses;
    if (!tus || !tus.length) continue;
    // Within a single assistant turn, walk tool_uses in reverse for the
    // same reason — last op in the turn wins for that path.
    for (let j = tus.length - 1; j >= 0; j--) {
      const t = tus[j];
      if (!t || !FILE_TOOLS.has(t.name)) continue;
      const path = t.input && (t.input.file_path || t.input.notebook_path);
      if (!path) continue;
      if (seen.has(path)) continue;
      seen.set(path, t.name);
    }
  }
  return [...seen.entries()].map(([path, op]) => ({ path, op }));
}
```

- [ ] **Step 4: Run, expect pass.**

```bash
npm test
```

Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add static/src/split/filesTouched.js static/src/split/__tests__/filesTouched.test.js
git commit -m "split: filesTouched selector (Read/Edit/Write/MultiEdit/NotebookEdit, dedup latest-wins)"
```

---

### Task 13: Lift `useTranscriptPoll` to write the `paneTranscript` signal

**Files:**
- Modify: `static/src/split/Transcript.jsx`

- [ ] **Step 1: Refactor the hook.**

Open `static/src/split/Transcript.jsx`. Find `useTranscriptPoll` (around line 18) and replace it with:

```jsx
import { transcriptSeen, paneTranscript } from "../store.js";

// Poll /api/pane/turns while THIS pane is the current selection. Writes
// the response to the shared `paneTranscript` signal (one entry per pid)
// so both TranscriptView (rendered messages) and Sidebar's Files section
// (selected pane's messages) read from the same store. Also flips
// transcriptSeen[pid] on first non-empty response — load-bearing for the
// auto-promote toggle (see computeMode in Detail.jsx). Eviction lives in
// Detail.jsx's openedTr pruning path.
function useTranscriptPoll(target, pid, selected) {
  useEffect(() => {
    if (!selected || !target) return;
    let alive = true;
    let timer = null;
    async function tick() {
      try {
        const res = await fetch(`/api/pane/turns?${targetQuery(target)}`);
        const data = await res.json();
        if (!alive) return;
        const msgs = data && data.turns === null ? [] : (data.messages || []);
        const sessionId = data?.session_id || null;
        paneTranscript.value = {
          ...paneTranscript.value,
          [pid]: { messages: msgs, sessionId },
        };
        if (msgs.length && !transcriptSeen.value[pid]) {
          transcriptSeen.value = { ...transcriptSeen.value, [pid]: true };
        }
      } catch (_) {
        /* transient; the next tick retries */
      }
      if (alive) timer = setTimeout(tick, TURNS_POLL_MS);
    }
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [target, pid, selected]);
  // No return — consumers read from the signal directly.
}
```

- [ ] **Step 2: Update `<TranscriptView>` to read from the signal.**

Find the line in `<TranscriptView>` (it's likely the function that calls `useTranscriptPoll`) where messages are consumed for rendering. Currently the hook returns `messages`. Change the consumer to:

```jsx
import { paneTranscript } from "../store.js";
// ...inside <TranscriptView>:
  useTranscriptPoll(target, pid, selected);
  const messages = paneTranscript.value[pid]?.messages || [];
```

(Search for `useTranscriptPoll(` and `const messages =` to find the spot. The exact line will be near the existing render logic.)

- [ ] **Step 3: Build + browser-verify.**

```bash
npm run build
```

Open the dashboard, select a Claude pane in split view. Expected: transcript renders identically to before; Cmd+R force-reload also renders correctly.

- [ ] **Step 4: Commit.**

```bash
git add static/src/split/Transcript.jsx static/dist/app.js
git commit -m "transcript: lift messages to paneTranscript signal (one poll, two readers)"
```

---

### Task 14: Evict `paneTranscript[pid]` in Detail.jsx pruning path

**Files:**
- Modify: `static/src/split/Detail.jsx`

- [ ] **Step 1: Wire eviction into the existing pruning loop.**

Open `static/src/split/Detail.jsx`. Find the loop that prunes `openedTr` (around line 423-426):

```jsx
  for (const pid of [...openedTr.current]) {
    const isSelected = isPane && paneW?.pid === pid;
    if (!isSelected && !livePids.has(pid)) openedTr.current.delete(pid);
  }
```

Replace with:

```jsx
  for (const pid of [...openedTr.current]) {
    const isSelected = isPane && paneW?.pid === pid;
    if (!isSelected && !livePids.has(pid)) {
      openedTr.current.delete(pid);
      // Evict from the shared transcript store to bound memory across
      // long sessions where many panes have been opened.
      if (paneTranscript.value[pid]) {
        const { [pid]: _drop, ...rest } = paneTranscript.value;
        paneTranscript.value = rest;
      }
    }
  }
```

Add `paneTranscript` to the imports at the top of the file:

```jsx
import { windows, activeTarget, railSelection, transcriptMode, transcriptSeen, paneTranscript } from "../store.js";
```

- [ ] **Step 2: Build + browser-verify.**

```bash
npm run build
```

Open multiple Claude panes, close all but one. Open DevTools console:

```js
console.log(Object.keys(window.__paneTranscriptDebug || {}));
```

(There's no debug hook; instead verify via behavior — transcripts of closed panes don't keep updating.)

- [ ] **Step 3: Commit.**

```bash
git add static/src/split/Detail.jsx static/dist/app.js
git commit -m "detail: evict paneTranscript[pid] when transcript host is pruned"
```

---

### Task 15: Sidebar Files section (reads `paneTranscript`, opens preview on click)

**Files:**
- Modify: `static/src/sidebar/Sidebar.jsx`
- Modify: `static/styles.css`

- [ ] **Step 1: Add the Files section component.**

Open `static/src/sidebar/Sidebar.jsx`. Add imports at the top:

```jsx
import { paneTranscript, transcriptSeen, previewPath } from "../store.js";
import { filesTouched } from "../split/filesTouched.js";
```

Add a new component just above the `Sidebar` export (around line 307):

```jsx
function FilesSection({ pid }) {
  if (!pid || !transcriptSeen.value[pid]) return null;
  const entry = paneTranscript.value[pid];
  if (!entry || !entry.messages) return null;
  const items = filesTouched(entry.messages);
  if (!items.length) return null;
  return (
    <section class="modal-side-section modal-side-files">
      <h4>Files</h4>
      <ul class="files-list">
        {items.map((it) => (
          <li
            key={it.path}
            class="files-row"
            onClick={() => { previewPath.value = { path: it.path, line: null }; }}
            title={`Open ${it.path} in preview overlay`}
          >
            <span class="files-op">{opGlyph(it.op)}</span>
            <span class="files-path">{it.path}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function opGlyph(op) {
  switch (op) {
    case "Read": return "👁";
    case "Write": return "+";
    case "Edit":
    case "MultiEdit":
    case "NotebookEdit": return "✎";
    default: return "·";
  }
}
```

- [ ] **Step 2: Insert `<FilesSection>` into `<Sidebar>` between Notes and Activity.**

Find the existing `<Sidebar>` return (around line 340). Insert `<FilesSection pid={pid} />` between the Notes and Activity sections:

```jsx
      <section class="modal-side-section modal-side-notes">
        <h4>Notes</h4>
        <NotesEditor key={pid} pid={pid} idPrefix={idPrefix} onRefresh={onRefresh} />
      </section>
      <FilesSection pid={pid} />
      <section class="modal-side-section modal-side-activity">
        <h4>Activity</h4>
        <ActivitySection data={data} streamRef={streamRef} />
      </section>
```

- [ ] **Step 3: Add CSS for the Files section.**

Append to `static/styles.css`:

```css
.modal-side-files .files-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid var(--line-soft);
  border-radius: 4px;
}
.files-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  cursor: pointer;
  font-family: var(--mono, "SF Mono", "JetBrains Mono", monospace);
  font-size: 12px;
  color: var(--fg-2, #c5c8c6);
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.files-row:last-child { border-bottom: none; }
.files-row:hover { background: rgba(255,255,255,0.04); color: var(--fg-1, #e6edf3); }
.files-op {
  flex: 0 0 16px;
  text-align: center;
  opacity: 0.8;
}
.files-path {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

- [ ] **Step 4: Build + browser-verify.**

```bash
npm run build
```

Open a Claude pane in split view. Expected: "Files" section appears in the sidebar (between Notes and Activity) listing files Claude has Read/Edited/Written. Clicking a row sets `previewPath` (no UI feedback yet — overlay lands in Task 17).

- [ ] **Step 5: Commit.**

```bash
git add static/src/sidebar/Sidebar.jsx static/styles.css static/dist/app.js
git commit -m "sidebar: Files section reads paneTranscript via filesTouched selector; click sets previewPath"
```

---

### Task 16: PaneHeader cwd breadcrumb + reveal click

**Files:**
- Modify: `static/src/split/Detail.jsx`

- [ ] **Step 1: Extend `PaneHeader` with a cwd segment.**

Open `static/src/split/Detail.jsx`. Find `<PaneHeader>` (around line 71). Find the section that pushes the session name segment (around line 111):

```jsx
    <span><b>{w.session || ""}</b></span>,
```

Add immediately after (within the `parts.push` chain — match the existing `if (w.branch) { parts.push(...) }` style):

```jsx
  if (w.cwd) {
    const parts2 = w.cwd.split("/").filter(Boolean);
    const tail = parts2.length >= 2 ? parts2.slice(-2).join("/") : (parts2[0] || w.cwd);
    parts.push(
      <>
        <span class="hsep">·</span>
        <span
          class="header-cwd-reveal"
          title={`Reveal ${w.cwd} in Finder`}
          onClick={async () => {
            try {
              await fetch(
                `/api/fs/open?session=${encodeURIComponent(w.session)}&index=${w.index}&path=.&action=reveal`,
                { method: "POST" },
              );
            } catch (_) { /* best-effort */ }
          }}
        >{tail}</span>
      </>
    );
  }
```

- [ ] **Step 2: Add CSS for `.header-cwd-reveal`.**

Append to `static/styles.css`:

```css
.header-cwd-reveal {
  cursor: pointer;
  text-decoration: underline dotted rgba(255,255,255,0.3);
  text-underline-offset: 3px;
}
.header-cwd-reveal:hover {
  color: var(--fg-1, #e6edf3);
  text-decoration-color: rgba(255,255,255,0.6);
}
```

- [ ] **Step 3: Build + browser-verify.**

```bash
npm run build
```

Open a Claude pane in split view. Expected: header shows `…session · dev/periscope · branch · ...`. Click `dev/periscope` → Finder opens to the pane's cwd.

- [ ] **Step 4: Commit.**

```bash
git add static/src/split/Detail.jsx static/styles.css static/dist/app.js
git commit -m "header: cwd-tail breadcrumb segment, click → Finder reveal via /api/fs/open"
```

---

### Task 17: PreviewOverlay component with CodeMirror 6

**Files:**
- Modify: `package.json` (CodeMirror 6 deps)
- Create: `static/src/preview/PreviewOverlay.jsx`
- Modify: `static/src/split/Detail.jsx` (mount the overlay)
- Modify: `static/styles.css`

- [ ] **Step 1: Add CodeMirror 6 packages.**

```bash
cd /Users/tom/dev/periscope
npm install --save \
  @codemirror/state@^6.4.0 \
  @codemirror/view@^6.34.0 \
  @codemirror/language@^6.10.0 \
  @codemirror/commands@^6.6.0 \
  @codemirror/lang-javascript@^6.2.0 \
  @codemirror/lang-python@^6.1.0 \
  @codemirror/lang-markdown@^6.3.0 \
  @codemirror/lang-html@^6.4.0 \
  @codemirror/lang-css@^6.3.0 \
  @codemirror/lang-json@^6.0.0 \
  @codemirror/lang-rust@^6.0.0
```

- [ ] **Step 2: Create the PreviewOverlay component.**

```jsx
// File preview overlay — CodeMirror 6 read-only.
//
// Entry points (all set previewPath.value = {path, line | null}):
//   1. Terminal Cmd+click on a path  (terminalCore link router → file handler)
//   2. Transcript tool-call file_path chip click
//   3. Sidebar Files row click
//
// Re-opening on a new path while the overlay is showing re-initializes
// CodeMirror in place (simple replace, no animated transition).
//
// Esc dismiss is via the shared useEscape LIFO. CodeMirror read-only does
// NOT autograb focus, so focus moves to the close button on open — keeps
// keystrokes from reaching xterm and gives Esc a target that bubbles to
// useEscape correctly.
//
// Visible-while-mounted: the overlay floats over .detail-pane-body (over
// terminal OR transcript content). The underlying terminal NEVER resizes
// (invariant: no tmux reflow on preview open).
import { useEffect, useRef, useState } from "preact/hooks";
import { previewPath, activeTarget } from "../store.js";
import { useEscape } from "../hooks/useEscape.js";

import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, lineNumbers, highlightActiveLineGutter, drawSelection } from "@codemirror/view";
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching } from "@codemirror/language";

import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { rust } from "@codemirror/lang-rust";

function languageExt(name) {
  switch (name) {
    case "javascript": return javascript();
    case "python": return python();
    case "markdown": return markdown();
    case "html": return html();
    case "css": return css();
    case "json": return json();
    case "rust": return rust();
    default: return null;
  }
}

export function PreviewOverlay() {
  const cur = previewPath.value;
  if (!cur) return null;
  return <PreviewOverlayInner key={cur.path} entry={cur} />;
}

function PreviewOverlayInner({ entry }) {
  const hostRef = useRef(null);
  const closeBtnRef = useRef(null);
  const [state, setState] = useState({ loading: true, error: null, content: null, lang: null, resolved: null });

  function close() { previewPath.value = null; }
  useEscape(close, true);

  // Fetch the file.
  useEffect(() => {
    let alive = true;
    async function load() {
      const t = activeTarget.value;
      if (!t) {
        setState({ loading: false, error: "no active pane", content: null, lang: null, resolved: null });
        return;
      }
      const [session, indexStr] = t.split(":");
      const params = new URLSearchParams({
        session, index: indexStr, path: entry.path,
      });
      try {
        const res = await fetch(`/api/fs/read?${params.toString()}`);
        if (!alive) return;
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setState({ loading: false, error: body.detail || `HTTP ${res.status}`, errorCode: res.status, content: null, lang: null, resolved: null });
          return;
        }
        const data = await res.json();
        setState({ loading: false, error: null, content: data.content, lang: data.language, resolved: data.path });
      } catch (e) {
        if (alive) setState({ loading: false, error: String(e), content: null, lang: null, resolved: null });
      }
    }
    load();
    return () => { alive = false; };
  }, [entry.path]);

  // Mount CodeMirror once content lands.
  useEffect(() => {
    if (state.loading || state.error || !hostRef.current || state.content == null) return;
    const langExt = languageExt(state.lang);
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: state.content,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          drawSelection(),
          bracketMatching(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          ...(langExt ? [langExt] : []),
        ],
      }),
    });
    // :NN line jump — EditorView.scrollIntoView returns a state effect.
    if (entry.line) {
      const lineNo = Number(entry.line);
      if (Number.isFinite(lineNo) && lineNo > 0) {
        const line = view.state.doc.line(Math.min(lineNo, view.state.doc.lines));
        view.dispatch({
          selection: { anchor: line.from, head: line.from },
          effects: EditorView.scrollIntoView(line.from, { y: "center" }),
        });
      }
    }
    return () => view.destroy();
  }, [state.loading, state.error, state.content, state.lang]);

  // Focus the close button on mount so keystrokes don't reach xterm.
  useEffect(() => {
    closeBtnRef.current?.focus();
  }, []);

  async function reveal() {
    const t = activeTarget.value;
    if (!t) return;
    const [session, indexStr] = t.split(":");
    const params = new URLSearchParams({
      session, index: indexStr, path: entry.path, action: "reveal",
    });
    try { await fetch(`/api/fs/open?${params.toString()}`, { method: "POST" }); }
    catch (_) {}
  }

  return (
    <div class="preview-overlay" role="dialog" aria-label="File preview">
      <header class="preview-header">
        <span class="preview-path">{state.resolved || entry.path}{entry.line ? `:${entry.line}` : ""}</span>
        <button class="preview-btn" title="Reveal in Finder" onClick={reveal}>⌖</button>
        <button class="preview-btn" title="Close (Esc)" onClick={close} ref={closeBtnRef}>✕</button>
      </header>
      <div class="preview-body">
        {state.loading && <div class="preview-loading">loading…</div>}
        {state.error && (
          <div class="preview-error">
            <div>{state.error}</div>
            {state.errorCode && <div class="preview-error-code">HTTP {state.errorCode}</div>}
            {state.errorCode === 415 && (
              <button class="preview-btn-large" onClick={reveal}>Open in Finder</button>
            )}
          </div>
        )}
        {!state.loading && !state.error && <div ref={hostRef} class="preview-cm-host" />}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Mount `<PreviewOverlay>` in `<Detail>`.**

Open `static/src/split/Detail.jsx`. Add the import at the top:

```jsx
import { PreviewOverlay } from "../preview/PreviewOverlay.jsx";
```

Find the main `<Detail>` return (around line 429: `return (<section id="detail">…`). Add `<PreviewOverlay />` just before the closing `</section>`:

```jsx
      {trPids.map((pid) => {
        // ...existing transcript-host map...
      })}
      <PreviewOverlay />
    </section>
```

- [ ] **Step 4: Add overlay CSS.**

Append to `static/styles.css`:

```css
.preview-overlay {
  position: absolute;
  inset: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  background: var(--bg-0, #1f232b);
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
}
.preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--line-soft);
  font-family: var(--mono, monospace);
  font-size: 12px;
}
.preview-path {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.preview-btn {
  background: transparent;
  border: none;
  color: var(--fg-2, #c5c8c6);
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 14px;
}
.preview-btn:hover { background: rgba(255,255,255,0.06); }
.preview-body {
  flex: 1;
  overflow: hidden;
  position: relative;
}
.preview-cm-host {
  position: absolute;
  inset: 0;
  overflow: auto;
}
.preview-cm-host .cm-editor { height: 100%; }
.preview-loading,
.preview-error {
  padding: 20px;
  font-family: var(--mono, monospace);
  font-size: 13px;
  color: var(--fg-2, #c5c8c6);
}
.preview-error-code { opacity: 0.6; margin-top: 6px; }
.preview-btn-large {
  margin-top: 12px;
  padding: 6px 12px;
  background: rgba(255,255,255,0.08);
  border: 1px solid var(--line-soft);
  color: inherit;
  border-radius: 4px;
  cursor: pointer;
}
```

The `.preview-overlay` is positioned `inset: 0` relative to its closest positioned ancestor. Ensure `.detail-pane-body` has `position: relative` — find it in `static/styles.css` and add `position: relative;` if it lacks one:

```css
.detail-pane-body {
  /* ...existing... */
  position: relative;
}
```

- [ ] **Step 5: Build + browser-verify.**

```bash
npm run build
```

Open a Claude pane in split view; in the Sidebar's Files section, click a row. Expected: preview overlay appears over the terminal/transcript area, file rendered with line numbers + syntax highlighting. Esc closes. `⌖` reveals in Finder. Try a binary file (any `.png` in the touched list — should show the 415 "binary file" message + Open-in-Finder button).

- [ ] **Step 6: Commit.**

```bash
git add package.json package-lock.json static/src/preview/PreviewOverlay.jsx static/src/split/Detail.jsx static/styles.css static/dist/app.js
git commit -m "preview: CodeMirror 6 read-only overlay (sidebar entry), Esc + Reveal"
```

---

### Task 18: Wire the terminal click dispatcher's file handler

**Files:**
- Modify: `static/src/split/Detail.jsx`

- [ ] **Step 1: Register the file handler when the pane mounts.**

Open `static/src/split/Detail.jsx`. Add the import:

```jsx
import { setTerminalFileCallback } from "../terminal/terminalCore.js";
import { previewPath } from "../store.js";
```

In `<PaneDetail>` (the function), add a `useEffect` right next to the existing `activeTarget` effect:

```jsx
  // Wire the terminal's file-link callback to the preview overlay.
  // Set on mount, cleared on unmount. Cmd+click on a path in the
  // terminal → previewPath.value → overlay opens.
  useEffect(() => {
    setTerminalFileCallback((rawPath) => {
      // Split off optional :NN line suffix for the line jump.
      let path = rawPath;
      let line = null;
      const m = path.match(/^(.*?):(\d+)$/);
      if (m) { path = m[1]; line = m[2]; }
      previewPath.value = { path, line };
    });
    return () => setTerminalFileCallback(null);
  }, [w.target]);
```

- [ ] **Step 2: Build + browser-verify.**

```bash
npm run build
```

In a Claude pane's terminal, find a file path in scrollback (e.g. one Claude just edited). Cmd+click. Expected: preview overlay opens to that file.

- [ ] **Step 3: Commit.**

```bash
git add static/src/split/Detail.jsx static/dist/app.js
git commit -m "detail: wire setTerminalFileCallback → previewPath (Cmd+click in terminal opens preview)"
```

---

### Task 19: Transcript tool-call file_path click handler

**Files:**
- Modify: `static/src/split/Transcript.jsx`

- [ ] **Step 1: Make tool-call file_path args clickable.**

Open `static/src/split/Transcript.jsx`. Find the `ToolCall` component (around line 95). Find the place where the tool's `arg` is rendered — it's the `<span class="tc-arg">` around the result of `toolArg(t)`.

Replace that span with a click-aware version that triggers `previewPath` for file-tool kinds. Look for the existing render line (something like `<span class="tc-arg">{toolArg(t)}</span>` — exact text may vary). Replace with:

```jsx
{(() => {
  const isFileTool =
    t.name === "Read" || t.name === "Edit" || t.name === "Write" ||
    t.name === "MultiEdit" || t.name === "NotebookEdit";
  const arg = toolArg(t);
  if (!isFileTool || !arg) {
    return <span class="tc-arg">{arg}</span>;
  }
  return (
    <span
      class="tc-arg tc-arg-clickable"
      title="Open preview"
      onClick={(e) => {
        e.stopPropagation();
        previewPath.value = { path: arg, line: null };
      }}
    >{arg}</span>
  );
})()}
```

Add the import at the top:

```jsx
import { transcriptSeen, paneTranscript, previewPath } from "../store.js";
```

Append CSS to `static/styles.css`:

```css
.tc-arg-clickable {
  cursor: pointer;
  text-decoration: underline dotted rgba(255,255,255,0.3);
  text-underline-offset: 2px;
}
.tc-arg-clickable:hover {
  text-decoration-color: rgba(255,255,255,0.7);
}
```

- [ ] **Step 2: Build + browser-verify.**

```bash
npm run build
```

Open a Claude pane in transcript mode. Find an Edit/Read/Write row. Click the path arg. Expected: preview overlay opens. Bash/Grep rows are not clickable.

- [ ] **Step 3: Commit.**

```bash
git add static/src/split/Transcript.jsx static/styles.css static/dist/app.js
git commit -m "transcript: file_path tool-call args open preview overlay on click"
```

---

## Final smoke pass

### Task 20: End-to-end manual verification + final commit

- [ ] **Step 1: Run the full pytest suite.**

```bash
cd /Users/tom/dev/periscope
uv run pytest -q
```

Expected: all tests pass. No new failures.

- [ ] **Step 2: Run vitest.**

```bash
npm test
```

Expected: filesTouched tests pass.

- [ ] **Step 3: Rebuild bundle.**

```bash
npm run build
```

- [ ] **Step 4: Manual smoke checklist (open a real Claude pane in dev periscope).**

Verify each:
- [ ] WebGL renderer active (no error in console); fallback to canvas works if WebGL is disabled
- [ ] Cmd+F opens the search bar, Enter / Shift+Enter cycle matches, Esc closes
- [ ] Cmd+click on a URL in terminal opens new tab
- [ ] Cmd+click on a file path in terminal opens preview overlay
- [ ] Cmd+click on a `.md` path in an LGTM-active pane still adds the doc (regression check)
- [ ] BEL flashes the terminal border
- [ ] Claude working → idle transition flashes the border
- [ ] Sidebar "Files" section lists touched files; click opens preview
- [ ] PaneHeader shows cwd-tail; click reveals in Finder
- [ ] Transcript file_path args open preview on click
- [ ] Preview overlay: Esc closes; ⌖ reveals; binary file shows 415 message with Open-in-Finder
- [ ] Switching panes during open preview gracefully transitions

- [ ] **Step 5: If anything in the smoke pass fails, fix and commit the fix, then re-run the checklist.**

- [ ] **Step 6: Final summary commit (if any small fixes landed).**

If the manual smoke needed a fix, commit it with a clear message describing what was discovered:

```bash
git add -p   # stage the fix specifically
git commit -m "polish: <one-line description of fix from smoke checklist>"
```

---

## Spec coverage map

| Spec section | Tasks |
|---|---|
| WebGL renderer | T1 |
| Search addon | T2, T5 |
| Theme module | T3 |
| Click dispatcher router | T4 |
| Visual bell + idle ping | T6 |
| Padding + cursor + ligatures | T3 |
| `periscope/fs.py` pure | T7, T8 |
| `_for_pane` wrappers | T9 |
| `/api/fs/read` + `/api/fs/open` routes | T10 |
| `paneTranscript` + `previewPath` signals | T11 |
| `filesTouched` selector | T12 |
| Lifted poll into shared signal | T13 |
| Eviction on prune | T14 |
| Sidebar Files section | T15 |
| PaneHeader cwd breadcrumb | T16 |
| CodeMirror 6 PreviewOverlay | T17 |
| Terminal Cmd+click → preview | T18 |
| Transcript chip → preview | T19 |
| Smoke pass | T20 |
| Invariant "preview never resizes terminal" | preserved by T17 CSS (absolute over body) |
| Invariant "fs.py is sole filesystem seam" | preserved by T8 / T10 (no other routes touch fs) |
| Invariant "click dispatcher is a pure router" | preserved by T4 (router has no fs/lgtm side effects) |
| Invariant "filesTouched is pure derivation" | preserved by T12 (pure function) |
| Invariant "CodeMirror starts and stays read-only in v1" | preserved by T17 (`EditorState.readOnly.of(true)`) |

---

## Out of scope (per the spec)

These are intentionally not implemented; do not add them in the smoke-fix pass:

- In-app editing (CodeMirror flips to writable in a follow-up spec)
- `tmux -CC` control-mode migration
- OSC 8 hyperlinks
- Image protocols (Kitty / sixel)
- Configurable theme / font UI
- Quick-open (Cmd+P) over the repo
- Drag-drop file handling
- Cross-pane scrollback search
- Composer / auto-scroll (already shipped before this plan)
- Linux / Windows portability of reveal-in-Finder
