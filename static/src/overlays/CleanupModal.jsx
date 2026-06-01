// Cleanup modal. Loads candidates from /api/cleanup/candidates, renders a
// checklist with signal badges, submits selected to /api/cleanup/archive.
// Ported from static/cleanup-modal.js. The innerHTML rendering becomes JSX;
// the dirty-row "not auto-selected" rule, the live submit-count, and the
// partial-failure refresh path are preserved.
//
// CSS contract preserved: #cleanup-modal / .cleanup-modal-overlay / -card /
// -head / -sub / -controls / -list / -error / -actions, .cleanup-row /
// -dirty / -check / -body / -title / -meta / -signals, .cleanup-badge /
// -badge-${kind}, .cleanup-dirty / .cleanup-loading / .cleanup-empty /
// .cleanup-delete-branches, body.cleanup-modal-open. Escape closes via the
// shared LIFO useEscape hook.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { modalRequest } from "./modalRequest.js";

const open = signal(false);

export function openCleanupModal() {
  open.value = true;
}
function close() {
  open.value = false;
}

export function CleanupModal() {
  useEscape(close, open.value);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [candidates, setCandidates] = useState([]);
  // Per-candidate checked state, keyed by index. Dirty rows seed false.
  const [checked, setChecked] = useState({});
  const [deleteBranches, setDeleteBranches] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const btn = document.getElementById("cleanup-btn");
    if (btn) btn.addEventListener("click", openCleanupModal);
    return () => { if (btn) btn.removeEventListener("click", openCleanupModal); };
  }, []);

  async function refresh() {
    setError("");
    setLoading(true);
    const { data, error: err } = await modalRequest("load candidates", "/api/cleanup/candidates");
    setLoading(false);
    if (err) { setError(err); setCandidates([]); return; }
    const cands = data.candidates || [];
    setCandidates(cands);
    // Healthy candidates checked, dirty ones unchecked.
    const seed = {};
    cands.forEach((c, i) => { seed[i] = !c.dirty; });
    setChecked(seed);
  }

  useEffect(() => {
    if (!open.value) return;
    setDeleteBranches(false);
    refresh();
  }, [open.value]);

  const selectedCount = Object.values(checked).filter(Boolean).length;

  async function handleSubmit() {
    setError("");
    const selected = [];
    candidates.forEach((c, i) => {
      if (checked[i]) selected.push({ pinned_dir: c.pinned_dir, delete_branch: deleteBranches });
    });
    if (selected.length === 0) return;
    setSubmitting(true);
    const { data: result, error: err } = await modalRequest("archive", "/api/cleanup/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates: selected }),
    });
    setSubmitting(false);
    if (err) { setError(err); return; }
    if (result.failed && result.failed.length > 0) {
      setError(
        `Archived ${result.archived.length}, ${result.failed.length} failed: ` +
        result.failed.map((f) => `${f.pinned_dir.split("/").pop()}: ${f.error}`).join("; ")
      );
      await refresh();
      return;
    }
    close();
  }

  if (!open.value) return null;

  return (
    <div
      id="cleanup-modal"
      class="cleanup-modal-overlay"
      onClick={(e) => { if (e.target.id === "cleanup-modal") close(); }}
    >
      <div class="cleanup-modal-card">
        <header class="cleanup-modal-head">
          <h2>🧹 cleanup</h2>
          <button id="cleanup-modal-close" title="close" onClick={close}>×</button>
        </header>
        <p class="cleanup-modal-sub">
          Worktrees with merged/closed PRs, deleted remote branches, or idle activity. Dirty worktrees are NOT auto-selected.
        </p>
        <div id="cleanup-modal-controls">
          <label class="cleanup-delete-branches">
            <input
              type="checkbox"
              id="cleanup-delete-branches"
              checked={deleteBranches}
              onChange={(e) => setDeleteBranches(e.currentTarget.checked)}
            /> also delete local branches
          </label>
        </div>
        <div id="cleanup-modal-list">
          {loading ? (
            <div class="cleanup-loading">Walking worktrees…</div>
          ) : candidates.length === 0 ? (
            <div class="cleanup-empty">No cleanup candidates. 🎉</div>
          ) : (
            candidates.map((c, i) => {
              const name = c.project_name || `(untracked: ${c.pinned_dir.split("/").pop()})`;
              return (
                <label key={c.pinned_dir} class={c.dirty ? "cleanup-row cleanup-row-dirty" : "cleanup-row"} data-i={i}>
                  <input
                    type="checkbox"
                    class="cleanup-row-check"
                    checked={!!checked[i]}
                    onChange={(e) => setChecked((prev) => ({ ...prev, [i]: e.currentTarget.checked }))}
                  />
                  <div class="cleanup-row-body">
                    <div class="cleanup-row-title">{name}</div>
                    <div class="cleanup-row-meta">
                      {c.branch} {c.dirty ? <span class="cleanup-dirty">⚠ dirty</span> : null}
                    </div>
                    <div class="cleanup-row-signals">
                      {(c.signals || []).map((s, si) => (
                        <span key={si} class={`cleanup-badge cleanup-badge-${s.kind}`}>{s.label}</span>
                      ))}
                    </div>
                  </div>
                </label>
              );
            })
          )}
        </div>
        <div id="cleanup-modal-error" class="cleanup-modal-error" hidden={!error}>{error}</div>
        <div class="cleanup-modal-actions">
          <button type="button" id="cleanup-cancel" onClick={close}>cancel</button>
          <button
            type="button"
            id="cleanup-submit"
            disabled={selectedCount === 0 || submitting}
            onClick={handleSubmit}
          >
            archive selected ({selectedCount})
          </button>
        </div>
      </div>
    </div>
  );
}
