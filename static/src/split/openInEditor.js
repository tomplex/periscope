// Pure: may this pane offer an "open in editor" action, and what does the
// button say? No DOM, no signals — the rail passes a window row and the
// configured editor name from /api/state.

// The server opens the pane's git toplevel, so a pane outside a repo has no
// worktree to open. `branch` is the cheapest live proof of repo-ness the
// window row already carries — it comes from cached_git_state(cwd), so it is
// non-empty exactly when the cwd resolved to a repo.
export function canOpenInEditor(w, editor) {
  return Boolean(editor) && Boolean(w?.branch);
}

// Title text for the hover action. Names the editor so it is obvious which
// app is about to steal focus, and the branch so it is obvious which worktree.
export function openInEditorTitle(w, editor) {
  return `open ${w?.branch ? `${w.branch} ` : ""}worktree in ${editor}`;
}
