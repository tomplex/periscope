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

// A typed filesystem path. Kept pure + exported so it unit-tests on its own,
// like parsePrRef. Existence and git-ness are NOT checked here — classify has
// no filesystem; the card is optimistic and POST /api/open 400s with the real
// reason ("no such directory" / "not inside a git repo"), which apiCall
// already surfaces as a toast.
const PATH_LIKE = /^(~|\.{0,2}\/)/;

export function parsePathRef(q) {
  const s = (q || "").trim();
  if (!s || !PATH_LIKE.test(s)) return null;
  return s.replace(/\/+$/, "") || "/";   // "/repo/" → "/repo"; bare "/" survives
}

function match(hay, needle) {
  return hay.toLowerCase().includes(needle.toLowerCase());
}

export function classify(query, catalog) {
  const q = (query || "").trim();
  const cards = [];
  if (!q) return cards;

  // An explicit path leads everything — it's the most specific intent a query
  // can carry. Reuses kind "open" so pick() routes it through the existing
  // POST /api/open, which registers the project (ensure_project, never 409)
  // when it isn't one yet. Skipped when the catalog already knows this exact
  // path: that card carries the better label (repo · branch).
  const typed = parsePathRef(q);
  if (typed && !(catalog.worktrees || []).some(w => w.path === typed)) {
    cards.push({ kind: "open", label: `open path: ${typed}`,
                 sub: "register + open", descriptor: { path: typed } });
  }

  // Create actions lead — a new worktree or track is almost always the target,
  // so they sit above "open existing". Grouped by kind (all new-worktree cards,
  // then all new-track cards) so each section header appears once even when
  // several repos match.
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "worktree", label: `${r.label} · new worktree…`,
                   sub: `off origin/${r.default_branch || "HEAD"}`,
                   repo: r.repo, branches: r.branches, descriptor: null });
    }
  }
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "track", label: `${r.label} · new track…`,
                   sub: "goal-scoped group", repo: r.repo, descriptor: null });
    }
  }
  // Open an existing worktree by path/branch match.
  for (const w of catalog.worktrees || []) {
    if (match(w.path, q) || match(w.branch || "", q)) {
      const repoLabel = catalog.repos.find(r => r.repo === w.repo)?.label || w.repo;
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
  // Free-text fallthrough: any non-empty query can be handed to the commander.
  // Pinned last so structured cards (open/worktree/pr) always rank above it.
  cards.push({ kind: "command", label: `⚡ run: ${q}`, text: q });
  return cards;
}
