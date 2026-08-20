# Pane Cost Pressure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace periscope's never-firing 🔥 burn flag with a live per-pane signal that says when a pane's accumulated context has grown expensive enough that clearing it pays — and which remedy to use.

**Architecture:** A new pure module `periscope/cost_pressure.py` holds every decision (record selection, tail reduction, payback arithmetic, banding, tooltip copy) with zero imports beyond stdlib and zero-fixture tests. `periscope/usage.py` keeps all I/O, caching, and threading, reworking its per-pane burn block in place. The frontend reuses the existing context chip: only its colour input and tooltip change.

**Tech Stack:** Python 3.14 + `uv` (pytest, ruff, ty), Preact + `@preact/signals` built by Vite (vitest), Biome.

**Spec:** `docs/superpowers/specs/2026-08-19-pane-cost-pressure-design.md`
**Structure:** `docs/superpowers/specs/2026-08-19-pane-cost-pressure-structure.md`

---

## Model tier per task

Each task is labelled with the cheapest model that can execute it correctly. The tier is earned by how much judgment survives into the code, not by how many lines change.

| Task | Tier | What it is |
|---|---|---|
| 1 | **haiku** | Transcribe constants + four frozen dataclasses |
| 2 | **opus** | `parse_usage_record` — the one genuinely ambiguous unit |
| 3 | **sonnet** | `summarize_tail` — enumerable cases |
| 4 | **sonnet** | `score` — pure arithmetic + banding |
| 5 | **sonnet** | `hint` — four sentences selected by `(band, active)` |
| 6 | **sonnet** | Two file readers on real temp files |
| 7 | **sonnet** | Cache + background refresh, mirroring the existing shape |
| 8 | **sonnet** | `annotate_cost_pressure` + route wiring |
| 9 | **haiku** | Delete the dead burn path and its tests |
| 10 | **sonnet** | Frontend pure helpers + their tests |
| 11 | **haiku** | Render sites, `title` attributes, CSS |
| 12 | **haiku** | `npm run build` + commit the bundle |
| 13 | **sonnet** | Calibration script + record the distribution |
| 14 | **haiku** | Docs: CLAUDE.md row, mockup page |

**Worktree:** all work happens in a worktree created with `EnterWorktree` (name: `cost-pressure`). Do not run `git worktree add` by hand. `main` is pushed as of `faf2695`.

**Addressing edits:** this repo commits straight to `main` and did so *during* the design of this feature (`faf2695`, 20:47, touched `periscope/usage.py` and `static/styles.css`). **Locate every edit by the quoted anchor text, not by line number.** Line numbers in this plan are hints for orientation and may be stale.

---

## File Structure

```
periscope/cost_pressure.py               CREATE  pure decision core, stdlib only
tests/test_cost_pressure.py              CREATE  pure unit tests, zero fixtures
periscope/usage.py                       MODIFY  rework the per-pane burn block
tests/test_usage.py                      MODIFY  reader tests on real temp files
periscope/routes/state.py                MODIFY  one import, one call
static/src/split/RailRows.jsx            MODIFY  helpers, render gates, delete flame
static/src/split/__tests__/railRender.test.jsx  MODIFY  helper + render assertions
static/styles.css                        MODIFY  cursor:help, delete .rail-burn
static/dist/app.js                       REBUILD committed build artifact
docs/mockups/rail-cards.html             MODIFY  drop the 🔥 documentation
CLAUDE.md                                MODIFY  one row in the module table
```

`cost_pressure.py` owns everything testable without a filesystem. `usage.py` owns everything that touches one. That boundary is what lets Tasks 2–5 be implemented from signatures alone.

---

## Task 1: `cost_pressure.py` skeleton — constants and value objects

**Tier: haiku.** Pure transcription. Every value is given below.

**Files:**
- Create: `periscope/cost_pressure.py`
- Test: `tests/test_cost_pressure.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cost_pressure.py`:

```python
"""Pure unit tests for the cost-pressure decision core. No fixtures, no files."""

from periscope import cost_pressure as cp


def test_constants_match_the_calibrated_values():
    assert cp._BASE_CAP == 80_000
    assert cp._PAYBACK_CALLS_WARN == 4
    assert cp._PAYBACK_MINS_HOT == 2
    assert cp._ACTIVE_WITHIN_S == 300
    assert cp._CACHE_READ_MULT == 0.1
    assert cp._CACHE_WRITE_MULT == 2.0


def test_value_objects_are_frozen():
    import dataclasses
    import pytest

    rec = cp.UsageRecord(ts=1.0, ctx_tokens=100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.ctx_tokens = 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.cost_pressure'`

- [ ] **Step 3: Write the module**

Create `periscope/cost_pressure.py`:

```python
"""Pure decision core for per-pane cost pressure.

No I/O, no threads, no state. Every ambiguous choice in this feature lives in
exactly one function here, which is what makes `usage.py`'s readers thin and
this module testable with zero fixtures. Modelled on `narrator.py`, whose
decision core is pure for the same reason.

The model: a Claude pane re-sends its whole conversation on every API call,
billed at the cache-read rate. A long-running pane therefore overpays on every
future call by an amount set by how far its context has grown past a fresh
start. Clearing costs one cache WRITE of the base context and then makes every
subsequent call cheap, so the question "should this be cleared" reduces to "how
many calls until that write pays for itself".
"""

from dataclasses import dataclass
from typing import Literal

Band = Literal["none", "warn", "hot"]

# Above the measured fresh-session ceiling (p50 50.7k, p95 67.9k, max 71.0k on
# this machine's corpus), so a genuinely fresh session is never clamped, while a
# transcript that inherited history stays an order of magnitude above it.
_BASE_CAP = 80_000

# Measured p50 of payback_calls across sessions active in the last 7 days.
# Warn is deliberately common: most working panes ARE past break-even, and warn
# is a recolour of a number already on screen rather than a new mark.
_PAYBACK_CALLS_WARN = 4

# Hot must stay rare — its remedy (/clear) is disruptive. An earlier value of 5
# put 12 of 15 recently-active panes in hot.
_PAYBACK_MINS_HOT = 2

# A pane with no API call in five minutes is not working, so it gets the parked
# remedy ("write a handoff") rather than the active one ("clear now").
_ACTIVE_WITHIN_S = 300

# Ratios to base input price, identical across every current model — which is
# why this feature is model-independent and never shows dollars.
_CACHE_READ_MULT = 0.1   # re-reading the whole context, once per API call
_CACHE_WRITE_MULT = 2.0  # re-writing base_ctx on the 1h TTL a subscription gets


@dataclass(frozen=True)
class UsageRecord:
    """One real assistant response's usage, already validated."""

    ts: float
    ctx_tokens: int  # input + cache_creation + cache_read


@dataclass(frozen=True)
class TailSummary:
    """What one pass over a transcript tail yields."""

    cur_ctx: int | None  # last record's context, IGNORING the cutoff
    last_ts: float | None
    calls: int  # records at/after the cutoff — API calls, not prompts


@dataclass(frozen=True)
class CostSample:
    """Clock-free measurement for one pane. This is what the cache holds."""

    cur_ctx: int
    base_ctx: int
    pace: float  # calls per minute over the burn window
    last_ts: float


@dataclass(frozen=True)
class Pressure:
    """A scored sample, ready to render."""

    band: Band
    active: bool
    cur_ctx: int
    payback_calls: float
    payback_mins: float | None  # None when pace is zero
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/cost_pressure.py tests/test_cost_pressure.py
git commit -m "feat(cost-pressure): pure decision core skeleton — constants and frozen value objects"
```

