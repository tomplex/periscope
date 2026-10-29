// Pure helpers for the review walkthrough: the mdast-side parser of the
// walkthrough.md convention (spec D3), the per-stop reviewed-mark signatures
// it feeds (THI-443 D6), ref→hunk matching against the branch diff and the
// code-changes grouping built on it (THI-443 D5), and the queue sort (D6).
// The backend has a line-based twin for the header/stop count; both agree on
// ATX headings and plain metadata quotes.
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import { linearTicket } from "../split/linearTicket.js";
import { metaPairs, parseCite, refTokens } from "../split/mdMeta.js";

const KINDS = new Set(["new-subsystem", "extension", "reuse", "system-touch", "behavior"]);

function slug(s) {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "stop";
}

function textOf(node) {
  if (node.type === "text" || node.type === "inlineCode") return node.value;
  return (node.children || []).map(textOf).join("");
}

export function parseWalkthrough(text) {
  const src = String(text || "");
  const tree = fromMarkdown(src, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] });
  const blocks = tree.children;
  let title = "";
  let header = null;
  let problem = "";
  let problemMarkdown = "";
  const stops = [];
  const ids = new Map();

  let i = 0;
  if (blocks[i]?.type === "heading" && blocks[i].depth === 1) {
    title = textOf(blocks[i]);
    i++;
    const pairs = blocks[i]?.type === "blockquote" ? metaPairs(blocks[i], src) : null;
    if (pairs) {
      const o = Object.fromEntries(pairs);
      if (o.branch && o.head) {
        header = { branch: o.branch, head: o.head, pr: /^\d+$/.test(o.pr || "") ? Number(o.pr) : null, repo: o.repo ?? null };
      }
      i++;
    }
    if (blocks[i]?.type === "paragraph") {
      problem = textOf(blocks[i]);
      problemMarkdown = src.slice(blocks[i].position.start.offset, blocks[i].position.end.offset);
      i++;
    }
  }

  for (; i < blocks.length; i++) {
    const b = blocks[i];
    if (b.type !== "heading" || b.depth !== 2) continue;
    const stopTitle = textOf(b);
    let kind = null;
    let refs = [];
    let bodyStart = b.position.end.offset;
    const q = blocks[i + 1];
    const pairs = q?.type === "blockquote" ? metaPairs(q, src) : null;
    if (pairs) {
      const o = Object.fromEntries(pairs);
      kind = o.kind ?? null;
      refs = refTokens(o.refs || "").map(parseCite).filter(Boolean);
      bodyStart = q.position.end.offset;
      i++;
    }
    let j = i + 1;
    while (j < blocks.length && !(blocks[j].type === "heading" && blocks[j].depth === 2)) j++;
    const bodyEnd = j < blocks.length ? blocks[j].position.start.offset : src.length;
    // Top-level mermaid fences move to the stop's full-width diagram row
    // (THI-443 D1). A fence nested in a list item or quote stays where it is:
    // lifting it out would reorder content we can't place cleanly.
    const diagrams = [];
    let body = "";
    let cursor = bodyStart;
    for (let k = i + 1; k < j; k++) {
      const node = blocks[k];
      if (node.type !== "code" || node.lang !== "mermaid") continue;
      diagrams.push(node.value);
      body += src.slice(cursor, node.position.start.offset);
      cursor = node.position.end.offset;
    }
    body += src.slice(cursor, bodyEnd);
    const base = slug(stopTitle);
    const n = (ids.get(base) || 0) + 1;
    ids.set(base, n);
    stops.push({
      id: n === 1 ? base : `${base}-${n}`,
      title: stopTitle,
      kind,
      knownKind: kind != null && KINDS.has(kind),
      refs,
      // The whole stop as written, diagrams included: stopSig hashes this.
      source: src.slice(b.position.start.offset, bodyEnd),
      diagrams,
      bodyMarkdown: body,
    });
    i = j - 1;
  }
  return { title, header, problem, problemMarkdown, stops };
}

