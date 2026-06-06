# LGTM tight integration — code structure proposal

**Spec:** `docs/superpowers/specs/2026-06-01-lgtm-tight-integration-design.md`
**Date:** 2026-06-01
**Status:** proposal for review

Scope-narrowing decisions from the spec's four open questions are treated as
settled: gateway v1 = `review_add_doc` + `review_comment` only; `review_comment`
takes a batch array; theme via `&theme=` query param; LGTM diff colors kept.

---

## 1. Spec pushback

Nothing structural to push back on. The spec already lands on the codebase's
conventions: module-level async functions, no classes, the existing
find-or-create / create-response-slug contract, the `_CHANNEL_TOOLS` record +
`_do_*` handler pattern. Two small clarifications I'm encoding as decisions
rather than disagreements (see §7):

- The spec says helpers "set the `X-LGTM-Host: periscope` header." The cleanest
  place is a single private `_lgtm_post(client, path, json)` wrapper inside
  `lgtm.py` that always sets the header, so no call site can forget it. The spec
  lists four public helpers but not this private one — I'm adding it.
- The spec's `review_add_doc` resolves cwd → git-toplevel → slug. The existing
  *route* (`/api/lgtm/add-doc`) does **not** do the toplevel resolution — it
  passes the raw `cwd`. The spec is explicit that the toplevel step is
  load-bearing for the channel tool. I'm putting toplevel resolution in the
  **shared `lgtm_find_or_create_slug`** so both the route and the tool get it,
  which is a behavior change to the route (see §6 test note).

---

## 2. Assumptions

- LGTM `POST /projects` returns `{ ok, slug, url, ... }` (confirmed:
  `app.ts:86-94`, `res.json({ ok: true, ...result })`). `find_or_create` reads
  `slug` off that response on the create path.
- LGTM `POST /project/:slug/items` takes `{ path }` and returns an object with
  `id` (confirmed: `app.ts:637-647`). `lgtm_add_item` passes an **absolute**
  path; LGTM joins relative paths against its own `repoPath`, but periscope
  resolves to abs first so behavior is independent of LGTM's cwd assumptions.
- LGTM `POST /project/:slug/comments` hard-requires `author`, `text`, `item`
  and 400s otherwise (confirmed: `app.ts:664-668`). The `X-LGTM-Host: periscope`
  header makes a `direct`-mode comment hand back a `channel` payload instead of
  LGTM doing its own MCP push (`app.ts:693-697`). `review_comment` seeds
  review-mode comments (no `mode`), so the channel branch won't fire for the
  seeding path — the header is still set for consistency and to cover any future
  direct seeding.
- `git -C <cwd> rev-parse --show-toplevel` is the toplevel resolver. Periscope
  already shells git elsewhere; I assume a small helper is acceptable rather than
  importing GitPython.
- A freshly registered session is **not** in periscope's poll cache yet; the
  create path must use the create-response slug, never a post-register cache
  re-read (spec resolution-chain edge; existing `routes/lgtm.py:add_doc`
  behavior).
- The MCP tool-result error shape is `{"ok": false, "error": "..."}` wrapped in
  `_tool_result(...)`, matching `_do_link_*` (confirmed in `channels.py`).
  Channel tools do **not** raise `HTTPException` — that's route-only.

---

## 3. File layout

```
periscope/lgtm.py                  CHANGED  + 4 public async helpers + _lgtm_post + _git_toplevel
periscope/routes/lgtm.py           CHANGED  refactor 3 routes onto shared helpers; drop inline httpx
periscope/channels.py              CHANGED  + 2 _CHANNEL_TOOLS records, + 2 _do_* handlers, + instructions
CLAUDE.md                          CHANGED  standalone-MCP / double-MCP config note (docs only)
static/modal.js                    CHANGED  + lgtmThemePalette(); append &theme= in 2 iframe-src builders
tests/test_lgtm.py                 CHANGED  + direct coverage for the 4 helpers + toplevel + find/create
tests/test_channels.py             CHANGED  + review_add_doc / review_comment handler tests
tests/routes/test_lgtm.py          CHANGED  add X-LGTM-Host header assertion; keep existing behavior green

# lgtm repo
frontend/src/main.tsx              CHANGED  apply host palette from ?theme= inside if(isEmbedded)
```

