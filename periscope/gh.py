"""Every `gh` periscope runs (git_pr, reviews, cleanup, projects).

gh's macOS keyring execs `/usr/bin/security` on every call to read its stored
token. GH_TOKEN takes precedence over stored credentials, so the token is read
with `gh auth token` every 10 minutes (so `gh auth switch` lands soon) and
handed to each call; telemetry and the update check are off, each of which
forks again per call. GH_TOKEN applies to github.com and any *.ghe.com host —
a call in a repo on the OTHER one of those two 401s with it; `run` retries
that case once without the token and remembers the cwd so later calls there
skip it outright. `AVAILABLE` is the one "is gh installed" check; `run`
returns without forking when it's False."""

import re
import shutil
import threading
import time

from periscope.tmux import _run

AVAILABLE = shutil.which("gh") is not None
_TOKEN_TTL_S = 600.0
# A failed `gh auth token` is retried after this long, and a 401 with a token
# re-reads the token no sooner than this after the last read (a revoked or
# rotated token). A *.ghe.com host whose GH_TOKEN doesn't apply there 401s on
# every call regardless of the re-read; `run`'s tokenless retry below is what
# recovers that case.
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
# cwds whose GH_TOKEN 401'd and then succeeded once retried without it: a
# *.ghe.com repo when the token is for github.com, or vice versa.
_tokenless_cwds: set[str | None] = set()


def _token() -> str | None:
    global _token_cache
    with _lock:
        now = time.monotonic()
        if _token_cache is None or now >= _token_cache[1]:
            code, out = _run(["gh", "auth", "token"], env_overrides=_QUIET)
            token = out if code == 0 and out else None
            _token_cache = (now, now + (_TOKEN_TTL_S if token else _RETRY_S), token)
        return _token_cache[2]


def run(*args: str, cwd: str | None = None, timeout: float = 3.0) -> tuple[int, str]:
    """`gh <args>` — (returncode, stdout, or stderr on failure), as tmux._run."""
    global _token_cache
    if not AVAILABLE:
        return 127, "gh not installed"
    token = None if cwd in _tokenless_cwds else _token()
    env_overrides = dict(_QUIET)
    if token:
        env_overrides["GH_TOKEN"] = token
    code, out = _run(["gh", *args], cwd=cwd, timeout=timeout, env_overrides=env_overrides)
    if code != 0 and token and _AUTH_FAILED_RE.search(out):
        # A revoked or rotated token: read it again once _RETRY_S has passed
        # since the last read.
        with _lock:
            if _token_cache is not None:
                read_at, good_until, cached = _token_cache
                _token_cache = (read_at, min(good_until, read_at + _RETRY_S), cached)
        # GH_TOKEN applies to github.com AND any *.ghe.com host, ahead of
        # stored credentials — a token for the other one 401s on every call
        # here. Retry once without it; on success, skip the token for this
        # cwd from now on instead of 401ing first every time.
        retry_code, retry_out = _run(["gh", *args], cwd=cwd, timeout=timeout, env_overrides=_QUIET)
        if retry_code == 0:
            with _lock:
                _tokenless_cwds.add(cwd)
        return retry_code, retry_out
    return code, out
