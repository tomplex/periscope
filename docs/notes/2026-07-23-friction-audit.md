# Periscope friction audit — 2026-07-23

Diagnosis only. No code changed. Six findings, ranked by impact × confidence.

## The unifying story

Five of the six findings trace to one cause: **the tracks / single-session
migration changed two ground assumptions, and several subsystems still encode
the old ones.**

| Assumption | Before | After tracks |
|---|---|---|
| `tmux_session` discriminates a project | per-project (`tc/fdy/…`) | constant `"periscope"` |
| Rail is keyed by repo + worktree + session | `repo_order` / `worktrees_by_repo` / `panes_by_worktree` | `track_order` / `tabs_by_track` / `branch_order_by_track` |

Anything that still keys off session name lost its discriminating power silently.
Anything that still writes the old pref keys now writes to `/dev/null` in effect.
Neither failure raises an error, which is why it reads as "stale and friction-y"
rather than "broken."

This predicts where the next bug will be: any remaining code that treats
`w["session"]` as identity, or writes pre-tracks pref keys.

---

## 1. Pane identity thrashes — re-mint storm

**Severity: highest.** Root cause of *two* separate reported symptoms.

`pids.py:_rebind_pid` decides a newly-sighted window *is* a previously-known
window and hands it that window's entire persisted state entry. Two passes,
both accepting any orphan younger than `_PID_TTL_S` = **30 days** (`pids.py:17`):

```python
# Pass 1: strong match on (session, name).
if ls.get("session") == session and ls.get("name") == name: return pid
# Pass 2: secondary match on (branch, cwd) when both are set.
if ls.get("branch") == branch and ls.get("cwd") == cwd: return pid
```

- **Pass 1 lost its power to the migration.** `session` is now constant
  (`config.py:33` `MANAGED_SESSION = "periscope"`), so Pass 1 matches on
  **window name alone** — while the narrator actively renames and recycles names.
- **Pass 2 is maximally collision-prone.** A fresh Claude at fdy root on master
  has `branch="master"`, `cwd=<fdy root>` — identical to every Claude started
  there in the last 30 days.

### 1a. Symptom: stale PR / Linear on a fresh pane

`pids.py:86-93` keeps the stale payload alive on purpose:

```python
_IMMUNITY_FIELDS = ("notes", "tags", "linked_pr", "linked_linear", ...)
```

