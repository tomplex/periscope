// Toast surface — bottom-right stack, auto-dismissing. Use instead of
// alert() for transient feedback that shouldn't steal focus or interrupt
// typing (the browser's modal alert() stops every keystroke dead until the
// user clicks OK, and silently no-ops in WKWebView/Tauri).
//
// Ported from static/toast.js. The imperative DOM-building is replaced by a
// signal-backed <Toaster> rendered once in <App>; the public showToast()
// API and every CSS class (#toast-container, .toast, .toast-${kind},
// .toast-show) and timing (default 4000ms, two-frame show, 200ms removal)
// are preserved so call sites and styles.css are unchanged.
import { signal } from "@preact/signals";

// Each toast: { id, message, kind, ms, show }. `show` flips on the next
// frame so the CSS transition actually runs (two-step add, as in the
// vanilla version).
const toasts = signal([]);
let nextId = 1;

export function showToast(message, kind = "info", ms = 4000) {
  const id = nextId++;
  toasts.value = [...toasts.value, { id, message, kind, ms, show: false }];
  // Flip `show` on the next frame so the enter transition runs.
  requestAnimationFrame(() => {
    toasts.value = toasts.value.map((t) => (t.id === id ? { ...t, show: true } : t));
  });
  setTimeout(() => {
    // Begin exit transition, then remove after it completes (200ms).
    toasts.value = toasts.value.map((t) => (t.id === id ? { ...t, show: false } : t));
    setTimeout(() => {
      toasts.value = toasts.value.filter((t) => t.id !== id);
    }, 200);
  }, ms);
  return id;
}

export function Toaster() {
  // Read the signal so the host re-renders on every change.
  const list = toasts.value;
  return (
    <div id="toast-container">
      {list.map((t) => (
        <div key={t.id} class={`toast toast-${t.kind}${t.show ? " toast-show" : ""}`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}
