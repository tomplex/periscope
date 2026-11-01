# Tauri shell (`src-tauri/`)

Optional native `.app` wrapper for periscope, so it shows up as its
own entry in Cmd-Tab / Dock instead of living inside a browser tab.
The shell is just a Tauri 2 window that loads `http://127.0.0.1:17374`
— launchd still manages the FastAPI server, the GUI app is a pure
presentation layer. Quitting the app doesn't stop the dashboard;
killing the server doesn't kill the app (it just shows a connection
error until the server is back).

Build + launch:

```sh
cd src-tauri
cargo tauri build --debug                  # produces target/debug/bundle/macos/Periscope.app
open target/debug/bundle/macos/Periscope.app
```

`cargo tauri dev` is **broken on this machine** — the raw debug
binary trips AMFI during icon load (`PNGReadPlugin::Initialize` on
the main thread), kernel sets PC=`0x000000000bad4007` and kills the
process before any window appears. Known Tauri-on-macOS class of
bug (tauri-apps/tauri#7351, #11912); no upstream fix. The `.app`
bundle launched via `open` goes through LaunchServices and is not
affected. So the dev loop is "build --debug + open .app" rather
than watch-mode HMR — incremental rebuilds are 5-10s once warm.

Frontend (`static/*`) changes don't need any rebuild — the shell
loads `localhost:17374`, so editing JS/CSS and reloading the window
(Cmd-R inside the app) picks up changes immediately. Only changes
to `src-tauri/src/*.rs` or config need a rebuild.

**Webview recycling (`src-tauri/src/recycle.rs`).** WKWebView leaks
IOSurface-backed graphics regions under real (trusted) user input — ~2.5
regions per rail click, measured 2026-07-28; 2645 regions ≈ 4.8GB killed a
4-day renderer. Nothing in-page fixes it: reload reuses the WebContent
process, memory_pressure doesn't reclaim, synthetic events don't even
reproduce it. The shell therefore destroys and recreates the webview window
when the renderer's phys_footprint (via WKWebView's private
`_webProcessIdentifier` + `proc_pid_rusage`) tops `PERISCOPE_RECYCLE_GB`
(default 2.0), the renderer has lived `PERISCOPE_RECYCLE_MIN_AGE_S` (default
1800s) since the last rebuild, and either periscope has been unfocused
`PERISCOPE_RECYCLE_UNFOCUSED_S` (default 30s) or system input has been idle
`PERISCOPE_RECYCLE_IDLE_S` (default 300s) — plus a manual View → Recycle
Webview item with no gate. The rebuild restores every saved window state but
visibility and takes focus only if periscope had it when the recycle
started, so a recycle firing while you've left for another app never pulls
you back; every rebuild attempt's closure sends its result on one shared
channel, so an attempt that times out but later builds the window still
counts as the success it is instead of the loop giving up with the renderer
leaked. Two hard-won invariants: the rebuild must happen on a LATER tick than
destroy() (same-tick rebuild exits the whole app; an ExitRequested veto in
main.rs covers the windowless gap), and a WebContent WebKit parks in its
process cache at full leaked size instead of exiting must still be killed —
every check re-reads the live WebContent pid on the main thread and, once
that read answers with no recycle in flight, SIGKILLs any other app-owned
WebContent (by `proc_listallpids` + `responsibility_get_pid_responsible_for_pid`)
at or above 256 MB, sparing WebKit's ~7 MB pre-warmed spare. Actions log to
`~/.config/periscope/shell.log`. Test by lowering `PERISCOPE_RECYCLE_GB`,
`PERISCOPE_RECYCLE_MIN_AGE_S`, `PERISCOPE_RECYCLE_UNFOCUSED_S` and
`PERISCOPE_RECYCLE_IDLE_S` via `launchctl setenv` (GUI apps don't inherit
shell env; unset afterwards) and watching that log.

**Zoom (`src-tauri/src/zoom.rs`).** A bare WKWebView has no ⌘+/⌘−, so
View carries Zoom In (⌘=), Zoom Out (⌘-) and Actual Size (⌘0), stepping
Chrome's ladder from 50% to 300% via `set_zoom` (WKWebView `pageZoom`, which
reflows rather than magnifies, so xterm re-renders crisp). The level is
owned by the shell, not the page: it is persisted to
`~/.config/periscope/zoom` and re-applied after every recycle, because a
rebuilt webview starts back at 100%. These are native menu accelerators, so
they reach the menu before the page — nothing in `static/src` may bind ⌘=,
⌘- or ⌘0 expecting to see them in the `.app`.

