# Account & model defaults — Unit 3: move-account override + waste marker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A walled pane can be moved to the other account even when the transcript looks live (the 60s mtime guard misreads the limit-reached message as activity) — the client turns that specific 409 into a "Move anyway?" confirm; and the usage pill marks an account 💤 when its Fable budget is on pace to go unused at reset.

**Architecture:** `_window_new_resume` gains `force: bool = False` that skips only the mtime guard (never the already-resumed guard) and the 409 detail carries the age; `pane_move_account` passes it through. `Rail.movePaneAccount` calls the endpoint via `modalRequest` (which gains a `status` field), confirms on that 409, and retries once with `force=1`. `usageSummary.wasteMark(acct, nowSec)` is a pure four-line client rule rendered by `UsagePill`. No Python outside `routes/sessions.py`.

**Tech Stack:** FastAPI / pytest, Preact / vitest, Vite build to the committed `static/dist/app.js`.

**Spec:** `docs/superpowers/specs/2026-09-08-account-defaults-design.md` (D7, D8, §Move-account override). **Structure:** `docs/superpowers/specs/2026-09-08-account-defaults-structure.md` (Unit 3, P7, P8, C4–C6). **Code is independent of Units 1 and 2**; the one coupling is documentation — Task 4 appends to `docs/account-routing.md`, which Unit 1 creates. This pipeline runs Unit 3 after Unit 1, so Task 4 appends; the step says what to do if the file is absent.

**Read before starting:** `CLAUDE.md`, `docs/testing.md`, `docs/second-account-setup.md` (why a session resumes on either account).

**Verification commands:** `uv run pytest tests/routes/test_sessions.py -q`, `uv run pytest -q`, `bin/check`, `npm test`, `npm run build`.

---

## File map

| File | Change |
|---|---|
| `periscope/routes/sessions.py` | `_window_new_resume(force=)`, 409 detail carries the age, `pane_move_account(force=)` |
| `static/src/overlays/modalRequest.js` | failure shape gains `status` |
| `static/src/split/Rail.jsx` | `movePaneAccount` confirm + one forced retry |
| `static/src/chrome/usageSummary.js` | `wasteMark` |
| `static/src/chrome/UsagePill.jsx` | 💤 on the account row + tooltip line |
| `static/styles.css` | `.usage-acct-waste` |
| `tests/routes/test_sessions.py`, `static/src/chrome/__tests__/usageSummary.test.js`, `static/src/chrome/__tests__/usagePillRender.test.jsx`, `static/src/overlays/__tests__/modalRequest.test.js` (NEW) | tests |
| `docs/account-routing.md` | the override and the marker |

---

### Task 1: `force` on `_window_new_resume` and `/api/pane/move-account`

**Files:**
- Modify: `periscope/routes/sessions.py:151-152,170-180,508-509,542-545`
- Test: `tests/routes/test_sessions.py` (after `test_window_new_resume_unknown_everywhere_still_404s`, and after `test_move_account_rejects_unknown_account`)

- [ ] **Step 1: Write the failing tests**

Insert after `test_window_new_resume_unknown_everywhere_still_404s`:

