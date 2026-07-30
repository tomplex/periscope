# Spec — opencode support

## Problem

Periscope models exactly one agent CLI. `parse_pane` returns `is_claude: bool`,
and 48 call sites across 8 Python modules and 6 JSX modules branch on it. An
opencode pane is therefore indistinguishable from a shell: no state coloring, no
model/context chips, no rail treatment as an agent tab, no attention sections.

opencode is in real daily use on this machine (`~/.local/share/opencode` has a
26 MB session DB; a live pane was mid-turn on `sts2-seed-finder` while this spec
was written). The dashboard is blind to it.

`is_claude` also conflates two different questions, which is why the field can't
just be reused:

- "is this pane an agent?" — rail treatment, state coloring, filter chip, glyph
- "is this pane **Claude**?" — Claude plan usage (`usage.py:507`), the
  `sessions/<pid>.json` authoritative-state override (`window_view.py:143`)

## Goal

opencode panes are first-class agent tabs in the rail with live state, and
opencode can call periscope's channel tools. Deliberately *not* feature-parity
with Claude — see Non-goals.

| Surface | v1 |
|---|---|
| Detected as agent (rail, coloring, filter, attention) | yes |
| `state`: working / idle | yes |
| `state`: needs-input | **no** — ground truth not yet captured |
| model, context %, cost | yes |
| Channel tools (`notify`, `link_pr`, `link_linear`, …) | yes, via existing MCP shim |
| Push notifications into the pane | no — impossible over MCP |
| Transcript view, narrator status lines | no |
| Spawning opencode from the launcher | no |
| Plan usage | no — Claude-only by construction |

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
   Four of ten frames are fully blank mid-turn. A cell-based working detector
   would flicker exactly like the pre-`smooth_spinner` Claude spinner did
   (invariant #7).

## Detection contract

An opencode pane is one whose **last 4 non-empty lines** contain
`ctrl+p commands` — the only observed string present in *both* states —
corroborated by the `╹▀` separator row immediately above the status row.

The "last 4 non-empty lines" window is not incidental: it is the same rule as
Claude detection (invariant #2), and it is what stops a scrolled-back opencode
footer from marking a pane as an agent after the user has quit back to a shell.

Rejected alternatives:

- **`pane_current_command == "opencode"`** (verified: the live pane reports
  exactly that, while Claude panes report the version string `2.1.220`). Cheap
  and robust, but it introduces a second source of truth that can disagree with
  the capture, and it cannot produce `state` — periscope needs the capture
  regardless. One source of truth, one conflict-resolution question avoided.
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
| `agent` | `ctrl+p commands` present | `"opencode"` |
| `state` | `esc interrupt` on the status row → `working`, else `idle` | fed through the existing `smooth_spinner` hysteresis, not read raw |
| `context_pct` | `(33%)` from `164.3K (33%)` | int, same units as Claude's |
| `cost` | `$4.31` | new field; Claude's status line carries `$` too but periscope does not parse it today. Additive and nullable |
| `model` | middle field of `Build · GPT-5.6 Sol OpenAI · xhigh` | **verbatim**, no provider split — see below |
| `oc_agent` | first field (`Build`) | opencode's own agent/mode name |
| `pending_input` | text inside the `┃` prompt box rows | same idiom as Claude's composer |
| `needs_input` | — | always `False` in v1 |

The model field has no delimiter between model name and provider (`GPT-5.6 Sol
OpenCode Zen` vs `GPT-5.6 Sol OpenAI` — the provider varies per session).
Splitting it would require a hardcoded provider list that goes stale on every
opencode release. Take the whole middle field as the model string; the rail shows
`GPT-5.6 Sol OpenAI`, which is more information than a wrong split.

`state` is two-valued in v1. `needs_input` requires capturing a live permission
dialog, which could not be forced on the running session; reverse-engineering it
from the binary was attempted and abandoned (the TUI is bundled JS and the
runtime strings drown the UI literals). Adding it later is a fixture plus a
detector, no structural change — the field already exists and already defaults
false.

## The `agent` field

`is_claude: bool` becomes `agent: str | None` (`"claude"` / `"opencode"` /
`None`). No compatibility shim, no additive second field — two fields that can
disagree is the bug this refactor exists to prevent.

`smooth_is_claude` → `smooth_agent` (`panes.py:86`). Stickiness must remember
*which* agent, not just that there was one: `_claude_last_seen: dict[str, float]`
becomes a dict of `(agent, timestamp)`, so a dialog that blanks an opencode
footer restores `"opencode"` rather than defaulting to Claude.

Sites where the current conflation is load-bearing and must become
Claude-specific, not agent-generic:

| Site | Change |
|---|---|
| `usage.py:507` | gate to `agent == "claude"` — Claude *plan* usage; an opencode pane_id in that list would poison the sample set |
| `window_view.py:143` | the `sessions/<pid>.json` override sets `agent = "claude"` (was `is_claude = True`). It is Claude's session-status file, so finding one *is* proof of Claude — the assignment just has to name the agent instead of a boolean |
| `channels.py:1096`, `activity.py:920` | agent-generic (both mean "is this an agent pane") |

The frontend has one idiom repeated five times —
`w.name || (w.is_claude ? "claude" : "shell")` (`RailRows.jsx:149`,
`Detail.jsx:419`, `Rail.jsx:261`, `Rail.jsx:298`, `AttentionSections.jsx:23`).
Under `agent` it collapses to `w.name || w.agent || "shell"`. Five concrete call
sites clears the dedup bar: extract `paneLabel(w)` into `static/src/util.js`.

Remaining frontend changes:

- `RailRows.jsx:146` — `stateCls` keys off `w.agent ? state : "shell"`
- `AttentionSections.jsx:249` — glyph per agent: `✻` Claude, `▣` opencode
  (opencode's own message-footer glyph), `$` shell
- `Detail.jsx:114,258,261,271,404,412,622` — `w.agent` truthiness; the model and
  context chips already render whatever strings the server sends
- `filter.js:15` — the `claude` filter becomes `agents` (`w.agent != null`).
  Per-agent filter chips are premature for a machine with one opencode pane and
  twelve Claude panes; revisit when the ratio changes
- an `OC` chip on opencode pane cards, so a mixed rail is readable at a glance

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
process was verified to carry `TMUX_PANE=%70` in its environment, and the shim
reads `$TMUX_PANE` to address itself. Nothing in the shim or in periscope's MCP
server is Claude-specific — the session registry keys on the shim's hello frame.

Two consequences to accept explicitly:

- **Push notifications will not work.** Periscope pushes
  `notifications/claude/channel`, a Claude extension; opencode's MCP client will
  ignore it. Out of v1 scope by decision, and the upgrade path is known (opencode
  plugins expose `experimental.chat.system.transform`, which can inject pending
  notifications into the next turn's system prompt).
- **`spawn_claude` called from an opencode pane spawns Claude.** Surprising but
  harmless. Leave it; gating it is a plan-phase judgement call, not a spec
  requirement.

Verification is a live check, not a unit test: add the config block, restart the
pane, confirm the pane appears in `_MCP_SESSIONS` and that a `notify` call from
opencode raises an alert on the right card.

## Testing

- `tests/test_panes.py` — the idle and working captures above go in verbatim as
  fixtures, alongside the Claude cases. This is the existing discipline: one case
  per observed TUI variation, in the one suite that tracks it.
- A "blank spinner frame mid-turn" case: a working capture whose spinner cells
  are all `⬝` must still parse as `working`, because `esc interrupt` is present.
- A scrollback case: an opencode footer *outside* the last 4 non-empty lines must
  parse as a shell (invariant #2, opencode dialect).
- `tests/test_window_view.py` — `smooth_agent` returns `"opencode"` (not
  `"claude"`, not `True`) across a detection gap.
- `tests/test_usage.py` — an opencode pane is excluded from the plan-usage pane
  list.
- `npm test` — `paneLabel` per agent; `filter.js` `agents` chip over a
  mixed-agent window list; rail render of an opencode row.

## Non-goals

Recorded so they don't get half-built:

- Transcript view and narrator status lines for opencode panes
- Push notifications into an opencode pane
- Launching opencode from the omnibox / launcher, or a per-repo default agent
- opencode in `usage.py`, `resurrect.py` (`--resume`), or `session_status.py`
- `pane_sessions` mapping for opencode

## Known fragility

The footer is the entire contract, and opencode 1.19 can move it. The mitigation
is the one that has worked for Claude's status line: fixtures in
`tests/test_panes.py`, and a marker chosen for durability — `ctrl+p commands` is
a keybinding hint, which churns less than a version string or a layout.

Failure mode if it does break: opencode panes silently render as shells. Same
triage entry as Claude's — "everything looks like a shell" means fix the
detector first.

## Discovered, deliberately unused

Facts found while investigating that the next slice will want:

- **Session id is in argv.** A resumed pane runs
  `opencode -s ses_0501e7023ffeVSptZuxTP10N2x`. That is a free pane→session
  mapping with no hook, for the transcript view — though it is absent for a
  freshly-started pane.
- **Sessions live in sqlite**, not JSONL: `~/.local/share/opencode/opencode.db`,
  tables `session` (directory, title, model, cost, tokens, `time_updated`),
  `session_message`, `part`. Held open by the pane's own process. A transcript
  adapter reads this; `turns.py` is JSONL-shaped and will need a second backend.
- **opencode auto-titles sessions** and puts the title in the pane title
  (`OC | Codebase overview`). The narrator's status-line role can be filled for
  opencode panes at zero Haiku cost by reading the title.
- **A plugin API exists** (`@opencode-ai/plugin`, already installed at 1.18.9)
  with native tool registration, `chat.message` (session id per prompt — the
  `pane_session_hook.py` analog), and the system-prompt transform that unlocks
  push. This is the door for everything MCP can't reach.
