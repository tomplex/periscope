# Backend Poll Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the `/api/state` fork storm so the dashboard poll stops contending with the focused pane's keystroke path — by skipping `capture-pane` on quiet panes and parallelizing the rest.

**Architecture:** Two independent, layered changes to the existing 3s poll (no new transport): (1) cache the parsed-pane result per target and skip `capture()`+`parse_pane()` for panes whose tmux `window_activity` is unchanged AND whose cached state is quiet (`idle`/`shell`); (2) run the per-window `build_window_view` fan-out through a `ThreadPoolExecutor` while keeping the serial git-warm/pid-resolution pre-phase and the single-lock stamp-write post-phase. Everything downstream of capture (focus stamps, done-edge, git/PR/LGTM/channel) keeps running every poll, so no recency or state-transition signal goes stale.

**Tech Stack:** Python 3.14, FastAPI, `concurrent.futures.ThreadPoolExecutor`, `pytest` + `pytest-mock` (run with `uv run pytest -q`).

**Source spec:** `docs/superpowers/specs/2026-06-01-frontend-architecture-design.md` (§Architecture — Backend).

**Invariant guardrails (from the behavior-inventory synthesis — do not violate):**
- `#{window_activity}` is a *skip-recapture hint ONLY*. It must never reach `_focused_at` / `update_focus_from_windows` / `note_focus`. Focus stays driven by `#{window_active}` (CLAUDE.md invariant #1 — the bug that invariant exists to prevent is focus bumping on any pane output).
- Only skip recapture for panes whose **cached state is `idle` or `shell`**. For `working`/`needs-input`/`error`, always recapture — the spinner 4s grace (`smooth_spinner`), the 120s `smooth_is_claude` stickiness, and the busy→idle "done" edge (`record_state_transition`) are non-idempotent and must keep running on a fresh capture.
- `record_state_transition`, the recency stamps, and the git/PR/LGTM/channel assembly run **every poll for every pane** (cache only short-circuits capture+parse+smooth), so `_prev_state[pid]` never stales and `focused_at`/`acted_at` stay fresh even on a skipped pane.
- The serial **git-warm + pid-resolution** pre-phase (`_attach_git_then_resolve_pids`, which writes `state.json` + tmux options) stays serial *before* the pool. All `_STATE` mutation (`set_window_fields_bulk`: one lock, one write) stays single-threaded *after* the join.
- Per-pane capture-exception isolation (`build_window_view`'s internal `try/except` → `{state:"error"}`) must survive the fan-out: one pane's capture timeout must not sink the whole `/api/state` response.
- Resume-GC `live_targets` is built from `list_windows` output, NOT from successfully-captured panes (unchanged — keep it that way so a skipped/failed pane isn't wrongly GC'd from `_resuming`).

---

### Task 1: Add `#{window_activity}` to `list_windows` (skip-predicate hint only)

**Files:**
- Modify: `periscope/panes.py:253-289` (`list_windows`)
- Test: `tests/test_panes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_panes.py`:

```python
def test_list_windows_parses_window_activity(mocker):
    """list_windows surfaces #{window_activity} as an int `activity` field."""
    from periscope import panes
    mocker.patch(
        "periscope.panes.tmux",
        return_value="sess\t0\twin\t1\t/cwd\tpid-abc\t%5\t1717250000\n",
    )
    rows = panes.list_windows()
    assert rows[0]["activity"] == 1717250000


def test_list_windows_activity_defaults_zero_when_absent(mocker):
    """A short row (no activity column) defaults activity to 0, not a crash."""
    from periscope import panes
    mocker.patch(
        "periscope.panes.tmux",
        return_value="sess\t0\twin\t1\t/cwd\tpid-abc\t%5\n",
    )
    rows = panes.list_windows()
    assert rows[0]["activity"] == 0


def test_update_focus_ignores_window_activity(mocker):
    """Invariant #1 regression: focus is keyed on window_active, never activity.
    Two polls where only `activity` changes (same active window) must NOT
    re-stamp focus."""
    from periscope import panes
    panes._focused_at.clear()
    panes._active_per_session.clear()
    windows_t1 = [{"session": "s", "index": 0, "active": True, "activity": 100}]
    windows_t2 = [{"session": "s", "index": 0, "active": True, "activity": 999}]
    panes.update_focus_from_windows(windows_t1)
    first = panes._focused_at["s:0"]
    panes.update_focus_from_windows(windows_t2)
    assert panes._focused_at["s:0"] == first  # activity bump did not touch focus
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_panes.py::test_list_windows_parses_window_activity tests/test_panes.py::test_list_windows_activity_defaults_zero_when_absent tests/test_panes.py::test_update_focus_ignores_window_activity -v`
Expected: first two FAIL with `KeyError: 'activity'`; the third PASSES already (it documents the existing-correct behavior and guards against regression in Task 1's edit).

- [ ] **Step 3: Add the column and parse it**

In `periscope/panes.py`, change the `list_windows` format string (line 258) to append `\t#{window_activity}`:

```python
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t#{@periscope_id}\t#{pane_id}\t#{window_activity}",
```

Then in the row-build block (after the `pane_id` line, ~line 277), parse it and add to the dict:

```python
        pane_id = parts[6] if len(parts) > 6 else ""
        # window_activity is tmux's last-output timestamp. Used ONLY as a
        # skip-recapture hint in window_view; it must NEVER feed focus
        # (invariant #1 — focus is keyed on window_active above).
        activity = int(parts[7]) if len(parts) > 7 and parts[7] else 0
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
                "pid_raw": pid_raw,
                "pane_id": pane_id,
                "activity": activity,
            }
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_panes.py -v`
Expected: all PASS, including the existing `list_windows` tests (the extra trailing column is additive).

- [ ] **Step 5: Commit**

```bash
git add periscope/panes.py tests/test_panes.py
git commit -m "poll-opt: surface window_activity from list_windows (skip-hint only)"
```

---

### Task 2: Parsed-pane cache + skip-recapture for quiet panes

**Files:**
- Modify: `periscope/window_view.py:31-69` (`build_window_view` — the capture+parse+smooth block)
- Modify: `tests/conftest.py` (clear the new cache in `clean_state`)
- Modify: `tests/test_window_view.py` (clear the new cache in the reset fixture)
- Test: `tests/test_window_view.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_window_view.py` (the file already defines `_window(...)` and a `_stub_subsystems`/`reset_panes_and_channels` fixture — reuse them; these tests assume `capture` is patched per-test as the existing tests do):

```python
def test_idle_pane_skips_recapture_when_activity_unchanged(mocker, clean_state):
    from periscope import window_view
    cap = mocker.patch("periscope.window_view.capture", return_value="")  # parses to shell/idle
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    assert cap.call_count == 1
    # Second poll, same activity, cached state is quiet → capture NOT called again.
    window_view.build_window_view(w, now_ts=1001)
    assert cap.call_count == 1


def test_pane_recaptures_when_activity_advances(mocker, clean_state):
    from periscope import window_view
    cap = mocker.patch("periscope.window_view.capture", return_value="")
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    w2 = _window()
    w2["activity"] = 700  # tmux saw new output
    window_view.build_window_view(w2, now_ts=1001)
    assert cap.call_count == 2


def test_working_pane_always_recaptures_even_if_activity_unchanged(mocker, clean_state):
    """Spinner grace + done-edge are non-idempotent, so a working pane is never
    skipped even when activity is stale."""
    from periscope import window_view
    cap = mocker.patch(
        "periscope.window_view.capture",
        return_value="claude\n⠋ thinking…",  # parses is_claude + spinner → working
    )
    w = _window()
    w["activity"] = 500
    view, _ = window_view.build_window_view(w, now_ts=1000)
    assert view["state"] == "working"
    window_view.build_window_view(w, now_ts=1001)  # same activity
    assert cap.call_count == 2  # working pane re-captured


def test_skipped_pane_still_reflects_fresh_focus(mocker, clean_state):
    """A skipped (quiet) pane still gets fresh focused_at — only capture+parse
    is skipped, not the recency assembly."""
    from periscope import window_view
    from periscope import panes
    mocker.patch("periscope.window_view.capture", return_value="")
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    panes.note_focus(f"{w['session']}:{w['index']}")  # focus shifts to this pane
    expected = panes._focused_at[f"{w['session']}:{w['index']}"]
    view, _ = window_view.build_window_view(w, now_ts=1001)  # activity unchanged → skip capture
    assert view["focused_at"] == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_window_view.py -k "skips_recapture or recaptures_when_activity or always_recaptures or reflects_fresh_focus" -v`
Expected: FAIL — `test_idle_pane_skips_recapture_when_activity_unchanged` fails on `cap.call_count == 1` (currently 2; no cache yet); the others fail or error similarly.

- [ ] **Step 3: Add the cache and split capture+parse+smooth out of `build_window_view`**

In `periscope/window_view.py`, add a module-level cache near the top (after imports, ~line 29):

```python
# Cache of the parsed-pane dict, keyed by (target, pane_id). Skips
# capture()+parse_pane()+smoothing on a poll when tmux reports no new output
# (window_activity unchanged) AND the cached state is quiet (idle/shell) — the
# only states where re-running smoothing / record_state_transition would be a
# no-op. Working/needs-input/error panes are never skipped. Keying on pane_id
# (not just target) avoids serving a stale parse if a closed window's
# session:index is reused by a new pane whose activity coincidentally matches.
# Bounded by pane count; stale entries for closed panes are harmless. Cleared
# between tests.
#
# Known minor staleness: a Claude pane that exits to a shell and then goes
# silent can stay cached as is_claude=True/idle until its next output (the
# 120s smooth_is_claude expiry only fires on a recapture). The card still
# shows idle; only the is_claude coloring lags. Accepted — the next real
# output recaptures and corrects it.
_view_cache: dict[tuple[str, str], dict] = {}

_QUIET_STATES = ("idle", "shell")
```

Replace the capture+parse+smooth block (current lines 45-69) with a cache-aware version. The block currently is:

```python
    try:
        content = capture(target)
        parsed = parse_pane(content)
    except Exception as e:
        parsed = {"error": str(e), "state": "error", "is_claude": False}

    parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
    parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
    if not parsed["is_claude"]:
        parsed["state"] = "shell"
    if (
        parsed.get("is_claude")
        and parsed.get("spinner")
        and parsed.get("state") not in ("working", "needs-input")
    ):
        parsed["state"] = "working"
```

Replace it with:

```python
    activity = w.get("activity", 0)
    cache_key = (target, w.get("pane_id", ""))
    cached = _view_cache.get(cache_key)
    if (
        cached is not None
        and cached["activity"] == activity
        and cached["parsed"].get("state") in _QUIET_STATES
    ):
        # No new output since last poll and the pane is quiet — reuse the
        # parsed result, skip the capture() subprocess + smoothing. Downstream
        # assembly (stamps, git, channel) still runs every poll below.
        parsed = dict(cached["parsed"])
    else:
        try:
            content = capture(target)
            parsed = parse_pane(content)
        except Exception as e:
            parsed = {"error": str(e), "state": "error", "is_claude": False}

        parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
        parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
        if not parsed["is_claude"]:
            parsed["state"] = "shell"
        if (
            parsed.get("is_claude")
            and parsed.get("spinner")
            and parsed.get("state") not in ("working", "needs-input")
        ):
            parsed["state"] = "working"

        _view_cache[cache_key] = {"activity": activity, "parsed": dict(parsed)}
```

Everything from `cur = parsed.get("state")` (current line 74) onward is unchanged — `record_state_transition`, recency stamps, git/PR/LGTM/channel, affiliation, and view assembly all keep running every poll.

- [ ] **Step 4: Wire the cache into the test-reset fixture**

`tests/test_window_view.py` is the only suite that exercises the real cache path (the route tests in `tests/routes/test_state.py` mock `build_window_view` wholesale, so the cache is never touched there). `clean_state` in `tests/conftest.py` is setup-only (it returns `fresh`, no `yield`/teardown), so it is NOT the right place.

Edit the `reset_panes_and_channels` autouse fixture at the top of `tests/test_window_view.py` (it already has both a setup half and a post-`yield` teardown half clearing `panes._*`). Add this import + clear in **both** halves, right after the `panes._claude_last_seen.clear()` line:

```python
    from periscope import window_view
    window_view._view_cache.clear()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_window_view.py -v`
Expected: all PASS — the four new tests plus every existing `build_window_view` test (the done-edge, linked-PR, channel, capture-exception tests still pass because the downstream assembly is unchanged and `clean_state` now resets the cache).

- [ ] **Step 6: Commit**

```bash
git add periscope/window_view.py tests/conftest.py tests/test_window_view.py
git commit -m "poll-opt: cache parsed pane, skip recapture for quiet unchanged panes"
```

---

### Task 3: Parallelize the per-window fan-out (serial pre/post phases preserved)

**Files:**
- Modify: `periscope/routes/state.py:33-51` (the per-window loop + stamp collection)
- Test: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/routes/test_state.py`. The file provides a `_patch(mocker, name, **kwargs)` helper (top of file) that patches `periscope.routes.state.<name>` — use it for consistency with the existing tests; `client` and `clean_state` are auto-discovered fixtures (`tests/routes/conftest.py`):

```python
def test_state_preserves_window_order_across_fanout(client, mocker, clean_state):
    """The parallel fan-out must return views in the same order as list_windows."""
    windows = [
        {"session": "s", "index": i, "active": i == 0, "activity": 0, "pane_id": f"%{i}", "cwd": ""}
        for i in range(5)
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: ({"index": w["index"]}, None))
    resp = client.get("/api/state")
    assert [v["index"] for v in resp.json()["windows"]] == [0, 1, 2, 3, 4]


def test_state_writes_stamps_once_after_join(client, mocker, clean_state):
    """All _STATE mutation stays single-threaded post-join: set_window_fields_bulk
    is called exactly once with every pane's stamp."""
    windows = [
        {"session": "s", "index": i, "active": False, "activity": 0, "pane_id": f"%{i}", "cwd": ""}
        for i in range(3)
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: ({"index": w["index"]}, (f"pid{w['index']}", 10, 5)))
    bulk = _patch(mocker, "set_window_fields_bulk")
    client.get("/api/state")
    assert bulk.call_count == 1
    written = bulk.call_args[0][0]
    assert set(written.keys()) == {"pid0", "pid1", "pid2"}


def test_state_isolates_one_pane_build_failure(client, mocker, clean_state):
    """A worker RAISING must not sink the response: _safe_build converts it to
    an error view, the other panes build normally, order is preserved."""
    windows = [
        {"session": "s", "index": 0, "active": True, "activity": 0, "pane_id": "%0", "cwd": ""},
        {"session": "s", "index": 1, "active": False, "activity": 0, "pane_id": "%1", "cwd": ""},
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})

    def fake_build(w, now_ts):
        if w["index"] == 0:
            raise RuntimeError("git blew up mid-build")  # NOT a capture error
        return ({"index": 1, "state": "idle"}, None)

    _patch(mocker, "build_window_view", side_effect=fake_build)
    resp = client.get("/api/state")
    views = {v["index"]: v for v in resp.json()["windows"]}
    assert views[0]["state"] == "error"   # _safe_build caught the raise
    assert views[1]["state"] == "idle"
```

- [ ] **Step 2: Run tests to verify the isolation test fails**

Run: `uv run pytest tests/routes/test_state.py -k "preserves_window_order or writes_stamps_once or isolates_one_pane_build_failure" -v`
Expected: `test_state_preserves_window_order_across_fanout` and `test_state_writes_stamps_once_after_join` PASS against the current serial loop (it already preserves order + single-write — they're the regression net for the parallelization). `test_state_isolates_one_pane_build_failure` FAILS — the current serial loop has no `_safe_build`, so a raising worker propagates and the request 500s. Step 3 makes it green.

- [ ] **Step 3: Replace the serial loop with a ThreadPoolExecutor fan-out**

In `periscope/routes/state.py`, add the import at the top (after `import time`):

```python
from concurrent.futures import ThreadPoolExecutor
```

Add a module-level `_safe_build` helper above the `state()` function (after the imports):

```python
def _safe_build(w: dict, now_ts: int) -> tuple[dict, tuple[str, int, int] | None]:
    """Per-worker exception isolation for the parallel fan-out.

    build_window_view already catches capture()/parse_pane() failures, but the
    git / PR / LGTM / affiliation calls run outside that guard. In executor.map
    one worker raising would re-raise on the join and 500 the whole /api/state
    response. Convert any per-pane exception into an error view so one bad pane
    can't sink the board.
    """
    try:
        return build_window_view(w, now_ts)
    except Exception as e:
        target = f"{w['session']}:{w['index']}"
        return (
            {**w, "target": target, "state": "error", "is_claude": False, "error": str(e)},
            None,
        )
```

Replace the per-window loop (current lines 35-41):

```python
    result = []
    stamp_updates: list[tuple[str, int, int]] = []
    for w in windows:
        view, stamp_update = build_window_view(w, now_ts)
        result.append(view)
        if stamp_update is not None:
            stamp_updates.append(stamp_update)
```

with:

```python
    # Parallel fan-out: capture()+parse per pane is the only per-poll
    # subprocess and dominates wall-clock. The serial git-warm + pid mint
    # above (_attach_git_then_resolve_pids — writes state.json + tmux options)
    # is NOT thread-safe and stays before this pool; the stamp write below
    # stays single-threaded after the join. executor.map preserves input
    # order, so `result` matches `windows`. _safe_build isolates any per-pane
    # exception (capture, git, affiliation) into an error view.
    if windows:
        with ThreadPoolExecutor(max_workers=min(32, len(windows))) as pool:
            built = list(pool.map(lambda w: _safe_build(w, now_ts), windows))
    else:
        built = []

    result = [view for view, _ in built]
    stamp_updates: list[tuple[str, int, int]] = [
        stamp for _, stamp in built if stamp is not None
    ]
```

The `_channel_gc`, `set_window_fields_bulk`, resume-GC, and `all_projects` blocks below are unchanged — all run single-threaded after the join.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/routes/test_state.py -v`
Expected: all PASS — order preserved, single bulk write, exception isolation intact.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "poll-opt: parallelize per-window fan-out, keep serial pre/post phases"
```

---

### Task 4: Full-suite regression + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend suite**

Run: `uv run pytest -q`
Expected: all pass (was 222 on a clean run; new tests add to that). Paste the last ~20 lines of output.

- [ ] **Step 2: Run the parse-pane regression script**

Run: `uv run test_parse_pane.py`
Expected: passes — confirms the `list_windows` format change didn't disturb status-line parsing.

- [ ] **Step 3: Manual smoke against a dev instance**

```bash
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
```

Open `http://localhost:8766/` and confirm, against a board with a mix of idle shells and an active Claude pane:
- A working Claude pane still updates its spinner/state every poll (not frozen).
- An idle shell that you `cd`/echo into flips to reflecting the new output within a poll (activity bump → recapture).
- Switching the active window in tmux still updates "viewed Xm" (`focused_at`) on the right card (focus path unaffected).
- A Claude pane that finishes a task still shows the `done` badge (busy→idle edge survives).

- [ ] **Step 4: Commit (if any smoke-driven tweaks were needed)**

```bash
git add -A
git commit -m "poll-opt: smoke-test fixes"   # only if Step 3 surfaced issues
```

---

## Self-review notes

- **Spec coverage:** §Architecture — Backend's two edits (parallelize fan-out; skip idle panes via `window_activity`) → Tasks 3 and 2; the git-already-cached fact means no git change is needed (correct — none planned).
- **Invariant #1:** Task 1's `test_update_focus_ignores_window_activity` is the explicit regression; the `activity` field never reaches `note_focus`/`update_focus_from_windows`.
- **Non-idempotent smoothing:** Task 2 skips only `idle`/`shell` cached states; working/needs-input/error always recapture (test `test_working_pane_always_recaptures...`).
- **Thread safety:** per-pane dict writes (`_view_cache`, `_prev_state`, `_completed_at`, smoothing last-seen) are keyed by distinct target/pid per worker; `_STATE` writes stay on the main thread post-join. GIL-safe under CPython for distinct keys; documented in the Task 3 code comment.
- **Type consistency:** `build_window_view(w, now_ts) -> (view, stamp|None)` used identically in Tasks 2 and 3; `activity` int added in Task 1 and read in Task 2.
