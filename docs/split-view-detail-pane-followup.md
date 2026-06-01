# Split-view detail pane: missing metadata + actions

**Status:** todo — deferred behind the in-flight preact migration.
**For:** the Claude session that lands after the preact migration merges.

## What's already done

The split-view rail (`▤ split` in the top-bar) is feature-complete and
the user's primary UX. The rail itself renders per-worktree info inline
(PR/CI/Linear chip strip via `.wt-meta`) and has all the actions:
drag-reorder with drop indicator, hover-X close, dblclick rename,
collapse, status-dot rollup. **Don't touch the rail unless you're
extending it.**

The **detail pane** (the right side of split view) is what this
follow-up is about. It currently shows:

- **Header:** `session · branch · #PR ✓ · LINEAR-ID · git · model · ctx% · ⚠ api-error`
  — every field there is fine; keep them.
- **Body:** xterm mirror for pane rows, LGTM iframe for review rows.
- **Side panel** (`#detail-side`): just `Pending input` / `Recap` /
  `Last line` from the per-window `/api/state` payload. Tiny.

The side panel has **a ton of wasted vertical space** because the rich
per-pane data (PR title, reviewers, activity timeline, notes, etc.)
isn't being surfaced. That data is already on `/api/pane?session=X&index=N&lines=80`
— the same endpoint the legacy modal sidebar polls.

## What's missing (and where to find it in the legacy code, if it still exists)

The pre-preact modal had a three-section sidebar built around a 1.5s
poll of `/api/pane`. **Port the same three sections into the split
view's `#detail-side`.** They are:

### 1. Linked

Two rich cards — PR and Linear:

- **PR card** when `data.pr` is set: PR number link, PR title, draft/open
  pill, `+N −M` diff size, CI badge (passing/failing/running with color
  + dot), requested-reviewers list as 2-letter avatar bubbles. When
  `data.pr` is null, show a `+ link pull request` button that POSTs to
  `/api/channel/push?pane=<pid>` with a canned prompt asking Claude to
  call its `link_pr` MCP tool. Button is disabled when
  `data.channel_attached === false` (no Claude on this pane).
- **Linear card** when `data.linked_linear` is set: ticket ID linking to
  `https://linear.app/issue/<id>`, `data.linked_linear_title`,
  optional `data.linked_linear_status` pill. When unset, same `+ link
  Linear ticket` button pattern as PR.

The pre-preact `static/modal.js` had these as `renderPRCard(data)` and
`renderLinearCard(data)` — pure functions, data in / HTML out. The
helpers (`avatarChars`, etc.) are alongside. If the preact rewrite
preserved them, reuse; otherwise re-derive from the same `data` shape.

### 2. Notes

Editable per-pane notes + tags, persisted via `prefs.setAnnotation(pid, {notes, tags})`:

- `<textarea>` debounced 600ms on input, flushed on blur.
- Tag chips with × to remove; tag input that accepts Enter or comma to add.
- Both DOM elements must have ID-prefixed names (e.g.
  `detail-notes`, `detail-tags`, `detail-tag-input`) so they don't
  collide with the modal's `modal-notes` etc. if the legacy modal
  survives in any form.

Was `renderNotesEditor(data)` + `wireNotesEditor(data)` in pre-preact `modal.js`.

### 3. Activity

The merged-timeline section:

- Pin the latest unresolved `need_human` channel alert at the top in a
  loud `.activity-pinned` block.
