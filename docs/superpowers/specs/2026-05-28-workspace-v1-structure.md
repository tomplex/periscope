# Workspace v1 — code structure proposal

**Date:** 2026-05-28
**Spec:** `2026-05-28-workspace-v1-design.md`
**Status:** awaiting Tom review

This is the structural blueprint the implementation plan should follow.
Decisions, not menus. The spec has already addressed pane→cwd resolution,
`?session=&index=` URLs, the scoped Vite bundle, the modal tab integration
points, and the ripgrep subprocess plumbing — those are taken as given and
not relitigated here.

---

## 1. Spec pushback

Two structural assumptions worth flagging. Neither blocks the spec — Tom
decides.

- **`mountEditor` returns a method bag, not a class.** The spec hints at an
  `EditorAPI` object with `{getContent, setContent, focus, isDirty, destroy}`.
  Tom's taste reads more like "frozen object with closured access to the
  CodeMirror `EditorView`" than a class with a constructor. Concrete shape
  below in §4. Not a real disagreement, just confirming the rung — no
  `class Editor`.

- **Where the "needs `npm run build` first" placeholder lives.** Spec §"A
  fresh checkout that runs `uv run server.py`…" puts the placeholder in
  Thread 2's frontend patch (the Code tab message). That's fine, but it
  means Thread 1's "done when" criterion ("no 404s because nothing fetches
  it yet") only holds until Thread 2 ships. After Thread 2, a fresh
  checkout that skipped `npm run build` will 404 on Code-tab activation.
  Worth either (a) the Thread 2 patch detecting the 404 and rendering the
  "run `npm run build`" placeholder, or (b) the bundle file getting
  committed (not my preference). The proposal assumes (a) — see §4 below.

## 2. Assumptions

Gaps filled to keep the proposal complete and reviewable. Each is a place
the spec didn't pin a structural detail.

- **Per-extension language map.** I propose 16 entries keyed off the
  extensions implied by the spec's language list. Mapping table is
  `Record<string, () => Promise<LanguageSupport>>`.
- **External-change polling cadence.** The spec says "on Code-tab focus,
  fetch …". I assume that means a window-`focus` listener and a Code-tab
  activation listener — no polling timer. Cheaper, no churn.
- **Path resolution for not-yet-existing files.** Spec implies write can
  create new files (the editor surface is general). I'm assuming the
  picker only lists existing files; `write_file` rejects paths whose parent
  doesn't exist. New-file creation via the picker is deferred.
- **Encoding: utf-8 only in v1.** Open question §4 in the spec — I bake
  in utf-8 only; non-utf-8 returns 415 from the read endpoint.
- **Line endings: detect-on-read, restore-on-write.** Open question §5 —
  baked in. The detected `\n` vs `\r\n` rides on the read response as part
  of `encoding` (`utf-8-lf` / `utf-8-crlf`) so the write can round-trip
  without a separate field.
- **Fuzzy filtering algorithm.** Spec says "fuzzy" without naming one.
  Proposal: a simple subsequence match with a small scoring function
  (consecutive-match bonus, start-of-segment bonus) — VS Code-style minus
  the bells. Lives as `_fuzzy_score(path: str, query: str) -> int | None`
  in `workspace.py`. Behavior is testable; a fancier scorer slots in
  later without API change.

## 3. File layout

New files:

```
periscope/workspace.py                    # logic: rg, fuzzy, read/write, search
periscope/routes/workspace.py             # 4 endpoints, cwd resolution
tests/test_workspace.py                   # unit + integration over real rg
tests/routes/test_workspace.py            # FastAPI client tests

static/file-picker.js                     # Cmd-P overlay (plain ES module)
static/codebase-search.js                 # Cmd-Shift-F overlay (plain ES module)
static/code-tab.js                        # Code-tab controller (plain ES module)
static/src/editor/index.js                # bundled: mountEditor() entry
static/src/editor/languages.js            # bundled: ext→importer map
static/src/editor/setup.js                # bundled: extension list, keymaps
```

Changed files:

