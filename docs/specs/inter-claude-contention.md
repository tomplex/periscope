# Spec — inter-Claude contention

Periscope's coordination primitives tell a pane that a collision *is already
happening*. Nothing prevents one, and nothing makes the cost of a collision
visible before it is paid. This spec covers five changes that close that gap,
in build order.

> **Parked idea — not reviewed.** The measurements and the inventory of what
> exists today are verified against the code. The proposals are not. Two
> assumptions in §3 are load-bearing and unchecked: that a `PreToolUse` hook
> can reliably identify which pane it is running in (`pane_session_hook.py`
> has to hunt for `TMUX_PANE` on a direct child because inherited env
> cross-contaminates — a hook may face the same problem), and that "is this
> file tracked" is cheap enough to evaluate on every `Edit` inside the
> sub-50ms budget §3 asserts. If either is false, §3's enforcement mechanism
> needs a different shape. §1 and §2 do not depend on them.

## Problem

Measured on 2026-07-27, one repo (`fdy`), one campaign (FDY-2637):

- **Four Claude panes share `~/dev/worktrees/fdy/tc-vendor-dict-ingestion`** —
  one git index, one build dir, 21 dirty files between them. Two of the four
  were doing read-only work (a spec, a perf analysis) and needed no write
  access to that tree at all.
- **The worktree is not a branch boundary.** That worktree is checked out to
  `tc/waiver-chief-ui`. Across 80+ worktrees in two layouts
  (`~/dev/worktrees/fdy/*` and `~/dev/fdy/.claude/worktrees/*`), the worktree
  has become a persistent project workspace whose branch gets swapped —
  `tc-attribute-config-refactor` alone has hosted five branches.
- **Branches are long-lived and merge sideways.** `tc/waiver-chief-ui` is 293
  commits ahead of master across 94 files with 19 merges, several of them
  sibling-into-sibling (`merge: tc/vendor-dict-ingestion into
  tc/waiver-chief-ui`). Two live forks of that branch touch **14 of the same
  files**, including `structure.sql` and a migration.
- **Staleness is invisible until merge time.** 81 merge-from-master commits
  across `tc/*` in 60 days; `tc/attribute-qa-dashboard` sits 60 commits behind
  master on a base from 2026-07-13.

### What that has cost

Confirmed from session transcripts, the private journal, and this repo's own
git log:

| Incident | Cost |
|---|---|
| `git commit -am` in a shared worktree swept a peer's uncommitted files into this session's commit (2026-07-15, `tc-vendor-dict-ingestion`) | Hours of git archaeology to restore the peer's two commits under their real messages before the PR could be merge-tested |
| `index.lock` held by a concurrent git process; the session read it as stale and proceeded | Left `suggestions.py` staged **with literal conflict markers**; caught only by a re-grep before commit |
| Stash-pop against 6 landed commits on the same file, WIP authored blind to them (2026-07-23) | One of three conflicts was a genuine design fork; had to escalate a UI decision rather than resolve mechanically |
| A peer's unfixed lint drift blocked the shared checkout | Every push in that worktree ran `--no-verify`, disabling the safety net for everyone |
| Supervisor ran `cargo test` in a worker's tree; the shared target-dir lock SIGTERM'd the worker's test binary | Worker read the death as a hang in its own work (this repo, commit `93875b0` — the reason `cwd_shared_with` exists) |
| Two branches each bumped `SUGGESTER_VERSION` (9→10→11→12) | Signature collision — **a prod worker silently carried forward the other branch's envelope** (journal, 2026-07-14) |
| A session avoided a peer's dirty `worker.js` by deploying from a pristine worktree — but HEAD already depended on that uncommitted protocol | **Production broken for a day**, found via the user's own bug report (journal, 2026-07-09, sts2-seed-finder) |

The last two are the shape that matters: the damage was not a merge conflict,
it was shipped behaviour. The `worker.js` one is the sharpest — the session did
everything carefully, protected the peer's WIP, and *that* was the bug.

## What exists today

Everything periscope offers is a read of the current instant, and every safety
property is prose in a tool description.

- `list_claudes` computes `cwd_shared_with` per call via a plain inverted index
  over each pane's worktree root (`channels.py:1047-1064`), keyed on
  `git_signal_for(path)["toplevel"]` — deliberately the **worktree** root, not
  the repo root, so non-contending panes aren't flagged (`git_pr.py:131-143`).
  It carries `head` / `head_subject` / `head_committed_at` / `dirty` from the
  same 5s-cached signal.
- The mitigation is a sentence in the tool description: *"do not run builds,
  tests, or any command that writes to that tree"* (`channels.py:1616`).
  Nothing checks it. A pane that never calls `list_claudes` collides silently.
- `send_to` / `report` are fire-and-forget pushes (`channels.py:995`,
  `emit_channel_event` at `:896`). No correlation ids, no acks beyond "bytes
  written to the shim", no queue — an unattached target is a hard error with
  nothing buffered. Nothing can await a reply.
