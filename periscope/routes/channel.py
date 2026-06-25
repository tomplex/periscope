"""POST /api/channel/{clear-unread,push} — MCP channel control endpoints."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from periscope.channels import (
    _CHANNEL_UNREAD,
    _CHANNELS_LOCK,
    emit_channel_event,
)

router = APIRouter()


@router.post("/api/channel/clear-unread")
def channel_clear_unread(pane_id: str = Query(...)):
    if not pane_id.startswith("%"):
        raise HTTPException(400, "pane_id must be a %N tmux pane id")
    with _CHANNELS_LOCK:
        _CHANNEL_UNREAD[pane_id] = 0
    return {"ok": True}


class PushBody(BaseModel):
    content: str


@router.post("/api/channel/push")
async def channel_push(body: PushBody, pane_id: str = Query(...)):
    # Surfaces in Claude's prompt as a <channel source="periscope"> block.
    # Frontend uses this for the "Push to Claude..." composer and for the
    # Inspector's "+ link pull request" / "+ link Linear ticket" buttons.
    if not pane_id.startswith("%"):
        raise HTTPException(400, "pane_id must be a %N tmux pane id")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content required")
    sent = await emit_channel_event(pane_id, content)
    return {"ok": sent}
