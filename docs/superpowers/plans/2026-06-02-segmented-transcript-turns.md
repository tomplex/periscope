# Segmented Transcript — Claude Turns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Transcript" content mode to periscope's split-view detail pane that renders a Claude pane's conversation as structured, collapsible turn segments sourced from the `history/` JSONL pipeline — no terminal emulation.

**Architecture:** A stateless endpoint (`GET /api/pane/turns`) returns the full parsed message list per poll; the client reconciles by `uuid`. Server parsing reuses `history/jsonl.py`'s `Event` stream via a new `messages_from_jsonl(path)` helper. The renderer is a Preact component in `static/src/split/`, with a Transcript⇄Terminal toggle; the live `<Terminal>` is hidden-not-destroyed, and each opened transcript is kept mounted (review-iframe pattern) so scroll + expand survive pane switches. Default mode is Terminal, auto-promoting to Transcript once a pane's first poll returns real turns.

**Tech Stack:** Python 3 / FastAPI / pytest (backend); Preact + `@preact/signals` / Vite (frontend). Run backend tests with `uv run pytest -q`.

**Reference docs:** spec `docs/superpowers/specs/2026-06-02-segmented-transcript-turns-design.md`; structure `docs/superpowers/specs/2026-06-02-segmented-transcript-turns-structure.md`.

**Execution context:** Per `CLAUDE.md` "Development workflow," execute in a git worktree running on :8766 (`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`). Prod stays on :8765. Commit-as-you-go straight to the branch; single-line commit messages.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `history/search.py` | `messages_from_jsonl(path)`; `get_session` refactored onto it | Modify |
| `history/tests/fixtures/turns_session.jsonl` | Fixture exercising pairing, filters, compact, in-flight | Create |
| `history/tests/test_search.py` | `messages_from_jsonl` unit tests | Modify |
| `periscope/turns.py` | Stateless `get_turns_for_pane(cwd)` resolver | Create |
| `tests/test_turns.py` | `get_turns_for_pane` unit tests | Create |
| `periscope/routes/pane.py` | `GET /api/pane/turns` route | Modify |
| `tests/routes/test_pane.py` | Route tests for `/api/pane/turns` | Modify |
| `static/src/store.js` | `transcriptMode` + `transcriptSeen` signals | Modify |
| `static/src/split/Transcript.jsx` | `<TranscriptView>` + poll hook + `<TurnSegment>` + `<ToolCall>` | Create |
| `static/src/split/Detail.jsx` | Toggle, computed mode, keep-mounted transcripts | Modify |
| `static/styles.css` | Transcript + toggle styles, place transcript over detail body | Modify |
| `static/dist/app.js` | Built bundle (committed) | Rebuild |

---

## Phase 1 — Server: parser + endpoint (TDD)

### Task 1: `messages_from_jsonl` in `history/search.py`

**Files:**
- Create: `history/tests/fixtures/turns_session.jsonl`
- Modify: `history/search.py` (add function; refactor `get_session` body ~lines 232-245)
- Test: `history/tests/test_search.py`

- [ ] **Step 1: Create the test fixture**

Create `history/tests/fixtures/turns_session.jsonl` with exactly these 9 lines (one JSON object per line, no trailing blank line issues — each line standalone):

```jsonl
{"type":"user","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:00.000Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":"please run the tests"}}
{"type":"assistant","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:01.000Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":[{"type":"text","text":"Running them now"},{"type":"tool_use","id":"tool_1","name":"Bash","input":{"command":"pytest -q"}}]}}
{"type":"user","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:02.000Z","uuid":"u2","parentUuid":"a1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool_1","content":"All pass"}]}}
{"type":"assistant","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:03.000Z","uuid":"a2","parentUuid":"u2","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool_2","name":"Read","input":{"file_path":"/x"}}]}}
{"type":"user","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:04.000Z","uuid":"m1","parentUuid":"a2","isMeta":true,"message":{"role":"user","content":"meta noise"}}
{"type":"assistant","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:05.000Z","uuid":"sc1","parentUuid":"a2","isSidechain":true,"message":{"role":"assistant","content":[{"type":"text","text":"sidechain noise"}]}}
{"type":"system","subtype":"compact_boundary","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:06.000Z","uuid":"c1","parentUuid":"a2"}
{"type":"system","subtype":"file-history-snapshot","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:07.000Z","uuid":"sys2","parentUuid":"c1"}
{"type":"user","sessionId":"turns-001","cwd":"/Users/tom/dev/turnsproj","gitBranch":"main","timestamp":"2026-06-01T10:00:08.000Z","uuid":"u3","parentUuid":"c1","message":{"role":"user","content":"thanks"}}
```

