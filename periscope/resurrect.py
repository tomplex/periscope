"""Rewrite tmux-resurrect save files so Claude panes resume their specific
session on restore.

tmux-resurrect + continuum already restore the workspace after a reboot, and
`@resurrect-processes 'claude'` makes resurrect re-run each Claude pane's
command. But the captured command launches a FRESH session — it has no
`--resume <uuid>` — and a periscope spawn's `--session-id <uuid>` fails, because
Claude refuses it twice. This module is invoked by resurrect's post-save-layout
hook (via `bin/periscope resurrect-rewrite`) to rewrite each Claude pane's
command in the just-written save file to `claude --resume <uuid> <its channel flags>`.

The rewrite must happen at SAVE time, while panes are live: the pane->session
map is keyed by tmux pane id, and pane ids are reassigned when the tmux server
restarts, so at restore time there is nothing left to map. The save file is the
one artifact that survives the reboot pairing each pane (by session:window.pane
position) with its command.

Two env prefixes are re-emitted here too: `CLAUDE_CONFIG_DIR=<dir>` (which
subscription the pane bills) and `CLAUDE_WRAPPER_PROFILE=<id>` (which plugin set
the `claude` wrapper gives it). resurrect captures commands from ps argv, and a
shell-level env prefix never reaches argv — so without this a pane restores onto
the DEFAULT account, quietly billing the wrong subscription, and onto the default
profile, quietly swapping its plugin set. Emitting a prefix requires
`@resurrect-processes '~claude'` (substring match) in tmux.conf; a bare
`'claude'` anchors at the start of the command and a prefixed line would not be
restored at all.

Import discipline: stdlib only, plus `periscope.config` and
`periscope.session_status` (both stdlib-only leaves). Never import
`periscope.activity` — its imports pull in the Anthropic SDK and other
non-stdlib deps, which would break the plain-`python3` hook invocation.
This module opens its own read connection to the session DB, the same
out-of-process pattern `pane_session_hook.py` uses as the writer.
"""

import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from periscope import config, session_status

# resurrect pane-line field layout (tab-separated):
#   0 'pane'  1 session  2 window_index  3 window_active  4 window_flags
#   5 pane_index  6 pane_title  7 :cwd  8 pane_active  9 pane_current_command
#   10 :full_command   (last field; may itself contain no tabs in practice)
_PANE_FIELDS = 11

