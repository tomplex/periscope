# Pane cost pressure — code structure

**Status:** proposal, for review before the implementation plan is written
**Spec:** `docs/superpowers/specs/2026-08-19-pane-cost-pressure-design.md`
**Date:** 2026-08-19

Structural blueprint only: what shape the code takes, which unit owns which
decision, and which tier of model can implement each unit. No sequencing — that
is the plan's job.

Tier labels used throughout:

- **mechanical** — delete / rename / move / transcribe a table. Cheapest model.
- **bounded** — implement against a stated signature with a fully enumerable
  case list. Mid-tier model.
- **judgment** — the unit where ambiguity actually lives. Highest tier.

The whole point of the split below is that there are exactly **two** judgment
units in the entire feature, and both are under 25 lines.

---

## Spec pushback

**P1. `ctx_class: ""` for the plain-with-cost-data band is a falsy-collision
trap. Send `"none"` instead.**

The spec (§Data flow 4) stamps `ctx_class` as `"" / "warn" / "hot"` and omits
the key when there is no cost data. In JS, `""` and `undefined` are both falsy,
so the two states the spec explicitly needs to keep distinguishable —
*"server said plain"* vs *"server said nothing"* — collapse under the natural
idiom `w.ctx_class || pctBand(w.context_pct)`. A pane the server has classified
as plain would then silently fall back to the auto-compact percent bands, which
is the exact bug the fallback design is trying to avoid, and it would be
invisible in review.

Proposal: the server stamps `ctx_class` as `"none" | "warn" | "hot"` whenever it
has cost data, and omits the key entirely otherwise. Every truthiness idiom then
gives the right answer, and the frontend's hardest decision becomes mechanical.
CSS class names are unchanged (`ctx-warn` / `ctx-hot`); `"none"` maps to `""`.

**P2. The `_W_*` burn weights are fully dead after this change — delete all
four, not just the flame.**

§Failure modes says "`_W_CACHE_W` stays 1.25 for the burn weighting it already
feeds". Verified: nothing feeds. `_W_INPUT/_W_CACHE_W/_W_OUT/_W_CACHE_R`
(`usage.py:466`) have exactly one consumer, `_weighted_burn_from_jsonl`, whose
only consumer is `_refresh_burn_into_cache` → `pane_burn_rates` →
`annotate_hot_panes`, all of which the spec deletes. Keeping a dead weighted sum
inside the reworked reducer is a ratchet. The payback math gets its own two
constants (`_CACHE_READ_MULT = 0.1`, `_CACHE_WRITE_MULT = 2.0`) in the new
module, which is also the spec's own point that 1.25 is the *wrong* number here.

**P3. Score at annotate time, not at refresh time.**

§Data flow 3 says `pane_cost_pressure()` returns "a small dict per pane",
unspecified as to whether it holds the score or the measurements. It must hold
the **measurements** (`cur_ctx`, `base_ctx`, `pace`, `last_ts`), with `score()`
applied per request against a fresh `now`. `active = now - last_ts <=
_ACTIVE_WITHIN_S` is a clock comparison, and the cache is stale-while-revalidate
— a score computed in the refresh thread would freeze `active=True` (and
therefore the `hot` band and the `/clear` advice) for however long the cache
goes unrefreshed. Caching a clock-free measurement also makes the cache trivially
testable.

**P4. Line-number drift in the spec.** The plan writer should re-locate rather
than trust these: `usage.py` is 610 lines, not 599; `.rail-burn` is
`styles.css:252-254` (with its two-line comment), not 222; `.pane-mini-ctx` is
`styles.css:2577`, not 2545; the expanded ctx chip is `RailRows.jsx:283`, not
282. Everything else in the spec's caller inventory verified exact.

---

## Assumptions

- **A1.** `ctx_tokens` is stamped on **every** annotated pane, not only those
  with an unparseable `context_pct`. Conditioning on `context_pct` would couple
  `annotate_cost_pressure` to `build_window_view`'s output for no gain; the
  client already decides what to display.
- **A2.** The head read for `base_ctx` runs **only when the tail read produced a
  `cur_ctx`**. This is the bound on the head read: the spec verified that every
  transcript with no usage record in its 4MB tail was under 200KB and genuinely
  usage-free, so the gate costs nothing and removes the need for a byte cap on a
  forward scan that would otherwise re-walk a pathological file every 60s.
- **A3.** The `base_ctx` cache is pruned each refresh to the session ids seen in
  that pass. Re-reading a returning pane's head costs a median 86KB; an
  unbounded per-session dict in a process that runs for weeks does not need to
  exist.
