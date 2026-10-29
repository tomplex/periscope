# Review queue + native walkthrough

A "Reviews" surface in periscope listing every branch waiting on review, and a
native stop-by-stop walkthrough per branch — structural claims with the hunks
as evidence — authored by the pane that owns the branch, commented on from
periscope, with comments landing back in that pane. Replaces the LGTM
integration and, with the render-time doc enrichment it introduces, the
doc-enricher pipeline.

## Smallest version

**Problem:** After a sprint, the PRs waiting on me — mine to land, and
others' to review — aren't visible in one place, and reviewing one means
reading code instead of a structural tour whose feedback reaches the pane
that wrote it.

**Seams:** four, all present today —
- review surface: the rail's per-track `review` row + its detail view (one
  producer `reviewRowFor` at Rail.jsx:494, one renderer `ReviewDetail` at
  Detail.jsx:520; gated today on an LGTM session)
- document rendering: `renderMarkdown` — 3 call sites (Transcript.jsx:201,
  :208; PreviewTabInner.jsx:598)
- message into a pane: `_deliver(pane_id, message, sender)` (channels.py:1139);
  an HTTP route already calls the same push (`POST /api/channel/push`)
- branch hunks: `gitdiff.diff_for(repo, base)` — what `/api/fs/diff` and the
  Changes tab render; pure in the repo path

**Minimal version (~30 files + dist; 7 new, ~23 edited, plus removals and
tests):** the review row appears per branch when a walkthrough file exists
and opens a native stop-by-stop view; a "Reviews" selection in the detail
area lists every branch with its PR/CI/walkthrough/comment state; comments
append to a sidecar file and are delivered to the owning pane; prepare/merge
are messages to that pane, never git operations periscope runs.

Left out, and what it costs:
- refs whose lines moved after the tour was written → chip renders "no hunk";
  recovery: prepare again
- a merge that fails (conflict, red CI) → reported by the pane in its
  transcript, not on the queue row
- a walkthrough whose worktree is gone → the row shows the tour and comments
  but no hunks; prepare is disabled; recovery: open the branch (⌘K)
- comments are matched to stops by title → a re-prepare that retitles a
  stop orphans its comments (they still show at the bottom, unattached)
- two repos with the same basename share one reviews dir
- the dev backend never binds the MCP socket, so at :17375 every comment /
  prepare / merge returns 409 (the file write still happens); delivery is
  verified in pytest with `_deliver` patched, and once in prod after merge

Accepted consequences:
- the queue polls only repos periscope knows (the unified-open catalog's
  discovered repos); a repo never opened in periscope is not in the queue
  until it is opened once
- the tour can be stale — the file records the head it was written at; the
  view says "N commits since" and leaves it to you
- walkthroughs and comments live outside the repo, so they are not shared
  with the team
- mermaid is a ~3.4 MB chunk of its own (minified), loaded on first diagram
- a merged branch's review dir stays until you click **clear** on its row —
  the one file deletion periscope performs, on an explicit click

Additions:
- **picked** +2 files → removes "no live pane on the branch": prepare spawns
  a Claude in the branch's worktree on the repo's default track, with the
  prepare prompt delivered the way `spawn_claude` already does it. Requires
  the worktree to exist on disk; otherwise prepare is disabled with
  "open the branch first"
- not picked → periscope runs the merge itself (moves a git mutation across
  the trust boundary for one click saved)
- later, own spec → decision log: same renderer, a `decision` convention
  beside `stop`

Dropped:
- a periscope DB table for walkthroughs/comments — the files are the store
- stacked-PR modeling — not in use
- a per-window `walkthrough` field in the state blob — the reviews payload
  is its own fetch on its own cadence (D6)

File budget: 32. Stop and report at 64. Removals and tests don't count.

## Decisions

### D1. Periscope never mutates git or GitHub

Prepare, re-prepare, merge, and "address comments" are all messages to the
pane that owns the branch. The dashboard stays a lens; the pane is the actor.

One carve-out: prepare on an open PR with no pane and no checkout fetches the
PR into a worktree with ⌘K's PR helper (local branch `pr-<N>`, the row's
alias; the spawned pane is stamped with the PR like a ⌘K open) and spawns the
pane there. This holds for my own PRs too — the branch helper forks a new
branch off the default, which is wrong for one that already exists. Creating
a worktree is the only git mutation prepare makes; it still never commits,
pushes, or merges.

