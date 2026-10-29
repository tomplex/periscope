import { describe, expect, it } from "vitest";
import { filterRows, matchRefs, nextSort, parseWalkthrough, sortRows, staleLabel, unresolvedTotal } from "../walkthrough.js";

const DOC = `# Cost gauge

> branch: feat/x · head: 3f2a1c9 · pr: 1234
> repo: fdy

Panes cost more and nothing said so.

## Adds a gauge
> kind: new-subsystem · refs: periscope/cost_pressure.py, static/src/split/ctxBand.js:1-40

Prose one.

## Adds a gauge
> kind: reuse

Prose two.

## No meta here

Prose three.
`;

describe("parseWalkthrough", () => {
  const w = parseWalkthrough(DOC);
  it("reads the header and problem", () => {
    expect(w.title).toBe("Cost gauge");
    expect(w.header).toEqual({ branch: "feat/x", head: "3f2a1c9", pr: 1234, repo: "fdy" });
    expect(w.problem).toBe("Panes cost more and nothing said so.");
  });
  it("splits stops with kind, refs, body, and deduped ids", () => {
    expect(w.stops.map((s) => s.id)).toEqual(["adds-a-gauge", "adds-a-gauge-2", "no-meta-here"]);
    expect(w.stops[0].kind).toBe("new-subsystem");
    expect(w.stops[0].refs.map((r) => r.raw)).toEqual(["periscope/cost_pressure.py", "static/src/split/ctxBand.js:1-40"]);
    expect(w.stops[0].bodyMarkdown.trim()).toBe("Prose one.");
    expect(w.stops[2].kind).toBeNull();
    expect(w.stops[2].refs).toEqual([]);
    expect(w.stops[2].bodyMarkdown.trim()).toBe("Prose three.");
  });
  it("parses dunder and backticked refs from raw source", () => {
    const x = parseWalkthrough("# T\n\n## S\n> kind: reuse · refs: periscope/__init__.py:1-5, `a/b.py:3`\n\nbody\n");
    expect(x.stops[0].kind).toBe("reuse");
    expect(x.stops[0].refs.map((r) => r.raw)).toEqual(["periscope/__init__.py:1-5", "a/b.py:3"]);
    expect(x.stops[0].bodyMarkdown.trim()).toBe("body");
  });
  it("tolerates a missing header", () => {
    expect(parseWalkthrough("# T\n\nhi\n\n## S\n\nb").header).toBeNull();
  });
});

describe("matchRefs", () => {
  const files = [
    { path: "a.py", hunks: [
      { new_start: 1, lines: [{ kind: "ctx" }, { kind: "add" }, { kind: "del" }] },   // new-side 1-2
      { new_start: 40, lines: [{ kind: "add" }, { kind: "add" }] },                   // 40-41
    ] },
    { path: "b.py", hunks: [{ new_start: 5, lines: [{ kind: "ctx" }] }] },
  ];
  it("whole file, intersecting range, no match", () => {
    const out = matchRefs([
      { path: "a.py", start: null, end: null, raw: "a.py" },
      { path: "a.py", start: 41, end: 60, raw: "a.py:41-60" },
      { path: "a.py", start: 3, end: 39, raw: "a.py:3-39" },
      { path: "c.py", start: null, end: null, raw: "c.py" },
    ], files);
    expect(out[0].hunks.length).toBe(2);
    expect(out[1].hunks.map((h) => h.new_start)).toEqual([40]);
    expect(out[2].hunks).toEqual([]);
    expect(out[3].file).toBeNull();
    expect(out.map((m) => m.generated)).toEqual([false, false, false, false]);
  });
  it("a generated file keeps its entry with no hunks and says so", () => {
    const gen = [...files, { path: "static/dist/app.js", hunks: [], generated: true }];
    const [m] = matchRefs([{ path: "static/dist/app.js", start: null, end: null, raw: "static/dist/app.js" }], gen);
    expect(m.file).toBe("static/dist/app.js");
    expect(m.hunks).toEqual([]);
    expect(m.generated).toBe(true);
  });
});