```
periscope/app.py                          # add workspace to include_router tuple
static/modal.js                           # add "Code" to buildTabSpec + dispatch
static/index.html                         # #modal-code-content pane sibling, init code-tab
static/styles.css                         # #modal[data-tab="code"] rule
static/app.js                             # Cmd-P / Cmd-S / Cmd-Shift-F gates
vite.config.js                            # add build.rollupOptions for static/dist/
package.json                              # CodeMirror deps + build script
dev.sh                                    # third bg process: vite build --watch
bin/periscope                             # command -v npm + npm install && npm run build
README.md                                 # prerequisites: ripgrep + node
CLAUDE.md                                 # narrow the "no bundler" invariant
```

No test category exists for frontend modules today. **I do not propose
adding one for v1.** Frontend code is intentionally manually verified
against the live dashboard (per existing convention). The structural
choice that makes this safe: keep complex behavior on the server side
(fuzzy scoring, path traversal, the rg streaming) where tests cover it;
keep the frontend modules thin enough that a manual smoke test ("can I
Cmd-P, type, pick, edit, save?") is sufficient.

## 4. Per-module structure

### `periscope/workspace.py` — plain functions, rung 1

No state owned by the module. cwd is passed in. Four entry-point
functions plus a small handful of private helpers.

**Public surface:**

```python
def find_files(cwd: Path, query: str, *, max_results: int = 5000) -> list[str]: ...

def read_file(cwd: Path, path: str) -> FileRead: ...

def write_file(cwd: Path, path: str, content: str, expected_mtime: float | None) -> FileWrite: ...

def search_codebase(cwd: Path, query: str, *, max_hits: int = 200) -> SearchResult: ...
```

**Typed return values** as frozen dataclasses (rung 2 for the data, rung
1 for the functions on it):

```python
@dataclass(frozen=True, slots=True)
class FileRead:
    content: str
    mtime: float
    encoding: str            # "utf-8-lf" | "utf-8-crlf"
    truncated: bool          # currently always False — reserved for future

@dataclass(frozen=True, slots=True)
class FileWrite:
    mtime: float

@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str               # relative to cwd
    line: int               # 1-indexed
    text: str               # the matched line, no trailing newline

@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[SearchHit]
    truncated: bool
```

Frozen + `slots=True` is the canonical "value-object" shape. The route
layer converts these to dicts for the JSON response (small price; the
typed objects are worth it for the internal call sites and tests).

**Private helpers — factored:**

```python
def _resolve_under(cwd: Path, rel_path: str) -> Path:
    """Resolve rel_path against cwd, refuse paths escaping cwd.
    Case-folded prefix check on realpath of both sides. Returns the
    resolved absolute Path or raises ValueError."""

def _run_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    max_lines: int,
    timeout_s: float = 30.0,
) -> Iterator[str]:
    """Popen + line-iterate + early-terminate. Used by both find_files
    and search_codebase. Yields stripped lines; closes the subprocess
    cleanly on early break or timeout."""

def _fuzzy_score(path: str, query: str) -> int | None:
    """Subsequence match with start-of-segment bonus.
    None = no match. Higher score = better."""
```

**Direct answers to the four taste questions:**

1. **Subprocess plumbing.** *Factored.* `_run_streaming` is the right
   abstraction — both `find_files` and `search_codebase` need the same
   shape (Popen, line iteration, early termination on cap, timeout), and
   the cap+terminate dance is the kind of thing that's easy to get
   wrong in two places. The function is small (≈25 LOC) and has one
   obvious unit test (give it a script that emits N lines, cap at 3,
   assert it terminates promptly). Not premature DRY: two consumers,
   identical contract, real risk of drift if duplicated.

2. **Error model.** *Built-in exceptions with contextful messages.*
   Periscope's convention (confirmed: `projects.py`, `worktree_spawn.py`,
   `channels.py` all use `ValueError`; route layer translates) is
   `ValueError` for "you asked for something invalid," `FileNotFoundError`
   for "the target isn't there," `PermissionError` for permission-style
   denials. The route layer maps:

   - `FileNotFoundError` → 404
   - `ValueError("path outside cwd: …")` → 400
   - `ValueError("file too large: … bytes > 10MB")` → 413
   - `ValueError("binary file")` → 415
   - `ValueError("not utf-8: …")` → 415
   - mtime conflict → I do propose **one** custom type:
     `StaleMTimeError(ValueError)` so the route catches `StaleMTimeError`
     specifically and returns 409 without false-positive matching on
     other ValueErrors. This is the "a caller catches this specific
     type to make a control-flow decision" trigger from the taste rules.
     One custom exception, named for its catcher.

