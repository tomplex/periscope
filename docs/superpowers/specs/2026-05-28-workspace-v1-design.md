# Workspace v1 — design spec

**Date:** 2026-05-28
**Status:** draft, awaiting review
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Turn periscope from a *dashboard over* tmux into a *workspace inside which Tom
actually works*. v1 closes the smallest set of frictions that today force a
context switch to PyCharm or another editor: **find a file in the active pane's
project, open it, edit it, save it.** Plus codebase search to navigate by
content, not just filename.

Concretely: add a fuzzy file picker (`Cmd-P`), a CodeMirror 6-based editor
surface as a new tab in the pane modal, a codebase search overlay
(`Cmd-Shift-F`), and the server endpoints they need. All scoped to the *active
pane's cwd* — there is no global project view yet.

This is also the moment periscope accepts a **production bundle step**. The
"vanilla ES modules served straight out of `static/`" invariant in `CLAUDE.md`
is retired for the editor surface. Vite is promoted from dev-HMR convenience to
producing a real `static/dist/` bundle that production loads. Everything else
in `static/` continues to load as before.

## Goals

- **Find a file fast.** `Cmd-P` inside a pane modal opens a fuzzy file finder
  scoped to that pane's cwd. Selecting a file opens it in the editor.
- **Edit a file fast.** A new **Code** tab on the pane modal renders the open
  file in CodeMirror 6 with syntax highlighting. `Cmd-S` writes to disk.
- **Search the codebase by content.** `Cmd-Shift-F` opens a ripgrep-backed
  search overlay; selecting a hit opens the file at that line.
- **Match the polish floor users have from other editors.** Reasonable syntax
  highlighting, multi-cursor, find/replace within file, line numbers,
  indent-respecting paste.
- **Bounded scope.** v1 is the four bullets above. Everything else is
  explicitly deferred.

## Non-goals

- **No LSP.** No symbol navigation, no rename-across-files, no diagnostics
  inline. Out-of-scope for v1; `pyright`/`ty`/`rust-analyzer` integration is
  the obvious v2 follow-up if it earns its place.
- **No vim mode.** CodeMirror has `@codemirror/vim`; it can land in v2 if Tom
  misses it. v1 stays default-keymap to keep the surface area small.
- **No file tree.** `Cmd-P` *is* the navigation. No persistent tree pane.
- **No multi-file tabs.** One file open at a time per pane modal. If you
  `Cmd-P` to a new file, the current file is replaced (after a dirty-warning
  if unsaved). Tabs may come in v2.
- **No diff viewer in this milestone.** LGTM still owns code review. If LGTM's
  diff viewer is the next-worst pain after this, that's a separate spec.
- **No autosave.** Save is explicit (`Cmd-S` or a Save button).
- **No editing files outside the pane's cwd.** Security and scoping
  simplification: the file finder, content read, and write endpoints all
  reject paths that resolve outside the active pane's cwd. Symlinks resolved
  before the check.
- **No cross-pane editor state.** Closing the modal closes the editor. The
  open-file state is modal-local, not persisted. (See "Open questions" — this
  may bite; we'll see.)
- **No project-scoped editor view.** v1 is per-pane (per-modal) only. A
  top-level workspace view ("open files across all my projects") is a v2
  conversation if v1 lands well.

## Strategic framing

This spec is the inflection point from "periscope is a dashboard over tmux" to
"periscope is the surface in which Tom works." Tom has stopped opening
Ghostty and PyCharm; he uses periscope for everything. The remaining frictions
that force a context switch are concrete (open a file, edit one line, search
the codebase), so the v1 scope is concrete.

What this is *not*: a bet to clone Warp or PyCharm. Periscope's value prop
remains "supervisory dashboard over many Claude sessions." Workspace features
serve that — they make the dashboard a place you stay, instead of a place you
peek at. If at any point a workspace feature degrades the dashboard's
at-a-glance utility, the dashboard wins.

## Architecture

### Bundle step decision

Production now requires `npm run build` before serving. CodeMirror 6 is
ESM-with-deps; it cannot be loaded via plain `<script>` like vendored xterm
can.

Approach: **scoped bundling.** Only the editor and its plugins go through
Vite's production build. The output lives at `static/dist/editor.js` (single
entry, code-split for language packs — see below). All other modules in
`static/` continue to load as plain ES modules straight out of FastAPI's
StaticFiles mount. The existing "vanilla JS, no build step" property is
*narrowed* to "everything outside `static/dist/` is vanilla."

