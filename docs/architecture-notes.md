# Architecture notes

## Key invariants the split preserved

- **No `from server import …` in `periscope/`.** Double-import landmine
  (shim runs as `__main__`; a separate import would re-execute it as
  module `server` with two copies of every global). Enforced by
  grep: `grep -rn "from server import\|^import server\b" periscope/ tests/`.
- **Lifespan owns `MCP_SOCKET_PATH` cleanup.** `periscope/channels.py`
  must never `os.unlink` the socket on shutdown — that's the lifespan's
  job. Double-unlink is benign today but the ownership is the invariant.
- **`_STATE` rebind across modules.** Multiple modules do
  `from periscope.store import _STATE` (binding the dict by reference).
  The `clean_state` fixture in `tests/conftest.py` must re-bind in every
  consumer module so test mutations are seen consistently.


## Frontend (`static/src/` → `static/dist/`)

A Preact + `@preact/signals` app, built by Vite from `static/src/` to the
committed `static/dist/app.js`. `index.html` is a shell that mounts `<App>`
into `#app`. Components grouped by area:

| Area | Modules |
|---|---|
| entry / state | `src/main.jsx` (mount + boot), `src/store.js` (transient signals — the read model), `src/prefs.js` (server-prefs cache as a signal — the persistence boundary) |
| chrome | `src/chrome/{Header,FilterBar,UsagePill}.jsx` |
| poll | `src/poll.js` — the single `/api/state` poll loop (writes `windows` / `projects` / `usage` signals); `openModal` bridge for poll-driven open requests |
| split view | `src/split/{Split,Rail,RailRows,Detail,AttentionSections,SectionHeader,Transcript}.jsx` + `src/split/railTree.js` (`mergeLiveAndPrefs`) — the only dashboard view (grid retired). Rail membership is TRACK-ANCHORED: every window carries a server-resolved `track_id`, and the tree is Track → (derived Branch) → Pane. A track spanning ≥2 branches renders branch sub-clusters; otherwise it renders flat. Branch rows are DERIVED from live `w.branch`, not entities — you can't close one, only tear down the track |
| terminal | `src/terminal/{Terminal,TerminalSearch}.jsx` + `src/terminal/terminalCore.js` (imperative xterm + `/ws/pane`) + `theme.js` |
| overlays | `src/overlays/{Dialog,Toast,Overlays,CommandsModal,CleanupModal,SettingsModal,LauncherModal,OpenOmnibox}.jsx` + `src/hooks/useEscape.js` (LIFO escape stack). `OpenOmnibox` is the command-palette (↑↓↵ nav, ⌘K, grouped cards) behind the header's single `+ new` button — it replaced the old `+ session` / `+ project` / `review PR` menu and the retired `NewProjectModal` / `ReviewPrModal` / `OpenPickerModal`. `src/open/classify.js` is its pure (unit-tested) query→cards classifier |
| util | `src/util.js` (`targetQuery` last-colon split, `apiCall`, `relTime`, `prUrl`, `rewriteLgtmHost`) |

Still vanilla under `static/`: `history.js` + `util.js` (the `/history` SPA —
its own `history.html` entry, untouched by the migration), `sw.js` (no-op PWA
gate), `vendor/xterm.{js,css}` (plain `<script>` so `Terminal`/`FitAddon` land
on `window` — don't edit, replace wholesale). `connection-banner` stays in
`index.html` (read by `src/poll.js`, not rendered by any component).

Migration notes worth knowing:
- **Split is the only view.** Grid and stream were both retired; there's no view
  switch in the header. The `body[data-view]="split"` attribute is still
  asserted on mount because some legacy CSS keys off it.
- **LGTM review iframes** (modal + detail) are created imperatively and parked
  in a Preact-owned host so reconciliation never reloads them; `<Detail>` keeps
  every opened review's iframe mounted (CSS-hidden) so switching never reloads.
- **Static is served `Cache-Control: no-cache`** (`_RevalidateStaticFiles` in
  `app.py`) — ETag revalidation, so a rebuild/restart never serves a stale
  bundle (the stable `app.js` filename would otherwise cache hard).
