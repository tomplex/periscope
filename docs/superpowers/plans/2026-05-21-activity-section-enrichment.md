# Activity Section Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make periscope's modal Activity section durable (survives restart), actionable (rows link to GitHub), and richer (Haiku "completed X" milestones + context-reset events).

**Architecture:** A new `periscope/activity.py` owns a SQLite store (`periscope.db`) for the events git can't reconstruct — channel alerts, context resets, milestones. Git commits/CI stay computed-on-demand in `git_pr.py`; `activity.py` merges both at read time. A prod-only background worker detects context resets (via the status-line context %) and emits Haiku milestones (on commit runs).

**Tech Stack:** Python 3 + stdlib `sqlite3` (no ORM), FastAPI, vanilla-JS ES modules, the Anthropic SDK via the existing `rename_ai.py` helper. Tests: `uv run pytest -q`.

**Spec:** `docs/superpowers/specs/2026-05-21-activity-section-enrichment-design.md`

---

## File Structure

**Created:**
- `periscope/activity.py` — store (`record`/`events_for`/`prune`), read-path merge (`cached_pane_activity`), transcript locator, reset detector, milestone summarizer, background worker.
- `tests/test_activity.py` — unit tests for every pure/testable function above.

**Modified:**
- `periscope/config.py` — `ACTIVITY_DB` path + `ACTIVITY_DAYS` window constants.
- `periscope/git_pr.py` — add `github_origin()`; widen `shared_activity_for()`'s window and add commit/CI `url`s; remove the old `cached_pane_activity()` + its activity cache (moves to `activity.py`).
- `periscope/channels.py` — `_do_notify_tool()` write-through to `activity.record()`.
- `periscope/routes/pane.py` — import `cached_pane_activity` from `activity`; pass `pane_id`; drop the now-redundant `channel_alerts` payload field.
- `periscope/app.py` — `activity.prune()` at startup; start the worker (prod only).
- `static/modal.js` — `activityRow()` renders `<a>` for rows with a `url`; `renderActivitySection()` reads the pre-merged `data.activity`; `timelineColor`/`timelineLabel` gain `reset`/`milestone` cases.
- `periscope/routes/alerts.py` — `/api/alerts/recent` merges `milestone` events.
- `static/alerts.js` — render milestone rows in the dashboard feed.

The split: `git_pr.py` keeps pure git/CI computation; `activity.py` owns persistence, merge, and the worker. `git_pr.py` must **never** import `activity.py` (would cycle — `activity.py` imports `git_pr`).

---

# Phase 1 — Durable store + wider window + actionable rows

*No AI. Ends with alerts surviving restart and rows linking to GitHub.*

## Task 1: Config constants

**Files:**
- Modify: `periscope/config.py`

- [ ] **Step 1: Add the constants**

Append to `periscope/config.py` (the file already imports `os` and `from pathlib import Path`):

```python
# Activity store (periscope/activity.py). Named generically — periscope.db,
# not activity.db — because it is the destination for other persistent
# state (prefs, projects) that may migrate out of state.json later; the
# generic name avoids a future rename. Path mirrors store.py:_state_path.
_XDG = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
ACTIVITY_DB = Path(_XDG) / "periscope" / "periscope.db"

# Activity timeline window: git commits + CI runs newer than this many
# days show in the modal's Activity section. Was a hardcoded 24h.
ACTIVITY_DAYS = int(os.environ.get("PERISCOPE_ACTIVITY_DAYS", "7"))
```

- [ ] **Step 2: Verify it imports**

Run: `uv run python -c "from periscope import config; print(config.ACTIVITY_DB, config.ACTIVITY_DAYS)"`
Expected: prints a path ending `/periscope/periscope.db` and `7`.

- [ ] **Step 3: Commit**

```bash
git add periscope/config.py
git commit -m "config: add ACTIVITY_DB + ACTIVITY_DAYS for the activity store"
```

## Task 2: The SQLite store

**Files:**
- Create: `periscope/activity.py`
- Create: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_activity.py`:

```python
"""Tests for periscope/activity.py."""
import pytest

from periscope import config, activity


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets an isolated periscope.db."""
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    activity._CONN = None
    yield
    if activity._CONN is not None:
        activity._CONN.close()
        activity._CONN = None


def test_record_then_events_for_roundtrips():
    activity.record("pane", "%1", "alert", "tests pass", detail="done", at=100)
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1
    assert out[0]["src"] == "alert"
    assert out[0]["kind"] == "done"
    assert out[0]["text"] == "tests pass"
    assert out[0]["at"] == 100


def test_events_for_returns_pane_and_branch_scopes():
    activity.record("pane", "%1", "alert", "a", detail="info", at=10)
    activity.record("branch", "/repo\x1fmain", "milestone", "m", at=20)
    activity.record("pane", "%2", "alert", "other pane", detail="info", at=30)
    out = activity.events_for("%1", "/repo", "main")
    texts = {e["text"] for e in out}
    assert texts == {"a", "m"}  # %2's alert excluded


def test_events_for_newest_first():
    activity.record("pane", "%1", "alert", "old", detail="info", at=10)
    activity.record("pane", "%1", "alert", "new", detail="info", at=20)
    out = activity.events_for("%1", "/repo", "main")
    assert [e["text"] for e in out] == ["new", "old"]


def test_dedup_key_makes_record_idempotent():
    activity.record("branch", "/r\x1fmain", "milestone", "x", at=1, dedup_key="m:abc")
    activity.record("branch", "/r\x1fmain", "milestone", "x again", at=2, dedup_key="m:abc")
    out = activity.events_for(None, "/r", "main")
    assert len(out) == 1
    assert out[0]["text"] == "x"  # first write wins


def test_prune_drops_old_rows():
    import time
    now = int(time.time())
    activity.record("pane", "%1", "alert", "recent", detail="info", at=now)
    activity.record("pane", "%1", "alert", "ancient", detail="info", at=now - 99 * 86400)
    activity.prune(max_age_days=30)
    out = activity.events_for("%1", "/repo", "main")
    assert [e["text"] for e in out] == ["recent"]
```

- [ ] **Step 2: Run the tests — verify they fail**

Run: `uv run pytest -q tests/test_activity.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.activity'`.

- [ ] **Step 3: Create `periscope/activity.py` with the store**

```python
"""Activity store + read-path merge + the background activity worker.

Owns periscope.db (SQLite): the durable events git cannot reconstruct —
channel alerts, context resets, Haiku milestones. Git commits and CI runs
stay computed-on-demand in git_pr.py; this module merges them with the
persisted rows at read time for the modal sidebar's Activity section.