- **A4.** "Output goes in the PR" (§Testing, calibration) reads as: this repo
  commits straight to `main`, so the calibration table goes in the commit message
  and into the spec's constants table. The script itself stays uncommitted in the
  session scratchpad — there is no home for one-shot scripts in this tree
  (`diag/` holds one shell tool), and `bin/check` lints everything committed.

---

## File layout

```
periscope/cost_pressure.py     NEW — pure decision core: record selection,
                                     tail reduction, payback math, banding,
                                     tooltip copy. stdlib imports only.
periscope/usage.py             CHANGED — I/O shells (tail + head reads), the
                                     stale-while-revalidate cache, the _bg
                                     refresh, annotate_cost_pressure.
                                     Deletes annotate_hot_panes, _HOT_PANE_SHARE,
                                     _weighted_burn_from_jsonl, the four _W_*.
periscope/routes/state.py      CHANGED — one import, one call (lines 28, 82).
tests/test_cost_pressure.py    NEW — pure unit tests, zero fixtures.
tests/test_usage.py            CHANGED — reader tests on real tmp_path JSONL;
                                     replaces the two annotate_hot_panes tests.
static/src/split/RailRows.jsx  CHANGED — ctxClass(w), chip text, title attrs,
                                     render gate; deletes the rail-burn block.
static/styles.css              CHANGED — cursor:help on .pane-mini-ctx;
                                     deletes .rail-burn + its comment.
static/dist/app.js             REBUILT + committed (npm run build).
docs/mockups/rail-cards.html   CHANGED — line 434 documents the 🔥 chip.
CLAUDE.md                      CHANGED — one row in the module table for
                                     periscope/cost_pressure.py.
```

---

## Per-module structure

### `periscope/cost_pressure.py` — rung 2 (frozen data + pure functions)

New module, no state, no I/O, no threads, stdlib only. This is where every
enumerable case list lives, and it is the file a mid-tier model can implement
without reading `usage.py` at all.

