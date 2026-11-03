import contextvars
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

from periscope import agent_processes as ap

# `ps -axo pid=,ppid=,lstart=,state=,args=` rows, shaped as macOS prints them.
PS = """\
  100     1 Wed Jul 29 10:00:00 2026     Ss   -zsh
  101   100 Wed Jul 29 10:00:01 2026     S+   /usr/local/bin/codex --model o4-mini
  102   100 Wed Jul 29 10:00:02 2026     S    sh -c echo codex
"""


def setup_function():
    ap.clear_caches()


def test_parse_takes_the_executable_from_argv():
    snapshot = ap.parse_ps_snapshot(PS)
    codex = snapshot[101]
    assert (codex.pid, codex.ppid, codex.state) == (101, 100, "S+")
    assert codex.exe == "/usr/local/bin/codex"
    assert codex.argv == "/usr/local/bin/codex --model o4-mini"
    assert snapshot[100].exe == "-zsh"


def test_a_french_lstart_reads_nothing():
    # Why _take_snapshot pins LC_ALL=C: the row regex reads only C-locale dates.
    assert ap.parse_ps_snapshot("  100     1 mer. 29 juil. 10:00:00 2026     Ss   -zsh\n") == {}


def test_parse_reads_lstart_as_utc():
    # ps_env's TZ=UTC makes lstart print in UTC; parsing it as naive local
    # time would read a different instant per host, and the wrong one across
    # a DST fall-back's repeated hour.
    snapshot = ap.parse_ps_snapshot("  100     1 Wed Jul 29 10:00:00 2026     Ss   -zsh\n")
    assert snapshot[100].started_at == int(datetime(2026, 7, 29, 10, 0, 0, tzinfo=UTC).timestamp())


def test_ps_env_pins_locale_and_timezone():
    env = ap.ps_env()
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC"


def test_snapshot_runs_ps_with_the_shared_env(mocker):
    ap.clear_caches()
    run = mocker.patch(
        "subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=PS, stderr="")
    )
    assert sorted(ap.process_snapshot()) == [100, 101, 102]
    assert run.call_args.args[0] == ["ps", "-axo", "pid=,ppid=,lstart=,state=,args="]
    assert run.call_args.kwargs["env"] == ap.ps_env()


def test_snapshot_reads_a_full_path_executable_from_live_ps(monkeypatch):
    # A 16-character comm would read this process as "/Users/tom/.loca"; a French
    # locale would empty the table.
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    ap.clear_caches()
    proc = subprocess.Popen(["/Users/tom/.local/bin/claude", "30"], executable="/bin/sleep")
    try:
        info = ap.process_snapshot()[proc.pid]
    finally:
        proc.kill()
        proc.wait()
    assert info.exe == "/Users/tom/.local/bin/claude"
    assert info.argv == "/Users/tom/.local/bin/claude 30"


