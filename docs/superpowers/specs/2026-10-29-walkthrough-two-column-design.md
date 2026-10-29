# Review walkthrough: two-column stops, legible diagrams, reviewed marks (THI-443)

The individual walkthrough view (`static/src/review/Walkthrough.jsx`) gets a
new layout and a type and color pass. Each stop becomes a card with its
diagrams across the full width, its text on the left at a readable line
length, and its files and code changes in a column on the right. The page
gains a fixed header, a contents list, per-stop "reviewed" marks saved in
the review folder, and a quieter comment box. Builds on
`2026-10-26-review-walkthrough-design.md`. D1 (periscope never mutates git
or GitHub), D2 (review state is files under `~/.claude/reviews/<repo>/<slug>`,
one writer per file) and D3 (the walkthrough.md format) are unchanged; this
replaces D5's layout.

## Problem

Measured in the running app on a 13-stop walkthrough (2026-10-29; the full
list is THI-443's 22 findings):

- **Text is a narrow centered column.** `.md-doc` (shared with file previews)
  sets `max-width: 72ch; margin: 0 auto`, and `.wt-stop-body` raises it to
  80ch. The body is 672px wide inside a 2085px card, centered, while the
  title, refs, hunks and comment box are full-width and left-aligned. The
  problem statement is a third column: 72ch, left-aligned, 13px.
- **Diagrams are unreadable.** Mermaid's `width="100%"` attribute fits each
  diagram to that 672px column (its inline `max-width` is the natural width,
  so the stylesheet's `max-width: 100%` never applies). The widest is naturally 2274px, so
  it draws at 30% scale with labels around 5px. It never grows and can't be
  opened larger. They also use mermaid's stock dark theme (Trebuchet MS,
  near-black nodes), not periscope's fonts or colors.
- **Code changes are hard to reach.** A wall of full paths in small mono
  comes before the text, a small "▸ 3 hunks" toggle sits below it, and
  expanding it lists every cited file, most reading "— not in this diff".
  Clicking a path opens all hunks rather than going to that file's.
- **No way through a long walkthrough.** No contents list or stop numbers,
  the header scrolls away, and nothing records which stops you've read.
- **Typography and color.** Eleven font sizes (10–16px; stop titles are
  smaller than body text), four kinds of text below 4.5:1 contrast, the
  staleness warning in the same grey as the repo name, stop kinds visible
  only in a 10px badge, inline code brighter than the prose around it, and
  cards with no fill. The problem statement drops its markdown.
- **Comment boxes everywhere.** Every stop has an open textarea and a
  disabled button drawn in the browser default (1.4:1). ⌘↩ posts, unannounced.

## Decisions

### D1. Diagrams are lifted out of a stop's text by the parser

`parseWalkthrough` already slices each stop out of the source by mdast
positions. Each stop now also returns `diagrams`: the code of every
**top-level** ```` ```mermaid ```` fence in its body, in order. Its
`bodyMarkdown` is the body with exactly those fences cut out (by their
`position` offsets). A mermaid fence nested inside a list or blockquote is
not lifted. It stays in the text and renders inline as today, because
moving it out of its list item can't be done without reordering content.

Rejected: moving diagrams with CSS (turning the text container into grid
items is fragile with an unknown number of paragraphs), and a "collect" mode
in the shared renderer (couples `renderMarkdown` to one page's layout).

The problem statement comes back as `problemMarkdown` (its source slice) in
addition to the existing plain-text `problem`, and renders as markdown.

### D2. Page structure: a fixed header over one scrolling body

`.walkthrough` becomes a flex column:

- **Header** (`.wt-head`, doesn't scroll): the title at 22px, then
  repo · branch · PR link at 12px. Beside them:
  - a **staleness badge**: "stale: N commits" in `--s-needs` when
    `stale_by > 0`; a neutral "no worktree" badge when the walkthrough has a
    header but no diff; "stale: unknown" in `--s-needs` when there is a diff
    but `stale_by` is null (the recorded head was rebased away, which the
    queue also calls "stale: unknown"); nothing when current;
  - **progress**: "4 / 13 reviewed" (D7).
- **Scrolling body** (`.wt-scroll`, `overflow: auto`,
  `container-type: size; container-name: wt`). Everything below lives here.

The top of the body uses the same two-column grid as a stop: the problem
statement on the left (15px, the prose measure), a numbered **contents list**
on the right. Each entry is the stop number, a dot in its kind's color, the
title, and ✓ once reviewed. Clicking an entry calls `scrollIntoView` on the
stop (buttons, not `#` links, so the URL and periscope's routing are
untouched).

