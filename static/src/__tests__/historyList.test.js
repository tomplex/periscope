// The /history list's head line and the state it shows with no row.
import { describe, expect, it } from "vitest";
import { closedLabel, emptyList, listHead } from "../../historyList.js";

const NO_FILTER = { project: "", since: "all", includeTrivial: false, rerank: false };
const rows = (n) => Array.from({ length: n }, (_, i) => ({ session_id: `s${i}` }));
const page = ({ filter, answered, ...over } = {}) => ({
  query: answered?.query ?? "",
  filter: { ...NO_FILTER, ...filter },
  results: [],
  answered: answered && { query: "", rerankUsed: false, limit: 50, unsummarizedHidden: null, ...answered },
  searchError: null,
  loading: false,
  stats: null,
  ...over,
});

describe("listHead", () => {
  it("says first N when the rows fill the cap that applied", () => {
    const reranked = page({ answered: { query: "spinner", rerankUsed: true, limit: 20 }, results: rows(20) });
    expect(listHead(reranked)).toBe("first 20 matches · reranked by Haiku");
  });

  it("gives the plain count under the cap", () => {
    expect(listHead(page({ answered: { query: "spinner", limit: 50 }, results: rows(20) }))).toBe(
      "20 matches · best text match first",
    );
  });

  it("names the order of a list with no query, and the filters that hide sessions", () => {
    const filtered = page({
      filter: { project: "/Users/tom/dev/fdy", since: "week", includeTrivial: true },
      answered: { limit: 50 },
      results: rows(50),
    });
    expect(listHead(filtered)).toBe(
      "first 50 sessions · newest last turn first · fdy · last turn this week · unsummarized included",
    );
  });

  it("says when a rerank was asked for and failed", () => {
    const failed = page({ filter: { rerank: true }, answered: { query: "spinner" }, results: rows(3) });
    expect(listHead(failed)).toBe("3 matches · best text match first (rerank failed)");
  });

  it("says a rerank is running, since it takes seconds", () => {
    const running = page({ filter: { rerank: true }, query: "spinner", loading: true, answered: { query: "spin" } });
    expect(listHead(running)).toBe("Reranking with Haiku…");
  });

  it("keeps the last answer's line while a text search runs", () => {
    const running = page({ query: "spinner", loading: true, answered: { query: "spin" }, results: rows(1) });
    expect(listHead(running)).toBe("1 match");
  });
});

describe("emptyList", () => {
  it("is null while there are rows, and before the first answer", () => {
    expect(emptyList(page({ answered: {}, results: rows(1) }))).toBeNull();
    expect(emptyList(page())).toBeNull();
  });

  it("offers the unsummarized sessions that match a query", () => {
    const answer = page({ answered: { query: "spinner", unsummarizedHidden: 3 }, stats: { total: 260, heuristic: 122 } });
    expect(emptyList(answer)).toEqual({ kind: "hidden", hidden: 3 });
  });

  it("does not offer the whole index's unsummarized count for a query that none of them match", () => {
    const answer = page({ answered: { query: "spinner", unsummarizedHidden: 0 }, stats: { total: 260, heuristic: 122 } });
    expect(emptyList(answer)).toEqual({ kind: "no-match" });
  });

  it("with no query, counts every unsummarized session, which recent() hides", () => {
    expect(emptyList(page({ answered: {}, stats: { total: 260, heuristic: 122 } }))).toEqual({ kind: "hidden", hidden: 122 });
  });

  it("offers nothing more once unsummarized sessions are included", () => {
    const answer = page({ filter: { includeTrivial: true }, answered: { query: "spinner", unsummarizedHidden: 3 } });
    expect(emptyList(answer)).toEqual({ kind: "no-match" });
  });

  it("blames a filter that hides sessions before the unsummarized ones", () => {
    const answer = page({ filter: { since: "today" }, answered: { query: "spinner", unsummarizedHidden: 3 } });
    expect(emptyList(answer)).toEqual({ kind: "filtered" });
  });

  it("tells an empty index apart, and a failed search first of all", () => {
    expect(emptyList(page({ answered: {}, stats: { total: 0, heuristic: 0 } }))).toEqual({ kind: "no-index" });
    expect(emptyList(page({ searchError: "periscope did not answer." }))).toEqual({ kind: "error" });
  });
});

describe("closedLabel", () => {
  it("says the weekday and the local time, with two digits each", () => {
    const at = new Date(2026, 10, 19, 4, 0).getTime() / 1000;
    expect(closedLabel(at)).toBe("closed Thu 04:00");
  });
});
