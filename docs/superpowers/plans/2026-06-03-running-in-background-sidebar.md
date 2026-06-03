# Running-in-background sidebar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Running" section to the split-view Detail sidebar showing Claude's background shells (process tree) and subagents (type/description + transcript drill-in), and fix the sidebar being empty in transcript mode.

**Architecture:** A new server leaf `periscope/running.py` derives the two lists; `/api/pane` carries them (the sidebar already consumes that payload). A new `/api/pane/subagent` route returns a subagent's transcript (parsed with a new `include_sidechain` flag). Frontend adds a `RunningSection` to `Sidebar.jsx` and a `SubagentOverlay` reusing an extracted `TranscriptBody`. The transcript-mode-empty bug is a one-line CSS fix (`grid-column: 2`).

**Tech Stack:** Python 3 / FastAPI / pytest (`uv run`); Preact + `@preact/signals` built by Vite to `static/dist/app.js`.

**Spec:** `docs/superpowers/specs/2026-06-03-running-in-background-sidebar-design.md`

---

## Setup (once, before Task 1)

Work in a dev worktree on port 8766, per `CLAUDE.md`:

```bash
git worktree add ../periscope-running -b feature/running-sidebar
cd ../periscope-running
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 PERISCOPE_NO_RECLAIM=1 uv run server.py   # backend (background)
npm install && npm run dev                                                   # vite HMR :5174 (background)
```

Frontend tasks verify in the browser via the dev instance (HMR at :5174 proxying API to :8766). There is no frontend test suite — that's the project convention. Server tasks use `uv run pytest`.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `history/search.py` | add `include_sidechain` param to `messages_from_jsonl` | Modify |
| `periscope/running.py` | `background_shells()` + `subagents()` derivation | Create |
| `periscope/turns.py` | `subagent_jsonl()` resolver | Modify |
| `periscope/routes/pane.py` | `/api/pane` new fields + `/api/pane/subagent` route | Modify |
| `tests/test_search.py` | sidechain-parse test | Modify |
| `tests/test_running.py` | `background_shells` + `subagents` tests | Create |
| `tests/routes/test_pane.py` | route tests | Modify |
| `static/src/store.js` | `subagentView` signal | Modify |
| `static/src/split/Transcript.jsx` | extract `TranscriptBody` | Modify |
| `static/src/split/SubagentOverlay.jsx` | subagent transcript overlay | Create |
| `static/src/sidebar/Sidebar.jsx` | `RunningSection` | Modify |
| `static/src/split/Detail.jsx` | render `<SubagentOverlay>` | Modify |
| `static/styles.css` | `.detail-side` grid fix + `.run-*` + overlay z-index | Modify |
| `static/dist/app.js` | rebuilt bundle | Modify (final task) |

---

## Task 1: `messages_from_jsonl` sidechain opt-in

**Files:**
- Modify: `history/search.py:208` (signature), `:242` (skip condition)
- Test: `tests/test_search.py`

- [ ] **Step 1: Write the failing test**

```python
def test_messages_from_jsonl_include_sidechain(tmp_path):
    from history.search import messages_from_jsonl
    p = tmp_path / "agent-x.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"x","cwd":"/x","timestamp":"2026-06-03T10:00:00.000Z",'
        '"uuid":"u1","parentUuid":null,"isSidechain":true,'
        '"message":{"role":"user","content":"sub task"}}\n'
    )
    assert messages_from_jsonl(str(p)) == []                              # default drops sidechain
    msgs = messages_from_jsonl(str(p), include_sidechain=True)
    assert len(msgs) == 1 and msgs[0]["text"] == "sub task"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_search.py::test_messages_from_jsonl_include_sidechain -v`
Expected: FAIL — `messages_from_jsonl() got an unexpected keyword argument 'include_sidechain'`.

- [ ] **Step 3: Implement**

In `history/search.py`, change the signature (line 208):

```python
def messages_from_jsonl(jsonl_path: str, include_sidechain: bool = False) -> list[dict]:
```

Change the skip line (currently line 242):

