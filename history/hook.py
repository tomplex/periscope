"""SessionEnd hook entry point.

Claude Code SessionEnd hooks pipe a JSON event over stdin to the configured
command. We extract `transcript_path` and index that one session. Errors
are swallowed and logged — a hook must never block Claude Code shutdown.

Install via ~/.claude/settings.json:

  {
    "hooks": {
      "SessionEnd": [
        { "command": "python -m history hook" }
      ]
    }
  }
"""
from __future__ import annotations

import json
import logging
import os
import sys

from .indexer import index_one

log = logging.getLogger(__name__)


def run_hook() -> int:
    """Read a SessionEnd JSON payload from stdin and index the named transcript.
    Always returns 0 — hooks must not block Claude Code."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except Exception as e:
        log.warning("history.hook: failed to read/parse stdin: %s", e)
        return 0
    if not isinstance(payload, dict):
        return 0
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return 0
    try:
        index_one(transcript_path)
    except Exception as e:
        log.warning("history.hook: index_one failed for %s: %s", transcript_path, e)
    return 0