- [ ] **Step 2: Write the failing test**

Append to `history/tests/test_search.py`:

```python
def test_messages_from_jsonl_pairs_filters_and_stamps_uuid(fixture_dir):
    from history.search import messages_from_jsonl
    msgs = messages_from_jsonl(str(fixture_dir / "turns_session.jsonl"))

    # Order preserved; isMeta(m1)/isSidechain(sc1)/tool-result-only(u2)/
    # non-compact-system(sys2) dropped.
    assert [m["uuid"] for m in msgs] == ["u1", "a1", "a2", "c1", "u3"]

    # tool_use/result pairing (tool_use.id <-> tool_result.tool_use_id)
    a1 = next(m for m in msgs if m["uuid"] == "a1")
    assert a1["role"] == "assistant"
    assert a1["text"] == "Running them now"
    assert a1["tool_uses"][0]["name"] == "Bash"
    assert a1["tool_uses"][0]["result"] == "All pass"

    # in-flight tool_use: no matching result yet -> None
    a2 = next(m for m in msgs if m["uuid"] == "a2")
    assert a2["tool_uses"][0]["result"] is None

    # compact_boundary emitted as a divider
    c1 = next(m for m in msgs if m["uuid"] == "c1")
    assert c1["role"] == "system" and c1["kind"] == "compact"

    # every emitted message carries a uuid + ts_ms (reconciliation depends on it)
    assert all(m["uuid"] and m["ts_ms"] for m in msgs)

    # deterministic: a second parse yields the same uuids in the same order
    again = [m["uuid"] for m in messages_from_jsonl(str(fixture_dir / "turns_session.jsonl"))]
    assert again == ["u1", "a1", "a2", "c1", "u3"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest history/tests/test_search.py::test_messages_from_jsonl_pairs_filters_and_stamps_uuid -v`
Expected: FAIL with `ImportError`/`AttributeError: cannot import name 'messages_from_jsonl'`.

- [ ] **Step 4: Implement `messages_from_jsonl`**

Add to `history/search.py` (near `get_session`):

```python
def messages_from_jsonl(jsonl_path: str) -> list[dict]:
    """Stream-parse a Claude JSONL into structured turn messages (full file).

    User/assistant turns in JSONL order. Assistant tool_use blocks are
    back-patched with their paired tool_result content (tool_use.id ==
    tool_result.tool_use_id from later user-role events); unpaired (in-flight)
    tool_uses get result=None. Each message carries `uuid` (the client's stable
    reconciliation key). compact_boundary system events become divider markers.

    Filters read `ev.raw` — isMeta/isSidechain/subtype are NOT lifted onto the
    Event by `_classify` (history/jsonl.py); reaching for ev.is_meta would be an
    AttributeError. Skip the event if any of:
      - ev.raw.get("isMeta") is True
      - ev.raw.get("isSidechain") is True
      - ev.type == "system" and ev.raw.get("subtype") != "compact_boundary"
      - the event carries no user_text, assistant_text, tool_uses, or tool_results
    """
    from .jsonl import parse_jsonl

    events = list(parse_jsonl(jsonl_path))

    # Pass 1: tool_use_id -> result content (full-file; a result can pair with
    # a tool_use emitted in an earlier event).
    results: dict[str, str] = {}
    for ev in events:
        for tr in ev.tool_results:
            tuid = tr.get("tool_use_id")
            if tuid is not None:
                results[tuid] = tr.get("content", "")

    # Pass 2: emit messages.
    messages: list[dict] = []
    for ev in events:
        raw = ev.raw
        if raw.get("isMeta") is True or raw.get("isSidechain") is True:
            continue
        if ev.type == "system":
            if raw.get("subtype") == "compact_boundary":
                messages.append({
                    "role": "system",
                    "kind": "compact",
                    "uuid": ev.uuid,
                    "ts_ms": ev.ts_ms,
                })
            continue
        if not (ev.user_text or ev.assistant_text or ev.tool_uses or ev.tool_results):
            continue
        if ev.type == "user" and ev.user_text:
            messages.append({
                "role": "user",
                "uuid": ev.uuid,
                "ts_ms": ev.ts_ms,
                "text": ev.user_text,
            })
        elif ev.type == "assistant":
            tool_uses = [{
                "id": tu.get("id"),
                "name": tu.get("name"),
                "input": tu.get("input") or {},
                "result": results.get(tu.get("id")),
            } for tu in ev.tool_uses]
            messages.append({
                "role": "assistant",
                "uuid": ev.uuid,
                "ts_ms": ev.ts_ms,
                "text": ev.assistant_text or "",
                "tool_uses": tool_uses,
            })
    return messages
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest history/tests/test_search.py::test_messages_from_jsonl_pairs_filters_and_stamps_uuid -v`
Expected: PASS.

