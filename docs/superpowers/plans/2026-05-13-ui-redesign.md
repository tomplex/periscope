# UI redesign — implementation plan

**Date:** 2026-05-13
**Source:** `~/Downloads/periscope.zip` → `design_handoff_periscope/`
**Status:** draft, awaiting Tom's input on phasing + scope-cuts before execution

## What the handoff is

A visual + interaction polish pass authored against the *actual* current stack:
single-file FastAPI + 6 vanilla JS modules + xterm.js, no build step. It ships
as a CSS token sheet + JSX reference components + a README that maps each
design change to the file(s) it touches and explicitly preserves the existing
invariants (state ladder, `focused_at` ordering, WS protocol, single-file
server).

Everything in the handoff fits the current architecture. None of it requires
a framework, a build step, splitting `server.py`, or touching `terminal.js`.

## What's actually in scope (handoff inventory)

| Piece | Where it lands | Server change? |
|---|---|---|
| Cool-gray oklch token palette | `styles.css` | no |
| New card markup (title/idx/status, meta, activity, foot w/ progress bar) | `grid.js#renderCard` | no |
| Split add-slot (claude / shell / vim) | `grid.js#renderNewTile` + new `mode=vim` | tiny (POST `/api/window/new`) |
| Top bar restyle + usage bars | `index.html` + `styles.css` | no |
| Filter chip restyle | `styles.css` | no |
| View switch (grid ↔ stream) | new `grid.js#renderStream` + `body.dataset.view` + persist | no |
| Modal header polish | `modal.js#updateModalHeader` | no |
| Modal sidebar — PR card | new `modal.js#renderModalSidebar` | yes (extend `cached_pr_state`) |
| Modal sidebar — Linear card | same | yes (new `cached_linear_state` — or `null` stub) |
| Modal sidebar — activity timeline | same | yes (new `cached_pane_activity`) |
| Tweaks panel (5 knobs) | new `app.js` panel + `:root[data-*]` switches | no |
| Reduced-motion wrap | `styles.css` | no |

## My read

