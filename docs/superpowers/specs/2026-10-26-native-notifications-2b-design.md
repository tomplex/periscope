# Native Mac notifications 2b, the actions: design (THI-422)

**Builds on:**
- `2026-10-21-native-notifications-design.md`. Its 2a shipped all six banner kinds, the dedupe, the grouping, the Settings toggles, the dock badge and click routing.
- That spec's 2b sections were the starting point. **This spec supersedes them wherever the two disagree.**

**Read before touching the area:**
- `docs/invariants.md`: pane identity and `routes/send.py`;
- `docs/channels.md`: the pane→session mapping;
- `docs/tauri-shell.md`: version skew and the webview recycle.

## Shipping: two PRs

| PR | What it adds | Agent acts? |
|---|---|---|
| **1. Shell + plain buttons** | <ul><li>The Rust category and action plumbing.</li><li>The JS dispatcher.</li><li>Mark seen, Open PR, Clean up worktree, Spawn on {B} and Details (Review diff waits on THI-433).</li></ul> | No |
| **2. Agent-acting buttons** | <ul><li>The `PermissionRequest` hook and `pending_permission`.</li><li>`/api/pane/permission` and `/api/pane/reply`.</li><li>Allow, Deny, Open, Send and Ask agent to fix.</li></ul> | Yes |

- **The shell is generic.** It knows categories and actions only as data the webview sends, so PR 1's single `.app` rebuild covers both PRs.
- **PR 2 is JS and server only.**
- **PR 2's endpoints are THI-426's plumbing as well.** That ticket puts Allow / Deny / Reply on the rail's needs-input card, so the endpoints take nothing banner-specific.

## Facts checked against Claude Code 2.1.280

These come from the installed CLI bundle, because the hooks docs don't publish the input payloads.

- **The `PermissionRequest` input carries:**
  - the common fields: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `agent_id`, `agent_type`, `prompt_id`;
  - `agent_id` is set for a subagent's call, and the dialog still shows in the parent's pane;
  - plus `hook_event_name`, `tool_name`, `tool_input`, `permission_suggestions` and, for MCP tools, `mcp_server`.
- **It has no `tool_use_id`.** PreToolUse, PostToolUse and PermissionDenied do carry one, but running a hook process on every tool call just to pair them isn't worth it.
- **A silent hook doesn't change the dialog.** If the hook prints nothing and exits 0, the dialog shows unchanged.
- **The hook runs before the dialog renders.** So its row exists by the time the pane reads as waiting.
- **The default timeout is 600 s,** so the hook must return at once.
- **Some Bash dialogs add a row under the description.** `python3 -c …` gets `This command requires approval`, one column in (shallower than the command), between the description and the question. A real capture is `tests/fixtures/permission_dialogs/bash_requires_approval.txt`.
- **The dialog's keys** (`1` = Yes, `Escape` = No and tell Claude what to do differently) aren't documented. PR 2 pins them with a fixture captured from a real dialog.

## PR 1: the shell and the plain buttons

### Rust, `src-tauri/src/notifications.rs`

- **A new event, `periscope:notify-categories`.** It's sent webview → Rust with the full set:

  ```
  [{ id, actions: [{ id, title, auth?, foreground?, destructive?, reply?: { button, placeholder } }] }]
  ```

  - Rust maps it onto `UNNotificationCategory` / `UNNotificationAction` / `UNTextInputNotificationAction`, then calls `setNotificationCategories`.
  - Every field is `#[serde(default)]` and tolerates `null`, as `NotifyRequest` does.
  - A pure `category_specs(&str)` parses and validates the set: an action needs an `id` and a `title`; an action that fails is dropped, not the whole category. It's cargo-tested.
- **When the set is registered.** The webview sends it when the feed starts and again whenever it changes. It's never sent from `send()`, because registration is asynchronous and replaces the whole set, so a banner posted straight after it can arrive without buttons.
- **`NotifyRequest` gains `category`.** No fallback to `kind`: 2b's category ids reuse 2a's kind names (`done`, `usage`, `pr_merged`, …), and the registered set outlives the webview, so a kind-only fallback would let an old webview's plain `kind:"done"` banners match a stale category after a JS rollback and grow buttons whose presses nothing listens for. A banner only gets `categoryIdentifier` when it names `category` explicitly, and only when it also has a `kind` — a plain (kindless) banner never gets one.
- **`did_receive` routes by action identifier:**

  | Action | What it emits |
  |---|---|
  | Default (a click) | `periscope:notification-opened`, as today |
  | Dismiss | nothing |
  | Anything else | `periscope:notification-action` with `{ target, kind, action, text, data }` |

  - `text` is `userText` when the response is a `UNTextInputNotificationResponse`, else `""`.
  - Plain (kindless) banners have no category, so they can't reach the third row.