3. **Path-traversal helper.** *Factored.* `_resolve_under(cwd, rel_path)`
   is the highest-risk surface in the module. Pulling it out lets the
   test suite drive it directly with a matrix of inputs (symlink in,
   symlink out, `..`, absolute path, case-fold collision, `/tmp` →
   `/private/tmp`) without setting up a whole file to read. The four
   public functions become one-liners against it: `target =
   _resolve_under(cwd, path)`. This is the most important factoring in
   the proposal.

4. **No additional patterns considered for `workspace.py`.** No class,
   no registry, no strategy — pure-functions-over-frozen-data is the
   right rung for "stateless data pipeline with subprocess fanout."

### `periscope/routes/workspace.py` — single APIRouter, four functions

```python
router = APIRouter()

def _resolve_cwd(session: str, index: int) -> Path:
    """Run tmux display-message; translate errors to HTTPException(404).
    Lives here, not in workspace.py — the route layer owns the
    session+index → cwd contract (workspace.py is cwd-agnostic by spec
    decision)."""

@router.get("/api/workspace/files")
def files(session: str, index: int, q: str = ""): ...

@router.get("/api/workspace/file")
def get_file(session: str, index: int, path: str): ...

@router.put("/api/workspace/file")
def put_file(session: str, index: int, path: str, body: WriteBody): ...

@router.get("/api/workspace/search")
def search(session: str, index: int, q: str, max_hits: int = 200): ...

class WriteBody(BaseModel):
    content: str
    expected_mtime: float | None = None
```

Each endpoint is `_resolve_cwd(...)` → call `workspace.xxx(cwd, ...)`
inside `try:` → translate exceptions → return dict. Single point where
the typed dataclass becomes JSON (`asdict(result)` or per-field
spelling for the hit list).

`_resolve_cwd` is a route-layer helper — *not* exported, *not* in
`workspace.py`, *not* duplicated across the four endpoints. Spread of
that resolver would be the structural smell to avoid.

### `static/src/editor/index.js` — bundled mountEditor entry

```js
export async function mountEditor(container, opts) {
  const { initialContent = "", language = null, onSave = null } = opts;
  const extensions = await buildExtensions({ language, onSave });
  const view = new EditorView({
    state: EditorState.create({ doc: initialContent, extensions }),
    parent: container,
  });
  let lastSavedContent = initialContent;
  return Object.freeze({
    getContent: () => view.state.doc.toString(),
    setContent: (text) => { /* dispatch full-replace, update lastSavedContent */ },
    setLanguage: async (lang) => { /* reconfigure compartment */ },
    markSaved: () => { lastSavedContent = view.state.doc.toString(); },
    isDirty: () => view.state.doc.toString() !== lastSavedContent,
    focus: () => view.focus(),
    scrollToLine: (line) => { /* EditorView.scrollIntoView at line */ },
    destroy: () => view.destroy(),
  });
}
```

- **Frozen returned object** — the value-bag pattern. No class. The
  closure over `view` and `lastSavedContent` is exactly what a class
  would have done with `this`, minus the ceremony.
- **`setLanguage` and `markSaved` added** beyond the spec's named
  surface. `setLanguage` is needed so a `Cmd-P`-driven file switch can
  re-enter without remounting (CodeMirror's `Compartment` makes this
  cheap). `markSaved` is needed so `Cmd-S` can flip `isDirty` to false
  without forcing the consumer to track "what did we last write."