```python
def _live_jsonl(tmp_path, *, age_s):
    """A transcript on disk written `age_s` seconds ago (the liveness guard reads mtime)."""
    import json
    import os
    import time
    jsonl = tmp_path / "abc.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "cwd": str(tmp_path)}) + "\n")
    os.utime(jsonl, (time.time() - age_s, time.time() - age_s))
    return jsonl


def test_window_new_resume_refuses_a_recently_written_transcript_and_says_how_recent(mocker, tmp_path):
    import re

    import pytest
    from fastapi import HTTPException

    from periscope.routes import sessions
    calls: list[tuple] = []
    _patch_resume_path(mocker, calls)
    mocker.patch("history.search.get_session", return_value=None)
    _patch(mocker, "jsonl_for_session", return_value=_live_jsonl(tmp_path, age_s=12))
    with pytest.raises(HTTPException) as e:
        sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume")
    assert e.value.status_code == 409
    # The client matches on this prefix (Rail.movePaneAccount) — keep them in sync.
    assert e.value.detail.startswith("session looks live")
    # ...and the detail carries the measured age (12s here), not a constant.
    assert re.match(r"session looks live \(written 1[23]s ago\); wait a minute", e.value.detail)
    assert not [c for c in calls if c and c[0] == "new-window"]


def test_window_new_resume_force_skips_only_the_mtime_guard(mocker, tmp_path):
    """Claude writes the limit-reached message INTO the transcript, so a walled
    pane — the one the user most wants to move — always looks live. `force`
    is the override for exactly that; it must not widen anything else."""
    import pytest
    from fastapi import HTTPException

    from periscope.routes import sessions
    calls: list[tuple] = []
    _patch_resume_path(mocker, calls)
    mocker.patch("history.search.get_session", return_value=None)
    _patch(mocker, "jsonl_for_session", return_value=_live_jsonl(tmp_path, age_s=12))

    result = sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume",
                                         force=True)
    assert result["ok"] is True
    assert [c for c in calls if c and c[0] == "new-window"]

    # The already-resumed-elsewhere guard is NOT behind force: two concurrent
    # appenders interleaving into one JSONL is a different failure entirely.
    # The successful call above registered the session (sessions.py:253-254).
    assert "abc" in sessions._resuming
    with pytest.raises(HTTPException) as e:
        sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume",
                                    force=True)
    assert e.value.status_code == 409
    assert "already resumed" in e.value.detail
```

Insert after `test_move_account_rejects_unknown_account`:

```python
def test_move_account_passes_force_through_and_defaults_it_off(client, mocker):
    resume, _move = _patch_move_account(mocker)
    r = client.post("/api/pane/move-account?pid=aa11&account=b")
    assert r.status_code == 200
    assert resume.call_args.kwargs.get("force") is False
    r = client.post("/api/pane/move-account?pid=aa11&account=b&force=1")
    assert r.status_code == 200
    assert resume.call_args.kwargs.get("force") is True


def test_move_account_force_does_not_widen_the_account_check(client, mocker):
    resume, _move = _patch_move_account(mocker)
    r = client.post("/api/pane/move-account?pid=aa11&account=nope&force=1")
    assert r.status_code == 400
    resume.assert_not_called()
```