### D3. A stop is a card: title row, diagram row, then two columns

```
┌──────────────────────────────────────────────────────────────┐
│▌ 4. Adds the Project resource…   [NEW-SUBSYSTEM]  [reviewed] │  title row
│▌ ┌──────────────────── diagram, full width ────────────────┐ │  diagram row
│▌ └──────────────────────────────────────────────────────────┘ │  (if any)
│▌ prose (≤ 72ch)                │ files: payload.ts  2 changes │
│▌ …                             │        compiler.ts 1 change  │
│▌ comments                      │        8 not in this diff    │
│▌ comment                       │ ─ hunks, expanded ─           │
└──────────────────────────────────────────────────────────────┘
```

- **Card:** `--bg-1` fill and a 3px left edge in the stop kind's accent (D9).
- **Title row:** "N." plus the title at 18px, the kind badge, and a
  "reviewed" toggle at the right edge.
- **Diagram row:** present only when `stop.diagrams` is non-empty (D4).
- **Two columns** (`.wt-cols`): `grid-template-columns: minmax(52ch, 72ch)
  minmax(540px, 1fr)` with the grid's font size at 15px so `ch` tracks body
  text. The prose gives up width (down to 52ch) before the code column drops
  below ~75 monospace characters. Left: the prose, then the stop's comments and the comment control
  (D8). Right: the code-changes column (D5).
- **Narrow:** `@container wt (width < 1050px)` switches every `.wt-cols`
  grid to one column, with the prose first and the code changes after. The
  page never scrolls sideways; long code lines scroll inside their hunk. The
  breakpoint was tuned in the browser (container queries measure the
  content box): 52ch + 540px + the gap and card chrome is about 1046px.
