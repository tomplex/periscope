# Multi-Account Capacity Pooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any periscope pane run on either of two Claude subscription accounts, so work can be spread across both weekly limits.

**Architecture:** Account identity is a `CLAUDE_CONFIG_DIR` pointed at a thin-shell config dir (`~/.claude-b`) whose `projects/`/`sessions/` symlink back to `~/.claude`, so all read paths are unchanged. The account reaches a pane as a tmux **window environment variable** (`new-window -e`), not a command-string prefix, so it survives the user re-running `claude` by hand. The registry is a two-element list in `state.json`.

**Tech Stack:** Python 3.14 / FastAPI, tmux 3.6a, SQLite, Preact.

**Spec:** `docs/specs/multi-account-pooling.md`

**Already shipped (do not redo):** `resurrect.py` re-emits the prefix into the save file (commit `c360876`), and `~/.tmux.conf` uses `@resurrect-processes '~claude'`.

**Phase 1 (Tasks 1-6)** delivers account selection end to end. **Phase 2 (Tasks 7-9)** delivers per-account usage and is gated on Task 7's discovery.

---

### Task 1: Account registry in `state.json`

**Files:**
- Modify: `periscope/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
def test_default_accounts_present(clean_state):
    accts = store.get_accounts()
    assert [a["id"] for a in accts] == ["default", "b"]
    assert accts[0]["config_dir"] == ""


def test_account_config_dir_resolves(clean_state):
    assert store.account_config_dir("default") == ""
    assert store.account_config_dir("b").endswith("/.claude-b")
    # unknown id must fail OPEN to the default account, never to a guess
    assert store.account_config_dir("nope") == ""
    assert store.account_config_dir(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -k account -v`
Expected: FAIL with `AttributeError: module 'periscope.store' has no attribute 'get_accounts'`

- [ ] **Step 3: Write the implementation**