Precedent: `narrator.py` is exactly this shape ("the decision core below is pure
(no DB / tmux / API) so the whole regeneration policy is unit-testable with zero
fixtures; tick() is the only place IO happens"), including `Regen =
Literal[...]` for a closed variant set and frozen dataclass results.

```python
Band = Literal["none", "warn", "hot"]

_BASE_CAP = 80_000
_PAYBACK_CALLS_WARN = 4
_PAYBACK_MINS_HOT = 2
_ACTIVE_WITHIN_S = 300
_CACHE_READ_MULT = 0.1     # a re-read of the whole context, per call
_CACHE_WRITE_MULT = 2.0    # re-writing base_ctx on the 1h TTL a sub gets

@dataclass(frozen=True)
class UsageRecord:
    ts: float
    ctx_tokens: int        # input + cache_creation + cache_read

@dataclass(frozen=True)
class TailSummary:
    cur_ctx: int | None    # last record's context, cutoff-IGNORING
    last_ts: float | None
    calls: int             # records at/after cutoff — API calls, not prompts

@dataclass(frozen=True)
class CostSample:          # clock-free measurement; what the cache holds
    cur_ctx: int
    base_ctx: int
    pace: float            # calls per minute over the burn window
    last_ts: float

@dataclass(frozen=True)
class Pressure:
    band: Band
    active: bool
    cur_ctx: int
    payback_calls: float
    payback_mins: float | None   # None when pace == 0

def parse_usage_record(rec: dict) -> UsageRecord | None: ...
def summarize_tail(records: Iterable[UsageRecord], *, cutoff: float) -> TailSummary: ...
def score(sample: CostSample, *, now: float) -> Pressure | None: ...
def hint(p: Pressure) -> str: ...
```

| Unit | Tier | Notes |
|---|---|---|
| constants block | mechanical | transcribe the spec's table + its rationale comments |
| the four dataclasses | mechanical | |
| `parse_usage_record` | **judgment** | the only ambiguous unit on the server |
| `summarize_tail` | bounded | |
| `score` | bounded | |
| `hint` | bounded | |

**`parse_usage_record` is the isolated judgment unit.** It answers "is this
record real?" once, and both the head read and the tail read use that one answer
— which is what makes the spec's "skip `<synthetic>` at both ends" a structural
guarantee rather than a discipline. Returns `None` for: no `timestamp` / not a
string / unparseable; no `message.usage`; `message.model == "<synthetic>"`; all
four token fields summing to zero. ~20 lines. Give it the spec's §Record
selection paragraph verbatim as its docstring — the transcript that ends on a
zero record after 503,613 tokens is the case that motivates it.

**`score` returns `None` for "no annotation"** — `base_ctx <= 0`, `debt <= 0`.
That collapses the spec's two guard paths into one absent-value contract the
caller already has to handle (no session, no usage record). `payback_calls =
_CACHE_WRITE_MULT * base_ctx / (_CACHE_READ_MULT * debt)` with
`base_ctx = min(observed, _BASE_CAP)` applied here, not in the reader — the
reader stays honest about what it saw and the clamp is testable without a file.
`pace == 0` → `payback_mins = None` → warn at most, never hot.

**`hint` is separate from `score` on purpose.** Copy churns; banding does not.
Keeping them apart means editing a sentence never touches an arithmetic test.
Four sentences, selected by `(band, active)` — the fifth case in the spec's
tooltip table ("plain, no cost data") is a frontend default, not a Python
string, because the server sends nothing at all in that case.

### `periscope/usage.py` — rung 1 (functions), unchanged style

Everything here is I/O, cache, or threading. The `# --- Per-pane burn
attribution ---` block (lines 452-568) is reworked in place into
`# --- Per-pane cost pressure ---`.

```python
PANE_COST_REFRESH_S = 60.0          # was PANE_BURN_REFRESH_S
_PANE_WINDOW_S = 1800               # was _PANE_BURN_WINDOW_S
_TAIL_BYTES = 4_000_000             # was _BURN_TAIL_BYTES

_cost_cache: tuple[float, dict[str, CostSample]] = (0.0, {})
_cost_in_flight = False
_cost_lock = threading.Lock()
_base_ctx_cache: dict[str, int] = {}   # session id -> observed first ctx

def _tail_summary_from_jsonl(path: Path, *, cutoff: float) -> TailSummary: ...
def _base_ctx_from_jsonl(path: Path) -> int | None: ...
def _refresh_cost_into_cache(pane_ids: list[str]) -> None: ...
def pane_cost_pressure(pane_ids: list[str]) -> dict[str, CostSample]: ...
def annotate_cost_pressure(views: list[dict]) -> None: ...
```

| Unit | Tier | Notes |
|---|---|---|
| delete `annotate_hot_panes`, `_HOT_PANE_SHARE`, `_weighted_burn_from_jsonl`, the four `_W_*` | mechanical | |
| `_tail_summary_from_jsonl` | bounded | same seek/readline/read as today; `json.loads` → `parse_usage_record` → `summarize_tail`. All the branching moved out |
| `_base_ctx_from_jsonl` | bounded | iterate the open handle line-by-line, return the first `parse_usage_record(...).ctx_tokens`, stop |
| `_refresh_cost_into_cache` | bounded | mirrors `_refresh_burn_into_cache` exactly; assembles `CostSample` per pane; owns the `_base_ctx_cache` read/write/prune |
| `pane_cost_pressure` | mechanical | rename + value type of `pane_burn_rates`; the `_bg` call is unchanged |
| `annotate_cost_pressure` | bounded | filter `agent == "claude"`, `score(..., now=time.time())`, stamp three keys or none |

**Threading constraint is preserved for free.** `_bg` is already imported into
`usage.py`'s namespace (`usage.py:30`) and `pane_cost_pressure` calls it as a
module global, exactly as `pane_burn_rates` does today — `tests/conftest.py:34-48`
neuters `usage._bg` by name and keeps working. Nothing in `cost_pressure.py`
imports `_bg` or `threading`, which is what makes that invariant structural
rather than remembered.

**`_base_ctx_cache` needs no lock.** `_cost_in_flight` guarantees at most one
refresh thread, and it is the only reader and writer. Say so in a comment; the
next person will otherwise add a lock or, worse, read it from the request path.

**`annotate_cost_pressure` drops a dependency the old function had.**
`annotate_hot_panes` called `cached_plan_usage()` (the OAuth path) to gate on the
session meter. The new one does not touch plan usage at all — one less cross-
concern coupling in `usage.py`, and one less monkeypatch per test.

### `periscope/routes/state.py` — mechanical

Line 28: `annotate_hot_panes` → `annotate_cost_pressure` in the existing
`from periscope.usage import ...`. Line 82: the call. `cached_plan_usage` stays
(line 166 still uses it). Nothing else.

### `static/src/split/RailRows.jsx` — pure helpers + two render sites

```js
export function ctxClass(w) {          // was ctxClass(p), 2 call sites
  if (w.ctx_class) return w.ctx_class === "none" ? "" : ` ctx-${w.ctx_class}`;
  return pctBand(w.context_pct);       // today's 80/60 ramp, extracted verbatim
}
export function ctxChipText(w) { ... } // "72%" | "600k" | null
function fmtTokens(n) { ... }          // 600_000 -> "600k"
```

| Unit | Tier | Notes |
|---|---|---|
| `pctBand` extraction from today's `ctxClass` body | mechanical | verbatim move |
| `ctxClass(w)` | bounded (**judgment, if P1 is rejected**) | with the `"none"` sentinel it is a three-line lookup; with `""` it is the trap described above |
| `ctxChipText` + `fmtTokens` | bounded | |
| render gate `w.context_pct != null` → `ctxChipText(w) != null`, both sites | mechanical | lines 209 and 276/282 |
| `title={w.ctx_hint \|\| "context window used"}` on both renders | mechanical | the compact render has no `title` today — this is the one new attribute |
| delete the `rail-burn` block (lines 212-217) | mechanical | |

`fmtTokens` lives in `RailRows.jsx`, not `util.js` — one consumer today. The
Python `hint()` also renders "600k"; that two-line duplication is accepted rather
than shipping a pre-formatted display string for one chip while every other chip
formats client-side.

### `static/styles.css` — mechanical

Add `cursor: help` to `.pane-mini-ctx` (line 2577). Delete `.rail-burn` and its
two-line comment (252-254). The `ctx-warn` / `ctx-hot` rules at 2578-2579 and
2640-2641 are unchanged — that is the whole point of D3.

---

## Patterns

**Used:**

- *Pure decision core + I/O shell* — `cost_pressure.py` vs `usage.py`. Directly
  modeled on `narrator.py`, which CLAUDE.md already documents as this repo's
  shape for "policy that must be testable with zero fixtures".
- *Frozen dataclass value objects* — `UsageRecord` / `TailSummary` /
  `CostSample` / `Pressure`. Matches `window_view.py:74`, `open_ops.py:22-39`,
  `narrator.py:74`.
- *`Literal` for a closed variant set* — `Band`, matching `narrator.py`'s `Regen`.
- *Keyword-only args* on every multi-argument function (`*, cutoff`, `*, now`).
- *Stale-while-revalidate module cache + `_bg`* — unchanged from
  `pane_burn_rates`; this is the repo's established shape (`cached_plan_usage`,
  `cached_git_state`, `pane_burn_rates` all do it).