- The only lock in the system is `repo_locks.repo_lock()` — an in-process
  `threading.Lock` registry (`repo_locks.py:19-43`) guarding `git worktree add`
  at two server-side call sites. Not reachable from any MCP tool, no owner, no
  TTL, no durability.
- No claim, lease, queue, mutex, or ownership registry exists for panes. The
  prior attempt is gone: `DROP TABLE IF EXISTS first_mate;`
  (`activity.py:108`).

The 2026-07-07 field report (`docs/notes/2026-07-07-coordination-field-report.md`)
ranked five gaps and put event subscription first. Two have since shipped:
`peek` ergonomics (gap 1) and shared-worktree visibility (gap 4, as
`cwd_shared_with` + `dirty`).

**This spec reverses that ranking.** The field report described a three-pane
campaign with a dedicated monitor, where the gap was polling. The shape now is
four panes in one tree with no monitor, where the gap is prevention. Claims
(§3) move ahead of `watch` (§4).

## 1. Base age and branch as collision axes

**Goal.** Make staleness and same-branch contention visible before they are
paid for. `cwd_shared_with` catches same-tree; it misses two panes in different
worktrees on the same branch (they collide at push) and it misses a branch
rotting against master.

**Shape.** Extend the existing signal rather than adding a subsystem.

- `git_signal_for` gains `base_age: {behind: int, days: int}` — commits behind
  the repo's default branch and days since the merge-base commit. Reuse
  `gitutil.detect_default_branch`. Both numbers come from one extra
  `git rev-list --count --left-right` plus the merge-base commit date; keep it
  inside the existing 5s cache.
- `list_claudes` rows gain `branch_shared_with`, computed by the same inverted
  index as `_mark_shared_trees` but keyed on `(repo_toplevel_of_repo, branch)`
  rather than worktree root. Generalize `_mark_shared_trees` to take a key
  function; two call sites, one helper.
- Surface `base_age` in the rail on the branch sub-cluster row (`railTree.js`
  already derives those), as a warning past a threshold.

**Risks.** `git rev-list` against a default branch that has never been fetched
returns a garbage count. Degrade to `null` and render nothing rather than
guessing — same stance as `git_signal_for` returning `None` for a non-repo
path. Do not fetch on the read path; the numbers may be stale by exactly one
`git fetch`, which is acceptable for a warning.

## 2. Stack view and cascade

**Goal.** Answer "master landed, what has to absorb it and in what order?" —
today that is reconstructed by hand for every branch, 81 times in 60 days.

**Shape.** Periscope already enumerates every worktree and branch
(`worktrees.py` cache) and already owns spawn and rail placement.

- A read endpoint returning the branch DAG for a repo: nodes are the default
  branch plus every branch with a worktree or a live pane; edges are
  merge-bases; each edge carries ahead/behind. This is derivable entirely from
  `git merge-base` + `git rev-list --count`, no new persistent state.
- Render it as a view: which branches descend from which, where master is, what
  is behind.
- A cascade action: given a moved default branch, emit the branches needing it
  in topological order, and spawn one Claude per level with the merge
  instruction. Levels run in sequence; each level's merge is what unblocks the
  next.

**Risks.** The DAG derived from merge-bases is a *guess* at intent — sideways
sibling merges make it genuinely ambiguous which branch is a topic and which is
a trunk. Render what git knows; do not infer a trunk. If cascade ordering is
wrong, a merge lands in the wrong order and the user re-does it, so cascade
must present the plan and not auto-run it.

Both §1 and §2 are pure reads with no new state machine, and both are useful
standalone. Build them first.

## 3. Claims, enforced by a PreToolUse hook

**Goal.** Convert an invisible collision into a blocked tool call naming the
holder. This is the step change: every existing property is advisory, and the
incidents above are all cases where a pane either didn't check or checked and
was right but had no way to *hold* what it found.

**Shape.**

- A `claims` table in `~/.config/periscope/periscope.db`:
  `resource_key TEXT, owner_pid TEXT, kind TEXT, acquired_at INTEGER,
  expires_at INTEGER, reason TEXT`. Follow the guarded-ALTER migration pattern
  at `activity.py:123-129`.
- `resource_key` for a tree is `git_signal_for(path)["toplevel"]` — already the
  correct canonical key, realpath'd worktree root, chosen for exactly this
  reason (`git_pr.py:131-143`).
- Two MCP tools, registered as one record each in `_CHANNEL_TOOLS` plus a
  `_do_*` handler (`channels.py:1257`): `claim(resource, kind, reason, ttl)` →
  granted, or denied with the holder's handle, reason, and age; and
  `release()`.
- Release on three paths: explicit call, TTL expiry swept by the 30s worker
  tick (`activity.py:_worker_tick`), and dead-pane pruning via the existing
  `prune_pane_*(alive)` pattern (`app.py:70-81`).
