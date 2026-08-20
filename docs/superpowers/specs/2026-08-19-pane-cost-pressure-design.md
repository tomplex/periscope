# Pane cost pressure — design

**Status:** approved; reviewed by spec-reviewer 2026-08-19; ready to plan
**Date:** 2026-08-19

Replace periscope's dead 🔥 burn flag with a live signal that tells you when a
pane's accumulated context has grown expensive enough that acting on it pays —
and which action to take.

Empirical claims in this spec were measured against this machine's transcript
corpus (339 files, 187 from August 2026) on Claude Code **2.1.236**. Record-shape
behavior is version-dependent; re-verify before trusting it on a later version.

---

## Decisions

These are the choices that shape the feature. Everything below this section is
mechanics.

### D1. The signal is debt vs. interest, not "how much is this pane spending"

A Claude pane re-sends its whole conversation on every API call, billed at the
cache-read rate. So a long-running pane overpays on *every future call* by an
amount set by how far its context has grown past a fresh start. Two independent
quantities:

- **Debt** — context carried above a fresh session. A fixed cost per future call,
  owed whether the pane is working or parked.
- **Interest rate** — how fast calls are happening right now. How quickly the debt
  is actually being paid.

**Debt decides whether the signal appears. Pace decides how loud it is.** A parked
pane still shows a signal, because the debt is real and will be paid the moment it
resumes.

### D2. Two states, two different remedies

| State | Meaning | What you should do |
|---|---|---|
| High debt, **active** | Every call is overpaying, right now | `/clear` or `/compact` — payback is measured in minutes |
| High debt, **parked** | A liability waiting to be resumed | Write a handoff instead of picking it up as-is |

The parked case is the one a rate-only design gets wrong. Its advice is also the
cheapest to follow — nothing is in flight to interrupt — and `~/.claude/handoffs/`
already exists as where handoffs go.

Parked is in fact *under*-modeled by the payback formula: on the 1-hour cache TTL,
an idle pane's cache entry expires, so its resume call pays a full `2.0×` **write**
of `cur_ctx` rather than a `0.1×` read. Payback for a long-parked pane is
effectively immediate.

**Active vs. parked is derived from the last usage record's timestamp**, not from
pace. A 30-minute rolling average still reports two-thirds of former pace ten
minutes after a pane stops, which would show the active copy — the wrong remedy —
for up to half an hour.

### D3. No new chip — the context chip changes meaning

The rail row is already crowded (icon, label, model/context%, account, profile,
pin, cwd, status, PR, Linear, git). This feature adds **no new chip**. The existing
context chip renders on every Claude pane in both layouts and already has a
three-band color system with CSS written for all three states.

What changes is the *input* to that banding — from raw context-window percent to a
cost-pressure score — and the tooltip, which becomes a sentence naming the
recommended action.

Two small render changes are unavoidable and were missed in the original decision:

- The compact render (`RailRows.jsx:210`) has **no `title` attribute** and
  `.pane-mini-ctx` (`styles.css:2577`) has **no `cursor: help`**. Both are needed
  for the hint to be reachable. One attribute, one CSS line.
- Both renders are gated on `w.context_pct != null` (`RailRows.jsx:209, 282`), and
  `context_pct` comes from scraping Claude's TUI status line (`panes.py:680`). A
  pane whose status line doesn't parse would receive a server-side band and paint
  nothing. The chip must render when cost data exists even if `context_pct` does
  not — displaying the context tokens in that case.

The displayed number stays the context percentage when it is available. It is
familiar, it is already the right quantity, and a number whose meaning flickers
between "percent" and "minutes" is worse than a stable one with a better tooltip.

### D4. `base_ctx` is measured per session, then capped

The break-even math needs to know what a *fresh* session in this pane would cost —
which varies by repo (CLAUDE.md size), MCP server set, and wrapper profile. Rather
than a magic constant, it is read from the session's own first usage record.