- **Action options:**

  | Option | Set when the spec has | Effect |
  |---|---|---|
  | `AuthenticationRequired` | `auth` | Touch ID or the password before the action runs from a locked screen |
  | `Foreground` | `foreground` | brings Periscope forward |
  | `Destructive` | `destructive` | macOS draws the button red |

- **Version skew.**

  | Combination | Result |
  |---|---|
  | Old shell, new JS | Nothing listens for the categories event, and `category` is an unknown field, so banners arrive without buttons, as in 2a. |
  | New shell, old JS | Old JS's banners never name a category, so even a set a newer webview left registered shows no buttons. |

  `docs/tauri-shell.md` gains a line for this.
- **Delivery when the webview is down.** An action that arrives while the webview is being rebuilt (a recycle) is lost, as a click is today. Every action is safe to lose: the banner stays in Notification Center, and the pane still shows what's waiting.

### JS

- **`tauri.js`** gains:
  - `setNotifyCategories(set)`, which emits `periscope:notify-categories`;
  - `onNotificationAction(cb)`, which listens on `periscope:notification-action`.
- **`notify/actions.js`, new:**
  - **`categories({ accounts, moveTargets })`.** Pure. It returns the full category set (table below).
  - **`runAction({ target, kind, action, text, data })`.** It dispatches a button. It's exported so a test can drive it with injected openers.
