"""GET /api/alerts/recent — cross-pane alert feed.

The dashboard no longer polls this: alerts ride the pushed `/api/state` blob
(`channels.recent_alerts`, wired into `routes/state.build_state`), so the feed
shares the state hub's transport and its notify()-driven kick. This endpoint
stays as the standalone REST surface — curl-able for debugging, and
independent of the dashboard's transport.

In-memory only, with a durable mirror in the `events` table that
`rehydrate_alerts_from_events` replays at startup.
"""

from fastapi import APIRouter, Query

from periscope.channels import recent_alerts
from periscope.panes import list_windows

router = APIRouter()


@router.get("/api/alerts/recent")
def alerts_recent(limit: int = Query(100, ge=1, le=500)):
    return {"items": recent_alerts(list_windows(), limit)}