Import discipline: this module imports git_pr, panes, rename_ai, config.
git_pr.py must NEVER import activity.py (would create a cycle). No DB work
happens at import time — the connection opens lazily on first use.
"""

import json
import sqlite3
import threading
import time

from periscope import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind  TEXT NOT NULL,         -- 'pane' | 'branch'
  scope_key   TEXT NOT NULL,         -- pane_id (%N)  |  repo_path\\x1fbranch
  event_kind  TEXT NOT NULL,         -- 'alert' | 'milestone' | 'reset'
  at          INTEGER NOT NULL,
  text        TEXT NOT NULL,
  detail      TEXT,
  url         TEXT,
  payload     TEXT,
  dedup_key   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_scope ON events (scope_kind, scope_key, at);
CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT);
"""

_CONN: sqlite3.Connection | None = None
_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    """Lazily open the SQLite connection. Caller must hold _LOCK."""
    global _CONN
    if _CONN is None:
        config.ACTIVITY_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.ACTIVITY_DB), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        c.commit()
        _CONN = c
    return _CONN


def record(scope_kind, scope_key, event_kind, text, *,
           at=None, detail=None, url=None, payload=None, dedup_key=None):
    """Persist one event. INSERT OR IGNORE on dedup_key, so a non-None
    dedup_key already present makes this a no-op. dedup_key=None inserts."""
    row = (
        scope_kind, scope_key, event_kind,
        int(at if at is not None else time.time()),
        text, detail, url,
        json.dumps(payload) if payload is not None else None,
        dedup_key,
    )
    with _LOCK:
        c = _conn()
        c.execute(
            "INSERT OR IGNORE INTO events "
            "(scope_kind,scope_key,event_kind,at,text,detail,url,payload,dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
        c.commit()


def events_for(pane_id, repo_path, branch, limit=40):
    """Persisted events for a pane: pane-scoped rows for pane_id plus
    branch-scoped rows for (repo_path, branch), newest-first, mapped into
    the frontend event model."""
    branch_key = f"{repo_path}\x1f{branch}" if repo_path and branch else "\x00"
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT event_kind,at,text,detail,url FROM events "
            "WHERE (scope_kind='pane' AND scope_key=?) "
            "   OR (scope_kind='branch' AND scope_key=?) "
            "ORDER BY at DESC LIMIT ?",
            (pane_id or "\x00", branch_key, limit),
        ).fetchall()
    return [_row_to_event(*r) for r in rows]


