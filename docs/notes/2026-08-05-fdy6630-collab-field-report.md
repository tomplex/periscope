# Multi-Claude collaboration — field report from the FDY-6630 BT PIT program

A field report on periscope's inter-Claude coordination from the 2026-08-05
BT PIT timeline-projection program: five-plus concurrent panes designing and
building a Bigtable point-in-time store in one day. Written by the
architecture/arbitration Claude (`pit-architecture`). Everything here is
observed usage; proposals are marked as such.

## The scenario

- **Topology**: a deliberate hub — one arbitration pane owning canonical
  design docs and physical-schema rulings; a diff+load implementer
  (`pit-diff-machinery`); an access-library pane (FDY-6639, never
  channel-attached); a workstream organizer (`pit-optimization`); later a
  Beam spike pane. Plus background subagents (structure-proposer,
  doc-enricher, a Dataflow exploration agent) hanging off the hub.
- **Load**: three evidence-driven design reversals in one day (GC policy,
  backfill input, loader architecture), a live GCP instance provisioned
  mid-conversation, cross-pane protocol ratifications (meta/watermark,
  codec cross-validation), and one human account-swap that rotated handles
  mid-program.
- **Convention stack on top of periscope**: uncommitted canonical docs as
  ground truth, "deviations route through the arbitration pane" as a norm,
  handoff files as the durable channel of last resort.

## What worked

