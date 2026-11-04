// The notify feed's dedupe state across reloads and webview recycles.
// recycle.rs rebuilds the webview once you've left periscope or been idle a
// while, which is exactly when banners matter; a fresh sentinel would
// swallow whatever arrived across the rebuild. So the state is saved after
// every pass and resumed when young enough. Older than RESUME_MS (a reload
// after a long absence), it's dropped and the feed reseeds, so old news
// never replays.

export const STORAGE_KEY = "periscope.notify.v1";
export const RESUME_MS = 15 * 60 * 1000;

// -> { state, held, heldSince } or null. The held burst rides along: its
// events are already in `seen`, so a recycle inside the window would otherwise
// drop that summary for good. Resumed overdue, the first pass flushes it.
export function resumeState(storage, nowMs) {
  try {
    const saved = JSON.parse(storage?.getItem(STORAGE_KEY) || "null");
    if (!saved?.state || !(saved.at <= nowMs) || nowMs - saved.at > RESUME_MS) return null;
    const held = Array.isArray(saved.held) ? saved.held : [];
    return { state: saved.state, held, heldSince: held.length ? saved.heldSince ?? saved.at : null };
  } catch {
    return null;
  }
}

export function saveState(storage, state, nowMs, held = [], heldSince = null) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify({ at: nowMs, state, held, heldSince }));
  } catch {
    // Private mode or a full quota: the next boot just reseeds.
  }
}