In `periscope/store.py`, next to `_DEFAULT_COMMANDS` (line ~309):

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

    Fails OPEN to the default account on an unknown id: a pane on the wrong
    account is a billing error, but an unknown id is a periscope bug, and the
    default is the one account guaranteed to be logged in.
    """
    if not account_id or account_id == "default":
        return ""
    for a in get_accounts():
        if a.get("id") == account_id:
            return a.get("config_dir", "")
    return ""
```

Ensure `Path` and `cast` are imported (both already are; verify `from pathlib import Path` is present).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store.py -k account -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add periscope/store.py tests/test_store.py
git commit -m "feat(store): two-account registry with config-dir resolution, failing open to the default account"
```

---

### Task 2: tmux env-args helper

**Files:**
- Modify: `periscope/tmux.py`
- Test: `tests/test_tmux.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tmux.py`:

```python
def test_env_args_empty_for_default_account():
    assert tmux.env_args("") == []
    assert tmux.env_args(None) == []


def test_env_args_builds_e_flag():
    assert tmux.env_args("/Users/tom/.claude-b") == [
        "-e", "CLAUDE_CONFIG_DIR=/Users/tom/.claude-b"
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tmux.py -k env_args -v`
Expected: FAIL with `AttributeError: module 'periscope.tmux' has no attribute 'env_args'`

- [ ] **Step 3: Write the implementation**

In `periscope/tmux.py`:

```python
def env_args(config_dir: str | None) -> list[str]:
    """`-e CLAUDE_CONFIG_DIR=...` args for new-window/new-session, or [].

    Set on the WINDOW rather than prefixed onto the command string so the
    binding is in the pane's process environment: a user re-running `claude`
    by hand in that pane stays on the same account, and resurrect can read it
    back off the live process at save time.
    """
    if not config_dir:
        return []
    return ["-e", f"CLAUDE_CONFIG_DIR={config_dir}"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tmux.py -k env_args -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux.py tests/test_tmux.py
git commit -m "feat(tmux): env_args helper for per-window CLAUDE_CONFIG_DIR"
```

---

### Task 3: `+ New tab` opens on a chosen account

**Files:**
- Modify: `periscope/routes/sessions.py:182` (`_window_new_plain`), `:364` (`window_new`)
- Test: `tests/routes/test_sessions.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/routes/test_sessions.py`:

```python
def test_window_new_passes_account_env_to_tmux(client, monkeypatch, tmp_path):
    """The account must reach tmux as `-e CLAUDE_CONFIG_DIR=...` on new-window."""
    calls: list[tuple] = []

    def fake_mutate(*args):
        calls.append(args)
        return True, "@9"

    monkeypatch.setattr("periscope.routes.sessions._tmux_mutate", fake_mutate)
    monkeypatch.setattr("periscope.routes.sessions.tmux", lambda *a: "9")
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp", lambda *a: None)

    client.post("/api/window/new?session=/repo&mode=claude&account=b")

    newwin = [c for c in calls if c and c[0] == "new-window"]
    assert newwin, "no new-window call"
    flat = list(newwin[0])
    assert "-e" in flat
    assert any(a.startswith("CLAUDE_CONFIG_DIR=") and a.endswith("/.claude-b") for a in flat)


def test_window_new_default_account_sends_no_env(client, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "periscope.routes.sessions._tmux_mutate",
        lambda *a: (calls.append(a), (True, "@9"))[1],
    )
    monkeypatch.setattr("periscope.routes.sessions.tmux", lambda *a: "9")
    monkeypatch.setattr("periscope.routes.sessions._send_and_stamp", lambda *a: None)

    client.post("/api/window/new?session=/repo&mode=claude")

    newwin = [c for c in calls if c and c[0] == "new-window"]
    assert newwin
    assert "-e" not in list(newwin[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routes/test_sessions.py -k account -v`
Expected: FAIL — `-e` absent (the route ignores the `account` param).

- [ ] **Step 3: Write the implementation**

In `periscope/routes/sessions.py`, change the `_window_new_plain` signature (line 182) to accept the account:

```python
def _window_new_plain(
    track_id: str, exec_cmd: str, mode: str,
    cwd_param: str | None = None, branch: str | None = None,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
) -> dict:
```

Resolve it once near the top of the function body, right after `repo` is computed:

```python
    config_dir = store.account_config_dir(account)
```

Add the env args to the `new-window` call (line 236-239):

```python
        ok, msg = _tmux_mutate(
            "new-window", "-t", f"={MANAGED_SESSION}:", "-c", cwd,
            *tmux_mod.env_args(config_dir),
            "-P", "-F", "#{window_id}",
        )
```

Apply the same `*tmux_mod.env_args(config_dir)` to the `new-session` branch immediately above it (the fallback that creates `MANAGED_SESSION` when absent).

Add `account` to the route (line 364):

```python
@router.post("/api/window/new")
def window_new(
    session: str,
    exec_cmd: str = Query("", alias="exec"),
    mode: str = "shell",
    resume_id: str | None = None,
    cwd: str | None = None,
    branch: str | None = None,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
):
```

and thread it into the `_window_new_plain(...)` call in that handler.

Imports: `from periscope import store` and `from periscope import tmux as tmux_mod` (the module already imports the `tmux()` *function*, so alias the module to avoid shadowing).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/routes/test_sessions.py -k account -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Verify no regression**

Run: `uv run pytest tests/routes/test_sessions.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "feat(sessions): account param on /api/window/new sets CLAUDE_CONFIG_DIR on the new window"
```

---

### Task 4: `_window_new_resume` stops discarding its `exec_cmd`

This is a pre-existing bug (spec, launch-site table): the create-session branch rebuilds the command from `CLAUDE_EXEC`, silently overriding the fully-formed command `channels.resume_session` passes in. Left unfixed, Task 5's account-aware command is ignored on exactly the path that runs after a tmux restart.

**Files:**
- Modify: `periscope/routes/sessions.py:95` (`_window_new_resume`)
- Test: `tests/routes/test_sessions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_window_new_resume_honours_caller_exec_cmd(monkeypatch):
    """The create-session branch must not rebuild the command from CLAUDE_EXEC."""
    sent: list[str] = []
    monkeypatch.setattr("periscope.routes.sessions._tmux_mutate", lambda *a: (True, "0"))
    monkeypatch.setattr("periscope.routes.sessions.tmux", lambda *a: "0")
    monkeypatch.setattr(
        "periscope.routes.sessions._send_and_stamp", lambda target, cmd: sent.append(cmd)
    )
    monkeypatch.setattr(
        "periscope.routes.sessions._resume_cwd", lambda _id: "/tmp", raising=False
    )

    sessions._window_new_resume("resumes", "CUSTOM_PREFIX claude --resume abc", "abc", "resume")

    assert sent, "nothing sent"
    assert sent[0].startswith("CUSTOM_PREFIX "), f"caller command was discarded: {sent[0]!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routes/test_sessions.py -k resume_honours -v`
Expected: FAIL — the sent command starts with `claude`, not `CUSTOM_PREFIX`.

- [ ] **Step 3: Write the implementation**

In `_window_new_resume`, on the branch that creates the sentinel session, replace the locally-rebuilt `f"{CLAUDE_EXEC} --resume {resume_id}"` with the `exec_cmd` argument the caller supplied, falling back to the rebuild only when it is empty:

```python
    cmd = exec_cmd.strip() or shlex.join(
        config.build_agent_command("claude", cwd=cwd, resume_id=resume_id)
    )
    _send_and_stamp(target, cmd)
```

Apply the identical expression on **both** branches (create-session and existing-session) so they cannot diverge again.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/routes/test_sessions.py -k resume -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "fix(sessions): _window_new_resume honours the caller's exec_cmd on the create-session branch"
```

---

### Task 5: MCP spawn/resume tools accept an account

**Files:**
- Modify: `periscope/channels.py` (`_do_spawn_claude_tool`, `_do_resume_session_tool`)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_spawn_claude_sets_account_env(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("periscope.channels.tmux", lambda *a: calls.append(a) or "1")

    await channels._do_spawn_claude_tool(
        {"prompt": "hi", "cwd": "/tmp", "account": "b"}, pane="%1"
    )

    win = [c for c in calls if c and c[0] in ("new-window", "new-session")]
    assert win, "no window created"
    assert any(a.startswith("CLAUDE_CONFIG_DIR=") for a in win[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py -k account -v`
Expected: FAIL — no `CLAUDE_CONFIG_DIR` in the tmux args.

- [ ] **Step 3: Write the implementation**

In `_do_spawn_claude_tool`, read the account off the tool arguments and add the env args to both the `new-session` and `new-window` calls:

```python
    config_dir = store.account_config_dir(args.get("account"))
```

```python
    tmux("new-window", "-t", f"{session}:", "-c", cwd,
         *tmux_mod.env_args(config_dir), "-P", "-F", "#{window_index}")
```

Declare `account` in the tool's input schema alongside `workspace`/`cwd`:

```python
"account": {
    "type": "string",
    "enum": ["default", "b"],
    "description": "Which Claude subscription to run the spawned pane on. "
                   "Omit for the default account.",
},
```

For `_do_resume_session_tool`, resolve the same way and pass a command string built with the prefix, since it delegates to `_window_new_resume` (which sends a string, not tmux args):

```python
    prefix = f"CLAUDE_CONFIG_DIR={config_dir} " if config_dir else ""
    result = _window_new_resume(
        tmux_session, f"{prefix}{CLAUDE_EXEC} --resume {session_id}",
        session_id, "resume",
    )
```

This is the one place a string prefix is still correct — `_window_new_resume` owns window creation internally. Task 4 is what makes this prefix survive.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_channels.py -k account -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): account argument on spawn_claude and resume_session"
```

---

### Task 6: Hook installers fan out over every config dir

Without this, a B pane writes no `pane_sessions` row and the transcript view, narrator, per-pane burn and the resurrect `--resume` rewrite all go dark for it (spec, "settings.json is the hook producer").

**Files:**
- Modify: `bin/periscope` (`install-claude-hook` at :100, `uninstall-claude-hook` at :135)
- Test: manual — this is a shell installer, exercised by running it

- [ ] **Step 1: Change the installer to loop over config dirs**

In `bin/periscope`, replace the single hardcoded path with a loop. The Python heredoc already takes `$REPO` as `argv[1]`; pass the settings paths as additional argv:

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

Apply the same argv-loop shape to `uninstall-claude-hook` at :135 and to the `SessionEnd` / history-hook registration if it is a separate block.

- [ ] **Step 2: Verify idempotency and coverage**

```bash
bin/periscope install-claude-hook
bin/periscope install-claude-hook   # second run must print nothing (idempotent)
python3 -c "
import json,os
for p in ('~/.claude/settings.json','~/.claude-b/settings.json'):
    d=json.load(open(os.path.expanduser(p)))
    h=d.get('hooks',{})
    for ev in ('SessionStart','UserPromptSubmit'):
        got=[c['command'] for g in h.get(ev,[]) for c in g.get('hooks',[])
             if 'pane_session_hook' in c.get('command','')]
        print(p, ev, len(got))
"
```
Expected: every line ends in `1` — registered exactly once in each config dir, no duplicates from the second run.

- [ ] **Step 3: Commit**

```bash
git add bin/periscope
git commit -m "fix(hooks): register pane-session hooks in every account config dir, not just ~/.claude"
```

---

### Task 7 (Phase 2, gated): Identify account B's credential item

Everything in Phase 2 depends on reading account B's OAuth token. The spec's open question — where that credential physically lives — is unresolved, and until it is, `usage.py:148`'s unfiltered `-w` lookup may return **either** account's token.

**This is an investigation task, not a code task. Do not write Task 8 or 9 until it produces an answer.**

- [ ] **Step 1: Locate the item without a keychain sweep**

`security dump-keychain` prompts once per item (~40 prompts observed) — do not use it. Probe candidate attributes instead; metadata lookups without `-w` do not prompt:

```sh
security find-generic-password -s "Claude Code-credentials" -a "$HOME/.claude-b"
security find-generic-password -s "Claude Code-credentials-$HOME/.claude-b"
security find-generic-password -s "Claude Code-credentials" -a "$(python3 -c "
import json,os;print(json.load(open(os.path.expanduser('~/.claude-b/.claude.json')))['oauthAccount']['accountUuid'])")"
```

- [ ] **Step 2: Record the finding**

Update `docs/specs/multi-account-pooling.md` "Open questions" with the answer (or with "still unresolved" plus what was ruled out). If unresolved, **stop here** — Tasks 8 and 9 cannot be specified correctly, and shipping a pill that may show the wrong account's meters is worse than showing one account's.

---

### Task 8 (Phase 2, blocked by Task 7): `account` column on `usage_samples`

Must land **before** a second account is ever metered, or the two accounts interleave into one series with no retroactive way to separate them.

**Files:**
- Modify: `periscope/activity.py:77` (schema), the guarded-ALTER block at `:124-130`
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing test**

```python
def test_usage_samples_has_account_column(fresh_activity_db):
    cols = {r[1] for r in activity._CONN.execute("PRAGMA table_info(usage_samples)")}
    assert "account" in cols


def test_usage_samples_separates_accounts(fresh_activity_db):
    activity.record_usage_samples({"session": 10.0}, account="default")
    activity.record_usage_samples({"session": 20.0}, account="b")
    rows = activity.usage_samples_since(0, account="b")
    assert [r["percent"] for r in rows] == [20.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_activity.py -k usage_samples -v`
Expected: FAIL — no `account` column.

- [ ] **Step 3: Write the implementation**

Add the column via the existing guarded-ALTER idiom (`activity.py:124-130`), defaulting existing rows to the default account so historical data stays attributed:

```python
have = {r[1] for r in c.execute("PRAGMA table_info(usage_samples)")}
if "account" not in have:
    c.execute("ALTER TABLE usage_samples ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")
```

The primary key `(meter, at)` must become `(account, meter, at)`. SQLite cannot alter a PK in place, so this needs a table rebuild inside the same guarded block: create `usage_samples_new` with the wider PK, `INSERT INTO … SELECT …, 'default'`, drop, rename.

Thread `account` through `record_usage_samples()` and `usage_samples_since()` with a `"default"` default so existing call sites are unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_activity.py -k usage_samples -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "feat(activity): account dimension on usage_samples so two subscriptions do not interleave"
```

---

### Task 9 (Phase 2, blocked by Task 7): Per-account meters in the pill

**Files:**
- Modify: `periscope/usage.py:148` (`_read_oauth_token`), `:391` (`cached_plan_usage`), `periscope/routes/state.py:157`, `static/src/chrome/UsagePill.jsx`
- Test: `tests/test_usage.py`, `static/src/chrome/__tests__/UsagePill.test.js`

- [ ] **Step 1: Write the failing test**

```python
def test_read_oauth_token_is_account_scoped(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "periscope.usage.subprocess.run",
        lambda argv, **kw: seen.append(argv) or _fake_ok(),
    )
    usage._read_oauth_token(account="b")
    assert any("b" in " ".join(argv) for argv in seen), (
        "token lookup was not scoped to the account"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_usage.py -k account_scoped -v`
Expected: FAIL — `_read_oauth_token()` takes no `account` argument.

- [ ] **Step 3: Write the implementation**

Give `_read_oauth_token(account="default")` the account-scoped lookup discovered in Task 7. Key `_plan_cache` by account rather than as a single module global, and have `cached_plan_usage()` return `{account_id: {meters, fetched_at}}`. `routes/state.py:157` returns that mapping as `usage_plan`.

`UsagePill.jsx` renders one meter group per account, labelled from the registry, preferring the account with the most headroom in the collapsed view.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_usage.py -q && npm test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/usage.py periscope/routes/state.py static/src/chrome/UsagePill.jsx static/dist/app.js tests/test_usage.py
git commit -m "feat(usage): per-account plan meters in the pill"
```

---

## Final verification

- [ ] `uv run pytest -q` — expect all green (953 before this plan; each task adds tests)
- [ ] `bin/check` — expect `✓ all checks passed`
- [ ] `npm run build && git add static/dist/app.js` if `static/src/` changed
- [ ] Open a `+ New tab` on account B, confirm the pane authenticates and its transcript appears in the detail view (proves the Task 6 hook fan-out works)
- [ ] Trigger a save and confirm the B pane's line carries the prefix:
  `~/.tmux/plugins/tmux-resurrect/scripts/save.sh && awk -F'\t' '$11 ~ /CLAUDE_CONFIG_DIR/' $(ls -t ~/.local/share/tmux/resurrect/*.txt | head -1)`
