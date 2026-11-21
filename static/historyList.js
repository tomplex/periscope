// The /history list's head line and empty states, kept pure so vitest can run
// them (static/src/__tests__/historyList.test.js). Self-contained, like util.js.
//
// `page` is the page's state (`S` in history.js): query, filter, results,
// answered, searchError, loading, stats.

export const SINCE_LABELS = { today: "today", week: "this week", month: "this month", all: "any time" };

export const baseName = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || "";

// The filters that hide sessions, as words. Rerank sorts; it hides nothing.
export function activeFilterLabels(filter) {
  const out = [];
  if (filter.project) out.push(baseName(filter.project));
  if (filter.since !== "all") out.push(`last turn ${SINCE_LABELS[filter.since]}`);
  if (filter.includeTrivial) out.push("unsummarized included");
  return out;
}

export function filtersHideSessions(filter) {
  return !!filter.project || filter.since !== "all";
}

// How many rows, in what order, under which filters.
export function listHead(page) {
  if (page.searchError) return "Search failed";
  // A rerank takes seconds; a text search answers before a word would show.
  if (page.loading && page.filter.rerank && page.query.trim()) return "Reranking with Haiku…";
  if (!page.answered) return "Loading sessions…";
  const n = page.results.length;
  const searching = !!page.answered.query.trim();
  const noun = searching ? (n === 1 ? "match" : "matches") : n === 1 ? "session" : "sessions";
  const parts = [n >= page.answered.limit ? `first ${n} ${noun}` : `${n} ${noun}`];
  if (n > 1) {
    if (!searching) parts.push("newest last turn first");
    else if (page.answered.rerankUsed) parts.push("reranked by Haiku");
    else if (page.filter.rerank) parts.push("best text match first (rerank failed)");
    else parts.push("best text match first");
  }
  return [...parts, ...activeFilterLabels(page.filter)].join(" · ");
}

// Why the list has no row, or null when it has rows or no answer yet. `kind`:
// "error" (the search failed), "no-index" (history holds no session),
// "filtered" (a filter hides sessions), "hidden" (`hidden` unsummarized
// sessions would show), "no-match".
export function emptyList(page) {
  if (page.searchError) return { kind: "error" };
  if (!page.answered || page.results.length) return null;
  if (page.stats?.total === 0) return { kind: "no-index" };
  if (filtersHideSessions(page.filter)) return { kind: "filtered" };
  // With no query and no filter, recent() hides every unsummarized session;
  // for a query, the route counts the unsummarized sessions that match it.
  const q = page.answered.query.trim();
  const hidden = page.filter.includeTrivial ? 0 : (q ? page.answered.unsummarizedHidden : page.stats?.heuristic) || 0;
  return hidden ? { kind: "hidden", hidden } : { kind: "no-match" };
}