---

## Task 2: `parse_usage_record` — the judgment unit

**Tier: opus.** This is the only genuinely ambiguous unit on the server. It answers "is this record real?" once, and both readers consume that single answer — which is what makes "skip `<synthetic>` at both ends" a structural guarantee rather than a discipline someone has to remember.

**Files:**
- Modify: `periscope/cost_pressure.py` (append)
- Test: `tests/test_cost_pressure.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cost_pressure.py`:

```python
def _rec(**over):
    """A well-formed assistant usage record, overridable per case."""
    rec = {
        "type": "assistant",
        "timestamp": "2026-08-19T12:00:00.000Z",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 300,
                "output_tokens": 40,
            },
        },
    }
    rec.update(over)
    return rec


def test_parse_sums_context_but_not_output():
    got = cp.parse_usage_record(_rec())
    assert got is not None
    assert got.ctx_tokens == 330  # 10 + 20 + 300; output is NOT context


def test_parse_reads_the_timestamp_as_epoch_seconds():
    got = cp.parse_usage_record(_rec())
    assert got is not None
    assert got.ts == 1787140800.0


def test_parse_rejects_synthetic_model():
    """Claude Code writes model=<synthetic> for interrupts, API errors and limit
    messages. Nine transcripts in the corpus END on one and nine BEGIN on one."""
    rec = _rec()
    rec["message"]["model"] = "<synthetic>"
    assert cp.parse_usage_record(rec) is None


def test_parse_rejects_all_zero_token_block():
    rec = _rec()
    rec["message"]["usage"] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    assert cp.parse_usage_record(rec) is None


def test_parse_keeps_an_output_only_record():
    """Output-only is real work, not a synthetic placeholder — the four fields
    do not all sum to zero, so it survives even though its context is zero."""
    rec = _rec()
    rec["message"]["usage"] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 99,
    }
    got = cp.parse_usage_record(rec)
    assert got is not None
    assert got.ctx_tokens == 0


def test_parse_rejects_non_assistant_and_missing_pieces():
    assert cp.parse_usage_record(_rec(type="user")) is None
    assert cp.parse_usage_record(_rec(timestamp=None)) is None
    assert cp.parse_usage_record(_rec(timestamp="not-a-date")) is None
    assert cp.parse_usage_record(_rec(message={})) is None
    assert cp.parse_usage_record(_rec(message={"model": "claude-opus-5"})) is None
    assert cp.parse_usage_record({}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cost_pressure.py -v -k parse`
Expected: FAIL — `AttributeError: module 'periscope.cost_pressure' has no attribute 'parse_usage_record'`

- [ ] **Step 3: Write the implementation**

Append to `periscope/cost_pressure.py` (add `from datetime import datetime` to the imports at the top):

```python
def parse_usage_record(rec: dict) -> UsageRecord | None:
    """One record from a session JSONL, or None if it is not a real API call.

    Rejecting the right records is the whole job. Claude Code writes an
    `assistant` record with `model: "<synthetic>"` and a fully-populated but
    all-zero usage block for interrupts, API errors and rate-limit messages.
    Unfiltered, those break the signal in both directions, and they land exactly
    when the user is already looking at the dashboard: one real transcript ends
    with a zero record reading "You've reached your Fable 5 limit" immediately
    after a 503,613-token record. Ending on one would report cur_ctx=0 and paint
    a half-million-token pane plain; beginning on one would report base_ctx=0 and
    paint it hot instantly.

    Sidechain records need no filter: on Claude Code 2.1.236 subagent transcripts
    live in a sibling `<session-uuid>/subagents/` directory, and zero of 72,999
    August usage records in main transcripts carry `isSidechain: true`. Re-verify
    if record shapes change.
    """
    if rec.get("type") != "assistant":
        return None
    message = rec.get("message") or {}
    if message.get("model") == "<synthetic>":
        return None
    usage = message.get("usage") or {}
    if not usage:
        return None

    ts_str = rec.get("timestamp")
    if not isinstance(ts_str, str):
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

    fresh = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    # An all-zero block is a synthetic placeholder even when the model field
    # doesn't say so. An output-only record is real work and survives.
    if fresh + written + read + out == 0:
        return None

    return UsageRecord(ts=ts, ctx_tokens=fresh + written + read)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/cost_pressure.py tests/test_cost_pressure.py
git commit -m "feat(cost-pressure): parse_usage_record rejects synthetic and all-zero records at both ends"
```

---

## Task 3: `summarize_tail` — reduce parsed records to a summary

**Tier: sonnet.** Fully enumerable. The one subtlety is stated explicitly: `cur_ctx` ignores the cutoff, `calls` respects it.

**Files:**
- Modify: `periscope/cost_pressure.py` (append)
- Test: `tests/test_cost_pressure.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cost_pressure.py`:

```python
def test_summarize_tail_counts_only_records_at_or_after_cutoff():
    records = [
        cp.UsageRecord(ts=100.0, ctx_tokens=1000),
        cp.UsageRecord(ts=200.0, ctx_tokens=2000),
        cp.UsageRecord(ts=300.0, ctx_tokens=3000),
    ]
    got = cp.summarize_tail(records, cutoff=200.0)
    assert got.calls == 2
    assert got.cur_ctx == 3000
    assert got.last_ts == 300.0


def test_summarize_tail_keeps_cur_ctx_when_everything_predates_the_cutoff():
    """The parked case. A pane idle longer than the window has no records in it,
    but its debt is real and must still be reported."""
    records = [cp.UsageRecord(ts=100.0, ctx_tokens=600_000)]
    got = cp.summarize_tail(records, cutoff=99_999.0)
    assert got.calls == 0
    assert got.cur_ctx == 600_000
    assert got.last_ts == 100.0


def test_summarize_tail_on_no_records():
    got = cp.summarize_tail([], cutoff=0.0)
    assert got == cp.TailSummary(cur_ctx=None, last_ts=None, calls=0)


def test_summarize_tail_takes_the_last_record_not_the_largest():
    records = [
        cp.UsageRecord(ts=100.0, ctx_tokens=900_000),
        cp.UsageRecord(ts=200.0, ctx_tokens=50_000),
    ]
    got = cp.summarize_tail(records, cutoff=0.0)
    assert got.cur_ctx == 50_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cost_pressure.py -v -k summarize`
Expected: FAIL — `AttributeError: module 'periscope.cost_pressure' has no attribute 'summarize_tail'`

- [ ] **Step 3: Write the implementation**

Add `from collections.abc import Iterable` to the imports at the top of `periscope/cost_pressure.py` — this task is its first consumer, and importing it in Task 1 would have failed ruff's F401 on the Task 1 and Task 2 commits.

Then append:

```python
def summarize_tail(records: Iterable[UsageRecord], *, cutoff: float) -> TailSummary:
    """Reduce one pass over a transcript tail.

    `cur_ctx` and `last_ts` IGNORE the cutoff; `calls` respects it. That
    asymmetry is the parked case: a pane idle longer than the burn window has no
    records inside it, but its context debt is real and is owed the moment it
    resumes. Filtering everything by the cutoff would silence exactly the panes
    the parked remedy exists for.
    """
    cur_ctx: int | None = None
    last_ts: float | None = None
    calls = 0
    for rec in records:
        cur_ctx = rec.ctx_tokens
        last_ts = rec.ts
        if rec.ts >= cutoff:
            calls += 1
    return TailSummary(cur_ctx=cur_ctx, last_ts=last_ts, calls=calls)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/cost_pressure.py tests/test_cost_pressure.py
git commit -m "feat(cost-pressure): summarize_tail keeps cur_ctx across the cutoff so parked panes still report"
```