**A review belongs to a (repo, branch) pair, not a track.** Tracks are not
branches — a repo-default track holds every worktree of the repo, and the
rail already derives branch sub-clusters inside a track. The **owning pane**
of a review is the live window whose `(repo_label, branch)` match, whose
`agent` is `claude`, and whose channel is attached; with several, the most
recently active. No such window → no owner; the row says so.

### D2. The walkthrough is a file on disk, not periscope state

`~/.claude/reviews/<repo>/<branch-slug>/walkthrough.md` plus `comments.md`
beside it. Outside the repo: no gitignore or exclude dance, nothing the team
sees, and it survives worktree removal — a cleaned-up branch still shows its
tour until you clear it. The authoring skill owns the path; periscope reads
it by convention.

- `<repo>` is periscope's `repo_label`: the basename of the realpath of the
  parent of `git rev-parse --git-common-dir`. That is the main checkout's
  name even from inside a linked worktree (`.claude/worktrees/x` → the repo,
  not `x`). The skill derives it with exactly
  `basename "$(realpath "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")")"`.
- `<branch-slug>` is the branch with every `/` → `--`.

One writer per file: the pane writes `walkthrough.md`; periscope only ever
appends to `comments.md`; the pane edits `comments.md` only to add a reply
paragraph inside an entry and flip its `resolved`. Periscope removes the
whole dir only on the row's **clear**. Periscope is also the only writer of
`reviewed.json` (per-stop reviewed marks, THI-443).

### D3. Markdown with three conventions, each degrading to plain markdown

```markdown
# <title>

> branch: feat/x · head: 3f2a1c9 · pr: 1234
> repo: fdy

<one-paragraph problem statement, no code vocabulary>

## Adds a per-pane cost-pressure gauge
> kind: new-subsystem · refs: periscope/cost_pressure.py, static/src/split/ctxBand.js:1-40

<prose: what it is, what it owns, what it depends on>

```mermaid
flowchart LR
  poll --> ctxBand --> Rail
```

## Reuses the PR cache for CI state
> kind: reuse · refs: periscope/git_pr.py:223-276

<prose: which existing thing, and why it is the right one>
```

- **Metadata blockquote:** a blockquote whose only child is one paragraph
  whose children are all plain text, split into pairs on ` · ` **and on
  newlines**, every pair matching `^[a-z-]+: .+$`. Anything else is an
  ordinary blockquote (so a superpowers plan's `> **For agentic workers:**…`
  under its H1 is not a header).
- **Header block:** the metadata blockquote directly after the H1. Required
  `branch`, `head`. Optional `pr`, `repo`.
- **Stop:** every `##` heading. The metadata blockquote directly under it
  carries `kind` (one of `new-subsystem`, `extension`, `reuse`,
  `system-touch`, `behavior`; an unknown value is kept as a plain chip) and
  `refs` (comma-separated `path` or `path:a-b`). A `##` without one is still
  a stop, with no kind and no refs.