`base_ctx = min(first_real_usage_record_context, _BASE_CAP)`, with
`_BASE_CAP = 80_000`. Measured fresh-session first records run p50 50.7k, p95
67.9k, max 71.0k, so 80k sits above every observed fresh floor while staying an
order of magnitude below any plausible inherited transcript.

The cap is insurance against a session that inherits history rather than starting
fresh. `turns.py:27` documents that Claude mints a new session id when a
conversation is resumed or compacted. **This spec's earlier claim that such a
transcript's first usage record "can already be 400k" is unverified** — the corpus
contains no compaction markers and no large-headed transcripts. The cap is cheap
and the failure it prevents is severe, so it stays; the magnitude does not.

### D5. 🔥 and `annotate_hot_panes` are deleted, not left alongside

`burn_hot` requires three ANDed conditions — account session meter hot, pane
carrying ≥40% of all burn, default account only (`usage.py:552-566`). It has never
been observed to fire. The active-and-expensive case it reached for is now the red
context chip.

Deleting it costs one capability: "which pane is eating the quota" when near a
limit. That is accepted — and it was a weaker capability than it appeared, because
`_weighted_burn_from_jsonl` reads only the main transcript. Measured over 7 days
across 30 sessions with subagents, subagent spend is 230M weighted tokens against
the main threads' 681M — **25% of spend the burn read cannot see**, running as high
as 3.7× the main thread on individual sessions.

This does not affect the payback math: clearing the main thread saves only
main-thread calls, so main-JSONL pace is the correct denominator. It does mean
`pace` must never be reused elsewhere as a spend proxy without accounting for the
2–4× undercount.

### D6. Calibration targets ~half of sessions in warn, and hot stays rare

Thresholds are set from measured percentiles, not a-priori reasoning. Replaying
this formula over sessions active in the last 7 days (n=53): `payback_calls`
p25 = 2.3, p50 = 3.8, p75 = 9.4; `cur_ctx` p50 = 300k, p90 = 723k.

**Warn is set at p50** — most working panes genuinely are past break-even, and
saying so is information rather than noise, because warn is a recolor of a number
already on screen rather than a new mark. **Hot must stay rare**, since its remedy
(`/clear`) is disruptive; it is gated on both a tight payback time and the pane
actually being active.

**Measured result, 2026-08-19** — the shipped constants replayed over the live
corpus. No constant needed changing:

```
sessions active in the last 7 days: 56
            warn    28   50.0%
            none    27   48.2%
             hot     1    1.8%
payback_calls  p25=2.3  p50=3.8  p75=11.6
cur_ctx        p50=299k  p90=730k
```

Warn landed at exactly the 50% target; hot stayed rare at 1.8%. These percentiles
independently reproduce the ones measured during spec review from a separate
implementation (p25 2.3, p50 3.8; `cur_ctx` p50 ≈300k, p90 ≈723k), which is the
strongest available evidence that the metric measures what it claims to.

The originally-proposed values put 12 of 15 recently-active panes in hot;
shipping a threshold nobody has replayed is how that happens twice.

**That calibration output is a deliverable, not a gate.** The finding that ~75% of
sessions sit past break-even is itself the most valuable thing measured here: it
says the expensive state is this user's business-as-usual rather than an anomaly,
which is why the bands are anchored to the measured distribution rather than to an
absolute break-even. A per-pane chip can never surface that — it describes one
pane, while the distribution describes a working style, and a ~50k fresh start
against a ~300k steady state is a permanent 6× structural fact. The calibration
script therefore reports the full distribution (`payback_calls` and `cur_ctx`
percentiles, band shares) and its output is kept, not discarded once thresholds
are chosen. Re-running it later is how threshold drift gets noticed.

### D7. Thresholds are module constants, not settings

Named constants in `usage.py`, with reasoning in comments. No config surface, no
prefs key, until there is evidence one is needed.

---

## The metric

Per API call, a pane re-reads its whole context at 0.1× base input rate (the
`_W_CACHE_R` weight periscope already uses). Clearing drops that to `base_ctx`, at
the cost of re-writing the base as a cache write (`2.0×` on the 1-hour TTL a
subscription gets by default).