---

## Task 4: `score` — payback arithmetic and banding

**Tier: sonnet.** Pure arithmetic over four numbers; every branch is enumerated below.

**Files:**
- Modify: `periscope/cost_pressure.py` (append)
- Test: `tests/test_cost_pressure.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cost_pressure.py`:

```python
def _sample(**over):
    kw = {"cur_ctx": 300_000, "base_ctx": 50_000, "pace": 2.0, "last_ts": 1000.0}
    kw.update(over)
    return cp.CostSample(**kw)


def test_score_computes_payback_calls():
    # 2.0 * 50_000 / (0.1 * 250_000) == 4.0
    got = cp.score(_sample(), now=1000.0)
    assert got is not None
    assert got.payback_calls == 4.0
    assert got.payback_mins == 2.0  # 4 calls / 2.0 calls-per-min


def test_score_clamps_base_ctx_at_the_cap():
    """The clamp lives here, not in the reader — the reader stays honest about
    what it saw, and the clamp is testable without touching a file."""
    got = cp.score(_sample(base_ctx=500_000, cur_ctx=900_000), now=1000.0)
    assert got is not None
    # base clamped to 80_000: 2.0 * 80_000 / (0.1 * 820_000)
    assert round(got.payback_calls, 4) == round(160_000 / 82_000, 4)


def test_score_returns_none_when_there_is_nothing_to_say():
    assert cp.score(_sample(base_ctx=0), now=1000.0) is None
    assert cp.score(_sample(cur_ctx=50_000, base_ctx=50_000), now=1000.0) is None
    assert cp.score(_sample(cur_ctx=10_000, base_ctx=50_000), now=1000.0) is None


def test_score_bands_plain_above_the_warn_threshold():
    # base 50k, cur 100k -> debt 50k -> 2.0*50_000/(0.1*50_000) == 20 calls
    got = cp.score(_sample(cur_ctx=100_000), now=1000.0)
    assert got is not None
    assert got.band == "none"


def test_score_bands_warn_at_the_threshold():
    got = cp.score(_sample(), now=1000.0)  # exactly 4.0 calls
    assert got is not None
    assert got.band in ("warn", "hot")
    assert got.payback_calls == 4.0


def test_score_bands_hot_only_when_active_and_fast():
    fast = cp.score(_sample(pace=8.0), now=1000.0)  # 4 calls / 8 per min = 0.5 min
    assert fast is not None
    assert fast.band == "hot"
    assert fast.active is True


def test_score_parked_pane_is_warn_never_hot():
    """Idle past _ACTIVE_WITHIN_S: the remedy is a handoff, not /clear."""
    got = cp.score(_sample(pace=8.0), now=1000.0 + 301)
    assert got is not None
    assert got.active is False
    assert got.band == "warn"


def test_score_zero_pace_gives_no_payback_mins_and_never_hot():
    got = cp.score(_sample(pace=0.0), now=1000.0)
    assert got is not None
    assert got.payback_mins is None
    assert got.band == "warn"


def test_score_active_boundary_is_inclusive():
    assert cp.score(_sample(pace=8.0), now=1000.0 + 300).active is True
    assert cp.score(_sample(pace=8.0), now=1000.0 + 301).active is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cost_pressure.py -v -k score`
Expected: FAIL — `AttributeError: module 'periscope.cost_pressure' has no attribute 'score'`

- [ ] **Step 3: Write the implementation**

Append to `periscope/cost_pressure.py`:

```python
def score(sample: CostSample, *, now: float) -> Pressure | None:
    """Band one pane's measurement against a fresh clock, or None for silence.

    None is the single "no annotation" contract, covering both a base we cannot
    trust (<= 0) and a pane that has not grown past its own fresh start
    (debt <= 0). The caller already handles absence for panes with no session
    and no usage record, so this collapses three silent paths into one.

    Scoring happens here rather than at cache-refresh time because `active` is a
    clock comparison and the cache is up to 60s stale — freezing `active` at
    refresh time would keep showing the active remedy for a minute after a pane
    stopped working.
    """
    base = min(sample.base_ctx, _BASE_CAP)
    if base <= 0:
        return None
    debt = sample.cur_ctx - base
    if debt <= 0:
        return None

    payback_calls = (_CACHE_WRITE_MULT * base) / (_CACHE_READ_MULT * debt)
    payback_mins = payback_calls / sample.pace if sample.pace > 0 else None
    active = (now - sample.last_ts) <= _ACTIVE_WITHIN_S

    band: Band = "none"
    if payback_calls <= _PAYBACK_CALLS_WARN:
        band = "warn"
        if active and payback_mins is not None and payback_mins <= _PAYBACK_MINS_HOT:
            band = "hot"

    return Pressure(
        band=band,
        active=active,
        cur_ctx=sample.cur_ctx,
        payback_calls=payback_calls,
        payback_mins=payback_mins,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: PASS, 21 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/cost_pressure.py tests/test_cost_pressure.py
git commit -m "feat(cost-pressure): score() computes payback and bands, clamping base and refusing to speak without debt"
```

---

## Task 5: `hint` — the tooltip sentence

**Tier: sonnet.** Four sentences selected by `(band, active)`. Kept separate from `score` so editing copy can never break a banding assertion.

**Files:**
- Modify: `periscope/cost_pressure.py` (append)
- Test: `tests/test_cost_pressure.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cost_pressure.py`:

```python
def test_hint_hot_names_the_payback_time_and_the_remedy():
    p = cp.Pressure(band="hot", active=True, cur_ctx=600_000,
                    payback_calls=4.0, payback_mins=2.0)
    got = cp.hint(p)
    assert "600k" in got
    assert "2 min" in got
    assert "clearing" in got.lower()


def test_hint_warn_parked_points_at_a_handoff_not_a_clear():
    p = cp.Pressure(band="warn", active=False, cur_ctx=600_000,
                    payback_calls=4.0, payback_mins=None)
    got = cp.hint(p)
    assert "handoff" in got.lower()
    assert "clearing pays" not in got.lower()


def test_hint_warn_active_names_the_payback_in_calls():
    p = cp.Pressure(band="warn", active=True, cur_ctx=300_000,
                    payback_calls=4.0, payback_mins=30.0)
    got = cp.hint(p)
    assert "300k" in got
    assert "4 calls" in got


def test_hint_plain_says_it_is_near_a_fresh_start():
    p = cp.Pressure(band="none", active=True, cur_ctx=90_000,
                    payback_calls=20.0, payback_mins=10.0)
    assert "fresh start" in cp.hint(p)


def test_fmt_tokens_rounds_to_k():
    assert cp.fmt_tokens(600_000) == "600k"
    assert cp.fmt_tokens(1_500) == "2k"
    assert cp.fmt_tokens(0) == "0k"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cost_pressure.py -v -k "hint or fmt"`
Expected: FAIL — `AttributeError: module 'periscope.cost_pressure' has no attribute 'hint'`