- *Absent-key contract* for "nothing to say" — matches how `status_rail` and
  `burn_hot` already behave on the pane view, and how `routes/state.py:93`
  reads them.

**Considered and rejected:**

- *A `CostPressure` class owning the cache + the math* — no coupled mutable
  state; the cache is one module-level tuple already guarded by a lock.
- *A `Reader` / strategy abstraction over head-read vs tail-read* — two
  functions, one shared record parser, no third reader foreseeable.
- *Moving the OAuth plan-usage block out of `usage.py`* — see close call C2.
- *Custom exceptions for `base_ctx <= 0` / `debt <= 0`* — these are ordinary
  "no annotation" outcomes, expressed as `score() -> Pressure | None`. Nothing
  catches a type here.
- *Sending the five raw numbers to the client and building the sentence in JSX*
  — five new per-pane fields on a 3s poll, and the banding logic would have to
  exist twice to stay consistent with the copy.
- *A settings/prefs surface for the thresholds* — D7 is explicit; module
  constants only.

---

## Test strategy

| Module | Approach | Dependencies |
|---|---|---|
| `cost_pressure.py` | `tests/test_cost_pressure.py` — pure unit, **zero fixtures, no tmp files, no monkeypatch**. Every case in the spec's §Testing list that is not about I/O lands here: banding at each threshold, `debt <= 0`, `base_ctx <= 0`, the `_BASE_CAP` clamp, `pace == 0` → warn-not-hot, `active` at the `_ACTIVE_WITHIN_S` boundary, `cur_ctx` surviving a cutoff older than the window, each of the four `hint` sentences. Inputs are hand-built `UsageRecord` / `CostSample` literals | none |
| `usage.py` readers | `tests/test_usage.py` — **real files on `tmp_path`**, extending the existing `_jsonl_line` helper at line 237. No mocked filesystem, ever: a mocked reader that passes while a real transcript's record shape has drifted is precisely the failure this signal exists to survive. Cases: `<synthetic>` at the head, `<synthetic>` at the tail, an all-zero record, a transcript entirely older than the cutoff (parked: `cur_ctx` present, `calls == 0`), a transcript with no usage record (both readers return nothing) | real filesystem |
| `usage.annotate_cost_pressure` | `tests/test_usage.py` — monkeypatch `usage.pane_cost_pressure` to return literal `CostSample`s, exactly the existing pattern at `test_usage.py:265`. This replaces the two deleted `annotate_hot_panes` tests. Assert the three stamped keys, and assert **no keys** on a pane with no sample | one monkeypatch |
| `RailRows.jsx` | `static/src/split/__tests__/railRender.test.jsx` — direct unit assertions on the exported `ctxClass` / `ctxChipText` (all three bands, `"none"` → plain, absent key → percent fallback, absent key + null `context_pct` → no chip), plus render-path assertions for the widened gate (`context_pct: null, ctx_tokens: 600000` paints) and the `title` on the compact render | none |
| calibration (D6) | one-shot script, uncommitted, output pasted into the commit message and back into the spec's constants table. Not a test | real `~/.claude/projects` |
| gate | `bin/check` at zero violations; `uv run pytest -q`; `npm test`; `npm run build` + commit `static/dist/app.js` | — |

