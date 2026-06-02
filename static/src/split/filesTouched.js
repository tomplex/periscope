// Pure selector: collapses /api/pane/turns messages into a per-path
// ordered list, most-recent op first. Used by Sidebar's Files section.
//
// Tool scope is intentional: Read / Edit / Write / MultiEdit / NotebookEdit
// — the tools whose `input.file_path` cleanly names the touched path.
// Bash 'rm' / 'mv' detection would be brittle (false positives in long
// commands) and Claude has no formal Delete tool; deletes via Bash are
// not shown (accepted v1 limitation, see design spec).
const FILE_TOOLS = new Set([
  "Read", "Edit", "Write", "MultiEdit", "NotebookEdit",
]);

export function filesTouched(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  // Walk newest-to-oldest so the first occurrence we see for any path is
  // its latest op. Track seen paths so we don't override.
  const seen = new Map();
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const tus = m && m.tool_uses;
    if (!tus || !tus.length) continue;
    // Within a single assistant turn, walk tool_uses in reverse for the
    // same reason — last op in the turn wins for that path.
    for (let j = tus.length - 1; j >= 0; j--) {
      const t = tus[j];
      if (!t || !FILE_TOOLS.has(t.name)) continue;
      const path = t.input && (t.input.file_path || t.input.notebook_path);
      if (!path) continue;
      if (seen.has(path)) continue;
      seen.set(path, t.name);
    }
  }
  return [...seen.entries()].map(([path, op]) => ({ path, op }));
}
