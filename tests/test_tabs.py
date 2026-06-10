"""periscope.tabs: server-owned tab mutations on windows[pid]."""

import pytest

from periscope.tabs import activate_tab, close_tab, open_tab


@pytest.fixture
def state(clean_state, mocker):
    mocker.patch("periscope.store._write_state")
    return clean_state


def test_open_tab_appends_and_activates(state):
    out = open_tab("p1", "/a/b.md", 3)

    assert out == {
        "open_tabs": [{"path": "/a/b.md", "line": 3}],
        "active_tab": "file:/a/b.md",
    }
    assert state["windows"]["p1"]["open_tabs"] == [{"path": "/a/b.md", "line": 3}]
    assert state["windows"]["p1"]["active_tab"] == "file:/a/b.md"


def test_open_tab_existing_path_activates_without_duplicating(state):
    open_tab("p1", "/a/b.md", 3)
    open_tab("p1", "/a/c.md")

    out = open_tab("p1", "/a/b.md")

    assert [t["path"] for t in out["open_tabs"]] == ["/a/b.md", "/a/c.md"]
    assert out["active_tab"] == "file:/a/b.md"
    assert out["open_tabs"][0]["line"] == 3  # original line preserved


def test_close_active_tab_falls_back_to_pane_and_clears_keys(state):
    open_tab("p1", "/a/b.md")

    out = close_tab("p1", "/a/b.md")

    assert out == {"open_tabs": [], "active_tab": "pane"}
    # empty list / "pane" are the defaults — persisted keys are dropped
    assert "open_tabs" not in state["windows"]["p1"]
    assert "active_tab" not in state["windows"]["p1"]


def test_close_inactive_tab_keeps_active(state):
    open_tab("p1", "/a/b.md")
    open_tab("p1", "/a/c.md")

    out = close_tab("p1", "/a/b.md")

    assert [t["path"] for t in out["open_tabs"]] == ["/a/c.md"]
    assert out["active_tab"] == "file:/a/c.md"
    assert state["windows"]["p1"]["active_tab"] == "file:/a/c.md"


def test_activate_pane_clears_persisted_key(state):
    open_tab("p1", "/a/b.md")

    activate_tab("p1", "pane")

    assert "active_tab" not in state["windows"]["p1"]
    assert state["windows"]["p1"]["open_tabs"]  # tabs untouched


def test_activate_file_tab_persists(state):
    open_tab("p1", "/a/b.md")
    open_tab("p1", "/a/c.md")

    out = activate_tab("p1", "file:/a/b.md")

    assert out == {"active_tab": "file:/a/b.md"}
    assert state["windows"]["p1"]["active_tab"] == "file:/a/b.md"