Build entry point: a new `static/src/editor/index.js` that exports a
`mountEditor(container, opts)` function. The Vite config grows a `build:`
section configured with `build.lib` (or a custom rollup input) emitting to
`static/dist/`. Language packs are imported lazily by extension — see
"Frontend: editor module" below.

**Dev mode.** Vite's dev server does *not* emit to `static/dist/` — that's a
production-build-only artifact. Two viable approaches; the spec picks (a):

- (a) **`vite build --watch` alongside `dev.sh`.** `dev.sh` gains a third
  background process: `vite build --watch --emptyOutDir false`. Editor
  source changes trigger a ~100-300ms rebuild; `modal.js` always imports
  `/static/dist/editor.js` regardless of dev vs prod. Simplest, no
  conditional import logic.
- (b) Conditional source-vs-bundle import: `modal.js` checks a runtime
  flag (e.g. a `<meta name="periscope-dev">` tag set only when served by
  Vite's dev server) and imports `/src/editor/index.js` in dev or
  `/static/dist/editor.js` in prod. Faster edit-reload (no build step
  per change) but adds branching.

(a) is the v1 choice. (b) is a follow-up if rebuild latency is felt.

**Production install + restart flow.** `bin/periscope install` gains:

- A `command -v npm` precondition check (same shape as the existing
  `command -v uv` on line 17 of `bin/periscope`); abort with a clear
  error if missing.
- `npm install && npm run build` runs before the launchd plist is
  written, so a fresh checkout produces `static/dist/editor.js` before
  the daemon comes up.
- After a build refresh on an *already-installed* periscope, the user
  must `bin/periscope restart` to pick up the new bundle. FastAPI's
  StaticFiles mount serves the file by path and an mtime change alone
  won't kick uvicorn. Documented in the install section's output.

**Node.js becomes a prerequisite** alongside ripgrep. README's
prerequisites list grows; `CLAUDE.md`'s "vanilla ES modules" invariant is
narrowed to "outside `static/dist/`."

**A fresh checkout that runs `uv run server.py` directly without the build
step** will 404 on `/static/dist/editor.js`. The user-visible degradation
(Code tab shows a "run `npm run build` first" placeholder) lives in
Thread 2's frontend patch; Thread 1 alone leaves the path un-referenced.

Update `README.md` and `CLAUDE.md`:

- README: the quick-start gains `brew install ripgrep node`, plus
  `npm install && npm run build` before first run.
- CLAUDE.md: the "no bundler in production" claim is replaced with "the
  editor bundle is the only build artifact; everything else is plain ES
  modules."

### New server module: `periscope/workspace.py`

Following periscope's one-file-per-subsystem convention. Functions take a
`cwd: Path` directly (the route layer resolves session+index → cwd before
calling in — see "New route module" below). Owns:

- `find_files(cwd, query) -> list[str]` — runs `rg --files` from `cwd`,
  applies fuzzy filtering, returns paths relative to `cwd`, caps at 5000.
- `read_file(cwd, path) -> {content, mtime, encoding, truncated}` —
  normalizes `path` against `cwd`, refuses binary, refuses >10MB.
- `write_file(cwd, path, content, expected_mtime) -> {mtime}` —
  optimistic-lock write; raises a conflict for the route to translate to
  409 if `expected_mtime` is stale.
- `search_codebase(cwd, query, *, max_hits=200) -> {hits, truncated}` —
  runs `rg --json` from `cwd`, parses streamed JSONL, returns up to
  `max_hits`.

**Subprocess invocation, *not* `tmux._run`.** The wrappers in `tmux.py` have
a 3-second timeout and buffer all stdout into a single string (see
`tmux.py:28-35`). Neither suits ripgrep: `rg --files` on a monorepo can
exceed 3s; `rg --json` over a wide pattern can emit tens of MB. `workspace.py`
shells out with `subprocess.Popen(..., stdout=PIPE, bufsize=1, text=True)`,
iterates `proc.stdout` line-by-line, terminates early when the cap is hit,
and raises real errors (not silent `(-1, "")` like `tmux._run` does).
Timeout via `proc.wait(timeout=30)` after stdout closes.

**`rg --json` shape, explicit:** the output is JSONL — one JSON object per
line — with `type` ∈ {`begin`, `match`, `context`, `end`, `summary`}.
`search_codebase` parses line-by-line, filters `type == "match"`, and reads
`data.lines.text` for the matched line. Entries that lack a UTF-8 `text`
(ripgrep emits `data.lines.bytes` as base64 for non-UTF-8 content) are
skipped, contributing to a `truncated` flag if any are seen.

`workspace.py` imports nothing from periscope except `config` (for any path
constants) — cwd is passed in, so it has no dependency on `panes` or
`tmux.py`. Nothing else imports `workspace.py` except the new route module.

### New route module: `periscope/routes/workspace.py`

One APIRouter, four endpoints. Per-pane addressing uses `?session=&index=`
query params to match the existing convention (see `routes/pane.py:32`'s
explicit comment about slash-bearing session names like `tc/foo/bar`
breaking path routing; same rationale applies here):

| Method | Path                       | Query                              | Returns                                   |
|--------|----------------------------|------------------------------------|-------------------------------------------|
| GET    | `/api/workspace/files`     | `session, index, q`                | `{files: [...]}` (capped, fuzzy-filtered) |
| GET    | `/api/workspace/file`      | `session, index, path`             | `{content, mtime, encoding, truncated}`   |
| PUT    | `/api/workspace/file`      | `session, index, path`             | `{mtime}` on success; 409 on stale write  |
| GET    | `/api/workspace/search`    | `session, index, q [, max_hits]`   | `{hits: [...], truncated: bool}`          |

**Pane → cwd resolution.** Each endpoint runs the same one-liner already
used by `routes/pane.py:48-55`:

```python
cwd = tmux("display-message", "-t", f"{session}:{index}",
           "-p", "#{pane_current_path}").strip()
```

`display-message` queries tmux directly and is real-time; there is no
caching layer to lag (this is why Risks §"Pane cwd lags" is retired below).
Errors from `tmux()` (e.g. session/index not found) translate to 404.

Why not `pane_id` (tmux `%N`) in the URL: it does not survive a tmux server
restart, so a cached editor URL or browser back-button could dangle.
`session+index` is human-stable.

**Path-traversal defense.** Every endpoint that accepts a `path` resolves it
via `os.path.realpath()` (full symlink resolution, including parent
components for not-yet-existing files), then compares against
`os.path.realpath(cwd)` using a case-folded prefix check on macOS
(`os.path.realpath(...).lower().startswith(os.path.realpath(cwd).lower() + os.sep)`).
This catches APFS case-insensitivity (`/Users/tom/Dev/...` vs
`/Users/tom/dev/...`) and the `/tmp` → `/private/tmp` symlink. TOCTOU is
acknowledged (file/symlink could be swapped between check and write) but
not mitigated — single-user localhost threat model.

Path errors return real HTTP codes (404 not found, 400 traversal, 409 stale
mtime, 413 file too large). Follows the existing route-error convention
(`raise HTTPException(...)`, never `{"ok": false, ...}`).

### Ripgrep dependency

`rg` is assumed present on the host (`which rg` at server start; log a clear
warning if missing, return 503 from workspace endpoints). It is *not* added
to `pyproject.toml` because it's not a Python dep. The macOS install path is
`brew install ripgrep`. README mentions it under prerequisites.

No vendored alternative for v1 — adding a pure-Python file searcher to the
PEP-723 header is more complexity than telling the user to `brew install rg`.

### Frontend: editor module

New tree under `static/src/editor/`:

```
static/src/editor/
├── index.js              # mountEditor(container, opts) entry — bundled
├── languages.js          # lazy language imports keyed by file extension
└── ...
```

Built by Vite to `static/dist/editor.js`. The existing `static/*.js` modules
import nothing from `static/src/`; they only fetch `static/dist/editor.js`
lazily on Code-tab activation.

`mountEditor(container, { initialContent, language, onSave }) -> EditorAPI`
returns `{ getContent, setContent, focus, isDirty, destroy }`.

Languages bundled in v1: javascript, typescript, jsx, tsx, python, rust, go,
java, json, yaml, markdown, html, css, sh, sql, toml. Anything else falls
back to plain-text. Language-bundle size is the main cost driver; we'll
measure and cut if needed.

### Frontend: new modal tab + overlays

**No tab registry exists today.** Tabs in `modal.js` are hard-coded in
`buildTabSpec` and `ensureStripStructure` (`:182-213`), with `renderReviewPane`
(`:507-525`) dispatching by id. Adding "Code" means concrete touch points:

- `buildTabSpec`: add a `{ id: "code", label: "Code" }` entry between
  Terminal and the LGTM-derived tabs.
- `ensureStripStructure`: create the `#modal-code-content` pane sibling of
  `#modal-review-content`.
- `renderReviewPane` (or a new sibling `renderCodePane`): mount the editor
  on first activation, leave mounted afterward.
- CSS: add a `#modal[data-tab="code"]` rule alongside the existing
  `#modal[data-tab="terminal"]` rule at `:110-113` to hide/show the right
  panel.

Introducing a real tab registry is *not* in v1 scope — the hard-coded
extension above is the minimum surgery. If a v2 tab arrives (e.g. a Diff
tab), revisit then.

**Code tab** — Empty state: "Press Cmd-P to open a file." On first
activation, `await import('/static/dist/editor.js')` runs and the bundle
loads (2-4 MB minified — see Risks §2). Render a skeleton/spinner during
the import; replace with the editor when it resolves. Subsequent
activations are instant (browser caches the module).

**File picker overlay** — `Cmd-P` inside the modal (only when the modal is
open and the editor is the topmost overlay; see "Keybinding capture"
below). Centered overlay with a search input and a results list. Fetches
`?q=<query>` on input change, debounced ~80ms. Returns up to 200 files;
arrow keys navigate, Enter opens. Esc closes via `overlay.js`.

**Codebase search overlay** — `Cmd-Shift-F`. Same shape as the file picker
but each result row shows `path:line — matched line content`. Enter opens
the file at that line.

**Save** — `Cmd-S` in the Code tab. Calls the PUT endpoint with the editor's
content + last-known mtime. On 409, shows a banner: "File changed on disk.
Reload or overwrite?" with two buttons (Reload discards local edits; Overwrite
sends a new PUT without `expected_mtime`).

