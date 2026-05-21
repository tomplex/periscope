"""POST /api/channel/{clear-unread,push} — MCP channel control endpoints."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from periscope.channels import (
    _CHANNELS_LOCK,
    _CHANNEL_UNREAD,
    emit_channel_event,
)

router = APIRouter()


@router.post("/api/channel/clear-unread")
def channel_clear_unread(pane: str = Query(...)):
    if not pane.startswith("%"):
        raise HTTPException(400, "pane must be a %N tmux pane id")
    with _CHANNELS_LOCK:
        _CHANNEL_UNREAD[pane] = 0
    return {"ok": True}


class PushBody(BaseModel):
    content: str


@router.post("/api/channel/push")
async def channel_push(body: PushBody, pane: str = Query(...)):
    # Surfaces in Claude's prompt as a <channel source="periscope"> block.
    # Frontend uses this for the "Push to Claude..." composer and for the
    # sidebar's "+ link pull request" / "+ link Linear ticket" buttons.
    if not pane.startswith("%"):
        raise HTTPException(400, "pane must be a %N tmux pane id")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content required")
    sent = await emit_channel_event(pane, content)
    return {"ok": sent}
