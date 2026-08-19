import { describe, expect, it } from "vitest";
import { canOpenInEditor, openInEditorTitle } from "../openInEditor.js";

describe("canOpenInEditor", () => {
  const repoPane = { pid: "@1", branch: "main", cwd: "/repo" };

  it("offers the action for a repo pane with an editor configured", () => {
    expect(canOpenInEditor(repoPane, "Cursor")).toBe(true);
  });
  it("hides the action when no editor is configured", () => {
    expect(canOpenInEditor(repoPane, "")).toBe(false);
    expect(canOpenInEditor(repoPane, null)).toBe(false);
    expect(canOpenInEditor(repoPane, undefined)).toBe(false);
  });
  it("hides the action for a pane outside a repo", () => {
    // The server opens the git toplevel, so a non-repo pane has nothing to
    // open — a visible button there could only ever 400.
    expect(canOpenInEditor({ pid: "@2", cwd: "/Users/x/Downloads" }, "Cursor")).toBe(false);
    expect(canOpenInEditor({ pid: "@2", branch: "" }, "Cursor")).toBe(false);
  });
  it("tolerates a missing window row", () => {
    expect(canOpenInEditor(null, "Cursor")).toBe(false);
    expect(canOpenInEditor(undefined, "Cursor")).toBe(false);
  });
});

describe("openInEditorTitle", () => {
  it("names both the branch and the editor", () => {
    expect(openInEditorTitle({ branch: "feat-x" }, "Cursor"))
      .toBe("open feat-x worktree in Cursor");
  });
  it("degrades gracefully with no branch", () => {
    expect(openInEditorTitle({}, "Zed")).toBe("open worktree in Zed");
  });
});
