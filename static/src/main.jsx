// Preact entry point. Mounts behind the per-surface mount switch
// (window.__PREACT_SURFACES__, set in index.html before either app script
// runs). The real per-surface <App> wiring lands in later tasks; for now
// this mounts the shared overlay hosts (toast + dialog) so the primitives
// are live and exercised, plus the scaffold placeholder. The vanilla
// dashboard remains the fallback for every un-Preact'd surface.
import { render } from "preact";
import { Toaster } from "./overlays/Toast.jsx";
import { DialogHost } from "./overlays/Dialog.jsx";

function App() {
  return (
    <>
      <div data-preact-root>periscope (preact scaffold)</div>
      <Toaster />
      <DialogHost />
    </>
  );
}

render(<App />, document.getElementById("app"));