```
debt          = cur_ctx - base_ctx                  tokens
payback_calls = (2.0 × base_ctx) / (0.1 × debt)     = 20 × base_ctx / debt
pace          = usage records per minute over the burn window
payback_mins  = payback_calls / pace                (undefined when pace is 0)
active        = now - last_usage_record_ts <= _ACTIVE_WITHIN_S
```

**`payback_calls` counts API calls, not prompts.** Every tool round-trip emits its
own usage record; measured at 10–26 usage records per human turn. The math is
correct in this unit — each call re-reads the context — but the naming must not
invite an implementer to filter to human turns, which would inflate `payback_mins`
by ~20× and prevent `hot` from ever firing.

**Banding:**

| Class | Condition |
|---|---|
| (plain) | `payback_calls > _PAYBACK_CALLS_WARN`, or no cost data |
| `ctx-warn` | `payback_calls <= _PAYBACK_CALLS_WARN` |
| `ctx-hot` | warn **and** `active` **and** `payback_mins <= _PAYBACK_MINS_HOT` |

Starting constants, to be confirmed by the D6 calibration step:

| Constant | Value | Rationale |
|---|---|---|
| `_BASE_CAP` | `80_000` | Above the measured 71k fresh-session max |
| `_PAYBACK_CALLS_WARN` | `4` | Measured p50 (≈6× base, ≈300k context) |
| `_PAYBACK_MINS_HOT` | `2` | Keeps hot rare; original `5` put 12/15 panes hot |
| `_ACTIVE_WITHIN_S` | `300` | A pane with no API call in 5 min is not working |

---

## Record selection

Two record types must be excluded when choosing the first and last usage records.

**`<synthetic>` records.** Claude Code writes an `assistant` record with
`model: "<synthetic>"` and a fully-populated but all-zero usage block for
interrupts, API errors, and rate-limit messages. Measured across 337 transcripts:
9 files end on one and 9 begin on one. Unfiltered, they break the signal in both
directions, and they occur exactly when the user is already looking at the
dashboard:

- Ending on one → `cur_ctx = 0` → `debt` negative → **plain on a half-million-token
  pane**. One real transcript ends with a zero record reading *"You've reached your
  Fable 5 limit"* immediately after a 503,613-token record.
- Beginning on one → `base_ctx = 0` → `payback_calls = 0` and `payback_mins = 0` →
  **instant hot**.

Skip any record whose four token fields sum to zero, or whose `message.model` is
`<synthetic>`. Additionally guard `base_ctx <= 0` → no annotation.

**Sidechain records: no filter needed, verified.** Across 187 August transcripts,
131,964 records carry an `isSidechain` key and **zero** are `true`; zero carry a
usage block; zero of 179 files end on one. Subagent transcripts live exclusively at
`<encoded-cwd>/<session-uuid>/subagents/agent-*.jsonl`, one directory below the
main file, which `jsonl_for_session`'s single-level glob (`turns.py:47`) cannot
match. This is version-dependent — re-verify if record shapes change.

---

## Data flow

Everything needed is already read. `usage._weighted_burn_from_jsonl()` runs every
`PANE_BURN_REFRESH_S` (60s) per Claude pane inside a `_bg` background thread,
reading a bounded 4MB tail and parsing each record's `message.usage`. It currently
returns one float.

1. **Extend the tail read** to also return, from the same single pass:
   - `cur_ctx` — the last non-synthetic usage record's
     `input + cache_creation + cache_read`, **regardless of timestamp**. The
     existing loop `continue`s on `ts < cutoff` before reading usage
     (`usage.py:499`), so a pane parked longer than the 30-minute window would
     otherwise yield nothing — silencing D2's parked case entirely. Only `pace` is
     cutoff-filtered.
   - `last_ts` — that record's timestamp, for the `active` determination.
   - `pace` — non-synthetic usage records within the cutoff ÷
     `_PANE_BURN_WINDOW_S / 60`.
