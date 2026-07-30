from periscope import agent_processes as ap

PS = """\
  100     1 Mon Jul 29 10:00:00 2026 S    /bin/zsh zsh
  101   100 Mon Jul 29 10:00:01 2026 S    /usr/local/bin/codex codex
  102   100 Mon Jul 29 10:00:02 2026 S    /bin/sh sh -c echo codex
"""


def setup_function():
    ap.clear_caches()


def test_parse_and_direct_codex_process(mocker):
    snapshot = ap.parse_ps_snapshot(PS)
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", 100) is True


def test_wrapped_codex_descendant(mocker):
    snapshot = ap.parse_ps_snapshot(
        PS.replace("  101   100", "  101   103")
        + "  103   100 Mon Jul 29 10:00:01 2026 S    /bin/wrapper wrapper\n"
    )
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", "100") is True


def test_argv_mention_does_not_identify_codex(mocker):
    snapshot = ap.parse_ps_snapshot(
        "\n".join(line for line in PS.splitlines() if "local/bin/codex" not in line)
    )
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", 100) is False


def test_snapshot_failure_is_no_opinion(mocker):
    mocker.patch.object(ap, "process_snapshot", return_value=None)
    assert ap.codex_process_for_pane("%1", 100) is None


def test_zombie_codex_is_not_live(mocker):
    snapshot = ap.parse_ps_snapshot(PS.replace("2026 S    /usr/local/bin/codex", "2026 Z    /usr/local/bin/codex"))
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", 100) is False


def test_pid_reuse_invalidates_cached_agent(mocker):
    first = ap.parse_ps_snapshot(PS)
    second = ap.parse_ps_snapshot(
        PS.replace("Mon Jul 29 10:00:00 2026", "Mon Jul 29 11:00:00 2026", 1)
        .replace("/usr/local/bin/codex codex", "/bin/sleep sleep 20")
    )
    snapshots = iter((first, second))
    mocker.patch.object(ap, "process_snapshot", side_effect=lambda: next(snapshots))
    assert ap.codex_process_for_pane("%1", 100) is True
    assert ap.codex_process_for_pane("%1", 100) is False


def test_agent_pid_start_mismatch_invalidates_cached_agent(mocker):
    first = ap.parse_ps_snapshot(PS)
    second = ap.parse_ps_snapshot(
        PS.replace("Mon Jul 29 10:00:01 2026", "Mon Jul 29 11:00:01 2026")
        .replace("/usr/local/bin/codex codex", "/bin/sleep sleep 20")
    )
    snapshots = iter((first, second))
    mocker.patch.object(ap, "process_snapshot", side_effect=lambda: next(snapshots))
    assert ap.codex_process_for_pane("%1", 100) is True
    assert ap.codex_process_for_pane("%1", 100) is False
