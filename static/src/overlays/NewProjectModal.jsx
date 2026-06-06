// New-project modal. Open/close + populate repo/branch pickers from
// /api/projects/discoverable, submit to /api/projects, close on success.
// Ported from static/new-project-modal.js.
//
// Must-not-drop: the deferred rail auto-add — on a successful create, wait
// ~3500ms for the new tmux session's panes to appear in the /api/state poll
// (the poll cadence is 3s), then addWorktreeToRail. The wait reads the live
// `windows` signal (replaces state.lastWindows). Timing-coupled to the poll.
//
// CSS contract preserved: #new-project-modal / .new-project-modal-overlay /
// -card / -head / -sub / -hint / -error / -actions, body.new-project-modal-open.
// Escape closes via the shared LIFO useEscape hook (vanilla used the
// modal-shell pushEscape stack — same LIFO semantics).
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { track } from "../track.js";
import { modalRequest } from "./modalRequest.js";
import { addWorktreeToRail } from "../prefs.js";
import { windows } from "../store.js";

const open = signal(false);

function openNewProjectModal() {
  open.value = true;
  track("overlay.open", { which: "newproject" });
}
function close() {
  open.value = false;
}

// Deferred rail auto-add, shared shape with review-pr. Reads the live
// `windows` signal once the poll has reflected the new session's panes.
function deferRailAdd(result) {
  if (!result.tmux_session) return;
  setTimeout(async () => {
    const sessionName = result.tmux_session;
    const wins = (windows.value || []).filter((w) => w.session === sessionName);
    if (wins.length === 0) return; // race; user can + open later
    await addWorktreeToRail({
      repoKey: wins[0].repo_key || result.repo,
      worktreeKey: sessionName,
      paneIds: wins.map((w) => w.pid),
      hasReview: true,
    });
  }, 3500);
}

export function NewProjectModal() {
  useEscape(close, open.value);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [repos, setRepos] = useState([]);
  const [branchesByRepo, setBranchesByRepo] = useState({});
  const [repoVal, setRepoVal] = useState("");

  const repoRef = useRef(null);
  const branchRef = useRef(null);
  const nameRef = useRef(null);

  // Register the opener for the Preact <Header>'s #new-project-btn.
  useEffect(() => {
    const btn = document.getElementById("new-project-btn");
    if (btn) btn.addEventListener("click", openNewProjectModal);
    return () => { if (btn) btn.removeEventListener("click", openNewProjectModal); };
  }, []);

  // On open: reset inputs, fetch discoverable repos, focus the repo input.
  useEffect(() => {
    if (!open.value) return;
    setError("");
    setRepoVal("");
    if (repoRef.current) repoRef.current.value = "";
    if (branchRef.current) branchRef.current.value = "";
    if (nameRef.current) nameRef.current.value = "";
    (async () => {
      const { data, error: err } = await modalRequest("load repos", "/api/projects/discoverable");
      if (err) { setError(err); return; }
      setRepos(data.repos || []);
      setBranchesByRepo(data.branches_by_repo || {});
    })();
    repoRef.current?.focus();
  }, [open.value]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    const repo = repoRef.current.value.trim();
    const branch = branchRef.current.value.trim();
    const name = nameRef.current.value.trim();
    if (!repo || !branch) { setError("repo and branch are required"); return; }
    setSubmitting(true);
    const { data: result, error: err } = await modalRequest("create project", "/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, branch, name: name || undefined }),
    });
    setSubmitting(false);
    if (err) { setError(err); return; }
    if (result.warning) console.warn("new-project warning:", result.warning);
    close();
    deferRailAdd(result);
  }

  if (!open.value) return null;
  const branches = branchesByRepo[repoVal] || [];

  return (
    <div
      id="new-project-modal"
      class="new-project-modal-overlay"
      onClick={(e) => { if (e.target.id === "new-project-modal") close(); }}
    >
      <div class="new-project-modal-card">
        <header class="new-project-modal-head">
          <h2>+ project</h2>
          <button id="new-project-modal-close" title="close" onClick={close}>×</button>
        </header>
        <p class="new-project-modal-sub">
          Creates a worktree off <code>origin/&lt;default&gt;</code>, a tmux session, and a 2-window claude+shell layout.
        </p>
        <form id="new-project-form" onSubmit={handleSubmit}>
          <label>
            Repo
            <input
              ref={repoRef}
              id="new-project-repo"
              list="new-project-repos"
              placeholder="/Users/tom/dev/foo"
              required
              onInput={(e) => setRepoVal(e.currentTarget.value.trim())}
              onChange={(e) => setRepoVal(e.currentTarget.value.trim())}
            />
            <datalist id="new-project-repos">
              {repos.map((r) => <option key={r} value={r} />)}
            </datalist>
          </label>
          <label>
            Branch
            <input ref={branchRef} id="new-project-branch" list="new-project-branches" placeholder="tc/new-feature" required />
            <datalist id="new-project-branches">
              {branches.map((b) => <option key={b} value={b} />)}
            </datalist>
          </label>
          <label>
            Name <span class="new-project-modal-hint">(defaults to branch)</span>
            <input ref={nameRef} id="new-project-name" placeholder="optional" />
          </label>
          <div id="new-project-error" class="new-project-modal-error" hidden={!error}>{error}</div>
          <div class="new-project-modal-actions">
            <button type="button" id="new-project-cancel" onClick={close}>cancel</button>
            <button type="submit" id="new-project-submit" disabled={submitting}>create</button>
          </div>
        </form>
      </div>
    </div>
  );
}