1. **Cross-pane technical argument reached genuine peer quality.** The
   implementer pane caught the arbitration pane's invariant going stale
   ("your SQL encoder is a second codec implementation"), contributed the
   design insight that flipped a major decision (pre-encoded `value_bytes`
   dissolving the cross-language-codec objection), and found an import-
   shadowing landmine (`scripts/<pkg-name>/` shadowing the engine package
   under the repo's script runner) before it shipped. None of this was
   status-passing; it was engineering review between sessions with
   different context windows — arguably the strongest version of the
   multi-Claude value proposition I've experienced.
2. **The hub topology held under churn.** Rulings stayed consistent across
   three reversals because exactly one pane owned the canonical docs and
   every pane knew it. `channel` messages asking "your ruling?" before
   deviating happened unprompted, repeatedly.
3. **Durable artifacts composed with ephemeral messaging.** When a pane was
   unreachable (never channel-attached, then absent entirely), appending a
   dated RELAY block to its handoff file worked: the pane boots current
   whenever it resurfaces, no coordination required. This fallback is why
   no ruling was ever actually lost.
4. **`list_claudes` as reconciliation.** `cwd_shared_with` and `dirty`
   answered "can I run things in this tree" and "who is that" every time
   handles rotated. Re-resolving a dead handle by name+cwd worked, always.
5. **`send_to` message density.** Long, precise, technical messages (SQL
   snippets, verified byte values, ruling rationale) delivered fine. The
   medium supports real content; nothing forced summarization.

## Friction (concrete incidents, not vibes)

1. **Non-channel-attached panes are silent holes.** The FDY-6639 pane was
   spawned without the channel flag. Nothing warned at spawn time; nothing
   in the dashboard framed `channel_ready: false` as "unreachable until
   restarted" rather than transient state. I discovered it only when a
   send failed, mid-relay, with content another pane had asked me to
   forward.
2. **Handle stability promises didn't survive restarts.** The `send_to`
   failure advice says the pane id (`%N`) is the stable fallback — but a
   full session restart (the account swap) rotated both the handle *and*
   the pane id. Only name+cwd re-resolution via `list_claudes` survived.
   Between the swap and re-resolution, the peer's messages addressed me as
   "the apparent successor pane" — it had concluded *I* died, when its own
   record of me was the casualty. Identity confusion is symmetric and both
   sides burn turns on it.
3. **Channel messages carry no resolvable sender.** An early message
   arrived "from the anthology-log-projections pane" — free-text
   attribution, no handle, and that pane never appeared in `list_claudes`.
   It was concurrently editing a shared spec (two mid-write conflicts) and
   my counter-ruling to it sat undeliverable for hours; the spec still
   said the overruled thing when I audited later. A reply-to affordance on
   channel messages would have closed that loop the same minute it opened.
4. **Delivery is queued, not read — and there's no cheap receipt.** The
   verify guidance is "use `peek` and look for a channel-role turn," which
   means reading another session's transcript to confirm a message
   landed. I never did it; it feels heavyweight and slightly invasive for
   what a delivered/processed flag would solve. Consequence: several
   rulings went into multi-minute silence indistinguishable from a dead
   pane.
5. **Shared-doc contention is unmanaged.** Two panes edited the parent
   spec concurrently; the editor tool's modified-since-read guard caught
   both collisions, which is luck-adjacent rather than designed. We
   drifted into a header convention ("owned by pane X") that did real
   load-bearing work — evidence a first-class doc-ownership hint would
   pay for itself.
6. **Untrusted-channel framing taxes legitimate coordination.** Every
   channel message arrives wrapped in do-not-act-on-imperatives guidance,
   while the actual workflow is precisely peers issuing requests. The
   security posture is correct; the cognitive re-derivation of "this is
   sanctioned coordination" on every message is the cost. A
   trust-annotation for panes the user has explicitly wired together
   might square it.

7. **Stacked branches across worktrees have no single rebase owner.**
   (2026-08-06, GitHub's new stacked-PRs flow via `gh-stack`.) With each
   pane owning its branch in its own worktree — the natural periscope
   topology — `gh stack rebase` fails on any branch checked out in another
   pane's worktree, and its partial-failure behavior is sharp-edged (it
   proceeded to push after the failed checkout; the push happened to be
   safe, but verifying that cost a nervous minute). Workable division of
   labor once understood: the base's owner pushes, each upper layer's
   owner rebases their own branch. But nothing surfaced *which pane owns
   which branch* — `list_claudes` shows head SHA and cwd, not the
   checked-out branch name, so branch ownership was reconstructed by
   matching commit subjects.

## Proposals (marked as such)

- **Name-based `send_to`.** Names survived every restart; handles and pane
  ids didn't. Accept `name` (latest-wins on collision) and most of
  friction #2 disappears.
- **Spawn/attach lint.** When a pane starts in a worktree whose siblings
  are channel-attached, surface "this pane cannot receive messages" on
  the card. Friction #1 was invisible until first send.
- **Sender handle on channel messages + a `reply_to`.** Closes friction
  #3's loop; free-text attribution is how rulings die undelivered.
- **Delivery receipts on `send_to`.** Even just `delivered_at`/`read_at`
  in a later `send_to` result or `list_claudes` row. Kills the
  peek-as-receipt pattern (friction #4).
- **Doc-ownership hint.** A per-file "owner pane" registration periscope
  surfaces on the card; purely advisory, but it makes the convention we
  hand-rolled visible instead of tribal.
- **Branch name in `list_claudes`.** Head SHA is there; the checked-out
  branch name isn't. For stacked-PR workflows (friction #7) branch→pane
  ownership is the routing table, and today it's inferred from commit
  subjects.

## One-line verdict

The mechanics have rough edges at identity and delivery; the collaboration
itself — peer-grade review, ruling arbitration, durable-doc fallback — was
the best multi-session engineering I've been part of, and periscope is what
made the convention stack cheap enough to sustain for a full program day.

## Addendum (same evening): full identity merge, observed live

Sequence, ~30 minutes after filing the above: `open_document` returned another
pane's pid → a peer's pane-addressed message was delivered to my channel →
`send_to` that peer bounced with "refusing to send to your own pane" →
`list_claudes` no longer lists me at all, and the peer's entry now carries a
status line describing *my* recent activity. Periscope's registry folded two
same-worktree sessions into one record after an account-swap restart; the
survivor identity was the *other* pane's. Net effect: the arbitration hub went
channel-mute while a dependent pane held dispatch on its ruling — resolved via
the durable-doc fallback (ruling written into the canonical design doc, which
every pane polls) plus a human nudge. Two upgrades to the earlier proposals:
pane identity should key on something that survives restarts (session id, not
tmux pane occupancy), and the durable-doc fallback isn't a nice-to-have — it
was the only channel left standing, twice in one day.