describe("sortRows", () => {
  const row = (o) => ({ repo: "r", branch: "b", role: "mine", pr: null, walkthrough: null, unresolved: 0, ...o });
  it("orders unresolved, review-requested, PR-without-walkthrough, rest, local, merged", () => {
    const rows = [
      row({ branch: "local", role: "local" }),
      row({ branch: "mine-walk", pr: { number: 3, state: "open" }, walkthrough: { stops: 1 } }),
      row({ branch: "merged", pr: { number: 9, state: "merged" } }),
      row({ branch: "prnowalk", pr: { number: 5, state: "open" } }),
      row({ branch: "theirs", role: "review-requested", pr: { number: 7, state: "open" } }),
      row({ branch: "unres", unresolved: 2, walkthrough: { stops: 1 } }),
      row({ branch: "prnowalk-hi", pr: { number: 8, state: "open" } }),
    ];
    expect(sortRows(rows).map((r) => r.branch)).toEqual([
      "unres", "theirs", "prnowalk-hi", "prnowalk", "mine-walk", "local", "merged",
    ]);
  });
  it("a live pane outranks a checkout outranks nothing local, whatever the D6 order says", () => {
    const rows = [
      row({ branch: "theirs-nowhere", role: "review-requested", pr: { number: 7, state: "open" } }),
      row({ branch: "unres-nowhere", unresolved: 2, walkthrough: { stops: 1 } }),
      row({ branch: "mine-wt", pr: { number: 3, state: "open" }, walkthrough: { stops: 1 }, worktree: "/wt" }),
      row({ branch: "local-pane", role: "local", owner: { name: "p" }, worktree: "/wt2" }),
      row({ branch: "merged-pane", pr: { number: 9, state: "merged" }, owner: { name: "p" } }),
      row({ branch: "unres-wt", unresolved: 1, walkthrough: { stops: 1 }, worktree: "/wt3" }),
    ];
    expect(sortRows(rows).map((r) => r.branch)).toEqual([
      "local-pane", "unres-wt", "mine-wt", "unres-nowhere", "theirs-nowhere", "merged-pane",
    ]);
  });
  it("a merged PR with unresolved comments still sorts last and is not counted", () => {
    const rows = [
      row({ branch: "m", pr: { number: 9, state: "merged" }, unresolved: 4 }),
      row({ branch: "o", pr: { number: 1, state: "open" }, walkthrough: { stops: 1 } }),
    ];
    expect(sortRows(rows).map((r) => r.branch)).toEqual(["o", "m"]);
    expect(unresolvedTotal(rows)).toBe(0);
  });
});

describe("sortRows by a column (THI-444)", () => {
  const row = (o) => ({ repo: "r", branch: "b", role: "mine", pr: null, walkthrough: null, unresolved: 0, owner: null, ...o });
  const open = (n) => ({ number: n, state: "open" });
  const order = (rows, key, dir) => sortRows(rows, { key, dir }).map((r) => r.branch);

  it("orders by branch name both ways", () => {
    const rows = [row({ branch: "b" }), row({ branch: "c" }), row({ branch: "a" })];
    expect(order(rows, "branch", "asc")).toEqual(["a", "b", "c"]);
    expect(order(rows, "branch", "desc")).toEqual(["c", "b", "a"]);
  });

  it("orders by PR number, rows with no PR last both ways", () => {
    const rows = [row({ branch: "none" }), row({ branch: "p5", pr: open(5) }), row({ branch: "p9", pr: open(9) })];
    expect(order(rows, "pr", "asc")).toEqual(["p5", "p9", "none"]);
    expect(order(rows, "pr", "desc")).toEqual(["p9", "p5", "none"]);
  });

  it("orders the walkthrough column by staleness; no walkthrough or unknown staleness last both ways", () => {
    const rows = [
      row({ branch: "none" }),
      row({ branch: "stale3", walkthrough: { stops: 2, stale_by: 3 } }),
      row({ branch: "unknown", walkthrough: { stops: 9, stale_by: null } }),
      row({ branch: "fresh", walkthrough: { stops: 1, stale_by: 0 } }),
      row({ branch: "stale1", walkthrough: { stops: 4, stale_by: 1 } }),
    ];
    // The two empties tie, so they keep the readiness order (here, by branch name).
    expect(order(rows, "walkthrough", "asc")).toEqual(["fresh", "stale1", "stale3", "none", "unknown"]);
    expect(order(rows, "walkthrough", "desc")).toEqual(["stale3", "stale1", "fresh", "none", "unknown"]);
  });

  it("orders by unresolved comments; zero renders a dash and sorts with the empties", () => {
    const rows = [row({ branch: "zero" }), row({ branch: "two", unresolved: 2 }), row({ branch: "five", unresolved: 5 })];
    expect(order(rows, "comments", "asc")).toEqual(["two", "five", "zero"]);
    expect(order(rows, "comments", "desc")).toEqual(["five", "two", "zero"]);
  });

  it("orders who by the owning pane's name, else the PR author", () => {
    const rows = [
      row({ branch: "nobody" }),
      row({ branch: "pane-zed", owner: { name: "zed" } }),
      row({ branch: "author-amy", pr: { ...open(3), author: "amy" } }),
      row({ branch: "pane-bob", owner: { name: "bob" }, pr: { ...open(4), author: "zoe" } }),
    ];
    expect(order(rows, "who", "asc")).toEqual(["author-amy", "pane-bob", "pane-zed", "nobody"]);
    expect(order(rows, "who", "desc")).toEqual(["pane-zed", "pane-bob", "author-amy", "nobody"]);
  });

  it("breaks ties with the default readiness order, in both directions", () => {
    const rows = [
      row({ branch: "plain", unresolved: 0, walkthrough: { stops: 1, stale_by: 2 } }),
      row({ branch: "unres", unresolved: 3, walkthrough: { stops: 1, stale_by: 2 } }),
      row({ branch: "pane", owner: { name: "p" }, walkthrough: { stops: 1, stale_by: 2 } }),
    ];
    expect(order(rows, "walkthrough", "asc")).toEqual(["pane", "unres", "plain"]);
    expect(order(rows, "walkthrough", "desc")).toEqual(["pane", "unres", "plain"]);
  });

  it("keeps merged rows at the bottom under any sort, ordered among themselves", () => {
    const rows = [
      row({ branch: "m-a", pr: { number: 1, state: "merged" } }),
      row({ branch: "o-b", pr: open(2) }),
      row({ branch: "m-c", pr: { number: 3, state: "merged" } }),
      row({ branch: "o-d" }),
    ];
    expect(order(rows, "branch", "asc")).toEqual(["o-b", "o-d", "m-a", "m-c"]);
    expect(order(rows, "branch", "desc")).toEqual(["o-d", "o-b", "m-c", "m-a"]);
    // An open row with no PR is empty for this column, yet still above every merged row.
    expect(order(rows, "pr", "desc")).toEqual(["o-b", "o-d", "m-c", "m-a"]);
  });

  it("falls back to the readiness order with no sort or an unknown column", () => {
    const rows = [row({ branch: "plain" }), row({ branch: "unres", unresolved: 1 })];
    expect(sortRows(rows, null).map((r) => r.branch)).toEqual(["unres", "plain"]);
    expect(order(rows, "nope", "asc")).toEqual(["unres", "plain"]);
  });
});