**Testability flags:**

- No unit in this proposal requires constructing a pane view, a tmux pane, a DB
  row, or a thread to reach its logic. That is the reason for the module split,
  not a side effect of it.
- `_refresh_cost_into_cache(pane_ids)` is a plain module function, so the whole
  background path is exercisable by calling it directly — which is required,
  since `conftest.py` neuters `_bg` and the thread never runs under pytest.
- The one place a mock could hide a real failure is the record shape itself, and
  the reader tests deliberately use real files to close it. §Record selection's
  findings are version-dependent (Claude Code 2.1.236); the reader tests are the
  regression net when that changes.

---

## Decisions to sanity-check

**C1. New module `periscope/cost_pressure.py` rather than a section of
`usage.py`.** Alternative: a `# --- pane cost pressure ---` block inside
`usage.py`, which is where the spec implies it goes ("`annotate_hot_panes` sets
the precedent for cost annotation living in `usage.py`"). Close because that
precedent is real and the annotate entry point does stay in `usage.py` either
way. Decided for the new module: the pure core has zero imports and a
zero-fixture test file, which is what makes it implementable by a cheap model
from a signature list alone — a mid-tier model implementing `score()` inside
`usage.py` has 610 lines of unrelated OAuth and keychain code in front of it.
CLAUDE.md's "one file per subsystem" table absorbs a new row cleanly.

**C2. `usage.py` keeps all three of its existing concerns.** Alternative:
extract the OAuth plan-usage path (lines 120-449) while we are here. Close
because the file genuinely is three concerns in one bucket and Tom's file rule
would normally split it. Decided against: nothing in this feature touches the
OAuth path, the spec's brief is scoped, and a 300-line move would bury the
actual change in review. After the deletions this feature makes, `usage.py`
lands around 560 lines — smaller than it is today. Worth doing later, on its own
commit.

**C3. `score()` and `hint()` as two functions rather than `hint` as a field on
`Pressure`.** Close — one call site consumes both, so a single
`score() -> Pressure` carrying `.hint` would be one fewer unit. Decided for two:
the tooltip copy is the thing most likely to be edited after ship, and it should
not be able to break a banding assertion when it is.

**C4. Scoring `active` against the request clock (P3) means the band can change
between two polls with no refresh in between** — a pane crosses
`_ACTIVE_WITHIN_S` and drops `hot` → `warn`, switching the advice from `/clear`
to "write a handoff". That is the correct behavior per D2, but it is a
user-visible transition that no cache invalidation triggers. Calling it out in
case the calibration step makes it look flickery.

---

## Approval

**Approved 2026-08-19.** C1 and C2 taken as proposed; C3 and C4 accepted as
written.

**C1 — new `periscope/cost_pressure.py`.** Taken. "Cost pressure" names a real
concept with one clear home, which is the bar for a structural boundary here;
this is not indirection with no referent. The pure core's zero imports and
zero-fixture test file are what make it implementable from a signature list
alone, and CLAUDE.md's one-file-per-subsystem table absorbs the row cleanly.

**C2 — the OAuth plan-usage concern stays in `usage.py`.** Taken. Nothing in
this feature touches it, and a 300-line move would bury the actual change in
review. Worth doing later on its own commit; not this one.

**P1, P2, P3 accepted** and already folded back into the design spec.