- Below it, an `<ol class="timeline activity-stream">` of recent events
  in chronological order: commits, CI runs, channel alerts, focus/reset
  events. Each row has a colored timeline dot (green for commit, red
  for ci failed, etc.), the event text (linkified when there's a URL),
  and a relative-time label (`commit · 2m ago`).
- Empty state: `<div class="timeline-empty">no recent activity</div>`.

Was `renderActivitySection(data)` + `activityRow(e)` + `timelineColor` +
`timelineLabel` + `alertDotColor` in pre-preact `modal.js`.

## Data source: `/api/pane?session=X&index=N&lines=80`

Returns the rich per-pane payload — `pid`, `target`, `cwd_raw`, `pr`,
`pr_title`, `pr_draft`, `pr_additions`, `pr_deletions`, `pr_reviewers`,
`ci`, `linked_linear`, `linked_linear_title`, `linked_linear_status`,
`channel_attached`, `channel_unread`, `pane_id`, `activity` (array), and
all the per-window fields `/api/state` has.

The poll cadence in the pre-preact modal was 1.5s. Same is fine for the
detail pane. Stop polling on `detailTeardown` (called by `applyView`
when leaving split view) and on `selectReview` (review iframe owns the
right pane; the side panel is hidden in that mode).

## Implementation outline

```ts
// In whichever component owns #detail-side post-preact:

const [paneData, setPaneData] = useState(null);

useEffect(() => {
  if (!selectedPaneTarget) return;
  let alive = true;
  const tick = async () => {
    const r = await fetch(`/api/pane?${qs(selectedPaneTarget)}&lines=80`);
    if (alive && r.ok) setPaneData(await r.json());
  };
  tick();
  const h = setInterval(tick, 1500);
  return () => { alive = false; clearInterval(h); };
}, [selectedPaneTarget]);

// In the render:
<aside id="detail-side">
  {paneData && (
    <>
      <LinkedSection data={paneData} />
      <NotesSection data={paneData} idPrefix="detail" />
      <ActivitySection data={paneData} />
    </>
  )}
</aside>
```

## Pitfalls (lessons from the legacy modal)

- **Don't rebuild while user is typing.** The modal's `renderModalSidebar`
  short-circuits when `modalSide.contains(document.activeElement)` —
  otherwise a poll lands mid-keystroke and clobbers the textarea. In
  preact terms: skip re-rendering the notes section if its textarea has
  focus, or use uncontrolled inputs with a ref.
- **Preserve activity-stream scrollTop across rebuilds.** Same modal
  trick: capture `scrollTop` before wholesale rebuild, restore after.
  Easier in preact with a `useRef` on the `<ol>`.
- **Channel-unread side effect.** When the detail pane is showing a
  pane with `data.channel_unread > 0`, the legacy modal POSTed
  `/api/channel/clear-unread?pane=<pid>` (fire-and-forget) so the
  pane's notification badge clears once you're actually looking at the
  pane. Replicate that — the user expects the badge to clear when they
  open the pane in split view, same as when they opened it in the
  modal.
- **Same-pane no-op.** `selectPane(pid)` is called every time the rail
  re-renders for the same selection. The terminal-mount already
  short-circuits via `currentMount === "pane" && currentMountKey === pid`.
  Apply the same guard to the side-panel mount so the /api/pane poll
  isn't restarted on every render.

## Smaller missing actions (lower priority)

- **Auto-rename button** — the modal had a `✨` button next to the title
  that POSTed `/api/auto-rename-window?session=X&index=N`. Could live
  in the detail-pane header.
- **Notification clearing** — beyond the per-pane unread mentioned
  above, the modal has no other notification UI in the sidebar; the
  cross-pane alerts rail (top-right `🔔 notifications`) covers it
  app-wide.

## CSS

All the relevant CSS classes (`modal-card-inset`, `pr-head`, `pr-num`,
`pr-mini`, `pr-ci`, `ci-passing/failing/running`, `modal-avatar`,
`tag-chip`, `tag-chip-x`, `modal-notes`, `modal-tag-input`,
`activity-pinned`, `timeline-row`, `timeline-dot`, `timeline-link`,
`timeline-empty`, `modal-side-section h4`) already exist in
`static/styles.css`. If the preact migration kept the classnames, the
detail-side sections will style identically to the modal's. If it
restyled, the classnames need updating but the structure is the same.

## How to confirm it's working

1. Open a Claude pane in split view that has a real PR + CI run.
2. The Linked card should show PR number, title, +/− diff, CI passing
   dot, requested reviewers.
3. The Activity timeline should show recent commits to that branch
   and CI runs with timestamps.
4. Typing in the Notes textarea, blur, reload the page — notes
   persisted.
5. Add a tag with `Enter` — appears as a chip; click × — removes.
6. The right side of split view should feel **as info-dense as the
   modal used to** — that's the bar.
