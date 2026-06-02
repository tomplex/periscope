// Inline-error fetch helper for the secondary modals. Replaces the
// `request` method of the vanilla createModalShell() (static/modal-shell.js):
// fetch a JSON endpoint, surfacing failures inline in the modal's own error
// element rather than via toast (these modals show their error in-card). The
// open/close + body-class half of createModalShell is now each modal's own
// open signal + useEscape, so only the request half survives here.
//
// Returns the parsed body on success, or null on failure (HTTP error,
// network error, or unparseable body) so callers early-return on null.
// Routes report errors as HTTPException → `{detail}`.
export async function modalRequest(label, path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    return { error: `${label} failed: ${err.message}` };
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    return { error: data.detail || `${label} failed: HTTP ${res.status}` };
  }
  return { data };
}
