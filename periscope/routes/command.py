"""POST /api/command — deliver a free-text command to the hidden commander pane."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import commander
from periscope.panes import list_windows
from periscope.routes.send import _send_to_target

router = APIRouter()


class CommandBody(BaseModel):
    text: str


@router.post("/api/command")
async def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text must be non-empty")
    marker = await commander.ensure_commander()
    if marker is None:
        raise HTTPException(503, "commander unavailable (not prod, or spawn failed)")
    win = next((w for w in list_windows() if w.get("pane_id") == marker.pane_id), None)
    if win is None:
        raise HTTPException(503, "commander pane not found in tmux")
    target = f"{win['session']}:{win['index']}"
    _send_to_target(target, paste=text, keys=["Enter"])
    return {"session": win["session"], "index": win["index"]}
