// Settings modal: GET /api/settings on open, PATCH /api/settings on save.
// Ported from static/settings-modal.js. The innerHTML override rows become
// JSX; overrides are held in component state (add/remove/edit) and collected
// on submit. The positive-integer validation on idle-days is preserved.
//
// CSS contract preserved: #settings-modal / .settings-modal-overlay / -card /
// -head / -error / -actions, .settings-overrides-row / -repo / -layout /
// -remove / -empty / -hint, body.settings-modal-open. Escape closes via the
// shared LIFO useEscape hook.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { modalRequest } from "./modalRequest.js";

const open = signal(false);

export function openSettingsModal() {
  open.value = true;
}
function close() {
  open.value = false;
}

export function SettingsModal() {
  useEscape(close, open.value);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [idleDays, setIdleDays] = useState(14);
  const [layoutDefault, setLayoutDefault] = useState("sibling");
  // overrides: array of { repo, layout } so editing/removing is stable.
  const [overrides, setOverrides] = useState([]);

  useEffect(() => {
    const btn = document.getElementById("settings-btn");
    if (btn) btn.addEventListener("click", openSettingsModal);
    return () => { if (btn) btn.removeEventListener("click", openSettingsModal); };
  }, []);

  useEffect(() => {
    if (!open.value) return;
    setError("");
    (async () => {
      const { data: body, error: err } = await modalRequest("load settings", "/api/settings");
      if (err) { setError(err); return; }
      const s = body.settings || {};
      setIdleDays(s.cleanup_idle_days ?? 14);
      setLayoutDefault(s.worktree_layout_default ?? "sibling");
      setOverrides(Object.entries(s.worktree_layout_overrides || {}).map(([repo, layout]) => ({ repo, layout })));
    })();
  }, [open.value]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    const idle = parseInt(idleDays, 10);
    if (!idle || idle < 1) { setError("cleanup idle days must be a positive integer"); return; }
    const overridesMap = {};
    for (const { repo, layout } of overrides) {
      if (repo && layout) overridesMap[repo] = layout;
    }
    const patch = {
      cleanup_idle_days: idle,
      worktree_layout_default: layoutDefault,
      worktree_layout_overrides: overridesMap,
    };
    setSubmitting(true);
    const { error: err } = await modalRequest("save settings", "/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    setSubmitting(false);
    if (err) { setError(err); return; }
    close();
  }

  if (!open.value) return null;

  return (
    <div
      id="settings-modal"
      class="settings-modal-overlay"
      onClick={(e) => { if (e.target.id === "settings-modal") close(); }}
    >
      <div class="settings-modal-card">
        <header class="settings-modal-head">
          <h2>🛠 settings</h2>
          <button id="settings-modal-close" title="close" onClick={close}>×</button>
        </header>
        <form id="settings-form" onSubmit={handleSubmit}>
          <label>
            Cleanup idle threshold (days)
            <input
              id="settings-cleanup-idle-days"
              type="number"
              min="1"
              value={idleDays}
              onInput={(e) => setIdleDays(e.currentTarget.value)}
            />
          </label>
          <label>
            Default worktree layout
            <select
              id="settings-worktree-default"
              value={layoutDefault}
              onChange={(e) => setLayoutDefault(e.currentTarget.value)}
            >
              <option value="sibling">sibling — ~/dev/worktrees/&lt;repo&gt;/&lt;branch&gt;</option>
              <option value="inline">inline — &lt;repo&gt;/.worktrees/&lt;branch&gt;</option>
            </select>
          </label>
          <fieldset>
            <legend>Per-repo overrides</legend>
            <div id="settings-overrides-list">
              {overrides.length === 0 ? (
                <div class="settings-overrides-empty">(none yet)</div>
              ) : (
                overrides.map((o, i) => (
                  <div key={o.repo} class="settings-overrides-row" data-repo={o.repo}>
                    <span class="settings-overrides-repo">{o.repo}</span>
                    <select
                      class="settings-overrides-layout"
                      value={o.layout}
                      onChange={(e) => {
                        const v = e.currentTarget.value;
                        setOverrides((prev) => prev.map((x, xi) => (xi === i ? { ...x, layout: v } : x)));
                      }}
                    >
                      <option value="sibling">sibling</option>
                      <option value="inline">inline</option>
                    </select>
                    <button
                      type="button"
                      class="settings-overrides-remove"
                      title="remove override"
                      onClick={() => setOverrides((prev) => prev.filter((_, xi) => xi !== i))}
                    >×</button>
                  </div>
                ))
              )}
            </div>
            <p class="settings-overrides-hint">
              Auto-detected on first <code>+ project</code> per repo. Edit or remove rows here.
            </p>
          </fieldset>
          <div id="settings-modal-error" class="settings-modal-error" hidden={!error}>{error}</div>
          <div class="settings-modal-actions">
            <button type="button" id="settings-cancel" onClick={close}>cancel</button>
            <button type="submit" id="settings-submit" disabled={submitting}>save</button>
          </div>
        </form>
      </div>
    </div>
  );
}
