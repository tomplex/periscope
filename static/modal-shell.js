// Shared modal lifecycle. Each of the small standalone modals
// (new-project, review-pr, settings, cleanup) composes one createModalShell()
// instead of re-implementing the open/close lifecycle, error display, and
// fetch-with-inline-error handling.

import { pushEscape, popEscape } from './overlay.js';

export function createModalShell({ modal, bodyClass, errorEl }) {
  let isOpen = false;

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = "";
  }

  function open() {
    // Returns false when the modal is already open so callers early-return.
    if (isOpen) return false;
    isOpen = true;
    clearError();
    modal.classList.remove("hidden");
    document.body.classList.add(bodyClass);
    pushEscape(close);
    return true;
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    modal.classList.add("hidden");
    document.body.classList.remove(bodyClass);
    popEscape(close);
  }

  // Fetch a JSON endpoint, surfacing failures inline in this modal's error
  // element. Returns the parsed body on success, or null on failure (HTTP
  // error, network error, or unparseable body) so callers early-return on
  // null. Routes report errors as HTTPException → `{detail}`.
  async function request(label, path, opts = {}) {
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      showError(`${label} failed: ${err.message}`);
      return null;
    }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      showError(data.detail || `${label} failed: HTTP ${res.status}`);
      return null;
    }
    return data;
  }

  return {
    open, close, showError, clearError, request,
    get isOpen() { return isOpen; },
  };
}
