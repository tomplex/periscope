"""Claude usage tracking: two parallel paths.

(1) JSONL parsing: walks ~/.claude/projects/*/*.jsonl and sums token usage
    in the current 5-hour window. Cheap, no IO with Claude. Returns
    approximate numbers (input/output/cache_creation/cache_read tokens).

(2) Plan usage from Anthropic's OAuth usage endpoint — the same data that
    powers Claude Code's /usage screen (session %, weekly %s, exact reset
    timestamps). Authoritative; refreshed every 5 minutes in a background
    thread.

The dashboard prefers (2) when available, falls back to (1).
"""

import contextlib
import hashlib
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from periscope import activity, store
from periscope.log import _bg, log

# --- Claude Code plan usage (parsed from session JSONL files) -------------
#
# Claude Code logs every assistant message to ~/.claude/projects/<encoded-cwd>/
# <session-id>.jsonl. Each line is a JSON record; assistant lines carry a
# `message.usage` block with input_tokens, cache_creation_input_tokens,
# cache_read_input_tokens, and output_tokens. Summing across files in a
# rolling 5h window gives a real measurement of plan token usage, no API
# subscription / billing endpoint required.

_USAGE_TTL = 30.0
_usage_cache: tuple[float, dict] | None = None
_usage_lock = threading.Lock()
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def compute_claude_usage(window_hours: float = 5.0) -> dict:
    """Walk every recent session JSONL and sum token usage in the window."""
    if not _CLAUDE_PROJECTS.exists():
        return {"available": False}

    cutoff = time.time() - window_hours * 3600
    fresh = cache_w = cache_r = out = msgs = 0
    earliest_msg_ts: float | None = None

    for jsonl in _CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            with jsonl.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = rec.get("timestamp")
                    if not isinstance(ts_str, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if ts < cutoff:
                        continue
                    usage = ((rec.get("message") or {}).get("usage")) or {}
                    if not usage:
                        continue
                    fresh += int(usage.get("input_tokens") or 0)
                    cache_w += int(usage.get("cache_creation_input_tokens") or 0)
                    cache_r += int(usage.get("cache_read_input_tokens") or 0)
                    out += int(usage.get("output_tokens") or 0)
                    msgs += 1
                    if earliest_msg_ts is None or ts < earliest_msg_ts:
                        earliest_msg_ts = ts
        except OSError:
            continue

    # The plan's 5h rolling reset is anchored at the *first* message of the
    # window, so the next reset is window_hours after the earliest in-window
    # message (not "now + 5h"). If we found nothing, the window is wide open.
    reset_at = int(earliest_msg_ts + window_hours * 3600) if earliest_msg_ts else None
    return {
        "available": True,
        "window_hours": window_hours,
        "messages": msgs,
        "input_tokens": fresh,
        "cache_creation_tokens": cache_w,
        "cache_read_tokens": cache_r,
        "output_tokens": out,
        "total_tokens": fresh + cache_w + cache_r + out,
        "reset_at": reset_at,
    }


def cached_claude_usage() -> dict:
    global _usage_cache
    now = time.time()
    with _usage_lock:
        if _usage_cache and now - _usage_cache[0] < _USAGE_TTL:
            return _usage_cache[1]
    data = compute_claude_usage()
    with _usage_lock:
        _usage_cache = (now, data)
    return data


# --- Authoritative plan usage from the OAuth usage endpoint ---
#
# The JSONL aggregation above is a free local approximation. The real numbers
# (session %, weekly %s) live server-side at Anthropic, behind the same
# undocumented endpoint Claude Code's /usage screen renders from:
# GET https://api.anthropic.com/api/oauth/usage, authenticated with the OAuth
# access token Claude Code keeps in the macOS Keychain. The User-Agent must
# identify as claude-code/<version> — anonymous clients land in an
# aggressively rate-limited bucket and get persistent 429s.

PLAN_USAGE_REFRESH_S = 300.0
# Failed fetches retry sooner: post-wake the network is often not up yet for
# the first attempt, and a full 5-minute backoff pins the dashboard to
# yesterday's numbers exactly when the user sits down.
PLAN_USAGE_RETRY_S = 60.0
_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# account id -> (next attempt eligible at, last successful data). Keyed per
# account because each subscription has its own credential and its own meters;
# a single tuple would have whichever account refreshed last overwrite the
# other's numbers every 5 minutes.
_plan_cache: dict[str, tuple[float, dict | None]] = {}
_plan_in_flight: set[str] = set()
_plan_lock = threading.Lock()

# (response field, meter key, display label) — response fields that are null
# (e.g. seven_day_opus on plans without an Opus meter) are skipped.
_PLAN_METERS = [
    ("five_hour", "session", "Current session"),
    ("seven_day", "week_all", "Current week (all models)"),
    ("seven_day_opus", "week_opus", "Current week (Opus only)"),
    ("seven_day_sonnet", "week_sonnet", "Current week (Sonnet only)"),
]


def _keychain_item(config_dir: str) -> str:
    """The keychain item holding an account's OAuth credential.

    Claude Code namespaces the item by config dir — verified live on this
    machine: the default dir uses the bare name, any other appends the
    sha256[:8] of the CLAUDE_CONFIG_DIR path ("/Users/tom/.claude-b" ->
    "-f79ff3dd", which returned account B's meters where the bare item
    returned account A's). Computing the name means adding a second
    subscription needs no keychain-discovery step.
    """
    if not config_dir:
        return "Claude Code-credentials"
    digest = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _read_oauth_token(config_dir: str = "") -> str | None:
    """Read one account's OAuth access token from the macOS Keychain.
    Returns None when missing or expired — we never refresh it ourselves;
    any running Claude Code session keeps it fresh."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", _keychain_item(config_dir), "-w"],
            capture_output=True, text=True, timeout=5,
        )
        creds = json.loads(out.stdout)["claudeAiOauth"]
        if creds.get("expiresAt", 0) / 1000 <= time.time():
            return None
        return creds["accessToken"]
    except Exception:
        return None


_claude_version: str | None = None


def _claude_user_agent() -> str:
    global _claude_version
    if _claude_version is None:
        try:
            out = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            m = re.search(r"(\d+\.\d+\.\d+)", out.stdout)
            _claude_version = m.group(1) if m else "2.0.0"
        except Exception:
            # launchd's minimal PATH may not resolve `claude`; a plausible
            # version string still lands in the friendly rate-limit bucket.
            _claude_version = "2.0.0"
    return f"claude-code/{_claude_version}"


_KNOWN_USAGE_FIELDS = {f for f, _, _ in _PLAN_METERS} | {"extra_usage"}
_warned_unknown_fields: set[str] = set()
# Keychain items already reported as tokenless, so the warning stays evidence
# rather than becoming per-minute noise (see fetch_plan_usage).
_warned_missing_token: set[str] = set()


def parse_plan_usage(data: dict) -> dict:
    """Map the OAuth usage response onto the dashboard's meters shape."""
    # The response carries codename fields for unreleased meters (all null
    # until Anthropic ships them — seven_day_opus appeared this way). Warn
    # once per process when one goes live so a new per-model meter (Fable?)
    # doesn't get silently dropped from the dashboard.
    for field, entry in data.items():
        if (field not in _KNOWN_USAGE_FIELDS
                and field not in _warned_unknown_fields
                and isinstance(entry, dict)
                and entry.get("utilization") is not None):
            _warned_unknown_fields.add(field)
            log.warning("usage endpoint has live unmapped meter %r: %s",
                        field, entry)
    meters: dict[str, dict] = {}
    for field, key, label in _PLAN_METERS:
        entry = data.get(field)
        if not isinstance(entry, dict) or entry.get("utilization") is None:
            continue
        resets_at = None
        rs = entry.get("resets_at")
        if isinstance(rs, str):
            with contextlib.suppress(ValueError):
                resets_at = int(datetime.fromisoformat(rs).timestamp())
        meters[key] = {
            "label": label,
            "percent": round(float(entry["utilization"])),
            "utilization": float(entry["utilization"]),
            "resets_at": resets_at,
        }
    # Per-model weekly sub-limits (e.g. Fable's "up to 50% of your weekly
    # limit") arrive as scoped entries in the `limits` array — NOT as a
    # top-level meter field, so the _PLAN_METERS loop above never sees them.
    # scope.model.display_name names the model; surface each active one as its
    # own meter keyed week_<slug>.
    for lim in data.get("limits", []):
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        pct = float(lim.get("percent") or 0)
        # Show once there's any usage OR the sub-limit is the binding constraint.
        # `is_active` alone means "currently-binding limit" (like weekly_all),
        # so it stays False while Fable usage accumulates below its cap —
        # gating on it alone would hide the meter during exactly that window.
        if not model or not (lim.get("is_active") or pct > 0):
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
        resets_at = None
        rs = lim.get("resets_at")
        if isinstance(rs, str):
            with contextlib.suppress(ValueError):
                resets_at = int(datetime.fromisoformat(rs).timestamp())
        meters[f"week_{slug}"] = {
            "label": f"Current week ({model} only)",
            "percent": round(pct),
            "utilization": pct,
            "resets_at": resets_at,
        }
    return {"available": bool(meters), "meters": meters}


# --- "On track to blow the limit" projections ---
#
# Two heuristics per meter, attached at fetch time (so at most 5 min stale):
#
#   projected_percent — average pace. The meter's window has a known length
#     and a known end (resets_at), so percent / fraction-of-window-elapsed
#     is where you land at reset if the whole-window average continues.
#     Suppressed early in the window (elapsed < 5%) where the ratio explodes.
#
#   projected_recent / limit_at — recent burn rate. Slope of the persisted
#     samples: last hour for the session meter (5h horizon — you're at the
#     desk, extrapolating active burn is right), last 24h for the weeklies.
#     The 24h window spans a full sleep/work cycle, so the weekly slope is
#     duty-cycle-adjusted for free — a shorter window measures active-hours
#     burn and extrapolating THAT across every remaining wall-clock hour of
#     the week over-projects ~3x. projected_recent extrapolates the rate to
#     reset; limit_at is when it crosses 100%, reported only when that lands
#     before resets_at (a pace that hits 100% after reset never blows the
#     limit). Usage is bursty, so avg and recent disagree often — the
#     frontend renders both as a range instead of pretending one number.
#     Each meter needs samples spanning at least half its slope window
#     before recent-burn outputs activate (12h for weeklies).
#
#   hot — recent rate >= 2x the even-burn rate for the window (the pace
#     that would consume exactly 100% by reset). The burst signal: 🔥.
#     With the 24h weekly slope this means "burning >2x sustainable for a
#     full day", not "had a hot morning".

_METER_WINDOW_S = {
    "session": 5 * 3600,
    "week_all": 7 * 86400,
    "week_opus": 7 * 86400,
    "week_sonnet": 7 * 86400,
}
_SLOPE_WINDOW_S = {
    "session": 3600,
    "week_all": 24 * 3600,
    "week_opus": 24 * 3600,
    "week_sonnet": 24 * 3600,
}
_MIN_ELAPSED_FRAC = 0.05
_MIN_SLOPE_SPAN_S = {
    "session": 600,
    "week_all": 12 * 3600,
    "week_opus": 12 * 3600,
    "week_sonnet": 12 * 3600,
}


def attach_projections(meters: dict, now: float, samples_for=None,
                       account: str = "default") -> None:
    """Annotate each meter dict in place with projected_percent / limit_at.
    samples_for(meter_key, since) -> [(at, percent)] is injectable for tests;
    defaults to the persisted usage_samples series for `account`. The seam
    stays account-free so injected fakes don't have to model the registry."""
    fetch = samples_for or (
        lambda key, since: activity.usage_samples_since(account, key, since))
    for key, m in meters.items():
        m["projected_percent"] = None
        m["projected_recent"] = None
        m["limit_at"] = None
        m["hot"] = False
        # Scoped model meters (week_fable, ...) are keyed dynamically, so they
        # miss the static per-key window dicts — fall back to the weekly cadence.
        weekly = key.startswith("week_")
        window = _METER_WINDOW_S.get(key) or (7 * 86400 if weekly else None)
        resets_at = m.get("resets_at")
        if not window or not resets_at:
            continue
        window_start = resets_at - window
        elapsed = now - window_start
        if window >= elapsed >= window * _MIN_ELAPSED_FRAC:
            m["projected_percent"] = round(m["utilization"] * window / elapsed)
        slope_win = _SLOPE_WINDOW_S.get(key, 24 * 3600 if weekly else 3600)
        min_span = _MIN_SLOPE_SPAN_S.get(key, 12 * 3600 if weekly else 600)
        since = int(max(window_start, now - slope_win))
        samples = fetch(key, since)
        if len(samples) < 2:
            continue
        (t0, p0), (t1, p1) = samples[0], samples[-1]
        if t1 - t0 < min_span or p1 <= p0:
            continue
        rate = (p1 - p0) / (t1 - t0)
        m["projected_recent"] = round(m["utilization"] + rate * (resets_at - now))
        m["hot"] = rate >= 2 * 100.0 / window
        eta = max(now, now + (100.0 - m["utilization"]) / rate)
        if eta < resets_at:
            m["limit_at"] = int(eta)


def fetch_plan_usage(config_dir: str = "") -> dict | None:
    # Failures warn rather than pass silently: a broken token or endpoint
    # would otherwise serve stale meters forever with zero log evidence
    # (httpx only logs requests that get a response). Attempt rate is capped
    # by the cache backoff, so this can't spam.
    token = _read_oauth_token(config_dir)
    if not token:
        # Once per item, not once per attempt: a registered account that was
        # never logged in has no keychain item at all, and it re-attempts every
        # PLAN_USAGE_RETRY_S — ~1400 identical lines a day otherwise.
        item = _keychain_item(config_dir)
        if item not in _warned_missing_token:
            _warned_missing_token.add(item)
            log.warning("plan usage: no valid OAuth token in keychain (%s)", item)
        return None
    try:
        resp = httpx.get(
            _OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": _claude_user_agent(),
            },
            timeout=10,
        )
        resp.raise_for_status()
        return parse_plan_usage(resp.json())
    except Exception as e:
        log.warning("plan usage fetch failed: %r", e)
        return None