- **`copy.js`'s `payloadFor`** sets `category` on each banner, choosing the variant whose buttons apply:

  | Banner | Category | Buttons |
  |---|---|---|
  | `done` | `done_seen` | Mark seen (Review diff is deferred to THI-433: every pane shares one tmux session, so a session-keyed review row would open the wrong pane's) |
  | `ci_failed`, a URL | `ci_failed_pr` | Open PR (PR 2 adds the `ci_failed` category, with **Ask agent to fix**) |
  | `ci_failed`, no URL | none | the click still opens the pane |
  | `pr_merged`, a `cwd` and a URL | `pr_merged` | **Clean up worktree** · Open PR |
  | `pr_merged`, a `cwd`, no URL | `pr_merged_cleanup` | **Clean up worktree** |
  | `pr_merged`, a URL, no `cwd` | `pr_merged_pr` | Open PR |
  | `pr_merged`, neither | none | the click still opens the pane |
  | `usage`, `moveTargets[A]` is another account B | `usage_to_{B}` | **Spawn on {B label}** · Details |
  | `usage`, no B | `usage` | Details |
  | `needs_input`, `question` | none in PR 1 | the click still opens the pane |

  - **{B}.** B is `move_targets[A]`, the server's `choose_move` pick for escaping A (`docs/account-routing.md`). It is not `launch_default`, which can be A itself while A runs hot.
  - **One category per B.** `categories()` registers a `usage_to_{id}` for every account that appears as a move target. The set changes, and is re-sent, when `accounts` or `moveTargets` change.
- **`feed.js` wiring:**
  - **At start:** it registers `onNotificationAction(runAction)` beside `onNotificationOpened`.
  - **Each pass:** it compares the category set against the last one sent and calls `setNotifyCategories` only on a change. Comparing JSON is enough, because the set is a few hundred bytes.
  - **Inside `runPass`'s try,** so a bad set can't throw into the transport.

### What each PR 1 button does

| action | Foreground | Does |
|---|---|---|
| `review` | yes | Deferred to THI-433: no category offers this button yet, since every pane shares one tmux session (`config.MANAGED_SESSION`) and a session-keyed review row would open the wrong pane's. `selectReview(session)` and `runAction`'s `review` case stay, ready for the follow-up: `selectReview(session)` (the live pane's, else the banner's `data.session`) sets `railSelection` to `review:{session}` and saves it as `lastSelected`, as Rail's `selectKey` does. Detail shows the live LGTM review, or "Start review →" when there's none. |
| `mark_seen` | no | `markReadySeen(w)`: adds the pid to `dismissedReadyPids`, without selecting. Split out of `openReadyPane`, which keeps calling it. Client-side only, like a rail dismissal: after a webview reload or recycle the READY row is back (the done banner itself never repeats — its dedupe key is kept). A server-side ack is a possible follow-up. |
| `open_pr` | no | `openExternal(data.url)`: the browser comes forward on its own. |
| `cleanup` | yes | `openCleanupModal({ paths: [data.cwd || w.cwd] })`: the banner names the worktree whose PR merged, and the pane may have moved since. Never deletes: the modal's own Archive button does, after you look. |
| `spawn_on` | no | `pickSpawnAccount(B)`: the header picker's `pick`, exported. It pins every new spawn to B until set back to auto; the header pin shows it. |
| `details` | yes | `openUsageDetails()` |

- **A missing pane.** A fallback or a failed opener selects the live pane found by `data.pid`, and reveals `target` only when that pane is gone, as a click does. Mark seen does nothing, since a gone pane has no READY row left to dismiss. `open_pr`, `spawn_on` and `details` don't need the pane.
- **`data` gains** `session` (for `review`) and `cwd` (for `cleanup`) on the banners that need them. `data.pid` is still the lookup key. The banner's own data is the fallback, since tmux renumbers windows.

### Server: `?paths=` on `/api/cleanup/candidates`

- **`?paths=a&paths=b`** (a repeated query param) has `compute_candidates` evaluate only the worktrees containing those paths, inside its usual walk of the known repos. A pane's `cwd` can be a subdirectory, so each path is resolved to its worktree root (`gitutil.worktree_root`: `git rev-parse --show-toplevel`, then realpath), and a repo holding none of them is skipped before its default branch is looked up. The modal stays fast this way, rather than taking minutes.
- **A listed path that isn't a candidate** (still busy, or already archived) is simply absent.
- **`CleanupModal`:**
  - `openCleanupModal({ paths })` fetches with `paths` and preselects what comes back;
  - **with nothing back,** it says "Nothing to clean up here. Cleanup only offers a worktree (never a main checkout) whose PR is merged or closed, whose remote branch is gone, or that sits idle.";
  - **without `paths`,** it behaves as today.

## PR 2: the buttons that make an agent act

### Revised after deploy: one button per banner

This supersedes every button list in this spec, in PR 1's sections and PR 2's alike.

**Why.** macOS, from Big Sur on, folds a notification's actions into an **Options** menu as soon as there are two or more. The first hand check found Allow hidden there. With a single action, the button shows directly.

**So each category carries exactly one action** (`categories()` in `notify/actions.js`):

| Category | Its one button |
|---|---|
| `needs_permission` | Allow |
| `question` | Reply |
| `ci_failed` and `ci_failed_fix` | Ask agent to fix |
| `pr_merged` and `pr_merged_cleanup` | Clean up worktree |
| `pr_merged_pr` and `ci_failed_pr` | Open PR |
| `usage_to_{B}` | Spawn on {B} |
| `usage` | Details |
| `done_seen` | Mark seen |
| `needs_dialog` | Open |

**What moved to the click.** The banner's click (`openFromBanner`) already opens the pane, the PR or the usage details, so the dropped Open / Open PR / Details buttons were duplicates.

**Deny has no button now.** Clicking the banner opens the pane, where the dialog's own No is one key away. `runAction` still handles `deny` and `open`, so a banner posted before the change still works.

A test pins one action per category.

### Revised in implementation (PR 2)

What shipped, checked against real Claude Code 2.1.280 captures (`tests/fixtures/permission_dialogs/`, `tests/fixtures/composer/`). **This supersedes the PR 2 sections below wherever they differ.** The plan's per-task notes say where the code moved from its blocks: `docs/superpowers/plans/2026-10-26-native-notifications-2b-pr2.md`.

**Matching: an exact match, or no Allow.** The planned screen check (the summary's first 40 characters, or a file's basename, found anywhere in the dialog) vouched for the wrong dialog in review:
- two `cd …/periscope && …` commands share their first 40 characters, and every `python3 - <<'EOF'` heredoc its first line;
- `git push origin main` was found inside a dialog for `… --force`;
- a Write row for `notes.md` matched a Bash dialog for `rm notes.md`;
- an AskUserQuestion asking "Do you want to …?" passed as a permission dialog.

So:
- **The hook stores a `match_key`** beside the summary, which is now display only:
  - **Bash:** `sha256:` + the hex digest of the squashed command + the squashed description. Squashed means all whitespace and the hidden controls removed, since a long command wraps mid-word on screen. A digest, so the WHOLE command never sits in the key: a command can carry a token, and `periscope.db` is 0644. The summary is display text by design, and keeps the command's head and tail in clear text (it's on `/api/state`).
  - **Edit / Write:** the absolute `file_path`, in the clear, since the suffix check needs it.
  - **Anything else** (NotebookEdit, WebFetch, MCP tools, …): no key. Their dialogs aren't verified, so their banners offer Open only.
  - **A Bash call with `dangerouslyDisableSandbox` set:** no key. The flag isn't keyed and the dialog may not show it, so an unsandboxed retry would otherwise match a sandboxed row.
