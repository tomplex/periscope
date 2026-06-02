# Transcript view — next steps (pick-up doc)

Deferred follow-ups to the segmented-transcript "Claude turns" view that shipped
in round 1 (`docs/superpowers/specs/2026-06-02-segmented-transcript-turns-design.md`,
`-structure.md`, plan `docs/superpowers/plans/2026-06-02-segmented-transcript-turns.md`).
Each item below is self-contained enough to pick up in a fresh session.

## Where the feature lives (orientation)

- **Renderer:** `static/src/split/Transcript.jsx` — `<TranscriptView>` (poll hook
  `useTranscriptPoll` + autoscroll), `<Turn>`, `<ToolCall>` (`fullInput`/`toolArg`
  per tool), `<Composer>` (Enter-sends, screenshot paste). Markdown →
  `static/src/split/markdown.jsx` (`renderMarkdown`, incl. GFM tables).
- **Detail wiring:** `static/src/split/Detail.jsx` — `computeMode(w)` (Transcript
  vs Terminal), the kept-mounted transcript hosts (one per opened pane), the
  `--detail-header-h` ResizeObserver, the terminal scroll-to-bottom button.
- **Mode/seen state:** `static/src/store.js` — `transcriptMode`, `transcriptSeen`.
- **Server:** `history/search.py:messages_from_jsonl(path)` (parse + tool pairing
  + uuid + filters), `periscope/turns.py` (`get_turns_for_pane(session,index)`,
  `session_id_for_pane`, glob-for-`<sid>.jsonl`), route `GET /api/pane/turns` in
  `periscope/routes/pane.py`.
- **Pane→session map:** `pane_session_hook.py` (SessionStart + UserPromptSubmit)
  writes `~/.config/periscope/pane_sessions/<tmux-pane>` = session id. See
  CLAUDE.md "Pane → session mapping".
- **Message shape** (what the renderer consumes): `{role, uuid, ts_ms, text,
  tool_uses:[{id,name,input,result}]}`; compact dividers `{role:"system",
  kind:"compact", uuid, ts_ms}`. `ts_ms` is **milliseconds** — `relTime` wants
  seconds, so render `relTime(m.ts_ms/1000)`.
- CSS: the `.transcript*`, `.turn*`, `.tc*`, `.md-*`, `.term-scroll-bottom`
  blocks at the end of `static/styles.css`.
- Frontend convention: no frontend test suite — verify in the browser on the dev
  instance (`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 PERISCOPE_NO_RECLAIM=1 uv run
  server.py`); rebuild + commit `static/dist/app.js` after `static/src/` changes.

---

## 1. Current-turn highlight + resume verify (small; was plan Task 7)

- **Highlight the newest turn. DONE** (commit a390101). `<TranscriptView>`
  computes the last non-system message uuid and passes `current` to `<Turn>`;
  `.turn-current` tints it `color-mix(in oklch, var(--accent) 7%, transparent)`.
  Pure CSS, no timer. Caveat: it can only highlight what the JSONL contains —
  see the in-flight-flush finding under item 2.
- **AskUserQuestion rendering. DONE** (same commit). Was falling into
  `toolArg`'s `default` and dumping truncated JSON. Now `toolArg` shows the
  question (or "N questions"), and expanding renders an `<AskQuestions>` block:
  per-question header chip + prompt + options (label/description) + multi-select
  marker. The chosen answer is already in the `tool_result` string (normal
  `tc-out`). See the `.tc-ask*` CSS.
- **Resume path-flip manual test. STILL OPEN.** With a pane in Transcript mode,
  run `claude --resume` / start a new session in the same cwd; confirm the
  transcript full-replaces to the new session's turns (no stale merge, no
  dupes). The full-resend design + uuid keying should already handle this —
  just verify.

---

## 2. Task / activity panel ("what is Claude doing")

The JSONL carries everything needed (confirmed against a real session):

- **Tasks:** `TaskCreate` / `TaskUpdate` tool_uses with
  `{subject, description, activeForm}` (TaskUpdate also `{taskId, status}`).
  Replay them in order to reconstruct the live todo list + each task's status
  (☐ pending / ◐ in_progress / ✓ completed) — i.e. the "N tasks, 1 in progress"
  view, live.
- **Current activity:** the last assistant turn's in-flight tool (`result===null`
  → "running Bash…"), or the in-progress task's `activeForm`, or idle. Cheap,
  high-signal at-a-glance header. **But the JSONL is an unreliable source for
  in-flight state** — see the flush finding below; the truly-live signal has to
  come from the live pane, not the transcript JSONL.