- **Reviewed stops fold** to the title row, as "viewed" folds a file in the
  Changes tab. On a reviewed stop the title itself is a button. Pressing it
  unfolds or refolds the stop for this visit without unmarking it; the
  "reviewed" toggle beside it is a separate button and only marks or
  unmarks. The unfolded state is local and not saved. A half-typed comment
  survives its stop folding and unfolding (D8). A folded stop with comments
  shows a chip in its title row, e.g. "2 comments · 1 open". The owning
  pane answers inside a comment and resolves it without rewriting the stop
  (the skill's step 6), so the stop stays folded and its reply would
  otherwise go unseen.

### D4. Diagrams render near natural size, in periscope's theme, and open full size

- **Size.** In the diagram row the SVG is `width: 100%` capped by mermaid's
  own inline `max-width` (its natural width), so it fills the card but never
  scales a small diagram up. It also gets a `min-width` of 75% of its
  natural width: below that, a wide flowchart's 16px labels drop under
  ~12px (measured at 7.6px on a 1600px window). Wider than the card, the
  diagram scrolls sideways inside its own box, never the page. A 70% height
  cap keeps a tall top-down chart from pushing the stop's text off screen.
  Several diagrams stack in order.
- **Full size on click.** With the new `zoom: true` renderer option:
  - a **diagram** opens on a mouse click, and gets a small real button in its
    corner (⤢, `aria-label="open diagram full size"`) as the keyboard and
    screen-reader way in. The diagram itself is not given `role="button"`,
    which would make its text labels presentational and hide them from
    screen readers;
  - an **image** in the text is wrapped in a `<button>` (biome rejects
    `<img role="button">`);
  - an image **inside a link** is left alone, so one click doesn't both zoom
    and follow the link.

  The lightbox is a dark backdrop with the content at natural size, on a
  solid panel so the page doesn't show through between a diagram's boxes,
  scrolling both ways when larger than the window. On open, focus goes to the scroller,
  so the arrow keys scroll a big diagram, and Tab moves only between the
  scroller and ✕. Esc (`useEscape`), a backdrop click or ✕ closes it, and
  focus returns to what opened it. For a diagram it copies the rendered SVG
  markup (still only mermaid's own sanitized output, per `Mermaid.jsx`'s
  innerHTML rule) and sets its width to the viewBox width. Its host is
  mounted once, in `overlays/Overlays.jsx`: inside `#split-view`
  (`position: fixed; z-index: 1`) the sticky app header would paint over it.
  Only the walkthrough passes `zoom: true`; file previews keep their existing
  in-place image zoom.
- **Theme (app-wide).** `Mermaid.jsx` initializes with `theme: "base"` and
  `themeVariables` from a pure `mermaidTheme(read)` builder, where
  `read(name)` returns a token's color as `#rrggbb`. The intent is:

  | Element | Token |
  |---|---|
  | node fill | `--bg-2` |
  | node text | `--fg-0` |
  | node border, arrows | `--fg-2` (non-text contrast ≥ 3:1 on `--bg-1`) |
  | edge-label background | `--bg-1` |
  | groups / clusters | `--bg-1-hi` |
  | other text | `--fg-1` |
  | font | `--sans` (Hanken Grotesk), 14px |

  The base theme derives pie, mindmap/timeline (`cScale*`) and git colors by
  rotating `primaryColor`'s hue, and `--bg-2` is near-grey. So those sets
  are given explicitly from periscope's opaque accents (`--accent`,
  `--s-done`, `--s-working`, `--s-needs`, `--effort`, `--worktree`,
  `--s-shell`, `--s-danger`), with `--bg-0` labels on them. Sequence notes,
  autonumbers and gantt sections also get token colors.

  `darkMode: true`. Mermaid's color parser can't read `oklch()`, so tokens
  are converted to hex at load by painting them on a 1×1 canvas and reading
  the pixel. The stylesheet stays the single source of truth, and only opaque
  tokens are used. This restyles diagrams in file previews too.
- Render failures are unchanged: the source and the error show as text. In
  the diagram row, outside `.md-doc`, that text gets its own code skin and
  scrolls sideways inside its box, never the page.

### D5. The code-changes column

A pure `evidenceFor(refs, diffFiles)` (built on `matchRefs`) returns:

- `shown`: refs with at least one matched hunk, each with its basename, any
  line range, the hunk count and the hunks;
- `hidden`: refs grouped as `missing` (no such file in the diff),
  `generated` (the server dropped its hunks) and `unchanged` (the file is in
  the diff but no hunk meets the cited lines);
- with `diffFiles === null` (no worktree), every ref goes to `shown` with no
  hunks, and the column shows "no worktree, so code changes are unavailable".

The column (`.wt-evidence`) renders:

- **File list:** one row per `shown` ref with the file name in mono, the line
  range, and "N changes", full path in `title`. Clicking a row scrolls the
  column to that file's block and flashes it. When the column scrolls on its
  own that's `col.scrollTo`; otherwise `scrollIntoView({block: "nearest"})`,
  which does nothing when the file is already on screen. Either way the page
  itself doesn't jump. A cite chip in the prose does the same, landing on
  the entry for its path whose hunks cover its line, else the first entry
  for that path. Chips with no match stay inert, as today.
- **One closing summary line** for `hidden`, e.g. "8 not in this diff · 1
  generated", a `<details>` that expands to the paths.