- **`panes.parse_permission_dialog`** reads the capture bottom up, with CSI / OSC escapes stripped first (`capture` keeps them). Each step fails closed:
  1. the footer is the **last** non-empty line, matching `Esc to cancel` but not AskUserQuestion's `Enter to select · … · Esc to cancel`;
  2. the nearest `Do you want to …?` above it has `1. Yes` right under it, wherever the cursor is;
  3. the full-width rule sits above that, within 60 rows, with no scrollback marker (`⏺`, `✻`, `⎿`) in between;
  4. the header is the first row under the rule.
- **`panes.dialog_matches(parsed, tool, match_key)`** replaces the planned `dialog_shows`, and is header-bound:
  - **Bash:** the header is `Bash command`, and the digest of the body **equals** the key. The body is taken after a leading `Tip:` and its wrap, and before a trailing `This command requires approval` row (`_TRAILING_CHROME`), with `│` gutters dropped, then squashed. Any other row shallower than the command, or that text anywhere but the block's end, stays in the body and fails closed. Never a prefix or a substring.
  - **Write** (`Create file` / `Overwrite file`) **and Edit** (`Edit file`): the path in the question (`Do you want to create / overwrite / make this edit to <path>?`) is a `/`-boundary suffix of the stored absolute path, so `notes.md` can't vouch for `old-notes.md`. The body (a preview, a diff) is never read: it can mention any file.
  - **Only `VERIFIABLE_TOOLS = ("Bash", "Edit", "Write")` can match.** The hook runs on bare `python3` and can't import `periscope`, so it and `panes.py` each define that tuple and the hidden-control class (`\u` escapes, never the raw characters), and tests assert the copies agree.
- **Accepted limits:**
  - Squashing loses whitespace-only differences and the command/description boundary (`rm -rf / tmp/x` squashes as `rm -rf /tmp/x`). A wrapped screen can't recover either; the single-row rule and the request id narrow it.
  - A file tool matches by file, not by change, so two pending edits to one file are interchangeable.
  - A dialog more than 60 rows tall, or a question wrapped in a very narrow pane, doesn't parse: a 409 that reveals the pane.

**The hook, `pane_permission_hook.py`:**
- **It prints nothing and exits 0 on every path.** A `PermissionRequest` hook's JSON stdout is read as its answer to the dialog.
- **py3.9-safe.** It's registered as `python3 -I -S <path> || true`, and `python3` can be the Mac's `/usr/bin/python3` (3.9). `-I -S` keeps a site or venv it doesn't own out; `|| true` turns even a missing file into exit 0.
- **A 0.5 s busy budget.** SQLite's busy wait ran ~3× past a 2 s budget under contention, and Claude kills the hook at its 5 s timeout. A missed row only makes the banner Open-only.
- **A self-healing `match_key` column.** A table created before it gets an `ALTER`, since the INSERT would otherwise fail, silently, forever. The server's `activity._conn` runs the same `ALTER`, so whichever opens the DB first leaves the shape the other expects.
- **Bash's summary is the whole command**, whitespace collapsed, hidden controls stripped, clipped to 80. The first line alone showed `cd /tmp` for `cd /tmp && rm -rf build`.