describe("staleLabel", () => {
  const wt = (stale_by, worktree = "/wt") => ({ walkthrough: { stops: 1, stale_by }, worktree });
  it("names every state the walkthrough sort ranks", () => {
    expect(staleLabel(wt(0))).toBe("current");
    expect(staleLabel(wt(3))).toBe("stale 3");
    // Head not found in the worktree (rebased away): staleness can't be counted.
    expect(staleLabel(wt(null))).toBe("stale: unknown");
    // Ranked with the empties, so the cell has to say why.
    expect(staleLabel(wt(null, null))).toBe("no worktree");
  });
  it("is null for a row with no walkthrough", () => {
    expect(staleLabel({ walkthrough: null, worktree: "/wt" })).toBeNull();
  });
});

describe("nextSort", () => {
  it("starts each column in its useful direction", () => {
    expect(nextSort(null, "branch")).toEqual({ key: "branch", dir: "asc" });
    expect(nextSort(null, "who")).toEqual({ key: "who", dir: "asc" });
    expect(nextSort(null, "pr")).toEqual({ key: "pr", dir: "desc" });
    expect(nextSort(null, "comments")).toEqual({ key: "comments", dir: "desc" });
    // Most stale first: which walkthroughs need re-preparing.
    expect(nextSort(null, "walkthrough")).toEqual({ key: "walkthrough", dir: "desc" });
  });

  it("flips on the second click and returns to the default order on the third", () => {
    const first = nextSort(null, "walkthrough");
    const second = nextSort(first, "walkthrough");
    expect(second).toEqual({ key: "walkthrough", dir: "asc" });
    expect(nextSort(second, "walkthrough")).toBeNull();
  });

  it("flips an ascending-first column to descending, then back to the default order", () => {
    expect(nextSort({ key: "branch", dir: "asc" }, "branch")).toEqual({ key: "branch", dir: "desc" });
    expect(nextSort({ key: "branch", dir: "desc" }, "branch")).toBeNull();
    expect(nextSort({ key: "who", dir: "asc" }, "who")).toEqual({ key: "who", dir: "desc" });
  });

  it("switching column starts that column fresh", () => {
    expect(nextSort({ key: "pr", dir: "asc" }, "branch")).toEqual({ key: "branch", dir: "asc" });
  });
});

