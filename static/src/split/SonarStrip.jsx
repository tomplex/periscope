// The sonar strip at the top of the rail: one line per track, one blip per
// pane, so every pane's state is on screen however long the tree below gets.
// The blip's shape says the state as well as its colour (attention.paneBlip),
// so it still reads for colour-blind eyes. Clicking a blip opens its pane;
// clicking a track's name shows only that track in the tree.
import { paneLabel, relTime, waitLabel } from "../util.js";

const LEGEND = ["asks", "broken", "done", "working", "idle", "shell"];

// What a blip says to a screen reader and in its tooltip.
function blipLabel(w, blip) {
  const name = paneLabel(w);
  if (blip === "asks") return `${name}: ${w.state === "needs-input" ? waitLabel(w.waiting_for) : "asked you something"}`;
  if (blip === "broken") return `${name}: ${w.api_error ? "API error" : "CI failing"}`;
  if (blip === "done" || blip === "done-old") return `${name}: done ${relTime(w.completed_at)} ago`;
  if (blip === "idle" && w.completed_at) return `${name}: idle ${relTime(w.completed_at)}`;
  return `${name}: ${blip}`;
}

// `tracks`: [{ id, label, panes: [w] }] in the tree's order. `blipOf(w)` is
// the rail's one paneBlip call, so a blip and its row's stripe always agree.
export function SonarStrip({ tracks, blipOf, selectedKey, scope, onScope, onSelect }) {
  return (
    <div class="sonar" role="group" aria-label="every pane, by track">
      {tracks.map(({ id, label, panes }) => {
        const scoped = scope === id;
        return (
          <div class="sonar-row" key={id}>
            <button
              class={`sonar-track${scoped ? " is-active" : ""}`}
              aria-pressed={scoped}
              title={scoped ? "show every track below" : `show only ${label} below`}
              onClick={() => onScope(scoped ? "all" : id)}
            >{label}</button>
            <span class="sonar-blips">
              {panes.map((w) => {
                const k = `pane:${w.pid}`;
                const blip = blipOf(w);
                const said = blipLabel(w, blip);
                return (
                  <button
                    key={k}
                    class={`blip blip-${blip}${k === selectedKey ? " is-selected" : ""}`}
                    aria-label={said}
                    aria-current={k === selectedKey ? "true" : undefined}
                    title={said}
                    onClick={() => onSelect(k)}
                  ></button>
                );
              })}
            </span>
            <span class="sonar-count">{panes.length}</span>
          </div>
        );
      })}
      <div class="sonar-legend" aria-hidden="true">
        {LEGEND.map((blip) => (
          <span key={blip}><span class={`blip blip-${blip}`}></span>{blip}</span>
        ))}
      </div>
    </div>
  );
}