**Dirty-warning on file switch** — `Cmd-P` selection or close-modal while
`isDirty()` → confirm dialog ("Discard unsaved changes?").

**External-change detection (light)** — on Code-tab focus, fetch
`/api/workspace/file?...&path=<current>` with a fresh request; if mtime
advanced and local is clean, reload silently; if dirty, show the conflict
banner.

### Keybinding capture

`Cmd-P`, `Cmd-S`, and `Cmd-Shift-F` conflict with browser defaults (print,
save-page, find). The existing global-keydown precedent in periscope is
**`static/app.js:27-117`**, which intercepts `Cmd-/`, `/`, `Tab`, arrows,
and Enter — gated on `body.dataset.view`, modal-open state, and the active
element being non-editable. `overlay.js` only handles Escape;
`commands-modal.js` has no document-level listeners.

The new bindings follow `app.js`'s shape, with a sign-flip on the gate:
fire *when* the modal is open AND focus is in the editor (or in no input).
`preventDefault()` on match, returns control to the browser otherwise.

**Tauri:** verified — `src-tauri/src/main.rs:38-46` adds only the standard
Edit menu (undo/redo/cut/copy/paste/select_all) and a View menu (Reload,
Devtools). No `Cmd-P` / `Cmd-S` / `Cmd-Shift-F` menu accelerators are
registered, so Tauri will not intercept these system-wide. The original
Tauri-conflict risk is closed.

