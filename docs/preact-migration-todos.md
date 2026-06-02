# Preact migration — post-cutover follow-ups

Tracked deferrals from the Preact frontend migration (dogfooding finds).

## Open

## Done

- [x] **Rail worktree meta badges missing.** Cause (a): `Rail.jsx` rendered
  `WorktreeMeta` under a `!wtCollapsed` gate the vanilla `worktreeRow` never had
  (vanilla emits `${meta}` outside the collapse-gated `${body}`), so the strip
  vanished on collapsed rows. Fix: drop the `!wtCollapsed` gate. Cause (b) was a
  non-issue — `window_view.py` spreads `**git, **pr` into the view, so
  `pr`/`ci`/`git`/`repo_slug`/`linked_linear` reach `wtWindows[0]` with the names
  `WorktreeMeta` reads.