- **Diagrams:** ` ```mermaid ` fences.
- **Citations:** `path` / `path:line` / `path:a-b` tokens in prose text or in
  backticks, where the path contains a `/` or ends in `.<letters>` before
  the colon (`127.0.0.1:17374` does not match: the segment before `:` ends
  in a digit).

Stops are ordered by damage-if-wrong; the file order is the tour order.

### D4. Enrichment happens at render time, for every document

The shared markdown renderer gains three opt-in behaviors — mermaid fences →
SVG, a metadata blockquote → a chip row, citation tokens → chips. The
transcript skin leaves them off and renders exactly as today. The preview
tab turns them on for every `.md`, so specs get diagrams and chips with no
second artifact. That is what retires the doc-enricher: the author writes
one markdown file; the look is periscope's job and improves for all docs at
once. Mermaid is its own vite chunk, loaded by dynamic import on the first
fence.

Accepted consequences of "every document": any blockquote directly under a
heading whose lines are all `key: value` (e.g. `> note: keep this`) renders
as a chip row, not a quote; a slashless file name chips only with a `:line`
suffix (`reviews.py:12`), so bare `os.path` / `node.js` in prose stay text,
while slashed repo-relative paths chip bare.

### D5. The walkthrough view is the renderer plus three things

Stops as cards (kind badge, anchor), the referenced hunks from the branch
diff collapsed under each stop — rendered by the Changes tab's hunk
component (extracted from its file block; intraline highlighting included;
the file-header click is disabled here because there is no active pane
target) — and a comment box per stop. A staleness chip when the worktree's
HEAD differs from the header's `head`. Hunks come from the branch's worktree
path, not a pane, so a branch with no live pane but a worktree on disk still
shows them.

> **Layout superseded by `2026-10-29-walkthrough-two-column-design.md`
> (THI-443):** the hunks now sit expanded in a column beside each stop's
> prose, diagrams render in a full-width row above it, the comment box is
> collapsed until used, and per-stop reviewed marks live in `reviewed.json`
> (`POST /api/reviews/reviewed`).

### D6. The Reviews queue is a detail-area selection, one row per (repo, branch), sourced from GitHub

**The queue answers "what's waiting on me," not "what's running in
periscope."** Its primary source is GitHub: every open PR you authored and
every open PR where your review is requested, across the repos periscope
knows (the unified-open catalog's discovered repos). Live panes, worktrees
on disk and walkthrough files are joined onto those rows, not the rows
themselves — after a reboot or a cleanup the panes are gone but the PRs are
not, and a teammate's PR has no pane or local branch at all. Two secondary
groups follow: local branches with a live pane and no PR (sprint work not
yet pushed), and `~/.claude/reviews/<repo>/<slug>` dirs matching neither.

Each row carries a **role**: `mine` or `review-requested` (with the author).
Columns: branch + repo + role/author, PR (number, then the row's Linear
ticket when one is known — alone when there's no PR — then draft, state
incl. merged, CI glyph), walkthrough (none / N stops / stale by N), comments (M
unresolved), who (the owning pane's name, else "opened by <author>", with a
"no pane" / "not checked out" state line). PR numbers link to GitHub; the
URL comes from the origin remote, so header-only rows link too. Sort is
readiness-major (a search box in the header then filters the sorted rows
client-side — THI-442, `2026-10-27-reviews-queue-improvements-design.md`
D4/D5): rows with a live pane, then rows with a worktree on disk,
then neither; merged last. Within a tier: unresolved comments, then
review-requested, then PR-without-walkthrough, then the rest, then
local-no-PR; then PR number desc, then branch.

Actions: **prepare** (message the owner to run the `walkthrough` skill; with
no owner but a worktree on disk, spawn one there; with neither but an open
PR, check the PR out first — the D1 carve-out — then spawn; a row with
neither and no open PR is disabled with "open the branch first (⌘K)"), **merge**
(message the owner; disabled with the reason when none), **clear** (remove
the review dir; offered when the PR is merged or the branch has no
worktree). Row click selects that branch's review row.

The PR lists are fetched with `gh` per known repo, cached 60 s with
stale-while-revalidate like the existing PR cache; with `gh` absent or
offline the queue degrades to the secondary groups.

**Rail placement:** the review row sits inside the branch sub-cluster when
the track spans ≥2 branches, else at the top of the track body — one row
per branch with a walkthrough, keyed `review:<repo>/<slug>`.

**Cadence:** the reviews payload is one `GET /api/reviews` on a 10 s timer
in the frontend (immediate on selecting the queue, on a comment post, and
on prepare/merge/clear), held in a `reviews` signal that the rail, the
header badge and the queue all read. It is not part of the state push. The
route reads the last published state snapshot (`state_hub._last_blob`) for
live windows — it never recomputes the blob.

### D7. Comments are an append plus a channel message

A comment appends one entry to `comments.md` and delivers the same text to
the owning pane as a channel message naming the stop:

```markdown
## 2026-10-26T09:14 · Adds a per-pane cost-pressure gauge
> resolved: no