### Pane scope, security, and worktree behavior

Cwd is the pane's *current* cwd at request time, fetched fresh from tmux on
each call (see "Pane → cwd resolution" above). For tmux panes inside a
worktree, that worktree is the editor's root.

Switching worktrees mid-session: open file X from a pane in worktree A;
`cd` the pane to a different worktree B; the editor still shows X (the
content was fetched as an absolute path and the editor holds the buffer);
the *next* `Cmd-P` searches B because cwd is re-resolved per request.
That's intentional — the file picker tracks the live cwd; an open buffer
tracks itself. Saving X still writes to the original path (not B) because
the editor stored the path absolutely.

The path-traversal check at write time uses `cwd` *as of the write
request*, so if the pane has moved to B, saving a file that lived under
A's tree returns 400. This is conservative; if it bites, relax the check
to "is the absolute target file inside any known periscope-tracked
worktree" later. Single-user, localhost — the threat model is weak.

## Threads

Each thread is a self-contained PR / commit-sequence. Ship in order.

### Thread 1 — server endpoints + bundling skeleton

*(First commits. No editor UX yet. Verifiable via curl + build artifacts.)*

- `periscope/workspace.py` with the four functions above.
- `periscope/routes/workspace.py` mounting the four endpoints.
- Wired into `periscope/app.py`'s `include_router` loop (adding the
  `workspace` import + tuple entry to the existing pattern at `:99-104`).
