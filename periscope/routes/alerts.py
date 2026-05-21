"""GET /api/alerts/recent — cross-pane alert feed for the dashboard rail.

Walks `_CHANNEL_ALERTS` (the in-memory per-pane alert log written by the
`notify` MCP tool — see periscope.channels) and flattens it into a single
reverse-chronological list with pane metadata attached. The frontend's
right-rail alerts panel polls this; each row links to the originating
pane's modal.

In-memory only — alerts vanish on restart and on pane GC. That's
intentional for v1 (alerts are ephemeral by nature). Promote to SQLite
when the lack of cross-restart history actually bites.
"""

from fastapi import APIRouter, Query

from periscope.channels import _CHANNELS_LOCK, _CHANNEL_ALERTS
from periscope.panes import list_windows

router = APIRouter()


@router.get("/api/alerts/recent")
def alerts_recent(limit: int = Query(100, ge=1, le=500)):
    # Build pane_id → window metadata map from the live tmux state so dead
    # panes drop out automatically (matches _channel_gc's invariant).
    windows = list_windows()
    by_pane: dict[str, dict] = {}
    for w in windows:
        pane_id = w.get("pane_id") or ""
        if pane_id:
            by_pane[pane_id] = w

    with _CHANNELS_LOCK:
        snapshot = {pid: list(rs) for pid, rs in _CHANNEL_ALERTS.items()}

    items: list[dict] = []
    for pane_id, alerts in snapshot.items():
        w = by_pane.get(pane_id)
        if not w:
            continue
        target = f"{w['session']}:{w['index']}"
        for r in alerts:
            items.append({
                "ts": int(r.get("ts") or 0),
                "kind": r.get("kind") or "info",
                "severity": r.get("severity") or "info",
                "message": r.get("message") or "",
                "pane_id": pane_id,
                "target": target,
                "session": w["session"],
                "index": w["index"],
                "name": w["name"],
            })

    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"items": items[:limit]}
