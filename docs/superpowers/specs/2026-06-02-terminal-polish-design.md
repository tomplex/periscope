# Terminal Polish — Ghostty-feel + Filesystem Integration — Design Spec

**Date:** 2026-06-02
**Status:** revised for landed transcript UI, ready for spec-reviewer
**Author:** Tom + Claude (brainstorm)

> **Revision note (2026-06-02, post-transcript merge).** The segmented
> transcript shipped between the first draft and now (commits
> `13fdb7a` → `8ed9729`). Module placements, data flow, and a few
> assumptions changed. This revision applies six fixes against the
> landed reality: module paths, files-touched data source, cwd
> resolution pattern, preview-overlay entry points, modal incidental
> landing, and the composer-already-shipped strike.

---

## Summary

Make periscope's split-view terminal — `<Detail>` plus the shared
`<Terminal>` leaf — feel like a polished native terminal app (Ghostty-grade
rendering, search, theme, visual bell), and wrap it in a **file-context
shell** that turns paths in the buffer into live UI: Cmd+click any path →
floating file-preview overlay, "Files Claude has touched" panel in the
detail sidebar, cwd/git breadcrumb above the terminal, and a small safe
filesystem API on the server.

The use-case driving this is **Claude oversight**: with the segmented
transcript taking over the structured render, the live terminal becomes
the forensic / intervention view — where you go to watch real bytes,
search long output, and click into the files Claude is touching. This
spec sharpens that view.

The grid-view modal is in deprecation and **out of scope for the
file-context shell.** The terminal-rendering polish (WebGL, search,
theme, dispatcher) lives in `<Terminal>` and lands incidentally wherever
it's mounted, including the modal — but no new modal UI is built.

## Goals

- **Live terminal feels native.** WebGL rendering, configurable cursor
  style with subtle blink, generous internal padding, ligature opt-in
  via `fontFeatureSettings`. No theme-config UI — one well-tuned
  default.
- **Find anything in scrollback.** Cmd+F opens an in-frame search bar;
  match-count, prev/next, case-sensitive toggle, Esc closes.
- **Every path is clickable.** The existing `.md → LGTM` link provider
  generalizes into a click-dispatcher router: URL → `window.open`, file
  path → file-preview overlay, `.md` (when an LGTM session exists for
  this cwd) → LGTM-add (existing behavior preserved).
- **Files-touched is a panel.** A new section in the detail sidebar
  lists files Claude has Read / Edited / Written this session,
  most-recent first, click → preview. Data is filtered from the
  segmented-transcript parser; no new server-side state.
- **File preview as floating overlay.** Cmd+click a path or click a
  files-touched row → CodeMirror 6 read-only renders the file over the
  terminal area. Esc dismisses. Doesn't resize the terminal (no tmux
  reflow). Reveal-in-Finder button in the overlay header. No
  "Open in editor" — see Trajectory.
- **Cwd + git breadcrumb is always-visible.** Extend the existing
  `<PaneHeader>` with a cwd segment (and refine the git/PR layout). The
  goal is "you can see at a glance which working tree you're inside."
- **Visual bell / idle ping.** Terminal frame border pulses once on
  `\x07` (BEL) and once on `working → idle` transitions for
  Claude panes.
- **Safe filesystem API.** One new module — `periscope/fs.py` — is the
  only filesystem access seam. Safe-path resolver scoped to a pane's
  cwd; refuses `..` escape, enforces a max file size, returns clean
  HTTP errors.

## Non-goals

- **In-app editing.** CodeMirror 6 is mounted read-only on purpose. Edit
  mode + `POST /api/fs/write` is a follow-up spec; this one positions
  for it (the read-only renderer becomes editable by flipping a flag)
  but does not deliver it.
- **Shell-out to external editors.** No `code <path>` button. The
  trajectory is in-app, not "open VS Code." Reveal-in-Finder stays
  because it's a fundamentally different operation (locate, not
  edit).
- **Modal polish.** Grid-view modal is being deprecated. New UI
  (file-context shell) only lands in `<Detail>`. Polish that lives in
  `<Terminal>` (WebGL, search, theme, click dispatcher) reaches the
  modal incidentally and that's fine.
