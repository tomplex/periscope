// The split-view surface: the #split-view container holding <Rail> (left) and
// <Detail> (right). Renders inside #app (the Preact root). The static
// #split-view in index.html is hidden by main.jsx's boot when this surface is
// claimed, so there's no duplicate-id collision and the Preact one is the only
// live split view.
//
// When mounted standalone behind ?preact=split (no chrome surface), an effect
// sets body[data-view]="split" so the CSS (#split-view visibility, plus the
// shared body[data-view] contract — convention #3) shows it. When chrome is
// also Preact-owned its ViewSwitch already mirrors the `view` signal onto
// body.dataset.view; this effect is idempotent with that.
import { useEffect } from "preact/hooks";
import { Rail } from "./Rail.jsx";
import { Detail } from "./Detail.jsx";
import { startPolling } from "../grid/poll.js";

export function Split() {
  // Own the single /api/state poll loop that feeds the `windows` signal — the
  // Rail + Detail render off it. startPolling is double-start-guarded, so when
  // the grid surface is also Preact-owned this is a no-op (Grid already started
  // it); when split is mounted alone it's the only poller writing the signals.
  useEffect(() => startPolling(), []);

  useEffect(() => {
    // Mirror the view onto the body attribute (CSS keys #split-view + grid
    // visibility off it). Only force it when nothing else owns it (standalone
    // split mount); harmless to re-assert.
    if (!document.body.dataset.view) document.body.dataset.view = "split";
  }, []);

  return (
    <div id="split-view">
      <Rail />
      <Detail />
    </div>
  );
}