- **`scrollToLine`** is for Thread 3 (codebase-search → open at line).
  Lands in Thread 2's API since changing the API in Thread 3 is more
  surgery than including it up front.

### `static/src/editor/languages.js` — extension → lazy importer

*Decision: own file, not inlined into `index.js`.* At 16 entries it's a
real chunk of code, and crucially it has one job (data mapping) that
`index.js` doesn't share. Separating it also makes "add or remove a
language" a single-file diff and a single-file blast radius.

```js
// Each value is an importer that resolves to a CodeMirror LanguageSupport.
// Imported on first use of the extension; cached afterward.
const loaders = {
  js:   () => import("@codemirror/lang-javascript").then(m => m.javascript()),
  jsx:  () => import("@codemirror/lang-javascript").then(m => m.javascript({ jsx: true })),
  ts:   () => import("@codemirror/lang-javascript").then(m => m.javascript({ typescript: true })),
  tsx:  () => import("@codemirror/lang-javascript").then(m => m.javascript({ jsx: true, typescript: true })),
  py:   () => import("@codemirror/lang-python").then(m => m.python()),
  // ...rs, go, java, json, yml/yaml, md, html, css, sh/bash, sql, toml
};
const cache = new Map();

export async function loadLanguage(ext) {
  const key = ext.toLowerCase();
  const loader = loaders[key];
  if (!loader) return null;        // plain-text fallback
  if (!cache.has(key)) cache.set(key, loader());
  return cache.get(key);           // returns the in-flight Promise; safe to await twice
}
```

Function over registry-class: just a module-level `Map` + two functions.
Per the taste rules.

### `static/src/editor/setup.js` — base extension list

basicSetup + keymap config + theme. Pulled out of `index.js` so
`mountEditor` reads as orchestration, not a wall of imports.

### `static/code-tab.js` — Code-tab controller

*This resolves question 3.* Of the three factorings offered:

- **(a) Everything in `modal.js`** — rejected. `modal.js` is already
  1182 lines and absorbs every concern adjacent to the modal. Adding
  the editor's lifecycle (lazy import, mount, dirty state, conflict
  banner, external-change reload) into it pushes it well past the
  point where "one file = one concern" still holds for that file.

- **(b) New `static/code-tab.js`** — *chosen.* It mirrors the
  pattern already used for cleanup-modal.js, commands-modal.js,
  review-pr-modal.js, settings-modal.js, new-project-modal.js —
  "modal-adjacent overlay or surface gets its own file." Even Tom's
  most modal-heavy refactor stopped at "this overlay deserves a file."

- **(c) Pull all existing overlays out** — rejected as scope creep.
  The mounted-doc dropdown, the LGTM iframe wiring, the link-ask
  buttons all currently live in `modal.js` and have done so without
  obvious harm. Touching them in a workspace PR mixes concerns.

`code-tab.js` exports:

```js
export function initCodeTab();                              // wires DOM listeners once
export async function activateCodeTab(target, lastPaneData); // called when tab clicked
export function deactivateCodeTab();                        // called on modal close / tab switch
export function isDirty();                                  // for the modal-close dirty-warning
export function getActiveTarget();                          // for the file-picker to query cwd
export async function openFileInEditor(path, line=null);    // called by file-picker / search
```

`modal.js` integration is the minimum surgery the spec calls out:

- `buildTabSpec` adds `{ id: "code", label: "Code" }` after Terminal.
- `ensureStripStructure` includes Code in the always-shown set
  (between Terminal and the optional Diff entry).
- The dispatch in `renderReviewPane` grows a `tabId === "code"` branch
  that calls `activateCodeTab(state.activeTarget, lastPaneData)`.
- `closeModal` calls `deactivateCodeTab()` (after the dirty-warning
  confirm).

The Code-tab module owns the lazy import:

```js
let editorModule = null;
async function loadEditor() {
  if (editorModule) return editorModule;
  try {
    editorModule = await import("/static/dist/editor.js");
  } catch (e) {
    // Per §1 pushback: render "run `npm run build` first" placeholder.
    renderBuildMissingPlaceholder();
    throw e;
  }
  return editorModule;
}
```