- **tmux control mode migration.** Replacing pipe-pane FIFO with
  `tmux -CC` is the obvious next architecture step but is its own
  multi-week project. Flagged in *Future*.
- **Image protocols (Kitty / sixel).** Claude doesn't emit them;
  xterm.js doesn't support them.
- **OSC 8 hyperlinks.** Universal Cmd+click on raw paths covers the
  90% case. OSC 8 is a future-easy add but not pursued here.
- **Cross-pane scrollback search.** Separate project.
- **Configurable theme / font UI.** No SettingsModal entries; better
  hardcoded defaults only.
- **Linux / Windows portability of reveal-in-Finder.** Periscope is
  macOS-only today (launchd integration, Tauri shell, `open -R`
  shell-out); cross-platform support is out of scope. New code
  should not pretend otherwise.

## Existing invariants — audit

Each terminal-related invariant in `CLAUDE.md` is re-examined here
explicitly. None are blindly inherited.

**1. `focused_at` is server-tracked, not tmux's `window_activity`.**
   Still applies; orthogonal to this spec. Polish does not introduce
   any new focus-tracking surface.

**2. Claude detection requires status line in last 4 non-empty lines.**
   Still applies; orthogonal. The polish doesn't touch `parse_pane`.

**3. WebSocket initial paint mirrors tmux's screen state (size, cursor,
   alt-screen).** Still applies. The pipe-pane FIFO + capture-pane
   bootstrap pattern is unchanged; the prefix/body/suffix dance in
   `ws.py` remains the contract. WebGL renderer + theme changes are
   client-side after the initial paint lands. Caveat: the preview
   overlay obscures terminal content (including the inline
   `[periscope: reconnecting…]` notice written by `terminalCore.js`
   on WS drop). Acceptable — reconnects are silent on success and
   the overlay is always user-dismissed; the user will see the
   notice on close if anything was wrong.

**4. `capture-pane` separates rows with bare `\n`; xterm needs `\r\n`.**
   Still applies. Pure correctness invariant of the existing
   architecture.

**5. Multi-line input goes via tmux paste-buffer, then Enter.** Still
   applies; orthogonal. The polish does not introduce a new input
   pathway (image-paste handler stays in `<Detail>`'s onPaste).

**6. Session/index are query params, not path segments.** Still
   applies. New `/api/fs/*` routes use query params (`session`,
   `index`, `path`) — same convention.

**7. Spinner has hysteresis at the data layer.** Still applies;
   orthogonal. Visual bell is a frame-border pulse, not a buffer-line
   mutation, so it can't disturb spinner smoothing.

**8. Background-thread crashes must surface (`_bg` / `_task`).** Still
   applies. New filesystem reads are synchronous handler bodies
   (FastAPI threadpool) — no new background tasks introduced.

**9. Pidfile reclaim treats reloader-child as the same instance.**
   Orthogonal.

**10. `channel_shim.py` exits 0 on every failure.** Orthogonal.

## New invariants this spec introduces

These are the constraints that make the new code safe to evolve. State
them once here so the implementation plan and future readers
understand the contract.

- **`periscope/fs.py` is the sole filesystem-access seam.** No other
  module reads files on behalf of the client. New filesystem-touching
  endpoints route through `fs.py`'s `safe_read(target, path)` helper.
  If a future feature needs filesystem access, it imports from
  `fs.py`, it does not roll its own.

- **The file-preview overlay never resizes the terminal.** Tmux
  reflows are expensive and mangle TUIs (Claude's tables, Ink
  frames) for a frame. The preview floats over the terminal area;
  the underlying terminal keeps its dimensions exactly. CSS:
  `position: absolute` on a sibling of `.detail-xterm`, not a
  layout-flow sibling.

- **The click dispatcher in `terminalCore.js` is a pure router.** It
  examines a clicked link's text and routes to one handler (file
  preview / URL open / LGTM add); it does not itself touch
  filesystem or LGTM. Handlers are registered by the parent
  (`<Detail>` for preview, the LGTM hook for `.md`). Routing
  precedence is deterministic and documented in code:
  1. Absolute URL → `window.open`.
  2. `.md` AND LGTM session exists for this cwd → LGTM add.
  3. File path → preview overlay.

  Note: xterm's `registerLinkProvider` supports N providers — this
  single-routing-provider choice is style, not a workaround. One
  provider keeps regex passes minimal (one per row) and routing
  decisions in a single readable place.