- [ ] **Step 3: Write the implementation**

Append to `periscope/cost_pressure.py`:

```python
def fmt_tokens(n: int) -> str:
    """600_000 -> '600k'. The frontend formats its own chip text; this is only
    for the sentence, and the two-line duplication is cheaper than shipping a
    pre-formatted display string for one chip."""
    return f"{round(n / 1000)}k"


def hint(p: Pressure) -> str:
    """The tooltip sentence. Separate from score() on purpose: copy churns and
    banding does not, so editing a sentence must not touch an arithmetic test.

    The spec's fifth case ("plain, no cost data") is a frontend default, not a
    string from here — in that case the server sends nothing at all.
    """
    ctx = fmt_tokens(p.cur_ctx)
    if p.band == "hot" and p.payback_mins is not None:
        return (f"each call re-reads {ctx} of context; clearing pays for itself "
                f"in ~{round(p.payback_mins)} min at this pace")
    if p.band == "warn" and not p.active:
        return (f"carrying {ctx} of context — write a handoff before resuming "
                f"rather than picking this up as-is")
    if p.band == "warn":
        return (f"carrying {ctx} of context; clearing pays back in "
                f"~{round(p.payback_calls)} calls")
    return f"context window used — near a fresh start ({ctx})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_pressure.py -v`
Expected: PASS, 26 passed

- [ ] **Step 5: Run the gate and commit**

```bash
bin/check
git add periscope/cost_pressure.py tests/test_cost_pressure.py
git commit -m "feat(cost-pressure): hint() renders one of four sentences by band and activity"
```

Expected from `bin/check`: zero violations.

---

## Task 6: The two file readers in `usage.py`

**Tier: sonnet.** Real files on `tmp_path` — never a mocked filesystem. A mocked reader that passes while a real transcript's record shape has drifted is precisely the failure this signal exists to survive.

**Files:**
- Modify: `periscope/usage.py`
- Test: `tests/test_usage.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_usage.py`.

**Do not reuse the existing `_jsonl_line` helper.** It emits `{"timestamp": ..., "message": {"usage": ...}}` with **no `"type"` key**, so `parse_usage_record` rejects every line it produces. These tests need their own helper that writes a well-formed assistant record:

```python
def _assistant_line(ts_iso, usage, model="claude-opus-5"):
    """A well-formed assistant record — the shape parse_usage_record accepts.

    NOTE: this file imports `json as _json` (near line 231), not `json`.
    """
    return _json.dumps({
        "type": "assistant",
        "timestamp": ts_iso,
        "message": {"model": model, "usage": usage},
    })


def _synthetic_line(ts_iso):
    """Claude Code's interrupt/error/limit placeholder: all-zero usage."""
    return _json.dumps({
        "type": "assistant",
        "timestamp": ts_iso,
        "message": {"model": "<synthetic>", "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0}},
    })


def test_tail_summary_ignores_a_synthetic_final_record(tmp_path):
    """A transcript that ends on a limit message must still report its real
    context, not zero."""
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text("\n".join([
        _assistant_line(_iso(now - 60), {"input_tokens": 5,
                                         "cache_read_input_tokens": 503_608}),
        _synthetic_line(_iso(now - 30)),
    ]) + "\n")
    got = usage._tail_summary_from_jsonl(path, cutoff=now - 1800)
    assert got.cur_ctx == 503_613
    assert got.calls == 1


def test_tail_summary_reports_cur_ctx_for_a_fully_parked_transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text(_assistant_line(_iso(now - 7200),
                                    {"cache_read_input_tokens": 600_000}) + "\n")
    got = usage._tail_summary_from_jsonl(path, cutoff=now - 1800)
    assert got.cur_ctx == 600_000
    assert got.calls == 0
    assert got.last_ts is not None


def test_base_ctx_skips_a_synthetic_first_record(tmp_path):
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text("\n".join([
        _synthetic_line(_iso(now - 300)),
        _assistant_line(_iso(now - 200), {"input_tokens": 1,
                                          "cache_creation_input_tokens": 49_999}),
        _assistant_line(_iso(now - 100), {"cache_read_input_tokens": 400_000}),
    ]) + "\n")
    assert usage._base_ctx_from_jsonl(path) == 50_000


def test_readers_return_nothing_on_a_usage_free_transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(_json.dumps({"type": "user", "timestamp": "x"}) + "\n")
    assert usage._tail_summary_from_jsonl(path, cutoff=0).cur_ctx is None
    assert usage._base_ctx_from_jsonl(path) is None


def test_readers_survive_a_missing_file(tmp_path):
    path = tmp_path / "nope.jsonl"
    assert usage._tail_summary_from_jsonl(path, cutoff=0).cur_ctx is None
    assert usage._base_ctx_from_jsonl(path) is None
```

Add this import-and-helper block to `tests/test_usage.py` alongside the new tests. **`import time` is required** — the file has no module-level `time` import (its only two are inside function bodies), so every new test that opens with `now = time.time()` would raise `NameError`:

```python
import time


def _iso(epoch):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_usage.py -v -k "tail_summary or base_ctx or readers"`
Expected: FAIL — `AttributeError: module 'periscope.usage' has no attribute '_tail_summary_from_jsonl'`

- [ ] **Step 3: Write the implementation**

In `periscope/usage.py`, add to the imports at the top:

```python
from periscope.cost_pressure import TailSummary, parse_usage_record, summarize_tail
```

Find the anchor `def _weighted_burn_from_jsonl` and insert the following directly **above** that line:

```python
def _tail_summary_from_jsonl(path: Path, *, cutoff: float) -> TailSummary:
    """Bounded tail read of one transcript. Transcripts run to tens of MB and
    4MB comfortably covers a heavy 30 minutes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
                f.readline()  # discard the partial line
            data = f.read()
    except OSError:
        return TailSummary(cur_ctx=None, last_ts=None, calls=0)

    def _records():
        for line in data.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            parsed = parse_usage_record(rec)
            if parsed is not None:
                yield parsed

    return summarize_tail(_records(), cutoff=cutoff)


def _base_ctx_from_jsonl(path: Path) -> int | None:
    """The session's FIRST real usage record — what a fresh session costs in this
    pane, which varies by repo CLAUDE.md, MCP set and wrapper profile. Median 86KB
    to reach it; stops at the first hit."""
    try:
        with path.open("rb") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                parsed = parse_usage_record(rec)
                if parsed is not None:
                    return parsed.ctx_tokens
    except OSError:
        return None
    return None
```

Add a new constant beside the existing one — do **not** rename `_BURN_TAIL_BYTES`. `_weighted_burn_from_jsonl` still uses it and is not deleted until Task 9; renaming here would break that function and leave this commit red. Find the anchor `_BURN_TAIL_BYTES = 4_000_000` and add directly below it:

```python
_TAIL_BYTES = 4_000_000
```

