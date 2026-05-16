"""LGTM mirror: cached_lgtm_state + _normalize_repo_path + _lgtm_submitted.

The SSE loop (_lgtm_periodic_refresh, _lgtm_sse_loop) and _lgtm_refresh_all
are integration-only; not covered here. Manual smoke is "boot periscope
while LGTM runs on :9900 and confirm the dashboard surfaces review-pane
indicators."
"""

from pathlib import Path

from periscope.lgtm import (
    LGTM_BASE_URL,
    _normalize_repo_path,
    _lgtm_submitted,
    cached_lgtm_state,
    _LGTM_LOCK, _LGTM_BY_REPO,
)


def test_lgtm_base_url_defaults_to_localhost_9900():
    assert "9900" in LGTM_BASE_URL


def test_normalize_repo_path_returns_absolute(tmp_path: Path):
    out = _normalize_repo_path(str(tmp_path))
    assert Path(out).is_absolute()


def test_normalize_repo_path_handles_none_and_empty():
    assert _normalize_repo_path(None) == ""
    assert _normalize_repo_path("") == ""


def test_normalize_repo_path_resolves_symlinks(tmp_path: Path):
    """If the input contains a `..` or symlink, resolve it."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    nonresolved = str(tmp_path / "a" / ".." / "a" / "b")
    out = _normalize_repo_path(nonresolved)
    assert Path(out) == nested.resolve()


def test_cached_lgtm_state_returns_none_for_unknown_repo():
    assert cached_lgtm_state("/no/such/repo/anywhere") is None


def test_cached_lgtm_state_returns_none_for_empty_input():
    assert cached_lgtm_state(None) is None
    assert cached_lgtm_state("") is None


def test_cached_lgtm_state_returns_stored_entry(tmp_path: Path):
    """Seed _LGTM_BY_REPO with a fake entry; cached_lgtm_state should
    return a copy. Use a resolved path because _normalize_repo_path
    resolves before lookup."""
    fake = {"slug": "fake", "url": "http://x", "branch": "main"}
    key = str(tmp_path.resolve())
    with _LGTM_LOCK:
        _LGTM_BY_REPO[key] = fake
    try:
        out = cached_lgtm_state(str(tmp_path))
        assert out is not None
        assert out["slug"] == "fake"
    finally:
        with _LGTM_LOCK:
            _LGTM_BY_REPO.pop(key, None)


def test_cached_lgtm_state_returns_copy_not_reference(tmp_path: Path):
    """Mutating the returned dict must not leak into the cache."""
    fake = {"slug": "x", "claude_comments": 3}
    key = str(tmp_path.resolve())
    with _LGTM_LOCK:
        _LGTM_BY_REPO[key] = fake
    try:
        out = cached_lgtm_state(str(tmp_path))
        out["claude_comments"] = 999
        with _LGTM_LOCK:
            assert _LGTM_BY_REPO[key]["claude_comments"] == 3
    finally:
        with _LGTM_LOCK:
            _LGTM_BY_REPO.pop(key, None)


def test_lgtm_submitted_false_when_signal_file_missing():
    assert _lgtm_submitted("nonexistent-slug-xyz") is False
