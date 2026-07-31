# Multi-Account Capacity Pooling Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any periscope pane run on either of two Claude subscription accounts, so work can be spread across both weekly limits.

**Architecture:** Account identity is a `CLAUDE_CONFIG_DIR` pointed at a thin-shell config dir (`~/.claude-b`) whose `projects/`/`sessions/` symlink back to `~/.claude`, so all read paths are unchanged. The account reaches a pane as a tmux **window** environment variable (`-e`), not a command-string prefix, so it survives the user re-running `claude` by hand.

**Tech Stack:** Python 3.14 / FastAPI, tmux 3.6a, SQLite, Preact.

**Spec:** `docs/specs/multi-account-pooling.md`

**Deliberate spec divergence:** the spec's "One command-builder" section says to add an account parameter to `config.build_agent_command`. This plan does **not**. That function returns an argv `list[str]`, and an environment variable has no argv slot — an account parameter there could only re-introduce the string prefix the spec rejects elsewhere. Account passing is `-e` at every window-creation site instead. The spec section is superseded.

**Already shipped (do not redo):** `resurrect.py` re-emits the prefix into the save file (commit `c360876`); `~/.tmux.conf` uses `@resurrect-processes '~claude'`.

**Out of scope:** per-account usage meters. `usage.py`'s single unfiltered keychain lookup means only one account is ever metered, so nothing is corrupted by deferring. That work needs its own plan once the credential-location question in the spec's "Open questions" is answered.