- **The hunks,** expanded, under each file's path, drawn by `Hunk` from
  `diff/ChangesTab.jsx` with `onOpen={null}`, memoized so a reviewed toggle
  doesn't re-highlight them. A file whose hunks total more than 300 lines
  (`COLLAPSE_LINES`) starts collapsed behind "show N lines". Whole-file
  cites of new files made one real walkthrough mount about 10.7k
  highlighted lines.
- **Wide panes:** `position: sticky; top: 12px; align-self: start;
  max-height: calc(100cqh - 24px); overflow: auto`. The column stays beside
  the text while the stop is on screen, scrolls on its own when taller, and
  leaves with the stop. **Narrow:** static, no max-height.
- A stop with no refs leaves the column empty.

The old `md-meta` refs row above the prose and the `wt-evidence-toggle` go
away.

### D6. Reviewed marks live in `reviewed.json` in the review folder

- **File:** `~/.claude/reviews/<repo>/<slug>/reviewed.json`, a JSON object
  of `{"<stop title>": "<sig>"}`. periscope is its only writer (D2 of the
  original spec). "clear" removes it with the folder, under the same flock
  as a mark. Otherwise a mark landing mid-`rmtree` makes the final `rmdir`
  fail, leaving a folder holding only `reviewed.json`, which would surface
  as an orphan queue row.
- **Signature:** `stopSig(stop)`, an FNV-1a 32-bit hash (8 hex chars) of the
  stop's full source slice, trailing whitespace trimmed: heading, metadata
  and body before diagrams are lifted. Any rewrite of the stop's title,
  kind, refs or text yields a new sig. Appending a stop after it, or the
  file's final newline, doesn't. Computed in the browser so it always
  matches what is displayed.
- **Expiry:** a stop is reviewed only while `reviewed[title] === stopSig(stop)`.
  A re-prepare that rewrites a stop lapses its mark, mirroring the Changes
  tab's `viewedSig`. Stale entries stay in the file harmlessly until the
  stop is marked or unmarked again.
- **Keyed by title**, like comments. Two stops with the same title share one
  slot, and since their sigs differ only one can show reviewed. Accepted,
  since the skill keeps titles distinct.
- **Server** (`periscope/reviews.py`):
  - `reviewed_path(repo, branch)`;
  - `read_reviewed(repo, branch) -> dict[str, str]`, where missing,
    unreadable or non-object JSON reads as `{}` and non-string values are
    dropped;
  - `set_reviewed(repo, branch, stop, sig | None)`, a read-modify-write under
    a module-level `threading.Lock` **and** an `fcntl.flock` on the review
    dir. `~/.claude/reviews` isn't instance-separated, so the dev and prod
    servers both write it. It's written atomically (temp file in the same
    dir, then `os.replace`, and the temp file is removed on error). `None`
    removes the entry, and an empty result deletes the file. It never
    creates the review dir: a missing one raises `FileNotFoundError`.
