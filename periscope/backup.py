"""Copy the persistent stores at boot, before this process deletes rows from them.

Boot and the first full-roster pass drop the rows of panes that tmux does not
show. After a reboot that is every pane, until the restore brings them back.
"""

import shutil
import sqlite3
import time
from contextlib import closing

from periscope import config, store

# shortcut: counts boots, so a restart loop pushes out every older copy; keep one per day if that happens.
KEEP = 10


def back_up_stores() -> None:
    """Copy state.json and periscope.db to `backups/<time>/`; keep the newest KEEP copies."""
    if not config.is_prod():
        return
    backups = config.config_dir() / "backups"
    copy = backups / time.strftime("%Y%m%dT%H%M%S")
    copy.mkdir(parents=True)
    state = store._state_path()
    if state.exists():  # a first boot has none
        shutil.copy2(state, copy)
    # SQLite's own backup, because a file copy misses the rows still in the WAL.
    with closing(sqlite3.connect(config.ACTIVITY_DB)) as src, closing(sqlite3.connect(copy / "periscope.db")) as dst:
        src.backup(dst)
    for old in sorted(backups.iterdir())[:-KEEP]:
        shutil.rmtree(old)