(`_resuming` entries are `{"target": ..., "started_at": ...}` — `sessions.py:227,254`; the guard reads only `existing["target"]`. `track` is already imported in `Rail.jsx:30`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/routes/test_sessions.py -q -k "force or recently_written"`
Expected: `TypeError: _window_new_resume() got an unexpected keyword argument 'force'`; the detail-prefix test fails on `"session looks live; wait..."` (no age).

- [ ] **Step 3: Implement**

`_window_new_resume` signature:
```python
def _window_new_resume(session: str, exec_cmd: str, resume_id: str | None, mode: str,
                       account: str | None = None, force: bool = False) -> dict:
```
Add to its docstring, after the `account` paragraph:
```
    `force` skips ONLY the transcript-mtime liveness guard. Claude writes the
    limit-reached message into the JSONL, so a walled pane — the one the user
    most wants to move off an exhausted subscription — always looks live to
    that guard. The already-resumed-elsewhere guard is never skipped: two
    concurrent appenders interleaving into one JSONL is a different failure.
```
Replace the mtime block:
```python
    # Liveness guard: refuse if the jsonl was written to in the last 60s
    # (the session may be currently active in another window/process, and
    # two concurrent appenders would interleave into the same JSONL). The
    # detail's "session looks live" prefix is what Rail.movePaneAccount
    # matches to offer "move anyway" — change both or neither.
    if resume_sess["jsonl_path"] and os.path.isfile(resume_sess["jsonl_path"]):
        mtime_age = time.time() - os.path.getmtime(resume_sess["jsonl_path"])
        if not force and mtime_age < 60:
            raise HTTPException(
                409, f"session looks live (written {int(mtime_age)}s ago); "
                     "wait a minute or pick another")
    # Already resumed elsewhere in this periscope process? Deliberately NOT
    # behind `force`.
    if resume_id in _resuming:
```

`pane_move_account`:
```python
@router.post("/api/pane/move-account")
def pane_move_account(pid: str, account: str, force: bool = False):
```
Add to its docstring's last paragraph: `` `force=1` bypasses the mtime guard only — the client sends it after the user confirms "move anyway" on that 409. `` And the call becomes `"resume", account=..., force=force,` (keep whatever `account=` expression is there after Unit 1).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/routes/test_sessions.py -q`
Expected: all pass.

Run: `bin/check` — Expected: zero violations.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "move-account: force=1 skips only the transcript-mtime guard (a walled pane always looks live); the 409 detail carries the age"
```

---

### Task 2: `modalRequest` reports the status; `Rail.movePaneAccount` confirms and retries

**Files:**
- Modify: `static/src/overlays/modalRequest.js`, `static/src/split/Rail.jsx:27-31,262-279`
- Test: `static/src/overlays/__tests__/modalRequest.test.js` (NEW)

- [ ] **Step 1: Write the failing test**

```js
// static/src/overlays/__tests__/modalRequest.test.js
// modalRequest is the no-toast fetch helper; Rail.movePaneAccount relies on
// its failure shape carrying the HTTP status so a 409 can become a confirm.
import { afterEach, describe, expect, it, vi } from "vitest";
import { modalRequest } from "../modalRequest.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

function respond(status, body) {
  vi.stubGlobal("fetch", async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

describe("modalRequest", () => {
  it("returns {data} on success", async () => {
    respond(200, { pid: "bb22" });
    expect(await modalRequest("move", "/x")).toEqual({ data: { pid: "bb22" } });
  });

  it("returns the route's detail and the status on an HTTP error", async () => {
    respond(409, { detail: "session looks live (written 12s ago); wait a minute or pick another" });
    const r = await modalRequest("move", "/x");
    expect(r.status).toBe(409);
    expect(r.error).toMatch(/^session looks live/);
    expect(r.data).toBeUndefined();
  });

  it("has no status on a network failure", async () => {
    vi.stubGlobal("fetch", async () => { throw new Error("offline"); });
    const r = await modalRequest("move", "/x");
    expect(r.error).toBe("move failed: offline");
    expect(r.status).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- modalRequest`
Expected: the 409 case fails (`status` undefined).

- [ ] **Step 3: `modalRequest.js`** — change the HTTP-error return to:

```js
  if (!res.ok) {
    // `status` rides along so a caller can treat one code specially (Rail's
    // move-account turns a 409 into a confirm); every existing caller reads
    // only `.error` / `.data`.
    return { error: data.detail || `${label} failed: HTTP ${res.status}`, status: res.status };
  }
```
and update the header comment's "Returns..." paragraph to: `Returns {data} on success, or {error, status?} on failure (status only for an HTTP error, not a network one) so callers early-return on a missing data.`

- [ ] **Step 4: `Rail.jsx`**

Imports: add `import { modalRequest } from "../overlays/modalRequest.js";` after the `confirmDialog` import, and `import { showToast } from "../overlays/Toast.jsx";` after it.

Replace `movePaneAccount`:
```js
  // Re-open a pane's Claude session on the other subscription. NOT a migration:
  // the server spawns a SECOND pane resuming the same transcript under the
  // target account and leaves this one running, so nothing is lost if the
  // resume doesn't take.
  //
  // Goes through modalRequest, not apiCall: apiCall toasts every failure and
  // returns null, and the one failure this action must READ is the 409 "session
  // looks live" — Claude writes the limit-reached message into the transcript,
  // so a walled pane (the one most worth moving) always trips the server's
  // 60s mtime guard. That 409 becomes a confirm and one forced retry; the
  // `force` parameter bounds the recursion to a single level. Not
  // `danger: true`: the move is additive (the original pane stays open).
  async function movePaneAccount(w, accountId, force = false) {
    const url =
      `/api/pane/move-account?pid=${encodeURIComponent(w.pid)}&account=${encodeURIComponent(accountId)}` +
      (force ? "&force=1" : "");
    const { data, error, status } = await modalRequest("move account", url, { method: "POST" });
    // apiCall used to emit this on every call; keep the instrumentation event.
    track("api:move account", { path: url, method: "POST", ok: !!data });
    if (data?.pid) {
      // Select the pane we just made — same reason /api/open does it: a spawn
      // the user can't see reads as a no-op. It lands in this pane's own
      // track, so the selection moves one row, not across the rail.
      railSelection.value = `pane:${data.pid}`;
      prefs.setLastSelected({ kind: "pane", pid: data.pid });
      return;
    }
    if (status === 409 && !force && error?.startsWith("session looks live")) {
      const ok = await confirmDialog(
        `${error}\n\nMove anyway? The original pane stays open; if the session really is mid-turn, the two copies will interleave writes to one transcript.`,
        { okLabel: "Move anyway" }
      );
      if (ok) await movePaneAccount(w, accountId, true);
      return;
    }
    showToast(`move account failed: ${error || "unknown error"}`, "bad", 6000);
  }
```

(`confirmDialog(message, opts)` takes `okLabel` and an optional `danger` flag — `overlays/Dialog.jsx:42-46`; `danger` is deliberately omitted here. `"bad"` is the failure kind `apiCall` itself uses — `util.js:146,154`.)

- [ ] **Step 5: Run tests, lint**

Run: `npm test` — Expected: all pass.
Run: `bin/check` — Expected: zero violations.

- [ ] **Step 6: Commit (source; dist rebuilt in Task 4)**

```bash
git add static/src/overlays/modalRequest.js static/src/split/Rail.jsx static/src/overlays/__tests__/modalRequest.test.js
git commit -m "rail: 'session looks live' 409 on move-account becomes a Move-anyway confirm with one forced retry; modalRequest carries the status"
```

---

### Task 3: `wasteMark` + 💤 on the usage pill

**Files:**
- Modify: `static/src/chrome/usageSummary.js` (append), `static/src/chrome/UsagePill.jsx:111-120,141-166`, `static/styles.css:237`
- Test: `static/src/chrome/__tests__/usageSummary.test.js`, `static/src/chrome/__tests__/usagePillRender.test.jsx`

- [ ] **Step 1: Write the failing tests**

Append to `static/src/chrome/__tests__/usageSummary.test.js` (add `wasteMark` to the import on line 2):

```js
describe("wasteMark", () => {
  const H = 3600;
  const fable = (projected, resetsIn, key = "week_fable") => ({
    available: true,
    meters: {
      week_all: { percent: 9, projected_percent: 11, resets_at: NOW + resetsIn },
      [key]: { percent: 14, projected_percent: projected, resets_at: NOW + resetsIn },
    },
  });

  it("marks a Fable budget on pace to go unused within 48h of its reset", () => {
    expect(wasteMark(fable(18, 30 * H), NOW)).toBe(true);
  });

  it("matches the sub-limit by prefix, like the server's sublimit()", () => {
    expect(wasteMark(fable(18, 30 * H, "week_fable_5_1"), NOW)).toBe(true);
  });

  it("stays quiet while there is more than 48h to burn it", () => {
    expect(wasteMark(fable(18, 49 * H), NOW)).toBe(false);
  });

  it("stays quiet when the pace reaches 100%", () => {
    expect(wasteMark(fable(100, 30 * H), NOW)).toBe(false);
    expect(wasteMark(fable(140, 30 * H), NOW)).toBe(false);
  });

  it("needs a projection and a Fable meter", () => {
    expect(wasteMark(fable(null, 30 * H), NOW)).toBe(false);
    expect(wasteMark({ available: true, meters: { week_all: { percent: 9, projected_percent: 11, resets_at: NOW + 30 * H } } }, NOW)).toBe(false);
    expect(wasteMark({ available: false }, NOW)).toBe(false);
  });
});
```

Append inside `describe("<UsagePill>", ...)` in `usagePillRender.test.jsx`:

```jsx
  it("marks an account 💤 when its Fable budget is on pace to go unused", () => {
    const soon = NOW() + 30 * 3600;
    usage.value = {
      plan: {
        default: {
          available: true, fetched_at: NOW(),
          meters: { week_all: { label: "w", percent: 4, projected_percent: 13, resets_at: NOW() + 100 * 3600 },
                    week_fable: { label: "f", percent: 4, projected_percent: 13, resets_at: NOW() + 100 * 3600 } },
        },
        b: {
          available: true, fetched_at: NOW(),
          meters: { week_all: { label: "w", percent: 9, projected_percent: 11, resets_at: soon },
                    week_fable: { label: "f", percent: 14, projected_percent: 18, resets_at: soon } },
        },
      },
      fallback: null,
    };
    const html = render(<UsagePill />);
    expect(html.match(/usage-acct-waste/g)).toHaveLength(1);
    expect(html).toContain("on pace for 18% at reset");
  });
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- usageSummary usagePillRender`
Expected: `wasteMark is not a function` / no `usage-acct-waste` in the render.

- [ ] **Step 3: `usageSummary.js`** — append:

```js
// Whether an account's Fable budget is on pace to go unused: its week_fable*
// meter projects under 100% at reset AND the reset is within 48h — past the
// point where the remaining budget is likely to be burned. The inverse of the
// server's 🔥 signal, from the same projected_percent field. Takes the raw
// plan entry (not a summarizeAccounts row) so it composes with either.
//
// The prefix scan mirrors launch_policy.sublimit on the server: sub-limit keys
// are slugified display names, so "Fable 5.1" would arrive as week_fable_5_1.
// Duplicated here in four lines rather than stamped server-side so this
// display rule ships without a Python change; move it if a second rule appears.
export const WASTE_HORIZON_S = 48 * 3600;

export function wasteMark(entry, nowSec) {
  const meters = (entry?.available && entry.meters) || {};
  const key = Object.keys(meters).find((k) => k === "week_fable" || k.startsWith("week_fable_"));
  const m = key && meters[key];
  if (!m || m.projected_percent == null || !m.resets_at) return false;
  return m.projected_percent < 100 && m.resets_at - nowSec < WASTE_HORIZON_S;
}
```

- [ ] **Step 4: `UsagePill.jsx`**

Import (line 21): `import { summarizeAccounts } from "./usageSummary.js";` → `import { summarizeAccounts, wasteMark } from "./usageSummary.js";`

In `UsagePill`, replace the line `const accounts = summarizeAccounts(u.plan, Math.floor(Date.now() / 1000));` with these three, in this order (`nowSec` must be declared before both uses):
```js
  const nowSec = Math.floor(Date.now() / 1000);
  const accounts = summarizeAccounts(u.plan, nowSec);
  const waste = new Set(Object.keys(u.plan || {}).filter((id) => wasteMark(u.plan[id], nowSec)));
```

In the account row, after `{a.stale && <span class="usage-stale-mark">⚠</span>}`:
```jsx
            {waste.has(a.id) && (
              <span class="usage-acct-waste" title="Fable budget on pace to go unused at reset">💤</span>
            )}
```

In `acctTitle`, the per-meter `paceLines` already say `window average → 18% at reset`; add an account-level line so the tooltip states the marker's meaning. Change the signature to `acctTitle(a, expanded, waste)` (Unit 2 may have added a `poke` parameter — keep both, `acctTitle(a, expanded, poke, waste)`, and pass `waste.has(a.id)` at the call site) and before the `if (a.stale)` line:
```js
  if (waste) {
    const f = a.meters.find(({ key }) => key === "week_fable" || key.startsWith("week_fable_"));
    if (f) lines.push(`💤 fable on pace for ${f.m.projected_percent}% at reset — burn it or lose it`);
  }
```

- [ ] **Step 5: `static/styles.css`** — after `.usage-stale-mark { cursor: help; }`:
```css
.usage-acct-waste { cursor: help; opacity: 0.8; }
```

- [ ] **Step 6: Run tests, lint**

Run: `npm test` — Expected: all pass.
Run: `bin/check` — Expected: zero violations.

- [ ] **Step 7: Commit**

```bash
git add static/src/chrome/usageSummary.js static/src/chrome/UsagePill.jsx static/styles.css static/src/chrome/__tests__/usageSummary.test.js static/src/chrome/__tests__/usagePillRender.test.jsx
git commit -m "usage pill: 💤 when an account's Fable budget projects under 100% within 48h of reset (wasteMark, prefix-matched)"
```

---

### Task 4: Build, browser check, docs

**Files:**
- Modify: `static/dist/*` (rebuild), `docs/account-routing.md` (append)

- [ ] **Step 1: Build and check in the browser**

Run: `npm run build` — Expected: `static/dist/app.js` rebuilt.

Run the dev server (`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`), open `http://127.0.0.1:8766/`:
- Pick a Claude pane whose transcript was written in the last minute (send it a message, then within 60s) and use the rail's "move to A/B" action: a dialog reading `session looks live (written Ns ago)… Move anyway?` appears; Cancel leaves everything as is; Move anyway spawns the second pane on the other account and selects it.
- The usage pill shows 💤 on an account only when its Fable line reads a projection under 100% with the reset inside 48h (with the live numbers from 2026-09-08 that is B, resetting Wed 23:00 at ~18% projected).

- [ ] **Step 2: Append to `docs/account-routing.md`**

(Unit 1 creates this file and its `CLAUDE.md` index row. If it is absent because Unit 3 is being run first, create it with the heading `# Account & model routing` and this one-paragraph preamble before the sections below — "Periscope pools two Claude subscriptions, A (`~/.claude`) and B (`~/.claude-b`). `~/.claude-b/projects` symlinks to `~/.claude/projects`, so a session started on either account resumes on the other." — and add a `CLAUDE.md` reference-docs row: `| the move-account route in `routes/sessions.py`, `UsagePill` | `docs/account-routing.md` |`.)

```markdown
## Moving a running pane (`/api/pane/move-account`)

A preference flip changes only new spawns. Moving a running pane is manual:
the rail's move action resumes the same session on the other account in a
second pane (the original stays open — `~/.claude-b/projects` is a symlink to
`~/.claude/projects`, so either account can resume it). The server refuses
with 409 when the transcript was written to in the last 60s, because two
concurrent appenders would interleave into one JSONL. That guard misreads the
case it matters most for: Claude writes the *limit-reached* message into the
transcript, so a walled pane always looks live. `force=1` skips only that
guard — never the already-resumed-elsewhere one — and the client offers it
as "Move anyway?" on exactly that 409 (matched on the detail's
`session looks live` prefix; the server test and the client test pin the
text together).

## 💤 — Fable budget on pace to go unused

The pill marks an account 💤 when its `week_fable*` meter projects under 100%
at reset and the reset is within 48h: past that point the remaining budget is
unlikely to be burned. It is the inverse of 🔥 (on pace to blow), computed on
the client from the same `projected_percent` field (`usageSummary.wasteMark`).
```

- [ ] **Step 3: Commit**

```bash
git add static/dist docs/account-routing.md
git commit -m "docs: move-account override and the 💤 marker; rebuild dist"
```

---

### Task 5: Final verification

- [ ] **Step 1:** `uv run pytest -q` — all pass. `npm test` — all pass. `bin/check` — zero. `git status --short` — empty.
- [ ] **Step 2:** Report the last ~20 lines of each suite and what the browser check showed (the dialog text you saw, and which account carried 💤). Unit 3 is then ready for whole-branch review and merge.
