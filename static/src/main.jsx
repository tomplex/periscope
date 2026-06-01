// Preact entry point. Mounts behind the per-surface mount switch
// (window.__PREACT_SURFACES__, set in index.html before either app script
// runs). Task 1 is scaffold only: mount an empty placeholder so the build
// has a real entry and we can verify the bundle loads alongside the still-
// live vanilla dashboard. Later tasks mount the real <App> per surface.
import { render } from "preact";

function App() {
  return <div data-preact-root>periscope (preact scaffold)</div>;
}

render(<App />, document.getElementById("app"));