Why does this bypass the signals store? The rail reads it straight from poll.
```

The pane acts, appends a `Reply (<head>): …` paragraph inside the entry, and
flips `resolved: no` → `yes` once addressed. The queue counts unresolved
entries; the walkthrough view lists each stop's comments under it. With no
owner the append still happens and the response says it was not delivered
(409); the next pane to prepare the branch is told to read `comments.md`.

### D8. The LGTM integration is removed in this change

It is the thing being replaced; leaving it means two review rows. Removed:
`lgtm.py`, `routes/lgtm.py`, the lgtm tasks/router in `app.py`, the
`cached_lgtm_state` uses in `routes/pane.py` and the `lgtm` field in
`window_view.py`; `ReviewDetail`/`ReviewStart` and the iframe in
`Detail.jsx`; `rewriteLgtmHost` in `util.js`; the `review` notify action
(`notify/actions.js`, `alertFeed.js`); the `.detail-review*`,
`.review-start-*`, `.icon-review*` CSS; the "add to LGTM" comment in
`terminalCore.js`; `test_lgtm.py`, `tests/routes/test_lgtm.py`, the lgtm
patches in `tests/routes/test_pane.py`, and the lgtm cases in
`test_window_view.py`, `test_app.py`, `tests/routes/test_state.py`;
`docs/lgtm.md`, the CLAUDE.md row and the Review-tab mention. The persisted
`{kind: "review", worktree}` selection is dropped on restore (it named an
LGTM session, which no longer exists).

### D9. The doc-enricher retires separately

After the renderer lands and one spec has been read through it: delete the
`doc-enricher` agent, `~/.claude/tools/doc-enrich`, and the "Document review"
section of the operator prompt; `open_document` the `.md` directly. Outside
this repo, so outside this spec's budget.

## Mechanics

### Server

**`periscope/reviews.py`** (new; no HTTP)

- `REVIEWS_DIR = ~/.claude/reviews`. Not periscope state, so prod and dev
  read the same tree (no `instance_file` split).
- `slug_for(branch)` — `/` → `--`; `review_dir(repo_label, branch)`,
  `walkthrough_path(...)`, `comments_path(...)`.
- `parse_header(text)` → `{branch, head, pr, repo}` from the metadata
  blockquote after the H1 (same pair rule as D3: split on ` · ` and `\n`).
- `count_stops(text)`; `parse_comments(text)` → `[{ts, stop, resolved,
  body}]`; `unresolved(text)`.
- `append_comment(repo_label, branch, stop, body, ts)` — append-only;
  creates the file if absent.
- `clear_review(repo_label, branch)` — `shutil.rmtree` of that one dir.
- `owner_for(windows, repo_label, branch)` → the window per D1 or `None`.
- `worktree_for(windows, repo_label, branch)` → a path: the owner's cwd's
  repo root if any window matches, else `worktrees.for_branch(repo_root,
  branch)` where `repo_root` is any live window's `repo_key` for that
  label; `None` when neither.
- `stale_by(worktree, head)` → `git rev-list --count <head>..HEAD`, cached
  by `(worktree, head, current HEAD sha)`; `None` without a worktree.
- `open_prs(repo_root)` → `[{number, title, branch, draft, ci, role,
  author}]` from two `gh pr list --state open` calls in that repo (`--author
  @me`, and `--search "review-requested:@me"`), deduped by number; `role`
  is `mine` for the first list. `cached_open_prs(repo_root)` is the 60 s
  stale-while-revalidate wrapper (background `_bg` fetch on miss/stale,
  same shape as `git_pr.cached_pr_state`); `[]` when `gh` is absent.
- `list_reviews(windows, repos, team_keys)` → rows `{repo, branch, slug,
  role: "mine" | "review-requested" | "local", pr: {number, title, draft,
  state, ci, author, url} | None, walkthrough: {stops, head, stale_by} |
  None, unresolved, comments, owner: {pid, pane_id, session, index, name} |
  None, worktree: path | None}` plus the pane payload's five Linear ticket
  fields (`linked_linear`, `linked_linear_title`, `linked_linear_status`,
  `linear_ticket`, `linear_ticket_source`), built by
  `git_pr.linear_ticket_fields` like a pane payload's: the owner pane's
  link, else what the row's own branch and PR title name (the owner's PR
  title when the row has none and its PR is the row's) — decided here,
  never in the browser (THI-442,
  `2026-10-27-reviews-queue-improvements-design.md` D1). `team_keys` is
  `settings.linear_team_keys`, passed in by the route. `repos` is the list
  of known repo roots (the route passes `open_ops.discover_repos()`);
  `repo` is each root's basename. Row order
  of assembly: PRs per repo, then live `(repo_label, branch)` pairs with no
  PR (`role: local`), then orphan review dirs. A row with a walkthrough
  header `pr:` not in the open lists uses `cached_linked_pr_state` (sees
  merged).

**`periscope/routes/reviews.py`** (new) — all `async def`, blocking work in
`asyncio.to_thread`:

- `GET /api/reviews` → `{rows}` from `list_reviews(windows of
  state_hub._last_blob or [], open_ops.discover_repos())`.
- `GET /api/reviews/walkthrough?repo=&branch=` → `{text, comments, head_now,
  stale_by, diff}` where `diff` is `gitdiff.diff_for(worktree,
  gitdiff.branch_base(worktree))` or `None` with no worktree; 404 with no
  file.
- `POST /api/reviews/comment` `{repo, branch, stop, body}` → append, then
  `_deliver(owner.pane_id, msg, "periscope")`; 409 `{detail: "not
  delivered: no owning pane"}` when no owner (append already done).
- `POST /api/reviews/prepare` `{repo, branch}` → owner: `_deliver` the
  prepare prompt; no owner + worktree: `channels.spawn_claude_with_prompt
  (cwd=worktree, track_id=tracks.repo_default_track(repo_root), prompt)`;
  neither: 409.
- `POST /api/reviews/merge` `{repo, branch}` → `_deliver` the merge prompt;
  409 with reason when no owner.
- `POST /api/reviews/clear` `{repo, branch}` → `clear_review`.

Prompts. Prepare: "Run the `walkthrough` skill for branch `<branch>` and
write it to `<walkthrough_path>`; read `<comments_path>` first if it exists."
Merge: "This branch has been reviewed in periscope. Read `<comments_path>`
first: if every entry is `resolved: yes`, merge it by this repo's convention
and report; otherwise reply with what is still open and do not merge."

**`periscope/channels.py`** — the spawn-then-deliver-prompt sequence inlined
in `_do_spawn_claude_tool` (window_new, wait for the consent dialog and TUI
mount, paste the prompt) is extracted into `spawn_claude_with_prompt(cwd,
track_id, prompt, *, name=None)`; the tool calls it. Behavior unchanged for
the tool.

**`window_view.py`**, **`routes/pane.py`**, **`app.py`** — lgtm removed;
reviews router added.

### Frontend

**`split/markdown.jsx`** — `renderMarkdown(text, {mermaid, meta, cite})`:
- the top-level walk gains sibling access so a blockquote can know the
  previous block was a heading;
- `meta: true` → a blockquote meeting the D3 rule, directly after a heading,
  renders `<div class="md-meta">` of `<span class="md-meta-kv"
  data-key=…>` chips; `refs:` values additionally pass through `cite`;
- `mermaid: true` → a `mermaid` fence renders `<Mermaid code>`; otherwise
  the plain fence;
- `cite: (ref) => vnode | null` → text nodes and `inlineCode` nodes matching
  the D3 citation rule are offered; `null` keeps the text.

**`split/Mermaid.jsx`** (new) — `import("mermaid")` on first mount (memoized),
`mermaid.render` into inline SVG, always `theme: "dark"` (the dashboard has no
light theme) with `securityLevel: "strict"`, parse error shown as text under
the fence. **`vite.config.js`** — `manualChunks` returns `"mermaid"` for any
module reachable only through the mermaid import graph (`mermaidOnly(id,
getModuleInfo)` walks importers; a module also imported from app or preview
stays where it was) so mermaid never lands in `app` or `preview`.
`package.json` gains `mermaid`.

**`preview/PreviewTabInner.jsx`** — the `.md` branch passes `{mermaid,
meta, cite}` with a chip that opens the file tab at that line.

**`diff/ChangesTab.jsx`** — `Hunk` and `Line` extracted and exported with
props `{hunk, lang, hl}`; `FileBlock` uses them. No behavior change.

**`review/walkthrough.js`** (new, pure) — `parseWalkthrough(text)` →
`{header, problem, stops: [{id, title, kind, refs, bodyMarkdown}]}`;
`matchRefs(refs, diffFiles)` → hunks per ref (`path` → every hunk in that
file; `path:a-b` → hunks whose new-side range — `new_start` plus the count
of `ctx`+`add` lines — intersects `[a, b]`); `sortRows(rows)` per D6.

**`review/Walkthrough.jsx`** (new) — fetches `/api/reviews/walkthrough`;
renders header + staleness chip, problem, then each stop: badge row, prose
via `renderMarkdown` (all three opts on; the cite chip scrolls to the hunk
when matched, else opens the file tab), matched hunks collapsed via `Hunk`,
the stop's comments, and a comment box posting to `/api/reviews/comment`
(a 409 shows "saved, not delivered — no pane on this branch").

**`review/ReviewsQueue.jsx`** (new) — the D6 table from the `reviews`
signal, with prepare/merge/clear.

**`store.js`** — `reviews` signal + `refreshReviews()`; **`poll.js`** — the
10 s timer (started with the state loop, stopped with it).

**`split/Detail.jsx`** — lgtm views go; `review:<repo>/<slug>` renders
`<Walkthrough>`; `reviews` renders `<ReviewsQueue>`.

**`split/Rail.jsx` / `RailRows.jsx` / `railTree.js`** — review rows per D6
placement from the `reviews` signal; `selectKey` persists `{kind:
"reviews"}` and `{kind: "review", repo, branch}`; **`main.jsx`** restores
both and drops the legacy `{kind: "review", worktree}`.

**`chrome/Header.jsx`** — "Reviews" button with the unresolved-count badge;
**`open/classify.js` + `overlays/OpenOmnibox.jsx`** — a `reviews` entry.

**CSS** — `md-meta`, stop cards, queue table, staleness chip; the LGTM
rules removed.

### Skill

**`~/.claude/skills/walkthrough/SKILL.md`** replaces `review-map`. Same
comprehension stance (problem statement with no code vocabulary; ranked by
damage-if-wrong; written from the diff and live code, never from the PR
body), but the output is D3's format at D2's path, with the repo derivation
one-liner verbatim. It carries: the five kinds with a one-line test for
each; the rule that a stop is a structural claim and refs are evidence, not
prose; "reuse" stops must name the existing thing and why it is the right
one (the #8867 check); a mermaid diagram on any stop that introduces or
crosses a boundary; and the closing step — read `comments.md`, address each
entry with a reply paragraph, flip `resolved`. Out of this repo's file
budget; the plan carries it as its own task.

### Tests

vitest:
- `review/__tests__/walkthrough.test.js` — header parse (` · ` and newline
  pairs), stop parse with and without a metadata blockquote, unknown kind
  kept, ref matching (whole file, intersecting range, no match),
  `sortRows`.
- `split/__tests__/markdown.test.js` — the meta blockquote only fires
  directly under a heading, only with `meta: true`, and not for a
  bold-prefixed blockquote; a mermaid fence is a plain fence with `mermaid`
  off; `cite` receives path tokens from text and inlineCode and not
  `127.0.0.1:17374`.
- `diff/__tests__` — `Hunk`/`Line` render the same lines as before
  (snapshot of one hunk).

pytest:
- `tests/test_reviews.py` — slug/path mapping, header/comment parsing,
  `append_comment` creates and appends, `owner_for` picks the attached
  claude window on the right branch (not another branch on the same
  track), `open_prs` parses both gh lists and assigns roles (patched
  `_run`), `list_reviews` assembles PR rows (patched `cached_open_prs`)
  then local-no-PR pairs then orphan dirs, `stale_by` against a temp git
  repo and its cache.
- `tests/routes/test_reviews.py` — comment route appends then delivers
  (patch `_deliver`, assert pane_id and message), 409 path still appends;
  prepare with no owner but a worktree calls `spawn_claude_with_prompt`
  (patched) with that cwd and the repo-default track; merge with no owner
  is 409; clear removes the dir. Windows come from a patched
  `state_hub._last_blob`; no state blob is computed, no threads spawn.
- removals per D8.

Browser check at :17375 with a hand-written walkthrough for this branch:
rail row placement under a multi-branch track, the queue sort, mermaid
rendering and chunk loading (network tab: `mermaid-*.js` only on first
fence), chips, hunks under stops, comment append (409 expected on dev).
