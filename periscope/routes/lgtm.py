"""LGTM bridge routes.

- POST /api/lgtm/start    → register a project with LGTM.
- POST /api/lgtm/add-doc  → register (if needed) + attach a doc path.
- DELETE /api/lgtm/items  → remove an item (document) from a session.

All three proxy through to LGTM and refresh periscope's cache so the
next poll carries the new state. Going through periscope keeps the
browser side same-origin (no CORS preflight against LGTM's port) and
gives us a place to add context the frontend doesn't have (resolving
relative paths against the pane's cwd, find-or-create logic).
"""

import os
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from periscope.lgtm import LGTM_BASE_URL, _lgtm_refresh_all, cached_lgtm_state

router = APIRouter()


class LgtmStartBody(BaseModel):
    cwd: str


@router.post("/api/lgtm/start")
async def lgtm_start(body: LgtmStartBody):
    import httpx
    cwd = os.path.expanduser((body.cwd or "").strip())
    if not cwd or not Path(cwd).is_dir():
        return {"ok": False, "error": "cwd must be an existing directory"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{LGTM_BASE_URL}/projects",
                json={"repoPath": cwd},
            )
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "error": f"lgtm unreachable: {e}"}

    # Refresh the cache now so the response carries the freshly-registered
    # session — the caller can use the URL immediately to mount the iframe
    # rather than waiting for the next periodic refresh tick.
    await _lgtm_refresh_all()
    slug = payload.get("slug")
    return {
        "ok": True,
        "slug": slug,
        "url": f"{LGTM_BASE_URL}/project/{slug}/" if slug else None,
    }


class LgtmAddDocBody(BaseModel):
    cwd: str
    path: str


@router.post("/api/lgtm/add-doc")
async def lgtm_add_doc(body: LgtmAddDocBody):
    """Attach a markdown document to the LGTM session for `cwd`.

    Resolves `path` against `cwd` if it's relative. Finds-or-creates
    the session — auto-creation matches the Cmd+click-from-terminal
    flow where the user expects "just works" semantics.
    """
    import httpx

    cwd = os.path.expanduser((body.cwd or "").strip())
    raw_path = os.path.expanduser((body.path or "").strip())
    if not cwd or not Path(cwd).is_dir():
        return {"ok": False, "error": "invalid cwd"}
    if not raw_path:
        return {"ok": False, "error": "path required"}

    abs_path = raw_path if os.path.isabs(raw_path) else os.path.join(cwd, raw_path)
    abs_path = os.path.normpath(abs_path)
    if not Path(abs_path).is_file():
        return {"ok": False, "error": f"file not found: {abs_path}"}

    existing = cached_lgtm_state(cwd)
    slug = existing.get("slug") if existing else None

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if not slug:
                # Auto-create the session for this repo before adding the doc.
                r = await client.post(
                    f"{LGTM_BASE_URL}/projects",
                    json={"repoPath": cwd},
                )
                r.raise_for_status()
                slug = r.json().get("slug")
                if not slug:
                    return {"ok": False, "error": "lgtm did not return a slug"}

            r = await client.post(
                f"{LGTM_BASE_URL}/project/{slug}/items",
                json={"path": abs_path},
            )
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "error": f"lgtm unreachable: {e}"}

    await _lgtm_refresh_all()
    item_id = payload.get("id")
    return {
        "ok": True,
        "slug": slug,
        "item_id": item_id,
        "tab_id": f"lgtm:{item_id}" if item_id else None,
    }


@router.delete("/api/lgtm/items")
async def lgtm_remove_item(
    slug: str = Query(..., min_length=1),
    item: str = Query(..., min_length=1),
):
    """Remove a single item (document) from an LGTM session.

    LGTM's items_changed SSE will fire on success and trigger a periscope
    cache refresh too — the explicit refresh below is belt-and-suspenders
    for the case where the SSE event arrives after the next /api/state
    poll the caller might already have in flight.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.delete(f"{LGTM_BASE_URL}/project/{slug}/items/{item}")
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "error": f"lgtm unreachable: {e}"}

    if r.status_code == 404:
        return {"ok": False, "error": "item not found"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"lgtm returned {r.status_code}"}

    await _lgtm_refresh_all()
    return {"ok": True}
