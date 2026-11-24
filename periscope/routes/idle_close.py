"""GET /api/idle-close/preview — the panes that idle close closes at the next 04:00."""

import asyncio

from fastapi import APIRouter

from periscope import idle_close, state_hub, store

router = APIRouter()


@router.get("/api/idle-close/preview")
async def idle_close_preview():
    """[] when the setting is off. The sweep decides again at 04:00."""
    weekdays = store.get_settings().get("idle_close_weekdays")
    if not weekdays:
        return []
    views = await state_hub.current_windows()
    return await asyncio.to_thread(idle_close.preview, views, weekdays)