- Tests under `tests/test_workspace.py` and `tests/routes/test_workspace.py`
  (mirroring the package convention).
- README + CLAUDE.md updated to document the prerequisites (`brew install
  ripgrep node`) and the build step.
- `package.json` gains CodeMirror 6 core + the language packs listed in
  "Frontend: editor module" as `dependencies`, a `build` script, and
  CodeMirror's lazy-load pattern wired up. Vite config grows a `build:`
  section emitting to `static/dist/editor.js`.
- `bin/periscope install` gains `command -v npm` check + `npm install &&
  npm run build` before launchd plist install.
- `dev.sh` gains a third background process: `vite build --watch
  --emptyOutDir false`.
- **No frontend changes yet** — neither `modal.js` nor anything else
  imports the editor bundle. The Code tab is not added. Visiting
  periscope shows no user-visible difference.

**Done when:**

- `curl` against each endpoint behaves (returns expected shapes; correct
  HTTP codes for missing files / path traversal / oversize / stale
  mtime).
- `npm run build` emits `static/dist/editor.js`; `dev.sh` starts the
  watcher and rebuilds on source change.
- `tests/test_workspace.py` and `tests/routes/test_workspace.py` pass.
- Running `uv run server.py` without `npm run build` still starts cleanly
  (the bundle file is absent but un-referenced; no 404s in the network
  log because nothing fetches it yet).
- `bin/periscope install` aborts with a clear error on a system missing
  `npm`.

### Thread 2 — Code tab + file picker

*(First user-visible workspace feature.)*

- `static/src/editor/index.js` (the `mountEditor` API).
- Code tab in `modal.js` (lazy-loads the editor bundle on first activation).
- File-picker overlay (`Cmd-P`) in a new `static/file-picker.js`, reusing
  `overlay.js`.
- Save (`Cmd-S`) with optimistic-lock + 409 conflict banner.
- Dirty-warning on file switch / modal close.
- External-change detection on tab focus.

**Done when:** Tom can `Cmd-P`, type, pick a file, edit it, save it, in any
pane modal.

### Thread 3 — codebase search

*(Polish thread — same UI shape as file picker.)*

- Search overlay (`Cmd-Shift-F`) in `static/codebase-search.js`.
- Result rows render `path:line — line content`.
- Enter opens the file in the Code tab at that line (uses CodeMirror's
  `EditorView.scrollIntoView`).

**Done when:** Search → result → file open at line works end-to-end.

## Risks & open questions

These are the things most likely to bite. Calling them out so the spec
reviewer + plan can address them deliberately.

1. **Editor + terminal-side editing collide.** Tom can open a file in
   periscope's editor *and* `vim` it in the tmux pane. The light
   external-change detection (mtime check on focus) handles the common case
   but not "edit in both simultaneously." Acceptable for v1 — the dirty
   conflict banner is loud enough. Revisit if it bites.

