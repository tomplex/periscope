// Preact entry point. Mounts the full dashboard into #app. Split is the only
// view (grid was retired).
import { render } from "preact";
import { Header } from "./chrome/Header.jsx";
import { installKeys } from "./keys.js";
import { startMemtest } from "./memtest.js";
import { DialogHost } from "./overlays/Dialog.jsx";
import { Overlays } from "./overlays/Overlays.jsx";
import { Toaster } from "./overlays/Toast.jsx";
import { getLastSelected, loadPrefs } from "./prefs.js";
import { Split } from "./split/Split.jsx";
import { railSelection } from "./store.js";
import { track } from "./track.js";

function App() {
  // Overlays are always mounted; each overlay manages its own open state.
  return (
    <>
      <Header />
      <Split />
      <Overlays />
      <Toaster />
      <DialogHost />
    </>
  );
}

async function boot() {
  // Prefs must load before first render: collapsed sessions, rail order,
  // commands, and last-selected all read from prefs.
  await loadPrefs();
  track("app.open");

  // Restore the persisted rail selection. last_selected is an OBJECT in prefs;
  // railSelection is a STRING highlight-key — deliberately different shapes.
  const sel = getLastSelected();
  if (sel?.kind === "pane") railSelection.value = `pane:${sel.pid}`;
  else if (sel?.kind === "review") railSelection.value = `review:${sel.worktree}`;

  render(<App />, document.getElementById("app"));

  // ⌘R / ⌘⇧R. Installed after mount so the first keypress can already see a
  // rendered selection.
  installKeys();

  // Memory-leak sweep driver; dormant unless static/memtest.json exists
  // (see memtest.js — one boot-time probe, nothing recurring in normal use).
  startMemtest();

  // PWA installability gate — the service worker is a no-op (see static/sw.js).
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
}

boot();
