import { beforeEach, describe, expect, it } from "vitest";
import {
  diffReview,
  fileState,
  setCollapsed,
  setViewed,
  viewedCount,
} from "../reviewState.js";

const REPO = "/repo";

beforeEach(() => { diffReview.value = {}; });

describe("fileState", () => {
  it("defaults to expanded and unviewed", () => {
    expect(fileState(diffReview.value, REPO, "a.py", "sig1"))
      .toEqual({ collapsed: false, viewed: false });
  });

  it("reports viewed while the signature matches", () => {
    setViewed(REPO, "a.py", "sig1", true);
    expect(fileState(diffReview.value, REPO, "a.py", "sig1").viewed).toBe(true);
  });

  it("EXPIRES viewed when the file changes again", () => {
    // The reason this module exists: a live diff must re-surface a file that
    // changed after you marked it viewed.
    setViewed(REPO, "a.py", "sig1", true);
    const after = fileState(diffReview.value, REPO, "a.py", "sig2");
    expect(after.viewed).toBe(false);
  });

  it("keys per repo, so same-named files in different worktrees don't collide", () => {
    setViewed(REPO, "a.py", "sig1", true);
    expect(fileState(diffReview.value, "/other", "a.py", "sig1").viewed).toBe(false);
  });
});

describe("collapse", () => {
  it("is independent of viewed and does not expire on content change", () => {
    setCollapsed(REPO, "a.py", true);
    // Different signature — collapse survives, since it's a view preference.
    expect(fileState(diffReview.value, REPO, "a.py", "whatever").collapsed).toBe(true);
  });

  it("marking viewed folds the file away", () => {
    setViewed(REPO, "a.py", "sig1", true);
    expect(fileState(diffReview.value, REPO, "a.py", "sig1").collapsed).toBe(true);
  });

  it("un-viewing unfolds it again", () => {
    setViewed(REPO, "a.py", "sig1", true);
    setViewed(REPO, "a.py", "sig1", false);
    expect(fileState(diffReview.value, REPO, "a.py", "sig1"))
      .toEqual({ collapsed: false, viewed: false });
  });
});

describe("bookkeeping", () => {
  it("drops empty entries instead of growing unbounded", () => {
    setCollapsed(REPO, "a.py", true);
    setCollapsed(REPO, "a.py", false);
    expect(diffReview.value).toEqual({});
  });

  it("counts only files whose viewed mark is still valid", () => {
    setViewed(REPO, "a.py", "sig1", true);
    setViewed(REPO, "b.py", "sigB", true);
    const files = [
      { path: "a.py", sig: "sig1" },   // still viewed
      { path: "b.py", sig: "CHANGED" }, // changed since viewing
      { path: "c.py", sig: "sigC" },   // never viewed
    ];
    expect(viewedCount(diffReview.value, REPO, files)).toBe(1);
  });
});
