// Preact entry point. Mounts the full dashboard into #app. Split is the only
// view (grid was retired).
import { render } from "preact";
import { Toaster } from "./overlays/Toast.jsx";
import { DialogHost } from "./overlays/Dialog.jsx";
import { Header } from "./chrome/Header.jsx";
import { Modal } from "./modal/Modal.jsx";
import { Split } from "./split/Split.jsx";
import { Overlays } from "./overlays/Overlays.jsx";
import { loadPrefs, getLastSelected } from "./prefs.js";
import { railSelection } from "./store.js";

function App() {
  // Modal + Overlays are always mounted (the modal shows itself when
  // modalTarget is set; overlays manage their own open state).
  return (
    <>
      <Header />
      <Split />
      <Modal />
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

  // Restore the persisted rail selection. last_selected is an OBJECT in prefs;
  // railSelection is a STRING highlight-key — deliberately different shapes.
  const sel = getLastSelected();
  if (sel?.kind === "pane") railSelection.value = `pane:${sel.pid}`;
  else if (sel?.kind === "review") railSelection.value = `review:${sel.worktree}`;

  render(<App />, document.getElementById("app"));

  // PWA installability gate — the service worker is a no-op (see static/sw.js).
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
}

boot();
