import { describe, expect, it } from "vitest";
import { classify, parsePrRef } from "../classify.js";

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
  it("pins the create actions (worktree, track) above open-existing", () => {
    const cards = classify("splash", catalog);
    const firstOpen = cards.findIndex(c => c.kind === "open");
    const wt = cards.findIndex(c => c.kind === "worktree");
    const tk = cards.findIndex(c => c.kind === "track");
    expect(wt).toBeGreaterThanOrEqual(0);
    expect(tk).toBeGreaterThanOrEqual(0);
    expect(wt).toBeLessThan(firstOpen);
    expect(tk).toBeLessThan(firstOpen);
  });
});