def _refresh_plan_usage_into_cache(account: str, config_dir: str) -> None:
    try:
        result = fetch_plan_usage(config_dir)
        if result:
            now = int(time.time())
            result["fetched_at"] = now
            activity.record_usage_samples([
                (now, account, k, m["utilization"], m.get("resets_at"))
                for k, m in result["meters"].items()
            ])
            attach_projections(result["meters"], now, account=account)
    except Exception:
        log.exception("plan usage: sample recording/projection failed")
        result = None
    with _plan_lock:
        if result:
            _plan_cache[account] = (time.time() + PLAN_USAGE_REFRESH_S, result)
        else:
            # Back off even on failure instead of re-hitting the endpoint on
            # every poll (it 429s readily), but at the shorter retry interval.
            # Keep the previously-cached data; the frontend renders its
            # fetched_at as a staleness marker.
            prev = _plan_cache.get(account, (0.0, None))[1]
            _plan_cache[account] = (time.time() + PLAN_USAGE_RETRY_S, prev)
        _plan_in_flight.discard(account)


def cached_plan_usage() -> dict[str, dict]:
    """Stale-while-revalidate, per account: {account_id: {available, meters,
    fetched_at}}. Serves each account's last successful fetch immediately and
    kicks off that account's background refresh whenever its next-attempt time
    has passed (PLAN_USAGE_REFRESH_S after a success, PLAN_USAGE_RETRY_S after
    a failure).

    Every registered account gets an entry every call, so one account's dead
    credential (missing keychain item, expired token) shows as
    {"available": False} instead of dropping the key and taking the other
    account's meters down with it. Before the first successful fetch that same
    shape stands in; the dashboard's next poll sees the real numbers.
    """
    now = time.time()
    out: dict[str, dict] = {}
    for acct in store.get_accounts():
        aid = acct.get("id")
        if not aid:
            continue
        with _plan_lock:
            next_at, data = _plan_cache.get(aid, (0.0, None))
            if now >= next_at and aid not in _plan_in_flight:
                _plan_in_flight.add(aid)
                _bg(f"plan-usage:{aid}", _refresh_plan_usage_into_cache,
                    aid, acct.get("config_dir", ""))
        out[aid] = data if data else {"available": False}
    return out


