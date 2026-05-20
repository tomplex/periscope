// Browser-portable replacements for window.confirm() / prompt() /
// alert() using the native <dialog> element. We need these because
// WKWebView (and therefore the Tauri shell) silently no-ops all three
// without showing any UI — so the kill / send-bulk / new-session /
// API-error paths appeared to do nothing in the .app. The HTML
// <dialog> path renders identically in Chrome, Safari, and the
// Tauri webview.
//
// All three are async (the native ones were sync, but you can't get
// blocking behavior in JS without busy-waiting). Call sites need
// `await`. Returns:
//   confirmDialog → boolean (false on cancel / backdrop / Esc)
//   promptDialog  → string | null (null on cancel)
//   alertDialog   → void (resolves on dismiss)

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

export function promptDialog(label, opts = {}) {
  const defaultValue = opts.defaultValue || "";
  const placeholder = opts.placeholder || "";
  const okLabel = opts.okLabel || "OK";
  const cancelLabel = opts.cancelLabel || "Cancel";

  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "confirm-dialog prompt-dialog";
    dlg.innerHTML = `
      <p class="confirm-dialog-msg">${escapeHtml(label).replace(/\n/g, "<br>")}</p>
      <input type="text" class="prompt-dialog-input" autocomplete="off" spellcheck="false">
      <div class="confirm-dialog-actions">
        <button type="button" class="confirm-dialog-cancel">${escapeHtml(cancelLabel)}</button>
        <button type="button" class="confirm-dialog-ok">${escapeHtml(okLabel)}</button>
      </div>
    `;
    document.body.appendChild(dlg);

    const input = dlg.querySelector(".prompt-dialog-input");
    input.value = defaultValue;
    if (placeholder) input.placeholder = placeholder;

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      try { dlg.close(); } catch (_) {}
      dlg.remove();
      resolve(result);
    };

    dlg.querySelector(".confirm-dialog-cancel").addEventListener("click", () => finish(null));
    dlg.querySelector(".confirm-dialog-ok").addEventListener("click", () => finish(input.value));
    dlg.addEventListener("close", () => finish(null));
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) finish(null);
    });
    // Enter submits, Esc cancels (Esc is the <dialog>'s default close).
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(input.value); }
    });

    dlg.showModal();
    input.focus();
    input.select();
  });
}

export function alertDialog(message, opts = {}) {
  const okLabel = opts.okLabel || "OK";
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "confirm-dialog";
    dlg.innerHTML = `
      <p class="confirm-dialog-msg">${escapeHtml(message).replace(/\n/g, "<br>")}</p>
      <div class="confirm-dialog-actions">
        <button type="button" class="confirm-dialog-ok">${escapeHtml(okLabel)}</button>
      </div>
    `;
    document.body.appendChild(dlg);

    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      try { dlg.close(); } catch (_) {}
      dlg.remove();
      resolve();
    };

    dlg.querySelector(".confirm-dialog-ok").addEventListener("click", finish);
    dlg.addEventListener("close", finish);
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) finish();
    });

    dlg.showModal();
    dlg.querySelector(".confirm-dialog-ok").focus();
  });
}