No new files. Every change lands in an existing concern-file — correct for this
codebase (one file per subsystem; LGTM wire knowledge already lives in
`lgtm.py`).

---

## 4. Per-module structure

### `periscope/lgtm.py` — rung 1 (plain async functions)

This is a functional module today (module-level functions + module-level cache
dicts behind a lock). The new helpers are stateless wire-translation functions.
**No class.** Confirmed: nothing here owns coupled mutable state that a class
would encapsulate; the cache is already a module global with its own lock, and
the helpers don't touch it (find path reads via existing `cached_lgtm_state`).

New private helper (the header gate — one chokepoint so no caller forgets it):

```python
async def _lgtm_post(client: httpx.AsyncClient, path: str, json: dict) -> httpx.Response:
    """POST to LGTM with the X-LGTM-Host: periscope header always set.
    `path` is relative to LGTM_BASE_URL (e.g. '/projects')."""
    r = await client.post(
        f"{LGTM_BASE_URL}{path}", json=json,
        headers={"X-LGTM-Host": "periscope"},
    )
    r.raise_for_status()
    return r


def _git_toplevel(cwd: str) -> str:
    """Resolve cwd to its git repo root (LGTM keys sessions by toplevel).
    Returns the resolved toplevel, or the normalized cwd if not in a repo."""
    # subprocess git rev-parse --show-toplevel -C cwd; on failure return cwd.
```

Public helpers (signatures exactly as spec lists, typed):

```python
async def lgtm_register(repo_path: str) -> dict: ...
    # POST /projects {repoPath}; returns LGTM's body (has slug). Caller refreshes.

async def lgtm_add_item(slug: str, abs_path: str) -> dict: ...
    # POST /project/{slug}/items {path: abs_path}; returns body (has id).

async def lgtm_add_comment(slug: str, payload: dict) -> dict: ...
    # POST /project/{slug}/comments payload; payload pre-translated by caller.

async def lgtm_find_or_create_slug(repo_root: str) -> str: ...
    # find: cached_lgtm_state(repo_root)['slug'].
    # create: lgtm_register(repo_root) -> response['slug'] (NEVER cache re-read),
    #         then await _lgtm_refresh_all(). Raises ValueError if no slug back.
```

Note: `lgtm_find_or_create_slug` takes a **repo_root already resolved to
toplevel** — toplevel resolution is the caller's job (both route and tool call
`_git_toplevel` first). Keeping the resolve out of `find_or_create` keeps that
function purely about the find-vs-create branch and makes it unit-testable
without a real git repo.

Each helper opens its own `httpx.AsyncClient(timeout=5.0)` context (matching the
current route style), OR — cleaner — accepts an optional client. Decision in §7:
**each opens its own client**; simpler call sites, the third-use refactor is
about killing duplication not connection pooling, and the existing code already
opens-per-call.

Error policy: helpers let `httpx.HTTPError` / `OSError` propagate. Callers
translate: routes → `HTTPException(500, ...)`; tools → `_tool_result({"ok":
False, "error": ...})`. `lgtm_find_or_create_slug` raises `ValueError` for "LGTM
returned no slug" (built-in exception, contextful message — no custom hierarchy).

### `periscope/routes/lgtm.py` — rung 1, thinned

