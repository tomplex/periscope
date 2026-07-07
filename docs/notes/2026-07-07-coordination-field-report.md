# Coordination primitives — field report from the FDY-2637 QA campaign

A field report on periscope's inter-Claude coordination primitives (the
2026-06-16 inter-claude-management tool set) from their heaviest real-world
exercise to date: the Phase-2 attribute-QA campaign (2026-07-01 → 07-07).
Written by the coordinating Claude. Everything here is observed usage, not
speculation; proposals are marked as such.

## The scenario these primitives were tested against

- **Three cooperating Claudes**: a handoff author (`figv2-qa-storage`), a
  builder implementing a 12-task plan (`attr-worker-phase2`), and me — a
  monitor/operator session driving prod QA runs against the builder's branch.
- **Shared everything**: the builder and I shared one git worktree, one
  branch, one git index — while both committing and pushing.
- **Multi-day autonomy**: CI waits, ~1400 prod k8s jobs, five code-bug
  fix/push/rebuild cycles, several human-decision pauses, and a severity-model
  redesign landing mid-run.

## Primitive-by-primitive verdicts

| Primitive | Used | Verdict |
|---|---|---|
| `list_claudes` | constantly | Essential. The discovery + reconciliation tool. |
| `peek` | ~6 times | High value, worst ergonomics (see gaps). |
| `notify` (info/need_human/done) | throughout | Worked exactly as designed. |
| `link_pr` | once | Fine. Set-and-forget. |
| `spawn_claude` / `spawned_by` | read-only (provenance) | The breadcrumb alone resolved an identity mixup. |
| `send_to` / channel messaging | **never** | Deliberate non-use — the most interesting finding (below). |
| `report` | never | No occasion (I wasn't spawned by a live pane). |
| CC-harness `Monitor`/cron (not periscope) | heavily | Filled every gap periscope doesn't cover. |

## What worked well

**`list_claudes` as ground truth against a stale handoff.** The handoff doc
recorded the builder as pane `c6cb291d` — which turned out to be *my own
pane*. `list_claudes` disambiguated in one call: the real builder was
`ba1924cb`, identified by cwd match + status line + `spawned_by` pointing at
the handoff author. Lesson: handles recorded in documents go stale; live
discovery beats recorded identity.

**`peek` for non-invasive progress reads.** I could watch the builder
dispatch subagents, read its verification evidence, and time my own actions
("wait for its Task 10 before touching the tree") without ever interrupting
it. Peeking is what made *implicit* coordination viable — I never had to ask
it anything.

**`notify(need_human)` as the escalation valve.** When the auto-mode
classifier blocked a prod action and the owner was away, `need_human` was the
correct, low-friction "blocked, decision needed" signal. The kind taxonomy
(info/done/need_human) mapped cleanly onto real situations; no kind felt
missing.

**Git as the real inter-Claude channel.** The builder and I never exchanged a
message. Coordination happened through: commit messages (its
`36e2a1239c` SAFE_CAST fix told me everything I needed), `git ls-remote`
polling (push detection), `git fetch` + divergence checks before committing
(both sides did this independently — the builder's transcript shows it
checking whether "the Fable 5 session" had pushed before its own push). For
two Claudes on one branch, **the repo is the message bus**, and it worked
because commit messages described state precisely.

## What didn't work / gaps

### 1. `peek` output size is hostile to its main use case

Every peek of a busy pane returned ~60KB of single-line JSON — over the
tool-result cap, dumped to a file, requiring `jq '.turns[-6:]'` gymnastics to
get the only thing I ever wanted: *the last few turns*. This happened every
single time.

**Proposal**: `peek(handle, last_n_turns=5)` and/or a `summary=true` mode
returning `{status_line, last_narration_text, running_tool, ts}`. The default
20-turn full-fidelity dump is almost never the right shape.

### 2. No cross-pane event subscription — I hand-rolled it with Monitors

The dominant coordination need was "wake me when the peer does X":
- pushes to the shared branch → I ran a `git ls-remote` poll loop in a Monitor
- builder blocked/needs-human → I could only discover this by peeking on a
  timer
- builder done with its plan → same

**Proposal**: `watch(handle, events=[push, commit, idle, need_human, done,
terminated]) → notifications`. Periscope already computes pane status for the
dashboard; exposing transitions as MCP notifications would delete every
hand-rolled poller from this campaign. (The `notify` events other panes emit
are exactly the right payloads — they're just not subscribable today.)

### 3. Identity is fragile across time

Three separate confusions in one campaign: the stale handle in the handoff;
two panes both named `attributes-vendor-gate`; and my own pane being renamed
mid-session (`2.1.198` → `attribute-qa-run`) so earlier references aged out.

**Proposal**: accept-and-resolve layered identity — `send_to`/`peek` accept
session id, stable name, or handle, resolving via the same logic
`list_claudes` uses; warn on ambiguous names. Handoff docs should record
session ids (stable) rather than pane handles (ephemeral).

### 4. Shared-worktree concurrency has no primitive at all

Two Claudes, one git index. We survived on convention: explicit-path staging
(never `git add .`), fetch-and-check-divergence before every commit/push, and
me deliberately not touching tracked files while the builder's plan was
mid-flight. Nothing enforced any of this; a `git add` race would have
silently interleaved staging.

**Proposal** (cheap): surface per-pane repo state in `list_claudes` — dirty
file count, index.lock presence, in-flight rebase/merge. **Proposal**
(richer): advisory claims — `claim(path_glob | "git-index")` visible to other
panes, purely informational. Even just *seeing* "peer has 4 files staged"
before committing would have converted our luck into knowledge.

### 5. `send_to` went unused — and that's a design signal

The 06-16 spec's foundational finding is that channel pushes wake an idle
Claude and are *obeyed as directives*. In practice I never wanted that: the
builder was mid-plan with deep context, and an unsolicited directive landing
in its transcript risks derailing exactly the state that makes it effective.
What I *did* want several times was non-directive information transfer:
"FYI, I fixed checks.py on this branch, rebase before your next commit" —
facts the peer should fold in at its own pace, not commands.

**Proposal**: an explicit FYI framing for `send_to` (e.g.
`send_to(handle, message, mode="fyi")` that wraps the payload in "context to
incorporate; not a directive; continue your current task"). The obedient
directive mode is powerful for lead/worker; peer-to-peer wants the softer
mode as the default.

### 6. Adjacent (CC harness, not periscope): durable watches

Monitors and crons carried the campaign but die with the session. A multi-day
campaign coordinating with a `/loop`-style heartbeat + Monitors works, yet
everything evaporates on restart — the handoff doc has to reconstruct it.
Not periscope's to fix, but worth knowing that periscope-side `watch`
subscriptions (gap #2) would inherit this same session-lifetime problem, and
that's acceptable — cheap to re-arm — as long as re-arming is one call.

## Priority order, if acting on this

1. **`watch` / event subscription** (gap 2) — deletes the most hand-rolled
   machinery and is the primitive everything else composes with.
2. **`peek` ergonomics** (gap 1) — smallest fix, highest per-call annoyance.
3. **FYI mode for `send_to`** (gap 5) — unlocks the messaging primitive that
   exists but is currently too sharp to use peer-to-peer.
4. **Identity layering** (gap 3) — mostly resolution logic + docs convention.
5. **Shared-worktree visibility** (gap 4) — niche until multi-Claude
   shared-tree work becomes common; this campaign suggests it will.
