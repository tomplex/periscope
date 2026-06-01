// Preact entry point. Mounts behind the per-surface mount switch
// (window.__PREACT_SURFACES__, set in index.html before either app script
// runs). The real per-surface <App> wiring lands in later tasks; for now
// this mounts the shared overlay hosts (toast + dialog) so the primitives
// are live and exercised, plus the scaffold placeholder. The vanilla
// dashboard remains the fallback for every un-Preact'd surface.
import { render } from "preact";
import { Toaster } from "./overlays/Toast.jsx";
import { DialogHost } from "./overlays/Dialog.jsx";
import { Terminal } from "./terminal/Terminal.jsx";
import { Header } from "./chrome/Header.jsx";
import { Grid } from "./grid/Grid.jsx";
import { Modal } from "./modal/Modal.jsx";
import { Split } from "./split/Split.jsx";
import { Overlays } from "./overlays/Overlays.jsx";
import { loadPrefs, getView, getLastSelected } from "./prefs.js";
import { view, railSelection } from "./store.js";

const SURFACES = window.__PREACT_SURFACES__ || new Set();

// `?preact=terminal&target=session:index` mounts a standalone <Terminal> for
// isolation-testing the riskiest unit (Task 3) before any consumer wires it in.
// Keyed on the target so re-selecting the same pane reuses the instance.
function TerminalProbe() {
  const target = new URLSearchParams(location.search).get("target") || "";
  if (!target) {
    return <div data-preact-root>preact: pass ?target=session:index to mount a terminal</div>;
  }
  return (
    <div style="position:fixed;inset:0;display:flex;flex-direction:column;background:#282c34">
      <Terminal key={target} target={target} />
    </div>
  );
}

function App() {
  // The terminal isolation probe is a standalone, full-bleed surface (Task 3).
  if (SURFACES.has("terminal")) {
    return (
      <>
        <TerminalProbe />
        <Toaster />
        <DialogHost />
      </>
    );
  }
  // Otherwise compose the claimed surfaces additively. Each is independent and
  // mounts only when its flag is present; the vanilla path skips its own
  // equivalent for any claimed surface (app.js consults __PREACT_SURFACES__).
  const anySurface =
    SURFACES.has("chrome") || SURFACES.has("grid") || SURFACES.has("modal") ||
    SURFACES.has("split") || SURFACES.has("overlays");
  return (
    <>
      {SURFACES.has("chrome") && <Header />}
      {SURFACES.has("grid") && <Grid />}
      {SURFACES.has("split") && <Split />}
      {SURFACES.has("modal") && <Modal />}
      {SURFACES.has("overlays") && <Overlays />}
      {!anySurface && <div data-preact-root>periscope (preact scaffold)</div>}
      <Toaster />
      <DialogHost />
    </>
  );
}

async function boot() {
  // The chrome surface needs prefs loaded before first render so the view
  // switch reflects the persisted view (and the body[data-view] mirror effect
  // lands on the right value). Hide the vanilla <header> so the Preact one
  // doesn't double up — the vanilla path already skips its chrome wiring when
  // this surface is claimed (app.js consults __PREACT_SURFACES__).
  if (SURFACES.has("chrome")) {
    await loadPrefs();
    const v = getView();
    view.value = v === "stream" ? "split" : v; // stream is cut → fall back to split
    const vanillaHeader = document.querySelector("header.periscope-header");
    if (vanillaHeader) vanillaHeader.style.display = "none";
  }
  // The grid surface needs prefs loaded before first render so collapsed /
  // session_order / commands are honored. Hide the vanilla <main id="grid">
  // (the vanilla path already skips initGrid when this surface is claimed) so
  // it doesn't sit empty beside the Preact grid; the Preact <Grid> renders its
  // own <main>. The grid drives the still-vanilla modal (Task 6) through a
  // window bridge that vanilla app.js installs.
  if (SURFACES.has("grid")) {
    await loadPrefs();
    const vanillaGrid = document.getElementById("grid");
    if (vanillaGrid) vanillaGrid.style.display = "none";
  }
  // The modal surface reads prefs (notes/tags annotations). Load before first
  // render; the vanilla path already skips initModal when "modal" is claimed,
  // and the static `#modal` div stays `.hidden`, so the Preact modal (which
  // renders its own `#modal` inside #app) is the only live one.
  if (SURFACES.has("modal")) {
    await loadPrefs();
    // Remove the static `#modal` from index.html so it can't collide (duplicate
    // id) with the Preact modal, which renders its own `#modal` inside #app.
    document.getElementById("modal")?.remove();
  }
  // The split surface needs prefs loaded before first render (rail order /
  // collapse / last_selected). Restore the persisted selection (an OBJECT) into
  // the railSelection signal (a STRING highlight-key) — the two shapes are
  // deliberately different and must not be crossed. Remove the static
  // #split-view from index.html so it can't collide (duplicate ids #rail /
  // #detail) with the Preact split, which renders its own inside #app.
  if (SURFACES.has("split")) {
    await loadPrefs();
    const sel = getLastSelected();
    if (sel?.kind === "pane") railSelection.value = `pane:${sel.pid}`;
    else if (sel?.kind === "review") railSelection.value = `review:${sel.worktree}`;
    document.getElementById("split-view")?.remove();
  }
  // The overlays surface (Task 8: secondary modals + alerts + tauri + sw)
  // reads prefs (alerts_open, commands, annotations on modal-open via the
  // alert→openModal path). Load before first render. Remove the static
  // equivalents from index.html so the Preact ones — which render their own
  // nodes carrying the SAME ids (#alerts-rail, #commands-modal, …) — can't
  // collide. The vanilla path still owns these while only overlays is claimed,
  // so removing them is only safe once we mount the Preact replacements; that
  // is exactly the order here (remove, then render).
  if (SURFACES.has("overlays")) {
    await loadPrefs();
    for (const id of [
      "alerts-rail",
      "commands-modal",
      "new-project-modal",
      "review-pr-modal",
      "cleanup-modal",
      "settings-modal",
      "open-picker-modal",
      "launcher-modal",
    ]) {
      document.getElementById(id)?.remove();
    }
  }
  render(<App />, document.getElementById("app"));
}

boot();