def prune(max_age_days=30):
    """Drop events older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        c.commit()


def _row_to_event(event_kind, at, text, detail, url):
    """Map a DB row into the frontend event model (spec §Event model)."""
    if event_kind == "alert":
        # detail holds the alert kind: done / need_human / info.
        return {"src": "alert", "kind": detail or "info", "at": at, "text": text}
    # reset / milestone — session-sourced rows.
    return {"src": "session", "kind": event_kind, "at": at,
            "text": text, "state": detail, "url": url}
```

- [ ] **Step 4: Run the tests — verify they pass**

Run: `uv run pytest -q tests/test_activity.py`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: SQLite store for durable activity events"
```

## Task 3: GitHub origin + wider window + actionable git URLs

**Files:**
- Modify: `periscope/git_pr.py` (`shared_activity_for`, ~lines 172-232; add `github_origin`)
- Test: `tests/test_git_pr.py` (create if absent)

- [ ] **Step 1: Write the failing test for `github_origin`**

`tests/test_git_pr.py` already exists with other tests — **append** the
following to it; do not recreate the file. Ensure `import subprocess` is
among its imports (add it if absent).

```python
from periscope.git_pr import github_origin


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def test_github_origin_ssh_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "git@github.com:faradayio/periscope.git")
    assert github_origin(str(tmp_path)) == "faradayio/periscope"


def test_github_origin_https_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://github.com/faradayio/periscope.git")
    assert github_origin(str(tmp_path)) == "faradayio/periscope"


def test_github_origin_none_for_non_github(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://gitlab.com/x/y.git")
    assert github_origin(str(tmp_path)) is None


def test_github_origin_none_when_no_remote(tmp_path):
    _git(tmp_path, "init")
    assert github_origin(str(tmp_path)) is None
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest -q tests/test_git_pr.py`
Expected: FAIL — `ImportError: cannot import name 'github_origin'`.

- [ ] **Step 3: Add `github_origin` and update `shared_activity_for`**

In `periscope/git_pr.py`, add `from periscope import config` to the top imports. Add this function just above `shared_activity_for`:

```python
def github_origin(path: str) -> str | None:
    """'owner/repo' for the repo's GitHub `origin` remote, or None for a
    non-GitHub remote or no remote. Handles git@ and https forms."""
    code, url = _run(["git", "-C", path, "remote", "get-url", "origin"])
    if code != 0 or not url:
        return None
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?\s*$", url)
    return m.group(1) if m else None
```

Replace the body of `shared_activity_for` (currently lines ~172-232) with:

```python
def shared_activity_for(path: str, branch: str) -> list[dict]:
    """Repo/branch-scoped events: commits within the ACTIVITY_DAYS window
    + CI runs on the branch. Commit and CI events carry a `url`."""
    events: list[dict] = []
    if not path or not os.path.isdir(path):
        return events
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return events
    slug = github_origin(path)
    # %ct = committer unix time, %H = full sha, %s = subject. Tab-separated
    # so subjects with spaces survive the split.
    code, out = _run(
        ["git", "-C", path, "log", "-20",
         f"--since={config.ACTIVITY_DAYS}d",
         "--pretty=format:%ct%x09%H%x09%s"],
        timeout=3.0,
    )
    if code == 0 and out:
        for line in out.split("\n"):
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                at = int(parts[0])
            except ValueError:
                continue
            sha, subj = parts[1], parts[2].strip()
            if not subj:
                continue
            ev = {"kind": "commit", "at": at, "text": subj}
            if slug:
                ev["url"] = f"https://github.com/{slug}/commit/{sha}"
            events.append(ev)

    if _GH_AVAILABLE and branch:
        code, out = _run(
            ["gh", "run", "list", "--branch", branch, "--limit", "10",
             "--json", "conclusion,status,createdAt,displayTitle,name,url"],
            cwd=path,
            timeout=5.0,
        )
        if code == 0 and out:
            try:
                runs = json.loads(out)
            except Exception:
                runs = []
            from datetime import datetime
            for run in runs:
                state = _gh_run_state(run)
                if state is None:
                    continue
                created = run.get("createdAt") or ""
                try:
                    at = int(
                        datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    )
                except Exception:
                    continue
                name = run.get("displayTitle") or run.get("name") or "workflow"
                ev = {"kind": "ci", "at": at, "text": name, "state": state}
                if run.get("url"):
                    ev["url"] = run["url"]
                events.append(ev)
    return events
```

Leave `cached_pane_activity` and the `_activity_*` cache **in place for now** — Task 6 removes them once `activity.py` owns the read path. The app keeps working: the extra `url` keys are ignored by today's frontend.

- [ ] **Step 4: Run the tests — verify they pass**

Run: `uv run pytest -q tests/test_git_pr.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Verify nothing else broke**

Run: `uv run pytest -q`
Expected: PASS — full suite green.

- [ ] **Step 6: Commit**

```bash
git add periscope/git_pr.py tests/test_git_pr.py
git commit -m "git_pr: github_origin + actionable commit/CI URLs + ACTIVITY_DAYS window"
```

## Task 4: The read-path merge in `activity.py`

**Files:**
- Modify: `periscope/activity.py` (add the merge + git SWR cache)
- Modify: `tests/test_activity.py` (add merge tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
import time as _time


def test_cached_pane_activity_merges_and_sorts(monkeypatch):
    # Pre-seed the git SWR cache so the merge runs with no bg fetch.
    activity._git_cache[("/repo", "main")] = (_time.time(), [
        {"kind": "commit", "at": 50, "text": "older commit"},
        {"kind": "commit", "at": 150, "text": "newer commit"},
    ])
    monkeypatch.setattr(activity, "_acted_at", {"sess:1": 100})
    activity.record("pane", "%9", "alert", "an alert", detail="info", at=120)

    out = activity.cached_pane_activity("sess:1", "%9", "/repo", "main")
    # Newest-first: 150 commit, 120 alert, 100 open, 50 commit.
    assert [e["at"] for e in out] == [150, 120, 100, 50]
    assert out[1]["src"] == "alert"
    assert out[2]["kind"] == "open"


def test_cached_pane_activity_tags_git_events_with_src(monkeypatch):
    activity._git_cache[("/repo", "main")] = (_time.time(), [
        {"kind": "commit", "at": 10, "text": "c", "url": "http://x"},
    ])
    monkeypatch.setattr(activity, "_acted_at", {})
    out = activity.cached_pane_activity("s:1", "%1", "/repo", "main")
    assert out[0]["src"] == "git"
    assert out[0]["url"] == "http://x"
```

These pre-seed `activity._git_cache` directly, so the merge logic is
exercised with no background thread and no timing. Step 3 makes the
`fresh_db` fixture clear that cache between tests.

- [ ] **Step 2: Add the merge to `periscope/activity.py`**

Add these imports to the top of `periscope/activity.py`:

```python
from periscope.git_pr import shared_activity_for
from periscope.log import _bg
from periscope.panes import _acted_at
```

Append to `periscope/activity.py`:

```python
# --- Read path: merge persisted events with computed git events --------
#
# shared_activity_for() runs git/gh subprocesses, so its result is held in
# a stale-while-revalidate cache keyed by (path, branch): a hit returns
# instantly, a miss/expiry kicks a background refresh and returns whatever
# is cached (possibly nothing on the very first call). Same pattern the
# PR cache uses in git_pr.py.

_GIT_TTL = 60.0
_git_cache: dict[tuple, tuple[float, list]] = {}
_git_fetching: set = set()
_git_lock = threading.Lock()


def _fetch_git_into_cache(path, branch):
    try:
        events = shared_activity_for(path, branch)
    except Exception:
        events = []
    with _git_lock:
        _git_cache[(path, branch)] = (time.time(), events)
        _git_fetching.discard((path, branch))


def cached_pane_activity(target, pane_id, path, branch, limit=40):
    """Merged Activity stream for a pane, newest-first: git/CI events
    (stale-while-revalidate cache) + persisted alert/reset/milestone
    events + the per-target 'opened in periscope' anchor."""
    events: list[dict] = []
    if path and branch:
        key = (path, branch)
        now = time.time()
        with _git_lock:
            cached = _git_cache.get(key)
            stale = cached is None or (now - cached[0] >= _GIT_TTL)
            if stale and key not in _git_fetching:
                _git_fetching.add(key)
                _bg("activity-git-fetch", _fetch_git_into_cache, path, branch)
            git_events = cached[1] if cached else []
        for e in git_events:
            events.append({**e, "src": "git"})
    # Persisted events (alerts, resets, milestones).
    events.extend(events_for(pane_id, path, branch, limit=limit))
    # Per-target "opened in periscope" anchor.
    opened_at = _acted_at.get(target, 0)
    if opened_at:
        events.append({"src": "git", "kind": "open", "at": opened_at,
                       "text": "opened in periscope"})
    events.sort(key=lambda e: e.get("at", 0), reverse=True)
    return events[:limit]
```

- [ ] **Step 3: Clear the git cache between tests**

`_git_cache` / `_git_fetching` are module globals; the `fresh_db` fixture
must clear them so a pre-seeded cache from one test cannot leak into the
next. In `tests/test_activity.py`, replace the `fresh_db` fixture with:

```python
@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets an isolated periscope.db and empty caches."""
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    activity._CONN = None
    activity._git_cache.clear()
    activity._git_fetching.clear()
    yield
    if activity._CONN is not None:
        activity._CONN.close()
        activity._CONN = None
```

- [ ] **Step 4: Run the tests — verify they pass**

Run: `uv run pytest -q tests/test_activity.py`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: read-path merge of git events + persisted events"
```

## Task 5: Write alerts through to the store

**Files:**
- Modify: `periscope/channels.py` (`_do_notify_tool`, ~lines 115-135)
- Modify: `periscope/app.py` (lifespan)

- [ ] **Step 1: Write alerts to the store in `_do_notify_tool`**

In `periscope/channels.py`, inside `_do_notify_tool`, immediately after
the `with _CHANNELS_LOCK:` block that appends to `_CHANNEL_ALERTS`, add:

```python
    # Durable mirror (survives restart, feeds the modal's merged Activity
    # stream). _CHANNEL_ALERTS above stays as the write-through cache for
    # the unread badge. record()'s positional args are
    # (scope_kind, scope_key, event_kind, text); the alert kind
    # (done/need_human/info) rides in `detail`.
    try:
        from periscope import activity
        activity.record("pane", pane, "alert", message,
                        detail=kind, at=entry["ts"])
    except Exception:
        log.warning("activity.record failed for notify()", exc_info=True)
```

