# LGTM ↔ Periscope tighter integration — design

**Date:** 2026-06-01
**Status:** draft
**Repos touched:** `periscope` (primary), `lgtm` (one frontend file, theme only)

## Context

Periscope embeds LGTM (a code-review web app on `:9900`) as an iframe in the
pane modal, mirrors LGTM's session list onto pane cards, and bridges
LGTM→Claude notifications through periscope's reliable channel. The two tools
are decoupled by design: LGTM is unaware of periscope; the contract is HTTP +
SSE + a thin `host=periscope` behavior switch (see periscope `CLAUDE.md`,
"LGTM integration").

Two concrete stumbles surfaced in daily use:

1. **Claude can't reliably add docs/comments to a review.** Claude tries to
   reach LGTM's own MCP server and the tools aren't available — LGTM's MCP is
   a *separate* service the pane's Claude must be independently configured for,
   and periscope can't guarantee it's connected. Meanwhile periscope already
   runs an in-process MCP server (`channel_shim.py` → unix socket) that *every*
   Claude pane is reliably wired to.

2. **LGTM's palette clashes with periscope's chrome** in the embedded iframe,
   and periscope's palette is preferred.

## Guiding principle

**Don't give periscope control over LGTM's lifecycle or internals. Thicken the
cooperative contract instead.** The existing notify-bridge is the template: a
one-directional `host=periscope` behavior switch, not a dependency. Both
changes below are more of the same — no cross-imports, no shared process, LGTM
keeps working standalone. The HTTP/SSE boundary stays the contract.

The symmetry this completes:

| Direction | Mechanism | Status |
|---|---|---|
| LGTM → Claude (feedback) | LGTM postMessages payload → periscope delivers over reliable channel | exists |
| Claude → LGTM (review ops) | Claude calls periscope MCP tool → periscope calls LGTM HTTP | **Part 1** |
| LGTM render (palette) | periscope passes its palette → LGTM applies when embedded | **Part 2** |

## Goals

- Claude in a pane can attach a document to its repo's LGTM review without any
  per-project MCP configuration, using periscope's already-connected channel.
- Claude in a pane can seed inline review comments the same way.
- The embedded LGTM iframe renders in periscope's palette, host-provided
  (not baked into LGTM), with no flash of LGTM's own dark theme.

## Non-goals

- Periscope managing LGTM's lifecycle (spawn/stop/health). Out of scope —
  LGTM stays launchd/manual managed independently.
- Wrapping LGTM's *full* tool surface. Specifically excluded from v1:
  - `set_walkthrough` — no HTTP equivalent (broadcast-only); can't be done
    over the contract without LGTM growing a new endpoint.
  - `set_analysis` / `read_analysis` — driven by LGTM's own `/lgtm analyze`
    skill agents, not by the pane-Claude. No present need.
  - `read_feedback` — feedback already arrives at the pane via the existing
    notify-bridge channel push; a pull path is redundant and LGTM exposes no
    HTTP GET for it.
  - `claim_reviews` as a distinct tool — the human starts a review from the
    modal's "+ Start review" button (existing). `review_add_doc` find-or-creates
    the session anyway, so a standalone start tool earns nothing yet.
- A general theme system in LGTM (light mode, user toggle). Only an
  embedded-host palette override.

---

## Part 1 — Periscope as the MCP gateway for LGTM review ops

### Approach

Add review tools to periscope's channel MCP (`periscope/channels.py`). Their
handlers translate to LGTM's documented HTTP API — the same translation
`periscope/routes/lgtm.py` already does for the browser. Claude in a pane never
talks to LGTM's MCP; it calls a periscope tool, periscope speaks HTTP to LGTM.

### New channel tools

Two tools, mirroring the two Claude→LGTM operations that matter during review:

**`review_add_doc(path)`**
- Attach a markdown document (design spec, plan, ADR) as a reviewable tab.
- `path` resolved against the pane's cwd if relative.
- Find-or-create the LGTM session for the cwd's repo, then add the item.
- Maps to: `POST {lgtm}/projects` (if needed) + `POST {lgtm}/project/:slug/items`.

**`review_comment(comments)`**
- Seed one or more inline comments authored by Claude.
- `comments`: array of `{ file?, line?, block?, comment, item? }` (mirrors
  LGTM's own `comment` MCP tool ergonomics so Claude can seed a batch in one
  call).
- Periscope loops `POST {lgtm}/project/:slug/comments` per entry, targeting the
  cwd's existing session (error if none — comments presuppose a started review;
  we do not auto-create here).
- **Payload translation is required — do not pass the tool args through.** The
  HTTP endpoint (`app.ts:662-668`) destructures `{ author, text, item, ... }`
  and hard-requires `author`, `text`, AND `item` (400 otherwise). The MCP-tool
  shape (`{comment}`, `item ?? 'diff'` default) exists only in `mcp.ts`/
  `session.ts`, which the HTTP path does not share. Each entry maps to:
  `{ author: "claude", text: c.comment, item: c.item ?? "diff", file: c.file,
  line: c.line, block: c.block }`.