### `static/file-picker.js` — Cmd-P overlay

Mirrors `commands-modal.js` structurally: module-scope `isOpen`,
`render()`, `openFilePicker()`, `closeFilePicker()`, `initFilePicker()`.
`openFilePicker` takes `{ target, onPick }` so the picker is decoupled
from `code-tab.js` (the only consumer in v1; that's fine — the seam
exists where it would matter for v2).

Uses `overlay.js`'s `pushEscape`/`popEscape` for Escape handling.
Debounce on input change is the only piece of "logic" worth naming —
≈80ms `setTimeout`, cleared on each keystroke.

### `static/codebase-search.js` — Cmd-Shift-F overlay

Same shape as `file-picker.js`. Different row template (`path:line —
content`). On selection, calls `openFileInEditor(path, line)` from
`code-tab.js`.

### `static/app.js` — keybinding gates

Adds three new keydown branches alongside the existing `Cmd-/`, `/`,
`Tab`, arrow handlers (`app.js:27-117` per the spec). Each:

```js
if (e.metaKey && e.key === "p" && !e.shiftKey && modalOpen() && tab === "code") {
  e.preventDefault();
  openFilePicker({ target: state.activeTarget, onPick: openFileInEditor });
  return;
}
```

Equivalent for `Cmd-S` (calls a save function exported by `code-tab.js`)
and `Cmd-Shift-F` (calls `openCodebaseSearch`).

## 5. Patterns

Used:

- **Frozen value-objects + pure functions** in `workspace.py` — rung 2.
- **Closure-bag returned object** in `mountEditor` — JS analogue of
  rung 1 (no class for a thing with no genuine polymorphism).
- **One file per concern** for the three new frontend modules
  (`code-tab.js`, `file-picker.js`, `codebase-search.js`) — matches
  existing periscope convention.
- **Typed config object** (`WriteBody`) for the one PUT body —
  pydantic, consistent with other route modules.
- **Module-level lazy-import cache** in `languages.js` — a `Map`, not
  a class.
- **One custom exception** (`StaleMTimeError`) keyed to a specific
  catch decision — per the taste rules.

Considered and rejected:

- **`class Editor` wrapping CodeMirror.** No polymorphism, no second
  implementation foreseen. Closure-bag is enough.
- **A `Workspace` class holding `cwd` and the four operations as
  methods.** Spec explicitly keeps cwd out of workspace.py state —
  module-level functions with `cwd` as first arg fit better.
- **A `FileSearcher`/`FileFinder` strategy split** for "ripgrep today,
  something pluggable tomorrow." Speculation, not a named future
  implementation. Single function until a second backend lands.
- **Custom exception hierarchy** (`WorkspaceError`, `TraversalError`,
  `TooLargeError`, …). Rejected per taste rules; ValueError +
  contextful message + route translation matches the rest of the
  codebase. Only `StaleMTimeError` survives, justified above.
- **A tab registry in `modal.js`.** Spec explicitly defers; concur.
- **A new "frontend test" category.** Concur with the existing
  no-frontend-tests posture for v1; the server-side surface absorbs
  the testable risk.

## 6. Test strategy

Per module:

### `periscope/workspace.py` → `tests/test_workspace.py`

*Mix: unit tests for logic, integration over real `rg` and real
filesystem.* The Q1 2026 incident is the reason: mocking the
subprocess would let a real-world `rg` JSON shape change slip past.

- `_resolve_under` — **unit tests**, exhaustive matrix. Pure-function
  inputs, pure-function outputs. tmpdir fixtures for the symlink
  cases. This is the security-critical surface; it gets the most
  cases.
- `_fuzzy_score` — **unit tests**, pure function over strings.
- `_run_streaming` — **integration**, fed a small shell script
  (`sh -c 'for i in $(seq 1 100); do echo $i; done'`) to verify
  cap-terminates-promptly behavior. Not mocked.
- `find_files` — **integration**, runs real `rg --files` in a tmpdir
  with a known file set. Covers the cap + the fuzzy filter together.
  Skipped (via `pytest.skip`) if `rg` is not on PATH; CI installs it.