describe("unresolvedTotal", () => {
  it("sums open rows only", () => {
    const row = (o) => ({ pr: { state: "open" }, unresolved: 0, ...o });
    expect(unresolvedTotal([row({ unresolved: 2 }), row({ unresolved: 3 }), row({ pr: { state: "merged" }, unresolved: 9 })])).toBe(5);
    expect(unresolvedTotal(null)).toBe(0);
  });
});

describe("parseWalkthrough lifts diagrams (THI-443 D1)", () => {
  const FENCES = `# T

> branch: feat/x · head: abc

Panes **cost** more and \`nothing\` said so.

## Adds a gauge
> kind: new-subsystem · refs: a.py

Before.

\`\`\`mermaid
flowchart LR
  a --> b
\`\`\`

Between.

- a list item

  \`\`\`mermaid
  flowchart TD
    nested --> stays
  \`\`\`

\`\`\`mermaid
flowchart LR
  c --> d
\`\`\`

After.
`;
  const w = parseWalkthrough(FENCES);
  const s = w.stops[0];

  it("returns the top-level mermaid fences in order", () => {
    expect(s.diagrams).toEqual(["flowchart LR\n  a --> b", "flowchart LR\n  c --> d"]);
  });
  it("cuts only those fences out of the body and leaves a nested one in place", () => {
    expect(s.bodyMarkdown).not.toContain("a --> b");
    expect(s.bodyMarkdown).not.toContain("c --> d");
    for (const kept of ["Before.", "Between.", "After.", "nested --> stays"]) expect(s.bodyMarkdown).toContain(kept);
  });
  it("keeps the stop's whole source, diagrams included, for its signature", () => {
    expect(s.source.startsWith("## Adds a gauge\n> kind: new-subsystem")).toBe(true);
    expect(s.source).toContain("a --> b");
  });
  it("returns the problem statement as markdown beside the plain text", () => {
    expect(w.problemMarkdown).toBe("Panes **cost** more and `nothing` said so.");
    expect(w.problem).toBe("Panes cost more and nothing said so.");
  });
  it("gives a stop without diagrams an empty list", () => {
    expect(parseWalkthrough(DOC).stops[0].diagrams).toEqual([]);
  });
});

describe("filterRows", () => {
  const rows = [
    { repo: "fdy", branch: "feat/restack", pr: { number: 8818, title: "Outcome restack", author: "sam" },
      owner: { name: "gauge-pane" }, linear_ticket: "FDY-8562" },
    { repo: "periscope", branch: "feat/queue", pr: { number: 45, title: "Queue search", author: "tdody" },
      owner: null, linked_linear: "THI-442", linked_linear_title: "Reviews tab improvements" },
    { repo: "periscope", branch: "feat/local", pr: null, owner: null },
  ];
  const branches = (q) => filterRows(rows, q).map((r) => r.branch);

  it("keeps every row, in order, for an empty or blank query", () => {
    expect(filterRows(rows, "")).toEqual(rows);
    expect(filterRows(rows, "   ")).toEqual(rows);
    expect(filterRows(null, "x")).toEqual([]);
  });
  it("matches each field the row shows", () => {
    expect(branches("feat/local")).toEqual(["feat/local"]);              // branch
    expect(branches("periscope")).toEqual(["feat/queue", "feat/local"]); // repo
    expect(branches("#8818")).toEqual(["feat/restack"]);                 // PR number
    expect(branches("search")).toEqual(["feat/queue"]);                  // PR title
    expect(branches("sam")).toEqual(["feat/restack"]);                   // PR author
    expect(branches("gauge")).toEqual(["feat/restack"]);                 // owner pane name
    expect(branches("fdy-8562")).toEqual(["feat/restack"]);              // derived ticket id
    expect(branches("thi-442")).toEqual(["feat/queue"]);                 // linked ticket id
    expect(branches("improvements")).toEqual(["feat/queue"]);            // linked ticket title
  });
  it("gives a row with no PR, owner or ticket nothing beyond its branch and repo", () => {
    expect(branches("#")).toEqual(["feat/restack", "feat/queue"]);
    expect(branches("undefined")).toEqual([]);
    expect(branches("null")).toEqual([]);
  });
  it("ignores case and needs every term", () => {
    expect(branches("QUEUE")).toEqual(["feat/queue"]);
    expect(branches("periscope local")).toEqual(["feat/local"]);
    expect(branches("periscope xyz")).toEqual([]);
  });
});
