"""POST /api/events — UI instrumentation ingest.

The frontend (static/src/track.js) batches usage events and ships them
here via navigator.sendBeacon (plus a fetch keepalive fallback). This
endpoint is fire-and-forget: it NEVER raises on a malformed batch. It is
not called through apiCall, so a 4xx/5xx would be silently swallowed by
sendBeacon / fetch().catch() anyway, and instrumentation must never
surface an error to the user. A bad body is logged and returns 200 n=0.

The raw request body is parsed by hand rather than via a Pydantic body
model: a model would raise 422 BEFORE the handler runs, so the handler
could not swallow it. dev is the inverse of config.is_prod() (prod port
AND not PERISCOPE_DEV), so a dev instance is flagged even if it lands
on 8765. Real-usage queries filter dev=0.
"""

import json

from fastapi import APIRouter, Request

from periscope import activity, config
from periscope.log import log

router = APIRouter()

_MAX_BATCH = 1000


@router.post("/api/events")
async def post_events(request: Request):
    try:
        raw = await request.body()
        payload = json.loads(raw) if raw else {}
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            events = []
    except (ValueError, TypeError):
        log.warning("POST /api/events: undecodable body, dropping batch")
        return {"ok": True, "n": 0}
    if len(events) > _MAX_BATCH:
        events = events[:_MAX_BATCH]
    n = activity.record_ui_events(events, dev=not config.is_prod())
    return {"ok": True, "n": n}