Task 9 deletes `_BURN_TAIL_BYTES` along with its last consumer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_usage.py -v`
Expected: PASS — all existing tests plus 5 new ones.

- [ ] **Step 5: Commit**

```bash
git add periscope/usage.py tests/test_usage.py
git commit -m "feat(cost-pressure): tail and head transcript readers built on the shared record parser"
```

---

## Task 7: Cache and background refresh

**Tier: sonnet.** Mirrors the existing `_refresh_burn_into_cache` / `pane_burn_rates` shape exactly.

**Critical constraint:** the refresh must be scheduled via `_bg(...)` called as a module global in `usage.py`. `tests/conftest.py` neuters `usage._bg` by name to keep DB-touching threads out of tests; importing `_bg` elsewhere or using `threading.Thread` directly defeats that fixture and violates the test-isolation invariant in CLAUDE.md.

**Files:**
- Modify: `periscope/usage.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_usage.py`:

```python
def test_pane_cost_pressure_serves_cache_and_schedules_refresh(monkeypatch):
    scheduled = []
    monkeypatch.setattr(usage, "_bg", lambda name, fn, *a: scheduled.append(name))
    monkeypatch.setattr(usage, "_cost_cache", (0.0, {"%1": "sample"}))
    monkeypatch.setattr(usage, "_cost_in_flight", False)

    got = usage.pane_cost_pressure(["%1"])

    assert got == {"%1": "sample"}          # stale value served immediately
    assert scheduled == ["pane-cost"]        # refresh scheduled, not awaited


def test_refresh_cost_into_cache_builds_a_sample(tmp_path, monkeypatch):
    """The background path never runs under pytest — conftest neuters usage._bg —
    so it is called directly here or it ships untested."""
    from periscope import turns
    now = time.time()
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text("\n".join([
        _assistant_line(_iso(now - 600), {"cache_creation_input_tokens": 50_000}),
        _assistant_line(_iso(now - 60), {"cache_read_input_tokens": 600_000}),
    ]) + "\n")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sess")
    monkeypatch.setattr(turns, "jsonl_for_session", lambda sid: jsonl)
    monkeypatch.setattr(usage, "_base_ctx_cache", {})
    monkeypatch.setattr(usage, "_cost_in_flight", True)

    usage._refresh_cost_into_cache(["%1"])

    sample = usage._cost_cache[1]["%1"]
    assert sample.cur_ctx == 600_000
    assert sample.base_ctx == 50_000
    assert sample.pace == 2 / 30.0          # 2 calls over the 30-minute window
    assert usage._cost_in_flight is False    # the finally block released it


def test_refresh_cost_into_cache_does_not_memoize_a_zero_base(tmp_path, monkeypatch):
    """Spec Data flow item 2: only a successful non-zero read is cached, so a
    brand-new session is retried rather than frozen for its lifetime."""
    from periscope import turns
    now = time.time()
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(_assistant_line(_iso(now - 60),
                                     {"output_tokens": 5}) + "\n")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sess")
    monkeypatch.setattr(turns, "jsonl_for_session", lambda sid: jsonl)
    monkeypatch.setattr(usage, "_base_ctx_cache", {})
    monkeypatch.setattr(usage, "_cost_in_flight", True)

    usage._refresh_cost_into_cache(["%1"])

    assert "sess" not in usage._base_ctx_cache


def test_pane_cost_pressure_does_not_double_schedule(monkeypatch):
    scheduled = []
    monkeypatch.setattr(usage, "_bg", lambda name, fn, *a: scheduled.append(name))
    monkeypatch.setattr(usage, "_cost_cache", (0.0, {}))
    monkeypatch.setattr(usage, "_cost_in_flight", True)

    usage.pane_cost_pressure(["%1"])

    assert scheduled == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_usage.py -v -k "pane_cost_pressure or refresh_cost"`
Expected: FAIL — `AttributeError: module 'periscope.usage' has no attribute '_cost_cache'`

- [ ] **Step 3: Write the implementation**

First extend the cost_pressure import at the top of `periscope/usage.py` to add `CostSample` (Task 6 deliberately left it out — it was unused there and would have failed ruff F401):

```python
from periscope.cost_pressure import CostSample, TailSummary, parse_usage_record, summarize_tail
```

Then find the anchor `_TAIL_BYTES = 4_000_000` (added in Task 6) and add directly below it:

```python
PANE_COST_REFRESH_S = 60.0
_PANE_WINDOW_S = 1800
```

Leave `PANE_BURN_REFRESH_S` and `_PANE_BURN_WINDOW_S` in place — their consumers survive until Task 9.

Find the anchor `_burn_cache: tuple[float, dict[str, float]] = (0.0, {})` and add below it:

```python
_cost_cache: tuple[float, dict[str, CostSample]] = (0.0, {})
_cost_in_flight = False
_cost_lock = threading.Lock()
# session id -> first observed context. Written and read ONLY by the single
# refresh thread that _cost_in_flight guarantees, so it needs no lock — and it
# must never be read from the request path.
_base_ctx_cache: dict[str, int] = {}
```

Then add, below `_base_ctx_from_jsonl`:

```python
def _refresh_cost_into_cache(pane_ids: list[str]) -> None:
    global _cost_cache, _cost_in_flight
    from periscope import turns
    samples: dict[str, CostSample] = {}
    seen_sids: set[str] = set()
    try:
        now = time.time()
        cutoff = now - _PANE_WINDOW_S
        for pid in pane_ids:
            sid = turns.session_id_for_pane(pid)
            jsonl = turns.jsonl_for_session(sid) if sid else None
            if not jsonl or not sid:
                continue
            tail = _tail_summary_from_jsonl(jsonl, cutoff=cutoff)
            if tail.cur_ctx is None or tail.last_ts is None:
                continue
            seen_sids.add(sid)
            # Gated on a successful tail read, which bounds the head read: a
            # transcript with no usage record in its 4MB tail is genuinely
            # usage-free, so we never re-walk a pathological file every 60s.
            base = _base_ctx_cache.get(sid)
            if base is None:
                base = _base_ctx_from_jsonl(jsonl) or 0
                if base > 0:  # only cache a real answer; a new session retries
                    _base_ctx_cache[sid] = base
            samples[pid] = CostSample(
                cur_ctx=tail.cur_ctx,
                base_ctx=base,
                pace=tail.calls / (_PANE_WINDOW_S / 60.0),
                last_ts=tail.last_ts,
            )
        for stale in set(_base_ctx_cache) - seen_sids:
            del _base_ctx_cache[stale]
    finally:
        with _cost_lock:
            _cost_cache = (time.time(), samples)
            _cost_in_flight = False


def pane_cost_pressure(pane_ids: list[str]) -> dict[str, CostSample]:
    """Stale-while-revalidate per-pane cost measurements. Serves the last
    computed samples immediately; refreshes in a background thread when older
    than PANE_COST_REFRESH_S. Returns MEASUREMENTS, not scores — banding needs a
    fresh clock and happens at annotate time."""
    global _cost_in_flight
    now = time.time()
    with _cost_lock:
        ts, samples = _cost_cache
        if now - ts >= PANE_COST_REFRESH_S and not _cost_in_flight:
            _cost_in_flight = True
            _bg("pane-cost", _refresh_cost_into_cache, list(pane_ids))
        return samples
```

- [ ] **Step 4: Reset the cost globals between tests**

`annotate_cost_pressure` is ungated, unlike the `annotate_hot_panes` it replaces (which returned early on a cold account meter, making `pane_burn_rates` effectively unreachable under pytest). So the first `/api/state` route test now flips `_cost_in_flight` to True — and because `conftest.py` stubs `usage._bg` to a no-op, the `finally` that would clear it never runs. It stays True for the rest of the session and silently suppresses every later refresh.

CLAUDE.md documents this repo's history with leaked test state; close it at the fixture layer rather than relying on each test to patch. In `tests/conftest.py`, find the `_no_plan_usage_refresh` fixture and add the cost-cache reset to it:

```python
    usage._cost_cache = (0.0, {})
    usage._cost_in_flight = False
    usage._base_ctx_cache = {}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS — the full suite, not just this file, since the fixture change touches every test.