Correct intent (archiving a project shouldn't erase its linkage), but it means
`#7538` / `FDY-2641` persist indefinitely as a landmine for the next fresh pane
on fdy/master to inherit. `window_view.py:158-166` then renders them, with
`linked_pr` **overriding** the auto-detected PR — stale beats truth.

### 1b. Symptom: detail pane navigates away while working in a shell

The codebase already documents this as an unsolved bug, with a tripwire
(`pids.py:120-129`):

> Loud on purpose: a re-mint changes the window's identity, so any pid-keyed UI
> state (detail-pane selection, pinned rows) silently detaches. **Reported as
> "detail pane closes on cd"; never caught in the act** — this tripwire names
> the window if it happens again.

**It has now been caught in the act.** `~/.config/periscope/periscope-8765.log`:

| Window | Re-mints (Jul 20 → Jul 23) |
|---|---|
| `periscope:13` | **683** |
| `periscope:12` | 27 |
| `periscope:20` | 4 |
| `tc-vendor-dict-ingestion:1` | 4 |
| `periscope:3` (the `shell-` window) | 1 — today 11:21 |

725 total in that log; 9,576 in `launchd-stderr.log`.

Chain: duplicate `@periscope_id` on two windows → `_resolve_one` re-mints
(`pids.py:113-131`) → identity changes → `railSelection` holds `pane:<old-pid>`
(`store.js:17`) → `Detail.jsx:564` finds nothing → selection detaches.

It re-mints **every poll** — note the 3s cadence at `16:59:31, :34, :37, :40, :43`.
Not a one-shot detach; continuous churn for as long as the duplicate persists.

Duplicate source is named in the code's own comment — *"session-copy,
swap-window, set-option racing with new-window."* Tracks leans hard on exactly
those: `migrate_single_session.py` does bulk `tmux move-window`, and moving a tab
between tracks does it again. Higher shuffle rate → higher collision rate.

---

## 2. Omnibox "open" is a silent no-op

**Severity: high.** Blocks a core workflow, fails without any error.

Two dead links in a row.

**Link 1 — `ensure_session` returns early, creating nothing** (`open_ops.py:166-169`):

```python
session = config.MANAGED_SESSION
if _session_live(session) and _session_owns_dir(session, pinned_dir):
    return session, _claude_pid_for_dir(session, pinned_dir)
```

Post-migration, *"already open?"* is answered by **cwd ownership inside the
shared session** (its own docstring, lines 163-165). If any pane anywhere in
`periscope` has fdy master as cwd — visible, collapsed, filtered, or stale —
this returns early and creates no window.

Note the irony: CLAUDE.md's own invariant reads *"Idempotent create-or-focus is
NAME-based, not cwd-based. cwd collides (the documented footgun)."* The
single-session migration had to move it back to cwd-based, reintroducing exactly
that footgun.

**Link 2 — the placement it writes is read by nobody.** `place_in_rail`
(`open_ops.py:130-131`) writes:

| Key written | Read by the tracks-era rail? |
|---|---|
| `worktrees_by_repo` | No — *"intentionally dropped"* (`prefs.js:263`) |
| `panes_by_worktree` | No — `getTabsByTrack()` spreads only `tabs_by_track` (`prefs.js:280`) |
| `repo_order` | Only as fallback — `ui.track_order \|\| ui.repo_order` (`prefs.js:267`) |

Once `track_order` exists — and `Rail.jsx:88+` writes it as soon as the rail
persists live entries — the write is a total no-op. The values are wrong-shaped
anyway: repo strings into a list ordered by `track_id`; panes keyed by tmux
session instead of track id.

**Why "not consistently":** fails hard for repos that already have a pane owning
their cwd (fdy master), appears to work for repos with none, where the window is
genuinely created and `mergeLiveAndPrefs` folds it in on the next poll.

No exception is raised on this path, so there is no toast and no error.

---

## 3. Rail capability hole — can't open a not-live thing into a chosen track

**Severity: medium-high.** Design gap rather than a bug.

| Surface | Target an existing track? | Open something not currently live? |
|---|---|---|
| Per-track **"+ New tab"** (`LauncherModal.jsx`) | Yes — `openLauncher(trackId)` | **No** |
| Header **⌘K omnibox** (`OpenOmnibox.jsx`) | **No** | Yes |

- `LauncherModal.jsx:39-48` — `trackBranches()` derives its branch list from
  `windows.value`, i.e. **live windows only**, skipping any without `w.branch`.
  "Existing branches" means *currently open* branches.
- `LauncherModal.jsx:94-96` — the only escape hatch is `+ new branch…`, gated on
  `trackRepo`, spawning off that track's repo. A loose/repo-less track gets
  "command list only" (its own comment, lines 92-93).
- `routes/open.py:17-23` builds only `PathTarget` / `BranchTarget` / `PRTarget` —
  **no `track_id` in the descriptor.** The server picks placement; the client
  cannot aim at a track.
- `classify.js:37` offers `· new track…` — the omnibox's only track verb is
  *create a new one*.

The affordance sitting next to the track you're looking at is the weak one; the
powerful one can't aim. Every "open X in track Y where X isn't running" is a detour.

Minor confusable: `periscope/tabs.py` is **file-preview tabs**, unrelated to rail
tabs. Two meanings of "tab" in the codebase.

---

## 4. Terminal cursor renders one cell ahead, with reconcile stutter

**Severity: medium.** Constant low-grade tax on shell work.

Not local echo — `terminalCore.js` `term.onData` (488) only sends; `term.write`
fires only on inbound data (547, 551). It is a sampling race in the reconcile
frame (`tmux_mirror.py:415-419`):

```python
# Capture first so the cursor sample (display-message) is the
# fresher of the two.
self._send_command(f"capture-pane -p -e -t {pane_id}", on_capture)
self._send_command(f"display-message -p -t {pane_id} '{DISPLAY_FMT}'", on_display)
```

Two separate tmux commands: body sampled at T1, cursor at T2 > T1. Any character
echoed between them advances the real cursor but is **absent from the captured
rows**. The frame paints stale body + fresh cursor.

`build_reconcile_frame:173` makes it visible rather than benign:

```python
parts.append(b"\x1b[%d;1H" % (i + 1) + row + b"\x1b[0m\x1b[K")
```

`\x1b[K` erases to end of line, wiping the character the fresher cursor already
moved past. Live `%output` then delivers that byte and snaps it back — the stutter,
once per reconcile.

Why raw shells specifically:
1. Typing in a shell means *you* generate the output — maximal chance of landing a
   keystroke inside the T1→T2 window.
2. Reconciles are *"armed only by output"* (`ReconcileTimer:179-182`) — typing
   continuously re-arms the racy frame.
3. Claude's alt-screen TUI repositions the cursor itself every redraw, masking it.

The intent comment is backwards: making the cursor *fresher* than the body is the
wrong direction. Stale-cursor-behind-text self-corrects invisibly on the next
byte; fresh-cursor-ahead leaves a visible gap.

---

## 5. Project CLAUDE.md is 48 commits stale

**Severity: medium, but the cheapest fix here.** A multiplier on everything else —
every Claude session in this repo is briefed on an architecture that no longer exists.

- Last touched 48 commits ago. Commit `38d09ad` — *"track-based rail organization
  (tracks replace project/worktree/workspace; single tmux session)"* — landed after.
- Still describes rail membership as **SESSION-ANCHORED**, grouped by
  project/worktree, with `panes_by_worktree.__main__` and the `OpenOmnibox` as the
  `+ new` menu replacement.
- Documents `place_in_rail` writing `repo_order` / `worktrees_by_repo` /
  `panes_by_worktree` as the correct mechanism — see finding #2 for why that is now
  a no-op.
- Asserts the create-or-focus invariant is name-based — see finding #2 for why it
  is now cwd-based.
- 11 modules exist that it never mentions: `tracks`, `tabs`, `workspaces`,
  `session_status`, `tmux_input`, `window_view`, `projects`, `resurrect`,
  `gitutil`, `bg_commander`, `migrate_single_session`.

**The global instruction stack is fine** — not the problem:

| Layer | File | Last touched |
|---|---|---|
| Core prompt | `~/.claude/prompts/tom-core.md` | Jun 22 |
| Mode overlay | `~/.claude/prompts/tom-personal.md` | May 21 |
| Global memory | `~/.claude/CLAUDE.md` | Mar 26 |

Every piece of tooling `tom-core.md` references exists and resolves
(`wrap.mjs`, `doc-render-cache/`, all four subagents). The staleness is confined
to the project layer.

---

## 6. Periscope silently disables tmux-continuum; reboot restored a 24-day-old layout

**Severity: high.** Not a one-off — an ongoing steady state, caused by periscope.

Saves in `~/.local/share/tmux/resurrect/`:

| Save | Size |
|---|---|
| Jul 23 11:31 | 269 B ← current `last` |
| Jul 23 11:22 | 161 KB — 28 panes, 25 in `periscope` |
| **Jun 29 13:35** | 120 KB |
| Jun 29 13:25 … 08:44 | 120–146 KB, every 10 min |

**Nothing saved between Jun 29 13:35 and Jul 23 11:22 — a 24-day hole.** On this
morning's reboot, continuum restored the newest thing it had: the Jun 29 layout.
That is the cruft.

Periscope's own hook is **not** at fault — run against a copy of the 11:22 file it
exits clean:

```
$ python3 -m periscope.resurrect /tmp/rr-test.txt
resurrect-resume: rewrote 2 claude pane(s)
exit=0
```

Continuum's config is also intact: `status on`, `status-interval 15`,
`status-right` contains `continuum_save.sh`, `@continuum-save-interval 10`,
`@continuum-restore on`.

**Live hazard:** the current `last` is a **269-byte, 2-pane** file naming a `main`
session that no longer exists. The next reboot restores essentially nothing — the
opposite failure, equally unwanted.

### Root cause: periscope's control-mode clients suppress continuum entirely

**tmux-continuum has no timer.** Its save fires purely as a side effect of
`status-right` format expansion — which is why the hook is installed there.
A status line is only expanded when it is *drawn for a client*.

Every client attached to this server is control-mode (periscope's mirror + input
clients), with no tty:

```
client-21443 | tty= | flags=attached,focused,control-mode,UTF-8
client-49554 | tty= | flags=attached,focused,control-mode,ignore-size,read-only,UTF-8
```

Control-mode clients render no status line → `status-right` is never expanded →
`continuum_save.sh` is never invoked → **no saves, indefinitely.**

| Period | Attached clients | Saves |
|---|---|---|
| ≤ Jun 29 13:35 | real terminal | every 10 min |
| Jun 30 – Jul 22 | dashboard only (control-mode) | **none — the 24-day gap** |
| Jul 23 11:22, 11:31 | briefly, during reboot/restore | 2 |
| since 11:31 | dashboard only | none (the 11:54 file was a manual run) |

Ruled out along the way: periscope's rewrite hook (runs clean), periscope's test
suite (isolated `-L` socket + `-f /dev/null`, `tests/test_tmux_input.py:48-58`),
periscope source (never touches `status-right`), the plugins (unchanged since
Mar 11), and `.tmux.conf` (unchanged since Jun 5).

**This is structural, not a one-off.** The more completely periscope replaces an
attached terminal, the more completely it disables tmux persistence. The
single-session/tracks migration accelerated it: one managed session, viewed
entirely through the dashboard, no reason to attach a real client. Every reboot
restores whatever was last saved while a real terminal happened to be attached —
this morning, 24 days stale.

Fix direction (not yet designed): periscope should drive the save itself rather
than depending on a status line it never renders — e.g. invoke
`continuum_save.sh` (or `resurrect`'s save directly) on its own interval from the
activity worker, which already ticks every 30s in prod.

---

## Suggested order, if this becomes work

1. **#1 pane identity** — fixes two symptoms at once; the log gives a ready-made
   regression signal (count `re-minting` warnings before/after).
2. **#2 omnibox no-op** — small, mechanical; `place_in_rail` writes the tracks
   keys and `ensure_session` stops answering "already open?" by cwd.
3. **#5 CLAUDE.md** — cheapest, and stops future sessions reasoning from a dead map.
4. **#4 cursor race** — self-contained in `tmux_mirror`.
5. **#6 continuum** — root cause established; periscope drives the save itself
   instead of relying on a status line it never renders. Until then, tmux
   persistence is effectively off.
6. **#3 rail capability hole** — needs a design conversation, not just a fix.