**`bin/periscope install-hook`:**
- **joins only a matcher-less group** (`matcher` absent, `""` or `"*"`), so a user's matcher-scoped group can't narrow ours;
- **repoints an existing entry only when its target is gone, or when it points at this same checkout** in an older form. Never another live checkout's: an install from a worktree would otherwise point prod's hook into the worktree, and removing the worktree would leave a dangling path behind;
- **fails open:** both observers (`pane_session_hook.py` and this one) end in `|| true`, because a nonzero `UserPromptSubmit` blocks the prompt.

**Publishing, `window_view.pending_permission_row` / `pending_permission_for`:**
- **Exactly one row stamped no earlier than 5 s before `needs_since`** (`created_at >= needs_since - 5`, no upper bound). `needs_since` is when periscope first saw the wait, and the hook's stamp can precede it by:
  - the session-file write;
  - session_status's 1 s index cache;
  - the build in flight;
  - `state_hub`'s 1 s sleep between builds.

  Too narrow drops the row and the buttons with it; too wide only lets back-to-back prompts read as two rows, which means Open only.
- **Keyed rows only.** The one row needs a `match_key`. Unkeyed rows still count toward "exactly one", since the dialog on screen may be theirs.
- **The key never leaves the server.** `pending_permission_row` returns the full row for the route; `pending_permission_for` publishes `{request_id, tool, summary}`.
- **Misses are logged at info.** A wait with no row in the window but one in the 10 s before it logs once per wait, to calibrate `_PERMISSION_WINDOW_S`. A DB error makes banners Open-only, with a warning at most once a minute.

**The endpoints, `routes/pane_actions.py`:**
- **JSON only.** 415 for anything but `application/json`. FastAPI's `strict_content_type` already refuses a typeless body, but the dependency is unpinned and older releases parsed one.
- **Addressed by `%id`.** The pane is found by pid (`pids.window_by_pid`), then captured and sent to by its pane id: tmux re-resolves `session:index` when the keys go in, so a renumber or a split window's focus moving in between would land them on another pane. `_send_to_target` gains `address=` for this; focus and acted-at stay keyed by the target. 404 for no live pane or no pane id.
- **Each request answered once, under a lock.** The checks and the send hold `_ACT_LOCK`, and the request id is marked answered before its key goes in. Two presses of one banner (the banner and the rail card, a double click) could otherwise both pass before Claude redraws, landing the second `1` in the composer or on the next dialog. A send that fails after the mark isn't retried from a banner; it's logged, and the pane is answered by hand.
- **`/api/pane/permission`: the row, the request id and the screen.** 409 unless:
  - the request wasn't answered from here already;
  - the pane waits on a permission prompt;
  - `pending_permission_row`, recomputed (the tool and key never come from the caller), has this `request_id`;
  - `dialog_matches` holds on a fresh capture.
- **`/api/pane/reply` refuses** with a 409 when:
  - the live session is `needs-input`, with or without a `waiting_for`;
  - `panes.dialog_open` sees a dialog on a fresh capture. It fails closed: an `Esc to cancel` in the last three lines counts;
  - the pane isn't a Claude: a shell is in the foreground (a ctrl+z'd Claude keeps a live session file under zsh's prompt), or there's neither a live session nor Claude on screen. A Codex pane gets the 409, since its approval prompt can't be ruled out;
  - `panes.composer_ready` fails. It passes only a plain, idle `❯` composer: one row between the last two full-width rules, empty or holding only Claude's dim/grey suggestion. Shell mode's `!` would run the text; a draft would be submitted with it; a near-black text colour is a draft, not a suggestion (the ghost greys now have a floor: RGB 64, 256-colour ramp entry 238). It also refuses when part of a dialog is drawn above the composer (a full-width column-0 rule, an `Esc to cancel` row, or a `1. Yes` option under the cursor or under a question): a capture taken mid-redraw must not read as an idle prompt. A plain `Do you want to …?` line alone doesn't refuse, since that's how Claude asks the question Reply answers. After the paste, `composer_holds` applies the same check before Enter, and it was pinned against real captures of a short paste and of a long one Claude collapses to `[Pasted text #N +M lines]`.
