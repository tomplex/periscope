"""Rewrite tmux-resurrect save files so Claude panes resume their specific
session on restore.

tmux-resurrect + continuum already restore the workspace after a reboot, and
`@resurrect-processes 'claude'` makes resurrect re-run each Claude pane's
command. But the captured command launches a FRESH session — it has no
`--resume <uuid>`. This module is invoked by resurrect's post-save-layout hook
(via `bin/periscope resurrect-rewrite`) to rewrite each Claude pane's command in
the just-written save file to `claude --resume <uuid> <its channel flags>`.

The rewrite must happen at SAVE time, while panes are live: the pane->session
map is keyed by tmux pane id, and pane ids are reassigned when the tmux server
restarts, so at restore time there is nothing left to map. The save file is the
one artifact that survives the reboot pairing each pane (by session:window.pane
position) with its command.

A pane running on a second Claude subscription (`CLAUDE_CONFIG_DIR=<dir> claude`)
gets that prefix re-emitted here too. resurrect captures commands from ps argv,
and a shell-level env prefix never reaches argv — so without this the pane
restores onto the DEFAULT account and quietly bills the wrong subscription.
Emitting the prefix requires `@resurrect-processes '~claude'` (substring match)
in tmux.conf; a bare `'claude'` anchors at the start of the command and a
prefixed line would not be restored at all.

Import discipline: stdlib only, plus `periscope.config` (a stdlib-only leaf).
Never import `periscope.activity` — its imports pull in the Anthropic SDK and
other non-stdlib deps, which would break the plain-`python3` hook invocation.
This module opens its own read connection to the session DB, the same
out-of-process pattern `pane_session_hook.py` uses as the writer.
"""
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from periscope import config

# resurrect pane-line field layout (tab-separated):
#   0 'pane'  1 session  2 window_index  3 window_active  4 window_flags
#   5 pane_index  6 pane_title  7 :cwd  8 pane_active  9 pane_current_command
#   10 :full_command   (last field; may itself contain no tabs in practice)
_PANE_FIELDS = 11

_CHANNEL_RE = re.compile(r"--dangerously-load-development-channels (\S+)")
_CONFIG_DIR_RE = re.compile(r"\bCLAUDE_CONFIG_DIR=(\S+)")

_CONTINUUM_SAVE_SH = (
    Path.home() / ".tmux/plugins/tmux-continuum/scripts/continuum_save.sh"
)


def save_now() -> None:
    """Drive tmux-continuum's periodic save.

    tmux-continuum has no timer: its save fires purely as a side effect of
    `status-right` being EXPANDED, which only happens when a status line is
    drawn for a client. Every client periscope attaches is control-mode
    (the pane mirror and the input client), and control-mode clients render no
    status line — so on a host driven entirely through the dashboard the save
    never runs. Observed: a 24-day gap between saves, after which a reboot
    restored a 24-day-old layout.

    Safe to call every worker tick: the script self-gates on
    `@continuum-save-interval` and takes its own lock, so it only writes when
    an interval has actually elapsed. Degrades silently when continuum isn't
    installed — same contract as the LGTM integration.
    """
    if not config.is_prod():
        return
    if not _CONTINUUM_SAVE_SH.exists():
        return
    subprocess.run([str(_CONTINUUM_SAVE_SH)], capture_output=True, check=False)


def _live_pane_map() -> dict[str, str]:
    """`session:window.pane` -> tmux pane id, for every live pane. Empty on any
    tmux failure (no server, etc.)."""
    fmt = "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}"
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", fmt],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    m: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        session, window, pane, pane_id = parts
        m[f"{session}:{window}.{pane}"] = pane_id
    return m


