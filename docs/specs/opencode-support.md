# Spec — opencode support

## Problem

Periscope models exactly one agent CLI. `parse_pane` returns `is_claude: bool`,
and 7 Python modules plus 7 JS modules branch on it. An opencode pane is
therefore indistinguishable from a shell: no state coloring, no model/context
chips, no rail treatment as an agent tab, no attention sections.

opencode is in real daily use on this machine (`~/.local/share/opencode` has a
26 MB session DB; a live pane was mid-turn on `sts2-seed-finder` while this spec
was written). The dashboard is blind to it.

`is_claude` also conflates two different questions, which is why the field can't
just be reused:

- "is this pane an agent?" — rail treatment, state coloring, filter chip, glyph
- "is this pane **Claude**?" — plan usage, the `sessions/<pid>.json` state
  override, the context-reset check, the transcript view, `list_claudes`

The second list is longer than it looks, and three of its members fail *silently*
when handed an opencode pane — see §Claude-only sites.

## Goal

opencode panes are first-class agent tabs in the rail with live state, and
opencode can call periscope's channel tools. Deliberately *not* feature-parity
with Claude — see Non-goals.

| Surface | v1 |
|---|---|
| Detected as agent (rail, coloring, filter, attention) | yes |
| `state`: working / idle / done | yes |
| `state`: needs-input | **no** — ground truth not yet captured |
| model, context % | yes |
| Channel tools (`notify`, `link_pr`, `link_linear`, …) | yes, via existing MCP shim |
| `list_claudes` / `send_to` / `peek` / `terminate` targeting an opencode pane | no |
| Push notifications into the pane | no — impossible over MCP |
| Transcript view, narrator status lines, context-reset events | no |
| Spawning opencode from the launcher | no |
| Plan usage | no — Claude-only by construction |

`done` is in scope because it is derived, not scraped: `window_view.py:162`
promotes `idle` → `done` from per-pid transition stamps, and that logic is
agent-generic. Two scraped states, three rendered.

## Ground truth

Captured from a live pane (`%70`, opencode 1.18.9). This is the whole contract,
so both states are recorded verbatim.

**Idle** (last three content rows):

```
  ┃  Build · GPT-5.6 Sol OpenCode Zen · high              ~/dev/sts2-seed-finder:main
  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   /Users/tom/dev/sts2-seed-finder        56.0K (5%) · $0.88  ctrl+p commands    • OpenCode 1.18.9
```

**Working:**

```
  ┃  Build · GPT-5.6 Sol OpenAI · xhigh
  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   ⬝⬝■■■■■■  esc interrupt                164.3K (33%) · $4.31  ctrl+p commands
```

Three things follow, each of which rules out an obvious detector:

1. **`• OpenCode <version>` is idle-only.** It is replaced by the spinner +
   `esc interrupt` during a turn. Keying detection on it would classify every
   working opencode pane as a shell — the exact failure mode of invariant #2
   (status line must be currently visible, not in scrollback).
2. **The cwd/branch fields are idle-only too** (`~/dev/…:main` on the box footer,
   the absolute path on the status row). Periscope already has cwd from tmux and
   branch from git; do not source either from the footer.
