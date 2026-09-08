// Inline-error fetch helper for the secondary modals. Replaces the
// `request` method of the vanilla createModalShell() (static/modal-shell.js):
// fetch a JSON endpoint, surfacing failures inline in the modal's own error
// element rather than via toast (these modals show their error in-card). The
// open/close + body-class half of createModalShell is now each modal's own
// open signal + useEscape, so only the request half survives here.
//
// Returns {data} on success, or {error, reason, status?} on failure (status
// only for an HTTP error, not a network one) so callers early-return on a
// missing data. Routes report errors as HTTPException → `{detail}`.
export async function modalRequest(label, path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    return { error: `${label} failed: ${err.message}`, reason: err.message };
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    // `status` rides along so a caller can treat one code specially (Rail's
    // move-account turns a 409 into a confirm); `reason` is the bare cause
    // (no `${label} failed:` prefix) so a caller can match on it without
    // re-parsing `error`. Every existing caller reads only `.error` / `.data`.
    const reason = data.detail || `HTTP ${res.status}`;
    return { error: data.detail || `${label} failed: HTTP ${res.status}`, reason, status: res.status };
  }
  return { data };
}
