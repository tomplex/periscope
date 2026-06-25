"""Lifespan startup + shutdown wiring + route registration."""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_app_construction_imports_cleanly():
    from periscope.app import app
    assert isinstance(app, FastAPI)


def test_app_includes_state_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/state" in paths


def test_app_includes_prefs_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/prefs" in paths


def test_app_includes_ws_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/ws/pane" in paths


def test_app_mounts_static_files():
    """The catch-all `app.mount("/", StaticFiles(...))` is registered last."""
    from periscope.app import app
    # Last route is the static mount.
    static_mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
    assert len(static_mounts) >= 1


def test_lifespan_starts_and_shuts_down_cleanly(mocker):
    """TestClient triggers lifespan startup on enter, shutdown on exit.

    Mock the heavyweight prewarms + async loops so the test is fast and
    doesn't bind a real unix socket. _lgtm_periodic_refresh and
    _mcp_listener are async functions wrapped via _task(name, coro) — they
    must return a coroutine, so we patch with a coroutine factory rather
    than return_value=None (which would make _task crash on
    asyncio.create_task(None)).
    """
    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_plan_usage")
    # Lifespan runs a synchronous pane_projects backfill before yield; mock it
    # so tests don't shell out to real tmux or write the real periscope.db.
    mocker.patch("periscope.projects.backfill_pane_projects", return_value=0)

    async def _noop():
        return None
    mocker.patch("periscope.app._lgtm_periodic_refresh", side_effect=_noop)
    mocker.patch("periscope.app._mcp_listener", side_effect=_noop)
    # The activity worker's FIRST tick runs immediately on startup — with
    # the real one, every pytest run on this machine executed a live tick
    # against the developer's actual tmux server: real capture-pane, real
    # narrator Haiku calls, and real renames of real windows. PORT defaults
    # to 8765 here, so the prod-only guard does not protect tests.
    mocker.patch("periscope.activity.run_worker", side_effect=_noop)
    # PORT defaults to 8765 (prod), so the lifespan generates the bg-commander
    # MCP config — stub it so the test doesn't write into the real config dir.
    mocker.patch("periscope.bg_commander.write_mcp_config")
    # Lifespan teardown still calls os.unlink(MCP_SOCKET_PATH) regardless
    # of whether the listener was real; without this patch, running pytest
    # while prod periscope is up deletes its live /tmp/periscope-mcp.sock
    # and kills every connected Claude's MCP channel.
    mocker.patch("periscope.app.os.unlink")

    from periscope.app import app
    with TestClient(app) as client:
        r = client.get("/api/state")
        assert r.status_code == 200
    # Regression for the intermittent 3.14 sqlite segfault + usage-bleed flake:
    # building /api/state must NOT kick off the real plan-usage refresh. That
    # background thread does httpx + record_usage_samples and, as a leaked
    # daemon, writes into whatever per-test ACTIVITY_DB is live when it lands —
    # corrupting unrelated tests' usage_samples and racing fresh_activity_db's
    # connection close (use-after-free → segfault). The autouse
    # _no_plan_usage_refresh fixture seeds the cache so no spawn happens.
    import threading
    assert not any(t.name == "plan-usage" for t in threading.enumerate())


def test_lifespan_skips_mcp_on_dev_port(mocker, monkeypatch, caplog):
    """When PORT != 8765, lifespan must not call _mcp_listener and must
    log that it's skipping."""
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8766)

    called = {"count": 0}
    async def fake_listener():
        called["count"] += 1
    mocker.patch("periscope.app._mcp_listener", side_effect=fake_listener)

    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_plan_usage")
    # Lifespan runs a synchronous pane_projects backfill before yield; mock it
    # so tests don't shell out to real tmux or write the real periscope.db.
    mocker.patch("periscope.projects.backfill_pane_projects", return_value=0)
    async def _noop():
        return None
    mocker.patch("periscope.app._lgtm_periodic_refresh", side_effect=_noop)

    from periscope.app import app
    with caplog.at_level("INFO", logger="periscope"), TestClient(app):
        pass

    assert called["count"] == 0
    assert any("skipping MCP listener" in r.message for r in caplog.records)


def test_lifespan_binds_mcp_on_prod_port(mocker, monkeypatch):
    """When PORT == 8765, lifespan calls _mcp_listener exactly once."""
    import periscope.config
    monkeypatch.setattr(periscope.config, "PORT", 8765)

    called = {"count": 0}
    async def fake_listener():
        called["count"] += 1
    mocker.patch("periscope.app._mcp_listener", side_effect=fake_listener)

    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_plan_usage")
    # Lifespan runs a synchronous pane_projects backfill before yield; mock it
    # so tests don't shell out to real tmux or write the real periscope.db.
    mocker.patch("periscope.projects.backfill_pane_projects", return_value=0)
    async def _noop():
        return None
    mocker.patch("periscope.app._lgtm_periodic_refresh", side_effect=_noop)
    # See test_lifespan_starts_and_shuts_down_cleanly: the real worker
    # fires a live tick against the developer's tmux on every test run.
    mocker.patch("periscope.activity.run_worker", side_effect=_noop)
    # Teardown unlinks MCP_SOCKET_PATH — no-op so we don't touch /tmp.
    mocker.patch("os.unlink")

    # The prod-gated lifespan generates the bg-commander MCP config; stub it
    # so the test doesn't write into the real config dir.
    mocker.patch("periscope.bg_commander.write_mcp_config")

    from periscope.app import app
    with TestClient(app):
        pass

    assert called["count"] == 1