2. **Add a cached head read** for `base_ctx` — the first non-synthetic usage record
   in the JSONL. Median 86KB to reach it (p99 428KB), read forward and stop. It
   runs **in the same background refresh**, never on the `/api/state` path. Cached
   keyed on session id; **only a successful non-zero read is cached**, so a
   brand-new session with no usage record yet is retried rather than frozen at
   `None` for its lifetime.
3. **`pane_burn_rates()` becomes `pane_cost_pressure()`**, returning the raw
   *measurements* per pane id (`cur_ctx`, `base_ctx`, `pace`, `last_ts`) — not a
   score — through the same stale-while-revalidate cache and the same `_bg`
   refresh. **Banding happens at annotate time, against a fresh clock.** `active`
   is a clock comparison and the cache is up to 60s stale, so scoring at refresh
   time would freeze `active` for up to a minute after a pane stops.
4. **`routes/state.py`** replaces its `annotate_hot_panes(result)` call (line 82)
   with `annotate_cost_pressure(result)`, stamping `ctx_class`, `ctx_hint` (the
   tooltip sentence), and `ctx_tokens`.

   **`ctx_class` is `"none" | "warn" | "hot"`, and the key is omitted entirely
   when there is no cost data.** Not `""`: in JS `""` and `undefined` are both
   falsy, so the natural idiom `w.ctx_class || pctBand(w.context_pct)` would
   collapse the two states this design specifically needs to keep apart — *server
   said plain* and *server said nothing* — silently falling a cost-classified pane
   back onto the auto-compact percent bands. The sentinel makes the frontend's
   hardest decision mechanical. CSS class names are unchanged; `"none"` maps to
   `""`.
5. **`RailRows.jsx`** — `ctxClass()` prefers `w.ctx_class`, falling back to today's
   percent bands when absent. Both renders gain a `title` from `w.ctx_hint`, and
   their `context_pct != null` gate widens to also admit `ctx_tokens`.

Classification is server-side: the payback math needs `base_ctx`, `pace`, and
`last_ts`, none of which are on the pane view, and `annotate_hot_panes` sets the
precedent for cost annotation living in `usage.py`.

**Threading constraint.** The new code must call `usage._bg` as a module attribute.
`tests/conftest.py:34-48` neuters `usage._bg` by name to keep DB-touching threads
out of tests; importing `_bg` into another module or using `threading.Thread`
directly defeats that fixture and violates the test-isolation invariant in
CLAUDE.md.

---

## Tooltip copy

- **hot** — `each call re-reads 600k of context; clearing pays for itself in ~2 min at this pace`
- **warn, parked** — `carrying 600k of context (≈8× a fresh start) — write a handoff before resuming rather than picking this up as-is`
- **warn, active** — `carrying 600k of context; clearing pays back in ~4 calls`
- **plain, with cost data** — `context window used — near a fresh start`
- **plain, no cost data** — unchanged: `context window used`

The percent-band fallback keeps two meanings on one color: cost pressure for panes
with a resolvable session, auto-compact proximity for those without. Considered
dropping the fallback to keep the color honest, and rejected — losing the
compaction warning is a real regression in a different feature. The tooltip always
names which meaning applies, which is enough.

---

## Failure modes and edges

- **No resolvable session** (`session_id_for_pane` returns None — reachable, since
  `turns.py:36-37` has no cwd fallback) → no annotation; `ctxClass` falls back to
  today's percent bands. Never worse than current behavior.
- **Session with no usage record yet** → no `cur_ctx`, no annotation, `base_ctx`
  not cached.
- **`debt <= 0` or `base_ctx <= 0`** → plain. Guard the division.
- **`pace == 0`** → `payback_mins` undefined → warn at most, never hot. Correct: a
  parked pane gets the handoff copy.
- **Auto-compaction** drops `cur_ctx` on its own, so the signal clears itself. This
  feature matters most on 1M-context models, where the auto-compact threshold is
  far away and a session can sit at 600k for hours.
