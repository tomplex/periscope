"""Server-owned file-preview tabs, persisted per-pid in state.json.

The browser's tab strip renders from these fields (surfaced through the
window view into /api/state) and mutates them via /api/pane/tabs/*; the
open_document MCP tool writes them directly. Server ownership is what
makes tabs survive page refresh, server restart, and land even when no
browser is polling at the moment Claude opens a document.

Persisted shape on windows[pid]:
  open_tabs:  [{"path": str, "line": int|None}, ...]   (absent when empty)
  active_tab: "file:<path>"                            (absent when "pane")

The read-modify-write here is get_window + set_window_fields, not atomic.
Two tab mutations racing (a user click and an MCP open within the same
few ms) can lose one — accepted for a single-user tool.
"""

from periscope.store import get_window, set_window_fields


def open_tab(pid: str, path: str, line: int | None = None) -> dict:
    """Add a tab for `path` (no-op if already open) and make it active."""
    tabs = list(get_window(pid).get("open_tabs") or [])
    if not any(t.get("path") == path for t in tabs):
        tabs.append({"path": path, "line": line})
    active = f"file:{path}"
    set_window_fields(pid, open_tabs=tabs, active_tab=active)
    return {"open_tabs": tabs, "active_tab": active}


def close_tab(pid: str, path: str) -> dict:
    """Remove the tab for `path`; if it was active, fall back to the pane."""
    w = get_window(pid)
    tabs = [t for t in (w.get("open_tabs") or []) if t.get("path") != path]
    active = w.get("active_tab") or "pane"
    if active == f"file:{path}":
        active = "pane"
    set_window_fields(
        pid,
        open_tabs=tabs or None,
        active_tab=None if active == "pane" else active,
    )
    return {"open_tabs": tabs, "active_tab": active}


def activate_tab(pid: str, tab: str) -> dict:
    """Set the active tab ("pane" or "file:<path>")."""
    set_window_fields(pid, active_tab=None if tab == "pane" else tab)
    return {"active_tab": tab}