3. **The spinner cells are unreliable.** Ten samples at 500 ms of a pane that was
   continuously working: `■■■■⬝⬝⬝⬝`, `⬝⬝⬝⬝⬝■■■`, `⬝⬝⬝⬝⬝⬝⬝⬝`, `⬝⬝⬝⬝⬝⬝⬝⬝`,
   `■■■■■⬝⬝⬝`, `⬝⬝⬝⬝■■■■`, `⬝⬝⬝⬝⬝⬝⬝⬝`, `⬝⬝⬝⬝⬝⬝⬝⬝`, `⬝⬝■■■■■■`, `■■■■■■⬝⬝`.
   Four of ten frames are fully blank mid-turn while `esc interrupt` stayed
   present. A cell-based working detector would flicker exactly like the
   pre-`smooth_spinner` Claude spinner did (invariant #7).

## Detection contract

An opencode pane is one whose **last 4 non-empty lines** contain
`ctrl+p commands` **and** a row matching the `╹▀`-prefixed separator. Both
conditions, not one plus prose corroboration: `ctrl+p commands` alone is a bare
substring that any shell could print.

The "last 4 non-empty lines" window is the same rule as Claude detection
(invariant #2), and it is what stops a scrolled-back opencode footer from marking
a pane as an agent after the user has quit to a shell. Verified safe against
trailing blank rows: `_split_buffers` (`panes.py:337-340`) filters to non-empty
rows *before* `_detect_status` slices `lines[-4:]`, so the 23 blank trailing rows
in a live 57-row capture cannot push the status row out of the window.

**Dialect precedence: Claude wins.** `parse_pane` runs Claude detection first and
only falls through to the opencode dialect when `STATUS_RE` does not match.
`STATUS_RE` is highly specific and `ctrl+p commands` is not; a sweep of all 18
live panes found zero false positives, but the ordering must be stated rather
than left to detector overlap.

Rejected alternatives:

- **`pane_current_command == "opencode"`** (verified: the live pane reports
  exactly that, while Claude panes report the version string `2.1.220`). Cheap
  and robust, but it introduces a second source of truth that can disagree with
  the capture, and it cannot produce `state` — periscope needs the capture
  regardless.
- **Querying opencode's local HTTP API.** `lsof` on the live process shows **no
  listening socket** in TUI mode (only an outbound HTTPS connection and a
  client-side unix socket); it holds `opencode.db` open directly. The server is
  only reachable with an explicit `--port` / `opencode serve`, so there is
  nothing to discover from the pane's pid.

## Signal extraction

New module `periscope/panes_opencode.py`. `parse_pane` stays the single entry
point in `panes.py` and dispatches on dialect; Claude's detectors are not moved.

| Field | Source | Notes |
|---|---|---|
| `agent` | `ctrl+p commands` + `╹▀` row | `"opencode"` |
| `spinner` | the literal `"working"` when `esc interrupt` is on the status row | see below — this is the verb channel, and it must carry a truthful verb |
| `context_pct` | `(33%)` from `164.3K (33%)` | int, percent-*used*, same polarity as Claude's (`RailRows.jsx:50-55` treats high as hot) |
| `model` | middle field of `Build · GPT-5.6 Sol OpenAI · xhigh` | **verbatim**, no provider split |
| `pending_input` | text inside the `┃` prompt box rows | same idiom as Claude's composer |
| `needs_input` | — | always `False` in v1 |

`$0.88` and the `Build` agent name are parsed by the same regex but **not
surfaced**: no consumer exists for either, Claude's `$` field is deliberately
unparsed today, and the cost figure's semantics are suspect (it read `$4.31`
against both `164.3K (33%)` and `180.1K (36%)` of the same session — it does not
track tokens the way the name implies). Adding them later is one capture group.

The model field has no delimiter between model name and provider (`GPT-5.6 Sol
OpenCode Zen` vs `GPT-5.6 Sol OpenAI` — the provider varies per session).
Splitting it would require a hardcoded provider list that goes stale on every
opencode release. Take the whole middle field; the rail shows
`GPT-5.6 Sol OpenAI`, which is more information than a wrong split.

**`spinner` is the working signal, and it keeps its hysteresis.** opencode's
footer has no verb (Claude renders `Brewing…`), so the opencode branch sets the
literal `"working"` — which is what `AttentionSections.jsx:233` already falls
back to (`spinner || "working"`). Routing it through `smooth_spinner` is
deliberate: the marker itself is stable, but a `capture-pane` that lands
mid-redraw can drop the whole footer row, and without hysteresis that reads as
`working → idle → working`. That is the same flicker class invariant #7 exists
for; cell-level stability does not cover row-level drops. What made this look
unsafe was the shared key, fixed below.

`needs_input` stays false in v1. It requires capturing a live permission dialog,
which could not be forced on the running session; reverse-engineering it from the
binary was attempted and abandoned (bundled JS, runtime strings drown the UI
literals). Adding it later is a fixture plus a detector, no structural change.

## The `agent` field

`is_claude: bool` becomes `agent: str | None` (`"claude"` / `"opencode"` /
`None`). No compatibility shim, no additive second field — two fields that can
disagree is the bug this refactor exists to prevent.

### Smoothing: one helper, keyed by pane

Two changes to `panes.py`, both load-bearing:

**Key on `pane_id`, not `target`.** `_claude_last_seen` (`panes.py:68`) is keyed
by `target` (`session:index`), which is safe *only* because the value is a
boolean. Indices renumber under `move-window` — which moving a tab between tracks
does routinely (invariant #11) — so a stale entry can be served to a different
pane. Today that returns `True` for a Claude pane: benign. Under `agent`, a
renumbered Claude pane would inherit `"opencode"` for up to `CLAUDE_STICKY_S`
(120 s) and then fall through every Claude-only gate this refactor adds — no plan
usage, no transcript, no narrator. `_view_cache` already keys on
`(target, pane_id)` for exactly this reason (`window_view.py:44-45`);
`smooth_agent` and `smooth_spinner` follow it.

Reset rule: a positive detection of a *different* agent **overwrites** the sticky
entry, it does not merge. Stickiness covers a hidden footer, never an agent swap.

**Extract the ladder.** The smoothing sequence exists in two verbatim copies —
`window_view.py:113-129` and `routes/pane.py:65-70` (`/api/pane` returns
`**parsed`, so it is the Detail view's field source). Four steps: smooth spinner,
smooth agent, force `"shell"` when no agent, promote a smoothed spinner back to
`working`. A dialect branch landing in one copy only ships "rail works, detail
header says shell." Extract `panes.smooth_parsed(target, pane_id, parsed)` and
call it from both.

### Claude-only sites

These fail silently — not loudly — when handed an opencode pane:

| Site | Change | Why |
|---|---|---|
| `activity.py:920` → `_check_reset` (`:923`) | Claude-only | **Fabricates data.** `_check_reset` fires on any `context_pct` drop, then `_compact_or_clear(cwd)` reads `live_transcript_for(cwd)` — the newest *Claude* JSONL in that directory. In a repo with both a Claude and an opencode pane, an opencode `/compact` records a `reset` event on the opencode card quoting Claude's token numbers |
| `activity.py:920` → `narrator.tick` (`:926`) | Claude-only, explicitly | Currently safe only *accidentally*: `narrator.py:422-424` requires a `pane_sessions` row and has no cwd fallback. That is the hook contract, not a gate. Make it a gate |
| `channels.py:1096` (`_do_list_claudes_tool`) | Claude-only | **Reports false success.** `list_claudes` is the discovery surface for `send_to`/`peek`/`terminate`. `send_to` → `_deliver` → `emit_channel_event` returns `True` once the `notifications/claude/channel` write succeeds (`channels.py:917-921`) and records a `channel` activity event claiming delivery. opencode ignores that notification, so a supervisor Claude gets `ok: True` for a message never seen. `terminate` on an opencode pane also becomes newly reachable |
| `Detail.jsx:114`, `:271`, `:622` | `agent === "claude"` | Transcript machinery: `computeMode`, the Transcript/Terminal toggle, and the `openedTr` mount set. `/api/pane/turns` returns `{"turns": null}` for a pane with no `pane_sessions` row (a declared non-goal), so agent-generic ships a Transcript button that opens a blank panel |
| `usage.py:507` | `agent == "claude"` | Clarity, **not** correctness: `_refresh_burn_into_cache` already skips panes with no Claude JSONL (`usage.py:476-477`), contributing nothing to `rates` or the denominator. Gate it so the intent is readable, but do not claim it prevents a bug |
| `window_view.py:143` | assign `agent = "claude"` | The `sessions/<pid>.json` override. Finding one *is* proof of Claude, so the logic is unchanged — the assignment just names the agent instead of setting a boolean |
| `memtest.js:69` | `agent === "claude"` | A Claude-webview memory repro; its `kind:"claude"` pool must not start including opencode panes |

### Agent-generic sites

| Site | Change |
|---|---|
| `window_view.py:109`, `routes/state.py:47` | error dicts: `"agent": None` |
| `window_view.py:118`, `routes/pane.py:67` | force `"shell"` when `agent is None` (moves into `smooth_parsed`) |
| `window_view.py:125`, `routes/pane.py:69` | spinner→`working` promotion (moves into `smooth_parsed`) |
| `window_view.py:162` | `idle` → `done` refinement. Agent-generic: an opencode pane that finishes should read `done`, and `record_state_transition` (`:153`) stamps the edge the same way |
| `RailRows.jsx:146` | `stateCls` keys off `w.agent ? state : "shell"` |
| `RailRows.jsx:188` | the **primary** rail glyph, for every pane row. Per-agent: `✻` Claude, `▣` opencode (its own message-footer glyph), `$` shell. Needs a third CSS class — `icon-claude`/`icon-shell` are the only two today |
| `AttentionSections.jsx:249` | same glyph treatment; this one is inside the `PINNED` block only |
| `Detail.jsx:258`, `:261`, `:404`, `:412` | `w.agent` truthiness; the model and context chips already render whatever strings the server sends |
| `filter.js:15` | the `claude` filter becomes `agents` (`w.agent != null`) |
| `FilterBar.jsx:22` | **the other half of the rename.** `{ key: "claude", label: "claude" }` is the chip that *writes* `currentFilter`; `filter.js` only reads it. Rename one and the chip falls through to `filter.js:18 return true`. It carries no `is_claude` token, which is why a grep-driven inventory misses it |

Per-agent filter chips are premature for a machine with one opencode pane and
twelve Claude panes; revisit when the ratio changes. No prefs migration is needed
— `currentFilter` is a transient signal (`store.js:20`).

### The label idiom

`w.name || (w.is_claude ? "claude" : "shell")` appears at `RailRows.jsx:149`,
`Detail.jsx:419`, `Rail.jsx:261`, `Rail.jsx:298`, and as a named function at
`AttentionSections.jsx:22-24`. Under `agent` it collapses to
`w?.name || w?.agent || "shell"`. Four inline sites plus an existing definition
clears the dedup bar: promote it to `paneLabel(w)` in `static/src/util.js`.

Keep the optional chaining — `originLabel` (`AttentionSections.jsx:26-28`) calls
it with a possibly-null `w`, and the unguarded form throws.

Plus an `OC` chip on opencode pane cards, so a mixed rail is readable at a glance.

## Channel

No periscope code. Point opencode at the existing shim in
`~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "mcp": {
    "periscope": {
      "type": "local",
      "command": ["python3", "/Users/tom/dev/periscope/channel_shim.py"],
      "enabled": true
    }
  }
}
```

This works because pane identity is inherited, not negotiated: the live opencode
process carries `TMUX_PANE=%70` in its environment, and the shim reads
`$TMUX_PANE` to address itself. The shim is genuinely agent-agnostic — every
`Claude` token in it is comment or docstring, no code branch — and periscope's
registry keys on `hello["pane"]` with no client-identity negotiation.

Three consequences to accept explicitly:

- **Push notifications will not work.** Periscope pushes
  `notifications/claude/channel`, a Claude extension; opencode's MCP client will
  ignore it. The upgrade path is known: opencode plugins expose
  `experimental.chat.system.transform`, which can inject pending notifications
  into the next turn's system prompt.
- **The inter-agent tool graph stays Claude-only** (see §Claude-only sites). The
  shim is clean; `list_claudes` → `send_to` is not, because delivery success is
  reported on a notification opencode drops.
- **`spawn_claude` called from an opencode pane spawns Claude.** Surprising but
  harmless — it creates a working pane. Leave it.

Verification is a live check, not a unit test: add the config block, restart the
pane, confirm it appears in `_MCP_SESSIONS`, and confirm a `notify` call from
opencode raises an alert on the right card.

## Testing

- `tests/test_panes.py` — the idle and working captures above go in verbatim as
  fixtures, alongside the Claude cases. Existing discipline: one case per
  observed TUI variation, in the one suite that tracks it.
- A **blank-spinner-frame** case: a working capture whose cells are all `⬝` must
  still parse as `working`, because `esc interrupt` is present.
- A **scrollback** case: an opencode footer *outside* the last 4 non-empty lines
  parses as a shell (invariant #2, opencode dialect).
- A **precedence** case: a pane matching `STATUS_RE` resolves to `"claude"` even
  if `ctrl+p commands` also appears in its buffer.
- `smooth_agent` tests join the three existing ones at `tests/test_panes.py:624-643`
  (which already import `CLAUDE_STICKY_S`) — **not** `test_window_view.py`. Cases:
  sticky returns `"opencode"` across a detection gap; a different agent overwrites
  rather than merges; two panes whose `session:index` collides after a renumber do
  not inherit each other's agent.
- `tests/routes/test_pane.py:23` and `tests/routes/test_state.py:42` patch
  `smooth_is_claude` — both need updating for `smooth_parsed`.
- `tests/test_usage.py` — an opencode pane is excluded from the plan-usage list.
- `tests/test_activity.py` — an opencode pane never reaches `_check_reset`.
- `npm test` — `paneLabel` per agent (including the null-`w` path); the `agents`
  filter chip end-to-end from `FilterBar` through `filter.js`; rail render of an
  opencode row with its glyph.

## Non-goals

Recorded so they don't get half-built:

- Transcript view, narrator status lines, and context-reset events for opencode
- Push notifications into an opencode pane
- `list_claudes` / `send_to` / `peek` / `terminate` reaching opencode panes
- Launching opencode from the omnibox / launcher, or a per-repo default agent
- opencode in `usage.py`, `resurrect.py` (`--resume`), or `session_status.py`
- `pane_sessions` mapping for opencode
- Surfacing cost or opencode's agent/mode name

## Known fragility

The footer is the entire contract, and opencode 1.19 can move it. The mitigation
is the one that has worked for Claude's status line: fixtures in
`tests/test_panes.py`, and a marker chosen for durability — `ctrl+p commands` is
a keybinding hint, which churns less than a version string or a layout.

Failure mode if it breaks: opencode panes silently render as shells. Same triage
entry as Claude's — "everything looks like a shell" means fix the detector first.

Unverified: the `╹▀` separator's stability across opencode themes. It could not
be tested without changing the theme on a live session.

## Discovered, deliberately unused

Facts found while investigating that the next slice will want:

- **Session id is in argv.** A resumed pane runs
  `opencode -s ses_0501e7023ffeVSptZuxTP10N2x`. A free pane→session mapping with
  no hook, for the transcript view — absent for a freshly-started pane.
- **Sessions live in sqlite**, not JSONL: `~/.local/share/opencode/opencode.db`,
  tables `session` (directory, title, model, cost, tokens, `time_updated`),
  `session_message`, `part`. Held open by the pane's own process. `turns.py` is
  JSONL-shaped and would need a second backend.
- **opencode auto-titles sessions** and puts the title in the pane title
  (`OC | Codebase overview`). The narrator's status-line role can be filled for
  opencode panes at zero Haiku cost by reading it.
- **A plugin API exists** (`@opencode-ai/plugin` 1.18.9, already installed) with
  native tool registration, `chat.message` (session id per prompt — the
  `pane_session_hook.py` analog), and the system-prompt transform that unlocks
  push. This is the door for everything MCP can't reach.