- [ ] **Step 6: Commit**

```bash
git add periscope/usage.py tests/test_usage.py tests/conftest.py
git commit -m "feat(cost-pressure): stale-while-revalidate sample cache with a lock-free base-context memo"
```

---

## Task 8: `annotate_cost_pressure` and route wiring

**Tier: sonnet.**

**Files:**
- Modify: `periscope/usage.py`, `periscope/routes/state.py`
- Test: `tests/test_usage.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_usage.py`:

```python
def test_annotate_stamps_three_keys_on_a_pressured_pane(monkeypatch):
    now = time.time()
    monkeypatch.setattr(usage, "pane_cost_pressure", lambda ids: {
        "%1": cost_pressure.CostSample(cur_ctx=600_000, base_ctx=50_000,
                                       pace=8.0, last_ts=now),
    })
    views = [{"pane_id": "%1", "agent": "claude"}]
    usage.annotate_cost_pressure(views)
    assert views[0]["ctx_class"] == "hot"
    assert "clearing" in views[0]["ctx_hint"]
    assert views[0]["ctx_tokens"] == 600_000


def test_annotate_stamps_nothing_without_a_sample(monkeypatch):
    monkeypatch.setattr(usage, "pane_cost_pressure", lambda ids: {})
    views = [{"pane_id": "%1", "agent": "claude"}]
    usage.annotate_cost_pressure(views)
    assert "ctx_class" not in views[0]
    assert "ctx_hint" not in views[0]
    assert "ctx_tokens" not in views[0]


def test_annotate_stamps_none_band_when_there_is_data_but_no_debt(monkeypatch):
    """'server said plain' must be distinguishable from 'server said nothing'."""
    now = time.time()
    monkeypatch.setattr(usage, "pane_cost_pressure", lambda ids: {
        "%1": cost_pressure.CostSample(cur_ctx=100_000, base_ctx=50_000,
                                       pace=1.0, last_ts=now),
    })
    views = [{"pane_id": "%1", "agent": "claude"}]
    usage.annotate_cost_pressure(views)
    assert views[0]["ctx_class"] == "none"


def test_annotate_ignores_shell_panes(monkeypatch):
    monkeypatch.setattr(usage, "pane_cost_pressure",
                        lambda ids: pytest.fail("should not be asked"))
    views = [{"pane_id": "%1", "agent": None}]
    usage.annotate_cost_pressure(views)
    assert "ctx_class" not in views[0]
```

Add two imports to `tests/test_usage.py` — `from periscope import cost_pressure` and `import pytest`. The file does **not** import pytest today, and `test_annotate_ignores_shell_panes` needs `pytest.fail`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_usage.py -v -k annotate`
Expected: FAIL — `AttributeError: module 'periscope.usage' has no attribute 'annotate_cost_pressure'`

- [ ] **Step 3: Write the implementation**

Extend the `cost_pressure` import at the top of `periscope/usage.py` to include `hint` and `score`:

```python
from periscope.cost_pressure import CostSample, TailSummary, hint, parse_usage_record, score, summarize_tail
```

Keep it on ONE line. Ruff's isort splits a parenthesized member list one-per-line and would flag `I001` on a single wrapped line; `E501` is off in this repo, so the long single-line form is legal and matches Task 6's.

Add below `pane_cost_pressure`:

```python
def annotate_cost_pressure(views: list[dict]) -> None:
    """Stamp ctx_class / ctx_hint / ctx_tokens on Claude panes with cost data.

    `ctx_class` is "none" | "warn" | "hot", and the key is ABSENT when there is
    nothing to say. Never "": in JS "" and undefined are both falsy, so the
    natural `w.ctx_class || pctBand(w.context_pct)` idiom would collapse "server
    said plain" into "server said nothing" and silently fall a cost-classified
    pane back onto the auto-compact percent bands.

    Unlike the burn flag it replaces, this touches no plan-usage state — no
    OAuth call, no account gate.
    """
    ids = [v["pane_id"] for v in views
           if v.get("agent") == "claude" and v.get("pane_id")]
    if not ids:
        return
    samples = pane_cost_pressure(ids)
    now = time.time()
    for v in views:
        sample = samples.get(v.get("pane_id") or "")
        if sample is None:
            continue
        pressure = score(sample, now=now)
        if pressure is None:
            continue
        v["ctx_class"] = pressure.band
        v["ctx_hint"] = hint(pressure)
        v["ctx_tokens"] = pressure.cur_ctx
```

In `periscope/routes/state.py`, find the anchor `from periscope.usage import annotate_hot_panes, cached_claude_usage, cached_plan_usage` and change `annotate_hot_panes` to `annotate_cost_pressure`. Then find the anchor `    annotate_hot_panes(result)` and change it to `    annotate_cost_pressure(result)`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Commit**

```bash
git add periscope/usage.py periscope/routes/state.py tests/test_usage.py
git commit -m "feat(cost-pressure): annotate pane views with band, hint and context tokens"
```

---

## Task 9: Delete the dead burn path

**Tier: haiku.** Pure deletion. Every target is named. Nothing here requires a decision.

**Files:**
- Modify: `periscope/usage.py`, `tests/test_usage.py`

- [ ] **Step 1: Delete the code**

In `periscope/usage.py`, delete each of these entirely:

1. The constant line `_HOT_PANE_SHARE = 0.4` and its comment if adjacent.
2. The line `_W_INPUT, _W_CACHE_W, _W_OUT, _W_CACHE_R = 1.0, 1.25, 5.0, 0.1`.
3. The whole function `def _weighted_burn_from_jsonl(path: Path, cutoff: float) -> float:` through its final `return total`.
4. The whole function `def _refresh_burn_into_cache(pane_ids: list[str]) -> None:` through the end of its `finally` block.
5. The whole function `def pane_burn_rates(pane_ids: list[str]) -> dict[str, float]:` through its `return rates`.
6. The whole function `def annotate_hot_panes(views: list[dict]) -> None:` through its final `v["burn_wtpm"] = round(r)`.
7. The three module globals `_burn_cache`, `_burn_in_flight`, `_burn_lock`.
8. The constants `PANE_BURN_REFRESH_S`, `_PANE_BURN_WINDOW_S` and `_BURN_TAIL_BYTES`.
9. Rename the section comment `# --- Per-pane burn attribution ---` to `# --- Per-pane cost pressure ---`.