_CHANNEL_RE = re.compile(r"--dangerously-load-development-channels (\S+)")
# session_status's UUID-only match, with the space before it so a strip
# leaves no double space.
_RESUME_ARG_RE = re.compile(r"\s*" + session_status.RESUME_RE.pattern + r"\b")
# A periscope spawn starts Claude with `--session-id <uuid>`, which Claude refuses once that Claude
# session exists ("Session ID … is already in use"). Restored, it must resume that Claude session instead.
_SESSION_ID_ARG_RE = re.compile(r"--session-id[= ]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


_CONTINUUM_SAVE_SH = Path.home() / ".tmux/plugins/tmux-continuum/scripts/continuum_save.sh"


# continuum_save.sh does nothing until its interval has passed since this time.
_LAST_SAVE_OPTION = "@continuum-save-last-timestamp"


def save_now(force: bool = False) -> None:
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
    installed.

    `force` saves now, whatever the interval: the script reads a cleared
    timestamp as 0, saves, and writes a new one.
    """
    if not config.is_prod():
        return
    if not _CONTINUUM_SAVE_SH.exists():
        return
    if force:
        subprocess.run(["tmux", "set-option", "-gu", _LAST_SAVE_OPTION], capture_output=True, check=False)
    subprocess.run([str(_CONTINUUM_SAVE_SH)], capture_output=True, check=False)


def _live_pane_map() -> dict[str, str]:
    """`session:window.pane` -> tmux pane id, for every live pane. Empty on any
    tmux failure (no server, etc.)."""
    fmt = "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}"
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", fmt],
            capture_output=True,
            text=True,
            check=True,
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


def _claude_pane(line: str) -> list[str] | None:
    """A claude pane line's fields; None for any other line."""
    parts = line.split("\t", _PANE_FIELDS - 1)
    if parts[0] != "pane" or len(parts) != _PANE_FIELDS or not parts[10].startswith(":"):
        return None
    if parts[10][1:].split(maxsplit=1)[:1] != ["claude"]:
        return None
    return parts


def _position(parts: list[str]) -> str:
    return f"{parts[1]}:{parts[2]}.{parts[5]}"


def _row_session(pane_id: str | None, session_map: dict[str, str]) -> str | None:
    return session_map.get(pane_id) if pane_id else None


def _session_id_as_resume(cmd: str) -> str:
    """`cmd` with its `--session-id <uuid>` read as `--resume <uuid>`."""
    return _SESSION_ID_ARG_RE.sub(r"--resume \1", cmd)


def _line_session(parts: list[str], pane_id: str | None, session_map: dict[str, str]) -> str | None:
    """The session a claude pane line resumes once rendered: its pane_sessions
    row (_rewrite_line rebuilds `claude --resume <row>`), else the `--resume`
    its captured argv already carries — a restored zombie's argv says it, and
    _session_id_as_resume reads a spawn's `--session-id` as one. The one rule
    _duplicate_resumes groups by; a duplicate grouped by any other escapes, and
    a line that isn't one loses its session."""
    m = _RESUME_ARG_RE.search(_session_id_as_resume(parts[10]))
    return _row_session(pane_id, session_map) or (m.group(1) if m else None)


def _rewrite_line(
    line: str,
    pane_map: dict[str, str],
    session_map: dict[str, str],
    config_dirs: dict[str, str] | None = None,
    profiles: dict[str, str] | None = None,
    models: dict[str, str] | None = None,
    *,
    demote: bool = False,
) -> tuple[str, bool]:
    """Rewrite one save-file line. Returns (line, rewritten?). Non-claude lines,
    non-pane lines, and panes with neither a resolvable session nor a non-default
    account/profile are returned unchanged.

    `demote`: another line keeps this pane's session (_duplicate_resumes). It
    renders as a pane with no session to resume — the session-lost shape below,
    its own captured `--resume` (or `--session-id`) stripped. Stripping `--resume`
    off the rendered resume line instead lost the model prefix and `--effort`
    that shape keeps."""
    parts = _claude_pane(line)
    if parts is None:
        return line, False
    captured = parts[10][1:]
    cmd = _session_id_as_resume(captured)
    if demote:
        cmd = _RESUME_ARG_RE.sub("", cmd)

    pane_id = pane_map.get(_position(parts))
    # The row only: without one the captured command is kept, its --resume
    # included and its --session-id read as one (_session_id_as_resume) — rebuilding
    # it would drop --effort.
    uuid = None if demote else _row_session(pane_id, session_map)
    cfg = (config_dirs or {}).get(pane_id) if pane_id else None
    prof = (profiles or {}).get(pane_id) if pane_id else None
    model = (models or {}).get(pane_id) if pane_id else None
    # The env prefixes must be emitted even when the session is unresolvable
    # (no pane_sessions row). Returning early there would restore a non-default
    # pane onto the DEFAULT account — burning the wrong subscription silently,
    # which is worse than losing --resume — or onto the default wrapper profile,
    # silently swapping the plugin set the pane was working under.
    if not uuid and not cfg and not prof and not model and cmd == captured:
        return line, False

    prefix = f"CLAUDE_CONFIG_DIR={cfg} " if cfg else ""
    if prof:
        prefix += f"{config.PROFILE_ENV_VAR}={prof} "
    if not uuid:
        # The model prefix is emitted ONLY here. A resumed session restores its
        # own model, and ANTHROPIC_MODEL at launch would override that — so on
        # the --resume path the spawn-time choice would clobber a later
        # in-session /model switch. With no session to resume, the spawn-time
        # choice is all that is left of the intent.
        if model:
            prefix += f"{config.MODEL_ENV_VAR}={model} "
        parts[10] = ":" + prefix + cmd
        return "\t".join(parts), True

    # Rebuild the command from scratch: `claude --resume <uuid>` followed by the
    # original channel flags in their on-disk order. Rebuilding (not editing in
    # place) inherently drops the stale --system-prompt and any pre-existing
    # --resume without needing to match them explicitly.
    channels = _CHANNEL_RE.findall(cmd)
    # `--effort` (the launch level) is dropped here with every other non-channel
    # flag, like the model prefix above: /effort can change a running session,
    # and re-emitting the launch level after a reboot would undo that. On the
    # no-session path the captured command keeps `--effort`.
    new_cmd = f"{prefix}claude --resume {uuid}"
    for ch in channels:
        new_cmd += f" --dangerously-load-development-channels {ch}"
    parts[10] = ":" + new_cmd
    return "\t".join(parts), True


def _duplicate_resumes(
    lines: list[str],
    pane_map: dict[str, str],
    session_map: dict[str, str],
    activity: dict[str, tuple[str | None, float]],
) -> set[int]:
    """Indexes of the claude pane lines to demote so that at most one line per
    session resumes it; the rest restore as a fresh `claude` (_rewrite_line's
    `demote`).

    Two Claudes resumed onto one session both append to its transcript — never
    intended. It got into a save via racing restores (see tmux_persist): extra
    panes, each later recorded with the same `--resume`. This does not remove
    those panes; only the declared-once rule stops them appearing. A line's
    session is the one it renders with (_line_session). Keeper: the pane whose
    live claude reports that session now, then the one that touched its
    session file last (the one in use), then the active pane, then the first
    line."""
    groups: dict[str, list[tuple[int, list[str], str | None]]] = {}
    for i, line in enumerate(lines):
        parts = _claude_pane(line)
        if parts is None:
            continue
        pane_id = pane_map.get(_position(parts))
        if uuid := _line_session(parts, pane_id, session_map):
            groups.setdefault(uuid, []).append((i, parts, pane_id))
    demoted: set[int] = set()
    for uuid, members in groups.items():
        keeper = max(members, key=lambda m, uuid=uuid: _keeper_rank(m, uuid, activity))
        demoted.update(i for i, _, _ in members if i != keeper[0])
    return demoted


def _keeper_rank(
    member: tuple[int, list[str], str | None],
    uuid: str,
    activity: dict[str, tuple[str | None, float]],
) -> tuple[bool, float, bool, int]:
    i, parts, pane_id = member
    live_sid, mtime = activity.get(pane_id, (None, float("-inf"))) if pane_id else (None, float("-inf"))
    return (live_sid == uuid, mtime, parts[8] == "1", -i)


def rewrite_save_file(save_path: Path) -> int:
    """Rewrite claude pane lines in a resurrect save file to resume their
    specific session. Returns the number of lines rewritten. Atomic."""
    save_path = Path(save_path)
    pane_map = _live_pane_map()
    session_map = _session_map()
    config_dirs = session_status.pane_config_dirs()
    profiles = session_status.pane_profiles()
    models = session_status.pane_models()

    with save_path.open() as f:
        lines = [raw.rstrip("\n") for raw in f]
    # Keepers are picked before rendering, so each line renders (and counts)
    # once, and a duplicate takes the same shape as any session-lost pane.
    demoted = _duplicate_resumes(lines, pane_map, session_map, session_status.pane_session_activity())
    if demoted:
        where = ", ".join(_position(lines[i].split("\t")) for i in sorted(demoted))
        print(f"resurrect-resume: dropped duplicate --resume on {where}", file=sys.stderr)

    out_lines: list[str] = []
    rewritten = 0
    for i, line in enumerate(lines):
        new, changed = _rewrite_line(line, pane_map, session_map, config_dirs, profiles, models, demote=i in demoted)
        rewritten += changed
        out_lines.append(new)

    if rewritten:
        with tempfile.NamedTemporaryFile("w", dir=save_path.parent, delete=False) as tmp:
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
