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

import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from periscope import activity
from periscope.log import _bg


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
_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_plan_cache: tuple[float, dict | None] = (0.0, None)
_plan_in_flight = False
_plan_lock = threading.Lock()

# (response field, meter key, display label) — response fields that are null
# (e.g. seven_day_opus on plans without an Opus meter) are skipped.
_PLAN_METERS = [
    ("five_hour", "session", "Current session"),
    ("seven_day", "week_all", "Current week (all models)"),
    ("seven_day_opus", "week_opus", "Current week (Opus only)"),
    ("seven_day_sonnet", "week_sonnet", "Current week (Sonnet only)"),
]


def _read_oauth_token() -> str | None:
    """Read Claude Code's OAuth access token from the macOS Keychain.
    Returns None when missing or expired — we never refresh it ourselves;
    any running Claude Code session keeps it fresh."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
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


def parse_plan_usage(data: dict) -> dict:
    """Map the OAuth usage response onto the dashboard's meters shape."""
    meters: dict[str, dict] = {}
    for field, key, label in _PLAN_METERS:
        entry = data.get(field)
        if not isinstance(entry, dict) or entry.get("utilization") is None:
            continue
        resets_at = None
        rs = entry.get("resets_at")
        if isinstance(rs, str):
            try:
                resets_at = int(datetime.fromisoformat(rs).timestamp())
            except ValueError:
                pass
        meters[key] = {
            "label": label,
            "percent": round(float(entry["utilization"])),
            "utilization": float(entry["utilization"]),
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
#     samples over the last hour (6h for weeklies — 1% of a week is too
#     coarse for an hourly slope). projected_recent extrapolates that rate
#     to reset; limit_at is when it crosses 100%, reported only when that
#     lands before resets_at (a pace that hits 100% after reset never blows
#     the limit). Usage is bursty, so avg and recent disagree often — the
#     frontend renders both as a range instead of pretending one number.
#
#   hot — recent rate >= 2x the even-burn rate for the window (the pace
#     that would consume exactly 100% by reset). The burst signal: 🔥.

_METER_WINDOW_S = {
    "session": 5 * 3600,
    "week_all": 7 * 86400,
    "week_opus": 7 * 86400,
    "week_sonnet": 7 * 86400,
}
_SLOPE_WINDOW_S = {
    "session": 3600,
    "week_all": 6 * 3600,
    "week_opus": 6 * 3600,
    "week_sonnet": 6 * 3600,
}
_MIN_ELAPSED_FRAC = 0.05
_MIN_SLOPE_SPAN_S = 600


def attach_projections(meters: dict, now: float,
                       samples_for=None) -> None:
    """Annotate each meter dict in place with projected_percent / limit_at.
    samples_for(meter_key, since) -> [(at, percent)] is injectable for tests;
    defaults to the persisted usage_samples series."""
    if samples_for is None:
        samples_for = activity.usage_samples_since
    for key, m in meters.items():
        m["projected_percent"] = None
        m["projected_recent"] = None
        m["limit_at"] = None
        m["hot"] = False
        window = _METER_WINDOW_S.get(key)
        resets_at = m.get("resets_at")
        if not window or not resets_at:
            continue
        window_start = resets_at - window
        elapsed = now - window_start
        if window >= elapsed >= window * _MIN_ELAPSED_FRAC:
            m["projected_percent"] = round(m["utilization"] * window / elapsed)
        since = int(max(window_start, now - _SLOPE_WINDOW_S[key]))
        samples = samples_for(key, since)
        if len(samples) < 2:
            continue
        (t0, p0), (t1, p1) = samples[0], samples[-1]
        if t1 - t0 < _MIN_SLOPE_SPAN_S or p1 <= p0:
            continue
        rate = (p1 - p0) / (t1 - t0)
        m["projected_recent"] = round(m["utilization"] + rate * (resets_at - now))
        m["hot"] = rate >= 2 * 100.0 / window
        eta = max(now, now + (100.0 - m["utilization"]) / rate)
        if eta < resets_at:
            m["limit_at"] = int(eta)


def fetch_plan_usage() -> dict | None:
    token = _read_oauth_token()
    if not token:
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
    except Exception:
        return None


def _refresh_plan_usage_into_cache() -> None:
    global _plan_cache, _plan_in_flight
    try:
        result = fetch_plan_usage()
        if result:
            now = int(time.time())
            activity.record_usage_samples([
                (now, k, m["utilization"], m.get("resets_at"))
                for k, m in result["meters"].items()
            ])
            attach_projections(result["meters"], now)
    except Exception:
        result = None
    with _plan_lock:
        if result:
            _plan_cache = (time.time(), result)
        else:
            # Stamp the timestamp even on failure so cached_plan_usage backs
            # off for PLAN_USAGE_REFRESH_S instead of re-hitting the endpoint
            # on every poll (it 429s readily). Keep the previously-cached data.
            _plan_cache = (time.time(), _plan_cache[1])
        _plan_in_flight = False


def cached_plan_usage() -> dict | None:
    """Stale-while-revalidate: serves the last successful fetch immediately
    and kicks off a background refresh whenever the cache is older than
    PLAN_USAGE_REFRESH_S. First-ever call returns None; the dashboard's
    next poll will see the freshly-cached result."""
    global _plan_in_flight
    now = time.time()
    with _plan_lock:
        ts, data = _plan_cache
        if now - ts < PLAN_USAGE_REFRESH_S:
            return data
        if not _plan_in_flight:
            _plan_in_flight = True
            _bg("plan-usage", _refresh_plan_usage_into_cache)
        return data


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
            jsonl = turns._jsonl_for_session(sid) if sid else None
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
    plan = cached_plan_usage() or {}
    if not ((plan.get("meters") or {}).get("session") or {}).get("hot"):
        return
    ids = [v["pane_id"] for v in views if v.get("is_claude") and v.get("pane_id")]
    rates = pane_burn_rates(ids)
    total = sum(rates.get(i, 0.0) for i in ids)
    if total <= 0:
        return
    for v in views:
        r = rates.get(v.get("pane_id") or "", 0.0)
        if r / total >= _HOT_PANE_SHARE:
            v["burn_hot"] = True
            v["burn_wtpm"] = round(r)