class _Clock:
    """A monotonic clock the test advances; ap.time reads it."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(ap, "time", SimpleNamespace(monotonic=lambda: self.now))


def test_a_ps_slower_than_the_ttl_is_still_served_after_it(monkeypatch, mocker):
    clock = _Clock(monkeypatch)

    def slow_ps():
        clock.now += ap._PS_TTL_S + 0.5
        return ap.parse_ps_snapshot(PS)

    take = mocker.patch.object(ap, "_take_snapshot", side_effect=slow_ps)
    first = ap.process_snapshot()
    assert ap.process_snapshot() is first
    take.assert_called_once()


def test_a_pinned_snapshot_outlives_the_ttl_for_its_block_only(monkeypatch, mocker):
    # build_state pins one table for the whole refresh, its fan-out threads
    # included; an off-refresh caller keeps the TTL.
    clock = _Clock(monkeypatch)
    take = mocker.patch.object(ap, "_take_snapshot", side_effect=lambda: ap.parse_ps_snapshot(PS))
    with ap.pinned_snapshot():
        first = ap.process_snapshot()
        clock.now += 5
        assert ap.process_snapshot() is first
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(lambda _: ctx.copy().run(ap.process_snapshot), range(2))) == [first, first]
        assert take.call_count == 1
        with ThreadPoolExecutor(1) as pool:
            off_refresh = pool.submit(ap.process_snapshot).result()
        assert off_refresh is not first
        assert take.call_count == 2
    clock.now += 5
    assert ap.process_snapshot() is not first
    assert take.call_count == 3


def test_overlapping_pinned_blocks_each_keep_their_own_snapshot(monkeypatch, mocker):
    # The hub's build and a REST /api/state can overlap: neither reads the other's table.
    clock = _Clock(monkeypatch)
    mocker.patch.object(ap, "_take_snapshot", side_effect=lambda: ap.parse_ps_snapshot(PS))
    a_took, b_took = threading.Event(), threading.Event()

    def refresh_a():
        with ap.pinned_snapshot():
            first = ap.process_snapshot()
            a_took.set()
            b_took.wait(5)
            return first, ap.process_snapshot()

    def refresh_b():
        with ap.pinned_snapshot():
            first = ap.process_snapshot()
            b_took.set()
            return first, ap.process_snapshot()

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(refresh_a)
        a_took.wait(5)
        clock.now += 5  # B's ps runs after A's has expired
        b = pool.submit(refresh_b)
        (a_first, a_last), (b_first, b_last) = a.result(), b.result()
    assert a_first is a_last
    assert b_first is b_last
    assert a_first is not b_first


def test_codex_launched_by_absolute_path(mocker):
    mocker.patch.object(ap, "process_snapshot", return_value=ap.parse_ps_snapshot(PS))
    assert ap.codex_process_for_pane("%1", 100) is True


def test_wrapped_codex_descendant(mocker):
    snapshot = ap.parse_ps_snapshot(
        PS.replace("  101   100", "  101   103") + "  103   100 Wed Jul 29 10:00:01 2026     S    /bin/wrapper\n"
    )
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", "100") is True


def test_argv_mention_does_not_identify_codex(mocker):
    snapshot = ap.parse_ps_snapshot("\n".join(line for line in PS.splitlines() if "local/bin/codex" not in line))
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", 100) is False


def test_snapshot_failure_is_no_opinion(mocker):
    mocker.patch.object(ap, "process_snapshot", return_value=None)
    assert ap.codex_process_for_pane("%1", 100) is None


def test_zombie_codex_is_not_live(mocker):
    snapshot = ap.parse_ps_snapshot(PS.replace("S+   /usr/local/bin/codex", "Z    /usr/local/bin/codex"))
    mocker.patch.object(ap, "process_snapshot", return_value=snapshot)
    assert ap.codex_process_for_pane("%1", 100) is False


def test_pid_reuse_invalidates_cached_agent(mocker):
    first = ap.parse_ps_snapshot(PS)
    second = ap.parse_ps_snapshot(
        PS.replace("Wed Jul 29 10:00:00 2026", "Wed Jul 29 11:00:00 2026", 1).replace(
            "/usr/local/bin/codex --model o4-mini", "/bin/sleep 20"
        )
    )
    snapshots = iter((first, second))
    mocker.patch.object(ap, "process_snapshot", side_effect=lambda: next(snapshots))
    assert ap.codex_process_for_pane("%1", 100) is True
    assert ap.codex_process_for_pane("%1", 100) is False


def test_agent_pid_start_mismatch_invalidates_cached_agent(mocker):
    first = ap.parse_ps_snapshot(PS)
    second = ap.parse_ps_snapshot(
        PS.replace("Wed Jul 29 10:00:01 2026", "Wed Jul 29 11:00:01 2026").replace(
            "/usr/local/bin/codex --model o4-mini", "/bin/sleep 20"
        )
    )
    snapshots = iter((first, second))
    mocker.patch.object(ap, "process_snapshot", side_effect=lambda: next(snapshots))
    assert ap.codex_process_for_pane("%1", 100) is True
    assert ap.codex_process_for_pane("%1", 100) is False
