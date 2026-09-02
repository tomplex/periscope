# Narrator (semantic pane status + auto-rename)

`periscope/narrator.py`, driven by the activity worker's 30s tick (prod
only). Per Claude pane: when the session JSONL changes (≥90s apart), one
Haiku call returns `{"status", "rename"}` — the status line surfaces in
the rail and detail header (`status_line`/`status_at` merged into
`/api/state` windows from the `pane_status` table); a non-null rename
applies via `tmux rename-window` with a `'rename'` activity event.

Invariants worth knowing before touching it:

- **Regeneration is session-id-first, size-second.** `/clear` mints a new
  smaller JSONL; a pure "grew" check would freeze the pre-clear status
  forever. Placeholder rows (`session_id` NULL, written by rename-route
  stamps) must NOT count as a session switch or they'd wipe the cooldown
  they exist to carry.
- **Humans win renames.** All three manual rename surfaces stamp
  `pane_status.renamed_at` (30-min cooldown); `seen_name` catches
  tmux-native renames; and `_generate` re-reads the live window name +
  row immediately before applying (a tick spans multi-second Haiku
  calls — the snapshot goes stale).
- **A deliberate name is PINNED, not cooled down.** `windows[pid].name_pinned`
  (state.json, `_IMMUNITY_FIELDS`) makes `narrator.is_name_pinned` return the
  `locked=True` that shuts `rename_decision` off entirely. Three writers:
  `/api/rename` (Tom typed it, including the rail's double-click), the
  `set_name` MCP tool (a pane naming itself), and `spawn_claude(name=…)` (a
  lead naming its worker). A cooldown was the wrong shape for all three —
  nothing re-asserts a deliberate name, so `RENAME_COOLDOWN_S` expiring
  unnoticed let the orchestrator pane drift through five generated names in
  one afternoon and put one worker on the orchestrator's own role name, so the
  dashboard misidentified both. The pin is NOT scoped to the name matching
  (the old `spawn_name` lock was): it marks the window as hand-named, so a
  later rename keeps it. Released only by `POST /api/name-pin {pinned: false}`
  — the 🔒 in the rail row's hover actions, distinct from the ★ beside it,
  which pins the tab into the rail's PINNED section and touches no name.
- **No cwd fallback** when a pane has no `pane_sessions` row — on a
  shared cwd a wrong-session status is worse than none; the hook
  self-corrects on the next prompt.
- **The lifespan tests mock `activity.run_worker`.** The real worker's
  first tick runs immediately, and in tests `PORT` defaults to 8765 — an
  unmocked worker executes a LIVE narrator tick (real Haiku, real
  renames of real windows) on every pytest run. This actually happened.
