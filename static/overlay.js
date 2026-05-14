// Lightweight shared overlay primitives. Multiple modals (pane modal,
// commands modal) need an Escape handler — without a shared registry they'd
// fight each other (whoever attached first or last would close every modal).
//
// Each modal registers an `onEscape` callback while open and unregisters on
// close. Only the most-recently-opened modal's callback fires per Escape.

const stack = [];

export function pushEscape(onEscape) {
  stack.push(onEscape);
}

export function popEscape(onEscape) {
  const i = stack.lastIndexOf(onEscape);
  if (i >= 0) stack.splice(i, 1);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!stack.length) return;
  const top = stack[stack.length - 1];
  top(e);
});
