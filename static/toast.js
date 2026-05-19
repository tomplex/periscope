// Tiny toast surface — bottom-right stack, auto-dismissing. Use this
// instead of alert() for any feedback that shouldn't steal focus or
// interrupt typing. The browser's modal alert() is the wrong primitive
// for transient background-poll feedback; it stops every keystroke
// dead until the user clicks OK.

let container = null;

function ensureContainer() {
  if (container) return container;
  container = document.createElement("div");
  container.id = "toast-container";
  document.body.appendChild(container);
  return container;
}

export function showToast(message, kind = "info", ms = 4000) {
  const c = ensureContainer();
  const t = document.createElement("div");
  t.className = `toast toast-${kind}`;
  t.textContent = message;
  c.appendChild(t);
  // Two-step add: insert at translateY off-screen, then flip the show
  // class on the next frame so the transition actually runs.
  requestAnimationFrame(() => t.classList.add("toast-show"));
  setTimeout(() => {
    t.classList.remove("toast-show");
    setTimeout(() => t.remove(), 200);
  }, ms);
  return t;
}