- **After the paste, a re-check before Enter.** The paste goes in alone. Enter follows only when a fresh capture (up to 4 looks, 100 ms apart) shows no dialog and `panes.composer_holds` the reply: its squashed first 24 characters, or Claude's `[Pasted text #N …]`. Otherwise it's a 409 and nothing is submitted; the paste may sit in the composer, or in a dialog that took focus. One look before the paste left ~150 ms for a dialog to render and take focus, and Enter on a dialog confirms `1. Yes`. The log line carries the pane and the length, never the text.

**The banners:**
- **Allow only with a published request.** `needs_permission` (Allow · Deny · Open) needs `w.pending_permission.request_id` and a pid. Otherwise the banner is `needs_dialog` (Open), with 2a's body.
- **Reply and Ask agent to fix only on Claude panes** (`w.agent === "claude"` with a pid), since the server refuses everything else.
  - `question` gets no category without one.
  - A `ci_failed` whose window is the worktree's shell borrows another live Claude window on the same PR as its fixer, and `data.pid` is the fixer's.
- **The `ci_failed` variants:**

  | Category | When | Buttons |
  |---|---|---|
  | `ci_failed` | a fixer and a URL | Ask agent to fix · Open PR |
  | `ci_failed_fix` | a fixer, no URL | Ask agent to fix |
  | `ci_failed_pr` | a URL, no fixer | Open PR |
  | none | neither | the click opens the pane |

- **The categories PR 2 adds:** `needs_permission`, `needs_dialog`, `question`, `ci_failed`, `ci_failed_fix`.
- **The Reply placeholder is a fixed "Reply…".** A category's buttons are static, so it can't name the pane. The button reads Reply; its text field's button reads Send.
- **Auth.** Allow, Reply and Ask agent to fix need it. Deny is Escape, which only stops the agent, so it doesn't.
- **A failed background press posts a notice.** These presses don't bring Periscope forward, so a toast or a revealed pane lands in a window nobody is looking at. Every failure therefore also posts a banner of kind `notice`:
  - no category, `quiet_when_focused`;
  - titled like "Couldn't allow", with the route's reason as the body;
  - a click opens the pane by pid.

  A 409 also reveals the pane; any other failure also reveals it and toasts. A banner with no pid, or Allow / Deny with no request id, posts the notice without calling the route.
- **`util.postJson`** returns `{ ok, status, detail }` and never throws. A network failure, or no answer within 10 s, is status 0.
- **An MCP tool's name reads as `tool (server)`** in a banner body. That's display only, and today unused: MCP rows carry no key, so they never publish.

### The hook, `pane_permission_hook.py`

- **Registered** as a `PermissionRequest` hook (no matcher, so every tool) by `bin/periscope install-hook`, beside the existing entries. `uninstall-hook` removes it.
- **Modelled on `pane_session_hook.py`.** It:
  1. reads stdin and `TMUX_PANE`;
  2. opens its own short-lived connection to the prod `periscope.db`;
  3. writes one row.

  It swallows every failure, **prints nothing and exits 0**. Any output would be read as a permission decision; this hook only observes. A pane without `TMUX_PANE` writes nothing.
- **The table:**

  ```sql
  pane_permissions(pane_id TEXT, session_id TEXT, agent_id TEXT, request_id TEXT PRIMARY KEY,
                   tool TEXT, summary TEXT, created_at REAL)
  ```

  Rows older than a day are pruned on insert.
- **`request_id`** is `sha1(session_id, tool_name, json.dumps(tool_input, sort_keys=True), created_at)`, hex, first 16 characters.
- **`summary`** depends on the tool:

  | Tool | Summary |
  |---|---|
  | Bash | the command's first line |
  | Edit / Write / NotebookEdit | the file path |
  | WebFetch | the URL |
  | anything else | the first string value in `tool_input` |

  It's capped at 80 characters, with whitespace collapsed.

### Publishing, `window_view.py`

`w.pending_permission = { request_id, tool, summary }` when all of these hold:

1. the pane is in `needs-input` with `waitKind(waiting_for) == "permission"`;
2. **exactly one** `pane_permissions` row for the pane's tmux pane id has `created_at >= needs_since - 2`. The 2 s covers clock skew: the hook stamps before the dialog renders, and `needs_since` is when periscope saw it.

