# Periscope Stability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the launchd auto-respawn / dev-prod port split / frontend reconnect work specified in `docs/superpowers/specs/2026-05-15-periscope-stability-design.md`. Phase 0 (logging, `_bg`/`_task`, pidfile reclaim, `PERISCOPE_DEV` gating) is already on `main`; this plan covers Phase 1+.

**Architecture:** Port-scoped pidfile/log paths + a `PORT` constant in `periscope/config.py` enable two coexisting periscope processes on 8765 (prod) and 8766 (dev). The MCP unix socket is bound only by `PORT == 8765`. launchd manages prod via `KeepAlive=true`. The browser dashboard tracks consecutive `/api/state` poll failures and shows a banner.

**Tech Stack:** Python 3.11+ / FastAPI / uvicorn, pytest + pytest-mock, vanilla JS frontend, launchd (macOS), `uv run server.py`.

---

## File Structure

**New files:**

- `periscope/routes/healthz.py` — `GET /api/healthz` returning `{ok, pid, port, uptime_s, version}`
- `tests/routes/test_healthz.py` — route test
- `com.tom.periscope.plist` — launchd plist (lives at repo root, copied to `~/Library/LaunchAgents/` by `bin/periscope install`)
- `bin/periscope` — helper for `install/uninstall/start/stop/restart/status/tail`

**Modified files:**

- `periscope/config.py` — add `PORT` constant
- `periscope/log.py` — port-scoped `_log_path()`
- `periscope/pidfile.py` — port-scoped path, `(pid, port)` storage, port-aware reclaim, `PERISCOPE_NO_RECLAIM` honored from server.py
- `periscope/app.py` — gate MCP listener bind + teardown on `PORT == 8765`
- `periscope/routes/__init__.py` (if it has an explicit list) — include `healthz` (currently imports are via the `app.py` tuple, no change needed there)
- `server.py` — pass `PORT` to `uvicorn.run`, honor `PERISCOPE_NO_RECLAIM`
- `static/index.html` — add `<div id="connection-banner">` as first child of `<body>`
- `static/styles.css` — banner + disconnected-state styles
- `static/grid.js` — track consecutive poll failures, toggle banner
- `CLAUDE.md` — worktree workflow section
- `README.md` — one-line pointer to the workflow section
- `tests/test_config.py`, `tests/test_log.py`, `tests/test_pidfile.py`, `tests/test_app.py` — coverage for the above

---

## Task 1: `PORT` constant in `periscope/config.py`

**Files:**
- Modify: `periscope/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_PORT_defaults_to_8765(monkeypatch):
    monkeypatch.delenv("PERISCOPE_PORT", raising=False)
    # Reimport to re-evaluate the module-level PORT.
    import importlib
    import periscope.config
    importlib.reload(periscope.config)
    assert periscope.config.PORT == 8765


def test_PORT_reads_PERISCOPE_PORT_env(monkeypatch):
    monkeypatch.setenv("PERISCOPE_PORT", "8766")
    import importlib
    import periscope.config
    importlib.reload(periscope.config)
    assert periscope.config.PORT == 8766
```

- [ ] **Step 2: Run test to verify it fails**

`uv run pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: module 'periscope.config' has no attribute 'PORT'`.

- [ ] **Step 3: Add `PORT` to `periscope/config.py`**

Append after the existing constants:

```python
import os

# Port the FastAPI server binds. Default 8765 = "prod" (launchd-managed).
# Override via PERISCOPE_PORT=8766 for a dev instance running alongside prod.
# Read once at module load — server.py invokes load_dotenv before importing
# anything else, so .env is honored. Module reload in tests is the normal
# way to re-evaluate this for env permutations.
PORT = int(os.environ.get("PERISCOPE_PORT", "8765"))
```

- [ ] **Step 4: Run test to verify it passes**

`uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/config.py tests/test_config.py
git commit -m "config: add PORT constant read from PERISCOPE_PORT env, default 8765"
```

---

## Task 2: Port-scoped log path

**Files:**
- Modify: `periscope/log.py`
- Test: `tests/test_log.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_log.py`:

```python
def test_log_path_includes_port(tmp_xdg_home, monkeypatch):
    import periscope.config
    import periscope.log
    monkeypatch.setattr(periscope.config, "PORT", 8766)
    # _log_path is the helper; the cached _LOG_PATH is set at module load
    # before our monkeypatch and isn't checked here.
    assert periscope.log._log_path().name == "periscope-8766.log"
```

- [ ] **Step 2: Run test to verify it fails**

`uv run pytest tests/test_log.py -v -k log_path`
Expected: FAIL — current path is `periscope.log` (no port).

- [ ] **Step 3: Update `periscope/log.py`**

Change the `_log_path` function and the import block. Replace:

```python
def _log_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "periscope.log"
```

with:

```python
from periscope import config


def _log_path() -> Path:
    # Read config.PORT via module attribute (not a `from periscope.config
    # import PORT` snapshot) so tests that monkeypatch config.PORT see the
    # new value here.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / f"periscope-{config.PORT}.log"
```

Note: `_LOG_PATH = _log_path()` at module load reads `config.PORT` at the time of the first `periscope.log` import. Tests assert against the function, not the cached `_LOG_PATH`.

- [ ] **Step 4: Run tests**

`uv run pytest tests/test_log.py -v`
Expected: PASS for the new tests; existing tests in this file should also pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/log.py tests/test_log.py
git commit -m "log: port-scope the log filename (periscope-{PORT}.log)"
```

---

## Task 3: Port-scoped pidfile path + `(pid, port)` storage

**Files:**
- Modify: `periscope/pidfile.py`
- Test: `tests/test_pidfile.py`

- [ ] **Step 1: Write the failing tests**

Update the existing `test_pidfile_path_under_xdg` and add new ones:

```python
def test_pidfile_path_under_xdg(tmp_xdg_home: Path, monkeypatch):
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)
    assert _pidfile_path() == tmp_xdg_home / "periscope" / "periscope-8765.pid"


def test_pidfile_path_uses_dev_port(tmp_xdg_home: Path, monkeypatch):
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8766)
    assert _pidfile_path() == tmp_xdg_home / "periscope" / "periscope-8766.pid"


def test_pidfile_stores_pid_and_port(tmp_xdg_home: Path, monkeypatch):
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)
    _write_pidfile()
    contents = _pidfile_path().read_text()
    assert contents.strip().split("\n") == [str(os.getpid()), "8765"]
```

The existing `test_write_then_remove_pidfile` and `test_remove_pidfile_ignores_other_owners` need to be updated for the new file format. Replace:

```python
def test_write_then_remove_pidfile(tmp_xdg_home: Path, monkeypatch):
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)
    _write_pidfile()
    path = _pidfile_path()
    assert path.is_file()
    pid_line, port_line = path.read_text().strip().split("\n")
    assert pid_line == str(os.getpid())
    assert port_line == "8765"
    _remove_pidfile()
    assert not path.exists()


def test_remove_pidfile_ignores_other_owners(tmp_xdg_home: Path, monkeypatch):
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999\n8765\n")
    _remove_pidfile()
    assert path.exists(), "must not delete a pidfile we don't own"
    assert path.read_text().startswith("99999")
```

- [ ] **Step 2: Run tests to verify failures**

`uv run pytest tests/test_pidfile.py -v`
Expected: FAIL on the path tests (no port suffix yet) and the `(pid, port)` tests (write only stores pid).

- [ ] **Step 3: Update `periscope/pidfile.py`**

Add `from periscope import config` to the imports (alongside the existing `from periscope.log import log`). Replace `_pidfile_path`, `_write_pidfile`, `_remove_pidfile`:

