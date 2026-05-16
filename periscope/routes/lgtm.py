"""POST /api/lgtm/start — register a project with LGTM from the dashboard.

Lets the modal's Review tab register a project with LGTM without going
through Claude. We just POST to LGTM's /projects with the pane's cwd
and trigger an immediate cache refresh so the next /api/state poll
carries the new session info.
"""

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from periscope.lgtm import LGTM_BASE_URL, _lgtm_refresh_all

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
