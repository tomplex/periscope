# Plan — rail stability pass + diff→file navigation

Two items. The second is mechanical; the first has one non-obvious decision.

## Why now

Three things landed recently that all made the *remaining* jank more visible,
not less: `/ws/state` push, the alert feed folding onto the hub, and the
Changes tab refreshing live. Everything updates faster and more often — and
every update is still a hard swap. Periscope is fast but it snaps.

Current state: **0 of 20** `transition` declarations are on a data-driven
change (all hover/pulse), and the four attention sections still render
conditionally (`AttentionSections.jsx:112/142/172/191`).

## The actual mechanism (worth being precise about)

Attention sections sit *above* the tree, and the same panes also appear *in*
the tree. So a pane going `working → idle`:

1. leaves RUNNING → that section shrinks or vanishes
2. everything below shifts up
3. may enter READY → that section grows
4. everything shifts again

RUNNING churns constantly, so this fires all the time. The audit's complaint —
"the tree jumps tens of pixels while you're aiming at a row" — is a *mis-click*
problem, not an aesthetic one.

That distinction matters: **animation alone makes mis-clicks worse**, because a
smoothly-moving target is still a moving target. Motion and aim need separate
fixes.

## 1a. Freeze membership while the pointer is in the rail

The decision worth flagging. Attention-section membership updates are deferred
while the pointer is over the rail, and flushed on leave (or after a short
idle). Row *contents* keep updating live — only the set of rows, and therefore
the layout, is held still.

Rationale: you only care about layout stability while you're aiming at
something, which is exactly when the pointer is in the rail. The rest of the
time you want it live. This costs one hover-latch and no permanent chrome.

Rejected alternatives:
- *Always render all four headers* — kills section appear/disappear shift, but
  adds four rows of permanent chrome and doesn't stop shift from row-count
  changes within a section.
- *Reserve fixed height* — section height varies with row count; nothing sane
  to reserve.
- *Animate only* — makes movement legible but doesn't fix aim (see above).

Fallback if it feels laggy in practice: shorten the flush delay, or scope the
freeze to the section under the pointer.

## 1b. Motion for the changes that do land

Once layout is stable under the pointer, animate the rest so updates read as
motion rather than teleport:

- Section enter/leave and row enter/leave — `grid-template-rows: 0fr → 1fr`
  (animatable, unlike `height: auto`).
- State stripe colour, chips (PR/CI/Linear), badges — plain colour/opacity
  transitions.
- Respect the existing `prefers-reduced-motion` block (`styles.css:1070`).

Text content can't be transitioned; status-line changes get a brief opacity
dip rather than a crossfade, which would need double-rendering.

## 3. Diff → file navigation

Clicking a file path (or a hunk header) in the Changes tab opens that file as a
preview tab, scrolled to the hunk's line. `openFileTab({path, line})` already
exists and the preview tab already accepts `line`; the diff already carries
`new_start` per hunk. Mechanical.

Path is repo-relative; preview resolves against the pane's cwd — need to
confirm the join is right when the pane has `cd`'d inside the repo.

## Verification

- Unit-test the freeze latch as a pure reducer (pending vs applied set), same
  as `reviewState`. Motion itself is CSS — browser-verified by Tom.
- Confirm transition count moves off 0 for data-driven properties.
- Confirm `prefers-reduced-motion` still disables it.
