import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "codex" / "0.146.0"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_normal_rollout_has_matching_session_and_turn_relationships():
    records = [
        json.loads(line)
        for line in (FIXTURES / "normal.jsonl").read_text().splitlines()
    ]
    meta, started, complete = records

    assert meta["type"] == "session_meta"
    assert meta["payload"]["session_id"] == meta["payload"]["id"]
    assert meta["payload"]["originator"] == "codex-tui"
    assert meta["payload"]["cli_version"] == "0.146.0"
    assert started["payload"]["type"] == "task_started"
    assert complete["payload"]["type"] == "task_complete"
    assert started["payload"]["turn_id"] == complete["payload"]["turn_id"]
    assert UUID_RE.match(meta["payload"]["session_id"])
    assert UUID_RE.match(started["payload"]["turn_id"])


def test_partial_final_line_is_deliberately_not_valid_json():
    lines = (FIXTURES / "partial-final-line.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["type"] == "session_meta"
    try:
        json.loads(lines[1])
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("last line must remain truncated")


def test_rollout_metadata_distinguishes_root_from_subagent():
    root = _json("root-session-meta.json")["payload"]
    child = _json("subagent-session-meta.json")["payload"]

    assert root["thread_source"] == "cli"
    assert child["thread_source"] == "subagent"
    assert child["source"]["subagent"]["thread_spawn"]["depth"] == 1
    assert child["parent_thread_id"] == root["id"]
    assert child["id"] != root["id"]


def test_fixtures_are_sanitized_and_gates_are_not_fabricated():
    all_text = "\n".join(path.read_text() for path in FIXTURES.iterdir())
    assert "/Users/" not in all_text
    assert "/home/" not in all_text
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{10,}", all_text)

    manifest = _json("evidence-manifest.json")
    assert manifest["unresolved_hard_gates"] == [
        "hook-payload-shapes",
        "hook-tmux-pane-inheritance",
        "hook-subagent-firing-and-root-binding-safety",
    ]
    for unsupported in (
        "session_start_startup.json",
        "session_start_resume.json",
        "user_prompt_submit.json",
        "stop.json",
        "session_end.json",
        "interrupted.jsonl",
        "failed-or-cancelled.jsonl",
        "approval-roundtrip.jsonl",
        "stop-continuation.jsonl",
    ):
        assert not (FIXTURES / unsupported).exists()
