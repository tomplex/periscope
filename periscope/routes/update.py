"""GET /api/update/status + POST /api/update — self-update from the dashboard.

POST returns as soon as the detached updater is spawned; it cannot report the
outcome, because a successful update kills this process. The client watches
the reconnect banner and /api/healthz's SHA instead. A FAILED update leaves
this server alive, so the reason is readable from GET /api/update/status.
"""

from fastapi import APIRouter, HTTPException

from periscope import updater

router = APIRouter()


@router.get("/api/update/status")
def update_status():
    return updater.status()


@router.post("/api/update")
def start_update():
    try:
        updater.start()
    except RuntimeError as e:
        # Dev instance, or one already in flight — both are the caller asking
        # for something this instance must not do, not a server fault.
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
