// LIFO Escape-stack hook — replaces overlay.js's pushEscape/popEscape.
//
// One global keydown listener (capture phase) drives a module-level stack;
// only the most-recently-pushed handler fires per Escape. This is what makes
// a dropdown opened over a modal close the dropdown first, then the modal,
// on successive Escapes — never closing all of them at once (which a
// per-component window listener would do). The stopPropagation keeps the
// Escape from also reaching deeper handlers in the same tick.
import { useEffect } from "preact/hooks";

const stack = [];

function onKey(e) {
  if (e.key === "Escape" && stack.length) {
    e.stopPropagation();
    stack[stack.length - 1](e);
  }
}

if (typeof window !== "undefined") window.addEventListener("keydown", onKey, true);

export function useEscape(handler, active = true) {
  useEffect(() => {
    if (!active) return;
    stack.push(handler);
    return () => {
      const i = stack.indexOf(handler);
      if (i >= 0) stack.splice(i, 1);
    };
  }, [handler, active]);
}
