# Preact migration — post-cutover follow-ups

Tracked deferrals from the Preact frontend migration (dogfooding finds).

## Open

- [ ] **Rail worktree meta badges missing.** The Preact split-view rail is not
  showing the per-worktree meta line (PR `#NNNN`, CI glyph `✓/✗/⟳`, git
  `clean`/dirty, Linear `TEAM-NNN` chip) that appears under each worktree row in
  vanilla. The component exists (`static/src/split/RailRows.jsx:WorktreeMeta`)
  and is rendered from `Rail.jsx` as `{!isOther && !wtCollapsed && <WorktreeMeta
  wtWindows={wtWindows} />}`. Likely causes to check: (a) the `!wtCollapsed`
  gate hides it on collapsed worktrees (the reference screenshot shows badges on
  *collapsed* rows, so they should show regardless of collapse), and/or (b)
  `wtWindows[0]` lacks the `pr`/`ci`/`git`/`linked_linear` fields the badges read
  (verify `/api/state`'s window shape reaches the rail's first window).
  Reference: the badges look like `#7224 ✗  clean *` and `#7256  FDY-161`.

## Done