- `read_file` — **unit/integration mix.** Real files in tmpdir for
  happy path, binary rejection (write `\x00\x00`), too-large rejection
  (write 10MB+1 byte), LF vs CRLF round-trip.
- `write_file` — **integration**, real filesystem. mtime conflict
  case writes the file, calls write_file with stale mtime, asserts
  `StaleMTimeError`.
- `search_codebase` — **integration**, real `rg --json` in a tmpdir
  with seeded matching content. One non-UTF-8 case (binary file with
  matching bytes) to exercise the `data.lines.bytes` skip path.

### `periscope/routes/workspace.py` → `tests/routes/test_workspace.py`

*Integration via FastAPI's TestClient.* Mocks only the tmux
`display-message` call (to make tests cwd-independent). The
`workspace.py` calls run real against a tmpdir fixture set up by the
test.

- Happy path for each endpoint.
- 404 when tmux returns empty cwd / session not found.
- 400 on path traversal (relative `..`, absolute path outside cwd,
  out-of-cwd symlink).
- 409 on stale mtime PUT.
- 413 on oversize read.
- 415 on binary read.
- 503 (or whatever the spec eventually picks) when `rg` is missing —
  test via `mocker.patch("shutil.which", return_value=None)`.

### Frontend modules

*No automated tests, per existing convention.* Manual smoke checklist
goes in the implementation plan as a "done when" criterion for Thread 2
and Thread 3, not as code.

### Testability flags

- The closure-bag editor API is harder to test than a class would be,
  but only manually-testable code lives in it (CodeMirror integration).
  No internal logic worth unit-testing is hidden inside it. Not a
  smell.
- `_resolve_under` being its own function is the explicit
  testability-driven factoring. Worth restating: the alternative
  (inline the check in each of read/write/find) would force tests to
  drive path-traversal cases through the full HTTP-and-rg surface,
  which is slower and noisier.

## 7. Decisions to sanity-check

The close calls. Tom decides; the rest were clear.

- **`_run_streaming` factored vs inlined.** I went factored. Alternative:
  inline the Popen-and-iterate dance into `find_files` and
  `search_codebase` separately. Close because there are only two
  consumers and the two have different post-processing — borderline
  premature-DRY territory. Factored wins for me because the
  early-terminate + timeout logic is exactly the kind of cleanup-on-
  break code that's easy to forget in one of two copies.

- **`StaleMTimeError` as the lone custom exception.** Alternative:
  use `ValueError` with a sentinel string the route matches on, or
  pass the conflict status back as part of a result object. Close
  because the taste rules push against custom exceptions and this
  one buys exactly one cleaner route-layer catch. I lean custom
  because the route's translation table (FileNotFoundError → 404,
  generic ValueError → 400, **mtime conflict → 409**) needs a clean
  discriminator, and string-matching `e.args[0].startswith(...)` is
  uglier than the type check.

- **`code-tab.js` as its own file vs growing `modal.js`.** Went
  factored (b). Alternative (a) — extending `modal.js` — would
  mirror how Review currently lives inside `modal.js`. Close because
  matching the existing precedent has value. Factored wins because
  the editor surface has its own lifecycle (lazy import, dirty
  state, save handler, conflict banner, external-change reload)
  beyond what Review needs, and `modal.js`'s size already feels
  near a refactor boundary.

- **`scrollToLine` in the editor API in Thread 2 vs Thread 3.**
  Went Thread 2 (include up front). Alternative: add it in Thread 3
  when codebase search needs it. Close because YAGNI says wait, but
  changing `mountEditor`'s frozen-object shape in Thread 3 is a
  cross-thread edit and worth avoiding.

- **Per-extension language map as its own file vs inlined.** Went
  own-file (`languages.js`). Alternative: inline into `index.js`
  since it's data, not logic, and the file would be small. Close
  because both factorings read fine. Own-file wins because adding
  or removing a language is a one-file diff and the file naturally
  evolves separately from `mountEditor`'s code.
