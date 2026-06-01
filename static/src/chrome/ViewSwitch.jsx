// View switch — now 2-way (split / grid); stream is cut (see arch design).
// Writes the `view` signal and persists via prefs.setView. Ported from the
// #view-switch markup in index.html + applyView in app.js:302-324.
//
// Must-not-drop: an effect mirrors the `view` signal onto
// document.body.dataset.view — CSS keys visibility off that attribute
// (grid-only chrome hides in split, #split-view shows in split). The class
// names (.view-switch, .view-switch-btn, .active) + aria-selected are
// preserved verbatim.
import { useEffect } from "preact/hooks";
import { view } from "../store.js";
import * as prefs from "../prefs.js";

const VIEWS = [
  { key: "split", label: "▤ split", title: "split view — curated rail + persistent detail (Tab to toggle)" },
  { key: "grid", label: "▦ grid", title: "grid view (Tab to toggle)" },
];

// 2-way cycle. A persisted "stream" pref (legacy) falls back to split via the
// view signal's default, so this only ever sees split/grid.
export function nextView(current) {
  return current === "split" ? "grid" : "split";
}

export function ViewSwitch() {
  const current = view.value;

  // Mirror the view signal onto body[data-view] — the CSS contract. An effect
  // (not render) so we touch the DOM attribute exactly once per change.
  useEffect(() => {
    document.body.dataset.view = current;
  }, [current]);

  function select(v) {
    if (view.value === v) return; // no-op click on active
    view.value = v;
    prefs.setView(v);
  }

  return (
    <div class="view-switch" id="view-switch" role="tablist" aria-label="view">
      {VIEWS.map((vw) => {
        const isActive = vw.key === current;
        return (
          <button
            key={vw.key}
            class={`view-switch-btn${isActive ? " active" : ""}`}
            data-view={vw.key}
            role="tab"
            aria-selected={isActive ? "true" : "false"}
            title={vw.title}
            onClick={() => select(vw.key)}
          >
            {vw.label}
          </button>
        );
      })}
    </div>
  );
}