- **Enforcement is a `PreToolUse` hook on `Edit|Write|Bash`.** The
  infrastructure exists: `bin/periscope install-hook` already edits
  `~/.claude/settings.json` idempotently (`bin/periscope:96-116`) and a
  `PreToolUse` Bash hook is already in use (`block-master-push.sh`). The hook
  asks the running server whether this pane may write path P / run command C,
  and returns a deny with the holder named.

**Scope it narrow to start.** Deny only: writes to *tracked* files, and
build/test commands, when another attached pane holds that tree. Let untracked
writes through. That covers the `-am` sweep, the `cargo test` SIGTERM, and the
two-read-only-panes case, without blocking a pane writing a spec into
`docs/`.

**Risks.**

- **A stuck pane is worse than a collision.** The deny message must name the
  holder handle, its reason, and how to message it, or a denied pane has no
  move. Leases must have a TTL so a crashed holder cannot deadlock a tree.
- **The hook must fail open.** If periscope is down or the socket is
  unreachable, allow the write. This is the `channel_shim.py` invariant
  (only ever exit 0, item 10 in CLAUDE.md) applied to enforcement: a
  nice-to-have safety layer must never become a hard dependency.
- **Latency.** The hook fires on every `Edit`. Budget sub-50ms; a unix-socket
  round-trip to a live server is well inside that, but the hook must have a
  short timeout and treat expiry as allow.
- **Force is required and must be loud.** `claim(force=true)` has to exist for
  a stale holder that TTL hasn't reaped; log it as an event and notify the
  displaced holder.
- Claims are protected by `activity.py`'s single process-local `_LOCK`
  (`activity.py:111-132`), not by cross-process SQLite transactions. Fine at
  this volume — periscope is the only writer — but it means a second periscope
  instance would break the guarantee. Dev already writes its own DB via
  `config.instance_file`, so this holds.

## 4. Watch — event subscription

**Goal.** Replace hand-rolled poll loops with "wake me when X". The field
report's original first priority; still unbuilt.

**Shape.** `watch(events=[...])` delivering through the existing
`emit_channel_event` push path. With claims in place the useful events are
`claim_released`, `tree_clean`, `peer_committed`, `peer_pushed`. Periscope
already computes most of these — `dirty`, `head`, and channel alerts are
recomputed every tick; this exposes the transitions.

**Risks.** Subscriptions die with the session, as Monitors do. Acceptable per
the field report's own reasoning, provided re-arming is a single call. A pane
blocked in a modal permission dialog takes no turns and will never see a
delivered event — an existing, documented limitation of the channel, not new
here.

## 5. FYI mode for `send_to`

**Goal.** Peer-to-peer facts, not directives. The field report's finding was
that `send_to` went deliberately unused because a channel push lands as an
obeyed directive and derails a peer mid-plan. Between same-branch peers the
messages that matter are facts — *"I rewrote `checks.py`, rebase before your
next commit"*.

**Shape.** `send_to(handle, message, mode="fyi")` wrapping the payload in
framing that marks it as context to fold in, not a task to switch to. Default
stays directive for lead→worker; `report` is unaffected.

**Risks.** Framing is a prompt-level guarantee, not an enforced one. A peer may
still drop what it is doing. That is acceptable — the alternative today is that
the message isn't sent at all.

## Not doing

- **A general distributed lock.** Claims are advisory, single-host,
  single-writer, TTL'd. No fairness, priority, deadlock detection, or queueing
  on contention — a denied claim returns immediately with the holder named and
  the caller decides.
- **Request/response between panes.** `_call_tool` handlers can be async so
  awaiting is mechanically possible (`channels.py`), but a blocking `ask()`
  needs correlation ids, a pending-request table, and a timeout convention.
  Out of scope; `watch` covers the real need.
- **Durable inter-pane message queues.** An unattached target stays a hard
  error.
- **Enforcing one-writer-per-tree structurally.** Claims make the collision
  visible and blockable; they do not stop the user from putting four panes in
  one tree.

## The other half of the fix

Half of the measured cost is workflow, not tooling, and periscope cannot fix
it. Recorded here because §1 and §2 exist specifically to support these rules:

- **One-way merges.** Name the integration trunk, keep one PR to master open,
  merge master→trunk daily by one pane, topic→trunk only, **never
  topic→topic**. Converts k² sibling merges into k trunk merges and localizes
  master conflict resolution to one place. When a checkpoint lands on master,
  merge master back into the trunk the same day — the 07-15 merge that had to
  reconcile branch-v17 files against master's v6 checkpoint is the cost of not
  doing this.
- **Partition by file set, not by feature.** Feature independence is not file
  independence: the two live forks overlap on 14 files.
  `attribute_update_worker.py` (55 commits) and `suggestions.py` (50) are
  single-writer resources.
- **No shared monotonic counters.** `SUGGESTER_VERSION`/`VALIDATOR_VERSION` are
  merge-ordering bugs by construction. Content hashes of what they version.
- **Generated files get a merge driver, not a merge.** `structure.sql`, dist
  bundles — regenerate from resolved source, never hand-resolve.
- **Read-only work does not get the hot tree.**