def _proc_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """(ppid -> child pids, pid -> command). Empty on any ps failure."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}, {}
    kids: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    for line in out.splitlines():
        bits = line.split(maxsplit=2)
        if len(bits) < 3:
            continue
        try:
            pid, ppid = int(bits[0]), int(bits[1])
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
        cmds[pid] = bits[2]
    return kids, cmds


def _config_dir_from_ps(cmd_only: str, with_env: str) -> str | None:
    """Extract CLAUDE_CONFIG_DIR from `ps eww` output.

    `ps eww` prints the command and the environment concatenated with no
    delimiter, so searching the whole string matches any process whose COMMAND
    merely mentions the variable (observed: a grep pattern containing the name
    was picked up as if it were a real value). Strip the command — obtained
    without `e` for the same pid — and search only the environment tail.
    """
    cmd_only, with_env = cmd_only.strip(), with_env.strip()
    if not with_env.startswith(cmd_only):
        return None
    env_tail = with_env[len(cmd_only):]
    m = _CONFIG_DIR_RE.search(env_tail)
    return m.group(1) if m else None


def _pane_config_dirs() -> dict[str, str]:
    """tmux pane id -> the CLAUDE_CONFIG_DIR its live Claude runs under.

    Read from the running process's environment rather than from any stored
    binding: env is what the process is actually using, and it needs no state to
    stay in sync. Panes on the default account have no entry.

    Only correct at SAVE time, while the panes are alive — which is when this
    module runs. A shell-level `VAR=x cmd` prefix is consumed by the shell and
    never reaches argv, so the config dir is absent from the command resurrect
    captures via ps; without this lookup every account-B pane silently restores
    onto the default account.
    """
    fmt = "#{pane_id}\t#{pane_pid}"
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", fmt],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    kids, cmds = _proc_table()
    dirs: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        pane_id, pid_raw = parts
        try:
            root = int(pid_raw)
        except ValueError:
            continue
        # BFS the pane's process subtree; the Claude process is a descendant of
        # the pane's shell, not the shell itself.
        seen: set[int] = set()
        queue = [root]
        while queue:
            pid = queue.pop()
            if pid in seen:
                continue
            seen.add(pid)
            queue.extend(kids.get(pid, ()))
        seen.discard(root)
        for pid in sorted(seen):
            if "claude" not in cmds.get(pid, ""):
                continue
            try:
                env_out = subprocess.run(
                    ["ps", "eww", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, check=False,
                ).stdout
            except OSError:
                continue
            cfg = _config_dir_from_ps(cmds[pid], env_out)
            if cfg:
                dirs[pane_id] = cfg
                break
    return dirs


def _session_map() -> dict[str, str]:
    """tmux pane id -> Claude session id, from the pane_sessions table in
    periscope.db. Empty if the DB or table is absent (degrades to all-fresh)."""
    if not config.ACTIVITY_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{config.ACTIVITY_DB}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT pane_id, session_id FROM pane_sessions").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return dict(rows)


def _rewrite_line(
    line: str,
    pane_map: dict[str, str],
    session_map: dict[str, str],
    config_dirs: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Rewrite one save-file line. Returns (line, rewritten?). Non-claude lines,
    non-pane lines, and panes with neither a resolvable session nor a non-default
    account are returned unchanged."""
    parts = line.split("\t", _PANE_FIELDS - 1)
    if parts[0] != "pane" or len(parts) != _PANE_FIELDS:
        return line, False
    cmd_field = parts[10]
    if not cmd_field.startswith(":"):
        return line, False
    cmd = cmd_field[1:]
    if cmd.split(maxsplit=1)[:1] != ["claude"]:
        return line, False

    pane_id = pane_map.get(f"{parts[1]}:{parts[2]}.{parts[5]}")
    uuid = session_map.get(pane_id) if pane_id else None
    cfg = (config_dirs or {}).get(pane_id) if pane_id else None
    # The account prefix must be emitted even when the session is unresolvable
    # (no pane_sessions row). Returning early there would restore a non-default
    # pane onto the DEFAULT account — burning the wrong subscription silently,
    # which is worse than losing --resume.
    if not uuid and not cfg:
        return line, False

    prefix = f"CLAUDE_CONFIG_DIR={cfg} " if cfg else ""
    if not uuid:
        parts[10] = ":" + prefix + cmd
        return "\t".join(parts), True

    # Rebuild the command from scratch: `claude --resume <uuid>` followed by the
    # original channel flags in their on-disk order. Rebuilding (not editing in
    # place) inherently drops the stale --system-prompt and any pre-existing
    # --resume without needing to match them explicitly.
    channels = _CHANNEL_RE.findall(cmd)
    new_cmd = f"{prefix}claude --resume {uuid}"
    for ch in channels:
        new_cmd += f" --dangerously-load-development-channels {ch}"
    parts[10] = ":" + new_cmd
    return "\t".join(parts), True


def rewrite_save_file(save_path: Path) -> int:
    """Rewrite claude pane lines in a resurrect save file to resume their
    specific session. Returns the number of lines rewritten. Atomic."""
    save_path = Path(save_path)
    pane_map = _live_pane_map()
    session_map = _session_map()
    config_dirs = _pane_config_dirs()

    out_lines: list[str] = []
    rewritten = 0
    with save_path.open() as f:
        for raw in f:
            stripped = raw.rstrip("\n")
            new, changed = _rewrite_line(stripped, pane_map, session_map, config_dirs)
            rewritten += changed
            out_lines.append(new)

    if rewritten:
        with tempfile.NamedTemporaryFile(
            "w", dir=save_path.parent, delete=False
        ) as tmp:
            tmp.write("\n".join(out_lines) + "\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(save_path)
    return rewritten


def main() -> None:
    try:
        n = rewrite_save_file(Path(sys.argv[1]))
        print(f"resurrect-resume: rewrote {n} claude pane(s)", file=sys.stderr)
    except Exception as e:  # a hook must never fail a continuum save
        print(f"resurrect-resume: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
