# Tauri shell (`src-tauri/`)

Optional native `.app` wrapper for periscope, so it shows up as its
own entry in Cmd-Tab / Dock instead of living inside a browser tab.
The shell is just a Tauri 2 window that loads `http://127.0.0.1:8765`
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
loads `localhost:8765`, so editing JS/CSS and reloading the window
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
(default 1.0) AND system input has been idle `PERISCOPE_RECYCLE_IDLE_S`
(default 300s) — plus a manual View → Recycle Webview item. Two hard-won
invariants: the rebuild must happen on a LATER tick than destroy() (same-tick
rebuild exits the whole app; an ExitRequested veto in main.rs covers the
windowless gap), and the displaced WebContent must be SIGKILLed after a grace
period (WebKit parks it in its process cache at full leaked size instead of
exiting — pid-identity-checked via proc_pidpath before the kill). Actions log
to `~/.config/periscope/shell.log`. Test by lowering the thresholds via
`launchctl setenv` (GUI apps don't inherit shell env) and watching that log.

The shell otherwise stays minimal on purpose: single-instance, window-state
persistence, notification plugin available. Native badge + native
notifications routing from the JS side via `window.__TAURI__` is
the next layer up (`static/src/tauri.js`), additive to existing UI —
the dashboard keeps working unchanged in a regular browser.
