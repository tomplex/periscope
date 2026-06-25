# What periscope could take over — usage mining, 2026-06-12

Sources: `~/.claude/history.db` (1,419 indexed sessions; 324 real sessions with
≥2 typed messages, 4,800 typed messages, 2026-04-07 → 06-12), mined by four
parallel agents pulling verbatim messages per theme, plus direct SQL. Builds on
the 2026-06-10 skills audit (`~/.claude/audits/usage-audit-2026-06-10.md`) —
that one produced skills (/handoff, /ship, work-rollup); this one asks what the
**dashboard/server** can absorb.

## Corpus facts that frame everything

- **You run a fleet, not a session.** 3,243 overlapping session pairs among the
  324 real sessions. Concurrency is the norm; periscope is the only thing that
  sees all panes at once, which makes it the natural home for cross-session
  work no skill can do.
- **~20-25% of mid-session messages carry ≤1 bit of intent** — bare approvals
  (6.7%), option-letter picks (~3-4%), commit/push/restart verbs (6.2%),
  interrupt-then-"keep going" (4.1%), status polls.
- **In periscope-dev sessions, 26.3% of all typed messages are the
  verification loop** (screenshot paste 11.1% + "still broken" 8.0% + "looks
  good" 7.2%). You are the harness.
- **94% of periscope-project sessions are spawn noise** (1,054/1,120 are
  ≤2-message `.empty-mcp.json` spawns, mostly /usage parsing) polluting the
  history index.

## Ranked opportunities

### 1. Quick replies on the rail (highest frequency, lowest lift)

~10% of mid-session messages are "yes" / "A" / "do it" / "keep going". The
pane already knows when Claude is waiting (needs-input detection); the rail
row could render the actual pending choice:

- Parse the pane's tail for AskUserQuestion option lists → render **Yes / A /
  B / C** chips → `deliver_input`. The plumbing (`/api/send`,
  `deliver_input`) all exists.
- **Continue** chip on any pane whose last event was a user interrupt —
  "sorry, keep going" is pure ceremony (4.1% of marathon messages).
- A small per-pane snippet palette for dispatch macros — "execute it,
  subagent-driven" was typed 5× in one session, once per phase; "commit and
  push please" 30+ times corpus-wide (once as "commit and pish").

### 2. Session-start context injection (branch mission + prior session)

Correction from a second pass: the heavy 300-600-word structured openers
("You're picking up FDY-2292…") are **Claude-composed** — written by a
sibling session and delivered via spawn_claude or paste-courier, not typed
by Tom. Tom's own re-establishment typing is small and specific:

- "review/explore the changes on this branch" — ~21 sessions, the single
  most repeated opener; relies entirely on cwd, gives the reviewer no intent
- "on this branch, we are implementing X. in this session, our goal is Y" —
  the mission sentence, typed verbatim twice in one day
- "find all existing spec docs… search prior claude conversations" — the
  ask when a session starts cold with no handoff

What's worth injecting (via `pane_session_hook.py` SessionStart →
`GET /api/pane/context` → `additionalContext`), ranked:

1. **Per-branch mission note** — one sentence in a tiny `branch_notes` table
   (repo+branch → text), written once (UI field or a `set_branch_note`
   channels tool at work start), injected on every session in that worktree.
   Kills the retyped mission sentence and gives the 21 bare branch-review
   sessions intent for free.
2. **Prior-session pointer, keyed by branch** (not cwd — narrator lesson:
   wrong-session context is worse than none): date, outcome, one-line
   summary from history.db. Replaces "search prior claude conversations".
3. **Mechanical block** (branch, worktree, PR + CI, Linear-from-branch-name,
   pinned UUIDs) — nearly free once the hook calls home, but secondary: its
   value is letting handoff-composing sessions skip that section (one source
   of truth that can't drift — the "730d vs 1-yr" provenance dispute was a
   stale composition) and saving cold sessions a discovery turn.

