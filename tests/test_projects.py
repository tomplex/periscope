"""Direct tests for periscope/projects.py (resolve_project_for_window).

CLAUDE.md flags projects.py as indirectly-covered-only; this starts the
direct mirror file. Route-level behavior stays in tests/routes/test_projects.py.
"""

import pytest

from periscope.projects import (
    MAIN_KEY,
    archive_project,
    create_project,
    resolve_project_for_window,
)


@pytest.fixture(autouse=True)
def _state(clean_state):
    # Isolation: without this, create_project persists into the REAL
    # state.json (clean_state is not autouse in tests/conftest.py).
    return clean_state


def test_resolve_matched_session_returns_pinned_dir():
    create_project("/Users/foo/dev/myproj", name="myproj", tmux_session="myproj")
    assert resolve_project_for_window({"session": "myproj"}) == "/Users/foo/dev/myproj"


def test_resolve_unknown_session_folds_to_main():
    # The fold rule: every non-empty session resolves to SOMETHING.
    assert resolve_project_for_window({"session": "adhoc-scratch"}) == MAIN_KEY


def test_resolve_empty_session_returns_none():
    assert resolve_project_for_window({"session": ""}) is None
    assert resolve_project_for_window({}) is None


def test_resolve_archived_project_still_matches():
    # Archived rows still resolve (the frontend folds them to dev via the
    # no-row-in-projects_view fallback; the resolver itself doesn't filter).
    create_project("/Users/foo/dev/oldproj", name="old", tmux_session="oldproj")
    archive_project("/Users/foo/dev/oldproj")
    assert resolve_project_for_window({"session": "oldproj"}) == "/Users/foo/dev/oldproj"
