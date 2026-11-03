"""Every `gh` periscope runs (git_pr, reviews, cleanup, projects).

gh's macOS keyring execs `/usr/bin/security` on every call to read its stored
token. GH_TOKEN takes precedence over stored credentials, so the token is read
with `gh auth token` every 10 minutes (so `gh auth switch` lands soon) and
handed to each call; telemetry and the update check are off, each of which
forks again per call."""

import os
import re
import threading
import time

from periscope.tmux import _run

_TOKEN_TTL_S = 600.0
# A failed `gh auth token` is retried after this long, and a 401 re-reads the
# token no sooner than this after the last read: a remote GH_TOKEN can't
# authenticate to (a *.ghe.com host) answers 401 on every call.
_RETRY_S = 60.0
_QUIET = {"GH_TELEMETRY": "false", "GH_NO_UPDATE_NOTIFIER": "1"}
# gh's 401s: `HTTP 401: Bad credentials (…)`, `failed to get runs: HTTP 401: …`,
# and the search API's `non-200 OK status code: 401 Unauthorized body: …`.
# A bare 401 would match `gh pr view 401` on a missing PR.
_AUTH_FAILED_RE = re.compile(r"HTTP 401\b|\b401 Unauthorized|Bad credentials")
_lock = threading.Lock()
# (read at, good until, token); token None when `gh auth token` failed — calls
# then run on gh's own credentials, as before.
_token_cache: tuple[float, float, str | None] | None = None


def _token() -> str | None:
    global _token_cache
    with _lock:
        now = time.monotonic()
        if _token_cache is None or now >= _token_cache[1]:
            code, out = _run(["gh", "auth", "token"], env={**os.environ, **_QUIET})
            token = out if code == 0 and out else None
            _token_cache = (now, now + (_TOKEN_TTL_S if token else _RETRY_S), token)
        return _token_cache[2]


def run(*args: str, cwd: str | None = None, timeout: float = 3.0) -> tuple[int, str]:
    """`gh <args>` — (returncode, stdout, or stderr on failure), as tmux._run."""
    global _token_cache
    token = _token()
    env = {**os.environ, **_QUIET}
    if token:
        env["GH_TOKEN"] = token
    code, out = _run(["gh", *args], cwd=cwd, timeout=timeout, env=env)
    if code != 0 and token and _AUTH_FAILED_RE.search(out):
        # A revoked or rotated token: read it again once _RETRY_S has passed
        # since the last read.
        with _lock:
            if _token_cache is not None:
                read_at, good_until, cached = _token_cache
                _token_cache = (read_at, min(good_until, read_at + _RETRY_S), cached)
    return code, out