Routes become thin callers. Remove the three inline `import httpx` + AsyncClient
blocks. `lgtm_start` → `lgtm_register` + refresh. `lgtm_add_doc` →
`_git_toplevel` + `lgtm_find_or_create_slug` + `lgtm_add_item` + refresh.
`lgtm_remove_item` stays a direct DELETE (no shared helper proposed — single use,
and it's a DELETE not in the helper set; leave it inline or add
`lgtm_remove_item` helper only if §7's "consistency" call goes that way). Routes
keep their Pydantic bodies, path/file validation, and `raise HTTPException`
error convention — none of that moves into `lgtm.py`.

Behavior-preserving except: `add_doc` now resolves toplevel before find/create
(matches the tool; see §6 test note) and all writes now carry the header.

### `periscope/channels.py` — rung 1, two records + two handlers

Two new `_CHANNEL_TOOLS` records (plain dict data, as the existing four are) and
two `_do_*` handlers. Async (they await the `lgtm_*` helpers and `tmux`), so
they go through the existing `asyncio.iscoroutinefunction` branch in `_call_tool`
— `_do_spawn_claude_tool` already proves that path.

```python
async def _do_review_add_doc_tool(pane: str, arguments: dict): ...
async def _do_review_comment_tool(pane: str, arguments: dict): ...
```

Resolution chain, shared by both (factor a small private helper):

```python
def _pane_repo_root(pane: str) -> str:
    """pane %N -> cwd (tmux display-message #{pane_current_path}) -> git toplevel.
    Returns "" if the pane vanished (empty cwd), mirroring _do_spawn_claude_tool's
    vanished-pane guard."""
    cwd = tmux("display-message", "-t", pane, "-p", "#{pane_current_path}").strip()
    if not cwd:
        return ""
    return _git_toplevel(cwd)   # imported from periscope.lgtm
```

- `_do_review_add_doc_tool`: resolve root; if empty → error result. Resolve
  `path` against the **pane cwd** (relative arg) to an abs path; existence check.
  `slug = await lgtm_find_or_create_slug(root)`; `await lgtm_add_item(slug,
  abs_path)`; return `{"ok": True, "slug": slug, "item_id": ...}`.
- `_do_review_comment_tool`: resolve root; **find only, never create** — use
  `cached_lgtm_state(root)`; if no slug → `{"ok": False, "error": "no active
  review for <root>; start one first"}` (spec: comments presuppose a started
  review). Then for each entry in `arguments["comments"]`, translate to the HTTP
  shape and `await lgtm_add_comment(slug, payload)`. Return `{"ok": True, "slug":
  slug, "count": n}`.

Payload translation (spec-mandated; do **not** pass tool args through):

```python
{"author": "claude", "text": c["comment"], "item": c.get("item") or "diff",
 "file": c.get("file"), "line": c.get("line"), "block": c.get("block")}
```

**New import edge:** `channels.py` will import `_git_toplevel`,
`lgtm_find_or_create_slug`, `lgtm_add_item`, `lgtm_add_comment`,
`cached_lgtm_state` from `periscope.lgtm`. `channels.py` does **not** import
`lgtm` today — flagged. The edge is acyclic: `lgtm.py` imports only from
`periscope.log`; it does not import `channels`. Safe.

`inputSchema` for the two records:

```jsonc
// review_add_doc
{ "type": "object",
  "properties": { "path": {"type": "string",
    "description": "Markdown doc to attach; relative to the pane's cwd or absolute."} },
  "required": ["path"] }

// review_comment
{ "type": "object",
  "properties": { "comments": { "type": "array", "minItems": 1, "items": {
      "type": "object",
      "properties": {
        "comment": {"type": "string"},
        "file":    {"type": "string"},
        "line":    {"type": "integer"},
        "block":   {"type": "integer"},
        "item":    {"type": "string", "description": "LGTM item id; defaults to 'diff'."}
      },
      "required": ["comment"] } } },
  "required": ["comments"] }
```

Plus the `CHANNEL_INSTRUCTIONS` review section (spec's draft triggers, terse
style matching existing entries).

### `static/modal.js` — rung 1 (pure helper + two call-site edits)

`lgtmThemePalette()` exactly as the spec drafts it (reads computed `--bg-0` etc.,
maps to LGTM var names — DRY against `styles.css`, no duplicated hex). Append
`&theme=${encodeURIComponent(JSON.stringify(lgtmThemePalette()))}` to the `url`
in both `renderLgtmItem` (line 554) and `renderLgtmWalkthrough` (line 528).
Compute the palette per-build (cheap; picks up any runtime palette change for
free). No class, no state.

### `lgtm/frontend/src/main.tsx` — one block, no new structure

Exactly the spec's snippet, inside the existing `if (isEmbedded)` block after
`document.body.classList.add('embedded')`: parse `params.get('theme')`, JSON,
loop, `root.style.setProperty(k, val)` for `--`-prefixed string entries, swallow
parse errors. No new file, no exported function — LGTM gains a generic
"apply host palette" capability, not periscope's colors.

---

## 5. Patterns

**Used:**

- Functional registry (`_CHANNEL_TOOLS` records + `_do_*` handlers) — extend the
  existing one; the blessed pattern here, no factory class.
- Single chokepoint wrapper (`_lgtm_post`) so the `X-LGTM-Host` header can't be
  omitted — the spec calls header-omission the exact failure this design exists
  to avoid.
- Shared pure-ish translation function (`lgtm_*` helpers) consumed by 3 call
  sites (route + 2 tools) — the third-use rule the spec invokes.

**Considered and rejected:**

- A `LgtmClient` class holding a base URL / session — rejected. No coupled
  mutable state; base URL is a module constant; functions are the right rung.
- A custom `LgtmError` exception type — rejected. No caller catches a specific
  LGTM error to branch; routes map any failure to 500, tools to an error result.
  `ValueError` with a message suffices (spec rules: no custom hierarchy).
- A shared httpx client passed through the helpers — rejected for v1 (§7); each
  helper opens its own, matching current code. Revisit only if connection churn
  shows up, which it won't at this call volume.
- postMessage for the theme — rejected per resolved open-question 3 (FOUC).
- Mapping diff colors to periscope's status palette — rejected per resolved
  open-question 4.

---

## 6. Test strategy

### `tests/test_lgtm.py` (unit; httpx + subprocess mocked)

New direct coverage for the refactored helpers — the spec calls the refactor a
good moment to add it. These are pure wire-translation, so unit tests with
mocked `httpx.AsyncClient` are the right tool (a real LGTM dependency adds
nothing to "did we POST the right body with the right header").

- `lgtm_register` — asserts POST to `/projects` with `{repoPath}` **and the
  `X-LGTM-Host: periscope` header** (the load-bearing assertion).
- `lgtm_add_item` — POST to `/project/<slug>/items` with `{path: abs}` + header.
- `lgtm_add_comment` — POST to `/project/<slug>/comments` with the pre-translated
  payload + header.
- `lgtm_find_or_create_slug`:
  - **find path** — seed `_LGTM_BY_REPO` (as existing tests do), assert no POST,
    returns cached slug.
  - **create path** — empty cache, mock `lgtm_register` → `{slug: "new"}`, assert
    returns `"new"` from the **response**, and that `_lgtm_refresh_all` was
    awaited, and that the slug did **not** come from a post-register cache read
    (assert cache was never consulted after register — the spec's sharpest
    invariant).
  - **no-slug** — register returns no slug → raises `ValueError`.
- `_git_toplevel` — one real-git integration case: `tmp_path` with `git init`
  and a subdir; assert resolving from the subdir returns the toplevel
  (subdir→toplevel resolution — the spec's load-bearing case). Plus a not-a-repo
  case returning the cwd. Real git, not mocked: this is the exact thing that
  silently mis-resolves and registers a second session at a subdir. The Q1-2026
  mocked-migration lesson applies — mock the toplevel resolver here and the bug
  this guards against passes the test while prod mis-registers.

### `tests/test_channels.py` (unit; tmux + helpers mocked)

Existing file already mocks `_resolve_pid_for_pane` and patches `_write_state` —
follow that style. Mock `tmux` (cwd), mock `_git_toplevel`, mock the `lgtm_*`
helpers / `cached_lgtm_state`.

- `review_add_doc` happy path — assert `lgtm_find_or_create_slug` called with the
  **toplevel** (not raw cwd), `lgtm_add_item` called with the abs path, result
  shape `{ok: True, slug, item_id}`.
- `review_add_doc` vanished pane — `tmux` returns empty → error result, no helper
  calls.
- `review_add_doc` relative path resolution — relative `path` arg resolves
  against the pane **cwd** (not toplevel) to abs.
- `review_comment` happy path — batch of 2; assert `lgtm_add_comment` called
  twice with the **translated** payloads (`author:"claude"`, `text` from
  `comment`, `item` defaulting to `"diff"`) — the comment-payload-translation
  invariant.
- `review_comment` missing session — `cached_lgtm_state` returns None → error
  result, no `lgtm_add_comment` calls (the missing-session-error invariant; the
  tool must **not** auto-create).

Register both handlers in the test file's import list and the `_CHANNEL_TOOLS`
presence (a cheap "the record is wired" assertion guards against adding the
handler but forgetting the record).

### `tests/routes/test_lgtm.py` (existing; keep green + one new assertion)

The refactor must not regress these. Two specifics:

- `test_lgtm_start_happy_path` still passes — `lgtm_register` now opens the
  client the test already mocks via `httpx.AsyncClient`. Add an assertion that
  the POST carried `X-LGTM-Host: periscope` (the refactor's point; the route
  omitted it before).
- `add_doc` route now calls `_git_toplevel`. Its route test (if present; add one
  if not) must mock `_git_toplevel` or run against a real `git init` tmp repo so
  the toplevel step doesn't shell out to a non-repo `tmp_path` and change the
  registered path. Flag: the current `tmp_path` cwd is not a git repo, so
  `_git_toplevel` returns it unchanged — behavior is preserved for the test, but
  add the mock to make the intent explicit.

### Theme (browser-verified, not unit-tested)

Per periscope UI convention (`CLAUDE.md`: vanilla JS, dev-server-as-oracle). Run
`npm run dev` + LGTM on :9900, open a review in the modal. Check: no flash of
LGTM's dark theme on iframe load (the whole reason for query-param-over-
postMessage), surfaces/text/borders match periscope chrome, diff add/del colors
still legible. No pytest case.

---

## 7. Decisions to sanity-check

1. **Each `lgtm_*` helper opens its own httpx client** (vs threading a shared
   `AsyncClient` through all of them). Chose own-client: matches existing route
   code, simplest call sites, the refactor is about de-duplicating wire logic not
   pooling. Close because find-or-create-then-add-item is two POSTs that could
   share one client; I judged the saving not worth the signature noise.

2. **Toplevel resolution lives in the callers, `find_or_create` takes an
   already-resolved root** (vs `find_or_create` calling `_git_toplevel` itself).
   Chose caller-resolves: keeps `find_or_create` git-free and unit-testable
   without a real repo, and the route already has a validated cwd. Close because
   "always resolve toplevel" is exactly the kind of thing you want impossible to
   forget — the counter-argument is the same one that motivated `_lgtm_post`.
   Mitigation: both call sites resolve, and the channel test asserts the
   toplevel (not raw cwd) reaches `find_or_create`.

3. **`add_doc` route gains toplevel resolution** (behavior change to an existing
   route). Chose to align route with tool so a doc added from the browser and one
   added by Claude target the same session. Close because it's a behavior change
   under a "refactor" banner; called out so it's a deliberate review decision,
   not a silent drift. If you'd rather keep the route's raw-cwd behavior, the tool
   still resolves toplevel and they diverge — say so.

4. **`lgtm_remove_item` stays inline in the route** (not promoted to a helper).
   Chose inline: single call site, it's a DELETE outside the spec's helper set,
   and the third-use rule isn't met. Close only on consistency grounds — if you
   want *all* LGTM wire calls in `lgtm.py`, add `lgtm_remove_item(slug, item)`
   too; it's a one-liner.
