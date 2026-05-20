// Browser-portable confirm() replacement using the native <dialog>
// element. We need this because WKWebView (and therefore the Tauri
// shell) silently returns false from window.confirm() without showing
// any UI — so the kill / send-bulk / etc. buttons appeared to do
// nothing in the .app. The HTML <dialog> path renders identically in
// Chrome, Safari, and the Tauri webview.
//
// Async-only. Existing call sites that did `if (!confirm(...)) return;`
// need to become `if (!await confirmDialog(...)) return;` inside an
// async function — both grid.js handlers and app.js send-bulk already
// are async, so it's a one-character ripple.

import { escapeHtml } from "./util.js";

export function confirmDialog(message, opts = {}) {
  const okLabel = opts.okLabel || "OK";
  const cancelLabel = opts.cancelLabel || "Cancel";
  // `danger: true` paints the OK button red — used for destructive ops
  // like kill where the default focus is also moved to Cancel.
  const danger = !!opts.danger;

  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "confirm-dialog";
    dlg.innerHTML = `
      <p class="confirm-dialog-msg">${escapeHtml(message).replace(/\n/g, "<br>")}</p>
      <div class="confirm-dialog-actions">
        <button type="button" class="confirm-dialog-cancel">${escapeHtml(cancelLabel)}</button>
        <button type="button" class="confirm-dialog-ok${danger ? " is-danger" : ""}">${escapeHtml(okLabel)}</button>
      </div>
    `;
    document.body.appendChild(dlg);

    // Guard against double-resolve: clicking OK calls dlg.close() which
    // fires the 'close' event, which would otherwise re-trigger cleanup.
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      try { dlg.close(); } catch (_) {}
      dlg.remove();
      resolve(result);
    };

    dlg.querySelector(".confirm-dialog-cancel").addEventListener("click", () => finish(false));
    dlg.querySelector(".confirm-dialog-ok").addEventListener("click", () => finish(true));
    // Escape key triggers <dialog>'s default close behavior; treat as cancel.
    dlg.addEventListener("close", () => finish(false));
    // Click on the backdrop (the dialog element itself, outside its content
    // box) also cancels — same affordance as most native modals.
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) finish(false);
    });

    dlg.showModal();
    // For destructive prompts, focus Cancel so accidental Enter / Space
    // doesn't trigger the action. Match macOS native NSAlert defaults.
    const initialFocus = danger
      ? dlg.querySelector(".confirm-dialog-cancel")
      : dlg.querySelector(".confirm-dialog-ok");
    initialFocus.focus();
  });
}
