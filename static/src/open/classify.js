// Pure: query string (+ catalog) → ranked candidate cards. No DOM, no signals.
// Each card: { kind: 'open'|'worktree'|'track'|'pr'|'reviews'|'command', label,
// descriptor }, plus `group` where the section header isn't the kind's own.

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

// A repo named exactly the query leads, then one that starts with it:
// "periscope" puts periscope above periscope-hardening.
function rank(label, q) {
  const l = label.toLowerCase(), n = q.toLowerCase();
  return l === n ? 0 : l.startsWith(n) ? 1 : 2;
}

export function classify(query, catalog) {
  const q = (query || "").trim();
  const cards = [];
  if (!q) return cards;

  if ("reviews".startsWith(q.toLowerCase()) && q.length >= 2) {
    cards.push({ kind: "reviews", label: "Reviews · open branches and walkthroughs", sub: "the review queue", descriptor: null });
  }

  // An explicit path leads the repo/worktree cards (it never collides with the
  // reviews card: path queries start with ~ . or /) — it's the most specific intent a query
  // can carry. Reuses kind "open" so pick() routes it through the existing
  // POST /api/open, which registers the project (ensure_project, never 409)
  // when it isn't one yet. Skipped when the catalog already knows this exact
  // path: that card carries the better label (repo · branch).
  const typed = parsePathRef(q);
  if (typed && !(catalog.worktrees || []).some(w => w.path === typed)) {
    cards.push({ kind: "open", label: `open path: ${typed}`,
                 sub: "register + open", descriptor: { path: typed } });
  }

  // One section per matching repo, holding everything you can start there:
  // its main checkout, a new worktree, a new track. The main checkout leads —
  // it is the usual target (an agent there makes its own worktrees), and
  // grouped by kind it sat below every repo's create rows.
  const worktrees = catalog.worktrees || [];
  const shown = new Set();   // paths that already have an open card
  const repos = (catalog.repos || []).filter(r => match(r.label, q) || match(r.repo, q));
  repos.sort((a, b) => rank(a.label, q) - rank(b.label, q));
  for (const r of repos) {
    const main = worktrees.find(w => w.repo === r.repo && w.is_main);
    if (main) {
      shown.add(main.path);
      cards.push({ kind: "open", group: r.label, label: `open · ${main.branch || "detached"}`,
                   sub: main.path, descriptor: { path: main.path } });
    }
    cards.push({ kind: "worktree", group: r.label, label: "new worktree…",
                 sub: `off origin/${r.default_branch || "HEAD"}`,
                 repo: r.repo, branches: r.branches, descriptor: null });
    cards.push({ kind: "track", group: r.label, label: "new track…",
                 sub: "goal-scoped group", repo: r.repo, descriptor: null });
  }
  // Every other worktree matching by path or branch. Once per path: the
  // catalog repeats a repo's worktrees under each of its checkouts that a root
  // scan found as a repo.
  for (const w of worktrees) {
    if (shown.has(w.path) || !(match(w.path, q) || match(w.branch || "", q))) continue;
    shown.add(w.path);
    const repoLabel = catalog.repos.find(r => r.repo === w.repo)?.label || w.repo;
    cards.push({ kind: "open", group: "Open worktree", label: `${repoLabel} · ${w.branch || "detached"}`,
                 sub: w.path, descriptor: { path: w.path } });
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