- **Routes** (`periscope/routes/reviews.py`):
  - `POST /api/reviews/reviewed` with `{repo, branch, stop, sig, reviewed}`:
    `_check(repo, branch)`; 400 for an empty stop or one with a newline (the
    comment route's rule), or a sig not matching `^[0-9a-f]{8}$`; 404 when
    the walkthrough file doesn't exist, or when the review is cleared
    mid-request (`FileNotFoundError`), so a mark never creates or recreates
    a folder; otherwise `{"ok": true}`.
  - `GET /api/reviews/walkthrough` gains `"reviewed": read_reviewed(...)`.
- **Frontend:** the toggle updates a local copy at once and POSTs. On failure
  it reverts and shows the error beside the toggle. A refetch whose request
  started before a mark settled doesn't overwrite the local marks. The
  toggle's accessible name is "reviewed" in both states, with
  `aria-pressed` carrying the state and the ✓ hidden from screen readers.

### D7. Progress and the contents list read the same marks

The header's "N / M reviewed" and the contents list's ✓ use the same
`isReviewed(stop, reviewed)` as the cards, so the three can't disagree.

### D8. The comment control is collapsed until used

The stop's comments still list under its prose. The always-open textarea
becomes a "comment" text button. It opens the box, focused, with "send" and
"cancel" and a hint "⌘↩ to send" ("Ctrl+↩ to send" when the platform isn't
a Mac). A successful post closes the box and shows the existing delivery
note. A 409 also closes it, with the "saved, not delivered" note. Any other
error keeps it open with the error. The buttons get periscope's normal
button styling rather than the browser default. A half-typed draft is kept
per stop outside the component, so folding a stop (which unmounts its box)
doesn't lose it.

### D9. Type and color are scoped to the walkthrough

On `.walkthrough`, so `.md-doc` and file previews don't change:

- **Five sizes** as custom properties:

  | Property | Size | Used for |
  |---|---|---|
  | `--wt-fs-meta` | 12px | header line, file paths, badges, comment timestamps, summary line |
  | `--wt-fs-ui` | 13px | buttons, links, contents list, comment text |
  | `--wt-fs-body` | 15px | prose and the problem statement |
  | `--wt-fs-stop` | 18px | stop titles |
  | `--wt-fs-title` | 22px | page title |

  Headings inside a stop's prose render at 15px semibold. Hunk code keeps
  the Changes tab's own sizes.
- **Contrast:** all text at 4.5:1 or better, estimated from the tokens'
  oklch lightness (luminance ≈ L³) and checked in the browser:

  | Text | `--bg-0` (page) | `--bg-1` (card) | `--bg-1-hi` | `--bg-2` (chips) |
  |---|---|---|---|---|
  | `--fg-3` | 3.0:1 | 2.6:1 | 2.3:1 | 2.2:1 |
  | `--fg-2` | 5.4:1 | 4.7:1 | 4.1:1 | 3.9:1 |
  | `--fg-1` | 10.6:1 | 9.2:1 | 8.1:1 | 7.6:1 |

  So metadata moves from `--fg-3` to `--fg-2` on the page and on cards, and
  any text on a `--bg-1-hi` or `--bg-2` chip (badges, the file list's hover,
  inline code) uses `--fg-1`. `--fg-3` is not used for text in the view,
  including the shared styles that would bring it in: hunk heads
  (`.dhunk-head`) take `--fg-1`, and ordered-list numbers and a closed PR
  link take `--fg-2`.
- **Inline code** in prose takes `--fg-1` (the body's color) on its chip
  background. `strong` stays `--fg-0`.
- **Kind tokens:** each kind gets `--kind-<name>` (an accent for the card
  edge and the contents dot, at ≥ 3:1 against `--bg-1`) beside its existing
  badge background and text pair, moved into `:root` as tokens. Unknown
  kinds use a neutral `--kind-other`.

## Mechanics

### Frontend

- **`review/walkthrough.js`:**
  - `parseWalkthrough` adds `problemMarkdown`, and per stop `source`,
    `diagrams` and the trimmed `bodyMarkdown` (D1);
  - new pure `stopSig`, `isReviewed` and `evidenceFor`.
- **`split/mermaidTheme.js`** (new, pure): `mermaidTheme(read)`, beside
  `Mermaid.jsx`, since the theme is app-wide and not review-specific. The
  canvas-based token reader lives in `Mermaid.jsx`, so the builder stays
  testable without a DOM.
- **`review/Walkthrough.jsx`** splits in two:
  - `Walkthrough` fetches and owns `reviewed` and the posting nonce;
  - a pure `WalkthroughView({data, repo, branch, pr, hl, reviewed,
    toggleErrors, error, onToggleReviewed, onPosted})` renders the page, so
    render-to-string can test it.

  Stops become `Stop`, `Evidence`, `Contents`, `Comments`, `CommentBox`
  (with a pure `CommentForm`) and `ReviewedToggle`.
- **`split/markdown.jsx`:** a new `zoom` option. An image is wrapped in a
  zoom button, except inside a link, and `Mermaid` receives `zoom`.
- **`split/Mermaid.jsx`:** base theme from `mermaidTheme`, plus the `zoom`
  prop (a click on the diagram, or its corner button, opens the lightbox).
- **`overlays/Lightbox.jsx`** (new): a `lightbox` signal and `<LightboxHost/>`
  (backdrop, scroller, ✕, `useEscape`, Tab kept inside). It re-renders a
  diagram from its source with a fresh id rather than copying the SVG, whose
  duplicated ids would bind its arrowheads to the original's markers.
  `overlays/Overlays.jsx` mounts the host at the app root.
- **`split/mermaidRender.js`** (new): the DOM side of diagrams. It loads
  mermaid lazily, converts tokens to hex, and waits for the UI font before
  mermaid measures labels. It re-initializes when Settings → Fonts changes
  `--sans`, and exposes `renderDiagram(code)`.
- **`static/styles.css`:** the walkthrough section is rewritten: tokens,
  grids, the container query, sticky column, cards, badges, comment control
  and lightbox.

### Server

`reviews.py` and `routes/reviews.py` per D6. No change to the queue,
`list_reviews`, comments or prepare.

### Skill

`skills/walkthrough/SKILL.md`, Diagrams section: one line saying the view
draws a stop's diagrams above its text at full width, so a diagram should
make sense before the prose explains it.

## Tests

- **vitest, pure** (`review/__tests__/walkthrough.test.js`):
  - `parseWalkthrough`: top-level diagrams lifted in order; nested fence left
    in place; `bodyMarkdown` loses only the lifted fences; `problemMarkdown`
    keeps inline markdown;
  - `stopSig`: stable for the same source and changed by a one-character
    edit to title, metadata or body;
  - `isReviewed`: match, mismatch, missing;
  - `evidenceFor`: shown, missing, generated, unchanged and no-diff cases,
    plus the summary counts.
- **vitest, pure** (`split/__tests__/mermaidTheme.test.js`, new):
  `mermaidTheme` maps a stub token reader to the D4 table.
- **vitest, render** (`review/__tests__/walkthroughRender.test.jsx`, new),
  against `WalkthroughView`:
  - the staleness badge class and text, and the progress count;
  - contents numbering and ✓;
  - card kind classes;
  - the diagram row present only with diagrams;
  - the two-column markup;
  - the hidden summary line;
  - a reviewed stop folded to its title row;
  - the comment link versus the open box.

  Buttons are pressed by calling components and invoking `onClick`, as
  `hunk.test.jsx` and `reviewsQueueRender.test.jsx` do.
- **pytest** (`tests/test_reviews.py`, `tests/routes/test_reviews.py`), per
  `docs/testing.md`:
  - `read_reviewed`: missing, corrupt, non-object and non-string values;
  - `set_reviewed`: set, overwrite, unset, and deleting the file when empty;
  - the route: 200 round-trip visible in `GET /api/reviews/walkthrough`; 400
    for a bad stop and a bad sig; 404 with no walkthrough; `_check`
    rejection.
- **In the browser,** on the dev server (:17375) with the real 13-stop
  walkthrough, at a wide and a narrow width:
  - diagrams are legible; the lightbox opens and closes (click, Enter, Esc,
    backdrop);
  - the code column sticks and scrolls on its own; a ref click lands on its
    file;
  - no sideways page scroll when narrow;
  - a reviewed mark survives a reload and lands in `reviewed.json`;
  - contrast measured.

## Out of scope

- **Why most refs read "not in this diff"** (THI-443 finding 11). It may be
  a ref-matching or diff-base bug on the server, and gets its own ticket.
- **The Reviews queue and file previews,** apart from the app-wide diagram
  theme (D4).
- **Showing reviewed progress in the queue.**