```python
def _pidfile_path() -> Path:
    # config.PORT accessed via module attribute (not snapshot import) so
    # tests can monkeypatch periscope.config.PORT and observe new paths.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / f"periscope-{config.PORT}.pid"


def _write_pidfile() -> None:
    """Pidfile format: two lines, '{pid}\\n{port}\\n'. Storing the port lets
    a reclaim verify it's killing the right port's prior instance even if a
    stale pidfile ever got pointed at a foreign process."""
    path = _pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n{config.PORT}\n")


def _remove_pidfile() -> None:
    """Only remove if the file's pid line matches our own pid."""
    path = _pidfile_path()
    try:
        first_line = path.read_text().split("\n", 1)[0].strip()
        if first_line == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

`uv run pytest tests/test_pidfile.py -v -k "path or stores or write_then_remove or ignores_other"`
Expected: PASS for these four tests. Other pidfile tests (reclaim) are still on the old format and will be updated in Task 4.

- [ ] **Step 5: Commit**

```bash
git add periscope/pidfile.py tests/test_pidfile.py
git commit -m "pidfile: port-scoped path + store (pid, port) so reclaim can verify port"
```

---

## Task 4: Port-aware reclaim

**Files:**
- Modify: `periscope/pidfile.py`
- Test: `tests/test_pidfile.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pidfile.py`:

```python
def test_reclaim_refuses_when_recorded_port_mismatches(
    tmp_xdg_home: Path, monkeypatch, mocker, caplog
):
    """A pidfile that records a port different from current PORT must not
    trigger SIGTERM — the recorded pid belongs to a different periscope."""
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8766)
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999\n8765\n")  # foreign port

    mocker.patch("periscope.pidfile._pid_is_periscope", return_value=True)
    killed = mocker.patch("os.kill")

    with caplog.at_level("WARNING", logger="periscope"):
        _reclaim_existing_instance()

    killed.assert_not_called()
    assert any("port mismatch" in r.message for r in caplog.records)


def test_reclaim_legacy_pidfile_with_no_port_line(tmp_xdg_home: Path, mocker):
    """A pidfile from before this change has just a pid, no port line. Treat
    as legacy: accept it and proceed with reclaim."""
    import periscope.config
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999")  # legacy format, no port

    is_per = mocker.patch("periscope.pidfile._pid_is_periscope")
    is_per.side_effect = [True, False]
    killed = mocker.patch("os.kill")

    _reclaim_existing_instance()

    import signal as _signal
    killed.assert_any_call(99999, _signal.SIGTERM)