- **Turn timing:** `type:"system"` events with `subtype:"turn_duration"` (and
  `stop_hook_summary`) carry per-turn duration. Currently filtered out by
  `messages_from_jsonl` (non-compact system events are dropped) — surface them
  if wanted (e.g. a faint duration on each assistant turn).

**Approach:** add a small reducer (probably server-side in `messages_from_jsonl`
or a sibling, so it's unit-testable — `tests/test_search.py`) that derives a
`tasks` summary + `activity` field alongside `messages`, returned by
`/api/pane/turns`. Render as a collapsible header strip or a pinned section in
`<TranscriptView>`. Keep `messages` as-is; add fields, don't reshape.

**Design note:** decide placement with Tom (pinned top strip vs. a toggle vs.
the side metadata panel). This is a brainstorm-worthy UX call before building.

**FINDING — JSONL does not reliably carry in-flight turns (measured, not
guessed).** A 4 Hz probe over a pending `AskUserQuestion` (`/tmp/ask_probe.sh`,
sampling both the resolved JSONL and `/api/pane/turns`) showed the question
**never** on disk while pending — it materialized only at turn completion,
**already paired with its result**, in a single atomic write. So:
- **Client-side pausing tools (`AskUserQuestion`, plan approval, etc.) are
  invisible in the transcript until answered.** The whole turn (prose + the
  tool) lands at once on answer. Not fixable from the JSONL.
- **Server-side tools (Bash/Read/…) DO flush live** — the assistant message
  with the tool_use is written before its result, so `result===null` →
  "running…" works for those (observed directly).
- There is *anecdotal* evidence the pending question is occasionally visible
  (one clear sighting), i.e. Claude Code's flush timing isn't 100% — but it's
  not dependable.

**Implication for this item:** a trustworthy "what is Claude doing / waiting on
you" indicator must read **live pane state** (capture-pane / the status-line
periscope already parses, the spinner, the alt-screen question box), NOT the
transcript JSONL. The JSONL-derived task list and per-turn timing are still fine
to surface — those are about *completed* state — but the live "now" signal is a
live-pane concern. Don't build the activity header purely off `/api/pane/turns`.

---

## 3. Subagent transcripts (re-association)

Subagent (Agent/Task tool) turns are **NOT** sidechains in the parent JSONL
(`isSidechain` count is 0 there). They live in their **own files**:

```
~/.claude/projects/<enc-cwd>/<PARENT_SESSION_ID>/subagents/agent-<agentId>.jsonl
```

So the re-association key is the **parent session id** (the subdir name). Given a
pane's session `S` (already resolved via `pane_sessions`), its subagents are
`projects/<enc-cwd>/S/subagents/*.jsonl`.

- Each `Agent` tool_use in the parent has `{description, subagent_type, prompt}`
  and a tool_use `id` (`toolu_…`); its **final report** is already in the paired
  `tool_result` (rendered today when you expand the `⚇ Agent` block).
- To link a specific Agent call to its `agent-<agentId>.jsonl`: the Agent tool
  *result* text ends with `agentId: a…` (the spawn id). Match that `agentId` to
  the `agent-<agentId>.jsonl` filename. (Verify the exact id↔filename mapping
  when building — confirm whether the filename id equals the result's `agentId`
  or a separate uuid; a fresh dispatch + inspection of the subdir will show it.)
- **Feature:** expanding an `⚇ Agent` block could drill into the subagent's own
  transcript (parse `agent-<id>.jsonl` with the same `messages_from_jsonl`), so
  you can see what the subagent actually did, not just its final report.

**Approach:** extend `periscope/turns.py` with a resolver for a parent session →
its subagent files (glob the `subagents/` subdir), and a route (or extend
`/api/pane/turns`) to fetch a named subagent's messages on demand (lazy — don't
parse all subagents up front). `messages_from_jsonl` already handles the parse;
the subagent JSONLs are the same format. Unit-test the resolver in
`tests/test_turns.py` against a seeded `projects/<enc>/<sid>/subagents/` dir.

---

## Known facts worth not re-discovering

- **cwd alone can't identify a pane's session** — many panes share a dir; that's
  why `pane_sessions` + glob-by-id exists. Don't reintroduce cwd-newest as the
  primary resolver.
- **Env scanning for the session id is unreliable** — inherited
  `CLAUDE_CODE_SESSION_ID`/`TMUX_PANE` cross-contaminate across tool/subagent
  subprocesses. The hook payload is the authoritative source.
- **`/clear` mints a NEW session id** (new JSONL) — confirmed empirically. The
  SessionStart hook handles it.
- **A session's JSONL is under its *start* cwd's encoded dir**, not the pane's
  current cwd (panes that `cd`/EnterWorktree move) — hence glob-by-session-id,
  not cwd-encode, in `turns.py`.