**The handoff is good and the architecture-fit is real.** No regressions to
existing invariants, no framework creep, no protocol changes. The CSS token
sheet plus the card-markup rewrite alone delivers the headline visual lift
(README's own assessment: steps 1+2 = 80% of the polish).

**Two opinions before execution starts.** I want input on both before I touch
code:

1. **Push back on the Tweaks panel.** Five user-pref knobs (density / status
   treatment / border variant / tint cards / show usage) is a personalization
   surface that earns its complexity in multi-user products. Periscope has one
   user. Recommendation: pick the variant you actually like (presumably the
   default `density=compact`, `statusTx=current`, `border=leftbar`,
   `tint=false`, `usage=on`) and bake it in. If you later want to A/B between
   two variants, add `?density=cozy` as a URL toggle — that's two lines.
   Saves ~80 LOC of panel UI + a localStorage key + the conditional-CSS
   plumbing.

2. **Decide whether PR/CI stays on the card.** The new card markup drops
   PR # / CI glyph from the card and moves PR data into the modal sidebar.
   That's a meaningful scan-pattern change — today the grid is the place you
   notice "CI just broke on #3421." If you want it preserved, the new
   `card-meta` row needs a `{pr}` + `{ci}` segment alongside branch + clean/
   dirty. Easy add, but worth picking deliberately.

**One thing not in the handoff but adjacent:** there's a draft spec for
worktree integration (`docs/superpowers/specs/2026-05-13-worktree-integration-
design.md`) that affects the `+ claude` button semantics. The handoff's
`+ claude / + shell / + vim` split assumes the current behavior (no
worktree). If worktree-integration lands first, the add-slot split needs to
agree on what each button does — `+ claude` becomes "new worktree + claude",
`+ shell` and `+ vim` probably stay current-cwd. Flag it; doesn't block
this work, just needs coordination on order.

## Phasing

Four phases, each independently shippable. Hard recommendation: ship phase 1
on its own and let it bake for a day before moving on.

### Phase 1 — Visual lift (token palette + card markup rewrite + topbar markup rewrite)

**Goal:** ship the 80% polish from the README's step 1+2. No new features,
no server changes, no behavior changes. Pure look.

**Scope realism:** this is *not* a drop-in CSS swap. It's a renderCard
rewrite + an index.html topbar rewrite + a stylesheet retarget pass against
~12 design selectors that don't match current periscope class names. The
rename map in the handoff README is a guide, not 1:1. Concrete renames
needed:

| Current | Design |
|---|---|
| `.card-name` | `.card-title` |
| `.card-state` | `.card-status` |
| `.card-branch` | `.card-meta` |
| `.card-snippet` | `.card-activity` |
| `header` (bare) | `.topbar` |
| `header h1` | `.brand` (+ new `.brand-cursor` span) |
| `.filters button` (descendant selector) | `.filter-btn` (class on each button) |
| `.meta` | (split into `.usage` + `.meta` containers with `.count-needs` / `.count-working` / `.count-waiting`) |

Plus net-new selectors the design introduces: `.card-progress`, `.card-pct`,
`.card-model`, `.card-viewed`.

**Changes:**

- `static/index.html`:
  - Add `class="filter-btn"` to each `<button data-filter="…">` in the
    filter nav. (Existing app.js `#filters button` selector still
    matches, so JS untouched.)
  - Restructure the topbar to the new layout: `.brand` (with
    `.brand-cursor` span), `.usage` host, `.meta` with separate count
    spans.
- `static/styles.css`: full rewrite against the new token palette. Tokens
  go in `:root`; selectors target the periscope class names (per the
  table above), not the handoff's `ps-*` namespace. Hard-code three
  data attributes on `<html>`: `data-density="compact"`,
  `data-status-tx="current"`, `data-border="leftbar"`. (Skipping the
  Tweaks panel per pushback above; these become the baked-in variant.)
- `static/grid.js#renderCard`: rewrite the template to emit
  `.card-head` (title / idx / status), `.card-meta` (branch · dirty/
  clean · PR/CI if we keep them — see decision below), `.card-activity`
  (pending or last_line), `.card-foot` (progress bar fill, %, model,
  viewed). Keep all `data-target` / `data-session` attrs — delegated
  handlers untouched.
- `static/grid.js#updateUsagePill`: minor — emit the new
  `.usage-item` markup the design uses (the data is unchanged).

**Decision needed before starting:** keep PR/CI visible on the card or not.
Plan-reviewer confirmed dropping it is a real scan-pattern regression
(grid.js:46-48 today renders `#{pr}` + CI glyph inside the meta row).
Default: keep — append `{pr} {ci}` as a third segment of the new
`.card-meta` row. The PR data is already on the windows payload, no server
change needed.

**Acceptance:**

- Dashboard renders against `uv run server.py` with the new look and zero
  functional regressions (filter / collapse / drag-reorder / kill /
  rename / new-window / modal-open all behave identically).
- No new browser console errors.
- No new server changes; `git diff server.py` is empty.

### Phase 2 — Stream view + view switch

**Goal:** add the alternate "feed" rendering. The data layer is unchanged;
this is a second renderer keyed off `body.dataset.view`.

**Changes:**

- `static/grid.js`: add `renderStream(windows)` that emits the
  `.stream`/`.stream-row` markup. Sort by `(state-priority, focused_at
  desc)` to match the existing within-session order convention. Reuse
  `passesFilter` unchanged.
- `static/grid.js#render`: dispatch on `document.body.dataset.view`
  between grid and stream renderers.
- `static/index.html`: add the view-switch pill to the filter bar.
- `static/state.js`: add `periscope:view` persisted key with `"grid"`
  default + `loadView()` / `saveView()` helpers. Run through the existing
  `migrateOldKey` pattern (no-op since the key is new).
- `static/app.js`: wire the pill → set `body.dataset.view` + persist +
  re-render from `state.lastWindows`.

**Acceptance:**

- Toggle persists across reloads.
- Stream view click on a row opens the modal for that target.
- Empty-state ("no windows match") renders in both views.

### Phase 3 — Modal sidebar (PR + activity timeline; Linear deferred)

**Goal:** add the right-hand sidebar with PR card + activity timeline. Linear
card ships as `null` (the `+ link Linear ticket` button is purely visual until
the connector exists).

**Phase split:** the handoff lists 6 timeline `kind`s (commit / push / ci /
turn / prompt / open). Phase 3 ships only **commit / ci / open** — the three
where the data source already exists or is a clean shell-out. Phase 3.1 adds
**push / turn / prompt** as a follow-up, because they need net-new parsing
(`prompt`/`turn` require scanning the full pane capture for ❯-bounded
turn blocks; `parse_pane`'s `PROMPT_LINE_RE` today only locates the most
recent prompt line, not a boundary list).

**Changes (Phase 3):**

- `server.py#pr_state_for`: extend the existing `gh pr list --head <branch>
  --state open --json …` call. Add `title,isDraft,additions,deletions,
  reviewRequests` to the `--json` field list. Keep the `prs[0]` indexing —
  *do not* switch to `gh pr view` even though the handoff suggests it; the
  current `list`-based call is the right shape, it just needs more fields.
  Return existing `{pr, ci}` plus `{pr_title, pr_draft, pr_additions,
  pr_deletions, pr_reviewers}`. Same TTL (60s), same `cached_pr_state`
  fetch path.
- `server.py`: new `cached_pane_activity(target, cwd, branch)` →
  `[{kind, at, text}]` sourced from:
  - `git log -10 --since=24h --pretty=format:%ct%x09%s` for `commit` events,
  - `gh run list --branch <branch> --limit 5 --json …` for `ci` events,
  - `focused_at` for `open` events.
  Cap at 8 events, sorted desc by `at`. Stale-while-revalidate cache
  pattern lifted from `cached_pr_state` (60s TTL).
- `server.py#pane` (`/api/pane`): include the new PR fields and
  `activity` array in the response.
- `static/index.html`: split the modal body into `modal-main` (existing
  xterm host) + new `modal-side` aside, width 300px.
- `static/modal.js`: new `renderModalSidebar(state)` called from
  `updateModalHeader`. Renders PR card (or `+ link` button), Linear card
  (always the placeholder for now), activity timeline.

**Changes (Phase 3.1 — separate ship):**

- `server.py`: add `find_prompt_boundaries(capture)` that walks every line
  of the buffer, returns `[(line_idx, "prompt" | "turn"), …]`. A prompt
  is a `❯`-prefixed line with non-empty content after it; a turn is the
  span between two prompts where assistant output appears (heuristic:
  any non-`❯` line that isn't pure whitespace and isn't the status
  block). Cap to last N turns; this is genuinely new parsing logic, not
  a tweak.
- Extend `cached_pane_activity` to emit `prompt` and `turn` events from
  `find_prompt_boundaries`. Push events from `git reflog` if we want them.

**Acceptance (Phase 3):**

- Opening any modal with an active PR shows PR title / +N -M / draft /
  reviewers / CI dot inside 3 seconds (one poll cycle).
- Modal with no PR shows the placeholder button.
- Activity timeline shows commit / ci / open events; ≤8 entries,
  scrolls when longer.
- xterm host width adjusts; `ResizeObserver(scheduleFit)` in
  `terminal.js:79` already wires the fit-addon to react to host shrink,
  so this should be automatic — verify in the browser.

**Width risk (corrected):** `.modal-card` is `width: min(1400px, 95vw)`
(`styles.css:540-548`), not 1100px. On a 1440px screen → 1368px modal →
~1040px terminal after a 300px sidebar. Usable. On viewports below
~1100px, add a media query that collapses the sidebar to a bottom drawer
(or hides it). Scope: one media query in Phase 3.

### Phase 4 — `+ vim` button + worktree alignment

**Goal:** ship the third add-slot button.

**Changes:**

- `server.py#window_new`: add `mode=vim` branch. Three-line addition
  alongside the existing `mode=claude` branch at line ~892:
  `elif mode == "vim": time.sleep(0.1); tmux("send-keys", "-t", target,
  "vim", "Enter")`.
- `static/grid.js#renderNewTile`: emit the split add-slot markup
  (`+ claude` primary, `+ shell` and `+ vim` stacked).

**Acceptance:**

- `+ vim` button spawns `vim` in the new window's cwd; window appears in
  next `/api/state` poll.

**Coordination with worktree-integration:** the worktree spec
(`docs/superpowers/specs/2026-05-13-worktree-integration-design.md`)
defines a **separate** endpoint `POST /api/window/new-worktree`, not a
`mode=worktree` branch on `/api/window/new`. If worktree-integration
lands first:

- `+ claude` posts to `/api/window/new-worktree` (different shape, returns
  `worktree_path`),
- `+ shell` and `+ vim` continue posting to `/api/window/new?mode=…`.

That means `renderNewTile` needs a `data-endpoint` (or `data-flow`) attr
per button, and `handleNewWindow` (`grid.js:335-352`) needs a switch on it.
Also: the worktree spec hides the `+ claude` button entirely on
non-repo sessions (worktree spec lines 71-73), which means we need a
`session_is_repo` flag on the windows payload — not currently exposed.

Recommendation: land worktree-integration first, then Phase 4 lands the
add-slot split with full awareness of both endpoints. Or land Phase 4
first against the current single endpoint and let the worktree work pick
up the `+ claude` rewire later.

## Things explicitly out of scope

Per the README's "What this design does *not* do" and the pushback above:

- No tab strip inside the modal (the terminal stays the only modal-body
  interface; matches existing model).
- No follow-up textbox / slash-command palette / wrapper-side input.
- No new WebSocket; sidebar data rides the existing 1.5s `/api/pane` poll.
- No `last_activity_at` or any pane-output-driven ordering signal —
  `focused_at` remains the sole ordering input.
- **No Tweaks panel.** Pick a variant, bake it in.
- **No Linear MCP wiring in Phase 3.** Sidebar shows the `+ link` button
  until a Linear connector exists.

## Open questions

1. PR/CI on card: keep or drop? (default: keep — plan-reviewer confirmed
   it's a real scan regression to drop.)
2. Tweaks panel: confirm scope-cut? (default: cut — plan-reviewer confirmed
   no codebase dependency.)
3. Phase 4 vs worktree-integration order: which lands first?
4. Activity timeline freshness: is a 30–60s TTL acceptable, or should it
   piggyback the 1.5s modal poll (= 8 git/gh commands every 1.5s per open
   modal, expensive)? Default: 60s TTL with background refresh, same
   pattern as `cached_pr_state`.
5. Phase 3 vs Phase 3.1 split: ship Phase 3 (commit/ci/open) alone, or
   wait until 3.1 (prompt/turn) is ready before any sidebar lands?
   Default: ship 3 alone; the timeline is useful with just git+ci even
   without per-turn detail.

## Suggested next step

If you're aligned on the phasing + the two scope-cuts, I'll start phase 1.
Phase 1 is independently shippable in a single sitting — token sheet +
`renderCard` rewrite + verify against the live dashboard. No commits to
main until you've eyeballed it in the browser.
