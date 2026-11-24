import { describe, expect, it } from "vitest";
import { classify, parsePathRef, parsePrRef } from "../classify.js";

describe("parsePrRef", () => {
  it("parses a github PR url", () => {
    expect(parsePrRef("https://github.com/fdy/repo/pull/1234"))
      .toEqual({ repo: "fdy/repo", pr: 1234 });
  });
  it("parses a bare #N", () => {
    expect(parsePrRef("#42")).toEqual({ repo: null, pr: 42 });
  });
  it("returns null for non-pr", () => {
    expect(parsePrRef("splash")).toBeNull();
  });
});

describe("parsePathRef", () => {
  it("accepts the path-like prefixes", () => {
    expect(parsePathRef("~/github/tomplex/periscope")).toBe("~/github/tomplex/periscope");
    expect(parsePathRef("/d/splash")).toBe("/d/splash");
    expect(parsePathRef("./here")).toBe("./here");
    expect(parsePathRef("../sibling")).toBe("../sibling");
    expect(parsePathRef("~")).toBe("~");
  });
  it("rejects anything not path-like", () => {
    expect(parsePathRef("splash")).toBeNull();
    expect(parsePathRef("#7")).toBeNull();
    expect(parsePathRef("")).toBeNull();
    expect(parsePathRef(undefined)).toBeNull();
    expect(parsePathRef("   ")).toBeNull();
  });
  it("strips trailing slashes but keeps bare root", () => {
    expect(parsePathRef("/d/splash///")).toBe("/d/splash");
    expect(parsePathRef("/")).toBe("/");
  });
  it("trims surrounding whitespace", () => {
    expect(parsePathRef("  ~/repo  ")).toBe("~/repo");
  });
});

describe("classify", () => {
  const catalog = {
    repos: [{ repo: "/d/splash", label: "splash", default_branch: "main", branches: ["main", "feat-x"] }],
    worktrees: [{ path: "/d/splash", repo: "/d/splash", branch: "main", is_main: true }],
  };
  it("surfaces an open-dir card for a matching worktree", () => {
    const cards = classify("splash", catalog);
    expect(cards.some(c => c.kind === "open" && c.descriptor.path === "/d/splash")).toBe(true);
  });
  it("offers a new-worktree card for a matching repo", () => {
    expect(classify("splash", catalog).some(c => c.kind === "worktree")).toBe(true);
  });
  it("offers a PR card for a #N query", () => {
    expect(classify("#7", catalog).some(c => c.kind === "pr")).toBe(true);
  });
  it("returns nothing for an empty query", () => {
    expect(classify("", catalog)).toEqual([]);
  });
  it("always offers a run-command card for non-empty query", () => {
    const cards = classify("create a worktree for foo", { repos: [], worktrees: [] });
    const cmd = cards.find(c => c.kind === "command");
    expect(cmd).toBeTruthy();
    expect(cmd.text).toBe("create a worktree for foo");
  });
  it("no command card for empty query", () => {
    expect(classify("", { repos: [], worktrees: [] })).toEqual([]);
  });
  it("offers a new-track card for a matching repo", () => {
    const cards = classify("splash", catalog);
    const tk = cards.find(c => c.kind === "track");
    expect(tk).toBeTruthy();
    expect(tk.repo).toBe("/d/splash");
    expect(tk.label).toContain("new track");
  });
  it("offers an open card for a typed path outside the catalog", () => {
    const cards = classify("~/github/tomplex/periscope", catalog);
    const c = cards.find(k => k.kind === "open");
    expect(c).toBeTruthy();
    expect(c.descriptor).toEqual({ path: "~/github/tomplex/periscope" });
    expect(c.label).toContain("open path");
  });
  it("ranks the typed-path card above the command fallthrough", () => {
    const cards = classify("~/github/tomplex/periscope", catalog);
    expect(cards.findIndex(c => c.kind === "open"))
      .toBeLessThan(cards.findIndex(c => c.kind === "command"));
  });
  it("does not duplicate a path the catalog already knows", () => {
    const cards = classify("/d/splash", catalog);
    const opens = cards.filter(c => c.kind === "open");
    expect(opens).toHaveLength(1);
    expect(opens[0]).toMatchObject({ group: "splash", label: "open \u00b7 main" });
  });
  it("emits no path card for a non-path query", () => {
    expect(classify("splash", catalog).some(c => c.label?.includes("open path"))).toBe(false);
  });
});

describe("classify: one section per repo", () => {
  // splash-old is a second checkout of splash that the root scan lists as a
  // repo of its own, so the catalog repeats every worktree under both.
  const wts = (repo) => [
    { path: "/d/splash", repo, branch: "main", is_main: repo === "/d/splash" },
    { path: "/d/splash-old", repo, branch: "legacy", is_main: repo === "/d/splash-old" },
    { path: "/d/wt/splash-feat", repo, branch: "feat-x", is_main: false },
  ];
  const catalog = {
    repos: [
      { repo: "/d/splash-old", label: "splash-old", default_branch: "main", branches: [] },
      { repo: "/d/splash", label: "splash", default_branch: "main", branches: [] },
    ],
    worktrees: [...wts("/d/splash-old"), ...wts("/d/splash")],
  };
  const rows = (q) => classify(q, catalog).filter(c => c.kind !== "command").map(c => [c.group, c.kind, c.label]);

  it("puts a repo's main checkout, new worktree and new track together, the repo named exactly first", () => {
    expect(rows("splash").slice(0, 6)).toEqual([
      ["splash", "open", "open · main"],
      ["splash", "worktree", "new worktree…"],
      ["splash", "track", "new track…"],
      ["splash-old", "open", "open · legacy"],
      ["splash-old", "worktree", "new worktree…"],
      ["splash-old", "track", "new track…"],
    ]);
  });

  it("lists the other worktrees after the repos, each path once", () => {
    expect(rows("splash").slice(6)).toEqual([["Open worktree", "open", "splash-old · feat-x"]]);
    const paths = classify("splash", catalog).filter(c => c.kind === "open").map(c => c.descriptor.path);
    expect(paths).toEqual(["/d/splash", "/d/splash-old", "/d/wt/splash-feat"]);
  });

  it("offers a main checkout that matches by branch alone as a worktree", () => {
    expect(rows("legacy")).toEqual([["Open worktree", "open", "splash-old · legacy"]]);
  });
});

describe("reviews queue card", () => {
  it("offers the reviews queue for 'rev' queries", () => {
    const cards = classify("rev", { repos: [], worktrees: [] });
    expect(cards[0]).toMatchObject({ kind: "reviews", label: "Reviews · open branches and walkthroughs" });
  });
  it("does not offer the reviews queue for unrelated queries", () => {
    expect(classify("zzz", { repos: [], worktrees: [] }).some((c) => c.kind === "reviews")).toBe(false);
  });
});