Both return `{ ok, slug, ... }` on success and `raise HTTPException`-equivalent
tool errors (`{ok: false, error}` in the tool-result body, matching the
existing `_do_link_*` handlers' shape) on failure.

### Slug resolution (the new piece)

Channel tool handlers close over `pane` (the tmux `%N` id), not a cwd or slug.
Resolution chain:

1. `pane` → cwd: `tmux display-message -t <pane> -p '#{pane_current_path}'`
   (identical to `_do_spawn_claude_tool`). Guard for empty (vanished pane), as
   spawn does.
2. **cwd → repo root: `git -C <cwd> rev-parse --show-toplevel`.** This is
   load-bearing, not optional. LGTM keys sessions by repo *root* (it registers
   `repoPath` at toplevel — see its `/open` route, `app.ts:884`, which does the
   same `--show-toplevel`). A Claude pane is frequently `cd`'d into a subdir
   (`src/`, a worktree subpath); passing the raw cwd would miss the cache and,
   worse, make `review_add_doc` *register a second session rooted at the
   subdir*. Resolve to toplevel first, then use that for both lookup and create.
3. root → slug: `cached_lgtm_state(root)` (existing) for the find path;
   `POST {lgtm}/projects {repoPath: root}` for the create path.

Edge: a freshly-created session won't be in periscope's 30s-poll cache yet
(the cache is populated only by `_lgtm_refresh_all`). The create path must use
the slug returned **directly from the create response**, never a post-register
cache re-read, then call `_lgtm_refresh_all()` — exactly what
`routes/lgtm.py:lgtm_add_doc` already does. `lgtm_find_or_create_slug` encodes
this contract: cache lookup on the find path, create-response slug on the
create path.

### Shared LGTM HTTP helpers (refactor)

`routes/lgtm.py` currently inlines `httpx` calls. With the channel tools added,
the same calls have multiple call sites. Factor them into `periscope/lgtm.py`
as reusable async functions, consumed by both the routes and the channel tools:

- `async def lgtm_register(repo_path: str) -> dict` → `POST /projects`
- `async def lgtm_add_item(slug: str, abs_path: str) -> dict` → `POST /project/:slug/items`
- `async def lgtm_add_comment(slug: str, payload: dict) -> dict` → `POST /project/:slug/comments`
- `async def lgtm_find_or_create_slug(repo_root: str) -> str` — cache lookup on
  the find path, **create-response slug on the create path** (never re-read the
  cache after register; see the resolution-chain edge note).

All helpers set the **`X-LGTM-Host: periscope` request header.** This is the
gate (`app.ts:693-697` on `/comments`, `:745` on `/submit`) that makes LGTM
return a `channel` payload for periscope to deliver over its reliable channel
instead of doing its own flaky MCP push. The existing routes omit the header
today; the refactor fixes that too. Omitting it silently routes notifications
through the path this whole design exists to avoid — the symmetry table above
depends on it.

This removes existing duplication (third-use rule satisfied: routes +
two tool handlers) and keeps all LGTM-wire knowledge in `lgtm.py`. Routes
become thin callers; tool handlers reuse the same path.

### Channel instructions

`CHANNEL_INSTRUCTIONS` (the MCP server instructions Claude reads) gains a
review-tools section with concrete triggers, in the same terse style as the
existing tool docs. Draft triggers:

- `review_add_doc` — when the user asks you to "put this up for review",
  "add the spec to the review", or you've written a design/plan doc and a
  review is the next step.
- `review_comment` — when seeding inline review comments on the diff for the
  user to respond to.

### Standalone-LGTM / double-MCP guidance (docs, not code)

When a project runs under periscope, its Claude should **not** also register
LGTM's own MCP server (else Claude sees two `add_document`-like tools and picks
wrong). The guidance: under periscope, omit LGTM's MCP from the project's
`.mcp.json`; periscope's channel provides `review_*`. LGTM's own MCP remains
the path for standalone (no-periscope) use. This is a configuration note for
periscope `CLAUDE.md`, not a code change.

### What does NOT change

LGTM requires **zero changes** for Part 1 — periscope only calls existing
HTTP endpoints. The boundary is untouched.

---

## Part 2 — Host-provided embedded theme

### Approach

Periscope passes its palette to the iframe as a query param; LGTM applies it to
`document.documentElement` when embedded. Query param (not postMessage) so the
override lands in `main.tsx` *before* first render — postMessage arrives after
iframe load and guarantees a flash of LGTM's dark theme first.

This is option 1 from the discussion: **host-provided**, not a periscope theme
baked into LGTM. LGTM gains a generic "apply host palette when embedded"
capability; it does not learn periscope's specific colors.

### Periscope side (`static/modal.js`)

When building the iframe `src` (both `renderLgtmItem` and the walkthrough
view), append `&theme=<encodeURIComponent(JSON.stringify(palette))>`.

Build `palette` by reading periscope's *own* computed CSS variables and mapping
them to LGTM's variable names — no hardcoded duplicate color values in JS
(DRY against `styles.css`):