// FNV-1a, 32-bit, over the stop as written. A reviewed mark is tied to this:
// any rewrite of the title, metadata or text gives a new sig and lapses the
// mark (THI-443 D6), as the Changes tab's viewedSig lapses when a file changes.
// Pinned (see the test vector): a silent change here would lapse every mark
// already saved in reviewed.json.
export function stopSig(stop) {
  // trimEnd: appending a stop, or the file's final newline, must not lapse
  // the old last stop's mark.
  const s = (stop.source || "").trimEnd();
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

export function isReviewed(stop, reviewed) {
  return !!reviewed && reviewed[stop.title] === stopSig(stop);
}

// One stop's mark set (sig) or cleared (null), leaving the others alone.
export function withMark(reviewed, title, sig) {
  const next = { ...reviewed };
  if (sig == null) delete next[title];
  else next[title] = sig;
  return next;
}

// New-side span of a hunk: new_start through new_start + ctx/add lines − 1.
function newEnd(h) {
  const n = (h.lines || []).filter((l) => l.kind === "ctx" || l.kind === "add").length;
  return h.new_start + Math.max(n, 1) - 1;
}

// Whether a hunk's new-side span intersects a cited range. Shared by
// matchRefs and evidenceIndex so the two can't disagree on "covers".
const overlaps = (h, r) => h.new_start <= r.end && newEnd(h) >= r.start;

export function matchRefs(refs, diffFiles) {
  const byPath = new Map((diffFiles || []).map((f) => [f.path, f]));
  return refs.map((ref) => {
    const file = byPath.get(ref.path) || null;
    if (!file) return { ref, file: null, hunks: [], generated: false };
    const hunks = ref.start == null ? file.hunks : file.hunks.filter((h) => overlaps(h, ref));
    // `generated`: the server kept the entry but dropped its hunks (bundle, lockfile).
    return { ref, file: file.path, hunks, generated: !!file.generated };
  });
}

const basename = (p) => p.slice(p.lastIndexOf("/") + 1);

// The code-changes column (THI-443 D5). `shown` gets a file-list row and a
// block of hunks; `hidden` folds into one summary line, so files with nothing
// to show don't bury the ones that do. diffFiles null means no worktree: every
// ref is listed and none has hunks.
export function evidenceFor(refs, diffFiles) {
  const hidden = { missing: [], generated: [], unchanged: [], noText: [] };
  if (diffFiles == null) {
    return { shown: refs.map((ref) => ({ ref, name: basename(ref.path), hunks: [] })), hidden, noDiff: true };
  }
  const shown = [];
  for (const m of matchRefs(refs, diffFiles)) {
    if (!m.file) hidden.missing.push(m.ref);
    else if (m.generated) hidden.generated.push(m.ref);
    // A whole-file cite to a file with no hunks (binary, pure rename,
    // mode-only change) has no lines to call "unchanged at"; a ranged cite
    // that just misses every hunk does.
    else if (!m.hunks.length) hidden[m.ref.start == null ? "noText" : "unchanged"].push(m.ref);
    else shown.push({ ref: m.ref, name: basename(m.ref.path), hunks: m.hunks });
  }
  return { shown, hidden, noDiff: false };
}

// Why a ref has nothing to show, in display order. The code column's summary
// line and its expanded list both read this, so the two can't disagree.
export const HIDDEN_WHY = {
  missing: "not in this diff",
  generated: "generated",
  unchanged: "unchanged at the cited lines",
  noText: "no text changes",
};

// One part per non-empty hidden group, in HIDDEN_WHY's order.
export function hiddenSummary(hidden) {
  return Object.keys(HIDDEN_WHY)
    .filter((k) => hidden[k]?.length)
    .map((k) => `${hidden[k].length} ${HIDDEN_WHY[k]}`)
    .join(" · ");
}

// A cite chip in the prose lands on the file-list entry for its path whose
// hunks cover its line; with no line, or none covering it, the first entry for
// the path. A stop citing a.py:1-5 and a.py:50-60 sends a.py:52 to the second.
export function evidenceIndex(shown, cite) {
  const covers = (e) => cite.start != null && e.hunks.some((h) => overlaps(h, cite));
  const hit = shown.findIndex((e) => e.ref.path === cite.path && covers(e));
  return hit >= 0 ? hit : shown.findIndex((e) => e.ref.path === cite.path);
}

// A whole-file cite can carry thousands of diff lines, and drawing them all
// at once stalled the page, so a block past COLLAPSE_LINES starts as a button.
export const COLLAPSE_LINES = 300;
export const lineCount = (hunks) => hunks.reduce((n, h) => n + (h.lines?.length || 0), 0);

// A folded stop's stand-in for its comment list. Folding hides the list, and
// with it any reply the owning pane wrote into a comment without rewriting the
// stop (which would have lapsed the mark).
export function commentsLabel(list) {
  if (!list.length) return "";
  const open = list.filter((c) => !c.resolved).length;
  return `${list.length} comment${list.length === 1 ? "" : "s"}${open ? ` · ${open} open` : ""}`;
}

// Merged rows sink to the bottom of the readiness order and of every column
// sort, and their comments stop counting toward the header badge.
const isMerged = (r) => r.pr?.state === "merged";

// Readiness first — a live pane, then a checkout on disk, then nothing local —
// and within a tier the D6 order: unresolved comments, review-requested, a PR
// with no walkthrough yet, the rest, local work with no PR. Merged last.
function group(r) {
  if (isMerged(r)) return 99;
  const tier = r.owner ? 0 : r.worktree ? 10 : 20;
  if (r.unresolved > 0) return tier;
  if (r.role === "review-requested") return tier + 1;
  if (r.pr && !r.walkthrough) return tier + 2;
  if (r.role === "local") return tier + 4;
  return tier + 3;
}

// The queue's sortable columns (THI-444), in header order: the queue draws one
// header per entry, so the headers and the sort can't disagree. `label` is the
// header's text, `what` its "sort by …" title. `value` is what a click orders
// by, null for a row with nothing to rank; nulls sort last both ways, since
// flipping a column must not float a block of empties to the top. `first` is
// the first click's direction: the end of the column you'd act on.
export const SORT_COLUMNS = {
  branch: { label: "branch", what: "branch name", first: "asc", value: (r) => r.branch },
  pr: { label: "PR", what: "PR number", first: "desc", value: (r) => r.pr?.number ?? null },
  // Staleness, not stop count: most stale first is "what needs re-preparing".
  // The cell shows it in every state (staleLabel), or the order looks random.
  walkthrough: { label: "walkthrough", what: "staleness", first: "desc", value: (r) => r.walkthrough?.stale_by ?? null },
  // 0 renders "—", so it ranks with the empties.
  comments: { label: "comments", what: "open comments", first: "desc", value: (r) => r.unresolved || null },
  who: { label: "who", what: "owner", first: "asc", value: (r) => r.owner?.name || r.pr?.author || null },
};

// The walkthrough cell's staleness, for every value its sort ranks. A bare
// "N stops" read as current whether stale_by was 0 or null, yet null sorts
// with the empties. Null means no worktree to count commits in, or a head the
// worktree no longer has (rebased away).
export function staleLabel(r) {
  if (!r.walkthrough) return null;
  const s = r.walkthrough.stale_by;
  if (s > 0) return `stale ${s}`;
  if (s === 0) return "current";
  return r.worktree ? "stale: unknown" : "no worktree";
}

const compareValues = (a, b) => (typeof a === "string" ? a.localeCompare(b) : a - b);

// `sort` is {key, dir} from a header click, or null for the readiness order.
// The readiness sort runs first and Array.sort is stable, so rows a column ties
// keep that order and don't swap places on every 10 s poll. Merged rows stay at
// the bottom whatever the column.
export function sortRows(rows, sort = null) {
  const ready = [...(rows || [])].sort((a, b) =>
    group(a) - group(b)
    || (b.pr?.number || 0) - (a.pr?.number || 0)
    || a.branch.localeCompare(b.branch));
  const col = sort && SORT_COLUMNS[sort.key];
  if (!col) return ready;
  const sign = sort.dir === "asc" ? 1 : -1;
  return ready.sort((a, b) => {
    const va = col.value(a);
    const vb = col.value(b);
    return isMerged(a) - isMerged(b)
      || (va === null) - (vb === null)
      || (va === null ? 0 : sign * compareValues(va, vb));
  });
}

// A header click: a new column starts in its `first` direction, the second
// click flips it, the third returns to the readiness order (null).
export function nextSort(sort, key) {
  const first = SORT_COLUMNS[key].first;
  if (sort?.key !== key) return { key, dir: first };
  if (sort.dir === first) return { key, dir: first === "asc" ? "desc" : "asc" };
  return null;
}

// The queue's search box (THI-442): every whitespace-separated term must occur,
// case-insensitively, in one of the searched fields — branch, repo, #N, PR title,
// PR author, owner pane name, the ticket id the row shows (linearTicket's pick,
// so the two can't disagree) and a linked ticket's title. "fdy thi" narrows to
// fdy rows tied to a THI ticket. Fields join on a space, so a term never spans two.
export function filterRows(rows, query) {
  const terms = (query || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return rows || [];
  return (rows || []).filter((r) => {
    const hay = [
      r.branch, r.repo, r.pr ? `#${r.pr.number}` : null, r.pr?.title, r.pr?.author, r.owner?.name,
      linearTicket(r)?.id, r.linked_linear_title,
    ].filter(Boolean).join(" ").toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

export function unresolvedTotal(rows) {
  return (rows || []).filter((r) => !isMerged(r)).reduce((n, r) => n + (r.unresolved || 0), 0);
}
