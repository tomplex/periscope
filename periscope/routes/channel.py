"""POST /api/channel/clear-unread — mark an MCP channel as read."""

from fastapi import APIRouter, Query

from periscope.channels import _CHANNELS_LOCK, _CHANNEL_UNREAD

router = APIRouter()


@router.post("/api/channel/clear-unread")
def channel_clear_unread(pane: str = Query(...)):
    if not pane.startswith("%"):
        return {"ok": False, "error": "pane must be a %N tmux pane id"}
    with _CHANNELS_LOCK:
        _CHANNEL_UNREAD[pane] = 0
    return {"ok": True}