- **Files-touched is a pure derivation from JSONL.** No server-side
  state. The parser already running for segmented transcript
  produces tool-call events; the detail sidebar filters them into
  the panel. Refreshes happen as part of the same polling cycle.

- **CodeMirror 6 starts read-only and stays read-only in this spec.**
  Edit mode lives in a future spec. The choice of CodeMirror over
  shiki is *because* it makes the future edit path a config-flag
  flip — not because we need edit features now.

## Architecture

Two independent halves; either can ship first.

### Half A — terminal rendering polish

Lives entirely in `static/src/terminal/`. No server changes.

**WebGL renderer.** Add the `@xterm/addon-webgl` script alongside the
existing vendored `xterm.js` + `addon-fit.js` in `static/vendor/`. In
`startLiveTerminal()`, after `term.open(containerEl)`, try
`term.loadAddon(new WebglAddon.WebglAddon())`; on any thrown error,
log and proceed (xterm falls back to canvas). The addon's
`onContextLoss` handler tears it down so we don't render to a dead
context (e.g. browser tab pushed to background and reclaimed).

**Search addon.** Add `@xterm/addon-search` vendored the same way.
`<Terminal>` renders a hidden search bar (`<div class="term-search">`)
above the xterm container; the parent (`<Detail>`) shows/hides it on
Cmd+F via a ref-forwarded `openSearch()` method (or via a
`searchOpen` signal — see *Open Questions*). Match-count, prev/next,
case-sensitive toggle. No regex.

**Theme module.** Pull the inline literal in `terminalCore.js` into
`static/src/terminal/theme.js` — a single exported object. Same shape
xterm expects (`background`, `foreground`, `cursor`, ANSI colors).
Refinements relative to the current theme:

- Sharper cursor (`cursor: "#7aa2f7"`, more saturated; reduces
  blink-against-background mush).
- Cleaner diff red/green (more contrast vs. background).
- `selectionBackground` darkened so selection over Claude's yellow
  status line stays readable.
- Background unchanged (`#282c34`) so existing screenshots and muscle
  memory don't shift.

**Cursor / font / padding.** In `startLiveTerminal()`:
- `cursorStyle: "block"` (was the implicit default; make explicit).
- `cursorBlink: true` (unchanged).
- `fontFeatureSettings: '"liga" 1, "calt" 1'` for ligature support
  when the font has them (SF Mono / JetBrains Mono).