NOT injected: the session goal (genuinely new each time — the human types
it), full handoff bodies (explicit /handoff resume path; stale state
injected silently is how sessions act on last week's plan), sibling pane
statuses (pull via a `fleet_status` tool, not push — 30s staleness).

Mirror image at session end: a per-pane "generate handoff" action writes
the summary the next injection reads — closing the loop currently closed by
typing "summarize this branch for another claude".

Wrinkle: SessionStart fires at startup and /clear, but sessions sometimes
enter their worktree after starting. Re-inject from the UserPromptSubmit
hook only when the pane's branch changed since last injection.

### 3. Watch objects — server-side babysitting (frees whole panes)

~64 sessions contain typed watch requests; PR-CI watching alone is ~24
sessions, near-daily, **still typed fresh every time despite /ship**. Today a
watch costs: composing the spec (target, poll interval, green/red policy,
notify channel) + a Claude pane burning tokens to run `gh pr checks` in a
loop. Every common target is something the server can poll itself:

- **PR CI** — periscope already detects the PR per pane (`git_pr.py` cache);
  poll checks server-side, alert on green/red. No pane, no Haiku, no prompt.
- **Merge → master CI → deploy** as one watch object with a single
  "deployed" alert (~8 sessions chained this by hand).
- **Anthology/k8s builds by resource UUID** (~12 sessions) — poll
  `update_job_states`, surface phase/progress as the pane status line; the
  narrator answers "what's the build progress like?" passively.
- **On-green hook**: watches are rarely watch-only — "don't update the sticky
  branch until we have a build", "kick the scope when the image is done". A
  watch needs an optional *on-complete: inject prompt into pane X* action.
  That's the bridge from passive dashboard to active orchestrator.

Mid-wait re-asks ("still waiting", "how's it looking?", bare "check ci",
2-6× per long wait) all disappear into a status row.

### 4. Pane-to-pane plumbing (you are currently the message bus)

- **Prompt courier (~18×, ~2/week):** session A composes a handoff prompt;
  you copy it, open a pane, paste it. → channels tool + rail affordance:
  "send this prompt to a new/existing pane", worktree creation included.
  `spawn_claude` exists; what's missing is *pane A handing off directly* and
  a UI path for "to an existing pane".
- **Live relay (~9×, bidirectional):** "another session and I found…",
  "i just told the other session about this" — you retype findings between
  concurrent panes, once by screenshotting one pane into another. →
  `send_to_pane` channels tool pushing into the sibling's prompt context
  (the notification plumbing already does exactly this shape).
- **Sibling awareness (~4× explicit + 1 dirty-tree collision):** "the deploy
  port is being built by another claude session, so don't worry about that."
  → inject same-repo sibling panes' narrator status lines on request (a
  `fleet_status` tool) so sessions can deconflict themselves.
- **Claim verification (~3×):** "another claude told me X, re-assess" /
  "when i spec'd this with another claude, i said 1-yr" — you paraphrase
  from memory because the other transcript isn't addressable. →
  `get_history_session` already exists; expose "fetch the session behind
  pane N" so claims check against the actual exchange.

### 5. Verification harness for frontend work (periscope-dev's 26%)

The invariant loop: Claude edits → restart → you check browser → paste
`/tmp/periscope-paste-*.png` → "still doing it" ×N → "boom! that did it".
Routine items take 2-3 round-trips; the LGTM iframe bug took ~14, with you
pasting console output four times.

- **Auto-screenshot:** after a frontend change, capture the dev-server page
  headlessly (Playwright against :5174/:8766) and feed the image back to the
  pane. Replaces the check-browser/paste cycle for the first-pass look.
- **"Still broken" / "Works" chips** on the pane card sending a canned
  message + fresh screenshot in one click.
- **Show which server is which:** one whole failure class ("my bad - i was
  using prod and not the dev server") is just prod-vs-dev confusion the
  dashboard could label.
- Console-log capture from the dev page piped to the pane would have erased
  the four manual console pastes in the iframe bug.

### 6. Hygiene: stop polluting your own history

1,054 spawn sessions (usage parsing, `.empty-mcp.json`) are 94% of
periscope-project history and re-pollute every search/rollup built on the
index. Tag spawned sessions at spawn time (env var or session-id registry)
so the indexer can mark `category=spawn` — or stop spawning Claude to parse
/usage at all now that `usage.py` reads the OAuth endpoint directly.

## What NOT to build

- **Auto-commit policy** — micro-verb absorption is tempting, but "commit
  please" is often a judgment call about *when* a change is a unit; a rail
  button (item 1) is the right altitude, not a policy.
- **More notification kinds** — the notify stack works; the audit showed the
  composition layer, not the delivery layer, is the gap.
- **Post-deploy soak watching** (Grafana metric thresholds) — real but rare
  (~3 sessions), unstructured, and the judgment is human. Skip until watch
  objects exist and earn it.

## Suggested order

1. Quick replies + Continue chip (small, all plumbing exists, daily payoff)
2. SessionStart context injection (data already held; deletes the biggest
   retyped block)
3. Watch objects for PR CI + master-deploy (then builds, then on-green hooks)
4. Prompt courier + send_to_pane (turns the fleet into a fleet)
5. Spawn-session tagging (quick, protects everything retrieval-shaped)
6. Auto-screenshot harness (biggest single-project win, most new machinery)
