"""Settings endpoints: GET + PATCH the persisted settings block."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.store import get_accounts, get_settings, update_settings

router = APIRouter()


_VALID_LAYOUTS = ("sibling", "inline")


@router.get("/api/settings")
def settings_get():
    return {"settings": get_settings()}


class SettingsPatch(BaseModel):
    # All fields optional. Pydantic's model_fields_set distinguishes
    # "not sent" from "sent as null"; null at the top level clears.
    worktree_layout_default: str | None = None
    worktree_layout_overrides: dict[str, str] | None = None
    cleanup_idle_days: int | None = None
    bg_account: str | None = None


@router.patch("/api/settings")
def settings_patch(body: SettingsPatch):
    sent = body.model_fields_set
    patch: dict = {}

    if "worktree_layout_default" in sent:
        v = body.worktree_layout_default
        if v is None or v in _VALID_LAYOUTS:
            patch["worktree_layout_default"] = v
        else:
            raise HTTPException(400, f"worktree_layout_default must be one of {_VALID_LAYOUTS}")

    if "worktree_layout_overrides" in sent:
        overrides = body.worktree_layout_overrides
        if overrides is not None:
            # Validate values + normalize keys to realpath. `_resolve_layout`
            # looks up by realpath, so accepting a non-realpath'd key from
            # the modal would mean the override silently never matches.
            # Normalize on the way in.
            normalized: dict[str, str] = {}
            for repo, layout in overrides.items():
                if layout not in _VALID_LAYOUTS:
                    raise HTTPException(
                        400, f"worktree_layout_overrides[{repo!r}] must be one of {_VALID_LAYOUTS}",
                    )
                normalized[os.path.realpath(repo)] = layout
            patch["worktree_layout_overrides"] = normalized
        else:
            patch["worktree_layout_overrides"] = None

    if "cleanup_idle_days" in sent:
        v = body.cleanup_idle_days
        if v is None or (isinstance(v, int) and v > 0):
            patch["cleanup_idle_days"] = v
        else:
            raise HTTPException(400, "cleanup_idle_days must be a positive integer")

    if "bg_account" in sent:
        v = body.bg_account
        # Reject an unknown id rather than letting account_config_dir fail open
        # to the default: at the write boundary a typo'd id is a user error worth
        # reporting, and silently billing the wrong account is the one outcome
        # this whole feature exists to prevent.
        if v is None or any(a.get("id") == v for a in get_accounts()):
            patch["bg_account"] = v
        else:
            raise HTTPException(400, f"unknown account id {v!r}")

    update_settings(patch)
    return {"ok": True, "settings": get_settings()}
