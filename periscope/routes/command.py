"""POST /api/command — dispatch a free-text command as a fresh `claude --bg`
commander (a tracked job). GET /api/command/jobs — the job list (newest-first,
status synced from `claude agents`). GET /api/command/jobs/{id}/turns — a job's
transcript from its session JSONL."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import bg_commander, turns
from history.search import messages_from_jsonl

router = APIRouter()


class CommandBody(BaseModel):
    text: str


@router.post("/api/command")
def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text must be non-empty")
    return {"job_id": bg_commander.dispatch(text)}


@router.get("/api/command/jobs")
def command_jobs():
    bg_commander.sync_jobs()      # on-open fresh read (the worker tick also syncs every 30s)
    return [
        {"id": j.id, "text": j.text, "status": j.status, "started_at": j.started_at}
        for j in bg_commander.list_jobs()
    ]


@router.get("/api/command/jobs/{job_id}/turns")
def command_job_turns(job_id: str):
    if bg_commander.get_job(job_id) is None:
        raise HTTPException(404, "unknown job")
    jsonl = turns.jsonl_for_session(job_id)     # session id IS the job id
    if jsonl is None:
        raise HTTPException(404, "no transcript yet")
    return {"session_id": job_id, "messages": messages_from_jsonl(str(jsonl))}