```python
        if raw.get("isMeta") is True or (
            not include_sidechain and raw.get("isSidechain") is True
        ):
            continue
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_search.py -q`
Expected: PASS (new test + existing search tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add history/search.py tests/test_search.py
git commit -m "search: messages_from_jsonl(include_sidechain=False) opt-in for subagent transcripts"
```

---

## Task 2: `periscope/running.py` — `background_shells`

**Files:**
- Create: `periscope/running.py`
- Test: `tests/test_running.py`

- [ ] **Step 1: Write the failing test**

```python
import periscope.running as running


def test_background_shells_walks_descendants_filters_infra_and_warms_up(monkeypatch):
    # pid, ppid, etime, command  — pane shell 100 → claude 200 → {dev server, mcp, tool bash}
    rows = [
        (100, 1,   "01:00:00", "-zsh"),                                   # pane login shell (excluded)
        (200, 100, "00:30:00", "/Users/t/.local/share/claude/versions/2.1.161"),  # claude (excluded)
        (300, 200, "00:05:00", "npm run dev"),                            # background dev server (keep)
        (301, 200, "00:00:03", "/opt/homebrew/bin/private-journal-mcp"),  # mcp server (excluded)
        (302, 200, "00:02:10", "node /Users/t/.local/.../ty"),            # LSP-ish node (excluded)
        (303, 200, "00:00:01", "/bin/zsh -c 'grep -r foo .'"),            # tool bash (kept by denylist, dropped by warmup)
    ]
    running._LAST_SEEN.clear()
    # First poll: warmup — nothing has been seen before, so empty.
    assert running.background_shells(100, key="s:1", rows=rows) == []
    # Second poll: same rows — persistent non-infra shells survive.
    out = running.background_shells(100, key="s:1", rows=rows)
    cmds = {r["cmd"]: r for r in out}
    assert "npm run dev" in cmds
    assert cmds["npm run dev"]["runtime_s"] == 300
    assert not any("private-journal-mcp" in c for c in cmds)
    assert not any("versions/" in c for c in cmds)
    assert "-zsh" not in cmds


def test_etime_to_s_formats():
    assert running._etime_to_s("00:05") == 5
    assert running._etime_to_s("01:02:03") == 3723
    assert running._etime_to_s("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_running.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.running'`.

- [ ] **Step 3: Implement `background_shells` (+ helpers)**

Create `periscope/running.py`:

```python
"""Derive what Claude is running in the background for a pane: detached shells
(from the process tree) and subagents (from the per-session subagents dir).

Both degrade to [] on any error — the sidebar section simply doesn't render.
Stdlib + periscope.* only (no `from server import`)."""
import json
import subprocess
import time

from periscope import activity, session_status

# Substrings in a process's command that mark it as Claude's own plumbing,
# not a user-launched background shell.
_INFRA_SUBSTRINGS = (
    "/share/claude/versions/",   # the versioned claude binary
    "/bin/claude",               # the claude launcher
    "-mcp",                      # MCP servers (private-journal-mcp, etc.)
    "channel_shim",              # periscope's own shim
    "caffeinate",
    "/ty",                       # the ty LSP (and node wrappers around it)
)
# Bare login/interactive shells with no command — not a "running" thing.
_BARE_SHELLS = {"-zsh", "-bash", "zsh", "bash", "/bin/zsh", "/bin/bash", "-fish", "fish"}

# Persistence filter: a pid must be seen on two consecutive polls to count
# (drops transient foreground tool-bash). Keyed by target ("session:index").
# First poll for a key is therefore empty (~1.5s warmup) — intended.
_LAST_SEEN: dict[str, set[int]] = {}


def _etime_to_s(etime: str) -> int:
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def _ps_rows() -> list[tuple]:
    """(pid, ppid, etime, command) for every process. command is the full
    invocation (rightmost, may contain spaces)."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
        except ValueError:
            continue
    return rows


def _is_infra(command: str) -> bool:
    if command.strip() in _BARE_SHELLS:
        return True
    return any(sub in command for sub in _INFRA_SUBSTRINGS)


def background_shells(pane_pid: int, *, key: str, rows: list[tuple] | None = None) -> list[dict]:
    """Detached/long-running, non-infra processes under the pane, that have
    survived two consecutive polls. -> [{pid, cmd, runtime_s}]."""
    if not pane_pid:
        return []
    rows = _ps_rows() if rows is None else rows
    children: dict[int, list[int]] = {}
    info: dict[int, tuple] = {}
    for pid, ppid, etime, command in rows:
        children.setdefault(ppid, []).append(pid)
        info[pid] = (etime, command)

    # BFS descendants of pane_pid.
    descendants: list[int] = []
    queue = list(children.get(pane_pid, []))
    while queue:
        pid = queue.pop()
        descendants.append(pid)
        queue.extend(children.get(pid, []))

    candidates = {}
    for pid in descendants:
        etime, command = info[pid]
        if _is_infra(command):
            continue
        candidates[pid] = {"pid": pid, "cmd": command.strip()[:80], "runtime_s": _etime_to_s(etime)}

    prev = _LAST_SEEN.get(key, set())
    _LAST_SEEN[key] = set(candidates)
    return [candidates[pid] for pid in candidates if pid in prev]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_running.py -v`
Expected: PASS (`test_background_shells_walks_descendants_filters_infra_and_warms_up`, `test_etime_to_s_formats`).

- [ ] **Step 5: Commit**

```bash
git add periscope/running.py tests/test_running.py
git commit -m "running: background_shells from the process tree (infra denylist + two-poll persistence filter)"
```

---

## Task 3: `periscope/running.py` — `subagents`

**Files:**
- Modify: `periscope/running.py`
- Test: `tests/test_running.py`

- [ ] **Step 1: Write the failing test**

```python
import json


def _seed_subagent(tmp_path, monkeypatch, session_id, agent_id, agent_type, desc, fresh=True):
    import time
    enc = tmp_path / "-Users-t-proj" / session_id / "subagents"
    enc.mkdir(parents=True, exist_ok=True)
    (enc / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": agent_type, "description": desc})
    )
    j = enc / f"agent-{agent_id}.jsonl"
    j.write_text("{}\n")
    if not fresh:
        old = time.time() - 3600
        import os
        os.utime(j, (old, old))
    monkeypatch.setattr(running.activity, "_PROJECTS_DIR", tmp_path)


def test_subagents_lists_meta_and_marks_running(tmp_path, monkeypatch):
    _seed_subagent(tmp_path, monkeypatch, "sid1", "a1b2c3", "code-reviewer", "review the diff")
    monkeypatch.setattr(running.session_status, "session_state_for",
                        lambda sid: {"state": "working"})
    out = running.subagents("sid1")
    assert out == [{"agent_id": "a1b2c3", "agent_type": "code-reviewer",
                    "description": "review the diff", "running": True}]


def test_subagents_not_running_when_parent_idle(tmp_path, monkeypatch):
    _seed_subagent(tmp_path, monkeypatch, "sid2", "dd", "Explore", "find callers")
    monkeypatch.setattr(running.session_status, "session_state_for",
                        lambda sid: {"state": "idle"})
    assert running.subagents("sid2")[0]["running"] is False


def test_subagents_not_running_when_jsonl_stale(tmp_path, monkeypatch):
    _seed_subagent(tmp_path, monkeypatch, "sid3", "ee", "Explore", "x", fresh=False)
    monkeypatch.setattr(running.session_status, "session_state_for",
                        lambda sid: {"state": "working"})
    assert running.subagents("sid3")[0]["running"] is False


def test_subagents_empty_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(running.activity, "_PROJECTS_DIR", tmp_path)
    assert running.subagents("nope") == []
    assert running.subagents(None) == []
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_running.py -k subagents -v`
Expected: FAIL — `AttributeError: module 'periscope.running' has no attribute 'subagents'`.

- [ ] **Step 3: Implement `subagents`**

Append to `periscope/running.py`:

```python
RUNNING_WINDOW_S = 10


def subagents(session_id: str | None) -> list[dict]:
    """Subagents under the session's project dir.
    -> [{agent_id, agent_type, description, running}]; agent_id is the bare
    hex id (filename stem minus the 'agent-' prefix)."""
    if not session_id:
        return []
    metas = sorted(
        activity._PROJECTS_DIR.glob(f"*/{session_id}/subagents/agent-*.meta.json")
    )
    if not metas:
        return []
    parent_working = (session_status.session_state_for(session_id) or {}).get("state") == "working"
    now = time.time()
    out = []
    for mp in metas:
        try:
            meta = json.loads(mp.read_text())
        except (OSError, ValueError):
            continue
        stem = mp.name[: -len(".meta.json")]          # "agent-<id>"
        agent_id = stem[len("agent-"):]
        jsonl = mp.with_name(stem + ".jsonl")
        try:
            fresh = (now - jsonl.stat().st_mtime) < RUNNING_WINDOW_S
        except OSError:
            fresh = False
        out.append({
            "agent_id": agent_id,
            "agent_type": meta.get("agentType", ""),
            "description": meta.get("description", ""),
            "running": bool(parent_working and fresh),
        })
    return out
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_running.py -q`
Expected: PASS (all subagents tests + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/running.py tests/test_running.py
git commit -m "running: subagents() from the per-session subagents dir (meta + jsonl-mtime/parent-working running gate)"
```

---

## Task 4: `turns.subagent_jsonl` resolver

**Files:**
- Modify: `periscope/turns.py`
- Test: `tests/test_turns.py`

- [ ] **Step 1: Write the failing test**

```python
def test_subagent_jsonl_resolves_by_session_and_agent(tmp_path, monkeypatch):
    import periscope.activity as activity
    import periscope.turns as turns
    d = tmp_path / "-enc-cwd" / "sidA" / "subagents"
    d.mkdir(parents=True)
    j = d / "agent-deadbeef.jsonl"
    j.write_text("{}\n")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    assert turns.subagent_jsonl("sidA", "deadbeef") == j
    assert turns.subagent_jsonl("sidA", "nope") is None
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_turns.py::test_subagent_jsonl_resolves_by_session_and_agent -v`
Expected: FAIL — `AttributeError: module 'periscope.turns' has no attribute 'subagent_jsonl'`.

- [ ] **Step 3: Implement**

Add to `periscope/turns.py` (next to `_jsonl_for_session`):

```python
def subagent_jsonl(session_id: str, agent_id: str):
    """The agent-<agent_id>.jsonl under the session's subagents dir, or None.
    Glob-by-id (not cwd-encode) for the same reason as _jsonl_for_session."""
    matches = list(
        activity._PROJECTS_DIR.glob(f"*/{session_id}/subagents/agent-{agent_id}.jsonl")
    )
    return matches[0] if matches else None
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_turns.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/turns.py tests/test_turns.py
git commit -m "turns: subagent_jsonl(session_id, agent_id) resolver (glob-by-id)"
```

---

## Task 5: `/api/pane` carries `background_shells` + `subagents`

**Files:**
- Modify: `periscope/routes/pane.py` (imports + `pane()` body/return)
- Test: `tests/routes/test_pane.py`

- [ ] **Step 1: Write the failing test**

```python
def test_pane_includes_running_fields(monkeypatch, client):
    # `client` is the existing FastAPI TestClient fixture in this file.
    import periscope.routes.pane as paneroute
    monkeypatch.setattr(paneroute, "background_shells", lambda pid, key: [{"pid": 9, "cmd": "npm run dev", "runtime_s": 300}])
    monkeypatch.setattr(paneroute, "subagents", lambda sid: [{"agent_id": "a1", "agent_type": "Explore", "description": "x", "running": True}])
    # tmux + session resolution are stubbed by the existing pane-route test harness;
    # follow the pattern already used by the other /api/pane tests in this file.
    r = client.get("/api/pane?session=demo&index=0")
    assert r.status_code == 200
    body = r.json()
    assert body["background_shells"][0]["cmd"] == "npm run dev"
    assert body["subagents"][0]["agent_type"] == "Explore"
```

> NOTE to implementer: open `tests/routes/test_pane.py` first and match its existing
> tmux/`list_windows` stubbing pattern (the route shells out to tmux). Adapt the test
> above to that harness rather than inventing new stubs.

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/routes/test_pane.py::test_pane_includes_running_fields -v`
Expected: FAIL — `KeyError: 'background_shells'`.

- [ ] **Step 3: Implement**

In `periscope/routes/pane.py` imports, add:

```python
from periscope.turns import get_turns_for_pane, session_id_for_pane, subagent_jsonl
from periscope.running import background_shells, subagents
```

In `pane()`, after `pane_id`/`target` are known and before the `return`, resolve the pane's process pid and session id:

```python
    try:
        pane_pid = int(tmux("display-message", "-t", target, "-p", "#{pane_pid}").strip())
    except (ValueError, Exception):
        pane_pid = 0
    sid = session_id_for_pane(pane_id)
```

Add two keys to the returned dict (alongside the existing ones):

```python
        "background_shells": background_shells(pane_pid, key=target) if pane_pid else [],
        "subagents": subagents(sid),
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/routes/test_pane.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/pane.py tests/routes/test_pane.py
git commit -m "pane: /api/pane carries background_shells + subagents"
```

---

## Task 6: `/api/pane/subagent` route

**Files:**
- Modify: `periscope/routes/pane.py`
- Test: `tests/routes/test_pane.py`

- [ ] **Step 1: Write the failing test**

```python
def test_pane_subagent_validates_and_returns_messages(monkeypatch, client, tmp_path):
    import periscope.routes.pane as paneroute
    import periscope.activity as activity, json
    d = tmp_path / "-enc" / "sidZ" / "subagents"; d.mkdir(parents=True)
    (d / "agent-abc123.jsonl").write_text(
        '{"type":"user","sessionId":"sidZ","cwd":"/x","timestamp":"2026-06-03T10:00:00.000Z",'
        '"uuid":"u1","parentUuid":null,"isSidechain":true,'
        '"message":{"role":"user","content":"sub work"}}\n'
    )
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(paneroute, "session_id_for_pane", lambda pane_id: "sidZ")
    # 400 on a bad agent id
    assert client.get("/api/pane/subagent?session=demo&index=0&agent=NOThex!").status_code == 400
    # messages for a valid id (sidechain event included)
    body = client.get("/api/pane/subagent?session=demo&index=0&agent=abc123").json()
    assert body["messages"][0]["text"] == "sub work"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/routes/test_pane.py::test_pane_subagent_validates_and_returns_messages -v`
Expected: FAIL — 404 (route doesn't exist).

- [ ] **Step 3: Implement**

Add near the top of `periscope/routes/pane.py` (after imports):

```python
import re
_AGENT_ID_RE = re.compile(r"^[0-9a-f]+$")
```

Add `messages_from_jsonl` to the `history.search` import (it isn't imported yet):

```python
from history.search import messages_from_jsonl
```

Add the route (after `/api/pane/turns`):

```python
@router.get("/api/pane/subagent")
def pane_subagent(session: str, index: int, agent: str):
    """A subagent's transcript (sidechain events included)."""
    if not _AGENT_ID_RE.match(agent):
        raise HTTPException(400, "invalid agent id")
    target = f"{session}:{index}"
    try:
        pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()
    except Exception:
        return {"messages": None}
    sid = session_id_for_pane(pane_id)
    if not sid:
        return {"messages": None}
    path = subagent_jsonl(sid, agent)
    if path is None:
        return {"messages": None}
    return {"messages": messages_from_jsonl(str(path), include_sidechain=True)}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/routes/test_pane.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/pane.py tests/routes/test_pane.py
git commit -m "pane: GET /api/pane/subagent returns a subagent transcript (validated agent id, sidechain-included parse)"
```

---

## Task 7: `subagentView` signal

**Files:**
- Modify: `static/src/store.js`

- [ ] **Step 1: Add the signal**

Next to the existing `previewPath` signal in `static/src/store.js`:

```javascript
// Open subagent transcript overlay: { session, index, agentId } | null.
// Mirrors previewPath (SubagentOverlay reads it; rows set it).
export const subagentView = signal(null);
```

- [ ] **Step 2: Verify it builds**

Run: `npm run build`
Expected: build succeeds (no usage yet; just confirms no syntax error).

- [ ] **Step 3: Commit**

```bash
git add static/src/store.js
git commit -m "store: subagentView signal for the subagent transcript overlay"
```

---

## Task 8: Extract `TranscriptBody` from `TranscriptView`

**Files:**
- Modify: `static/src/split/Transcript.jsx`

- [ ] **Step 1: Add the presentational `TranscriptBody`**

Add a new exported component (above `TranscriptView`):

```jsx
// Presentational transcript body — the message list only. Shared by the live
// TranscriptView (currentUuid = newest turn) and SubagentOverlay (currentUuid
// = null, static history). No poll/composer/status — those stay in callers.
export function TranscriptBody({ messages, currentUuid }) {
  if (!messages || messages.length === 0) {
    return <div class="transcript-empty">No transcript yet.</div>;
  }
  return messages.map((m) =>
    m.role === "system" && m.kind === "compact"
      ? <div key={m.uuid} class="transcript-compact"><span>context compacted</span></div>
      : <Turn key={m.uuid} m={m} current={m.uuid === currentUuid} />
  );
}
```

- [ ] **Step 2: Use it inside `TranscriptView`**

Replace the inline `messages.length === 0 ? … : messages.map(…)` block inside the
`.transcript` div (currently lines ~371–377) with:

```jsx
        <TranscriptBody messages={messages} currentUuid={lastUuid} />
```

Leave the two status banners (`transcript-status-waiting`, `transcript-working`)
and `<Composer>` exactly as they are.

- [ ] **Step 3: Verify the live view is unchanged (browser)**

On the dev instance (:5174), open a Claude pane in transcript mode. Confirm the
transcript still renders turns/tool calls, the current-turn highlight still shows
on the newest turn, and the working/waiting banners still appear. No visual change.

- [ ] **Step 4: Commit**

```bash
git add static/src/split/Transcript.jsx
git commit -m "transcript: extract presentational TranscriptBody (reused by the subagent overlay)"
```

---

## Task 9: `SubagentOverlay` component

**Files:**
- Create: `static/src/split/SubagentOverlay.jsx`

- [ ] **Step 1: Write the component**

```jsx
// Subagent transcript overlay. Floats over .detail-pane-body when subagentView
// is non-null. One-shot fetch of /api/pane/subagent (subagents are historical —
// no poll). Esc-dismiss via the shared useEscape LIFO. Re-fetches on agentId
// change. Reuses TranscriptBody (read-only: currentUuid=null, no composer).
import { useEffect, useState } from "preact/hooks";
import { subagentView } from "../store.js";
import { useEscape } from "../hooks/useEscape.js";
import { TranscriptBody } from "./Transcript.jsx";

export function SubagentOverlay() {
  const view = subagentView.value;
  const [messages, setMessages] = useState(null);

  useEscape(!!view, () => { subagentView.value = null; });

  useEffect(() => {
    if (!view) { setMessages(null); return; }
    let alive = true;
    setMessages(null);
    const q = `session=${encodeURIComponent(view.session)}&index=${view.index}&agent=${encodeURIComponent(view.agentId)}`;
    fetch(`/api/pane/subagent?${q}`)
      .then((r) => r.json())
      .then((d) => { if (alive) setMessages(d.messages || []); })
      .catch(() => { if (alive) setMessages([]); });
    return () => { alive = false; };
  }, [view && view.session, view && view.index, view && view.agentId]);

  if (!view) return null;
  return (
    <div class="subagent-overlay">
      <header class="subagent-overlay-head">
        <span>subagent transcript</span>
        <button class="subagent-overlay-close" title="close (Esc)"
                onClick={() => { subagentView.value = null; }}>✕</button>
      </header>
      <div class="subagent-overlay-body">
        {messages === null
          ? <div class="transcript-empty">loading…</div>
          : <TranscriptBody messages={messages} currentUuid={null} />}
      </div>
    </div>
  );
}
```

> Implementer: confirm `useEscape`'s signature against `static/src/hooks/useEscape.js`
> (it's used by `PreviewOverlay`). If it takes `(active, handler)` use as shown; if it
> registers differently, match `PreviewOverlay`'s usage exactly.

- [ ] **Step 2: Verify it builds**

Run: `npm run build`
Expected: success.

- [ ] **Step 3: Commit**

```bash
git add static/src/split/SubagentOverlay.jsx
git commit -m "subagent: SubagentOverlay renders a subagent transcript via TranscriptBody"
```

---

## Task 10: `RunningSection` in the sidebar

**Files:**
- Modify: `static/src/sidebar/Sidebar.jsx`

- [ ] **Step 1: Add `RunningSection` + helper**

Add above the `Sidebar` export (model it on `FilesSection`):

```jsx
import { subagentView } from "../store.js";   // add to the existing store import if grouping

function fmtRuntime(s) {
  if (s == null) return "";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// "Running" — background shells + subagents for the selected pane. Sub-labels
// only when BOTH kinds are present (the ▶/⚇ glyphs self-identify otherwise).
// Returns null when nothing is running.
function RunningSection({ data }) {
  const shells = data?.background_shells || [];
  const agents = data?.subagents || [];
  if (!shells.length && !agents.length) return null;
  const both = shells.length > 0 && agents.length > 0;
  return (
    <section class="modal-side-section modal-side-running">
      <h4>Running</h4>
      {shells.length > 0 && (
        <div class="run-grp">
          {both && <div class="run-sub">Background shells</div>}
          {shells.map((s) => (
            <div class="run-row" key={`sh:${s.pid}`}>
              <span class="run-ic">▶</span>
              <span class="run-cmd" title={s.cmd}>{s.cmd}</span>
              <span class="run-rt">{fmtRuntime(s.runtime_s)}</span>
            </div>
          ))}
        </div>
      )}
      {agents.length > 0 && (
        <div class="run-grp">
          {both && <div class="run-sub">Subagents</div>}
          {agents.map((a) => (
            <div class="run-row run-agent" key={`ag:${a.agent_id}`}
                 title={`Open ${a.agent_type} transcript`}
                 onClick={() => {
                   subagentView.value = { session: data.session, index: data.index ?? indexFromTarget(data.target), agentId: a.agent_id };
                 }}>
              <span class="run-ic">⚇</span>
              <span class="run-agtype">{a.agent_type}</span>
              {a.running && <span class="run-dot" />}
              <span class="run-agdesc">{a.description}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// /api/pane carries `target` ("session:index") but not a separate index; derive it.
function indexFromTarget(target) {
  const i = String(target || "").lastIndexOf(":");
  return i >= 0 ? Number(target.slice(i + 1)) : 0;
}
```

> Implementer note: `/api/pane` returns `session` and `target` (see `pane()` return),
> not a standalone `index` — hence `indexFromTarget(data.target)`. Verify these field
> names on the live payload before finalizing.

- [ ] **Step 2: Render it first in `Sidebar`**

In the `Sidebar` return, add `<RunningSection data={data} />` as the **first**
child of the `<aside>`, above the Linked `<section>`.

- [ ] **Step 3: Verify (browser)**

On a pane with a `run_in_background` shell going (start one, e.g. a dev server) and/or
a dispatched subagent, confirm the Running section appears at the top with the shell
command + runtime and/or the subagent type/description. Confirm it disappears when
nothing is running. (Sub-labels only when both present.)

- [ ] **Step 4: Commit**

```bash
git add static/src/sidebar/Sidebar.jsx
git commit -m "sidebar: RunningSection (background shells + subagents; sub-labels only when both)"
```

---

## Task 11: Render `SubagentOverlay` in Detail

**Files:**
- Modify: `static/src/split/Detail.jsx`

- [ ] **Step 1: Import + render**

Add the import:

```jsx
import { SubagentOverlay } from "./SubagentOverlay.jsx";
```

Render it alongside the existing `PreviewOverlay` (same region — inside the
`#detail` section, after `<PaneDetail>` / wherever `PreviewOverlay` is rendered):

```jsx
      <SubagentOverlay />
```

- [ ] **Step 2: Verify (browser)**

Click a subagent row in the Running section → the overlay opens with that subagent's
transcript (turns + tool calls render). Esc closes it. Cmd+click a file path inside
the subagent transcript → the file PreviewOverlay opens on top; Esc closes the
preview first, then (Esc again) the subagent overlay.

- [ ] **Step 3: Commit**

```bash
git add static/src/split/Detail.jsx
git commit -m "detail: mount SubagentOverlay"
```

---

## Task 12: CSS — transcript-mode sidebar fix + Running styles + overlay stacking

**Files:**
- Modify: `static/styles.css`

- [ ] **Step 1: Pin the sidebar to column 2 (the transcript-mode-empty fix)**

Add (near the `.detail-side` rules):

```css
/* Keep the sidebar in the 280px grid column even in transcript mode, where the
   term-host is display:none (which would otherwise collapse the sidebar into
   col 1, under the absolute transcript overlay). The transcript host stops at
   right:280px, leaving this slot for the sidebar. */
.detail-side { grid-column: 2; }
```

- [ ] **Step 2: Running section styles**

Append near the other sidebar section styles:

```css
.modal-side-running .run-grp { margin-bottom: 6px; }
.run-sub { font-size: 9px; text-transform: uppercase; letter-spacing: .06em; color: var(--fg-4); margin: 6px 0 3px; }
.run-grp:first-child .run-sub { margin-top: 0; }
.run-row { display: flex; align-items: center; gap: 7px; padding: 3px 0; font-family: var(--mono); font-size: 11.5px; min-width: 0; }
.run-ic { flex: none; color: var(--fg-3); }
.run-cmd { color: var(--fg-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
.run-rt { flex: none; color: var(--fg-4); font-size: 10.5px; }
.run-agent { cursor: pointer; }
.run-agent:hover .run-agtype { color: var(--accent); }
.run-agtype { color: var(--fg-1); flex: none; }
.run-agdesc { color: var(--fg-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.run-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--s-working); flex: none; animation: transcript-status-pulse 1.4s ease-in-out infinite; }
```

- [ ] **Step 3: Subagent overlay styles + stacking**

```css
/* Subagent transcript overlay — floats over .detail-pane-body, above the
   transcript host (z-index:1) and composer (z-index:2). PreviewOverlay sits
   above this (so a file chip clicked inside a subagent transcript layers on
   top); useEscape LIFO dismisses the preview first, then this. */
.subagent-overlay {
  position: absolute; inset: var(--detail-header-h, 40px) 280px 0 0;
  z-index: 5; display: flex; flex-direction: column;
  background: var(--bg-0); border-right: 1px solid var(--line);
}
.subagent-overlay-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 12px; border-bottom: 1px solid var(--line-soft);
  font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--fg-3);
}
.subagent-overlay-close { background: none; border: 0; color: var(--fg-3); cursor: pointer; font-size: 13px; }
.subagent-overlay-close:hover { color: var(--fg-0); }
.subagent-overlay-body { flex: 1; min-height: 0; overflow-y: auto; padding: 16px 20px; }
```

If `PreviewOverlay` lacks an explicit z-index higher than 5, bump it (check its
rule) so it stacks above `.subagent-overlay`.

- [ ] **Step 4: Verify (browser)**

- Transcript mode: the sidebar (Linked/Notes/Files/Activity + Running) is now
  **visible** (previously empty). Terminal mode unchanged.
- Running rows styled correctly; subagent run-dot pulses.
- Subagent overlay floats correctly and leaves the 280px sidebar visible.

- [ ] **Step 5: Commit**

```bash
git add static/styles.css
git commit -m "css: sidebar grid-column:2 (transcript-mode fix) + Running section + subagent overlay stacking"
```

---

## Task 13: Build bundle + full verification

**Files:**
- Modify: `static/dist/app.js`

- [ ] **Step 1: Run the full server test suite**

Run: `uv run pytest -q`
Expected: PASS (all existing + new tests).

- [ ] **Step 2: Build the production bundle**

Run: `npm run build`
Expected: `dist/app.js` rebuilt, no errors.

- [ ] **Step 3: Full browser verification (dev instance)**

Confirm end-to-end:
- A real `run_in_background` shell (e.g. a dev server) appears in Running with command + runtime, after the ~1.5s warmup; vanishes when killed.
- A dispatched subagent appears with type + description + pulsing dot while running; clicking opens its transcript (turns render — sidechain content present); Esc closes.
- Sidebar visible in **both** terminal and transcript mode.
- Nothing-running → no Running section.

- [ ] **Step 4: Commit the bundle**

```bash
git add static/dist/app.js
git commit -m "build: running-in-background sidebar bundle"
```

- [ ] **Step 5: Integrate (merge + restart prod)**

```bash
cd ~/dev/periscope
git merge feature/running-sidebar
bin/periscope restart
git worktree remove ../periscope-running
```

---

## Notes / accepted tradeoffs (from the spec)

- **Foreground leak:** a foreground command running >1 poll can appear in shells; the two-poll persistence filter drops transients. Accepted.
- **First-poll warmup:** shells empty for ~1.5s after selecting a pane, then populate. Intended.
- **Subagent running gate is coarse** (per-session parent-`working` + 10s mtime window); a just-finished agent can read "running" ≤10s. Accepted.
- **Undocumented internals:** subagents meta + session-status file are Claude Code internals; all reads degrade to empty.
- **Subagent overlay is split-view-only (v1).** `Sidebar` is shared with the modal, so the Running section (and clickable subagent rows) also appear in the modal — but `SubagentOverlay` mounts only in the split-view `Detail`, and its CSS is positioned for `.detail-pane-body`. In the modal, a subagent row sets `subagentView` but no overlay appears. Acceptable: this work targets split view (the premise). A later pass can mount the overlay in the modal too.
