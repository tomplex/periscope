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
  if (SURFACES.has("terminal")) {
    return (
      <>
        <TerminalProbe />
        <Toaster />
        <DialogHost />
      </>
    );
  }
  return (
    <>
      <div data-preact-root>periscope (preact scaffold)</div>
      <Toaster />
      <DialogHost />
    </>
  );
}

render(<App />, document.getElementById("app"));
