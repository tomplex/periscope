// Pure: query string (+ catalog) → ranked candidate cards. No DOM, no signals.
// Each card: { kind: 'open'|'worktree'|'pr', label, descriptor }.

const PR_URL = /github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/;
const PR_HASH = /^#?(\d+)$/;

export function parsePrRef(q) {
  const u = q.match(PR_URL);
  if (u) return { repo: u[1], pr: Number(u[2]) };
  const h = q.trim().match(PR_HASH);
  if (h) return { repo: null, pr: Number(h[1]) };
  return null;
}

function match(hay, needle) {
  return hay.toLowerCase().includes(needle.toLowerCase());
}

export function classify(query, catalog) {
  const q = (query || "").trim();
  const cards = [];
  if (!q) return cards;

  // Create actions lead — a new worktree or workspace is almost always the
  // target, so they sit above "open existing". Grouped by kind (all new-worktree
  // cards, then all new-workspace cards) so each section header appears once
  // even when several repos match.
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "worktree", label: `${r.label} · new worktree…`,
                   sub: `off origin/${r.default_branch || "HEAD"}`,
                   repo: r.repo, branches: r.branches, descriptor: null });
    }
  }
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "workspace", label: `${r.label} · new workspace…`,
                   sub: "goal-scoped group", repo: r.repo, descriptor: null });
    }
  }
  // Open an existing worktree by path/branch match.
  for (const w of catalog.worktrees || []) {
    if (match(w.path, q) || match(w.branch || "", q)) {
      const repoLabel = (catalog.repos.find(r => r.repo === w.repo) || {}).label || w.repo;
      cards.push({ kind: "open", label: `${repoLabel} · ${w.branch || "detached"}`,
                   sub: w.path, descriptor: { path: w.path } });
    }
  }
  const pr = parsePrRef(q);
  if (pr) {
    cards.push({ kind: "pr",
                 label: pr.repo ? `review PR #${pr.pr} in ${pr.repo}` : `review PR #${pr.pr}`,
                 sub: pr.repo || "choose a repo",
                 needsRepo: !pr.repo, pr });
  }
  return cards;
}