**Scroll anchoring is off app-wide (`* { overflow-anchor: none }` in
`static/styles.css`).** Safari/WebKit 27 (macOS 27) turns scroll anchoring on
by default. In that WebKit, an anchoring adjustment made while a `box-shadow`
animation is running leaves the compositor's scroll offset ahead of layout's,
and the error accumulates per adjustment: the scroller creeps slowly downward
on screen while `scrollTop` and hit-testing stay where they were, so hover and
clicks land on rows above the pointer. A user scroll resyncs the two; a reload
appears to fix it only because it returns the scroller to the top, where
anchoring is inert. All three ingredients are required — anchoring, a
`box-shadow` animation anywhere on the page (the rail's `rail-pulse` /
`rail-pulse-done` on needs-input and done panes), and content changing height
above the viewport of a scrolled container (attention-section rows coming and
going every poll). Measured 2026-10-01 on WebKit 22625.1.29 in an off-screen
WKWebView loading the real stylesheet, comparing `scrollTop` against the
overflow node's `scroll position` in `-[WKWebView _scrollingTreeAsText]`:
after ~45 add/remove cycles of one 25.5px row, layout sat at 321 and the
compositor at 347; a second scroller with no animation inside it drifted the
same way. With `overflow-anchor: none`, or with the pulse animating `opacity`
instead of `box-shadow`, the two stay equal. Page script cannot observe the
drift — `_scrollingTreeAsText` is the only readout. `diag/scroll-drift/run.sh`
is that comparison as a script: it prints LOCKED for the stylesheet as shipped,
and `--anchoring` forces anchoring back on, so DRIFT means the WebKit of the
day still has the bug and LOCKED means the `overflow-anchor` rule can go.

The shell otherwise stays minimal on purpose: single-instance, window-state
persistence, notification plugin available. Native badge + native
notifications routing from the JS side via `window.__TAURI__` is
the next layer up (`static/src/tauri.js`), additive to existing UI —
the dashboard keeps working unchanged in a regular browser.

## Notifications across version skew

The banners come from `static/src/notify/` and go through `src-tauri/src/notifications.rs`. The webview's JS is served by periscope, but the shell is whatever `.app` is installed, so either one can be the older side.

**The shell accepts anything.** Every request field has a default and tolerates `null`, so an old webview's `{title, body, target}` still parses.

**Clicks and presses go out on three events** (categories come in on a fourth, `periscope:notify-categories`, webview → Rust):
- A banner sent with a `kind` stores `{target, kind, data, quiet_when_focused}` in `userInfo`, and its click goes out on `periscope:notification-opened`. `feed.js` routes it by `data.open`: a pane, a URL, or the usage pill.
- A banner without a kind goes out on the legacy `periscope:notification-clicked` with the plain target, which `alertFeed.js` reveals.
- A press on a banner's button (2b) goes out on `periscope:notification-action` as `{target, kind, action, text, data}`; `notify/actions.js`'s `runAction` dispatches it.

**An old shell with new JS** loses:
- subtitles and threads;
- quiet-while-focused: every banner sounds;
- URL and usage clicks, which fall back to revealing the pane;
- the buttons (2b): it never registers the categories the webview sends, so banners arrive without them.

**A new shell with old JS** shows no buttons either: old JS's banners never name a category, and the shell never falls back to a banner's kind (2b's category ids reuse 2a kind names), so even a set a newer webview left registered shows no buttons. The shell treats buttons as data, so a new button needs new JS only, never a new `.app`.

**2b's PR 2 needs no shell rebuild.** It adds:
- five categories (`needs_permission`, `needs_dialog`, `question`, `ci_failed`, `ci_failed_fix`), whose buttons are Allow, Deny, Open, Reply and Ask agent to fix;
- the `notice` banner a failed press posts.

All of that is data the webview sends, so PR 1's `.app` covers it. The presses that make an agent act POST to `routes/pane_actions.py`, which re-checks the pane, so a stale banner's press can only reveal it.

So **install a rebuilt `.app` before deploying JS** that relies on new shell fields.

The design and the dedupe rules are in `docs/superpowers/specs/2026-10-21-native-notifications-design.md`; the categories and buttons are in `docs/superpowers/specs/2026-10-26-native-notifications-2b-design.md`.
