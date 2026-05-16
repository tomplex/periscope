"""LGTM (Looks Good To Me) mirror.

Periscope polls localhost:9900 for active code-review sessions and
subscribes per-session SSE streams to keep its in-memory mirror fresh.
The dashboard reads `cached_lgtm_state(cwd)` to surface a review-pane
badge for any pane whose cwd matches a repo under review.

LGTM running is optional; if /api/lgtm/sessions is unreachable, all
helpers return None / empty silently.
"""

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import httpx

from periscope.log import log, _task


LGTM_BASE_URL = os.environ.get("PERISCOPE_LGTM_URL", "http://127.0.0.1:9900")
LGTM_REFRESH_S = 30.0  # full /projects refresh interval; SSE handles in-between deltas

_LGTM_LOCK = threading.Lock()
# repoPath (resolved, absolute) -> session info dict, see _lgtm_refresh_all
_LGTM_BY_REPO: dict[str, dict[str, Any]] = {}
# slug -> running SSE task; reconciled against /projects each refresh
_LGTM_SSE_TASKS: dict[str, asyncio.Task] = {}


def _normalize_repo_path(p: str | None) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).resolve())
    except OSError:
        return p


def cached_lgtm_state(cwd: str | None) -> dict | None:
    """Return the LGTM session info for a pane's cwd, or None.

    Result shape:
        {"slug", "url", "branch", "base_branch", "pr",
         "claude_comments", "user_comments", "submitted"}
    """
    if not cwd:
        return None
    key = _normalize_repo_path(cwd)
    with _LGTM_LOCK:
        entry = _LGTM_BY_REPO.get(key)
        return dict(entry) if entry else None


def _lgtm_submitted(slug: str) -> bool:
    """True if the user has submitted feedback for this session.

    LGTM writes /tmp/claude-review/<slug>.md.signal on every submit.
    Existence of the signal file is the cheapest reliable check; we
    don't read it because periscope doesn't need the round number.
    """
    return Path(f"/tmp/claude-review/{slug}.md.signal").exists()


async def _lgtm_fetch_items(client, slug: str) -> list[dict]:
    """One project's item list. Returns [] on any failure so the caller
    doesn't need to special-case missing items per slug."""
    try:
        r = await client.get(f"{LGTM_BASE_URL}/project/{slug}/items", timeout=2.0)
        if r.status_code != 200:
            return []
        items = r.json().get("items", []) or []
        # Keep only the fields the frontend needs for tab rendering.
        return [
            {"id": i.get("id"), "type": i.get("type"), "title": i.get("title")}
            for i in items if i.get("id")
        ]
    except Exception:
        return []


async def _lgtm_refresh_all() -> None:
    """Pull /projects + per-slug /items, rebuild the cache, reconcile SSE subs."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{LGTM_BASE_URL}/projects")
            r.raise_for_status()
            payload = r.json()
            projects = payload.get("projects", []) or []
            # Fetch each project's items in parallel — keeps refresh latency
            # at one round-trip even with a dozen sessions.
            slugs = [p["slug"] for p in projects if p.get("slug")]
            items_lists = await asyncio.gather(
                *[_lgtm_fetch_items(client, s) for s in slugs]
            )
            items_by_slug = dict(zip(slugs, items_lists))
    except (httpx.HTTPError, OSError):
        # LGTM not running, port closed, etc. Keep the existing cache; the
        # next refresh will fix it. No log — silence on the common path.
        return

    new_map: dict[str, dict[str, Any]] = {}
    seen_slugs: set[str] = set()
    for p in projects:
        slug = p.get("slug")
        repo = _normalize_repo_path(p.get("repoPath"))
        if not slug or not repo:
            continue
        seen_slugs.add(slug)
        new_map[repo] = {
            "slug": slug,
            "url": f"{LGTM_BASE_URL}/project/{slug}/",
            "branch": p.get("branch"),
            "base_branch": p.get("baseBranch"),
            "pr": p.get("pr"),
            "claude_comments": int(p.get("claudeCommentCount") or 0),
            "user_comments": int(p.get("userCommentCount") or 0),
            "submitted": _lgtm_submitted(slug),
            "items": items_by_slug.get(slug, []),
        }
    with _LGTM_LOCK:
        _LGTM_BY_REPO.clear()
        _LGTM_BY_REPO.update(new_map)

    # Reconcile SSE subscriptions: subscribe to new slugs, cancel gone ones.
    for slug in list(_LGTM_SSE_TASKS):
        if slug not in seen_slugs:
            t = _LGTM_SSE_TASKS.pop(slug)
            if not t.done():
                t.cancel()
    for slug in seen_slugs - set(_LGTM_SSE_TASKS):
        _LGTM_SSE_TASKS[slug] = _task(_lgtm_sse_loop(slug), f"lgtm-sse-{slug}")


async def _lgtm_sse_loop(slug: str) -> None:
    """Long-lived SSE subscription that refreshes the cache on every event.

    Any event (comments_changed, items_changed) is treated as "this slug's
    counts changed" — we just rerun /projects rather than diffing the SSE
    payload, since the canonical numbers come from /projects anyway.
    """
    url = f"{LGTM_BASE_URL}/project/{slug}/events"
    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise httpx.HTTPError(f"sse status {r.status_code}")
                    backoff = 1.0
                    async for line in r.aiter_lines():
                        # SSE payloads are "event: ..."/"data: ..." pairs; any
                        # non-empty content means there was a delta worth
                        # refreshing on.
                        if line.startswith("data:"):
                            await _lgtm_refresh_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stream errored / LGTM restarted. Back off and retry.
            pass
        await asyncio.sleep(min(backoff, 30.0))
        backoff *= 2


async def _lgtm_periodic_refresh() -> None:
    """Top-level loop. Catches both 'LGTM just started' and 'new session
    appeared we don't have an SSE for yet'."""
    while True:
        try:
            await _lgtm_refresh_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lgtm refresh failed")
        await asyncio.sleep(LGTM_REFRESH_S)