- `padding` is a CSS concern: add `padding: 8px` to `.detail-xterm`
  (and the modal class for parity, since the leaf doesn't know
  which parent it's under).

**Visual bell + idle ping.** Two triggers:
- `\x07` in the byte stream → xterm fires its `onBell` event → we add
  a `.bell` class to the terminal container for 400ms (CSS animation
  pulses the border).
- Claude pane `state` transition from `"working"` → `"idle"` *or*
  `"working"` → `"done"`. **Both states matter**: the raw four-state
  machine is in `periscope/panes.py:_resolve_state` (`shell` /
  `idle` / `working` / `needs-input`), but `periscope/window_view.py`
  lines 120-121 refine `idle → done` when Claude has an
  unacknowledged completion stamp (`completed > acked`). The
  client-side `windows` signal almost always sees `working → done`
  on the normal "Claude just finished a turn" case, and
  `working → idle` only in the rarer un-stamped path. Trigger on
  either. Emitted from `<Detail>`, throttled per-pane.

**Click dispatcher.** Replace the single-purpose
`registerMarkdownLinkProvider` with a routing link provider that:
- Matches URLs (`/(https?|wss?):\/\/\S+/`), absolute file paths
  (`/(?:^|[\s({"'`])(\/[^\s)"']+)/`), and relative paths with a
  known extension (`/\.\.?\/[\w.\-/]+/` plus a small set of
  always-clickable extensions).
- On Cmd/Ctrl+click, calls a routed handler the parent has
  registered (separate setters: `setTerminalUrlHandler`,
  `setTerminalFileHandler`, `setTerminalLinkCallback` retained for
  LGTM `.md`).
- Precedence: URL → file-preview if a file handler is registered →
  LGTM `.md` if a markdown handler is registered.

The regexes are conservative on purpose: prefer false negatives
(don't claim it's a path) over false positives (Cmd+click pops a
useless overlay). Add unit tests for the regex in
`static/src/terminal/__tests__/clickRouter.test.js` (vitest is
already configured per the segmented-transcript spec).

### Half B — file-context shell

Lives in `<Detail>`, the shared `<Sidebar>` component, the existing
`<PaneHeader>`, plus a new `static/src/preview/` module for the
overlay, plus new server code.

**Modal incidental landing.** `<Sidebar>` is shared between
`<Detail>` (split-view) and the grid-view modal — both render the
same `Sidebar` component with their own container id/class. Adding
a "Files" section to `<Sidebar>` therefore lands it in the modal
too. **This is acceptable, not a goal.** The modal stays functional
through its eventual deprecation; we don't add bespoke modal UI
(no preview overlay in the modal, no cwd breadcrumb in the modal
header — those live only in `<Detail>`). Acknowledging it here so
"new section appeared in modal" doesn't read as a regression.

**Files-touched panel.** New stacked section in `<Sidebar>` —
joining the existing Linked / Notes / Activity sections (the
current Sidebar uses `<section>` blocks stacked vertically,
not a tab strip; this spec preserves that). Labeled "Files".
Renders a scrollable list:

```
✎ src/lib/foo.ts
+ tests/test_foo.py
✎ src/lib/foo.ts        (later edit; deduped, latest op wins)
👁 README.md
```

**Source — single poll, two readers.** The transcript view already
polls `/api/pane/turns` every 2s (`useTranscriptPoll` inside
`static/src/split/Transcript.jsx`) and holds `messages` in
component-local state. The Files section needs the same data without
a second 2s poll.

The revision: **lift `messages` to a shared `paneTranscript:
{ [pid]: { messages, sessionId } }` signal in `store.js`,** owned by
a single poll loop hoisted into `<PaneDetail>` (or a hook called
there). `<TranscriptView>` reads its messages from the signal via
prop / direct subscription; the Sidebar's Files section reads the
same. One poll, two consumers, no duplicated network or parser
work.

A new pure selector function `filesTouched(messages)` in
`static/src/split/filesTouched.js` collapses the message stream into
a per-path ordered list with the latest op as the icon.
Trivially testable.

**Tool scope.** The selector reads `input.file_path` from these
tools only: `Read` (👁), `Edit` (✎), `Write` (+), `MultiEdit` (✎),
`NotebookEdit` (✎). Bash invocations are NOT parsed for `rm` / `mv`
/ etc. — Claude's official tool surface has no `Delete`, and
parsing Bash command strings to infer file mutations is brittle.
Deletes-via-Bash are not shown; that's an accepted v1 limitation.

Click on a row → opens the preview overlay for that path.

When no Claude JSONL is resolvable for the pane (shell pane), the
section is hidden — same pattern as the segmented-transcript
auto-promote (driven by `transcriptSeen[pid]`).

**Cwd + git breadcrumb.** Extend `<PaneHeader>` (`Detail.jsx:71`)
with a cwd segment:

```
✨ session · cwd-tail · branch · ✓dirty · #PR ⟳ · linear · model · 87%
```

`cwd-tail` is the cwd path truncated to the last 2-3 segments
(`…/dev/periscope` for `/Users/tom/dev/periscope`). Click → "Reveal
in Finder" via `/api/fs/open?action=reveal&...`. Title attribute
holds the full path.

Data already exists in the pane payload (`w.cwd`); no schema change.

**File-preview overlay.** New component
`static/src/preview/PreviewOverlay.jsx`. Mounted unconditionally in
`<Detail>` (so it can animate in/out without remounting); visibility
driven by a `previewPath` signal in `static/src/store.js`. When non-
null, the overlay covers the `.detail-pane-body` area — *over* the
terminal-or-transcript content, not in flow (see Invariant *never
resizes terminal*).

**Two entry points, one overlay.** The same preview is opened from:
1. **Terminal Cmd+click** on a path (dispatched by the routing link
   provider in `terminalCore.js`).
2. **Transcript tool-call file_path chip** — `<Transcript>` already
   renders `Read("path")`, `Edit("path")`, `Write("path")`, etc. as
   `⏺ Name(arg)` rows. Wrapping the arg in a click handler on
   `{Read, Edit, Write, MultiEdit, NotebookEdit}` opens the same
   overlay. No modifier required for transcript-side clicks (it's a
   direct UI affordance, unlike terminal where Cmd avoids hijacking
   scrollback selection).
3. **Files-touched row click** in the sidebar — third entry point,
   trivially same overlay.

All three set `previewPath.value = {path, line}`; the overlay
doesn't care about origin.

Structure:
```
<div class="preview-overlay">
  <header class="preview-header">
    <span class="preview-path">tests/test_foo.py:42</span>
    <button title="Reveal in Finder">⌖</button>
    <button title="Close (Esc)">✕</button>
  </header>
  <div class="preview-body" ref={cmHostRef} />
</div>
```

The body hosts a CodeMirror 6 view, configured read-only. Language
auto-detected from extension (fall back to plain text). `:N` suffix
scrolls and highlights line N. Escape dismisses (registered in the
existing useEscape stack).

**Error states.** If `/api/fs/read` returns 403 / 404 / 413 / 500,
the overlay shows the error message + status code in place of the
CodeMirror body (small monospace block, same dismiss controls).
Errors are surfaced inline, never silently swallowed.

**Focus + Esc contract.** On overlay open, focus moves to the
overlay container (the close button is the focus target; CodeMirror
read-only views do not autograb focus). This routes keystrokes away
from xterm.

Esc handling already works correctly for the overlay because
`useEscape` (`static/src/hooks/useEscape.js:20`) registers a
**window-level capture-phase** listener — it sees the Esc event
*before* xterm's `attachCustomKeyEventHandler`, regardless of where
DOM focus is. The overlay's `useEscape(...)` registration pushes
onto the LIFO stack and pops dismissed first. No new wiring needed
in `terminalCore.js` for the overlay path.

**What `terminalCore.js` line ~188 IS load-bearing for:** the
"empty stack" case. When no overlay or modal is open, plain Esc
must NOT emit `\x1b` to the pane (that would collapse Claude's Ink
dialogs and confuse the user). The line returns `false` so xterm
skips its default Esc emission. This is the *one* load-bearing job
of that line; the preview overlay does not rely on it. Call this
out in a code comment when implementing — a future "tighten the
Esc handler" refactor that removes the `return false` would break
the empty-stack case (plain Esc would start poking Claude).

CodeMirror 6 deps: `@codemirror/state`, `@codemirror/view`,
`@codemirror/language` for core; `@codemirror/lang-javascript`,
`-python`, `-markdown`, `-html`, `-css`, `-json`, `-rust` for
languages we'll see most often in Tom's panes. Bundled via Vite into
`static/dist/app.js` (no separate chunks for v1; revisit if bundle
gets fat).

Estimated CodeMirror weight: **150-200KB minified + gzipped** for
the curated set above (core ~80-100KB, lang packs ~10-20KB each ×
~7 langs). **Measure during Phase 3.** If the actual delta exceeds
**250KB**, fall back to lazy-loading language packs on first use,
or trim the curated set. Acceptable up to that threshold given the
"never edit, sometimes preview" v1 usage shape.

**Click router wiring.** `<Detail>` registers handlers on mount:
- `setTerminalFileHandler((path, line) => previewPath.value = {path, line})`
- `setTerminalUrlHandler((url) => window.open(url, "_blank", "noopener"))`
- `setTerminalLinkCallback(...)` — existing LGTM `.md` handler (only
  registered when an LGTM session exists for the cwd).

The dispatcher in `terminalCore.js` decides which to call based on
the regex match + handler-registered checks.

### Server half

**`periscope/fs.py`.** Single new module. Exports:

```python
def safe_read(target: str, raw_path: str, max_bytes: int = 1_000_000) -> tuple[str, str]:
    """Read `raw_path` resolved against the pane's cwd.

    Returns (resolved_abs_path, contents).

    Raises:
      HTTPException(400) — path empty, not utf-8 decodable, etc.
      HTTPException(403) — resolved path escapes the safe roots.
      HTTPException(404) — pane unknown or file missing.
      HTTPException(413) — file exceeds max_bytes.
    """

def safe_reveal(target: str, raw_path: str) -> None:
    """Run `open -R <resolved_path>` (macOS reveal-in-Finder)."""
```

**Resolution rules.** `target` is the standard `session:index` tmux
target.

Cwd lookup: **server resolves cwd via one `tmux display-message`
call**, same pattern as `periscope/turns.py:get_turns_for_pane`
(`#{pane_current_path}`). One subprocess per `/api/fs/read` request
(~20-80ms) is fine for a click-to-preview interaction — comparable
to the cost the transcript polls already pay every 2 seconds. The
client does not pass cwd as a parameter. This is strictly tighter
than a client-supplied value (no spoofing surface to defend
against), and the pattern is consistent with the rest of the
pane-keyed routes.

`raw_path` is then:
1. If absolute, used directly.
2. If relative, joined against cwd.
3. Tilde-expanded (`~/...`).
4. Realpath'd to resolve symlinks and `..` segments.

Note: `pane_current_path` reflects the cwd of the pane's
foreground process group. While Claude is running, that's Claude's
launch cwd (Claude doesn't `chdir`). If Tom launched Claude from a
non-project directory, `w.cwd` won't be the project root — almost
always still what you want (the launch context), but worth knowing
when debugging a "why does the preview look at the wrong place"
report.

**Safe roots.** The resolved path must be inside one of:
- The pane's cwd (and its descendants).
- The pane's git repo root, if any.
- A small allowlist: `~`, `/tmp`, `/var/tmp`. (Tom occasionally
  pastes paths in `/tmp` from build artifacts.)

Anything else → 403. The check is `os.path.commonpath` based; reject
prefix-only matches (`/foo` vs `/foobar`).

**`periscope/routes/fs.py`.** Two routes:
- `GET /api/fs/read?session=...&index=...&path=...` →
  `{path, content, language}`. `language` is the CodeMirror language
  id ("javascript", "python", etc.), derived from the extension.
- `POST /api/fs/open?session=...&index=...&path=...&action=reveal`
  → `{ok: true}`. Only `action=reveal` is supported in v1 (single
  shell-out path: `open -R <path>` — macOS-only; see Non-goals).
  Returns 400 for unknown action.

Server-side cwd resolution: each route runs one
`tmux display-message -t {session}:{index} -p '#{pane_current_path}'`
to get the pane's cwd before calling `fs.safe_read(target, path)`.
Same pattern as `routes/pane.py:pane_turns` → `turns.get_turns_for_pane`.

Both use safe-path resolution from `fs.py`. Errors follow the
project-wide convention (`raise HTTPException(...)`).

**Binary file handling.** `safe_read` reads the file as bytes and
attempts UTF-8 decode. On `UnicodeDecodeError`, the route returns
**415 Unsupported Media Type** with `{detail: "binary file"}`. The
overlay surfaces this as a "Binary file — open in Finder?"
message with the Reveal button still wired. Image rendering
(inline preview for `.png`/`.jpg`/etc.) is a follow-up; for v1,
click on an image row in files-touched can route directly to
reveal-in-Finder instead of opening the overlay (small UX shortcut,
implementer's call).

Register the router in `periscope/app.py` next to the others.

**Tests.** `tests/test_fs.py`:
- `safe_read` happy path inside cwd.
- `safe_read` happy path inside repo root above cwd.
- `safe_read` 403 outside safe roots.
- `safe_read` 403 on `../` escape attempt.
- `safe_read` 413 on oversize file.
- `safe_read` 404 on missing.
- Tilde expansion.
- Prefix-confusion guard (`/safe-root` vs `/safe-rootless`).

`tests/routes/test_fs.py`:
- 200 happy path with mocked pane cwd.
- 400 on missing/blank path.
- 403, 404, 413 surface from `safe_read`.

## Data flow

### Files-touched

```
JSONL on disk
  → segmented-transcript parser (server)
  → /api/pane/turns (per the segmented-transcript spec)
  → <Detail>'s turns poller in <Sidebar>
  → filesTouched(events) selector
  → "Files" tab renders the list
  → click → previewPath.value = {path, line: null}
  → <PreviewOverlay> mounts CodeMirror + fetches /api/fs/read
```

### Click in terminal

```
xterm link provider runs per-row
  → MD_PATH_RE / URL_RE / FILE_PATH_RE matches
  → underline rendered
  → user Cmd+clicks
  → dispatcher decides URL / .md+LGTM / file
  → calls registered handler:
      - file:  previewPath.value = {path, line}
      - url:   window.open(url, "_blank", "noopener")
      - .md:   existing LGTM add-doc
```

### Preview overlay

```
previewPath signal becomes non-null
  → <PreviewOverlay> shows itself
  → fetches /api/fs/read?path=...
  → builds CodeMirror EditorView, read-only, with language extension
  → if path has :N suffix, scrolls to line N + highlights briefly
  → user hits Esc or close button
  → previewPath.value = null
  → overlay hides; CodeMirror instance destroyed
```

## Sequencing

Phases are independent — either half can ship first; they don't
share code. Suggested order optimizes for impact per hour:

**Phase 1 — Rendering polish (Half A, ~1 day).**
- WebGL addon vendored + loaded with fallback.
- Search addon vendored + Cmd+F bar.
- Theme module extracted + tuned.
- Visual bell / `working → idle|done` ping.
- Click dispatcher router + regex tests.
- **URL handler wired in Phase 1** (`window.open(url, "_blank",
  "noopener")` — no server needed). File handler stays unwired
  until Phase 3; clicking a file path in Phase 1 is a no-op
  (handler not registered).
- Padding + cursor refinements.

Visible after Phase 1: search-in-buffer + WebGL + better theme +
URL Cmd+click. Big felt improvement, zero server changes.

**Phase 2 — Server fs (Half B, server-only, ~half day).**
- `periscope/fs.py` + `periscope/routes/fs.py`.
- Unit tests for safe-path resolver.
- Route tests.

Phase 2 has no visible effect alone — gates Phase 3.

**Phase 3 — File-context shell UI (Half B, client, ~1-1.5 days).**
- `<PreviewOverlay>` + CodeMirror 6 deps.
- Files-touched selector + Sidebar tab.
- PaneHeader cwd breadcrumb.
- Wire dispatcher handlers in `<Detail>`.

Visible after Phase 3: full file-context shell.

**Phase 4 — Modal cleanup (follow-up, not in this spec).**
- Remove grid view + modal entirely (once segmented-transcript is
  the default for Claude panes and the dust has settled).

## Tests

**New unit tests:**
- `static/src/terminal/__tests__/clickRouter.test.js` — regex
  matching + dispatcher precedence.
- `static/src/split/__tests__/filesTouched.test.js` — selector
  collapses messages correctly: dedup + latest-op-wins over
  `{Read, Edit, Write, MultiEdit, NotebookEdit}` only (matches the
  narrowed tool scope in the Files-touched section; no Bash-derived
  delete handling in v1). Path matches the home of the
  segmented-transcript code (`static/src/split/Transcript.jsx`,
  `static/src/split/markdown.jsx`).
- `tests/test_fs.py` — safe-path resolver edge cases.
- `tests/routes/test_fs.py` — endpoint happy paths + error mapping.

**Manual smoke (verification-before-completion):**
- WebGL falls back to canvas when forced off (`?webgl=0` debug flag
  or env var).
- Cmd+F finds a string from 10k lines back without freezing.
- Cmd+click `/abs/path` → preview.
- Cmd+click `./rel/path` in a pane with a known cwd → preview.
- Cmd+click `https://...` → opens in new tab.
- Cmd+click `notes.md` in a pane whose cwd has an LGTM session →
  still does LGTM-add (regression check).
- Preview overlay Esc dismisses without changing terminal cursor.
- Resizing the browser window with overlay open keeps overlay
  centered, doesn't reflow tmux.

## Future / out-of-scope follow-ups

These are explicitly not in this spec but worth recording so
implementation choices align.

- **In-app editing.** Flip `EditorState.readOnly` to false, add a
  save action wired to a new `POST /api/fs/write` route with the
  same `safe_*` gating. Probably also: a small modeline ("modified",
  "saved 2s ago"), Cmd+S binding, dirty-state indicator on the
  files-touched row.

- **tmux control mode migration.** Replace pipe-pane FIFO with
  `tmux -CC` for structured events (`%output`,
  `%session-changed`, `%layout-change`). Cleaner reconnect, real
  OSC 133 framing for the shell-block render, foundation for
  alt-screen state tracking without `display-message` round-trips.

- **OSC 133 prompt markers + per-command blocks.** The
  segmented-transcript framing spec lists this as the shell-block
  half. With control mode, prompt markers become trivially
  available.

- **OSC 8 hyperlinks.** Cheap add once the dispatcher is in place
  (an OSC 8 link is just text-with-URI; the same router can
  consume it).

- **SettingsModal entries for theme / font / cursor.** Once Tom
  actually wants alternatives, this becomes a 1-hour add: schema
  in `prefs.py`, UI knobs in SettingsModal, read in `theme.js`.

- **Cross-pane scrollback search.** Out of scope for this. Would
  index pane scrollback into the existing FTS5 history DB.

## Open questions

These are decisions I want Tom's read on before implementation
starts. Not blockers for the spec itself.

1. **Search-bar wire format.** Two reasonable shapes: (a) a
   `searchOpen` signal in `store.js` plus a `<TerminalSearch>`
   component owned by `<Detail>`, communicating to `terminalCore`
   via setters; (b) ref-forwarded methods on `<Terminal>` so
   `<Detail>` calls `ref.current.openSearch()`. (a) is more
   consistent with existing patterns (signals + setters); (b) is
   less code. Default is (a).

2. **Files-touched scope.** Just the current pane's session, or
   "all files Claude has touched in this worktree (across resumes
   / clears)"? The latter is a richer panel but requires
   cross-session aggregation in the parser. Default is current
   session only.

3. **Preview overlay size.** Floats over the entire `.detail-body`
   area (terminal + sidebar) or just `.detail-xterm` (preserving
   sidebar visibility)? Floating over just the terminal keeps the
   files-touched list visible while you preview — feels right.
   Default is just `.detail-xterm`.

4. **Bell pulse intensity.** A single 400ms border pulse, or a
   double-pulse for `working → idle` (to distinguish from raw
   BEL)? Default single, identical for both — visual bell is "look
   here," differentiating gets cluttered.

5. **Vendored vs npm for xterm addons.** Current xterm.js is
   vendored as a `<script>` tag. WebGL + Search addons are
   available the same way. If we ever want type safety / tree
   shaking, we'd switch all four to npm. Default: vendor the new
   ones (consistent with current pattern), revisit holistically
   later.

6. **Cwd breadcrumb dedup.** When the cwd's last 2 segments
   (`dev/periscope`) duplicate the tmux session name (`periscope`),
   showing both feels noisy. Options: (a) always show both, (b)
   suppress cwd when it's a suffix of session, (c) always show
   just session + a separate cwd icon-tooltip. Default (a) — easy
   to refine later.

   Click-target ambiguity follow-up: only the cwd-tail span
   triggers reveal-in-Finder, never the session span. If (a) lands,
   the cwd-tail span needs its own distinct CSS class
   (`.header-cwd-reveal` or similar) and pointer cursor so the
   click target is unambiguous in hover.