```js
function lgtmThemePalette() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    "--bg":           v("--bg-0"),
    "--bg-secondary": v("--bg-1"),
    "--bg-tertiary":  v("--bg-2"),
    "--border":       v("--line"),
    "--text":         v("--fg-1"),
    "--text-muted":   v("--fg-3"),
    "--accent":       v("--accent"),
    "--hover":        v("--bg-1-hi"),
    "--comment-bg":   v("--bg-2"),
    "--comment-border": v("--accent"),
  };
}
```

### LGTM side (`frontend/src/main.tsx`)

Inside the existing `if (isEmbedded) { ... }` block (after
`document.body.classList.add('embedded')`):

```ts
const themeParam = params.get('theme');
if (themeParam) {
  try {
    const palette = JSON.parse(themeParam) as Record<string, string>;
    const root = document.documentElement;
    for (const [k, val] of Object.entries(palette)) {
      if (typeof val === 'string' && k.startsWith('--')) {
        root.style.setProperty(k, val);
      }
    }
  } catch { /* malformed theme param — fall back to LGTM's default palette */ }
}
```

Setting custom properties on `documentElement` (inline style) overrides the
`:root` defaults in `style.css`; all `var(--*)` consumers pick up the new
values with no recompile.

Note on value form: `getComputedStyle().getPropertyValue('--line')` returns the
*declared* string — e.g. `color-mix(in oklch, oklch(...) 30%, transparent)` —
not a resolved color. That raw expression passes through the query param and
`setProperty` fine; it resolves at each `var()` use site. The only thing this
breaks is any LGTM code that string-parses a palette var as hex (none in the
overridden set). The param is small (~10 short values) and static — no URL
length concern. `encodeURIComponent` handles the parens/spaces cleanly.

### Variable mapping & scope of override

Override the **surfaces, text, borders, accent, and interactive states** — the
variables that visibly clash with periscope's chrome. Map (periscope → LGTM):

| LGTM var | periscope var | role |
|---|---|---|
| `--bg` | `--bg-0` | page background |
| `--bg-secondary` | `--bg-1` | panels / headers |
| `--bg-tertiary` | `--bg-2` | inset / chips |
| `--border` | `--line` | borders |
| `--text` | `--fg-1` | primary text |
| `--text-muted` | `--fg-3` | secondary text |
| `--accent` | `--accent` | links / highlights |
| `--hover` | `--bg-1-hi` | hover state |
| `--comment-bg` | `--bg-2` | comment box |
| `--comment-border` | `--accent` | comment box border |

**Deliberately not overridden** (keep LGTM's defaults): the semantic diff
colors — `--add-text/--add-bg/--del-text/--del-bg/--hunk-text` etc. Green-add /
red-del are review-domain semantics, tuned for diff legibility; periscope's
status palette (`--s-done`, `--s-danger`) is tuned for card pills, not diff
line backgrounds. Revisit only if they read wrong against the new surfaces.

---

## Cross-repo change summary

| Change | Repo | File(s) |
|---|---|---|
| `lgtm_register/add_item/add_comment/find_or_create_slug` helpers | periscope | `periscope/lgtm.py` |
| `review_add_doc` + `review_comment` tools | periscope | `periscope/channels.py` |
| Routes refactored onto shared helpers | periscope | `periscope/routes/lgtm.py` |
| Channel instructions + standalone-MCP note | periscope | `periscope/channels.py`, `CLAUDE.md` |
| Pass `&theme=` to iframe src | periscope | `static/modal.js` |
| Apply host palette when embedded | **lgtm** | `frontend/src/main.tsx` |

## Test strategy

- `tests/test_lgtm.py` — the new helpers, with `httpx` mocked: register,
  add-item, add-comment, find-or-create (cache-hit and create paths). This
  module currently has indirect coverage via `tests/routes/test_lgtm.py`;
  the helper refactor is a good moment to add direct coverage.
- `tests/test_channels.py` — `review_add_doc` / `review_comment` handlers:
  pane→cwd resolution mocked (`tmux`), helpers mocked; assert correct slug
  resolution, error on missing session for `review_comment`, tool-result
  shape on success/failure.
- Theme: browser-verified, not unit-tested (per periscope UI convention —
  the dev server + eyes are the oracle). Check: no FOUC, surfaces match,
  diff colors still legible.

## Open questions

1. **Gateway scope** — is `review_add_doc` + `review_comment` the right v1, or
   do you also want `review_start` (explicit claim, so Claude can surface the
   diff without the human clicking "+ Start review")? Leaning no (YAGNI;
   add-doc find-or-creates), but it's a cheap addition if wanted.
2. **`review_comment` batch vs single** — array (matches LGTM's MCP) vs one
   comment per call. Leaning array.
3. **Theme transport** — query param (chosen, no FOUC) vs postMessage (lets
   periscope re-theme live if its palette changes at runtime, which it doesn't).
   Confirm query param is fine.
4. **Override breadth** — keep semantic diff colors as LGTM's, or also map
   them to periscope's status palette? Leaning keep LGTM's.
