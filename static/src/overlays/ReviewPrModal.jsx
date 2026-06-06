// Review PR modal. Repo + PR number → POST /api/projects/pr-review. Ported
// from static/review-pr-modal.js.
//
// Must-not-drop: the deferred rail auto-add (same shape + ~3500ms timing as
// new-project) — reads the live `windows` signal after the poll reflects the
// new session's panes, then addWorktreeToRail.
//
// CSS contract preserved: #review-pr-modal / .review-pr-modal-overlay / -card
// / -head / -sub / -hint / -error / -actions, body.review-pr-modal-open.
// Escape closes via the shared LIFO useEscape hook.
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { track } from "../track.js";
import { modalRequest } from "./modalRequest.js";
import { addWorktreeToRail } from "../prefs.js";
import { windows } from "../store.js";

const open = signal(false);

function openReviewPRModal() {
  open.value = true;
  track("overlay.open", { which: "reviewpr" });
}
function close() {
  open.value = false;
}

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

export function ReviewPrModal() {
  useEscape(close, open.value);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [repos, setRepos] = useState([]);

  const repoRef = useRef(null);
  const prRef = useRef(null);
  const nameRef = useRef(null);

  useEffect(() => {
    const btn = document.getElementById("review-pr-btn");
    if (btn) btn.addEventListener("click", openReviewPRModal);
    return () => { if (btn) btn.removeEventListener("click", openReviewPRModal); };
  }, []);

  useEffect(() => {
    if (!open.value) return;
    setError("");
    if (repoRef.current) repoRef.current.value = "";
    if (prRef.current) prRef.current.value = "";
    if (nameRef.current) nameRef.current.value = "";
    (async () => {
      const { data, error: err } = await modalRequest("load repos", "/api/projects/discoverable");
      if (err) { setError(err); return; }
      setRepos(data.repos || []);
    })();
    repoRef.current?.focus();
  }, [open.value]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    const repo = repoRef.current.value.trim();
    const pr = parseInt(prRef.current.value, 10);
    const name = nameRef.current.value.trim();
    if (!repo) { setError("repo is required"); return; }
    if (!pr || pr <= 0) { setError("PR number must be a positive integer"); return; }
    setSubmitting(true);
    const { data: result, error: err } = await modalRequest("start PR review", "/api/projects/pr-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, pr_number: pr, name: name || undefined }),
    });
    setSubmitting(false);
    if (err) { setError(err); return; }
    close();
    deferRailAdd(result);
  }

  if (!open.value) return null;

  return (
    <div
      id="review-pr-modal"
      class="review-pr-modal-overlay"
      onClick={(e) => { if (e.target.id === "review-pr-modal") close(); }}
    >
      <div class="review-pr-modal-card">
        <header class="review-pr-modal-head">
          <h2>review PR</h2>
          <button id="review-pr-modal-close" title="close" onClick={close}>×</button>
        </header>
        <p class="review-pr-modal-sub">
          Fetches <code>pull/&lt;N&gt;/head:pr-&lt;N&gt;</code>, creates a worktree, opens a tmux session with claude.
        </p>
        <form id="review-pr-form" onSubmit={handleSubmit}>
          <label>
            Repo
            <input ref={repoRef} id="review-pr-repo" list="review-pr-repos" placeholder="/Users/tom/dev/foo" required />
            <datalist id="review-pr-repos">
              {repos.map((r) => <option key={r} value={r} />)}
            </datalist>
          </label>
          <label>
            PR number
            <input ref={prRef} id="review-pr-number" type="number" min="1" placeholder="1234" required />
          </label>
          <label>
            Name <span class="review-pr-modal-hint">(defaults to the PR's branch name)</span>
            <input ref={nameRef} id="review-pr-name" placeholder="optional" />
          </label>
          <div id="review-pr-error" class="review-pr-modal-error" hidden={!error}>{error}</div>
          <div class="review-pr-modal-actions">
            <button type="button" id="review-pr-cancel" onClick={close}>cancel</button>
            <button type="submit" id="review-pr-submit" disabled={submitting}>review</button>
          </div>
        </form>
      </div>
    </div>
  );
}
