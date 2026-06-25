# Periscope architecture audit — 2026-06-24

Four-axis deep audit (parallel agents + spot verification). Framed around the
four questions: outgrown architecture, hand-rolled vs library, JSON→SQLite, and
hardening for daily development.

**Headline:** The codebase is in good structural health. The hand-rolls are
almost all justified (niche protocols, private contracts). The real exposure is
**not architecture — it's the safety net**: zero CI gate, a stale-docs map, two
narrow event-loop stalls, one genuine god-module, and a `state.json` hot-path
that's papered over with two thrash-avoidance hacks. Ranked by payoff below.

---

## TL;DR — recommended order of attack

| # | Move | Effort | Why it's first |
|---|------|--------|----------------|
| 1 | **CI gate**: `uv run pytest -q` + `npm test` on push/PR | ~1hr | 728 tests exist and gate *nothing*. Single biggest gap. |
| 2 | **Fix CLAUDE.md drift** (Channels/LGTM "in server.py"→modules; 4→~19 tools) | ~30min | The map actively misleads every fresh session. Verified false. |
| 3 | **Wrap 2 async handlers' tmux calls** in `run_in_executor` | ~15min | Only true event-loop stalls. Verified. |
| 4 | **Dist-staleness check** (`npm run build && git diff --exit-code static/dist/`) | ~20min | Stale-bundle footgun: edit src, forget build, ship stale prod JS. |
| 5 | **Migrate `windows` out of state.json → SQLite** | ~half day | Kills the write-amplification the two existing hacks paper over. |
| 6 | **Split `channels.py`** (1616 lines) into `channels/` package | ~half day | The one genuine god-module. |
| 7 | **Golden-file tests** for unguarded `gh`/`git`/`tmux`/JSONL parsers | ~2hr | These break silently on upstream format bumps. |
| 8 | **Direct tests** for `worktrees.py`, `repo_locks.py`, `cleanup.py` | ~2hr | Small, untested, do destructive git work. |

---

## Q1: Architecture choices we've outgrown

**Mostly no — with three real exceptions.** The "rebuild the world every 3s" poll
is *not* outgrown: the expensive work (git status, gh PR, usage, LGTM) is already
cached + bg-threaded off the poll path, and `/api/state` is a sync `def` handler
so it runs in Starlette's threadpool — it does **not** block the event loop. The
single-process model is fine.

What *is* outgrown:

1. **CLAUDE.md drift (highest friction).** The architecture map is wrong about its
   two biggest subsystems. Verified: `server.py` is 93 lines (pure shim), has
   **zero** `# --- Channels ---` / `# --- LGTM ---` blocks — that code lives in
   `periscope/channels.py` and `periscope/lgtm.py`. The tool list documents 4
   tools; the registry (`channels.py` `_CHANNEL_TOOLS`) has ~19. `spawn_claude`'s
   documented signature is missing `repo`/`branch`/`workspace_id`. Also: CLAUDE.md
   references an `openModal` bridge in `poll.js` that doesn't exist. Cheap to fix,
   high leverage — this is the file every session reads first.

2. **`channels.py` is a genuine god-module (1616 lines).** Six unrelated concerns:
   MCP socket/server, 19 tool handlers, alert/session registry, notification
   emission, TUI-dialog detection, and a 450-line static tool-registry literal.
   Worst offender inside it: `_do_spawn_claude_tool` is **189 lines**. Clean seam
   exists (`channels/mcp_server.py` + `channels/notifications.py` + `channels/alerts.py`
   + `channels/tools/*.py` + `channels/dialogs.py`); coupling is low and cycles are
   already guarded by late-imports. By contrast `activity.py` (834 lines) is *not* a
   god-module — 8 clearly-commented subsystems behind one DB lock; leave it until ~1200.

3. **pid-as-identity is showing strain (~200 lines of accidental complexity).**
   `@periscope_id` lives in *tmux* window state (lost on server restart), forcing
   rebind heuristics + a GC-immunity hack + a spawn-collision special-case in
   `pids.py`. The "detail pane closes on cd" class of bug traces here. Not a blocker
   — a periscope-owned persisted id would delete the workarounds, but only worth it
   when the dedup/detach bugs recur.

