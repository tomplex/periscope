"""Launch a GUI code editor on a directory.

macOS-only, same as the `open -R` Finder reveal in fs.py. The launch goes
through `open -a <App>` rather than a CLI shim (`code`, `cursor`) on purpose:
the server runs under launchd with no interactive shell, so a shim that is a
shell function or an alias — which `code` is on this host — simply isn't on
PATH. Same trap as `claude` being a zsh function (see CLAUDE.md invariant 14).
"""

import os
import subprocess
from pathlib import Path

# Display name → .app bundle. Ordered by preference so the first detected
# entry is a sensible default for a fresh install.
KNOWN_EDITORS: tuple[tuple[str, str], ...] = (
    ("Cursor", "Cursor.app"),
    ("Visual Studio Code", "Visual Studio Code.app"),
    ("Zed", "Zed.app"),
    ("Windsurf", "Windsurf.app"),
    ("Sublime Text", "Sublime Text.app"),
)

# Both the system-wide and the per-user install locations.
_APP_DIRS = ("/Applications", os.path.expanduser("~/Applications"))


def detect_editors() -> list[str]:
    """Display names of the KNOWN_EDITORS actually installed, in
    KNOWN_EDITORS order. Cheap (a handful of isdir calls) — no cache, so
    installing an editor shows up without a restart."""
    found = []
    for name, bundle in KNOWN_EDITORS:
        if any(Path(d, bundle).is_dir() for d in _APP_DIRS):
            found.append(name)
    return found


def open_in_editor(app: str, path: str) -> None:
    """`open -a <app> <path>`. Raises ValueError if `app` isn't a detected
    editor, or if the launch fails.

    The membership check is the security boundary: it is what keeps this
    from being an arbitrary-command feature, since `app` originates in the
    settings block which the client can PATCH. argv is a list and never a
    shell string, so `path` can't inject either.
    """
    if app not in detect_editors():
        raise ValueError(f"not an available editor: {app}")
    proc = subprocess.run(
        ["open", "-a", app, path], capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        # Unlike fs.safe_reveal's best-effort Finder reveal, surface this: the
        # user clicked expecting a window, so silence would read as a no-op.
        raise ValueError(
            (proc.stderr or "").strip() or f"could not open {app} (exit {proc.returncode})"
        )