Otherwise it's `null`.

- **Two or more rows means we can't tell which request the dialog shows:**
  - parallel tool calls, each asking;
  - back-to-back prompts inside one poll, so `needs_since` never moved.

  Then there's no `pending_permission`, and the banner is the dialog variant with Open only. That's conservative: the cost is a button, never a wrong approval.
- **The row is read on each state build,** with one indexed query per waiting pane (rarely more than a few).

### The banner

- **With a `pending_permission`,** `needs_input` reads:
  - title: `{pane} needs you`;
  - subtitle: `{session} · {branch}`;
  - body: `Allow {tool}: {summary}?`;
  - category: `needs_permission`, with **Allow** (auth) · Deny (destructive) · Open (foreground).
- **Without one,** the body is 2a's ("Waiting for your approval / answer / input.") and the category is `needs_dialog`, with Open (foreground).
- **The dedupe key stays `needs:{pid}@{needs_since}`.** A pane already announced isn't re-announced when this deploys, or when its row appears a poll late. `data.request_id` carries the request.
- **`question`** gets the category `question`, with **Send** (a text-input action: auth, button "Send", placeholder "Reply to {pane}…") · Open.
- **`ci_failed`.** With a live pane (`data.pid`), its category becomes `ci_failed`: **Ask agent to fix** (auth) · Open PR. Without one it stays `ci_failed_pr`.

### The endpoints, `routes/pane_actions.py`

Both take a **JSON body**: a body-less POST with query params is a CORS simple request that any website could fire at `localhost:17374`, whereas a JSON body needs a preflight the server never answers. Both find the pane by `pid`, never by a target that tmux may have renumbered. They use `channels._resolve_window_by_pid`, which moves to `pids.py` as `window_by_pid` so a route doesn't import from the MCP module; `channels.py` keeps calling it.

**`POST /api/pane/permission`** `{ pid, request_id, choice: "allow" | "deny" }`

1. **400** for any other `choice`.
2. **404** for an unknown pid.
3. **409** unless all of these hold:
   - the pane is in `needs-input`, waiting on a permission;
   - its `pending_permission.request_id == request_id`;
   - a fresh `capture-pane` shows a permission dialog **whose text contains the request's summary**. That's checked by a new `panes.permission_dialog(text)`: the `_detect_needs_input` footer plus the dialog's question line, returning the dialog's text or `None`. A long summary is matched on its first 40 characters, after collapsing whitespace on both sides.

   The screen check is what stops a stale row from approving a different dialog that happens to be up.
4. Then `_send_to_target(target, None, ["1"])` for allow, `["Escape"]` for deny.
5. Returns `{ ok: true }`.

- **Deny's `Escape`** is "No, and tell Claude what to do differently", so the pane is then waiting for you.
- **A pinned fixture in `tests/test_panes.py`**, captured from a real dialog during the plan, covers both keys: `permission_dialog` recognises it, and the option the key selects is the one asserted.

**`POST /api/pane/reply`** `{ pid, text }`

1. **400** for empty `text`.
2. **404** for an unknown pid.
3. **409** if any dialog is open, by either signal:
   - the pane is in `needs-input` with a `waiting_for`;
   - a fresh capture shows a dialog footer (`panes._detect_needs_input`, the "Enter to select · Esc to cancel" family, which covers AskUserQuestion and permission dialogs alike).

   Pasting `1 sounds good` + Enter into a numbered dialog would pick an option.
4. Otherwise `_send_to_target(target, text, ["Enter"])`, a bracketed paste then Enter, as the composer does.
5. Returns `{ ok: true }`.

**Neither endpoint** is a general "send keys" route: `/api/send` already is one. These carry the guards a remote button needs.

### What each PR 2 button does

| action | auth | Does |
|---|---|---|
| `allow` / `deny` | Allow only | `POST /api/pane/permission` with `data.pid`, `data.request_id` |
| `send` | yes | `POST /api/pane/reply` with the typed `text`; an empty reply does nothing |
| `fix` | yes | `POST /api/pane/reply` with `CI failed on #{pr}: {failing checks}. Please investigate and fix.`, where the checks are the `✗` names from `data.checks`, which `payloadFor` adds |
| `open` | no (foreground) | reveals the pane |

