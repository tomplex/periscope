import { describe, expect, it } from "vitest";
import {fileExt, 
  filesTouched, partitionFilesByPriority,
} from "../filesTouched.js";

// Matches the /api/pane/turns shape (history/search.py:262-275): each
// message has {role, text, tool_uses} — the selector only reads
// tool_uses, but the fixture mirrors the real shape so future
// maintainers don't propagate a wrong type.
const u = (text) => ({ role: "user", text });
const a = (toolUses = []) => ({ role: "assistant", text: "", tool_uses: toolUses });
const tu = (name, file_path, extra = {}) => ({
  id: Math.random().toString(36),
  name,
  input: { file_path, ...extra },
});

describe("filesTouched", () => {
  it("returns empty for no messages", () => {
    expect(filesTouched([])).toEqual([]);
  });

  it("collapses one Read into a single entry", () => {
    const out = filesTouched([
      u("hi"),
      a([tu("Read", "src/a.ts")]),
    ]);
    expect(out).toEqual([{ path: "src/a.ts", op: "Read" }]);
  });

  it("dedups by path, latest op wins", () => {
    const out = filesTouched([
      a([tu("Read", "src/a.ts")]),
      a([tu("Edit", "src/a.ts")]),
    ]);
    expect(out).toEqual([{ path: "src/a.ts", op: "Edit" }]);
  });

  it("orders most-recent first", () => {
    const out = filesTouched([
      a([tu("Read", "src/a.ts")]),
      a([tu("Write", "src/b.ts")]),
    ]);
    expect(out).toEqual([
      { path: "src/b.ts", op: "Write" },
      { path: "src/a.ts", op: "Read" },
    ]);
  });

  it("recognizes MultiEdit and NotebookEdit", () => {
    const out = filesTouched([
      a([tu("MultiEdit", "src/a.ts")]),
      a([tu("NotebookEdit", "nb.ipynb", { notebook_path: "nb.ipynb" })]),
    ]);
    expect(out.map((x) => x.path)).toEqual(["nb.ipynb", "src/a.ts"]);
  });

  it("ignores non-file tools (Bash, Grep, Glob)", () => {
    const out = filesTouched([
      a([tu("Bash", undefined, { command: "rm foo.txt" })]),
      a([tu("Grep", undefined, { pattern: "TODO" })]),
    ]);
    expect(out).toEqual([]);
  });

  it("ignores tool_uses lacking file_path", () => {
    const out = filesTouched([
      a([{ id: "x", name: "Read", input: {} }]),
    ]);
    expect(out).toEqual([]);
  });
});

describe("fileExt", () => {
  it("lowercases the extension after the last dot", () => {
    expect(fileExt("/a/b/Spec.MD")).toBe("md");
    expect(fileExt("out.html")).toBe("html");
    expect(fileExt("a/b/c.test.js")).toBe("js");
  });
  it("returns empty for no extension or leading-dot files", () => {
    expect(fileExt("/a/Makefile")).toBe("");
    expect(fileExt("README")).toBe("");
  });
});

describe("partitionFilesByPriority", () => {
  it("splits html/md into priority, keeps recency order within groups", () => {
    const items = [
      { path: "src/a.js", op: "Edit" },
      { path: "docs/spec.md", op: "Read" },
      { path: "out/page.html", op: "Write" },
      { path: "src/b.js", op: "Edit" },
    ];
    const { priority, others } = partitionFilesByPriority(items);
    expect(priority.map((i) => i.path)).toEqual(["docs/spec.md", "out/page.html"]);
    expect(others.map((i) => i.path)).toEqual(["src/a.js", "src/b.js"]);
  });
  it("empty priority group when nothing matches", () => {
    const items = [{ path: "a.js", op: "Edit" }];
    expect(partitionFilesByPriority(items).priority).toEqual([]);
  });
});