- [ ] **Step 6: Refactor `get_session` onto the helper**

In `history/search.py:get_session`, replace the inline message-building loop (currently `for ev in parse_jsonl(jsonl_path): ...` building `messages`, ~lines 231-245) with:

```python
    messages = [] if jsonl_missing else messages_from_jsonl(jsonl_path)
```

Leave the rest of `get_session` unchanged. (Its public schema gains `uuid` + `tool_uses[i].result` + compact dividers; `/history`'s `renderMsg` reads only `t.name`/`t.input`, so the extra keys are inert.)

- [ ] **Step 7: Run the search test module to verify no regression**

Run: `uv run pytest history/tests/test_search.py -q`
Expected: PASS (incl. the existing `test_get_session_returns_full_row_and_messages`).

- [ ] **Step 8: Commit**

```bash
git add history/search.py history/tests/test_search.py history/tests/fixtures/turns_session.jsonl
git commit -m "history: messages_from_jsonl (pairing + uuid + filters); get_session reuses it"
```

---

### Task 2: `periscope/turns.py` — stateless resolver

**Files:**
- Create: `periscope/turns.py`
- Test: `tests/test_turns.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_turns.py`:

```python
"""Tests for periscope.turns.get_turns_for_pane (stateless cwd -> messages)."""
import json


def _write_jsonl(path, cwd):
    line = {
        "type": "user", "sessionId": path.stem, "cwd": cwd,
        "gitBranch": "main", "timestamp": "2026-06-01T10:00:00.000Z",
        "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": "hello transcript"},
    }
    path.write_text(json.dumps(line) + "\n")


def test_get_turns_for_pane_resolves_messages(tmp_path, monkeypatch):
    import periscope.activity as activity
    cwd = "/Users/tom/dev/turnsproj"
    enc_dir = tmp_path / activity._encode_cwd(cwd)
    enc_dir.mkdir(parents=True)
    _write_jsonl(enc_dir / "sid-123.jsonl", cwd)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)

    from periscope.turns import get_turns_for_pane
    out = get_turns_for_pane(cwd)
    assert out is not None
    assert out["session_id"] == "sid-123"
    assert out["jsonl_path"].endswith("sid-123.jsonl")
    assert out["messages"][0]["role"] == "user"
    assert out["messages"][0]["text"] == "hello transcript"


def test_get_turns_for_pane_none_when_no_match(tmp_path, monkeypatch):
    import periscope.activity as activity
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    from periscope.turns import get_turns_for_pane
    assert get_turns_for_pane("/no/such/cwd") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turns.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'periscope.turns'`.

- [ ] **Step 3: Implement `periscope/turns.py`**

```python
"""Stateless turn-transcript resolver: a pane's cwd -> its live Claude
transcript messages. No cache (full-resend per poll; see the design spec for
why a since_ts/parse cache is deferred). Imports only periscope.* / history.*
— never `from server import`."""
from history.search import messages_from_jsonl
from periscope.activity import live_transcript_for


def get_turns_for_pane(cwd: str) -> dict | None:
    """Resolve `cwd` to its newest matching Claude JSONL and parse it.

    Returns {session_id, jsonl_path, messages} or None when no live transcript
    matches (history not indexed / no JSONL / cwd has no recorded match)."""
    jsonl = live_transcript_for(cwd)
    if jsonl is None:
        return None
    return {
        "session_id": jsonl.stem,
        "jsonl_path": str(jsonl),
        "messages": messages_from_jsonl(str(jsonl)),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_turns.py -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add periscope/turns.py tests/test_turns.py
git commit -m "turns: stateless get_turns_for_pane(cwd) resolver"
```

---

### Task 3: `GET /api/pane/turns` route

**Files:**
- Modify: `periscope/routes/pane.py` (add import + route)
- Test: `tests/routes/test_pane.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/routes/test_pane.py` (reuse the existing `_patch` helper at the top of the file):

```python
def test_pane_turns_returns_messages_end_to_end(client, mocker, tmp_path, monkeypatch):
    # Exercise route -> get_turns_for_pane -> messages_from_jsonl against a real
    # tmp transcript (the Q1-2026 mocked-migration lesson: don't mock the path
    # the bug would live in). Only the tmux boundary is faked.
    import json
    import periscope.activity as activity
    cwd = "/Users/tom/dev/turnsproj"
    enc = tmp_path / activity._encode_cwd(cwd)
    enc.mkdir(parents=True)
    (enc / "sid-9.jsonl").write_text(json.dumps({
        "type": "user", "sessionId": "sid-9", "cwd": cwd,
        "timestamp": "2026-06-01T10:00:00.000Z", "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": "hi there"},
    }) + "\n")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    _patch(mocker, "tmux", return_value=cwd + "\n")

    r = client.get("/api/pane/turns?session=main&index=0")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sid-9"
    assert body["messages"][0]["text"] == "hi there"


def test_pane_turns_null_when_no_transcript(client, mocker, tmp_path, monkeypatch):
    import periscope.activity as activity
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)  # empty -> no match
    _patch(mocker, "tmux", return_value="/no/such/cwd\n")
    r = client.get("/api/pane/turns?session=main&index=0")
    assert r.status_code == 200
    assert r.json() == {"turns": None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/routes/test_pane.py -k pane_turns -v`
Expected: FAIL with 404 (route not registered yet).

- [ ] **Step 3: Implement the route**

In `periscope/routes/pane.py`, add the import near the top (with the other `periscope.*` imports):

```python
from periscope.turns import get_turns_for_pane
```

Add the route (sibling to `/api/pane`):

```python
@router.get("/api/pane/turns")
def pane_turns(session: str, index: int):
    """Structured turn transcript for a Claude pane. Full message list per call
    (stateless; the client reconciles by uuid). Returns {turns: null} when the
    pane's cwd resolves to no live transcript. Session/index are query params so
    slash-bearing session names don't collide with path routing (invariant 6)."""
    target = f"{session}:{index}"
    try:
        cwd = tmux("display-message", "-t", target, "-p", "#{pane_current_path}").strip()
    except Exception:
        raise HTTPException(500, "tmux display-message failed")
    if not cwd:
        return {"turns": None}
    out = get_turns_for_pane(cwd)
    return out if out is not None else {"turns": None}
```

(`HTTPException` and `tmux` are already imported in this module — confirm at the top; the existing `/api/pane` route uses both.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/routes/test_pane.py -k pane_turns -v`
Expected: PASS (both).

- [ ] **Step 5: Run the full backend suite (own-the-branch check)**

Run: `uv run pytest -q`
Expected: PASS (all green, including pre-existing tests).

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/pane.py tests/routes/test_pane.py
git commit -m "routes: GET /api/pane/turns — stateless turn transcript for a pane"
```

- [ ] **Step 7: Manual smoke (dev server on :8766)**

Start the dev server (`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`), then with a real Claude pane open run:

```bash
curl 'http://127.0.0.1:8766/api/pane/turns?session=<a-real-claude-session>&index=<n>' | python3 -m json.tool | head -40
```

Expected: JSON with `session_id`, `jsonl_path`, and a `messages` array of role-tagged turns (assistant turns carry `tool_uses` with `result`). A shell pane returns `{"turns": null}`.

---

## Phase 2 — Frontend: transcript view + toggle (build + manual-verify)

> Per project convention (CLAUDE.md "UI work: test in the browser") these tasks are built and verified in the browser, not unit-tested. Rebuild the bundle (`npm run build`) and verify on :8766 before committing.

### Task 4: store signals

**Files:**
- Modify: `static/src/store.js`

- [ ] **Step 1: Add the two signals**

Append to `static/src/store.js` (after the existing signal exports):

```js
// Transcript content-mode state (split-view detail). transcriptMode holds only
// an EXPLICIT user toggle per pid; transcriptSeen is set once the pane's first
// poll returns real turns. Displayed mode is computed from both (see Detail.jsx
// computeMode): explicit override wins, else transcript iff seen, else terminal.
export const transcriptMode = signal({});   // { [pid]: "transcript" | "terminal" }
export const transcriptSeen = signal({});   // { [pid]: true }
```

- [ ] **Step 2: Commit**

```bash
git add static/src/store.js
git commit -m "store: transcriptMode + transcriptSeen signals"
```

---

### Task 5: `static/src/split/Transcript.jsx`

**Files:**
- Create: `static/src/split/Transcript.jsx`

- [ ] **Step 1: Write the component module**

Create `static/src/split/Transcript.jsx`:

```jsx
// Structured Claude-turn transcript for the split-view detail pane. Polls
// /api/pane/turns for the selected pane (full message list per poll; reconcile
// by uuid), renders turn segments with expandable tool calls. No xterm/emulation
// — JSONL is already structured. See the segmented-transcript design spec.
import { useEffect, useState } from "preact/hooks";
import { transcriptSeen } from "../store.js";
import { targetQuery, relTime } from "../util.js";

const TURNS_POLL_MS = 2000;

// Poll while this pane is the current selection — in EITHER sub-mode, so a
// Terminal-mode pane still discovers its transcript and auto-promotes. Fires
// once immediately on becoming selected, then every TURNS_POLL_MS. On the first
// non-empty response, flips transcriptSeen[pid] (drives the auto-promote).
function useTranscriptPoll(target, pid, selected) {
  const [messages, setMessages] = useState([]);
  useEffect(() => {
    if (!selected || !target) return;
    let alive = true;
    let timer = null;
    async function tick() {
      try {
        const res = await fetch(`/api/pane/turns?${targetQuery(target)}`);
        const data = await res.json();
        if (!alive) return;
        const msgs = data && data.turns === null ? [] : (data.messages || []);
        setMessages(msgs);                       // full-replace (resume-safe)
        if (msgs.length && !transcriptSeen.value[pid]) {
          transcriptSeen.value = { ...transcriptSeen.value, [pid]: true };
        }
      } catch (_) {
        /* transient; the next tick retries */
      }
      if (alive) timer = setTimeout(tick, TURNS_POLL_MS);
    }
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [target, pid, selected]);
  return messages;
}

function toolSummary(t) {
  const inp = t.input || {};
  switch (t.name) {
    case "Bash": return inp.command || "";
    case "Read":
    case "Edit":
    case "Write": return inp.file_path || "";
    default: return JSON.stringify(inp).slice(0, 200);
  }
}

function ToolCall({ t }) {
  const [open, setOpen] = useState(false);
  const running = t.result == null;
  return (
    <div class="toolcall">
      <button class="toolcall-head" onClick={() => setOpen(!open)}>
        <span class="toolcall-name">{t.name}</span>
        <span class="toolcall-summary">{toolSummary(t)}</span>
        {running && <span class="toolcall-running">running…</span>}
      </button>
      {open && t.name === "Edit" && (
        <pre class="toolcall-diff">{`- ${t.input?.old_string || ""}\n+ ${t.input?.new_string || ""}`}</pre>
      )}
      {open && !running && t.name !== "Edit" && (
        <pre class="toolcall-output">{t.result}</pre>
      )}
    </div>
  );
}

function TurnSegment({ m }) {
  const [open, setOpen] = useState(false);
  const preview = (m.text || "").split("\n").find((l) => l.trim()) || "";
  return (
    <div class={`turn turn-${m.role}`}>
      <button class="turn-head" onClick={() => setOpen(!open)}>
        <span class="turn-role">{m.role}</span>
        <span class="turn-time">{relTime(Math.floor((m.ts_ms || 0) / 1000))}</span>
        <span class="turn-preview">{preview.slice(0, 140)}</span>
      </button>
      {open && (
        <div class="turn-body">
          {m.text && <div class="turn-text">{m.text}</div>}
          {(m.tool_uses || []).map((t, i) => <ToolCall key={t.id || i} t={t} />)}
        </div>
      )}
    </div>
  );
}

export function TranscriptView({ target, pid, selected }) {
  const messages = useTranscriptPoll(target, pid, selected);
  if (!messages.length) {
    return <div class="transcript transcript-empty">No transcript yet.</div>;
  }
  return (
    <div class="transcript">
      {messages.map((m) =>
        m.role === "system" && m.kind === "compact"
          ? <hr key={m.uuid} class="transcript-compact" />
          : <TurnSegment key={m.uuid} m={m} />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit (compiles on next bundle build in Task 6)**

```bash
git add static/src/split/Transcript.jsx
git commit -m "split: Transcript.jsx — TranscriptView + poll hook + TurnSegment/ToolCall"
```

---

### Task 6: wire the toggle + keep-mounted transcripts into `Detail.jsx`

**Files:**
- Modify: `static/src/split/Detail.jsx`
- Modify: `static/styles.css`

- [ ] **Step 1: Imports + mode helpers**

In `static/src/split/Detail.jsx`, extend the store import and add the Transcript import:

```js
import { windows, activeTarget, railSelection, transcriptMode, transcriptSeen } from "../store.js";
import { TranscriptView } from "./Transcript.jsx";
```

Add these module-level helpers (near `lookupWindow`):

```js
function computeMode(w) {
  if (!w || !w.is_claude) return "terminal";
  const explicit = transcriptMode.value[w.pid];
  if (explicit) return explicit;
  return transcriptSeen.value[w.pid] ? "transcript" : "terminal";
}

function setTranscriptMode(pid, mode) {
  transcriptMode.value = { ...transcriptMode.value, [pid]: mode };
}
```

- [ ] **Step 2: Add the toggle to `PaneHeader`**

Change the `PaneHeader` signature to accept `mode`/`onMode`, and append the toggle to its rendered output. Update the function header line:

```jsx
function PaneHeader({ w, mode, onMode }) {
```

Immediately before the final `return` of `PaneHeader` (which builds `parts`), add the toggle as the last entry when the pane is Claude:

```jsx
  if (w.is_claude) {
    parts.push(
      <span class="detail-mode-toggle">
        <button class={mode === "transcript" ? "is-active" : ""}
                onClick={() => onMode("transcript")}>Transcript</button>
        <button class={mode === "terminal" ? "is-active" : ""}
                onClick={() => onMode("terminal")}>Terminal</button>
      </span>
    );
  }
```

- [ ] **Step 3: `PaneDetail` — compute mode, hide the terminal in transcript mode**

The real `PaneDetail` (`Detail.jsx:187-211`) renders `<PaneHeader w={w} />`, then a `.detail-pane-body` containing `<Terminal key={w.pid} .../>` and `<SidePanel key={w.target} target={w.target} />`. Replace it with the version below. **Two things must be preserved exactly:** the `<SidePanel>` call keeps `key={w.target} target={w.target}` (it takes `target`, not `w`, and the key wipes stale paneData on pane switch — passing `w` blanks the whole sidebar), and `<Terminal>` keeps `key={w.pid}`. The only changes are: header gets `mode`/`onMode`, and `<Terminal>` is wrapped in a display-toggled host (`display:contents` keeps it a direct layout child when shown). `<TranscriptView>` is intentionally NOT rendered here — it lives as a kept-mounted sibling in `Detail()` (Step 4):

```jsx
function PaneDetail({ w }) {
  const mode = computeMode(w);
  // Set the shared paste/active-target before anything else (coupling #5).
  useEffect(() => {
    activeTarget.value = w.target;
  }, [w.target]);

  return (
    <div id="detail-pane" class="detail-pane">
      <PaneHeader w={w} mode={mode} onMode={(next) => setTranscriptMode(w.pid, next)} />
      <div class="detail-pane-body">
        <div class="detail-term-host" style={mode === "terminal" ? "display:contents" : "display:none"}>
          <Terminal
            key={w.pid}
            id="detail-xterm"
            class="detail-xterm"
            target={w.target}
            onPaste={handleDetailPaste}
          />
        </div>
        {/* Keyed on target so switching panes wipes paneData rather than
            showing the previous pane's PR/notes for ~1.5s until the new tick
            lands. SidePanel takes `target`, NOT `w`. */}
        <SidePanel key={w.target} target={w.target} />
      </div>
    </div>
  );
}
```

- [ ] **Step 4: `Detail()` — keep every opened transcript mounted**

In the `Detail()` function, mirror the review-iframe keep-mounted pattern for transcripts. After the existing `paneW` computation and before the `return`, add:

```jsx
  // Keep every opened Claude transcript mounted (CSS-hidden when not active) so
  // scroll position + expanded segments survive pane switches — same discipline
  // as the review iframes above. Pruned to pids still live in /api/state.
  const openedTr = useRef(new Set());
  if (isPane && paneW?.is_claude) openedTr.current.add(paneW.pid);
  const livePids = new Set(ws.map((x) => x.pid));
  const selMode = computeMode(paneW);
  for (const pid of [...openedTr.current]) {
    const isSelected = isPane && paneW?.pid === pid;
    if (!isSelected && !livePids.has(pid)) openedTr.current.delete(pid);
  }
  const trPids = [...openedTr.current];
```

Then add the transcript siblings to the returned `<section id="detail">`, after the review map:

```jsx
      {trPids.map((pid) => {
        const tw = lookupWindow(pid);
        const isSelected = isPane && paneW?.pid === pid;
        const shown = isSelected && selMode === "transcript";
        return (
          <div key={`tr:${pid}`} class="detail-transcript-host"
               style={shown ? "display:block" : "display:none"}>
            <TranscriptView target={tw?.target} pid={pid} selected={isSelected} />
          </div>
        );
      })}
```

- [ ] **Step 5: Add styles**

Append to `static/styles.css`:

```css
/* Transcript content mode (split-view detail).
   The host is absolutely positioned over #detail's BODY region — it must start
   BELOW #detail-pane-header so the Transcript/Terminal toggle stays visible and
   clickable. PaneDetail still renders its header in normal flow at the top of
   #detail (its body's terminal is display:none in transcript mode), so the host
   is offset by the header height. #detail is full-height from the split layout,
   so top+bottom anchoring fills the body. */
#detail { position: relative; }
#detail-pane-header { position: relative; z-index: 2; }
.detail-transcript-host {
  position: absolute;
  top: 2.4em;          /* ≈ #detail-pane-header height — verify in browser (Step 7) */
  left: 0; right: 0; bottom: 0;
  z-index: 1;
  overflow-y: auto;
  background: var(--bg, #111);
}
.detail-mode-toggle { margin-left: auto; display: inline-flex; gap: 2px; }
.detail-mode-toggle button {
  font: inherit; padding: 1px 8px; cursor: pointer;
  border: 1px solid var(--border, #333); background: transparent; color: inherit;
}
.detail-mode-toggle button.is-active { background: var(--accent, #2a4); color: #fff; }
.transcript { padding: 8px 10px; font-size: 13px; }
.transcript-empty { opacity: 0.6; padding: 16px; }
.transcript-compact { border: 0; border-top: 1px dashed var(--border, #555); margin: 10px 0; }
.turn { margin: 4px 0; border-left: 2px solid transparent; }
.turn-user { border-left-color: #49f; }
.turn-assistant { border-left-color: #a6f; }
.turn-head {
  display: flex; gap: 8px; width: 100%; text-align: left; cursor: pointer;
  background: transparent; border: 0; color: inherit; padding: 3px 4px;
}
.turn-role { text-transform: uppercase; font-size: 10px; opacity: 0.7; min-width: 64px; }
.turn-time { opacity: 0.5; min-width: 32px; }
.turn-preview { opacity: 0.85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.turn-body { padding: 4px 4px 8px 12px; }
.turn-text { white-space: pre-wrap; margin-bottom: 6px; }
.toolcall { margin: 3px 0; border: 1px solid var(--border, #333); border-radius: 3px; }
.toolcall-head {
  display: flex; gap: 8px; width: 100%; text-align: left; cursor: pointer;
  background: transparent; border: 0; color: inherit; padding: 3px 6px;
}
.toolcall-name { font-weight: 600; }
.toolcall-summary { opacity: 0.8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; }
.toolcall-running { color: #fb4; }
.toolcall-output, .toolcall-diff { margin: 0; padding: 6px; white-space: pre-wrap; overflow-x: auto; max-height: 320px; }
```

(The absolute-positioned `.detail-transcript-host` over `#detail` is the layout coupling the structure doc flagged — verify in Step 6 it covers the detail body cleanly and the header/toggle stay visible. If the header is occluded, scope the host to sit below `#detail-pane-header` instead of `inset:0` on `#detail`.)

- [ ] **Step 6: Build the bundle**

Run: `npm run build`
Expected: Vite writes `static/dist/app.js` with no errors.

- [ ] **Step 7: Manual verification (dev server on :8766)**

Verify each, on a real Claude pane (start dev server per Execution context):

1. Selecting an **established** Claude pane lands on **Transcript** within a moment (auto-promote), no lasting flash.
2. The transcript lists turns; expanding a turn shows its text + tool calls; a Bash tool call shows the command and (expanded) its output; an Edit shows old/new.
3. An **in-flight** tool call shows `running…`, then fills its result in place on a later poll **without the segment collapsing** (expand state persists).
4. Toggle to **Terminal**: the xterm shows the live pane and does **not** reload (watch for a reconnect/repaint, not a fresh capture). Toggle back to Transcript: no reload either way.
5. Expand a couple of turns, scroll up, switch to another pane, switch back: **scroll position and expanded turns are preserved**.
6. A **freshly started** Claude pane (sitting at the channel-accept / first-prompt screen) shows **Terminal**, not an empty Transcript.
7. A **shell** (non-Claude) pane shows Terminal only, no toggle.
8. Timestamps render as sane deltas (`3m`, `1h`), not a ~57-year value.
9. In Transcript mode the **Transcript/Terminal toggle stays visible and clickable** (not occluded by the transcript host) — adjust the host `top` to the real header height if it overlaps.
10. The metadata **sidebar (Linked/Notes/Activity) still renders** in both modes — confirm the `<SidePanel target=...>` poll wasn't broken by the `PaneDetail` edit (regression guard for Must-fix #1).
11. A Claude pane that scrolls back into shell scrollback may flip `is_claude` off (invariant 2) — confirm the toggle appearing/disappearing with that flip is acceptable, not jarring.

- [ ] **Step 8: Commit**

```bash
git add static/src/split/Detail.jsx static/styles.css static/dist/app.js
git commit -m "split: Transcript⇄Terminal toggle + keep-mounted transcripts in detail pane"
```

---

## Phase 3 — Polish (build + manual-verify)

### Task 7: resume path-flip + current-turn highlight + edge cases

**Files:**
- Modify: `static/src/split/Transcript.jsx`
- Modify: `static/styles.css`

- [ ] **Step 1: Current-turn highlight**

In `TranscriptView`, mark the newest non-system turn. Compute it and pass a flag:

```jsx
  const lastTurnUuid = [...messages].reverse().find((m) => m.role !== "system")?.uuid;
```

Pass `current={m.uuid === lastTurnUuid}` to `<TurnSegment>` and add `class={... ${current ? "turn-current" : ""}}` on the wrapper. Add to `static/styles.css`:

```css
.turn-current { background: rgba(120, 160, 255, 0.08); }
```

- [ ] **Step 2: Build**

Run: `npm run build`
Expected: clean build.

- [ ] **Step 3: Manual verification — resume path-flip (the spec's flagged test)**

With a Claude pane selected in Transcript mode, run `claude --resume` in that pane (or start a new session in the same cwd). Confirm: the transcript full-replaces with the resumed/new session's turns on the next poll (no stale merge, no duplicated turns, no crash). The newest turn carries the highlight.

- [ ] **Step 4: Commit**

```bash
git add static/src/split/Transcript.jsx static/styles.css static/dist/app.js
git commit -m "split: current-turn highlight + resume path-flip verified"
```

---

## Integration: merge to prod

- [ ] **Step 1: Full backend suite green**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 2: Merge the worktree branch + restart prod**

Per CLAUDE.md "Development workflow":

```bash
cd ~/dev/periscope
git merge <feature-branch>
bin/periscope restart
git worktree remove <worktree-path>
```

- [ ] **Step 3: Smoke prod (:8765)** — open the dashboard, select a Claude pane in split view, confirm the transcript renders and the toggle works.

---

## Self-review notes

- **Spec coverage:** server parser (Task 1), resolver (Task 2), endpoint (Task 3), signals (Task 4), renderer + poll (Task 5), toggle + keep-mounted persistence + default-mode/auto-promote (Task 6), current-turn highlight + resume (Task 7). Shell blocks / OSC 133 are out of scope per the spec.
- **Deferred (not in this plan), per spec:** server-side parse/`since_ts` cache — only if profiling shows the full-resend is too heavy.
- **Type consistency:** message shape `{role, uuid, ts_ms, text, tool_uses?}` and tool shape `{id, name, input, result}` are used identically in `messages_from_jsonl` (Task 1), the route payload (Task 3), and the renderer (Tasks 5/7). `computeMode`/`setTranscriptMode` names match between Detail.jsx steps.
