import os
from pathlib import Path

import pytest

from periscope.codex_state import (
    ReconciledState,
    StateEdge,
    clear_rollout_cache,
    reconcile_codex_state,
    rollout_edge_for,
)

SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
FIXTURES = Path(__file__).parent / "fixtures/codex/0.146.0"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_rollout_cache()


def test_normal_fixture_reduces_to_idle(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "normal.jsonl"
    path.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    edge = rollout_edge_for(path, session_id=SESSION, sessions_root=root)
    assert edge is not None
    assert (edge.session_id, edge.turn_id, edge.kind, edge.source) == (
        SESSION,
        TURN,
        "idle",
        "rollout",
    )


def test_incremental_partial_line_becomes_working(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "partial.jsonl"
    # The captured fixture terminates its partial record with a newline. Remove
    # that delimiter to exercise a genuinely append-repairable partial write.
    original = (FIXTURES / "partial-final-line.jsonl").read_bytes().rstrip(b"\n")
    path.write_bytes(original)
    partial_session = "33333333-3333-4333-8333-333333333333"
    assert rollout_edge_for(
        path, session_id=partial_session, sessions_root=root
    ) is None
    with path.open("ab") as fh:
        fh.write(b"}}\n")
    edge = rollout_edge_for(path, session_id=partial_session, sessions_root=root)
    assert edge is not None
    assert edge.kind == "working"
    assert edge.turn_id == "44444444-4444-4444-8444-444444444444"


def test_truncation_resets_cursor(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout.jsonl"
    path.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    assert rollout_edge_for(path, session_id=SESSION, sessions_root=root).kind == "idle"
    first_two = (FIXTURES / "normal.jsonl").read_bytes().splitlines(keepends=True)[:2]
    path.write_bytes(b"".join(first_two))
    assert rollout_edge_for(path, session_id=SESSION, sessions_root=root).kind == "working"


def test_mismatch_unknown_event_and_malformed_line_are_conservative(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout.jsonl"
    data = (FIXTURES / "normal.jsonl").read_bytes().splitlines(keepends=True)[0]
    path.write_bytes(data + b"{bad}\n" + b'{"type":"future","payload":{}}\n')
    assert rollout_edge_for(path, session_id=SESSION, sessions_root=root) is None
    assert rollout_edge_for(path, session_id="different", sessions_root=root) is None


def test_rejects_symlink_and_hardlink_escape(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    symlink = root / "symlink.jsonl"
    symlink.symlink_to(outside)
    assert rollout_edge_for(symlink, session_id=SESSION, sessions_root=root) is None
    hardlink = root / "hardlink.jsonl"
    os.link(outside, hardlink)
    assert rollout_edge_for(hardlink, session_id=SESSION, sessions_root=root) is None


def _edge(kind, *, session=SESSION, turn=TURN, source="rollout", order=1):
    return StateEdge(session, turn, kind, source, order)


@pytest.mark.parametrize(
    ("process", "edge", "expected"),
    [
        ("dead", _edge("working"), None),
        ("unknown", _edge("working"), ReconciledState("unknown", SESSION, None, None)),
        ("live", None, ReconciledState("unknown", SESSION, None, None)),
        ("live", _edge("working"), ReconciledState("working", SESSION, TURN, "rollout")),
        ("live", _edge("idle"), ReconciledState("idle", SESSION, None, "rollout")),
    ],
)
def test_reconciliation_liveness(process, edge, expected):
    assert (
        reconcile_codex_state(
            session_id=SESSION, process=process, rollout_edge=edge
        )
        == expected
    )


def test_reconciliation_rejects_mismatch_and_unorderable_turns():
    mismatch = _edge("idle", session="different")
    assert reconcile_codex_state(
        session_id=SESSION, process="live", rollout_edge=mismatch
    ).state == "unknown"
    assert reconcile_codex_state(
        session_id=SESSION,
        process="live",
        rollout_edge=_edge("working"),
        tui_marker=_edge("idle", turn="other", source="tui"),
    ).state == "unknown"


def test_same_source_order_and_exact_turn_completion():
    result = reconcile_codex_state(
        session_id=SESSION,
        process="live",
        rollout_edge=_edge("idle", order=2),
        tui_marker=None,
    )
    assert result == ReconciledState("idle", SESSION, None, "rollout")


def test_unverified_hook_input_forces_unknown():
    result = reconcile_codex_state(
        session_id=SESSION,
        process="live",
        rollout_edge=_edge("working"),
        hook_edge=_edge("idle", source="hook"),
    )
    assert result == ReconciledState("unknown", SESSION, None, None)
