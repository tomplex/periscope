// Global keybindings that can't live in one component because they dispatch on
// what is FOCUSED rather than what is mounted. Component-local bindings (⌘K in
// OpenOmnibox, ⌘F in TerminalSearch, ⌘/ in Header) stay where they are — this
// module is for keys whose meaning depends on the current selection.
//
// ⌘R is taken from the browser deliberately. Reloading the whole dashboard is
// the rare intent; refreshing the thing you are looking at is the common one,
// and the browser's default served the rare case. Full reload moves to ⌘⇧R.
import { requestTerminalReconcile } from "./terminal/terminalCore.js";
import { docRefreshNonce, paneActiveTab, railSelection } from "./store.js";

// Which pane the rail has selected, or null. railSelection is a string
// highlight-key ("pane:<pid>" | "review:<worktree>" | null) — only the pane
// form carries a pid.
export function selectedPid(selection) {
  if (typeof selection !== "string") return null;
  return selection.startsWith("pane:") ? selection.slice("pane:".length) : null;
}

// What ⌘R should act on, given the selection and the per-pane active-tab map.
// Pure so the dispatch table is testable without a DOM: returns "document"
// when a file tab is frontmost, else "terminal".
export function refreshTargetFor(selection, activeTabByPid) {
  const pid = selectedPid(selection);
  const tab = pid ? activeTabByPid?.[pid] : null;
  return typeof tab === "string" && tab.startsWith("file:") ? "document" : "terminal";
}

// True for the keydown that means "refresh what I'm looking at".
// Accepts ctrl as well as meta so a non-mac keyboard behaves the same.
export function isRefreshKey(e) {
  return (e.metaKey || e.ctrlKey) && (e.key === "r" || e.key === "R");
}

export function installKeys(target = window) {
  target.addEventListener("keydown", (e) => {
    if (!isRefreshKey(e)) return;
    // Always preventDefault once we've claimed the chord — otherwise the
    // browser reloads out from under whichever branch we take.
    e.preventDefault();

    if (e.shiftKey) {
      location.reload();
      return;
    }

    if (refreshTargetFor(railSelection.value, paneActiveTab.value) === "document") {
      // One global bump rather than per-tab keying: only the visible tab
      // fetches (inactive PreviewTabs early-return), and an inactive tab
      // re-reads on its next activation anyway.
      docRefreshNonce.value = docRefreshNonce.value + 1;
      return;
    }
    requestTerminalReconcile();
  });
}