Confirm `log` is imported in `channels.py` — if not, add
`from periscope.log import log` to its imports.

- [ ] **Step 2: Prune the store at startup**

In `periscope/app.py`, inside `lifespan`, after the `kill_orphan_usage_sessions()` line, add:

```python
    # Bound activity.db growth — drop events older than 30 days.
    from periscope import activity
    _bg("activity-prune", activity.prune)
```

- [ ] **Step 3: Smoke-test the write path**

Run: `uv run python -c "from periscope import config, activity; import tempfile, pathlib; config.ACTIVITY_DB = pathlib.Path(tempfile.mkdtemp())/'p.db'; activity.record('pane','%1','alert','hi',detail='done'); print(activity.events_for('%1','/r','main'))"`
Expected: prints a one-element list with `text='hi'`, `kind='done'`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py periscope/app.py
git commit -m "channels: write notify() alerts through to the activity store"
```

## Task 6: Cut the read path over + actionable rows in the UI

This task moves the read path off `git_pr.cached_pane_activity` and onto
`activity.cached_pane_activity`, removes the dead code, and updates the
frontend — all in one commit so the app works before and after.

**Files:**
- Modify: `periscope/routes/pane.py` (~lines 16, 62, 80, 105)
- Modify: `periscope/git_pr.py` (remove old `cached_pane_activity` + `_activity_*`)
- Modify: `static/modal.js` (`activityRow`, `renderActivitySection`)

- [ ] **Step 1: Repoint `routes/pane.py`**

In `periscope/routes/pane.py`:

Line 16 — change the git_pr import to drop `cached_pane_activity`:
```python
from periscope.git_pr import cached_git_state, cached_pr_state
```
Add a new import line below it:
```python
from periscope.activity import cached_pane_activity
```

Delete line 62 (`activity = cached_pane_activity(target, cwd, git.get("branch"))`). The call moves below, after `pane_id` is resolved.

After the `for w in list_windows():` loop that sets `pane_id` (~lines 73-76), add:
```python
    activity = cached_pane_activity(target, pane_id, cwd, git.get("branch"))
```

Remove the now-unused `channel_alerts` local (~line 80):
```python
        channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
```
(delete the `channel_alerts = ...` line in that block).

Remove the `"channel_alerts": channel_alerts,` key from the returned dict (~line 105).

If `_CHANNEL_ALERTS` is now unused in `pane.py`, drop it from the
`from periscope.channels import ...` line.

- [ ] **Step 2: Remove the dead read path from `git_pr.py`**

In `periscope/git_pr.py`, delete:
- the `_ACTIVITY_TTL`, `_activity_cache`, `_activity_fetching`, `_activity_lock` module globals,
- `_fetch_activity_into_cache`,
- `cached_pane_activity`.

Keep `shared_activity_for` and `_gh_run_state`. If `_acted_at` is now
unused in `git_pr.py`, drop it from `from periscope.panes import ...`
(keep `list_windows` — `prewarm_pr_cache` still uses it).

- [ ] **Step 3: Update `activityRow` for actionable rows**

In `static/modal.js`, replace the non-alert branch of `activityRow` (the
`return` after the `if (e.src === "alert")` block) with:

```javascript
  const body = e.url
    ? `<a class="timeline-text timeline-link" href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.text)}</a>`
    : `<div class="timeline-text">${escapeHtml(e.text)}</div>`;
  return `
    <li class="timeline-row" data-kind="${escapeHtml(e.kind)}">
      <span class="timeline-dot" style="background:${timelineColor(e.kind, e.state)}"></span>
      <div class="timeline-body">
        ${body}
        <div class="timeline-when">${escapeHtml(timelineLabel(e.kind, e.state))} · ${escapeHtml(relTime(e.at))} ago</div>
      </div>
    </li>
  `;
```

- [ ] **Step 4: Update `renderActivitySection` to read the merged stream**

`data.activity` is now the fully merged, newest-first stream (git +
alerts + resets + milestones). Replace the body of `renderActivitySection`
in `static/modal.js` with:

```javascript
function renderActivitySection(data) {
  const stream = data.activity || [];

  // Pin the latest unresolved need_human alert above the stream.
  const latestAlert = stream
    .filter((e) => e.src === "alert")
    .reduce((best, a) => (best && best.at >= a.at ? best : a), null);
  const pinned =
    latestAlert && latestAlert.kind === "need_human" ? latestAlert : null;
  const rest = stream.filter((e) => e !== pinned);

  let html = "";
  if (pinned) {
    html += `
      <div class="activity-pinned">
        <div class="activity-pinned-label">needs you · ${escapeHtml(relTime(pinned.at))} ago</div>
        <div class="activity-pinned-text">${escapeHtml(pinned.text)}</div>
      </div>
    `;
  }
  if (rest.length) {
    html += `<ol class="timeline activity-stream">${rest.map(activityRow).join("")}</ol>`;
  } else if (!pinned) {
    html += `<div class="timeline-empty">no recent activity</div>`;
  }
  return html;
}
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q`
Expected: PASS — and `grep -rn "cached_pane_activity" periscope/` shows it only in `activity.py` and `routes/pane.py`.

- [ ] **Step 6: Verify in the browser**

Start dev periscope: `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`, open `http://localhost:8766/`, open a pane modal with a Claude session. Confirm: the Activity section still renders; commit rows are now clickable links (hover shows a GitHub `/commit/` URL); a CI row links to the run. Have a pane call `notify()` and confirm the alert appears in the stream. Restart periscope and confirm the alert is still there.

- [ ] **Step 7: Commit**

```bash
git add periscope/routes/pane.py periscope/git_pr.py static/modal.js
git commit -m "activity: cut modal Activity onto the durable store; rows link to GitHub"
```

---

# Phase 2 — Context-reset events

*Adds `/clear` + compaction detection via the status-line context %.*

## Task 7: Locate the live transcript

**Files:**
- Modify: `periscope/activity.py` (add `live_transcript_for`)
- Modify: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
import json as _json


def _write_transcript(path, cwd, *, mtime=None):
    # Faithful to the real shape: the first line is a file-history-snapshot
    # with no cwd; user text lives at message.content, not a top-level key.
    lines = [
        {"type": "file-history-snapshot"},
        {"type": "user", "cwd": cwd,
         "message": {"role": "user", "content": "hi"}},
    ]
    path.write_text("\n".join(_json.dumps(d) for d in lines) + "\n")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))


