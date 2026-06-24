"""POST /api/command — deliver a free-text command to the hidden commander pane;
GET /api/command/status — whether the commander is still working (drives the
omnibox console's done-detection instead of guessing from transcript timing)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import activity, commander
from periscope.panes import list_windows, parse_pane
from periscope.routes.send import _send_to_target
from periscope.tmux import capture

router = APIRouter()


class CommandBody(BaseModel):
    text: str


def _commander_window():
    """The live tmux window dict for the marked commander pane, or None."""
    marker = activity.get_commander()
    if marker is None:
        return None
    return next((w for w in list_windows() if w.get("pane_id") == marker.pane_id), None)


@router.post("/api/command")
async def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text must be non-empty")
    marker = await commander.ensure_commander()
    if marker is None:
        raise HTTPException(503, "commander unavailable (not prod, or spawn failed)")
    win = _commander_window()
    if win is None:
        raise HTTPException(503, "commander pane not found in tmux")
    target = f"{win['session']}:{win['index']}"
    _send_to_target(target, paste=text, keys=["Enter"])
    return {"session": win["session"], "index": win["index"]}


@router.get("/api/command/status")
def command_status():
    """`running` = the commander pane is actively generating (parse_pane state
    'working'). The console polls this and declares the command done once the
    commander goes idle — robust to the multi-second gaps between its tool calls
    that a transcript-growth timer mistakes for completion."""
    win = _commander_window()
    if win is None:
        return {"alive": False, "running": False}
    parsed = parse_pane(capture(f"{win['session']}:{win['index']}"))
    return {"alive": True, "running": parsed.get("state") == "working"}
