import { beforeEach, describe, expect, it } from "vitest";
import {
  clearReview,
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

describe("auto-collapse defaults", () => {
  it("takes the default fold when you have no opinion", () => {
    expect(fileState(diffReview.value, REPO, "dist/app.js", "s", true).collapsed)
      .toBe(true);
    expect(fileState(diffReview.value, REPO, "src/a.py", "s", false).collapsed)
      .toBe(false);
  });

  it("an explicit expand OVERRIDES the default and sticks", () => {
    // The generated-file case: default says fold, you said no. Yours wins —
    // otherwise the bundle would silently re-fold on the next live refresh.
    setCollapsed(REPO, "dist/app.js", false);
    expect(fileState(diffReview.value, REPO, "dist/app.js", "s", true).collapsed)
      .toBe(false);
  });

  it("an explicit collapse sticks against a false default", () => {
    setCollapsed(REPO, "src/a.py", true);
    expect(fileState(diffReview.value, REPO, "src/a.py", "s2", false).collapsed)
      .toBe(true);
  });
});

describe("bookkeeping", () => {
  it("keeps an explicit expand — it is an opinion, not an empty entry", () => {
    setCollapsed(REPO, "a.py", true);
    setCollapsed(REPO, "a.py", false);
    expect(diffReview.value).not.toEqual({});
    expect(fileState(diffReview.value, REPO, "a.py", "s", true).collapsed).toBe(false);
  });

  it("clearReview forgets one repo's opinions and leaves others alone", () => {
    setViewed(REPO, "a.py", "sig1", true);
    setCollapsed("/other", "b.py", true);
    clearReview(REPO);
    expect(fileState(diffReview.value, REPO, "a.py", "sig1").viewed).toBe(false);
    expect(fileState(diffReview.value, "/other", "b.py", "s").collapsed).toBe(true);
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
