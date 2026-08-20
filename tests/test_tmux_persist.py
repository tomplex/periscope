"""Tests for periscope.tmux_persist — the ~/.tmux.conf line rules and the
healthz status built from live tmux options."""
import time
from pathlib import Path

import pytest

import periscope.tmux_persist as tp

REPO = Path("/opt/periscope")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local/share"))
    return tmp_path


# --- ensure_sourced / remove_sourced ---------------------------------------

def test_source_line_inserted_above_existing_tpm_run(home):
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\nset -g @plugin 'tmux-plugins/tpm'\nrun '~/.tmux/plugins/tpm/tpm'\n")
    assert tp.ensure_sourced(conf) is True
    lines = conf.read_text().splitlines()
    assert lines[-2] == tp.source_line()
    assert lines[-1] == "run '~/.tmux/plugins/tpm/tpm'"
    assert lines[:2] == ["set -g mouse on", "set -g @plugin 'tmux-plugins/tpm'"]


def test_source_line_appended_with_tpm_run_when_absent(home):
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on")   # no trailing newline
    assert tp.ensure_sourced(conf) is True
    assert conf.read_text() == f"set -g mouse on\n{tp.source_line()}\n{tp.TPM_RUN_LINE}\n"


def test_ensure_sourced_creates_missing_conf(home):
    conf = home / ".tmux.conf"
    assert tp.ensure_sourced(conf) is True
    assert conf.read_text() == f"{tp.source_line()}\n{tp.TPM_RUN_LINE}\n"


def test_ensure_sourced_is_idempotent(home):
    conf = home / ".tmux.conf"
    conf.write_text("run-shell ~/.tmux/plugins/tpm/tpm\n")
    tp.ensure_sourced(conf)
    before = conf.read_text()
    assert tp.ensure_sourced(conf) is False
    assert conf.read_text() == before


def test_source_line_recognised_with_quotes_and_tilde(home):
    conf = home / ".tmux.conf"
    conf.write_text('source-file -q "~/.config/periscope/tmux.conf"\nrun ~/.tmux/plugins/tpm/tpm\n')
    assert tp.is_sourced(conf) is True
    assert tp.ensure_sourced(conf) is False


def test_remove_sourced_round_trips_and_leaves_tpm_run(home):
    conf = home / ".tmux.conf"
    original = "set -g mouse on\nrun '~/.tmux/plugins/tpm/tpm'\n"
    conf.write_text(original)
    tp.ensure_sourced(conf)
    assert tp.remove_sourced(conf) is True
    assert conf.read_text() == original
    assert tp.remove_sourced(conf) is False


def test_uninstall_removes_owned_file_and_line(home):
    tp.owned_conf().parent.mkdir(parents=True)
    tp.owned_conf().write_text("x\n")
    conf = home / ".tmux.conf"
    tp.ensure_sourced(conf)
    actions = tp.uninstall()
    assert len(actions) == 2
    assert not tp.owned_conf().exists()
    assert conf.read_text() == f"{tp.TPM_RUN_LINE}\n"


def test_owned_conf_carries_this_checkouts_hook():
    text = tp.owned_conf_text(REPO)
    assert "set -g @resurrect-hook-post-save-layout '/opt/periscope/bin/periscope resurrect-rewrite'" in text
    assert "set -g @resurrect-processes '~claude'" in text
    assert "set -g @continuum-restore 'on'" in text
    # TPM is run from ~/.tmux.conf, after this file is sourced — never here.
    assert not any(line.startswith("run") for line in text.splitlines())


# --- status ----------------------------------------------------------------

def _stub_live(monkeypatch, options):
    """options=None simulates no tmux server."""
    def live(name):
        if options is None:
            return None
        return options.get(name, "")
    monkeypatch.setattr(tp, "_live_option", live)


def _healthy_home(home):
    for p in tp.REQUIRED_PLUGINS:
        (home / ".tmux/plugins" / p).mkdir(parents=True)
    tp.ensure_sourced(home / ".tmux.conf")


HEALTHY = {
    "@continuum-restore": "on",
    "@resurrect-processes": "~claude",
    "@resurrect-hook-post-save-layout": "/opt/periscope/bin/periscope resurrect-rewrite",
    "@continuum-save-interval": "10",
}


def test_status_ok_when_everything_lines_up(home, monkeypatch):
    _healthy_home(home)
    _stub_live(monkeypatch, HEALTHY)
    s = tp.status(REPO)
    assert s["ok"] is True
    assert s["last_save_at"] is None and s["last_save_stale"] is False


def test_status_reads_live_server_not_file(home, monkeypatch):
    _healthy_home(home)
    _stub_live(monkeypatch, {**HEALTHY, "@resurrect-hook-post-save-layout": "~/old/periscope/bin/periscope resurrect-rewrite"})
    s = tp.status(REPO)
    assert s["conf_sourced"] is True
    assert s["hook_current"] is False
    assert s["ok"] is False


def test_status_accepts_tilde_hook_path(home, monkeypatch):
    _healthy_home(home)
    _stub_live(monkeypatch, {**HEALTHY, "@resurrect-hook-post-save-layout": "~/periscope/bin/periscope resurrect-rewrite"})
    assert tp.status(home / "periscope")["hook_current"] is True


def test_status_no_server_fails_live_checks(home, monkeypatch):
    _healthy_home(home)
    _stub_live(monkeypatch, None)
    s = tp.status(REPO)
    assert s["plugins"] == dict.fromkeys(tp.REQUIRED_PLUGINS, True)
    assert s["restore_on"] is False and s["hook_current"] is False
    assert s["ok"] is False


def test_status_stale_save_flips_ok(home, monkeypatch):
    _healthy_home(home)
    _stub_live(monkeypatch, HEALTHY)
    d = home / ".local/share/tmux/resurrect"
    d.mkdir(parents=True)
    target = d / "tmux_resurrect_old.txt"
    target.write_text("")
    (d / "last").symlink_to(target.name)
    stale_at = time.time() - 3 * 10 * 60   # 3 intervals at the live 10-min setting
    import os
    os.utime(target, (stale_at, stale_at))
    s = tp.status(REPO)
    assert s["last_save_stale"] is True
    assert s["ok"] is False
    fresh = time.time() - 60
    os.utime(target, (fresh, fresh))
    s = tp.status(REPO)
    assert s["last_save_stale"] is False
    assert s["ok"] is True