In `tests/test_usage.py`, delete **three** tests in full — `test_weighted_burn_from_jsonl_sums_recent_weighted` (it exercises a function this task deletes), `test_annotate_hot_panes_flames_majority_burner`, and `test_annotate_hot_panes_noop_when_meter_not_hot` — plus **two** now-unused module-level imports — `from periscope.usage import _weighted_burn_from_jsonl` and `from datetime import UTC` (its only consumers live inside the deleted burn test; the `_iso` helper imports `UTC` locally) — and the now-unused `_jsonl_line` helper (Task 6's tests use `_assistant_line`). Leaving `UTC` behind fails `bin/check` at Step 3 with F401.

- [ ] **Step 2: Verify nothing references the deleted names**

Run:
```bash
grep -rn "annotate_hot_panes\|pane_burn_rates\|_weighted_burn_from_jsonl\|_HOT_PANE_SHARE\|_W_CACHE_R\|_W_INPUT\|burn_hot\|burn_wtpm\|_burn_cache\|PANE_BURN_REFRESH_S" periscope/ tests/ static/src/
```
Expected: only hits in `static/src/split/RailRows.jsx` (deleted in Task 11). No hits under `periscope/` or `tests/`.

- [ ] **Step 3: Run the gate**

Run: `uv run pytest -q && bin/check`
Expected: all tests pass; zero lint/type violations.

- [ ] **Step 4: Commit**

```bash
git add periscope/usage.py tests/test_usage.py
git commit -m "refactor(usage): delete the never-firing burn flag and its now-dead weighted-token machinery"
```

---

## Task 10: Frontend pure helpers

**Tier: sonnet.** Three small exported functions plus their tests.

**Files:**
- Modify: `static/src/split/RailRows.jsx`
- Test: `static/src/split/__tests__/railRender.test.jsx`

- [ ] **Step 1: Write the failing tests**

Append to `static/src/split/__tests__/railRender.test.jsx`:

```jsx
import { ctxClass, ctxChipText } from "../RailRows.jsx";

describe("ctxClass", () => {
  it("uses the server band when present", () => {
    expect(ctxClass({ ctx_class: "hot" })).toBe(" ctx-hot");
    expect(ctxClass({ ctx_class: "warn" })).toBe(" ctx-warn");
  });

  it("treats an explicit 'none' as plain, NOT as a missing value", () => {
    // The trap: "" and undefined are both falsy, so a "" sentinel would fall
    // through to the percent bands and silently mis-colour a classified pane.
    expect(ctxClass({ ctx_class: "none", context_pct: 95 })).toBe("");
  });

  it("falls back to the percent bands when the server said nothing", () => {
    expect(ctxClass({ context_pct: 95 })).toBe(" ctx-hot");
    expect(ctxClass({ context_pct: 65 })).toBe(" ctx-warn");
    expect(ctxClass({ context_pct: 10 })).toBe("");
    expect(ctxClass({})).toBe("");
  });
});

describe("ctxChipText", () => {
  it("prefers the percentage when the status line parsed", () => {
    expect(ctxChipText({ context_pct: 72, ctx_tokens: 600000 })).toBe("72%");
  });

  it("falls back to tokens when the status line did not parse", () => {
    expect(ctxChipText({ context_pct: null, ctx_tokens: 600000 })).toBe("600k");
  });

  it("returns null when there is nothing to show", () => {
    expect(ctxChipText({})).toBe(null);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm test -- railRender`
Expected: FAIL — `ctxClass is not exported` / import error.

- [ ] **Step 3: Write the implementation**

In `static/src/split/RailRows.jsx`, find the anchor:

```js
function ctxClass(p) {
  if (p == null) return "";
  if (p >= 80) return " ctx-hot";
  if (p >= 60) return " ctx-warn";
  return "";
}
```

Replace it entirely with:

```js
// Today's context-window ramp, kept verbatim for panes the server could not
// classify: it warns about auto-compact proximity, which is a different thing
// from cost pressure and still worth showing.
function pctBand(p) {
  if (p == null) return "";
  if (p >= 80) return " ctx-hot";
  if (p >= 60) return " ctx-warn";
  return "";
}

// The server sends "none" | "warn" | "hot", and omits the key entirely when it
// has nothing to say. It must never send "" — "" and undefined are both falsy,
// so the two states would collapse here and a cost-classified pane would fall
// back to the percent bands.
export function ctxClass(w) {
  if (w.ctx_class) return w.ctx_class === "none" ? "" : ` ctx-${w.ctx_class}`;
  return pctBand(w.context_pct);
}

function fmtCtxTokens(n) {
  return `${Math.round(n / 1000)}k`;
}

// The chip shows the percentage when Claude's status line parsed, and the raw
// context otherwise — so a pane whose status line is unreadable still paints.
export function ctxChipText(w) {
  if (w.context_pct != null) return `${w.context_pct}%`;
  if (w.ctx_tokens != null) return fmtCtxTokens(w.ctx_tokens);
  return null;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test -- railRender`
Expected: PASS

**Expected intermediate state — do not stop to debug it.** Between this task and Task 11 the render sites still call `ctxClass(w.context_pct)` against the new `ctxClass(w)` signature, so a number is passed where an object is expected and every chip loses its colour. Nothing user-visible ships, because the bundle is not rebuilt until Task 12. Task 11 fixes the call sites.

- [ ] **Step 5: Commit**

```bash
git add static/src/split/RailRows.jsx static/src/split/__tests__/railRender.test.jsx
git commit -m "feat(rail): ctxClass reads the server band with a none sentinel, chip text falls back to tokens"
```

---

## Task 11: Render sites and CSS

**Tier: haiku.** Three mechanical edits in JSX and two in CSS, all anchored on quoted text.

**Files:**
- Modify: `static/src/split/RailRows.jsx`, `static/styles.css`

- [ ] **Step 1: Update the compact render**

Find the anchor:

```jsx
          : (w.context_pct != null && (
              <span class={`pane-mini-ctx${ctxClass(w.context_pct)}`}>{w.context_pct}%</span>
            ))}
```

Replace with:

```jsx
          : (ctxChipText(w) != null && (
              <span
                class={`pane-mini-ctx${ctxClass(w)}`}
                title={w.ctx_hint || "context window used"}
              >{ctxChipText(w)}</span>
            ))}
```

- [ ] **Step 2: Delete the flame block**

Find and delete this entire block:

```jsx
        {w.burn_hot && (
          <span
            class="rail-burn"
            title={`eating the session quota — ~${w.burn_wtpm || "?"} weighted tok/min over the last 30m`}
          >🔥</span>
        )}
```

- [ ] **Step 3: Update the expanded render and its gate**

Find the anchor `{expanded && (w.pr || w.linked_linear || isDirty(w.git) || w.context_pct != null) && (` and replace `w.context_pct != null` with `ctxChipText(w) != null`.

Then find the anchor:

```jsx
          {w.context_pct != null && (
            <span class={`pane-pill pane-pill-ctx${ctxClass(w.context_pct)}`} title="context window used">{w.context_pct}%</span>
          )}
```

Replace with:

```jsx
          {ctxChipText(w) != null && (
            <span
              class={`pane-pill pane-pill-ctx${ctxClass(w)}`}
              title={w.ctx_hint || "context window used"}
            >{ctxChipText(w)}</span>
          )}
```

- [ ] **Step 4: Update CSS**

In `static/styles.css`, find the anchor `.rail-burn { flex: 0 0 auto; font-size: 10px; cursor: help; }` and delete that line together with any comment block immediately above it that describes the burn chip.

Then find the anchor `.pane-mini-ctx { font-size: 10px; color: var(--fg-4); }` and replace it with:

```css
.pane-mini-ctx { font-size: 10px; color: var(--fg-4); cursor: help; }
```

- [ ] **Step 5: Add the render-path assertion**

Task 10 covered the pure helpers; this catches a mis-typed gate or a dropped `title=`, which is the wiring-error class `railRender.test.jsx` exists to catch. Append to `static/src/split/__tests__/railRender.test.jsx`, following the file's existing `<Rail>` render pattern:

```jsx
it("paints the context chip from tokens when the status line did not parse", () => {
  windows.value = [{
    pid: "p1", pane_id: "%1", agent: "claude", name: "worker",
    track_id: "t1", context_pct: null,
    ctx_tokens: 600000, ctx_class: "hot", ctx_hint: "clearing pays for itself",
  }];
  const html = render(<Rail />);
  expect(html).toContain("600k");
  expect(html).toContain("ctx-hot");
  expect(html).toContain("clearing pays for itself");
});
```

Match the window-object shape the existing cases in this file use — copy their fields rather than inventing them; the assertions above are what matters.

- [ ] **Step 6: Verify and run the gate**

Run:
```bash
grep -rn "burn_hot\|burn_wtpm\|rail-burn" static/src/ static/styles.css
```
Expected: no output.

Run: `npm test && npm run lint`
Expected: all tests pass, zero lint violations.

- [ ] **Step 7: Commit**

```bash
git add static/src/split/RailRows.jsx static/styles.css static/src/split/__tests__/railRender.test.jsx
git commit -m "feat(rail): context chip carries the cost hint and paints from tokens when the status line is unreadable"
```

---

## Task 12: Rebuild and commit the bundle

**Tier: haiku.** `static/dist/app.js` is the one committed build artifact; CLAUDE.md requires rebuilding it whenever `static/src/` changes.

**Files:**
- Modify: `static/dist/app.js`

- [ ] **Step 1: Build**

Run: `npm run build`
Expected: Vite writes `static/dist/app.js` with no errors.

- [ ] **Step 2: Confirm the bundle changed**

Run: `git status --short static/dist/`
Expected: ` M static/dist/app.js`

- [ ] **Step 3: Commit**

```bash
git add static/dist/app.js
git commit -m "build: rebuild bundle for the cost-pressure context chip"
```

---

## Task 13: Calibration

**Tier: sonnet.** Produces the deliverable D6 calls for: the measured distribution, not just a threshold check.

**Files:**
- Create: a throwaway script (not committed — there is no home for one-shot scripts in this tree)
- Modify: `docs/superpowers/specs/2026-08-19-pane-cost-pressure-design.md` (constants table)

- [ ] **Step 1: Write the calibration script**

Write to `/tmp/calibrate_cost_pressure.py`:

```python
"""Replay the shipped constants over the real corpus and report the distribution.

Run: uv run python /tmp/calibrate_cost_pressure.py
"""
import glob
import json
import os
import statistics
import time

from periscope import cost_pressure as cp

PROJECTS = os.path.expanduser("~/.claude/projects")
now = time.time()
rows = []

for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
    try:
        if os.path.getmtime(path) < now - 7 * 86400:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            records = []
            for line in f:
                try:
                    parsed = cp.parse_usage_record(json.loads(line))
                except ValueError:
                    continue
                if parsed is not None:
                    records.append(parsed)
    except OSError:
        continue
    if not records:
        continue
    base = records[0].ctx_tokens
    tail = cp.summarize_tail(records, cutoff=now - 1800)
    if tail.cur_ctx is None or tail.last_ts is None:
        continue
    sample = cp.CostSample(cur_ctx=tail.cur_ctx, base_ctx=base,
                           pace=tail.calls / 30.0, last_ts=tail.last_ts)
    pressure = cp.score(sample, now=now)
    rows.append((pressure.band if pressure else "no-annotation",
                 pressure.payback_calls if pressure else None,
                 tail.cur_ctx))

bands = {}
for band, _, _ in rows:
    bands[band] = bands.get(band, 0) + 1
print(f"sessions active in the last 7 days: {len(rows)}")
for band, n in sorted(bands.items(), key=lambda kv: -kv[1]):
    print(f"  {band:>14s}  {n:4d}  {n / len(rows) * 100:5.1f}%")

paybacks = sorted(p for _, p, _ in rows if p is not None)
ctxs = sorted(c for _, _, c in rows)
if len(paybacks) >= 2:  # quantiles() raises on a single data point
    q = statistics.quantiles(paybacks, n=4)
    print(f"payback_calls  p25={q[0]:.1f}  p50={q[1]:.1f}  p75={q[2]:.1f}")
if len(ctxs) >= 2:
    q = statistics.quantiles(ctxs, n=10)
    print(f"cur_ctx        p50={q[4]/1000:.0f}k  p90={q[8]/1000:.0f}k")
```

- [ ] **Step 2: Run it**

Run: `uv run python /tmp/calibrate_cost_pressure.py`
Expected output shape:

```
sessions active in the last 7 days: 53
            warn    24   45.3%
   no-annotation    18   34.0%
            none     8   15.1%
             hot     3    5.7%
payback_calls  p25=2.3  p50=3.8  p75=9.4
cur_ctx        p50=300k  p90=723k
```

- [ ] **Step 3: Judge the result**

The target from D6: **warn near half of scored sessions, hot rare** (a handful, not a majority). If `hot` exceeds ~15% of scored sessions, lower `_PAYBACK_MINS_HOT` to `1` in `periscope/cost_pressure.py` and re-run. If `warn` is under 25%, raise `_PAYBACK_CALLS_WARN` toward the measured p50 and re-run.

- [ ] **Step 4: Record the numbers**

Paste the script's output into the spec's D6 section under the constants table, replacing "The proposed constants are a starting point, not a result" with the measured result and the date.

- [ ] **Step 5: Commit**

```bash
git add periscope/cost_pressure.py
git commit -m "tune(cost-pressure): calibrate thresholds against the measured seven-day distribution"
```

(If Step 3 required no change, skip the commit — the spec is uncommitted by preference.)

---

## Task 14: Documentation

**Tier: haiku.**

**Files:**
- Modify: `CLAUDE.md`, `docs/mockups/rail-cards.html`

- [ ] **Step 1: Add the module row**

In `CLAUDE.md`, find the anchor row:

```
| `periscope/usage.py` | Claude plan usage (JSONL parse + OAuth usage-endpoint fetch) |
```

Insert immediately below it:

```
| `periscope/cost_pressure.py` | Pure decision core for per-pane context-cost pressure (record selection, payback math, banding, tooltip copy) |
```

- [ ] **Step 2: Update the mockup page**

In `docs/mockups/rail-cards.html`, line ~434 is a `<p class="legend">` listing the available atoms, with `burn_hot` 🔥 among them — it is a legend entry, not a section. Find the literal text `burn_hot` in that legend and replace that one atom's entry with:

```
ctx_class (context chip: plain | warn amber — clearing pays back soon, parked panes get the handoff hint | hot red — active and paying back within minutes)
```

Leave every other atom in the legend untouched.

- [ ] **Step 3: Verify the whole gate one last time**

Run: `uv run pytest -q && npm test && bin/check`
Expected: all green, zero violations.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/mockups/rail-cards.html
git commit -m "docs: cost_pressure module row and the context chip's three states"
```

---

## Done when

- `uv run pytest -q` green, `npm test` green, `bin/check` at zero violations.
- `grep -rn "burn_hot\|burn_wtpm\|rail-burn\|annotate_hot_panes" periscope/ tests/ static/src/ static/styles.css` returns nothing.
- `static/dist/app.js` rebuilt and committed.
- The calibration output is recorded in the spec's D6 section.
- Dev server on :8766 shows the chip: a long-running pane paints amber or red with a hint naming a remedy, and a fresh pane paints plain.