**The invariant this plan must not break:** an account-A pane must never silently run on B, or vice versa. Failure must be visible (a pane that won't authenticate), never silent (a pane billing the wrong subscription).

---

### Task 1: Account registry in `state.json`

**Files:**
- Modify: `periscope/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`. Note the in-body import — this file has no module-level `store` import:

```python
def test_default_accounts_present(clean_state):
    import periscope.store as store
    accts = store.get_accounts()
    assert [a["id"] for a in accts] == ["default", "b"]
    assert accts[0]["config_dir"] == ""


def test_account_config_dir_resolves(clean_state):
    import periscope.store as store
    assert store.account_config_dir("default") == ""
    assert store.account_config_dir("b").endswith("/.claude-b")
    # unknown id fails OPEN to the default account, never to a guess
    assert store.account_config_dir("nope") == ""
    assert store.account_config_dir(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -k account -v`
Expected: FAIL — `AttributeError: module 'periscope.store' has no attribute 'get_accounts'`

- [ ] **Step 3: Write the implementation**

In `periscope/store.py`, next to `_DEFAULT_COMMANDS` (`:309`). `Path` (`:62`) and `cast` (`:63`) are already imported:

```python
class Account(TypedDict, total=False):
    id: str          # stable key; "default" is the machine's ~/.claude login
    label: str       # shown in the launcher
    config_dir: str  # "" for the default account, else an absolute path


# Exactly two accounts (see spec "Not doing"). A list, so widening is
# mechanical. `config_dir` is the de-facto primary key: the Claude credential
# binds to the PATH, so changing it orphans that account's login.
_DEFAULT_ACCOUNTS: list[Account] = [
    {"id": "default", "label": "account A", "config_dir": ""},
    {"id": "b", "label": "account B", "config_dir": str(Path.home() / ".claude-b")},
]


def get_accounts() -> list[Account]:
    """Snapshot of the account registry (copies of each entry)."""
    with _STATE_LOCK:
        accts = _STATE.get("accounts") or _DEFAULT_ACCOUNTS
        return [cast(Account, dict(a)) for a in accts]


def account_config_dir(account_id: str | None) -> str:
    """CLAUDE_CONFIG_DIR for an account id, or "" for the default account.

    Fails OPEN to the default account on an unknown id: an unknown id is a
    periscope bug, and the default is the one account guaranteed to be logged
    in. A pane that fails to authenticate is recoverable; one silently billing
    the wrong subscription is not.
    """
    if not account_id or account_id == "default":
        return ""
    for a in get_accounts():
        if a.get("id") == account_id:
            return a.get("config_dir", "")
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store.py -k account -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add periscope/store.py tests/test_store.py
git commit -m "feat(store): two-account registry with config-dir resolution, failing open to the default account"
```

---

### Task 2: tmux env helpers (args + session scrub)

The scrub is not optional. `tmux new-session -e VAR=val` sets the **session** environment, and periscope has one shared `MANAGED_SESSION` — without scrubbing, every later window inherits the first window's account.

**Files:**
- Modify: `periscope/tmux.py`
- Test: `tests/test_tmux.py`

- [ ] **Step 1: Write the failing test**

`tests/test_tmux.py` imports names directly (`:9-17`), so add `env_args` to that import list and call it bare:

```python
def test_env_args_empty_for_default_account():
    assert env_args("") == []
    assert env_args(None) == []


def test_env_args_builds_e_flag():
    assert env_args("/Users/tom/.claude-b") == [
        "-e", "CLAUDE_CONFIG_DIR=/Users/tom/.claude-b"
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tmux.py -k env_args -v`
Expected: FAIL — `ImportError: cannot import name 'env_args'`

- [ ] **Step 3: Write the implementation**

In `periscope/tmux.py`:

```python
def env_args(config_dir: str | None) -> list[str]:
    """`-e CLAUDE_CONFIG_DIR=...` args for new-window/new-session, or [].

    Set on the window rather than prefixed onto the command string so the
    binding lives in the pane's process environment: a user re-running
    `claude` by hand in that pane stays on the same account, and resurrect
    reads it back off the live process at save time.
    """
    if not config_dir:
        return []
    return ["-e", f"CLAUDE_CONFIG_DIR={config_dir}"]


def scrub_session_env(session: str) -> None:
    """Unset CLAUDE_CONFIG_DIR from a SESSION's environment.

    `new-session -e` sets the session env, not just the first window's — so
    every later window in periscope's single shared session would inherit the
    first window's account, silently billing account-A panes to account B.
    The first window's shell has already forked with the value, so scrubbing
    immediately after creation keeps that pane correct and leaves the session
    clean for the next one.
    """
    _tmux_mutate("set-environment", "-t", f"={session}", "-u", "CLAUDE_CONFIG_DIR")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tmux.py -k env_args -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux.py tests/test_tmux.py
git commit -m "feat(tmux): env_args + scrub_session_env for per-window CLAUDE_CONFIG_DIR"
```

---

### Task 3: `+ New tab` opens on a chosen account

**Files:**
- Modify: `periscope/routes/sessions.py:182` (`_window_new_plain`), `:229-232` (new-session), `:236-239` (new-window), `:364` (`window_new`)
- Test: `tests/routes/test_sessions.py`

- [ ] **Step 1: Write the failing test**

`_run` must be patched — `sessions.py:227` shells out to a real `tmux has-session`, which decides which branch runs and is otherwise environment-dependent. The `tmux` stub must dispatch on its arguments, or `pane_id` comes back as a bogus `"9"` and hits real `tracks.move_pane`:

```python
def _tmux_fake(*args):
    if "#{window_index}" in args:
        return "9"
    if "#{pane_id}" in args:
        return "%99"
    return ""


def test_window_new_passes_account_env_to_tmux(client, monkeypatch):
    calls: list[tuple] = []

    def fake_mutate(*args):
        calls.append(args)
        return True, "@9"

    monkeypatch.setattr("periscope.routes.sessions._run", lambda *a, **k: (True, ""))
    monkeypatch.setattr("periscope.routes.sessions._tmux_mutate", fake_mutate)
    monkeypatch.setattr("periscope.routes.sessions.tmux", _tmux_fake)
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp", lambda *a: None)
    monkeypatch.setattr("periscope.tracks.move_pane", lambda *a: None)

    client.post("/api/window/new?session=/repo&mode=claude&account=b")

    created = [c for c in calls if c and c[0] in ("new-window", "new-session")]
    assert created, "no window created"
    flat = list(created[0])
    assert "-e" in flat
    assert any(a.startswith("CLAUDE_CONFIG_DIR=") and a.endswith("/.claude-b") for a in flat)


def test_window_new_default_account_sends_no_env(client, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("periscope.routes.sessions._run", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        "periscope.routes.sessions._tmux_mutate",
        lambda *a: (calls.append(a), (True, "@9"))[1],
    )
    monkeypatch.setattr("periscope.routes.sessions.tmux", _tmux_fake)
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp", lambda *a: None)
    monkeypatch.setattr("periscope.tracks.move_pane", lambda *a: None)

    client.post("/api/window/new?session=/repo&mode=claude")

    created = [c for c in calls if c and c[0] in ("new-window", "new-session")]
    assert created
    assert "-e" not in list(created[0])


def test_new_session_env_is_scrubbed(client, monkeypatch):
    """Otherwise every later window inherits the first window's account."""
    calls: list[tuple] = []

    def fake_mutate(*args):
        calls.append(args)
        return True, "@9"

    monkeypatch.setattr("periscope.routes.sessions._run", lambda *a, **k: (False, ""))
    monkeypatch.setattr("periscope.routes.sessions._tmux_mutate", fake_mutate)
    monkeypatch.setattr("periscope.routes.sessions.tmux", _tmux_fake)
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp", lambda *a: None)
    monkeypatch.setattr("periscope.tracks.move_pane", lambda *a: None)

    client.post("/api/window/new?session=/repo&mode=claude&account=b")

    assert any(c[0] == "set-environment" and "-u" in c and "CLAUDE_CONFIG_DIR" in c
               for c in calls), f"session env never scrubbed: {calls}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routes/test_sessions.py -k "account or scrub" -v`
Expected: FAIL — no `-e`, no `set-environment`.

- [ ] **Step 3: Write the implementation**

Add `account` to `_window_new_plain`'s signature (`:182`):

```python
def _window_new_plain(
    track_id: str, exec_cmd: str, mode: str,
    cwd_param: str | None = None, branch: str | None = None,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
) -> dict:
```

Resolve once after `repo` is computed:

```python
    config_dir = store.account_config_dir(account)
```

Add `*tmux_mod.env_args(config_dir)` to **both** the `new-session` (`:229-232`) and `new-window` (`:236-239`) calls, and scrub immediately after the `new-session` branch succeeds:

```python
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", MANAGED_SESSION, "-c", cwd,
            *tmux_mod.env_args(config_dir),
            "-P", "-F", "#{window_id}",
        )
        if not ok:
            raise HTTPException(500, msg)
        if config_dir:
            tmux_mod.scrub_session_env(MANAGED_SESSION)
```

Add `account: str | None = None` to the `window_new` route (`:364`) and thread it into the `_window_new_plain(...)` call.

Imports: `from periscope import store` and `from periscope import tmux as tmux_mod` — the module already imports the `tmux()` *function* (`:42`), so the module must be aliased to avoid shadowing.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/routes/test_sessions.py -k "account or scrub" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Verify no regression**

Run: `uv run pytest tests/routes/test_sessions.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "feat(sessions): account param on /api/window/new, with the session-env scrub that keeps it per-window"
```

---

### Task 4: Launcher UI for account selection

Without this there is no way to pick an account except curl.

**Files:**
- Modify: `static/src/overlays/LauncherModal.jsx` (the `/api/window/new` call at `:243`)
- Test: `static/src/overlays/__tests__/LauncherModal.test.js` (create if absent)

- [ ] **Step 1: Write the failing test**

```js
import { describe, it, expect } from "vitest";
import { accountQuery } from "../LauncherModal.jsx";

describe("accountQuery", () => {
  it("omits the param for the default account", () => {
    expect(accountQuery("default")).toBe(null);
    expect(accountQuery(null)).toBe(null);
  });
  it("passes a non-default account through", () => {
    expect(accountQuery("b")).toBe("b");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- LauncherModal`
Expected: FAIL — `accountQuery` is not exported.

- [ ] **Step 3: Write the implementation**

In `LauncherModal.jsx`, export the pure helper and use it:

```js
export function accountQuery(account) {
  return !account || account === "default" ? null : account;
}
```

Add a two-pill account selector to the modal body, defaulting to `"default"`, sourced from `prefs`/the registry, and in the submit path:

```js
const acct = accountQuery(account);
if (acct) qs.set("account", acct);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test -- LauncherModal`
Expected: PASS

- [ ] **Step 5: Rebuild the bundle and commit**

```bash
npm run build
git add static/src/overlays/LauncherModal.jsx static/src/overlays/__tests__/LauncherModal.test.js static/dist/app.js
git commit -m "feat(launcher): account picker on + New tab"
```

---

### Task 5: `_window_new_resume` stops discarding its `exec_cmd`

Pre-existing bug, verified: `sessions.py:143` sends `f"{CLAUDE_EXEC} --resume {resume_id}"` while `:167` (the existing-session branch) honours `exec_cmd`. Left unfixed, Task 6's account-aware resume command is ignored on exactly the path that runs after a tmux restart.

**Files:**
- Modify: `periscope/routes/sessions.py:143`
- Test: `tests/routes/test_sessions.py`

- [ ] **Step 1: Write the failing test**

`get_session` is a **function-local** import inside `_window_new_resume` (`from history.search import get_session`), so it must be patched at its source module, not on `periscope.routes.sessions`:

```python
def test_window_new_resume_honours_caller_exec_cmd(monkeypatch):
    from periscope.routes import sessions
    sent: list[str] = []

    monkeypatch.setattr("history.search.get_session",
                        lambda _id: {"project_path": "/tmp"})
    monkeypatch.setattr("periscope.routes.sessions._run", lambda *a, **k: (False, ""))
    monkeypatch.setattr("periscope.routes.sessions._tmux_mutate", lambda *a: (True, "0"))
    monkeypatch.setattr("periscope.routes.sessions.tmux", _tmux_fake)
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp",
                        lambda target, cmd: sent.append(cmd))
    monkeypatch.setattr("periscope.tracks.move_pane", lambda *a: None)

    sessions._window_new_resume("resumes", "PFX=1 claude --resume abc", "abc", "resume")

    assert sent, "nothing sent"
    assert sent[0].startswith("PFX=1 "), f"caller command was discarded: {sent[0]!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routes/test_sessions.py -k resume_honours -v`
Expected: FAIL — sent command starts with `claude`, not `PFX=1`.

- [ ] **Step 3: Write the implementation**

At `sessions.py:143`, use the caller's command when present, falling back to the rebuild only when it is empty. Keep the fallback on **this** (create-session) branch only — the existing-session branch at `:167` currently sends nothing for an empty `exec_cmd`, and changing that would alter reachable behaviour from `channels.py`:

```python
    cmd = exec_cmd.strip() or shlex.join(
        config.build_agent_command("claude", cwd=cwd, resume_id=resume_id)
    )
    _send_and_stamp(target, cmd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/routes/test_sessions.py -k resume -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "fix(sessions): _window_new_resume honours the caller's exec_cmd on the create-session branch"
```

---

### Task 6: MCP spawn/resume tools accept an account

**Files:**
- Modify: `periscope/channels.py:509` (`_do_spawn_claude_tool`, async), `:855` (`_do_resume_session_tool`, sync), `:588-592` (new-session branch)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

`pytest-asyncio` is **not installed** and `--strict-markers` is on, so `@pytest.mark.asyncio` errors at collection — drive the coroutine with `asyncio.run`. Window creation goes through `_tmux_mutate`, not `tmux`, and the consent/settle loops must be short-circuited or the test burns ~10s:

```python
def test_spawn_claude_sets_account_env(monkeypatch):
    import asyncio
    from periscope import channels

    calls: list[tuple] = []
    monkeypatch.setattr("periscope.channels._tmux_mutate",
                        lambda *a: (calls.append(a), (True, "1"))[1])
    monkeypatch.setattr("periscope.channels.tmux", lambda *a: "1")
    monkeypatch.setattr("periscope.channels._dev_channels_consent_visible",
                        lambda *a, **k: False)
    monkeypatch.setattr("periscope.channels._plain_pane_snapshot",
                        lambda *a, **k: "auto mode on")

    asyncio.run(channels._do_spawn_claude_tool(
        "%1", {"prompt": "hi", "cwd": "/tmp", "account": "b"}
    ))

    created = [c for c in calls if c and c[0] in ("new-window", "new-session")]
    assert created, "no window created"
    assert any(a.startswith("CLAUDE_CONFIG_DIR=") for a in created[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py -k account -v`
Expected: FAIL — no `CLAUDE_CONFIG_DIR` in the tmux args.

- [ ] **Step 3: Write the implementation**

In `_do_spawn_claude_tool`, resolve the account and add env args to both creation branches, scrubbing after `new-session`:

```python
    config_dir = store.account_config_dir(arguments.get("account"))
```

```python
    ok, msg = _tmux_mutate("new-window", "-t", f"{session}:", "-c", cwd,
                           *tmux_mod.env_args(config_dir),
                           "-P", "-F", "#{window_index}")
```

```python
    if config_dir:
        tmux_mod.scrub_session_env(session)
```

Declare `account` in the tool's input schema next to `workspace`/`cwd`:

```python
"account": {
    "type": "string",
    "enum": ["default", "b"],
    "description": "Which Claude subscription to run the spawned pane on. "
                   "Omit for the default account.",
},
```

For `_do_resume_session_tool` (sync — do **not** add `await`), pass a prefixed command string, since it delegates to `_window_new_resume`, which owns window creation internally. This is the one place a string prefix remains correct, and Task 5 is what makes it survive:

```python
    prefix = f"CLAUDE_CONFIG_DIR={config_dir} " if config_dir else ""
    result = _window_new_resume(
        tmux_session, f"{prefix}{CLAUDE_EXEC} --resume {session_id}",
        session_id, "resume",
    )
```

Imports: `channels.py` imports specific names from `store` (`:38`) and the `tmux` *function* (`:40`) — add `from periscope import store` and `from periscope import tmux as tmux_mod`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_channels.py -k account -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): account argument on spawn_claude and resume_session"
```

---

### Task 7: The unified-open surface honours the account

`worktree_spawn._layout_two_window` (`:252-265`) creates panes for `open_ops.py:198,200` — the ⌘K omnibox, `POST /api/open`, PR review and new-project flows. Without this, every pane from periscope's primary launch path is permanently account A.

**Files:**
- Modify: `periscope/worktree_spawn.py:216` (`_layout_two_window`), `:252-265`; `periscope/open_ops.py:198,200`
- Test: `tests/test_worktree_spawn.py`

- [ ] **Step 1: Write the failing test**

```python
def test_layout_two_window_passes_account_env(monkeypatch):
    from periscope import worktree_spawn
    calls: list[tuple] = []
    monkeypatch.setattr("periscope.worktree_spawn._tmux_mutate",
                        lambda *a: (calls.append(a), (True, "@1"))[1])
    monkeypatch.setattr("periscope.worktree_spawn.tmux", lambda *a: "1")
    monkeypatch.setattr("periscope.worktree_spawn.stamp_new_window", lambda *a: "pid1")

    worktree_spawn._layout_two_window("sess", "/tmp", account="b")

    created = [c for c in calls if c and c[0] in ("new-window", "new-session")]
    assert created
    assert any(a.startswith("CLAUDE_CONFIG_DIR=") for a in created[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worktree_spawn.py -k account -v`
Expected: FAIL — `_layout_two_window() got an unexpected keyword argument 'account'`

- [ ] **Step 3: Write the implementation**

Add `account: str | None = None` to `_layout_two_window`, resolve `config_dir = store.account_config_dir(account)`, add `*tmux.env_args(config_dir)` to both creation calls, and scrub after the `new-session` branch. Thread `account` through `open_ops.ensure_session` to both call sites (`open_ops.py:198,200`), defaulting to `None` so existing callers are unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worktree_spawn.py -k account -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/worktree_spawn.py periscope/open_ops.py tests/test_worktree_spawn.py
git commit -m "feat(open): unified-open surface honours the selected account"
```

---

### Task 8: Background commander bills a chosen account

`bg_commander._dispatch_env` (`:157`) returns `{**os.environ, …}` from the launchd server's environment, which never contains `CLAUDE_CONFIG_DIR` — so every background commander bills account A forever. One line, no tmux seam, no UI: the cheapest capacity lever in this plan.

**Files:**
- Modify: `periscope/bg_commander.py:157` (`_dispatch_env`), `periscope/store.py` (setting)
- Test: `tests/test_bg_commander.py`

- [ ] **Step 1: Write the failing test**

```python
def test_dispatch_env_sets_account_config_dir(monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr("periscope.store.get_settings", lambda: {"bg_account": "b"})
    env = bg_commander._dispatch_env(handle="h1")
    assert env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")


def test_dispatch_env_default_account_unset(monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr("periscope.store.get_settings", lambda: {})
    env = bg_commander._dispatch_env(handle="h1")
    assert "CLAUDE_CONFIG_DIR" not in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bg_commander.py -k dispatch_env -v`
Expected: FAIL — `KeyError: 'CLAUDE_CONFIG_DIR'`

- [ ] **Step 3: Write the implementation**

In `_dispatch_env`, after the `ANTHROPIC_*` strip (which exists so the commander bills the subscription rather than API credits — the same reasoning extends to *which* subscription):

```python
    config_dir = store.account_config_dir(store.get_settings().get("bg_account"))
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
```

Default is unset, so behaviour is unchanged until explicitly switched.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bg_commander.py -k dispatch_env -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Point the commander at account B**

```bash
curl -s -X POST http://127.0.0.1:8765/api/prefs/settings \
  -H 'Content-Type: application/json' -d '{"bg_account":"b"}'
```
Expected: the setting persists; the next commander job runs on account B.

- [ ] **Step 6: Commit**

```bash
git add periscope/bg_commander.py tests/test_bg_commander.py
git commit -m "feat(bg-commander): bill background jobs to a configurable account"
```

---

### Task 9: Show which account a pane is on

A feature for balancing two limits is unusable if you cannot see which subscription a pane is using. `resurrect._pane_config_dirs()` already derives this live from each pane's process environment and encodes the `ps eww` command/env delimiter trap — reuse it rather than reinventing the scan or adding a table.

**Files:**
- Modify: `periscope/window_view.py`, `static/src/split/RailRows.jsx`
- Test: `tests/test_window_view.py`

- [ ] **Step 1: Write the failing test**

```python
def test_window_view_reports_account(monkeypatch):
    from periscope import window_view
    monkeypatch.setattr("periscope.resurrect._pane_config_dirs",
                        lambda: {"%7": "/Users/tom/.claude-b"})
    view = window_view.build_window_view(_fake_window(pane_id="%7"))
    assert view["account"] == "b"


def test_window_view_default_account_is_none(monkeypatch):
    from periscope import window_view
    monkeypatch.setattr("periscope.resurrect._pane_config_dirs", lambda: {})
    view = window_view.build_window_view(_fake_window(pane_id="%7"))
    assert view.get("account") in (None, "default")
```

Use the module's existing window-dict helper for `_fake_window`; match whatever `tests/test_window_view.py` already does.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_window_view.py -k account -v`
Expected: FAIL — `KeyError: 'account'`

- [ ] **Step 3: Write the implementation**

Map config dir back to an account id via the registry (reverse of `store.account_config_dir`) and stamp `account` onto the view. Cache `_pane_config_dirs()` per poll — it forks `ps` — and render a small `B` chip in `RailRows.jsx` for any pane whose `account` is not the default.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_window_view.py -q && npm test`
Expected: all pass.

- [ ] **Step 5: Rebuild and commit**

```bash
npm run build
git add periscope/window_view.py static/src/split/RailRows.jsx static/dist/app.js tests/test_window_view.py
git commit -m "feat(rail): account chip so a pane's subscription is visible"
```

---

### Task 10: Hook installers fan out over every config dir

Without this a B pane writes no `pane_sessions` row and the transcript view, narrator, per-pane burn and the resurrect `--resume` rewrite all go dark for it.

`bin/periscope` registers `pane_session_hook.py` on `SessionStart` and `UserPromptSubmit` only — there is **no** `SessionEnd`/history-hook block, despite what the spec says. Fix the spec while here.

**Files:**
- Modify: `bin/periscope:88` (`install-claude-hook`, heredoc `:96-124`), `:137` (`uninstall-claude-hook`), `docs/specs/multi-account-pooling.md`

- [ ] **Step 1: Loop the installer over config dirs**

Pass the settings paths as extra argv to the existing heredoc:

```sh
    python3 - "$REPO" \
      "$HOME/.claude/settings.json" \
      "$HOME/.claude-b/settings.json" <<'PY'
import json, os, sys
repo = sys.argv[1]
cmd = f"python3 {repo}/pane_session_hook.py"
for p in sys.argv[2:]:
    if not os.path.isdir(os.path.dirname(p)):
        continue          # that account isn't set up on this machine
    try:
        with open(p) as f: d = json.load(f)
    except FileNotFoundError:
        d = {}
    hooks = d.setdefault("hooks", {})
    added = []
    for ev in ("SessionStart", "UserPromptSubmit"):
        groups = hooks.setdefault(ev, [])
        if any(h.get("command", "").endswith("pane_session_hook.py")
               for g in groups for h in g.get("hooks", [])):
            continue
        entry = {"type": "command", "command": cmd, "timeout": 5}
        if groups:
            groups[0].setdefault("hooks", []).append(entry)
        else:
            groups.append({"matcher": "", "hooks": [entry]})
        added.append(ev)
    if added:
        with open(p, "w") as f: json.dump(d, f, indent=2)
        print(f"installed pane-session hook in {p} on: {', '.join(added)}")
PY
```

Apply the same argv-loop to `uninstall-claude-hook` at `:137`.

- [ ] **Step 2: Verify it actually installs, from a clean slate**

`~/.claude-b/settings.json` already carries the hooks (hand-copied), so a plain re-run proves nothing. Remove them first:

```bash
python3 -c "
import json,os
p=os.path.expanduser('~/.claude-b/settings.json')
d=json.load(open(p))
for ev in ('SessionStart','UserPromptSubmit'):
    for g in d.get('hooks',{}).get(ev,[]):
        g['hooks']=[h for h in g.get('hooks',[]) if 'pane_session_hook' not in h.get('command','')]
json.dump(d,open(p,'w'),indent=2)"
bin/periscope install-claude-hook     # expect: installs into ~/.claude-b
bin/periscope install-claude-hook     # expect: prints nothing (idempotent)
python3 -c "
import json,os
for p in ('~/.claude/settings.json','~/.claude-b/settings.json'):
    d=json.load(open(os.path.expanduser(p)))
    h=d.get('hooks',{})
    for ev in ('SessionStart','UserPromptSubmit'):
        n=len([c for g in h.get(ev,[]) for c in g.get('hooks',[])
               if 'pane_session_hook' in c.get('command','')])
        print(p, ev, n)"
```
Expected: every count is exactly `1`.

- [ ] **Step 3: Correct the spec's SessionEnd claim**

In `docs/specs/multi-account-pooling.md`, drop `history hook` on `SessionEnd` from the settings.json section — `bin/periscope` does not register it.

- [ ] **Step 4: Commit**

```bash
git add bin/periscope docs/specs/multi-account-pooling.md
git commit -m "fix(hooks): register pane-session hooks in every account config dir"
```

---

## Final verification

- [ ] `uv run pytest -q` — expect all green (953 before this plan)
- [ ] `bin/check` — expect `✓ all checks passed`
- [ ] `npm run build && npm test`; `static/dist/app.js` committed
- [ ] Open a `+ New tab` on account B from the launcher; confirm it authenticates and its transcript appears in the detail view (proves Task 10's fan-out)
- [ ] **Cross-contamination check** — the invariant this plan exists to protect. Open a B pane, then an A pane in the same session, and confirm the A pane is *not* on B:
  ```sh
  tmux list-panes -a -F '#{pane_id} #{pane_pid}' | while read id pid; do
    echo "$id $(ps eww -p $(pgrep -P $pid | head -1) -o command= 2>/dev/null | grep -o 'CLAUDE_CONFIG_DIR=[^ ]*')"
  done
  ```
  Expected: only the B pane shows a value.
- [ ] Trigger a save and confirm the B pane's line carries the prefix:
  `~/.tmux/plugins/tmux-resurrect/scripts/save.sh && awk -F'\t' '$11 ~ /CLAUDE_CONFIG_DIR/' $(ls -t ~/.local/share/tmux/resurrect/*.txt | head -1)`