# --- Per-pane burn attribution ---
#
# Which pane is eating the quota? Each Claude pane maps to its session JSONL
# (pane_sessions table, via periscope.turns); summing the usage blocks in the
# last 30 minutes gives a per-pane burn rate. Tokens are weighted to roughly
# match how the plan meters count them — output dominates, cache reads are
# nearly free. The absolute scale is meaningless (Anthropic's weighting is
# opaque); only the per-pane SHARES are used, so weighting errors mostly
# cancel.

PANE_BURN_REFRESH_S = 60.0
_PANE_BURN_WINDOW_S = 1800
_BURN_TAIL_BYTES = 4_000_000
_HOT_PANE_SHARE = 0.4
_W_INPUT, _W_CACHE_W, _W_OUT, _W_CACHE_R = 1.0, 1.25, 5.0, 0.1

_burn_cache: tuple[float, dict[str, float]] = (0.0, {})
_burn_in_flight = False
_burn_lock = threading.Lock()


def _weighted_burn_from_jsonl(path: Path, cutoff: float) -> float:
    """Weighted token total from usage records newer than cutoff. Bounded
    tail read — transcripts can be tens of MB and 4MB comfortably covers a
    heavy 30 minutes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _BURN_TAIL_BYTES:
                f.seek(size - _BURN_TAIL_BYTES)
                f.readline()  # discard the partial line
            data = f.read()
    except OSError:
        return 0.0
    total = 0.0
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts_str = rec.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        u = ((rec.get("message") or {}).get("usage")) or {}
        if not u:
            continue
        total += (_W_INPUT * int(u.get("input_tokens") or 0)
                  + _W_CACHE_W * int(u.get("cache_creation_input_tokens") or 0)
                  + _W_OUT * int(u.get("output_tokens") or 0)
                  + _W_CACHE_R * int(u.get("cache_read_input_tokens") or 0))
    return total


def _refresh_burn_into_cache(pane_ids: list[str]) -> None:
    global _burn_cache, _burn_in_flight
    from periscope import turns
    rates: dict[str, float] = {}
    try:
        cutoff = time.time() - _PANE_BURN_WINDOW_S
        for pid in pane_ids:
            sid = turns.session_id_for_pane(pid)
            jsonl = turns.jsonl_for_session(sid) if sid else None
            if jsonl:
                rates[pid] = _weighted_burn_from_jsonl(jsonl, cutoff) / (
                    _PANE_BURN_WINDOW_S / 60.0)
    finally:
        with _burn_lock:
            _burn_cache = (time.time(), rates)
            _burn_in_flight = False


def pane_burn_rates(pane_ids: list[str]) -> dict[str, float]:
    """Stale-while-revalidate: weighted tokens/min per pane over the last
    30 minutes. Serves the last computed rates immediately; refreshes in a
    background thread when older than PANE_BURN_REFRESH_S."""
    global _burn_in_flight
    now = time.time()
    with _burn_lock:
        ts, rates = _burn_cache
        if now - ts >= PANE_BURN_REFRESH_S and not _burn_in_flight:
            _burn_in_flight = True
            _bg("pane-burn", _refresh_burn_into_cache, list(pane_ids))
        return rates


def annotate_hot_panes(views: list[dict]) -> None:
    """Stamp burn_hot/burn_wtpm on the pane views eating the quota: only
    while the session meter is hot, and only on panes carrying >=40% of the
    current weighted burn across Claude panes (so at most two flames)."""
    # Default account only. Burn is measured from activity._PROJECTS_DIR
    # (~/.claude/projects), which holds the default account's transcripts and
    # nothing else — a second account's hot meter has no measurable panes here,
    # so gating on it would flame panes that provably aren't the cause. Widen
    # this when per-account transcript discovery lands.
    plan = cached_plan_usage().get("default") or {}
    if not ((plan.get("meters") or {}).get("session") or {}).get("hot"):
        return
    ids = [
        v["pane_id"]
        for v in views
        if v.get("agent") == "claude" and v.get("pane_id")
    ]
    rates = pane_burn_rates(ids)
    total = sum(rates.get(i, 0.0) for i in ids)
    if total <= 0:
        return
    for v in views:
        r = rates.get(v.get("pane_id") or "", 0.0)
        if r / total >= _HOT_PANE_SHARE:
            v["burn_hot"] = True
            v["burn_wtpm"] = round(r)
