// Shared popover-closing wiring: Escape (via the LIFO useEscape stack) and a
// document click outside `ref` while `open`. Four header popovers
// (ResourcePill, Header's Dropdown, FilterBar, UpdatePill) each repeated this;
// every caller keeps its own open-state and markup.
import { useEffect } from "preact/hooks";
import { useEscape } from "./useEscape.js";

export function usePopoverDismiss(ref, open, close) {
  useEscape(close, open);
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) close();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open, close]);
}