**Narrow event-loop hazard (real, not the poll):** two `async def` handlers call
blocking subprocesses directly on the loop — `routes/command.py:52` (`capture()`)
and `routes/paste_image.py:70-71` (`tmux()`). Both verified `async def`. These *do*
stall the WS mirrors + MCP socket. `routes/ws.py` does it correctly with
`run_in_executor`; `routes/send.py`'s `time.sleep(0.10)` is fine because send is a
sync `def` (threadpool). Fix: make the two handlers sync `def`, or wrap their tmux
calls. Surgical.

---

## Q2: Hand-rolled code that should be a library

**Verdict: almost everything hand-rolled here is correctly hand-rolled.** I went in
looking for "you reinvented X, swap in library Y" wins and found essentially none —
the hand-rolls cover niche protocols or private/undocumented contracts no library
addresses. Honest pushback: don't add dependencies here.

| Area | Hand-rolled | Verdict |
|------|-------------|---------|
| `tmux_mirror.py` | control-mode protocol parser + reconcile frames | **Keep.** No lib does tmux `-C` streaming; `libtmux`/`tmuxp` wrap the *command* interface, different problem. (Note: pyte is **test-only**, not a prod emulator — CLAUDE.md's "+pyte" framing is misleading.) |
| `channels.py` | unix-socket transport, hello-frame, `_MCP_SESSIONS` push registry | **Keep.** JSON-RPC state machine *is* the SDK; the hand-rolls fill genuine SDK gaps (pane-tagged transport, server-initiated push). |
| `channel_shim.py` | reconnect/replay proxy w/ byte-level JSON-RPC sniffing | **Wrap-but-keep** — the *best* library-safety candidate. The reconnect capability must be custom, but frame classification uses raw `dict.get("method")` instead of the `mcp.types.JSONRPCMessage` model it already pins. Brittle if Claude ever batches requests. Low priority (covered by tests + exit-0 invariant). |
| `poll.js` 3s poll | full-state pull | **Keep.** Aggregate rollup is legitimately a pull; live bytes already use WS, event data uses SSE. SSE/WS push would be *more* machinery. |
| `pidfile.py`/signals/`log.py` | reclaim, SIGTERM, `_bg`/`_task` | **Keep.** Generic libs lack the periscope-specific identity/reload logic; the rest is minimal-correct. |
| `git_pr.py`/`gitutil.py`/`worktree_spawn.py` | shell to git/gh + parse | **Keep decisively.** `gh` has no Python binding; pygit2/dulwich don't cover worktrees or PR/CI data and would *add* surface. Already uses `--json`/porcelain; injection already mitigated. |
| `usage.py` | JSONL parse + Keychain + OAuth usage fetch | **Keep.** Every input is a private/undocumented contract; no library can exist. |
| `store.js`/`railTree.js` | pref-merge tree + optimistic-mutation timestamp guard | **Keep** merge (domain logic). Mild watch-item: the `lastTabMutation` optimistic-update hack is what React Query/SWR solve — only reach for a query cache if that pattern proliferates. |

**One soft rec, one watch-item.** That's the whole haul. The pushback the question
invited lands on "these hand-rolls are right."

---

## Q3: Data storage that should migrate JSON → SQLite

The author already pre-declared this direction (`config.py:71-75`) and pre-built
the table patterns (upsert, dead-pane prune, age-prune, WAL checkpoint) in
`activity.py`. `state.json` is 84KB; live breakdown:

| Key | Size | Nature | Recommendation |
|-----|------|--------|----------------|
| `windows` (210 entries) | **50KB (~60%)** | per-pid annotations + `last_seen` heartbeat, mutated on the 3s hot path | **TIER 1 — migrate.** Highest payoff. |
| `projects` (36) | 10KB | entity registry | **TIER 2 — migrate.** |
| `workspaces` (1) | 0.2KB | entity registry | **TIER 2 — migrate.** |
| `ui` / `settings` / `commands` | <3KB | config-shaped, user-action-mutated, read as whole blob at boot | **TIER 3 — leave as JSON.** |

**Tier 1 (`windows`) is the whole win.** Every mutation today is a whole-file
rewrite under a lock → O(file size) write amplification. Two hacks exist *only*
to dodge this cost: `dirty`-gating in `pids.py:156-172` (skip writes on pure
`last_seen` bumps) and batched-write in `store.py:350-376`. A `windows` table makes
each stamp a single-row `UPDATE`, GC a `DELETE … WHERE ts < cutoff`, and the
dead-pane prune identical to the existing `prune_pane_status`. Keys on the same
`pane_id` as four existing tables.

**Tier 2 (`projects`/`workspaces`)** collapses a *second ad-hoc store*: `projects.py`
and `workspaces.py` reach **past** the typed-accessor boundary into raw `_STATE` +
`_STATE_LOCK` — they're effectively a parallel store implementation. And
`workspaces`' membership *already* lives in `pane_workspaces` in SQLite while the
entity sits in JSON — an awkward split worth healing.

**Tier 3 stays JSON** — small, low-cardinality, human-editable config; migrating
buys nothing. Once Tier 1+2 leave, state.json shrinks to ~3KB of true config,
exactly what a JSON file should be. No append-only/event data remains in state.json
(that's already in `events`/`captain_log`/`ui_events`/`usage_samples`).

**Cruft (not migration, but noted):** stale `state.json.{bak,bak2,pre-test,pre-v2-*}`
from May 18, safe to delete. And the big one — `launchd-stdout.log` (96MB) +
`launchd-stderr.log` (55MB) are unrotated (the *app's* `periscope.log` rotates, but
launchd captures the raw streams). 151MB of unrotated logs on a daily tool.

---

## Q4: Hardening against heavy daily development

The structure is fine; the **safety net is the exposure**. Ranked:

1. **No CI gate (the #1 gap).** `.github/workflows/` has only `release.yml` — no
   pytest, no vitest, no lint on push/PR. `.git/hooks/` has only samples. 622 Python
   + 106 frontend tests gate *nothing*; correctness relies on Tom remembering
   `uv run pytest`. A `push`/`pull_request` workflow running `uv sync && uv run
   pytest -q` + `npm ci && npm test` turns "the server's flaky" into a red check.
   (`@needs_tmux` tests need a tmux binary on the runner or a skip marker.)

2. **Unguarded external-format parsers that fail silently.** CLAUDE.md's status-line
   regexes *are* guarded (`test_panes.py`). The dangerous ones aren't:
   - `worktrees.py:45-59` — `git worktree list --porcelain` positional parse — **no test**, whole worktree feature
   - `git_pr.py:126-127` — `gh pr list --json statusCheckRollup` keys — **no test**, PR CI badge → None
   - `git_pr.py:227-248` — `gh run list --json` fields — **no test**, activity timeline
   - `usage.py:75-81` — Claude JSONL `message.usage.*` — **no test**, plan meters + hot-pane detection
   - `panes.py:258` — `tmux list-windows -F` 8-field positional — **no test**

   Fix is the pattern `test_panes.py` already proves: commit one real blob of each as
   a golden fixture, assert the parser extracts the expected fields. Loud failure on
   upstream bump instead of `is_claude=false` everywhere.

3. **No typecheck/lint on the frontend + dist-bundle footgun.** Plain JSX, no
   eslint/tsconfig. The committed `static/dist/app.js` is the only build artifact;
   `bin/periscope restart` does **not** build (only `install` does). Edit src, forget
   `npm run build`, commit, restart → prod serves stale JS. Guard: pre-commit/CI step
   `npm run build && git diff --exit-code static/dist/`.

4. **No `/api/state` contract guard.** Hand-built dicts, no `response_model`, no
   shared schema; frontend reads string keys (`is_claude`, `status_line`, `pane_id`)
   untyped across ~8 files. Rename a key server-side → frontend silently reads
   `undefined`. `_safe_build` isolates per-pane exceptions (good) but that's failure
   isolation, not contract checking. A single test asserting the payload key-set, or
   a `response_model`, makes a rename fail a test.

5. **Test gaps in small destructive modules.** `worktrees.py`, `repo_locks.py`,
   `cleanup.py` — no direct tests; `cleanup`/`worktrees` do git/destructive work and
   `repo_locks` guards concurrent `worktree add` races (threading bugs invisible in
   sequential tests). Small + self-contained → high coverage-per-effort.

**Clean ✅:** Error visibility (invariant 8) holds — grep found no naked
`threading.Thread`/`asyncio.create_task` bypassing `_bg`/`_task`. The frontend
grid/stream retirement left no orphaned files. The validation logic for `open`
descriptors is duplicated between `routes/open.py` and `channels.py` (sync 3 sites
to add a target type) — minor, only bites when a 4th target lands.