- **409:** reveal the pane, so you see what changed.
- **Any other failure:** reveal the pane and toast the error (`apiCall` toasts already).
- **Success:** nothing more. The pane's next state shows it working.

## Errors

- **The shell.** A bad categories payload is logged to `shell.log` and dropped. A bad action payload can't happen: Rust builds it.
- **JS.** `runAction` catches everything. A throw logs with `console.error("[notify] action failed", …)` and reveals the pane when there is one.
- **The hook** swallows everything and always exits 0, so a broken hook can never block a prompt.

## Tests

**vitest:**
- `actions.test.js`:
  - `categories()` for one, two and three accounts;
  - each category's buttons and options;
  - `runAction` calls the right opener or endpoint per action;
  - a 409 reveals the pane, and a missing pane falls back to `revealPane`;
  - `send` with empty text does nothing.
- `copy.test.js`:
  - the category per banner and variant;
  - `needs_permission`'s body;
  - `data` carries `request_id` / `session` / `cwd` / `checks`.
- `feed.test.js`: the category set is re-sent only on a change.

**pytest:**
- `tests/test_pane_permission_hook.py`:
  - the summary per tool;
  - the `request_id` shape;
  - the hook prints nothing and exits 0 on garbage input and on an unwritable DB;
  - a row lands with `TMUX_PANE` set.
- `tests/test_window_view.py`: `pending_permission` is published with exactly one row in the episode, and is null with zero rows, with two rows, or outside a permission wait.
- `tests/routes/test_pane_actions.py`:
  - permission: 400 / 404;
  - 409 on a different `request_id`, on a pane that left needs-input, and on a screen without the summary;
  - allow sends `1`, deny sends `Escape`;
  - reply: 409 on `waiting_for`, 409 on a dialog footer on screen; a paste + Enter otherwise.
- `tests/test_panes.py`: the captured dialog fixture for `permission_dialog`.
- `tests/routes/test_cleanup.py`: `?paths=` evaluates only those worktrees.

**cargo:**
- `category_specs`: a full set; a bad action dropped and the rest kept; nulls tolerated.
- `parse_request` with `category`.

**By hand on macOS:**
- **After rebuilding the `.app`:**
  - every button does what its row says;
  - Allow and Send ask for Touch ID on a locked screen.
- **After PR 2:**
  - Allow and Deny on a real Bash prompt;
  - Allow on a banner whose dialog has since changed reveals the pane and sends nothing;
  - Send on a question;
  - Ask agent to fix on a red PR.

## Deploying

- **PR 1:** rebuild/relaunch the shell and deploy the JS (merge + `bin/periscope restart`) in either order — each half without the other just means no buttons yet (old shell: never registers the new categories; old JS: never names one), same as any other version-skew gap (`docs/tauri-shell.md`).
  - Run `cargo tauri build` in `src-tauri/`; the running app is `target/release/bundle/macos/Periscope.app`.
- **PR 2:**
  - Merge, then `bin/periscope install-hook`, which registers `PermissionRequest`, then `bin/periscope restart`.
  - Claude reads hooks at session start, so the hook covers only sessions started after the install. Older panes get the dialog variant (Open only) until they restart.

## Done when

- **Every button does what its row says.**
- **Allow and Deny can only ever answer the request their banner was shown for, and only once.** The request id is recomputed from the hook's row, the dialog on screen must match that row's key exactly (`dialog_matches`), and the key goes to the checked pane's `%id`. A tool whose dialog can't be verified gets Open only.
- **No button that lets an agent proceed or types into it works from a locked screen** without authenticating. Deny, which only stops one, needs no auth.
- **No reply is submitted into a dialog, a shell, Claude's shell mode, a Codex pane or onto a draft.** Its Enter goes in only after a second look shows the paste in the composer and no dialog. The paste itself can still land in a dialog that renders in the ~10–20 ms before it; nothing is submitted then. Control characters are stripped first, so the text can't end tmux's bracketed paste and type the rest as keys.
- **A failed press always says so**, in a `notice` banner, since the app isn't brought forward for it.
- **Clean up never deletes without the modal's confirmation.**
- **Nothing changes in a browser tab, or for an older shell or webview.**