```

Also: the existing `test_reclaim_signals_live_periscope` and `test_reclaim_escalates_to_sigkill_after_3s` need their `path.write_text("99999")` updated to `path.write_text("99999\n8765\n")` (or set PORT to match). Update them in this step too.

- [ ] **Step 2: Run tests to verify failures**

`uv run pytest tests/test_pidfile.py -v -k reclaim`
Expected: the new `port_mismatches` test FAILS (no port-check logic yet); legacy test PASSES (current parser handles bare pid).

- [ ] **Step 3: Update `_reclaim_existing_instance` in `periscope/pidfile.py`**

Replace the function:

```python
def _reclaim_existing_instance() -> None:
    """If the pidfile points at a live periscope on the same port, SIGTERM
    it (escalate to SIGKILL after 3s) so we can bind the port cleanly.

    Refuses to act when the pidfile's recorded port differs from the
    current PORT — that means the pidfile belongs to a different periscope
    (theoretically impossible given per-port pidfile paths, but a stale
    file from a recycled pid is the edge case we're guarding against).
    """
    path = _pidfile_path()
    try:
        text = path.read_text().strip()
    except OSError:
        return
    lines = text.split("\n")
    try:
        prev = int(lines[0])
    except (ValueError, IndexError):
        return
    if len(lines) >= 2:
        try:
            recorded_port = int(lines[1])
        except ValueError:
            recorded_port = None
        if recorded_port is not None and recorded_port != config.PORT:
            log.warning(
                "pidfile %s has port %d, expected %d — refusing reclaim",
                path, recorded_port, config.PORT,
            )
            return
    # else: legacy pidfile (no port line) — fall through and reclaim.
    if prev == os.getpid() or not _pid_is_periscope(prev):
        return
    log.info("reclaiming previous periscope instance pid=%d", prev)
    try:
        os.kill(prev, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_is_periscope(prev):
            return
        time.sleep(0.1)
    log.warning("pid=%d ignored SIGTERM; sending SIGKILL", prev)
    try:
        os.kill(prev, signal.SIGKILL)
    except ProcessLookupError:
        pass
```

- [ ] **Step 4: Run tests**

`uv run pytest tests/test_pidfile.py -v`
Expected: all tests PASS, including the new mismatch test and the updated existing reclaim tests.

- [ ] **Step 5: Commit**

```bash
git add periscope/pidfile.py tests/test_pidfile.py
git commit -m "pidfile: reclaim refuses if recorded port disagrees with current PORT"
```

---

## Task 5: server.py uses `config.PORT` for `uvicorn.run`

(Reordered to land *before* the `PERISCOPE_NO_RECLAIM` change so subsequent smoke tests can bind a non-default port.)

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Update the uvicorn.run call**

In `server.py`, replace:

```python
    uvicorn.run(
        "periscope.app:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
```

with:

```python
    from periscope.config import PORT
    uvicorn.run(
        "periscope.app:app",
        host="127.0.0.1",
        port=PORT,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
```

- [ ] **Step 2: Smoke-test on the dev port**

`PERISCOPE_PORT=8766 uv run server.py &`
Wait 2 seconds, then `curl -s http://127.0.0.1:8766/api/state | head -c 100`.
Expected: JSON output. Then `kill %1`.

If a previous periscope on 8765 is running, this won't collide (different port). The pidfile reclaim still applies for *this* port — if a previous run on 8766 left a pidfile, it'll be reclaimed; that's expected.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "server.py: pass PORT to uvicorn.run for dev/prod port separation"
```

---

## Task 6: `PERISCOPE_NO_RECLAIM` env var in server.py

**Files:**
- Modify: `server.py`

No test — `server.py` is a thin shim that pytest doesn't import (it has `if __name__ == "__main__":` gating). Verification is by reading the diff + smoke test.

- [ ] **Step 1: Update `server.py` to honor `PERISCOPE_NO_RECLAIM`**

Replace the block:

```python
    _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
```

with:

```python
    # PERISCOPE_NO_RECLAIM=1 disables the SIGTERM-the-previous-instance
    # step. Use this when intentionally running a second periscope (e.g.
    # for debugging a launchd-managed prod, or running a dev instance on
    # the same port as prod temporarily). See spec §"Opt-out flag for
    # reclaim."
    if os.environ.get("PERISCOPE_NO_RECLAIM") != "1":
        _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
```

- [ ] **Step 2: Smoke-test by running the script**

`PERISCOPE_NO_RECLAIM=1 PERISCOPE_PORT=8799 uv run server.py &`
Wait 2 seconds, then `curl -s http://127.0.0.1:8799/api/state | head -c 200`.
Expected: JSON response. Then `kill %1` to clean up.

Confirm `~/.config/periscope/periscope-8799.pid` was written and removed.

Reclaim-skip check: before starting, manually write a stale pidfile pointing at a fake process: `echo "99999\n8799" > ~/.config/periscope/periscope-8799.pid`. Then run with `PERISCOPE_NO_RECLAIM=1`. Confirm no SIGTERM noise in the log (search for "reclaiming"). Then re-run without the flag and confirm the reclaim line *does* appear.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "server.py: honor PERISCOPE_NO_RECLAIM=1 to skip pidfile reclaim"
```

---

## Task 7: Gate MCP listener on `PORT == 8765`

**Files:**
- Modify: `periscope/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py` (follows the existing TestClient-driven lifespan pattern used in `test_lifespan_starts_and_shuts_down_cleanly`; no pytest-asyncio needed):

```python
def test_lifespan_skips_mcp_on_dev_port(mocker, monkeypatch, caplog):
    """When PORT != 8765, lifespan must not call _mcp_listener and must
    log that it's skipping."""
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8766)

    called = {"count": 0}
    async def fake_listener():
        called["count"] += 1
    mocker.patch("periscope.app._mcp_listener", fake_listener)

    # No-op the heavyweight prewarms / LGTM loop.
    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_scraped_usage")
    mocker.patch("periscope.app.kill_orphan_usage_sessions")
    async def fake_lgtm():
        pass
    mocker.patch("periscope.app._lgtm_periodic_refresh", fake_lgtm)

    from periscope.app import app
    with caplog.at_level("INFO", logger="periscope"):
        with TestClient(app):
            pass

    assert called["count"] == 0
    assert any("skipping MCP listener" in r.message for r in caplog.records)


def test_lifespan_binds_mcp_on_prod_port(mocker, monkeypatch):
    """When PORT == 8765, lifespan must call _mcp_listener exactly once."""
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)

    called = {"count": 0}
    async def fake_listener():
        called["count"] += 1
    mocker.patch("periscope.app._mcp_listener", fake_listener)

    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_scraped_usage")
    mocker.patch("periscope.app.kill_orphan_usage_sessions")
    async def fake_lgtm():
        pass
    mocker.patch("periscope.app._lgtm_periodic_refresh", fake_lgtm)
    # Teardown unlinks MCP_SOCKET_PATH — no-op so we don't touch /tmp.
    mocker.patch("os.unlink")

    from periscope.app import app
    with TestClient(app):
        pass

    assert called["count"] == 1
```

- [ ] **Step 2: Run tests to verify failures**

`uv run pytest tests/test_app.py -v -k mcp`
Expected: `test_lifespan_skips_mcp_on_dev_port` FAILS (lifespan currently always calls `_mcp_listener`); `test_lifespan_binds_mcp_on_prod_port` PASSES (default behavior).

- [ ] **Step 3: Update `periscope/app.py`**

Replace the lifespan body with the port-gated version:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    from periscope.config import PORT
    log.info("periscope starting (pid=%d, port=%d)", os.getpid(), PORT)
    kill_orphan_usage_sessions()
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_scraped_usage)
    # MCP listener bound only by the :8765 instance. Channel shims hard-
    # code that path, so prod always serves channels; dev (any other port)
    # leaves them alone.
    if PORT == 8765:
        mcp_task = _task(_mcp_listener(), "mcp-listener")
    else:
        mcp_task = None
        log.info("dev port %d: skipping MCP listener", PORT)
    lgtm_task = _task(_lgtm_periodic_refresh(), "lgtm-refresh")
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        if mcp_task is not None:
            mcp_task.cancel()
        lgtm_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        if mcp_task is not None:
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                os.unlink(MCP_SOCKET_PATH)
            except FileNotFoundError:
                pass
```

- [ ] **Step 4: Run tests**

`uv run pytest tests/test_app.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/app.py tests/test_app.py
git commit -m "app: gate MCP listener bind+teardown on PORT==8765 (dev skips it)"
```

---

## Task 8: `/api/healthz` route

**Files:**
- Create: `periscope/routes/healthz.py`
- Create: `tests/routes/test_healthz.py`
- Modify: `periscope/app.py` (include new router)

- [ ] **Step 1: Write the failing test**

Create `tests/routes/test_healthz.py`:

```python
"""GET /api/healthz returns liveness + version metadata."""

from fastapi.testclient import TestClient

from periscope.app import app


def test_healthz_returns_ok_with_metadata():
    client = TestClient(app)
    res = client.get("/api/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert isinstance(data["port"], int)
    assert isinstance(data["uptime_s"], (int, float)) and data["uptime_s"] >= 0
    assert isinstance(data["version"], str) and data["version"]
```

- [ ] **Step 2: Run test to verify it fails**

`uv run pytest tests/routes/test_healthz.py -v`
Expected: FAIL with 404 (route doesn't exist yet).

- [ ] **Step 3: Create `periscope/routes/healthz.py`**

```python
"""GET /api/healthz — liveness probe with metadata.

Returns pid, port, uptime, and git short-SHA. Used as a quick "is this
periscope alive and which version" check from `bin/periscope status`
and as a future frontend reconnect probe.
"""

import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter

from periscope.config import PORT

router = APIRouter()


def _git_short_sha() -> str:
    """Captured at module load. Falls back to 'unknown' if git isn't on
    PATH or the working tree isn't a git repo — neither should happen in
    practice but the launchd PATH is minimal."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent.parent,
            capture_output=True, text=True, timeout=2.0,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"


_BOOT_TS = time.time()
_VERSION = _git_short_sha()


@router.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "pid": os.getpid(),
        "port": PORT,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
        "version": _VERSION,
    }
```

- [ ] **Step 4: Mount the route in `periscope/app.py`**

In the import block:

```python
from periscope.routes import (
    auto_rename, channel, healthz, history, pane, paste_image, prefs, send,
    sessions, state, ws,
)
```

In the `for r in (...)` tuple, add `healthz`:

```python
for r in (
    auto_rename, channel, healthz, history, lgtm_route, pane, paste_image,
    prefs, send, sessions, state, ws,
):
    app.include_router(r.router)
```

- [ ] **Step 5: Run test**

`uv run pytest tests/routes/test_healthz.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/healthz.py periscope/app.py tests/routes/test_healthz.py
git commit -m "routes/healthz: add /api/healthz with pid/port/uptime/version"
```

---

## Task 9: launchd plist

**Files:**
- Create: `com.tom.periscope.plist` (repo root)

No automated tests — verified by `launchctl bootstrap` in Task 11.

- [ ] **Step 1: Create the plist**

Write `com.tom.periscope.plist` at the repo root:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tom.periscope</string>
  <key>WorkingDirectory</key>
  <string>/Users/tom/dev/periscope</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/tom/.local/bin/uv</string>
    <string>run</string>
    <string>server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/tom/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>/Users/tom</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>/Users/tom/.config/periscope/launchd-stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/tom/.config/periscope/launchd-stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Validate the plist**

`plutil -lint com.tom.periscope.plist`
Expected: `com.tom.periscope.plist: OK`.

- [ ] **Step 3: Commit**

```bash
git add com.tom.periscope.plist
git commit -m "launchd: add com.tom.periscope.plist with KeepAlive auto-respawn"
```

---

## Task 10: `bin/periscope` helper

**Files:**
- Create: `bin/periscope`

- [ ] **Step 1: Create the script**

Make the directory and write the file:

```bash
mkdir -p bin
```

Then create `bin/periscope`:

```sh
#!/bin/sh
# Helper for managing the launchd-managed periscope service. See
# docs/superpowers/specs/2026-05-15-periscope-stability-design.md.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO/com.tom.periscope.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.tom.periscope.plist"
LABEL="gui/$(id -u)/com.tom.periscope"
LOG="$HOME/.config/periscope/periscope-8765.log"

case "$1" in
  install)
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    ;;
  uninstall)
    launchctl bootout "$LABEL" 2>/dev/null
    rm -f "$PLIST_DST"
    ;;
  start)
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    ;;
  stop)
    launchctl bootout "$LABEL"
    ;;
  restart)
    launchctl kickstart -k "$LABEL"
    ;;
  status)
    launchctl print "$LABEL" 2>/dev/null | grep -E 'state|pid' || echo "not loaded"
    ;;
  tail)
    tail -F "$LOG"
    ;;
  *)
    echo "usage: $0 {install|uninstall|start|stop|restart|status|tail}"
    exit 1
    ;;
esac
```

- [ ] **Step 2: Make it executable**

`chmod +x bin/periscope`

- [ ] **Step 3: Smoke-test usage**

`./bin/periscope`
Expected: prints `usage: ./bin/periscope {install|uninstall|start|stop|restart|status|tail}` and exits 1.

`./bin/periscope status`
Expected: `not loaded` (since we haven't installed yet).

- [ ] **Step 4: Commit**

```bash
git add bin/periscope
git commit -m "bin/periscope: launchctl wrapper for install/start/stop/restart/status/tail"
```

---

## Task 11: Frontend banner element + CSS

**Files:**
- Modify: `static/index.html`
- Modify: `static/styles.css`

- [ ] **Step 1: Add the banner element**

In `static/index.html`, find the opening `<body>` tag (probably `<body class="..."` or similar). Insert immediately after it, before `<header class="periscope-header">`:

```html
<div id="connection-banner" hidden>
  <span>⚠ disconnected from periscope — retrying…</span>
</div>
```

- [ ] **Step 2: Add the CSS**

Append to `static/styles.css`:

```css
/* Connection-loss banner. Visible when /api/state has failed >= 2 times
   in a row. In normal document flow (not position:fixed) so the header
   beneath naturally shifts down when it shows. */
#connection-banner {
  background: #5a4a10;
  color: #f5e9c0;
  padding: 6px 12px;
  text-align: center;
  font-size: 13px;
  font-weight: 500;
}

body.disconnected .grid {
  opacity: 0.6;
  transition: opacity 0.2s;
}
```

- [ ] **Step 3: Manual browser smoke-test**

Open the dashboard. Banner is hidden by default. In DevTools console, run:

```js
document.getElementById("connection-banner").hidden = false;
document.body.classList.add("disconnected");
```

Expected: yellow strip appears at the top, header shifts down, grid fades to 60% opacity.

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/styles.css
git commit -m "frontend: banner element + disconnected-state CSS for poll-fail signal"
```

---

## Task 12: Poll-failure tracking in `grid.js`

**Files:**
- Modify: `static/grid.js`

- [ ] **Step 1: Update the `poll()` function**

In `static/grid.js`, find the existing `export async function poll()`. Replace with:

```js
let consecutivePollFails = 0;
const bannerEl = document.getElementById("connection-banner");

export async function poll() {
  if (state.editingTarget) return;  // user is mid-rename; don't blow away their input
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.lastWindows = data.windows;
    render(state.lastWindows);
    updateUsagePill(data.usage_scrape, data.usage);
    lastUpdate.textContent = `updated ${new Date().toLocaleTimeString()}`;
    if (consecutivePollFails > 0) {
      consecutivePollFails = 0;
      if (bannerEl) bannerEl.hidden = true;
      document.body.classList.remove("disconnected");
    }
  } catch (e) {
    consecutivePollFails += 1;
    // Threshold of 2 avoids false-positive flicker on transient hiccups
    // (laptop sleep, single throttled fetch, unrelated 500).
    if (consecutivePollFails >= 2) {
      if (bannerEl) bannerEl.hidden = false;
      document.body.classList.add("disconnected");
    }
    lastUpdate.textContent = `poll failed: ${e.message}`;
  }
}
```

Note: declare `consecutivePollFails` and `bannerEl` at module scope, near the other top-level state (or just above the function).

- [ ] **Step 2: Manual browser test**

Open the dashboard. With prod running, banner stays hidden.

Stop prod: `bin/periscope stop` (after Task 13). Within ~6s (two failed 3s polls), banner appears. Restart prod: `bin/periscope start`. Within ~3s of recovery, banner disappears and grid resumes updating.

(If `bin/periscope install` hasn't been run yet, simulate by `pkill -f "server.py"` and then `uv run server.py` to bring it back.)

- [ ] **Step 3: Commit**

```bash
git add static/grid.js
git commit -m "grid.js: track consecutive poll fails, show banner after 2 in a row"
```

---

## Task 13: Install launchd + integration smoke

**Files:** none — this is a deployment step + verification.

- [ ] **Step 1: Stop any running periscope**

```bash
pkill -f "uv run server.py" || true
sleep 1
```

Confirm port 8765 is free: `lsof -nP -iTCP:8765 | grep LISTEN`
Expected: no output.

- [ ] **Step 2: Install via the helper**

```bash
./bin/periscope install
sleep 2
./bin/periscope status
```

Expected: `state = running` and a pid printed.

`curl -s http://127.0.0.1:8765/api/healthz`
Expected: JSON with `"ok": true`, `"port": 8765`, real pid, version SHA.

- [ ] **Step 3: Crash-respawn test**

`PID=$(curl -s http://127.0.0.1:8765/api/healthz | python3 -c "import sys, json; print(json.load(sys.stdin)['pid'])")`
`kill -SEGV $PID`
`sleep 6`  (covers ThrottleInterval=5)
`curl -s http://127.0.0.1:8765/api/healthz`
Expected: JSON with a *different* pid, confirming launchd respawned.

- [ ] **Step 4: Dev-coexistence test**

Start a dev instance on 8766 in another shell:

```bash
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
```

In yet another shell:

```bash
curl -s http://127.0.0.1:8765/api/healthz | python3 -c "import sys, json; d=json.load(sys.stdin); print('prod', d['pid'], d['port'])"
curl -s http://127.0.0.1:8766/api/healthz | python3 -c "import sys, json; d=json.load(sys.stdin); print('dev',  d['pid'], d['port'])"
lsof -nP -iTCP:8765,8766 | grep LISTEN
lsof /tmp/periscope-mcp.sock
```

Expected: two distinct pids; one on each port; `/tmp/periscope-mcp.sock` is bound by the :8765 process only. The dev periscope's startup log line (`tail -f ~/.config/periscope/periscope-8766.log`) shows `dev port 8766: skipping MCP listener`.

Then stop dev with `Ctrl+C` and confirm `~/.config/periscope/periscope-8766.pid` is removed.

- [ ] **Step 5: Frontend reconnect test**

Open http://127.0.0.1:8765/ in the browser. Run `./bin/periscope restart`. Within ~6s, banner appears; within ~3s of recovery, banner disappears. Confirmed visually.

- [ ] **Step 6: Commit a verification note (no code change)**

Nothing to commit unless something needed adjusting. If a config tweak was needed (e.g., a permission issue), commit that and re-run the failing test.

---

## Task 14: Worktree workflow doc + README pointer

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Append the workflow section to `CLAUDE.md`**

Append at the end of `/Users/tom/dev/periscope/CLAUDE.md`:

```markdown
## Development workflow

Periscope runs in two flavors:

- **Prod** — launchd-managed (`com.tom.periscope`), port 8765, runs from
  this repo's `main` branch. Never edit files in the prod working tree;
  launchd respawns on crash and picks up changes on next restart whether
  intended or not. Manage with `bin/periscope {start|stop|restart|status|tail}`.

- **Dev** — manually started in a git worktree, port 8766. This is where
  edits and iteration happen. Browse it at http://localhost:8766/. Dev
  doesn't bind the MCP socket — Claude's channels always talk to prod
  on 8765.

Standard loop for a periscope change:

    # one-time per feature
    git worktree add ../periscope-feature -b feature/my-change
    cd ../periscope-feature
    PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
    # edit, test at http://localhost:8766/

    # when done
    cd ~/dev/periscope
    git merge feature/my-change
    bin/periscope restart       # launchd respawns prod with new code
    git worktree remove ../periscope-feature

`PERISCOPE_NO_RECLAIM=1` skips the pidfile-reclaim step in `__main__`. Set
it when intentionally running a second instance that shouldn't kill the
existing one (rare; debug only).
```

- [ ] **Step 2: Add a one-liner to `README.md`**

Read the current README header. Find a logical place near the top (after the project description / "Running" section). Add:

```markdown
## Development

See [CLAUDE.md → Development workflow](./CLAUDE.md#development-workflow)
for the prod/dev port split and `bin/periscope` management commands.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: worktree workflow + bin/periscope quickref"
```

---

## Self-Review Notes

After completing all tasks, run the full test suite once:

```bash
uv run pytest -x
```

Expected: all tests pass. If any fail, the per-task tests had the right shape and a later task probably broke an assumption — fix before declaring done.

Then verify the integration once more end-to-end:

```bash
./bin/periscope status   # should show running
curl -s http://127.0.0.1:8765/api/healthz   # ok, port 8765, version sha
```

## Notes on parallelization

Tasks 1-6 are strictly sequential (each depends on the previous file change). Tasks 7-8 are independent of 9-10. Tasks 11-12 (frontend) are independent of 13 (launchd install). A subagent-driven execution can fan out Tasks 7-8 alongside 9-10, and 11-12 alongside 13.