2. **Bundle size.** CodeMirror 6 core + basicSetup is ~400 KB minified;
   each `@codemirror/lang-*` package adds 30-150 KB plus its Lezer
   grammar (Python's grammar alone is ~500 KB). 16 languages bundled
   together is realistically **2-4 MB minified, 6-10 MB unminified**.
   Mitigation: language packs are lazy-loaded by file extension via
   dynamic `import()` from the editor entry — the initial Code-tab
   activation only pulls CodeMirror core + the language for the first
   file opened. Measure after Thread 1's build and trim the languages
   list if the language-load latency is felt. Loading-state UX is a
   skeleton/spinner on first activation.

3. **`rg --files` on large repos.** A monorepo with 100k+ files is slow
   enough to be felt. v1 ships with a hard cap (max 5000 files returned
   from `rg --files`; query filters server-side). Optimization (incremental
   indexing, fzf-style scoring) is v2 if it bites.

4. **Symlinks and macOS case-insensitivity.** Cwd check uses
   `os.path.realpath()` on both sides + case-folded prefix compare (see
   "Path-traversal defense" above). This handles APFS case-insensitivity
   and the `/tmp` → `/private/tmp` symlink. TOCTOU (symlink swapped
   between check and write) is unmitigated; single-user localhost threat
   model. Policy: symlinks pointing *out* of cwd are rejected; symlinks
   pointing *into* or *within* cwd work. Revisit if users want to follow
   out-of-cwd symlinks.

5. **Open files don't persist across modal close.** This is intentional in
   v1 (no global workspace state). Real risk: Tom is in the middle of a
   one-line edit, clicks away to another pane, closes the modal, comes
   back, edit is gone. Mitigation: the dirty-warning on modal close
   confirms before discarding. **If this still feels wrong in practice,
   the v2 conversation is "should there be a persistent open-files
   list?"**

6. **Keybinding conflicts.** Capture-and-`preventDefault` on `Cmd-P`,
   `Cmd-S`, `Cmd-Shift-F`. Tauri checked (see "Keybinding capture"
   above) — no menu accelerators conflict.

7. **No autosave is a deliberate cut.** If Tom finds himself losing one-line
   edits to forgotten `Cmd-S`, autosave is a v1.1 addition (debounce
   500ms after typing stops). Not blocking v1.

8. ~~**Pane cwd lags.**~~ Retired — `display-message -p
   '#{pane_current_path}'` queries tmux directly with no caching layer.
   The cwd is real-time as of the request.

## Out of scope for this design, on the runway for v2

- LSP integration (`pyright`/`ty`/`rust-analyzer` over CodeMirror's LSP
  plugin).
- Vim mode (`@codemirror/vim`).
- Multi-file tabs in the modal.
- Persistent open-files list / project-scoped editor view.
- File tree pane.
- Inline git blame.
- Diff viewer in periscope (or LGTM diff-viewer improvements — separate
  spec).
- Shell integration → block model (previously discussed; still on the
  list but not on this milestone).

## Open questions to resolve in spec review

1. **Modal-tab vs. its own surface.** Code as a tab in the existing pane
   modal is the v1 plan. Is that the right home, or should the editor be a
   sibling view to the dashboard from day one? Cost of "tab" → cheap, fits
   existing patterns. Cost of "own view" → higher up-front, but probably
   the right end-state.

2. **Bundling boundary.** Is "only the editor goes through Vite-build"
   actually clean? Or is the right move to bite the bullet and build the
   whole frontend? The former keeps the dashboard's hot-reload-via-edit
   property; the latter is simpler conceptually.

3. **Single-pane scope for v1.** Are we sure no global workspace view is
   needed for v1? Counter-argument: Tom might open files from different
   projects in quick succession; modal-per-pane forces him to close one
   modal, open another, lose state. Counter-counter: that's exactly the
   v2 conversation; ship v1 and see.

4. **Encoding handling.** v1 plan: utf-8 only, refuse non-utf-8 with a
   clear error. Acceptable? Or do we need latin-1 / cp1252 fallback?

5. **CRLF / line endings.** v1 plan: preserve whatever was on disk
   (detect on read, restore on write). Confirm.