def test_live_transcript_for_matches_on_cwd(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-Users-tom-dev-periscope"
    d.mkdir(parents=True)
    tf = d / "abc.jsonl"
    _write_transcript(tf, "/Users/tom/dev/periscope")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/Users/tom/dev/periscope") == tf


def test_live_transcript_for_picks_newest_mtime(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-repo"
    d.mkdir(parents=True)
    old, new = d / "old.jsonl", d / "new.jsonl"
    _write_transcript(old, "/repo", mtime=1000)
    _write_transcript(new, "/repo", mtime=2000)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/repo") == new


def test_live_transcript_for_rejects_cwd_mismatch(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-repo"
    d.mkdir(parents=True)
    tf = d / "abc.jsonl"
    _write_transcript(tf, "/some/other/repo")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/repo") is None
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest -q tests/test_activity.py -k transcript`
Expected: FAIL — `AttributeError: module 'periscope.activity' has no attribute 'live_transcript_for'`.

- [ ] **Step 3: Implement `live_transcript_for`**

Add to `periscope/activity.py` (add `from pathlib import Path` to the imports):

```python
# --- Live transcript location ------------------------------------------
#
# Claude Code writes transcripts to ~/.claude/projects/<encoded-cwd>/
# <session-uuid>.jsonl. We resolve via the encoded dir ('/' and '.' ->
# '-') as a fast path — scanning all ~3500 transcript dirs every worker
# tick is the wrong cost. The cwd-field check below still guards file
# selection within that dir. If Claude Code ever encodes a character
# differently, that cwd gets no transcript (graceful: resets still fire
# from the context-% drop; milestones still summarize commit messages).

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace(".", "-")


def _transcript_cwd(jsonl_path: Path) -> str | None:
    """The `cwd` recorded in a transcript. Scans the first 15 lines — a
    transcript opens with cwd-less entries (file-history-snapshot,
    queue-operation, last-prompt) before the first real turn."""
    try:
        with jsonl_path.open() as fh:
            for _ in range(15):
                line = fh.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except Exception:
                    continue
                if cwd:
                    return cwd
    except Exception:
        return None
    return None


def live_transcript_for(cwd: str) -> Path | None:
    """The live transcript JSONL for a pane at `cwd`: newest-mtime file in
    the encoded projects dir whose recorded `cwd` matches. None if absent."""
    d = _PROJECTS_DIR / _encode_cwd(cwd)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        if _transcript_cwd(f) == cwd:
            return f
    return None
```

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest -q tests/test_activity.py -k transcript`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: live_transcript_for — locate a pane's transcript by cwd"
```

## Task 8: Context-reset detection

**Files:**
- Modify: `periscope/activity.py` (add `_check_reset`, `_compact_or_clear`, helpers)
- Modify: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
def test_check_reset_fires_on_context_drop(monkeypatch):
    # Keep _compact_or_clear hermetic — no real ~/.claude lookup.
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    last = {}
    assert activity._check_reset("%1", "/repo", 60, last) is False   # baseline
    assert activity._check_reset("%1", "/repo", 62, last) is False   # climbing
    assert activity._check_reset("%1", "/repo", 8, last) is True     # dropped
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1 and out[0]["kind"] == "reset"


def test_check_reset_ignores_none_readings(monkeypatch):
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    last = {}
    activity._check_reset("%1", "/repo", 60, last)
    assert activity._check_reset("%1", "/repo", None, last) is False  # obscured
    assert activity._check_reset("%1", "/repo", 61, last) is False    # climbed
    assert activity.events_for("%1", "/repo", "main") == []


def test_compact_or_clear_labels_cleared_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    detail, text = activity._compact_or_clear("/repo")
    assert detail == "cleared"
    assert "clear" in text.lower()


def test_compact_or_clear_labels_compacted_with_marker(tmp_path, monkeypatch):
    tf = tmp_path / "t.jsonl"
    entry = {
        "type": "system", "subtype": "compact_boundary",
        "timestamp": "2026-05-21T12:00:00.000Z",
        "compactMetadata": {"trigger": "auto",
                            "preTokens": 303000, "postTokens": 14000},
    }
    tf.write_text(_json.dumps(entry) + "\n")
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: tf)
    monkeypatch.setattr(activity, "_compact_is_recent", lambda ts: True)
    detail, text = activity._compact_or_clear("/repo")
    assert detail == "compacted"
    assert "303k" in text and "14k" in text
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest -q tests/test_activity.py -k "reset or compact"`
Expected: FAIL — `AttributeError: ... has no attribute '_check_reset'`.

- [ ] **Step 3: Implement reset detection**

Add to `periscope/activity.py` (add `from datetime import datetime, timezone` to imports):

```python
# --- Context-reset detection -------------------------------------------
#
# Both /clear and a compaction reset Claude's context. /clear leaves no
# transcript marker, so detection keys off the status-line context %,
# which climbs monotonically during a session and drops only on a reset.

def _human_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _compact_is_recent(ts: str) -> bool:
    """True if an ISO8601 timestamp is within the last 5 minutes."""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() < 300


def _recent_compact_meta(jsonl_path) -> dict | None:
    """Scan the tail of a transcript for a recent compact_boundary entry;
    return its compactMetadata, or None."""
    try:
        data = jsonl_path.read_text()
    except Exception:
        return None
    for line in reversed(data.splitlines()[-200:]):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            if _compact_is_recent(d.get("timestamp") or ""):
                return d.get("compactMetadata") or {}
    return None


def _compact_or_clear(cwd: str) -> tuple[str, str]:
    """Best-effort label for a context reset. A recent compact_boundary in
    the live transcript -> ('compacted', text); else ('cleared', text)."""
    try:
        tf = live_transcript_for(cwd)
        if tf:
            meta = _recent_compact_meta(tf)
            if meta is not None:
                trig = meta.get("trigger") or "auto"
                pre = _human_tokens(meta.get("preTokens"))
                post = _human_tokens(meta.get("postTokens"))
                return "compacted", f"context compacted ({trig} · {pre} → {post})"
    except Exception:
        pass
    return "cleared", "context cleared (/clear)"


def _check_reset(pane_id: str, cwd: str, context_pct, last_ctx: dict) -> bool:
    """Compare context_pct to the last reading for pane_id. A drop between
    two non-None readings is a context reset — record it. Returns True if
    a reset was recorded. last_ctx is the worker's per-pane memory."""
    prev = last_ctx.get(pane_id)
    if context_pct is not None:
        last_ctx[pane_id] = context_pct
    if prev is None or context_pct is None or context_pct >= prev:
        return False
    detail, text = _compact_or_clear(cwd)
    record("pane", pane_id, "reset", text, detail=detail)
    return True
```

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest -q tests/test_activity.py -k "reset or compact"`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: context-reset detection via the status-line context %"
```

## Task 9: Render `reset` rows in the modal

**Files:**
- Modify: `static/modal.js` (`timelineColor`, `timelineLabel`)

- [ ] **Step 1: Add the `reset` case to `timelineColor`**

In `static/modal.js`, in `timelineColor`, add before the final `return`:

```javascript
  if (kind === "reset") return "var(--s-working)";
```

- [ ] **Step 2: Add the `reset` case to `timelineLabel`**

In `timelineLabel`, add before the final `return kind;`:

```javascript
  if (kind === "reset") return evState === "compacted" ? "compacted" : "cleared";
```

- [ ] **Step 3: Verify in the browser**

Reload the dev periscope window. (Reset events are emitted by the worker
built in Task 10 — after that lands, run `/clear` in a watched Claude
pane and confirm a "cleared" row appears in that pane's Activity section
within ~30s.) For now just confirm the modal still renders.

- [ ] **Step 4: Commit**

```bash
git add static/modal.js
git commit -m "modal: render context-reset rows in the Activity stream"
```

## Task 10: The background worker

**Files:**
- Modify: `periscope/activity.py` (add `run_worker`, `_worker_tick`)
- Modify: `periscope/app.py` (start the worker, prod only)

- [ ] **Step 1: Implement the worker in `activity.py`**

Add these imports to `periscope/activity.py`:

```python
import asyncio

from periscope.log import log
from periscope.panes import list_windows, parse_pane
from periscope.tmux import tmux
```

Append to `periscope/activity.py`:

```python
# --- Background worker -------------------------------------------------
#
# One lifespan-driven loop (prod instance only — see app.py). Every ~30s
# it captures each active Claude pane and runs the context-reset check.
# Phase 3 extends _worker_tick with the milestone check.

def _worker_tick(last_ctx: dict) -> None:
    """One worker pass. Blocking (tmux subprocesses) — run off-loop."""
    for w in list_windows():
        target = f"{w['session']}:{w['index']}"
        try:
            content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-60")
            parsed = parse_pane(content)
        except Exception:
            continue
        if not parsed.get("is_claude"):
            continue
        _check_reset(w.get("pane_id") or "", w.get("cwd") or "",
                     parsed.get("context_pct"), last_ctx)


async def run_worker() -> None:
    """Lifespan task: drive _worker_tick every 30s. The blocking tick runs
    in a thread so it never stalls the event loop."""
    last_ctx: dict = {}
    while True:
        try:
            await asyncio.to_thread(_worker_tick, last_ctx)
        except Exception:
            log.exception("activity worker tick failed")
        await asyncio.sleep(30)
```

- [ ] **Step 2: Start the worker in the lifespan**

In `periscope/app.py`, inside `lifespan`, after the `lgtm_task = ...` line:

```python
    # Activity worker: context-reset + milestone detection. Prod only —
    # periscope.db is a single shared file; two workers would race the
    # milestone cursor and double-spend Haiku. Same guard as the MCP
    # listener above. NB: _task's signature is _task(name, coro).
    if config.PORT == 8765:
        from periscope import activity
        activity_task = _task("activity-worker", activity.run_worker())
    else:
        activity_task = None
```

In the `finally:` block, alongside `lgtm_task.cancel()`:

```python
        if activity_task is not None:
            activity_task.cancel()
```

- [ ] **Step 3: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Verify in the browser**

Restart prod periscope: `bin/periscope restart`. In a watched Claude
pane, run `/clear`. Within ~30s, open that pane's modal — the Activity
section shows a `cleared` row. Trigger a compaction (or wait for one) and
confirm a `compacted · NNk → MMk` row appears.

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py periscope/app.py
git commit -m "activity: prod-only background worker driving reset detection"
```

---

# Phase 3 — Haiku "completed X" milestones

*Summarizes a run of commits into one milestone row via Haiku.*

## Task 11: The milestone prompt builder

**Files:**
- Modify: `periscope/activity.py` (add `build_milestone_prompt`)
- Modify: `tests/test_activity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity.py`:

```python
def test_build_milestone_prompt_shape():
    p = activity.build_milestone_prompt(
        ["add the parser", "wire the parser into routes"],
        ["please add a config parser"],
    )
    assert "completed:" in p
    assert "add the parser" in p
    assert "wire the parser into routes" in p
    assert "please add a config parser" in p
    # Asks for exactly one line.
    assert "ONE line" in p or "one line" in p


def test_build_milestone_prompt_without_prompts():
    p = activity.build_milestone_prompt(["fix the bug"], [])
    assert "fix the bug" in p
    assert "completed:" in p
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest -q tests/test_activity.py -k milestone_prompt`
Expected: FAIL — `AttributeError: ... 'build_milestone_prompt'`.

- [ ] **Step 3: Implement `build_milestone_prompt`**

Append to `periscope/activity.py`:

```python
# --- Haiku milestones --------------------------------------------------

def build_milestone_prompt(commits: list[str], prompts: list[str]) -> str:
    """Prompt asking Haiku to compress a run of work into one line.
    commits: subject lines, oldest first. prompts: user-turn text."""
    lines = [
        "A developer just finished a run of work on one git branch.",
        "Summarize what was accomplished as ONE line, in exactly this form:",
        "  completed: <feature>",
        "Constraints: <= 80 characters total, concrete, no trailing period.",
        "",
        "Commits (oldest first):",
    ]
    lines += [f"  - {c}" for c in commits]
    if prompts:
        lines += ["", "What the developer asked for:"]
        lines += [f"  - {p}" for p in prompts]
    lines += ["", "Return ONLY the single 'completed: ...' line, nothing else."]
    return "\n".join(lines)
```

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest -q tests/test_activity.py -k milestone_prompt`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: build_milestone_prompt for Haiku milestone summaries"
```

## Task 12: Emit milestones on commit runs

**Files:**
- Modify: `periscope/activity.py` (add `maybe_emit_milestone` + git/cursor helpers; extend `_worker_tick`)
- Modify: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": __import__("os").environ["PATH"]})


def _commit(repo, msg):
    (repo / "f.txt").write_text(msg)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", msg)


def test_recent_user_prompts_reads_message_content(tmp_path, monkeypatch):
    # Regression guard: user text is at message.content, never a top-level
    # `text` key. A buggy d.get("text") reader returns [] and this fails.
    tf = tmp_path / "t.jsonl"
    entries = [
        {"type": "user", "isMeta": True,
         "message": {"role": "user", "content": "<system junk>"}},
        {"type": "user",
         "message": {"role": "user", "content": "add a config parser"}},
        {"type": "assistant",
         "message": {"role": "assistant", "content": "ok"}},
        {"type": "user",
         "message": {"role": "user", "content": [{"type": "tool_result"}]}},
        {"type": "user",
         "message": {"role": "user", "content": "now wire it up"}},
    ]
    tf.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: tf)
    assert activity._recent_user_prompts("/repo") == [
        "add a config parser", "now wire it up"]


def test_maybe_emit_milestone_summarizes_a_commit_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _commit(repo, "first commit")
    _commit(repo, "second commit")
    monkeypatch.setattr(activity, "claude_complete",
                        lambda prompt: "completed: the thing")
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)

    activity.maybe_emit_milestone(str(repo), "main", settled=True)
    out = activity.events_for(None, str(repo), "main")
    assert len(out) == 1
    assert out[0]["kind"] == "milestone"
    assert out[0]["text"] == "completed: the thing"


def test_maybe_emit_milestone_noop_when_head_unchanged(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _commit(repo, "only commit")
    calls = []
    monkeypatch.setattr(activity, "claude_complete",
                        lambda p: calls.append(p) or "completed: x")
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)

    activity.maybe_emit_milestone(str(repo), "main", settled=True)
    activity.maybe_emit_milestone(str(repo), "main", settled=True)  # HEAD unchanged
    assert len(calls) == 1
    assert len(activity.events_for(None, str(repo), "main")) == 1


def test_maybe_emit_milestone_noop_when_not_settled(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _commit(repo, "a commit")
    monkeypatch.setattr(activity, "claude_complete", lambda p: "completed: x")
    activity.maybe_emit_milestone(str(repo), "main", settled=False)
    assert activity.events_for(None, str(repo), "main") == []
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest -q tests/test_activity.py -k "maybe_emit or recent_user_prompts"`
Expected: FAIL — `AttributeError` (`maybe_emit_milestone` / `_recent_user_prompts` not defined yet).

- [ ] **Step 3: Implement `maybe_emit_milestone` + helpers**

Add to the imports of `periscope/activity.py`:

```python
from periscope.git_pr import cached_git_state, github_origin, shared_activity_for
from periscope.rename_ai import claude_complete
from periscope.tmux import _run
```

(Replace the existing `from periscope.git_pr import shared_activity_for`
line with the combined import above.)

Append to `periscope/activity.py`:

```python
def _cursor_get(key: str) -> str | None:
    with _LOCK:
        c = _conn()
        row = c.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _cursor_set(key: str, value: str) -> None:
    with _LOCK:
        c = _conn()
        c.execute("INSERT INTO cursors (key,value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))
        c.commit()


def _git_head(path: str) -> str | None:
    code, out = _run(["git", "-C", path, "rev-parse", "HEAD"])
    return out.strip() if code == 0 and out else None


def _commits_since(path: str, last: str | None, head: str) -> list[tuple[int, str]]:
    """(committer_unix_time, subject) for commits in last..head, oldest
    first. When last is None, the most recent 15 commits up to head."""
    rev = f"{last}..{head}" if last else f"-15 {head}"
    code, out = _run(["git", "-C", path, "log", *rev.split(),
                      "--pretty=format:%ct%x09%s"], timeout=3.0)
    rows: list[tuple[int, str]] = []
    if code == 0 and out:
        for line in out.split("\n"):
            ct, _, subj = line.partition("\t")
            if ct.isdigit() and subj.strip():
                rows.append((int(ct), subj.strip()))
    rows.reverse()  # oldest first
    return rows[-15:]


def _recent_user_prompts(cwd: str, limit: int = 4) -> list[str]:
    """Best-effort: the last few real user-turn texts from the live
    transcript. Claude Code stores user text at message.content — a string
    for a typed prompt, a list of blocks for tool-result/image turns.
    Skip meta turns and non-string content; there is no top-level `text`."""
    tf = live_transcript_for(cwd)
    if not tf:
        return []
    prompts: list[str] = []
    try:
        for line in tf.read_text().splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "user" or d.get("isMeta"):
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, str) and content.strip():
                prompts.append(content.strip()[:300])
    except Exception:
        return []
    return prompts[-limit:]


def maybe_emit_milestone(path: str, branch: str, settled: bool) -> None:
    """If HEAD advanced past the last summarized SHA and every Claude pane
    on the branch is settled, summarize the commit run via Haiku and
    record one milestone event."""
    head = _git_head(path)
    if not head:
        return
    branch_key = f"{path}\x1f{branch}"
    cursor = f"milestone:{branch_key}"
    last = _cursor_get(cursor)
    if last == head:
        return
    if not settled:
        return  # debounce — wait for the commit run to finish
    commits = _commits_since(path, last, head)
    if not commits:
        _cursor_set(cursor, head)
        return
    prompts = _recent_user_prompts(path)
    try:
        line = claude_complete(
            build_milestone_prompt([c[1] for c in commits], prompts)
        ).strip()
    except Exception:
        log.warning("milestone summary failed", exc_info=True)
        return  # do NOT advance the cursor — retry next tick
    text = (line.splitlines()[0][:90]) if line else "completed: (work)"
    slug = github_origin(path)
    url = (f"https://github.com/{slug}/compare/{last}...{head}"
           if slug and last else None)
    record("branch", branch_key, "milestone", text,
           at=commits[-1][0], url=url, dedup_key=f"milestone:{head}")
    _cursor_set(cursor, head)
```

- [ ] **Step 4: Extend `_worker_tick` with the milestone check**

Replace `_worker_tick` in `periscope/activity.py` with:

```python
def _worker_tick(last_ctx: dict) -> None:
    """One worker pass. Blocking (tmux + git subprocesses) — run off-loop."""
    panes: list[tuple[dict, dict]] = []
    for w in list_windows():
        target = f"{w['session']}:{w['index']}"
        try:
            content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-60")
            parsed = parse_pane(content)
        except Exception:
            continue
        if not parsed.get("is_claude"):
            continue
        panes.append((w, parsed))
        _check_reset(w.get("pane_id") or "", w.get("cwd") or "",
                     parsed.get("context_pct"), last_ctx)
    # Milestones: one check per unique (cwd, branch) across Claude panes.
    seen: set = set()
    for w, _parsed in panes:
        cwd = w.get("cwd")
        if not cwd:
            continue
        branch = (cached_git_state(cwd) or {}).get("branch")
        if not branch or (cwd, branch) in seen:
            continue
        seen.add((cwd, branch))
        on_branch = [p for p in panes if p[0].get("cwd") == cwd]
        settled = all(p[1].get("state") in ("idle", "shell") for p in on_branch)
        try:
            maybe_emit_milestone(cwd, branch, settled)
        except Exception:
            log.exception("maybe_emit_milestone failed for %s", cwd)
```

- [ ] **Step 5: Run the tests — verify they pass**

Run: `uv run pytest -q tests/test_activity.py`
Expected: PASS (all activity tests).

- [ ] **Step 6: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: emit Haiku 'completed X' milestones on commit runs"
```

## Task 13: Render `milestone` rows in the modal

**Files:**
- Modify: `static/modal.js` (`timelineColor`, `timelineLabel`)

- [ ] **Step 1: Add the `milestone` case to `timelineColor`**

In `static/modal.js`, in `timelineColor`, add before the final `return`:

```javascript
  if (kind === "milestone") return "var(--s-success)";
```

- [ ] **Step 2: Add the `milestone` case to `timelineLabel`**

In `timelineLabel`, add before the final `return kind;`:

```javascript
  if (kind === "milestone") return "milestone";
```

- [ ] **Step 3: Verify in the browser**

After Task 12's worker is live: in a watched Claude pane, make 2-3
commits, then let the pane go idle. Within ~30s a green `milestone` row
(`completed: …`) appears in that pane's Activity section, linking to the
GitHub compare view.

- [ ] **Step 4: Commit**

```bash
git add static/modal.js
git commit -m "modal: render milestone rows in the Activity stream"
```

## Task 14: Milestones in the dashboard notifications feed

**Files:**
- Modify: `periscope/routes/alerts.py` (`/api/alerts/recent`)
- Modify: `static/alerts.js` (`renderRow`)

- [ ] **Step 1: Merge milestone events into `/api/alerts/recent`**

In `periscope/routes/alerts.py`, after the loop that builds `items` from
`_CHANNEL_ALERTS` and before `items.sort(...)`, add:

```python
    # Milestones are branch-scoped — surface recent ones dashboard-wide.
    # (Reset events stay modal-only, per the spec.)
    from periscope import activity
    seen_branches: set = set()
    for w in windows:
        cwd = w.get("cwd") or ""
        if not cwd:
            continue
        gs = activity.cached_git_state(cwd)
        branch = (gs or {}).get("branch")
        if not branch or (cwd, branch) in seen_branches:
            continue
        seen_branches.add((cwd, branch))
        target = f"{w['session']}:{w['index']}"
        for e in activity.events_for(None, cwd, branch, limit=10):
            if e.get("kind") != "milestone":
                continue
            items.append({
                "ts": e["at"],
                "kind": "milestone",
                "severity": "info",
                "message": e["text"],
                "pane_id": w.get("pane_id") or "",
                "target": target,
                "session": w["session"],
                "index": w["index"],
                "name": w["name"],
            })
```

Note: `activity.cached_git_state` is re-exported — `activity.py` already
imports `cached_git_state` from `git_pr` (Task 12). If the linter flags
the indirection, import `cached_git_state` directly from
`periscope.git_pr` in `routes/alerts.py` instead.

- [ ] **Step 2: Render milestone rows in `alerts.js`**

In `static/alerts.js`, in `renderRow`, change the `icon` line to handle
the `milestone` kind:

```javascript
  const icon =
    kind === "need_human" ? "⚠"
    : kind === "done" ? "✓"
    : kind === "milestone" ? "★"
    : "•";
```

- [ ] **Step 3: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Verify in the browser**

With a milestone recorded (from Task 12), open the dashboard
notifications feed (the right rail). A `★` milestone row appears, linking
to the originating pane's modal on click.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/alerts.py static/alerts.js
git commit -m "alerts: surface milestones in the dashboard notifications feed"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:
- Durable store (`periscope.db`, `events`/`cursors`, `record`/`events_for`/`prune`) → Tasks 1, 2.
- `pane_id` keying / alerts write-through → Tasks 2, 5.
- Wider window (`ACTIVITY_DAYS`) → Tasks 1, 3.
- Actionable rows (commit/CI `url`, `github_origin`) → Tasks 3, 6.
- Read-path merge (`cached_pane_activity` moves to `activity.py`) → Tasks 4, 6.
- `live_transcript_for` → Task 7.
- Context-reset detection (status-line %, compact-vs-clear label) → Tasks 8, 9, 10.
- Prod-gated worker → Task 10.
- Haiku milestones (`build_milestone_prompt`, `maybe_emit_milestone`, commit-run trigger, settled gate, dedup) → Tasks 11, 12, 13.
- Milestones in the notifications feed → Task 14.
- Import discipline (`git_pr` never imports `activity`; `routes/pane.py` repointed) → Task 6.

**Deviations from the spec, called out:**
- The worker runs as an async `_task` that offloads the blocking tick via `asyncio.to_thread`. The spec said `_task`; this honors that while keeping subprocess work off the event loop.
- `live_transcript_for` resolves a transcript via the *encoded projects-dir* path only (`/` and `.` → `-`), not a full `~/.claude/projects/*/*.jsonl` glob. The spec's wording leans on the glob with the encoded dir as "an optimization"; scanning all ~3500 transcript dirs every 30s worker tick is the wrong cost. The `cwd`-field check still guards file selection *within* the encoded dir. Failure mode if Claude Code ever encodes a character differently: that cwd gets no milestone prompts and no compact-vs-clear label — graceful, since resets still fire from the context-% drop and milestones still summarize from commit messages.

**Placeholder scan:** none — every code step shows the exact code to write, every command its expected output.

**Type consistency:** `record()` / `events_for()` / `cached_pane_activity()` / `_check_reset()` / `maybe_emit_milestone()` signatures are used identically across tasks and tests. Event dicts always carry `src` + `kind` + `at` + `text`; `url`/`state` optional. The `cursors` key for milestones is `milestone:{path}\x1f{branch}` in both `maybe_emit_milestone` and its tests.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-activity-section-enrichment.md`. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session via executing-plans, batched with checkpoints.

Which approach?
