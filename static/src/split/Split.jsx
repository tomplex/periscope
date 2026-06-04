// The split-view surface: the #split-view container holding <Rail> (left) and
// <Detail> (right). This is the only view (grid retired).
import { useEffect } from "preact/hooks";
import { Rail } from "./Rail.jsx";
import { Detail } from "./Detail.jsx";
import { startPolling } from "../poll.js";
import { startAlertFeed } from "./alertFeed.js";

export function Split() {
  // Own the single /api/state poll loop that feeds the `windows` signal — the
  // Rail + Detail render off it. startAlertFeed similarly owns the alert poll.
  useEffect(() => { startPolling(); startAlertFeed(); }, []);

  useEffect(() => {
    // Mirror the view onto the body attribute (legacy CSS still keys some
    // selectors off body[data-view]="split"). One-shot at mount.
    if (!document.body.dataset.view) document.body.dataset.view = "split";
  }, []);

  return (
    <div id="split-view">
      <Rail />
      <Detail />
    </div>
  );
}