- **Model differences.** The 0.1× / 2.0× multipliers are ratios to base input and
  are identical across every current model, so the payback math is
  model-independent. Only absolute dollars would need per-model rates, and this
  feature never shows dollars.
- **5m vs 1h cache TTL.** The payback math uses its own `_CACHE_WRITE_MULT = 2.0`,
  because a subscription gets the 1-hour TTL by default. On usage credits the TTL
  drops to 5 minutes (1.25×), making clearing *cheaper* than modeled —
  conservative in the right direction.

  An earlier draft of this spec said `_W_CACHE_W = 1.25` stays "for the burn
  weighting it already feeds". **That is wrong — nothing feeds it.** All four
  `_W_*` weights (`usage.py:466`) have exactly one consumer,
  `_weighted_burn_from_jsonl`, whose only consumer chain is
  `_refresh_burn_into_cache` → `pane_burn_rates` → `annotate_hot_panes`, every
  link of which this feature deletes. Keeping a dead weighted sum inside the
  reworked reducer would be a ratchet; all four go.
- **4MB tail vs. a huge final record.** Checked: every transcript with no usage
  record in its 4MB tail was under 200KB and genuinely usage-free. No guard needed.

---

## Testing

- `tests/test_usage.py` — replace the two `annotate_hot_panes` tests
  (lines 260-289) with coverage of: banding at each threshold; `debt <= 0` and
  `base_ctx <= 0` guards; the `_BASE_CAP` clamp; `<synthetic>` records skipped at
  both head and tail; `cur_ctx` surviving a cutoff-older-than-window transcript
  (the parked case); `pace == 0` yielding warn-not-hot; and the no-session
  no-annotation path. Fixtures are small synthetic JSONL files; the existing tests
  already build pane views as plain dicts.
- `static/src/split/__tests__/railRender.test.jsx` — the three chip states, the
  fallback when `ctx_class` is absent, and the render path when `context_pct` is
  null but `ctx_tokens` is present.
- **Calibration check (D6)** — replay the final constants over
  `~/.claude/projects` and report the band distribution across recently-active
  panes. Not a unit test; a one-shot script whose output goes in the PR.
- `bin/check` must stay at zero violations.
- **`npm run build` and commit `static/dist/app.js`** — required whenever
  `static/src/` changes.

---

## Deletions

- `usage.annotate_hot_panes` (`usage.py:543-568`) and its import + call site
  (`routes/state.py:28, 82`). `state.py` keeps its own `cached_plan_usage()` at
  line 166, so that import survives.
- `_HOT_PANE_SHARE` (`usage.py:465`) and all four `_W_*` weights (`usage.py:466`)
  — their only consumers go with `annotate_hot_panes` and
  `_weighted_burn_from_jsonl`, and ruff does not flag unused module-level private
  constants.
- `_weighted_burn_from_jsonl` itself (`usage.py:473-510`), replaced by the tail
  reader described in Data flow.
- The `rail-burn` JSX block (`RailRows.jsx:212-217`) and `.rail-burn` CSS
  (`styles.css:254`).
- Update `docs/mockups/rail-cards.html:434`, which documents the 🔥 chip.

Verified caller inventory: `annotate_hot_panes` → `routes/state.py:28, 82` only.
`pane_burn_rates` → `usage.py:560` + `tests/test_usage.py:265, 284`.
`burn_hot`/`burn_wtpm` → `RailRows.jsx:212-216` only. `ctxClass` →
`RailRows.jsx:210, 283` only.

---

## Out of scope

- Per-session historical cost in the history index. Separately valuable, shares the
  extraction idea, not needed for a live flag.
- Dollar figures anywhere in the UI. Units stay weighted tokens and time.
- Any automatic action. The signal advises; clearing stays a human decision.
- Subagent spend attribution, despite D5's 25% blind spot. Correct denominator for
  this feature is main-thread pace.
- Multi-account burn attribution. `_PROJECTS_DIR` still holds one account's
  transcripts, exactly as `annotate_hot_panes` documented.
