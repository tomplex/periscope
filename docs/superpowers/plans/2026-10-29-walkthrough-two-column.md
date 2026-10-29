# Walkthrough two-column stops: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the review walkthrough view as the THI-443 spec describes (`docs/superpowers/specs/2026-10-29-walkthrough-two-column-design.md`):

- a fixed header over one scrolling body;
- each stop a card, with its diagrams in a full-width row, then its prose beside its files and code changes;
- legible, themed diagrams that open full size;
- per-stop reviewed marks saved in `reviewed.json`;
- a collapsed comment box;
- a scoped type scale and contrast pass.

**Architecture:**
- **Pure helpers** in `static/src/review/walkthrough.js` (parsing, signatures, evidence grouping) and `static/src/split/mermaidTheme.js` carry the logic, all tested without a DOM.
- **`Walkthrough.jsx`** splits into a fetching `Walkthrough` and a pure `WalkthroughView`, so render-to-string tests can see the page.
- **Server side** is two small helpers in `periscope/reviews.py` and one POST route in `periscope/routes/reviews.py`.
- **CSS** is scoped under `.walkthrough`, because the Reviews queue shares `.wt-head`, `.wt-sub` and `.wt-empty`.

**Tech stack:** Preact + `@preact/signals`, Vite, vitest (`preact-render-to-string`, `environment: "node"`, no DOM), FastAPI, pytest, mermaid 11.

**Ground rules for this repo:**
- Work in the worktree `/Users/thibaultdody/github/tomplex/periscope/.claude/worktrees/thi-443-walkthrough-layout`, branch `thibaultdody/thi-443-review-walkthrough-full-width-layout-legible-diagrams`.
- Comments explain *why*, tersely. Match the surrounding style. Biome is lint-only; keep the terse hand-written UI style.
- Routes raise `HTTPException(status, detail)` with real status codes.
- Commit after every task. Single-line messages ending with the `Co-Authored-By` trailer from the session's attribution rule.
- Never push or open a PR without the user's go-ahead.
- `static/dist/app.js` is committed and must be rebuilt (`npm run build`) after `static/src/` changes. That's done once, in Task 11.

---

## File map

| File | Change | Responsibility |
|---|---|---|
| `static/src/review/walkthrough.js` | modify | `parseWalkthrough` lifts diagrams and returns `problemMarkdown` and `source`; new `stopSig`, `isReviewed`, `withMark`, `evidenceFor`, `hiddenSummary`, `evidenceIndex` |
| `static/src/review/__tests__/walkthrough.test.js` | modify | cases for all of the above |
| `static/src/split/mermaidTheme.js` | create | pure `mermaidTheme({color, font})` → mermaid config |
| `static/src/split/__tests__/mermaidTheme.test.js` | create | the token mapping |
| `static/src/split/Lightbox.jsx` | create | `lightbox` signal, `openLightbox`, `closeLightbox`, `<LightboxHost/>` |
| `static/src/overlays/Overlays.jsx` | modify | mounts `<LightboxHost/>` at the app root (inside `#split-view` it would sit under the header) |
| `static/src/split/__tests__/lightbox.test.jsx` | create | host render, open/close, keys |
| `static/src/split/Mermaid.jsx` | modify | base theme from `mermaidTheme`, `zoom` prop |
| `static/src/split/markdown.jsx` | modify | `zoom` option for images and diagrams |
| `static/src/split/__tests__/markdown.test.jsx` | modify | zoom cases |
| `periscope/reviews.py` | modify | `reviewed_path`, `read_reviewed`, `set_reviewed` |
| `tests/test_reviews.py` | modify | helper cases |
| `periscope/routes/reviews.py` | modify | `POST /api/reviews/reviewed`, `reviewed` in the walkthrough GET, shared `_check_stop` |
| `tests/routes/test_reviews.py` | modify | route cases |
| `static/src/review/Walkthrough.jsx` | rewrite | `Walkthrough` (fetch, marks) and `WalkthroughView` (pure page), `Stop`, `Evidence`, `Contents`, `ReviewedToggle`, `CommentForm`, `CommentBox`, `sendHint`, `commentOutcome` |
| `static/src/review/__tests__/walkthroughRender.test.jsx` | create | the page's render states |
| `static/styles.css` | modify | kind tokens in `:root`; the walkthrough block rewritten; lightbox |
| `docs/superpowers/specs/2026-10-26-review-walkthrough-design.md` | modify | D5 points at the new spec |
| `skills/walkthrough/SKILL.md` | modify | one line in Diagrams |
| `CLAUDE.md` | modify | the reviews row names the new spec |

---

### Task 0: Set up the worktree

- [ ] **Step 1: Install frontend dependencies and sync Python**

Run (in the worktree):
```bash
npm ci --no-audit --no-fund
uv sync --locked
```
Expected: both finish without errors.

- [ ] **Step 2: Confirm the suites are green before changing anything**

Run:
```bash
npx vitest run static/src/review static/src/split
uv run pytest tests/test_reviews.py tests/routes/test_reviews.py -q
```
Expected: all pass.

---

### Task 1: The parser lifts diagrams and keeps the problem's markdown

**Files:**
- Modify: `static/src/review/walkthrough.js` (`parseWalkthrough`)
- Test: `static/src/review/__tests__/walkthrough.test.js`

- [ ] **Step 1: Write the failing tests**

Append to the end of `static/src/review/__tests__/walkthrough.test.js`:

````js
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
````

(`DOC` is the constant already defined at the top of this file.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: the 5 new tests fail (`s.diagrams` is `undefined`, `w.problemMarkdown` is `undefined`).

- [ ] **Step 3: Implement**

In `static/src/review/walkthrough.js`, inside `parseWalkthrough`:

Replace
```js
  let problem = "";
```
with
```js
  let problem = "";
  let problemMarkdown = "";
```

Replace
```js
    if (blocks[i]?.type === "paragraph") { problem = textOf(blocks[i]); i++; }
```
with
```js
    if (blocks[i]?.type === "paragraph") {
      problem = textOf(blocks[i]);
      problemMarkdown = src.slice(blocks[i].position.start.offset, blocks[i].position.end.offset);
      i++;
    }
```

Replace
```js
    const bodyEnd = j < blocks.length ? blocks[j].position.start.offset : src.length;
    const base = slug(stopTitle);
    const n = (ids.get(base) || 0) + 1;
    ids.set(base, n);
    stops.push({
      id: n === 1 ? base : `${base}-${n}`,
      title: stopTitle,
      kind,
      knownKind: kind != null && KINDS.has(kind),
      refs,
      bodyMarkdown: src.slice(bodyStart, bodyEnd),
    });
```
with
```js
    const bodyEnd = j < blocks.length ? blocks[j].position.start.offset : src.length;
    // Top-level mermaid fences move to the stop's full-width diagram row
    // (THI-443 D1). A fence inside a list item stays where it is: lifting it
    // out would reorder content we can't place cleanly.
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
```

Replace
```js
  return { title, header, problem, stops };
```
with
```js
  return { title, header, problem, problemMarkdown, stops };
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: all pass, the existing `parseWalkthrough` cases included.

- [ ] **Step 5: Commit**

```bash
git add static/src/review/walkthrough.js static/src/review/__tests__/walkthrough.test.js
git commit -m "reviews walkthrough: parseWalkthrough lifts top-level mermaid fences into stop.diagrams, keeps stop.source and the problem's markdown (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Stop signatures and reviewed marks (pure)

**Files:**
- Modify: `static/src/review/walkthrough.js`
- Test: `static/src/review/__tests__/walkthrough.test.js`

- [ ] **Step 1: Write the failing tests**

Change the import line at the top of `static/src/review/__tests__/walkthrough.test.js` to:
```js
import {
  filterRows, isReviewed, matchRefs, nextSort, parseWalkthrough, sortRows, staleLabel, stopSig, unresolvedTotal, withMark,
} from "../walkthrough.js";
```

Append:
```js
describe("stopSig, isReviewed, withMark (THI-443 D6)", () => {
  const w = parseWalkthrough(DOC);
  const first = (doc) => parseWalkthrough(doc).stops[0];

  it("is 8 hex chars and stable for the same source", () => {
    expect(stopSig(w.stops[0])).toMatch(/^[0-9a-f]{8}$/);
    expect(stopSig(first(DOC))).toBe(stopSig(w.stops[0]));
  });
  it("changes with a one-character edit to the title, the metadata or the text", () => {
    const sig = stopSig(w.stops[0]);
    // String.replace with a string pattern edits the first stop only.
    for (const edited of [
      DOC.replace("## Adds a gauge", "## Adds a gaugE"),
      DOC.replace("kind: new-subsystem", "kind: new-subsystems"),
      DOC.replace("Prose one.", "Prose one!"),
    ]) expect(stopSig(first(edited))).not.toBe(sig);
  });
  it("ignores trailing whitespace, so appending a stop keeps the old last stop's mark", () => {
    // The last stop's source runs to the end of the file; a re-prepare that adds
    // a stop gives it a trailing blank line it didn't have.
    const a = "# T\n\n## One\n\nbody one\n\n## Two\n\nbody two\n";
    const b = `${a}\n## Three\n\nbody three\n`;
    expect(stopSig(parseWalkthrough(b).stops[1])).toBe(stopSig(parseWalkthrough(a).stops[1]));
  });
  it("counts a stop reviewed only while its title maps to its current sig", () => {
    const s = w.stops[2];
    expect(isReviewed(s, { [s.title]: stopSig(s) })).toBe(true);
    expect(isReviewed(s, { [s.title]: "00000000" })).toBe(false);
    expect(isReviewed(s, {})).toBe(false);
    expect(isReviewed(s, null)).toBe(false);
  });
  it("withMark sets or clears one title and leaves the rest alone", () => {
    const marks = { A: "11111111", B: "22222222" };
    expect(withMark(marks, "A", "33333333")).toEqual({ A: "33333333", B: "22222222" });
    expect(withMark(marks, "A", null)).toEqual({ B: "22222222" });
    expect(marks).toEqual({ A: "11111111", B: "22222222" });
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: the 5 new tests fail with `stopSig is not a function` (or the same for `isReviewed` / `withMark`). The existing tests still pass, because a missing named export imports as `undefined` rather than failing the file.

- [ ] **Step 3: Implement**

Append to `static/src/review/walkthrough.js`, after `nextSort`:
```js
// FNV-1a, 32-bit, over the stop as written. A reviewed mark is tied to this:
// any rewrite of the title, metadata or text gives a new sig and lapses the
// mark (THI-443 D6), as the Changes tab's viewedSig lapses when a file changes.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: every test in the file passes.

- [ ] **Step 5: Commit**

```bash
git add static/src/review/walkthrough.js static/src/review/__tests__/walkthrough.test.js
git commit -m "reviews walkthrough: stopSig (FNV-1a of the stop's source), isReviewed and withMark for per-stop reviewed marks (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Evidence grouping (pure)

**Files:**
- Modify: `static/src/review/walkthrough.js`
- Test: `static/src/review/__tests__/walkthrough.test.js`

- [ ] **Step 1: Write the failing tests**

Change the import at the top of `static/src/review/__tests__/walkthrough.test.js` to:
```js
import {
  evidenceFor, evidenceIndex, filterRows, hiddenSummary, isReviewed, matchRefs, nextSort,
  parseWalkthrough, sortRows, staleLabel, stopSig, unresolvedTotal, withMark,
} from "../walkthrough.js";
import { parseCite } from "../../split/mdMeta.js";
```

Append:
```js
describe("evidenceFor, hiddenSummary, evidenceIndex (THI-443 D5)", () => {
  const files = [
    { path: "a/one.py", hunks: [
      { new_start: 1, lines: [{ kind: "add", text: "x" }] },
      { new_start: 50, lines: [{ kind: "ctx", text: "y" }] },
    ] },
    { path: "dist/app.js", hunks: [], generated: true },
  ];
  const refs = ["a/one.py", "a/one.py:200-210", "b/missing.py", "dist/app.js"].map(parseCite);

  it("shows refs with hunks and sorts the rest into missing, generated and unchanged", () => {
    const ev = evidenceFor(refs, files);
    expect(ev.noDiff).toBe(false);
    expect(ev.shown.map((e) => [e.ref.raw, e.name, e.hunks.length])).toEqual([["a/one.py", "one.py", 2]]);
    expect(ev.hidden.missing.map((r) => r.raw)).toEqual(["b/missing.py"]);
    expect(ev.hidden.generated.map((r) => r.raw)).toEqual(["dist/app.js"]);
    expect(ev.hidden.unchanged.map((r) => r.raw)).toEqual(["a/one.py:200-210"]);
  });
  it("summarizes the hidden refs in one line, skipping empty groups", () => {
    expect(hiddenSummary(evidenceFor(refs, files).hidden)).toBe(
      "1 not in this diff · 1 generated · 1 unchanged at the cited lines",
    );
    expect(hiddenSummary(evidenceFor(refs.slice(0, 1), files).hidden)).toBe("");
  });
  it("lists every ref with no hunks when there is no worktree diff", () => {
    const ev = evidenceFor(refs, null);
    expect(ev.noDiff).toBe(true);
    expect(ev.shown.map((e) => [e.name, e.hunks.length])).toEqual([["one.py", 0], ["one.py", 0], ["missing.py", 0], ["app.js", 0]]);
    expect(hiddenSummary(ev.hidden)).toBe("");
  });
  it("lands a prose cite on the first shown file with its path", () => {
    const { shown } = evidenceFor(refs, files);
    expect(evidenceIndex(shown, parseCite("a/one.py:3"))).toBe(0);
    expect(evidenceIndex(shown, parseCite("b/missing.py"))).toBe(-1);
  });
  it("prefers the cited range whose hunks cover the cite's line", () => {
    const { shown } = evidenceFor(["a/one.py:1-5", "a/one.py:50-60"].map(parseCite), files);
    expect(evidenceIndex(shown, parseCite("a/one.py:50"))).toBe(1);
    expect(evidenceIndex(shown, parseCite("a/one.py:2"))).toBe(0);
    // No line, or a line no hunk covers: the first entry for the path.
    expect(evidenceIndex(shown, parseCite("a/one.py"))).toBe(0);
    expect(evidenceIndex(shown, parseCite("a/one.py:999"))).toBe(0);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: FAIL, `evidenceFor is not a function`.

- [ ] **Step 3: Implement**

Append to `static/src/review/walkthrough.js`:
```js
const basename = (p) => p.slice(p.lastIndexOf("/") + 1);

// The code-changes column (THI-443 D5). `shown` gets a file-list row and a
// block of hunks; `hidden` folds into one summary line, so files with nothing
// to show don't bury the ones that do. diffFiles null means no worktree: every
// ref is listed and none has hunks.
export function evidenceFor(refs, diffFiles) {
  const hidden = { missing: [], generated: [], unchanged: [] };
  if (diffFiles == null) {
    return { shown: refs.map((ref) => ({ ref, name: basename(ref.path), hunks: [] })), hidden, noDiff: true };
  }
  const shown = [];
  for (const m of matchRefs(refs, diffFiles)) {
    if (!m.file) hidden.missing.push(m.ref);
    else if (m.generated) hidden.generated.push(m.ref);
    else if (!m.hunks.length) hidden.unchanged.push(m.ref);
    else shown.push({ ref: m.ref, name: basename(m.ref.path), hunks: m.hunks });
  }
  return { shown, hidden, noDiff: false };
}

export function hiddenSummary(hidden) {
  const parts = [];
  if (hidden.missing.length) parts.push(`${hidden.missing.length} not in this diff`);
  if (hidden.generated.length) parts.push(`${hidden.generated.length} generated`);
  if (hidden.unchanged.length) parts.push(`${hidden.unchanged.length} unchanged at the cited lines`);
  return parts.join(" · ");
}

// A cite chip in the prose lands on the file-list entry for its path whose
// hunks cover its line; with no line, or none covering it, the first entry for
// the path. A stop citing a.py:1-5 and a.py:50-60 sends a.py:52 to the second.
export function evidenceIndex(shown, cite) {
  const covers = (e) => cite.start != null && e.hunks.some((h) => h.new_start <= cite.end && newEnd(h) >= cite.start);
  const hit = shown.findIndex((e) => e.ref.path === cite.path && covers(e));
  return hit >= 0 ? hit : shown.findIndex((e) => e.ref.path === cite.path);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run static/src/review/__tests__/walkthrough.test.js`
Expected: every test in the file passes.

- [ ] **Step 5: Commit**

```bash
git add static/src/review/walkthrough.js static/src/review/__tests__/walkthrough.test.js
git commit -m "reviews walkthrough: evidenceFor splits a stop's refs into shown (with hunks) and hidden (missing, generated, unchanged); hiddenSummary and evidenceIndex (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Diagrams in periscope's theme

**Files:**
- Create: `static/src/split/mermaidTheme.js`
- Create: `static/src/split/__tests__/mermaidTheme.test.js`
- Modify: `static/src/split/Mermaid.jsx`

- [ ] **Step 1: Write the failing test**

Create `static/src/split/__tests__/mermaidTheme.test.js`:
```js
import { describe, expect, it } from "vitest";
import { mermaidTheme } from "../mermaidTheme.js";

describe("mermaidTheme", () => {
  const hex = {
    "--bg-1": "#111111", "--bg-1-hi": "#1a1a1a", "--bg-2": "#222222",
    "--fg-0": "#f0f0f0", "--fg-1": "#d0d0d0", "--fg-2": "#a0a0a0",
  };
  const t = mermaidTheme({ color: (n) => hex[n], font: "Hanken Grotesk, sans-serif" });

  it("uses mermaid's configurable base theme in dark mode", () => {
    expect(t.theme).toBe("base");
    expect(t.themeVariables.darkMode).toBe(true);
  });
  it("maps periscope's tokens onto nodes, lines, labels and groups", () => {
    expect(t.themeVariables).toMatchObject({
      primaryColor: "#222222",
      primaryTextColor: "#f0f0f0",
      nodeTextColor: "#f0f0f0",
      primaryBorderColor: "#a0a0a0",
      lineColor: "#a0a0a0",
      clusterBorder: "#a0a0a0",
      edgeLabelBackground: "#111111",
      background: "#111111",
      clusterBkg: "#1a1a1a",
      secondaryColor: "#1a1a1a",
      textColor: "#d0d0d0",
      fontFamily: "Hanken Grotesk, sans-serif",
      fontSize: "14px",
    });
  });
  it("reads only opaque tokens, which the canvas converter can paint", () => {
    const asked = new Set();
    mermaidTheme({ color: (n) => { asked.add(n); return "#000000"; }, font: "x" });
    expect([...asked].sort()).toEqual(["--bg-1", "--bg-1-hi", "--bg-2", "--fg-0", "--fg-1", "--fg-2"]);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run static/src/split/__tests__/mermaidTheme.test.js`
Expected: FAIL, `Failed to resolve import "../mermaidTheme.js"`.

- [ ] **Step 3: Implement the builder**

Create `static/src/split/mermaidTheme.js`:
```js
// Mermaid's "base" theme in periscope's colors (THI-443 D4), app-wide: file
// previews draw diagrams with it too. `color(name)` returns a token as
// #rrggbb. mermaid's color math can't parse the stylesheet's oklch(), so
// Mermaid.jsx converts each token before it gets here. Pure, so the mapping is
// testable without a DOM.
export function mermaidTheme({ color, font }) {
  const card = color("--bg-1");
  const raised = color("--bg-1-hi");
  const line = color("--fg-2"); // ≥ 3:1 against a card: borders and arrows must read
  return {
    theme: "base",
    themeVariables: {
      darkMode: true,
      fontFamily: font,
      fontSize: "14px",
      background: card,
      primaryColor: color("--bg-2"),
      primaryTextColor: color("--fg-0"),
      nodeTextColor: color("--fg-0"),
      titleColor: color("--fg-0"),
      primaryBorderColor: line,
      lineColor: line,
      clusterBorder: line,
      secondaryColor: raised,
      clusterBkg: raised,
      tertiaryColor: card,
      edgeLabelBackground: card,
      textColor: color("--fg-1"),
    },
  };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run static/src/split/__tests__/mermaidTheme.test.js`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire it into `Mermaid.jsx`**

In `static/src/split/Mermaid.jsx`:

Add after the existing `import { useEffect, useRef, useState } from "preact/hooks";`:
```js
import { mermaidTheme } from "./mermaidTheme.js";
```

Replace
```js
let mermaidP = null;
function loadMermaid() {
  if (!mermaidP) {
    mermaidP = import("mermaid").then((m) => {
      const mermaid = m.default;
      // The dashboard has one (dark) theme; following prefers-color-scheme
      // would draw a light diagram on the dark UI whenever macOS is light.
      // Without suppressErrorRendering, render() on bad source draws the "Syntax
      // error" bomb SVG into a temp div on document.body and throws before
      // removing it, leaving a stray error diagram on the dashboard.
      mermaid.initialize({
        startOnLoad: false,
        theme: "dark",
        securityLevel: "strict",
        suppressErrorRendering: true,
      });
```
with
```js
const tokenValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let paint = null;
// Paint a token on a 1×1 canvas and read the pixel back: the portable way to
// turn the stylesheet's oklch() into the #rrggbb mermaid's color parser takes.
function tokenHex(name) {
  if (!paint) paint = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  paint.clearRect(0, 0, 1, 1);
  paint.fillStyle = "#000";
  paint.fillStyle = tokenValue(name);
  paint.fillRect(0, 0, 1, 1);
  const [r, g, b] = paint.getImageData(0, 0, 1, 1).data;
  return `#${[r, g, b].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

let mermaidP = null;
function loadMermaid() {
  if (!mermaidP) {
    mermaidP = import("mermaid").then((m) => {
      const mermaid = m.default;
      // Periscope's own tokens on mermaid's base theme (THI-443 D4), read once:
      // the dashboard has one dark theme, so following prefers-color-scheme
      // would only ever draw a light diagram on the dark UI.
      // Without suppressErrorRendering, render() on bad source draws the "Syntax
      // error" bomb SVG into a temp div on document.body and throws before
      // removing it, leaving a stray error diagram on the dashboard.
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        suppressErrorRendering: true,
        ...mermaidTheme({ color: tokenHex, font: tokenValue("--sans") }),
      });
```

- [ ] **Step 6: Run the split tests**

Run: `npx vitest run static/src/split`
Expected: all pass. The markdown mermaid tests only check the host markup; `loadMermaid` never runs under render-to-string.

- [ ] **Step 7: Commit**

```bash
git add static/src/split/mermaidTheme.js static/src/split/__tests__/mermaidTheme.test.js static/src/split/Mermaid.jsx
git commit -m "diagrams: mermaid's base theme in periscope's tokens (node fill bg-2, text fg-0, lines fg-2, Hanken Grotesk 14px), oklch tokens converted to hex on a 1×1 canvas (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full-size view for diagrams and images

**Files:**
- Create: `static/src/split/Lightbox.jsx`
- Create: `static/src/split/__tests__/lightbox.test.jsx`
- Modify: `static/src/split/Mermaid.jsx`, `static/src/split/markdown.jsx`, `static/src/overlays/Overlays.jsx`
- Test: `static/src/split/__tests__/markdown.test.jsx`

Design notes from the plan review:
- **The host is mounted in `Overlays.jsx`,** at the app root. Inside `#split-view` (`position: fixed; z-index: 1`), the sticky app header (`z-index: 10`) would paint over the lightbox, ✕ included.
- **An image is wrapped in a real `<button>`.** `<img role="button" tabindex>` fails biome's `noNoninteractiveElementToInteractiveRole` and `useFocusableInteractive`, and the repo uses no `biome-ignore` suppressions.
- **A diagram isn't made a button.** `role="button"` makes its children presentational, which would hide the diagram's text labels from screen readers. Instead, a mouse click on the diagram opens it, and a small real button in its corner is the keyboard and screen-reader way in.
- **Images inside a link don't zoom,** or one click would both open the lightbox and follow the link.

- [ ] **Step 1: Write the failing lightbox tests**

Create `static/src/split/__tests__/lightbox.test.jsx`:
```jsx
import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { closeLightbox, LightboxHost, lightbox, openLightbox } from "../Lightbox.jsx";

afterEach(() => { lightbox.value = null; });

describe("lightbox", () => {
  it("renders nothing while closed", () => {
    expect(render(<LightboxHost />)).toBe("");
  });
  it("shows an image at natural size in a focusable scroller with a close button, and closes", () => {
    openLightbox({ kind: "img", src: "https://example.com/a.png", alt: "chart" });
    const html = render(<LightboxHost />);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('<img src="https://example.com/a.png" alt="chart"');
    expect(html).toContain('aria-label="close"');
    // The scroller takes focus on open, so the arrow keys scroll a big diagram.
    expect(html).toContain('class="lightbox-body" tabindex="-1"');
    closeLightbox();
    expect(lightbox.value).toBeNull();
  });
  it("gives a diagram an empty host that its effect fills with the SVG", () => {
    openLightbox({ kind: "svg", svg: "<svg></svg>" });
    expect(render(<LightboxHost />)).toContain('class="lightbox-svg"');
  });
});
```

- [ ] **Step 2: Write the failing markdown zoom tests**

Append to `static/src/split/__tests__/markdown.test.jsx` (it already defines `html`):
````jsx
describe("zoom", () => {
  const img = "![chart](https://example.com/a.png)";
  const diagram = "```mermaid\nflowchart LR\n  a --> b\n```";
  it("leaves images and diagrams plain without zoom", () => {
    const out = html(img);
    expect(out).toContain('<img class="md-img" src="https://example.com/a.png" alt="chart"/>');
    expect(out).not.toContain("md-img-zoom");
    expect(html(diagram, { mermaid: true })).not.toContain("md-mermaid-zoom");
  });
  it("wraps an image in a button that opens it full size", () => {
    expect(html(img, { zoom: true })).toContain(
      '<button type="button" class="md-img-zoom" aria-label="open image full size: chart"><img class="md-img is-zoomable" src="https://example.com/a.png" alt="chart"/></button>',
    );
  });
  it("gives a diagram a click target and a real full-size button", () => {
    const out = html(diagram, { mermaid: true, zoom: true });
    expect(out).toContain('class="md-mermaid-svg is-zoomable"');
    expect(out).not.toContain('role="button"');
    expect(out).toContain(
      '<button type="button" class="md-mermaid-zoom" aria-label="open diagram full size" title="open full size">⤢</button>',
    );
  });
  it("leaves a linked image a plain image, so a click follows the link only", () => {
    const out = html("[![badge](https://x.test/b.svg)](https://x.test/)", { zoom: true });
    expect(out).toContain('<a class="md-link" href="https://x.test/"');
    expect(out).not.toContain("md-img-zoom");
  });
});
````

- [ ] **Step 3: Run both to verify they fail**

Run: `npx vitest run static/src/split/__tests__/lightbox.test.jsx static/src/split/__tests__/markdown.test.jsx`
Expected:
- The lightbox file fails to load `../Lightbox.jsx` ("Failed to load url … Does the file exist?").
- In markdown, "wraps an image…" and "gives a diagram…" fail.
- "leaves … plain" and "leaves a linked image…" already pass, which is expected: zoom is off today.

- [ ] **Step 4: Implement `Lightbox.jsx`**

Create `static/src/split/Lightbox.jsx`:
```jsx
// Full-size view for a walkthrough diagram or image (THI-443 D4). One host,
// mounted at the app root by Overlays.jsx, and one signal: a zoomable diagram
// or image calls openLightbox, and the host draws it at natural size over a
// backdrop that scrolls both ways.
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";

// { kind: "svg", svg } | { kind: "img", src, alt }, plus `from`: the element
// that opened it, refocused on close so keyboard users land where they were.
export const lightbox = signal(null);

export function openLightbox(item) {
  lightbox.value = { ...item, from: typeof document === "undefined" ? null : document.activeElement };
}

export function closeLightbox() {
  const from = lightbox.value?.from;
  lightbox.value = null;
  from?.focus?.();
}

export function LightboxHost() {
  const item = lightbox.value;
  const body = useRef(null);
  const svgHost = useRef(null);
  const closeBtn = useRef(null);
  useEscape(closeLightbox, !!item);
  useEffect(() => {
    if (!item) return;
    // The scroller, not ✕: arrow keys and Page Down then scroll a big diagram.
    body.current?.focus();
    if (item.kind !== "svg" || !svgHost.current) return;
    // Only ever mermaid's own sanitized SVG, copied from a rendered diagram
    // (Mermaid.jsx's innerHTML rule).
    svgHost.current.innerHTML = item.svg;
    const svg = svgHost.current.querySelector("svg");
    const vb = svg?.viewBox?.baseVal;
    if (svg && vb?.width) {
      // Natural size: mermaid's inline max-width and width="100%" would refit it.
      svg.style.maxWidth = "none";
      svg.setAttribute("width", String(Math.round(vb.width)));
      svg.setAttribute("height", String(Math.round(vb.height)));
    }
  }, [item]);
  if (!item) return null;
  const onBackdrop = (e) => { if (e.target === e.currentTarget) closeLightbox(); };
  // aria-modal promises focus stays in here: Tab moves between the scroller and ✕.
  const onKeyDown = (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    (document.activeElement === closeBtn.current ? body.current : closeBtn.current)?.focus();
  };
  return (
    <div class="lightbox" role="dialog" aria-modal="true" aria-label={item.kind === "img" ? "image, full size" : "diagram, full size"}
      onKeyDown={onKeyDown}>
      <button type="button" class="lightbox-close" ref={closeBtn} aria-label="close" onClick={closeLightbox}>✕</button>
      <div class="lightbox-body" ref={body} tabIndex={-1} onClick={onBackdrop}>
        {item.kind === "img" ? <img src={item.src} alt={item.alt} /> : <div class="lightbox-svg" ref={svgHost} />}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Add `zoom` to `Mermaid.jsx`**

In `static/src/split/Mermaid.jsx`, add to the imports:
```js
import { openLightbox } from "./Lightbox.jsx";
```

Replace
```jsx
export function Mermaid({ code }) {
```
with
```jsx
export function Mermaid({ code, zoom = false }) {
```

Replace
```jsx
  return (
    <div class="md-mermaid">
      {/* innerHTML is only ever mermaid's own SVG output, never document text.
          The source can be any previewed markdown; strict mode runs DOMPurify
          on the SVG — the one exception to convention #8. */}
      <div ref={host} class="md-mermaid-svg" />
```
with
```jsx
  const open = () => { if (host.current?.innerHTML) openLightbox({ kind: "svg", svg: host.current.innerHTML }); };
  const canZoom = zoom && !error;
  return (
    <div class="md-mermaid">
      {/* innerHTML is only ever mermaid's own SVG output, never document text.
          The source can be any previewed markdown; strict mode runs DOMPurify
          on the SVG — the one exception to convention #8. */}
      {/* zoom (the walkthrough): a click on the diagram opens it full size, and
          the button is the keyboard and screen-reader way in. role="button" on
          the diagram itself would hide its text labels from screen readers. */}
      <div ref={host} class={`md-mermaid-svg${canZoom ? " is-zoomable" : ""}`} onClick={canZoom ? open : undefined} />
      {canZoom ? (
        <button type="button" class="md-mermaid-zoom" aria-label="open diagram full size" title="open full size" onClick={open}>⤢</button>
      ) : null}
```

- [ ] **Step 6: Add `zoom` to `markdown.jsx`**

In `static/src/split/markdown.jsx`, add to the imports:
```js
import { openLightbox } from "./Lightbox.jsx";
```

In the `link` case, replace
```jsx
          {inlineAll(node.children, { ...ctx, cite: null })}
```
with
```jsx
          {/* zoom off inside a link: a zoom button in an <a> would both open the
              lightbox and follow the link on one click. */}
          {inlineAll(node.children, { ...ctx, cite: null, zoom: false })}
```

Replace
```jsx
      return <img class="md-img" src={url(node.url, ctx)} alt={node.alt || ""} key={key} />;
```
with
```jsx
    {
      const src = url(node.url, ctx);
      const alt = node.alt || "";
      if (!ctx.zoom) return <img class="md-img" src={src} alt={alt} key={key} />;
      // A real button around the image: <img role="button"> fails biome's
      // noNoninteractiveElementToInteractiveRole, and a button gets Enter and Space free.
      return (
        <button type="button" class="md-img-zoom" key={key}
          aria-label={alt ? `open image full size: ${alt}` : "open image full size"}
          onClick={() => openLightbox({ kind: "img", src, alt })}>
          <img class="md-img is-zoomable" src={src} alt={alt} />
        </button>
      );
    }
```
(This is the `image` case's final return. The `if (!ABSOLUTE_RE…)` early return above it stays.)

Replace
```jsx
      if (ctx.mermaid && node.lang === "mermaid") return <Mermaid code={node.value} key={key} />;
```
with
```jsx
      if (ctx.mermaid && node.lang === "mermaid") return <Mermaid code={node.value} zoom={ctx.zoom} key={key} />;
```

In `renderMarkdown`, replace
```js
    mermaid: opts.mermaid ?? false,
  };
```
with
```js
    mermaid: opts.mermaid ?? false,
    // Images and diagrams open full size on click (the walkthrough, THI-443).
    zoom: opts.zoom ?? false,
  };
```

- [ ] **Step 7: Mount the host at the app root**

In `static/src/overlays/Overlays.jsx`, add to the imports:
```js
import { LightboxHost } from "../split/Lightbox.jsx";
```

Replace
```jsx
      <OpenOmnibox />
    </>
```
with
```jsx
      <OpenOmnibox />
      {/* Here, not in the walkthrough: inside #split-view (fixed, z-index 1) the
          sticky header (z-index 10) would paint over it, ✕ included. */}
      <LightboxHost />
    </>
```

- [ ] **Step 8: Run the split tests and lint**

Run:
```bash
npx vitest run static/src/split
npx biome lint static/src/split static/src/overlays
```
Expected:
- vitest: everything passes, including the new lightbox and zoom cases.
- biome: no errors. The only warnings are the two `useOptionalChain` warnings already in `Rail.jsx`.

- [ ] **Step 9: Commit**

```bash
git add static/src/split/Lightbox.jsx static/src/split/__tests__/lightbox.test.jsx static/src/split/Mermaid.jsx static/src/split/markdown.jsx static/src/split/__tests__/markdown.test.jsx static/src/overlays/Overlays.jsx
git commit -m "diagrams: opt-in zoom — an image becomes a button and a diagram gets a click target plus a full-size button; one lightbox at the app root (focus on its scroller, Tab cycles, Esc/backdrop/✕ close, focus returns) (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `reviewed.json` helpers on the server

**Files:**
- Modify: `periscope/reviews.py`
- Test: `tests/test_reviews.py`

Design note from the plan review: `~/.claude/reviews` is **not** separated per instance (`config.instance_file()` doesn't apply to it), so the dev server (:17375) and prod (:17374) both write it. A thread lock alone let two processes collide on the shared temp file. In the review's experiment, 288 of 600 marks failed and only 43 survived. So writes also take an `fcntl.flock` on the review dir. And a mark never creates the dir: a review cleared mid-request must not come back as an orphan row holding only `reviewed.json`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_reviews.py`, add `import json` beside `import os`. Then append:
```python
def test_read_reviewed_missing_corrupt_and_mangled_read_as_empty(tmp_path):
    assert reviews.read_reviewed("fdy", "feat/x") == {}
    p = reviews.reviewed_path("fdy", "feat/x")
    p.parent.mkdir(parents=True)
    for bad in ["{not json", "[1, 2]", '"a string"']:
        p.write_text(bad)
        assert reviews.read_reviewed("fdy", "feat/x") == {}
    p.write_text(json.dumps({"A": "0123abcd", "B": 7, "C": None}))
    assert reviews.read_reviewed("fdy", "feat/x") == {"A": "0123abcd"}


def test_set_reviewed_sets_overwrites_unsets_and_removes_the_empty_file(tmp_path):
    reviews.review_dir("fdy", "feat/x").mkdir(parents=True)
    reviews.set_reviewed("fdy", "feat/x", "Stop A", "0123abcd")
    reviews.set_reviewed("fdy", "feat/x", "Stop B", "11111111")
    reviews.set_reviewed("fdy", "feat/x", "Stop A", "ffffffff")
    assert reviews.read_reviewed("fdy", "feat/x") == {"Stop A": "ffffffff", "Stop B": "11111111"}
    reviews.set_reviewed("fdy", "feat/x", "Stop A", None)
    reviews.set_reviewed("fdy", "feat/x", "never marked", None)
    assert reviews.read_reviewed("fdy", "feat/x") == {"Stop B": "11111111"}
    reviews.set_reviewed("fdy", "feat/x", "Stop B", None)
    assert not reviews.reviewed_path("fdy", "feat/x").exists()
    assert not list(reviews.review_dir("fdy", "feat/x").glob("*.tmp"))


def test_set_reviewed_never_creates_the_review_dir():
    with pytest.raises(FileNotFoundError):
        reviews.set_reviewed("fdy", "gone", "Stop A", "0123abcd")
    assert not reviews.review_dir("fdy", "gone").exists()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_reviews.py -k reviewed -q`
Expected: the 3 tests fail with `AttributeError: module 'periscope.reviews' has no attribute 'read_reviewed'` (or `'reviewed_path'`/`'set_reviewed'`).

- [ ] **Step 3: Implement**

In `periscope/reviews.py`:

Add `import fcntl` to the stdlib imports, in alphabetical order (after `from __future__ import annotations`, before `import json`).

Change the module docstring's first paragraph from
```
"""The review walkthrough convention: files under ~/.claude/reviews that the
`walkthrough` skill writes and periscope renders. Periscope only ever appends
to comments.md and removes a whole review dir on an explicit clear — the pane
is the sole writer of walkthrough.md (spec D2).
```
to
```
"""The review walkthrough convention: files under ~/.claude/reviews that the
`walkthrough` skill writes and periscope renders. Periscope only ever appends
to comments.md, writes reviewed.json (per-stop reviewed marks, THI-443) and
removes a whole review dir on an explicit clear — the pane is the sole writer
of walkthrough.md (spec D2).
```

After `comments_path`, add:
```python
def reviewed_path(repo: str, branch: str) -> Path:
    return review_dir(repo, branch) / "reviewed.json"
```

After `append_comment`, add:
```python
# reviewed.json is a read-modify-write, and the dev and prod servers both write
# ~/.claude/reviews (it isn't instance_file()-separated): a flock on the review
# dir serializes writers across processes, the thread lock within one.
_reviewed_lock = threading.Lock()


def read_reviewed(repo: str, branch: str) -> dict[str, str]:
    """stop title → the stop's sig when it was marked (THI-443 D6). A missing,
    unreadable or hand-mangled file reads as nothing reviewed."""
    try:
        data = json.loads(_read(reviewed_path(repo, branch)) or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def set_reviewed(repo: str, branch: str, stop: str, sig: str | None) -> None:
    """Mark `stop` reviewed at `sig`, or unmark it with None. Written through a
    temp file + os.replace so a crash mid-write can't leave half a JSON object;
    an empty result removes the file rather than leaving `{}` behind. Raises
    FileNotFoundError when the review dir is gone: a mark never creates it."""
    path = reviewed_path(repo, branch)
    with _reviewed_lock:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            fcntl.flock(dfd, fcntl.LOCK_EX)
            marks = read_reviewed(repo, branch)
            if sig is None:
                marks.pop(stop, None)
            else:
                marks[stop] = sig
            if not marks:
                path.unlink(missing_ok=True)
                return
            # One temp name is safe under the flock; a crash leaves at most this
            # file, which the next write replaces.
            tmp = path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(marks, indent=1, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        finally:
            os.close(dfd)  # releases the flock
```
(`json`, `os` and `threading` are already imported in `reviews.py`.)

- [ ] **Step 4: Run the tests and the format check**

Run:
```bash
uv run pytest tests/test_reviews.py -q
uv run ruff format --check periscope/reviews.py tests/test_reviews.py
uv run ruff check periscope/reviews.py tests/test_reviews.py
```
Expected: all tests pass, and both ruff commands are clean. If format complains, run `uv run ruff format periscope/reviews.py tests/test_reviews.py` and re-check.

- [ ] **Step 5: Commit**

```bash
git add periscope/reviews.py tests/test_reviews.py
git commit -m "reviews: reviewed.json — read_reviewed (corrupt reads as empty) and set_reviewed (thread lock + flock on the review dir for dev/prod, atomic, removes the file when empty, never creates the dir) (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `POST /api/reviews/reviewed` and `reviewed` in the walkthrough payload

**Files:**
- Modify: `periscope/routes/reviews.py`
- Test: `tests/routes/test_reviews.py`

- [ ] **Step 1: Write the failing tests**

In `tests/routes/test_reviews.py`, in `test_walkthrough_404_without_file_and_no_diff_without_worktree`, change the last line to:
```python
    assert body["text"] == WT and body["comments"] == [] and body["diff"] is None and body["stale_by"] is None
    assert body["reviewed"] == {}
```

Append (already `ruff format`ted to the repo's 120 columns):
```python
def test_reviewed_round_trips_through_the_walkthrough(client):
    _write_wt()
    q = {"repo": "fdy", "branch": "feat/x"}
    r = client.post("/api/reviews/reviewed", json={**q, "stop": "Stop A", "sig": "0123abcd", "reviewed": True})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/reviews/walkthrough", params=q).json()["reviewed"] == {"Stop A": "0123abcd"}
    r = client.post("/api/reviews/reviewed", json={**q, "stop": "Stop A", "sig": "0123abcd", "reviewed": False})
    assert r.status_code == 200
    assert client.get("/api/reviews/walkthrough", params=q).json()["reviewed"] == {}
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


@pytest.mark.parametrize(
    "stop,sig",
    [
        ("", "0123abcd"),
        ("  ", "0123abcd"),
        ("a\nb", "0123abcd"),
        ("Stop A", "xyz"),
        ("Stop A", "0123ABCD"),
        ("Stop A", "0123abcd0"),
        ("Stop A", "0123abcd\n"),
    ],
)
def test_reviewed_bad_stop_or_sig_is_400(client, stop, sig):
    _write_wt()
    r = client.post(
        "/api/reviews/reviewed", json={"repo": "fdy", "branch": "feat/x", "stop": stop, "sig": sig, "reviewed": True}
    )
    assert r.status_code == 400
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


def test_reviewed_without_a_walkthrough_is_404_and_creates_nothing(client):
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 404
    assert not reviews.review_dir("fdy", "feat/x").exists()


def test_reviewed_on_a_review_cleared_mid_request_is_404(client, mocker):
    _write_wt()
    mocker.patch("periscope.routes.reviews.reviews.set_reviewed", side_effect=FileNotFoundError)
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 404


def test_reviewed_rejects_traversal(client):
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "..", "branch": "feat/x", "stop": "A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/routes/test_reviews.py -k "reviewed or 404_without" -q`
Expected: FAIL. The GET has no `reviewed` key (`KeyError`), and the POST returns 405 from the static catch-all because the route doesn't exist.

- [ ] **Step 3: Implement**

In `periscope/routes/reviews.py`:

Change the docstring line
```
channel messages to the pane that owns the branch; the only filesystem
writes are an append to comments.md and the explicit clear of a review dir.
```
to
```
channel messages to the pane that owns the branch; the only filesystem
writes are an append to comments.md, reviewed.json's per-stop marks, and the
explicit clear of a review dir.
```

Add `import re` to the imports (after `import os`).

After `class CommentBody(ReviewRef): …`, add:
```python
class ReviewedBody(ReviewRef):
    stop: str
    sig: str
    reviewed: bool


# stopSig in static/src/review/walkthrough.js: 32-bit FNV-1a as 8 lowercase hex.
_SIG_RE = re.compile(r"[0-9a-f]{8}")


def _check_stop(stop: str) -> None:
    # A newline would split comments.md's heading line, and parse_comments
    # would drop the entry; reviewed.json keys follow the same rule.
    if not stop.strip() or "\n" in stop or "\r" in stop:
        raise HTTPException(400, "bad stop")
```

In `reviews_comment`, replace
```python
    _check(body.repo, body.branch)
    # A newline would split the heading line and parse_comments would drop the entry.
    if not body.stop.strip() or "\n" in body.stop or "\r" in body.stop:
        raise HTTPException(400, "bad stop")
```
with
```python
    _check(body.repo, body.branch)
    _check_stop(body.stop)
```

In `reviews_walkthrough`'s `load()`, replace
```python
            "comments": reviews.parse_comments(reviews._read(reviews.comments_path(repo, branch))),
```
with
```python
            "comments": reviews.parse_comments(reviews._read(reviews.comments_path(repo, branch))),
            "reviewed": reviews.read_reviewed(repo, branch),
```

After the `reviews_comment` function, add:
```python
@router.post("/api/reviews/reviewed")
async def reviews_reviewed(body: ReviewedBody):
    """Mark or unmark one stop reviewed (THI-443 D6). The sig ties the mark to
    the stop's text, so a re-prepare that rewrites the stop lapses it."""
    _check(body.repo, body.branch)
    _check_stop(body.stop)
    if not _SIG_RE.fullmatch(body.sig):
        raise HTTPException(400, "bad sig")
    # A dir can hold comments.md but no walkthrough: there's nothing to mark.
    if not reviews.walkthrough_path(body.repo, body.branch).is_file():
        raise HTTPException(404, f"no walkthrough for {body.repo} {body.branch}")
    sig = body.sig if body.reviewed else None
    try:
        await asyncio.to_thread(reviews.set_reviewed, body.repo, body.branch, body.stop, sig)
    except FileNotFoundError:
        # The review was cleared mid-request; a mark never recreates it (D6).
        raise HTTPException(404, f"no walkthrough for {body.repo} {body.branch}") from None
    return {"ok": True}
```

- [ ] **Step 4: Run the review tests and the format check**

Run:
```bash
uv run pytest tests/test_reviews.py tests/routes/test_reviews.py -q
uv run ruff format --check periscope/routes/reviews.py tests/routes/test_reviews.py
uv run ruff check periscope/routes/reviews.py tests/routes/test_reviews.py
```
Expected: all pass, `test_comment_bad_stop_is_400` included (now through `_check_stop`), and both ruff commands are clean.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/reviews.py tests/routes/test_reviews.py
git commit -m "reviews: POST /api/reviews/reviewed marks or unmarks a stop (400 bad stop/sig, 404 without a walkthrough or when cleared mid-request); the walkthrough GET carries reviewed (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: The new walkthrough page

**Files:**
- Rewrite: `static/src/review/Walkthrough.jsx`
- Create: `static/src/review/__tests__/walkthroughRender.test.jsx`

`Detail.jsx` mounts `<Walkthrough repo branch pr sig />`, and those props don't change.

- [ ] **Step 1: Write the failing render tests**

Create `static/src/review/__tests__/walkthroughRender.test.jsx`:
````jsx
// Render-path tests for the walkthrough page (THI-443). render-to-string runs
// no effects, so these drive the pure WalkthroughView with fixed data, and
// press the pure pieces (ReviewedToggle) by calling them, as hunk.test.jsx does.
import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { CommentForm, commentOutcome, ReviewedToggle, sendHint, WalkthroughView } from "../Walkthrough.jsx";
import { parseWalkthrough, stopSig } from "../walkthrough.js";

const DOC = `# Gauge

> branch: feat/x · head: abc · pr: 12

Panes cost more and \`nothing\` said so.

## Adds a gauge
> kind: system-touch · refs: a/one.py, b/missing.py

Prose one.

\`\`\`mermaid
flowchart LR
  a --> b
\`\`\`

## Reuses the cache
> kind: reuse

Prose two.
`;
const diff = { files: [{ path: "a/one.py", hunks: [{ new_start: 1, lines: [{ kind: "add", text: "x" }] }] }] };
const base = { text: DOC, comments: [], diff, stale_by: 0 };
const view = (over = {}, props = {}) => render(
  <WalkthroughView data={{ ...base, ...over }} repo="fdy" branch="feat/x" pr={null} hl={null}
    reviewed={{}} toggleErrors={{}} error={null} onToggleReviewed={() => {}} onPosted={() => {}} {...props} />,
);
const count = (html, s) => html.split(s).length - 1;
const firstSig = stopSig(parseWalkthrough(DOC).stops[0]);

describe("<WalkthroughView> header", () => {
  it("shows the title, the PR and progress, and no staleness badge when current", () => {
    const html = view();
    expect(html).toContain('<h2 class="wt-title">Gauge</h2>');
    expect(html).toContain("PR #12");
    expect(html).toContain('<span class="wt-progress">0 / 2 reviewed</span>');
    expect(html).not.toContain("wt-badge");
  });
  it("warns when stale and says when there is no worktree", () => {
    expect(view({ stale_by: 3 })).toContain('<span class="wt-badge is-warn">stale: 3 commits</span>');
    expect(view({ stale_by: 1 })).toContain(">stale: 1 commit<");
    expect(view({ stale_by: null, diff: null })).toContain('<span class="wt-badge">no worktree</span>');
  });
  it("says staleness is unknown when the worktree no longer has the recorded head", () => {
    // stale_by null with a diff: the head was rebased away (reviews.stale_by).
    expect(view({ stale_by: null })).toContain('<span class="wt-badge is-warn">stale: unknown</span>');
  });
});

describe("<WalkthroughView> body", () => {
  it("renders the problem statement as markdown", () => {
    expect(view()).toMatch(/class="md-doc wt-problem"><p class="md-p">Panes cost more and <code class="md-icode">nothing<\/code>/);
  });
  it("numbers the contents and the stops, and marks reviewed ones in the contents", () => {
    const html = view({}, { reviewed: { "Adds a gauge": firstSig } });
    expect(html).toContain('<span class="wt-toc-n">1.</span>');
    expect(html).toContain('<span class="wt-toc-n">2.</span>');
    // The ✓ sits on the reviewed entry, and only there.
    expect(html).toMatch(/<span class="wt-toc-title">Adds a gauge<\/span><span class="wt-toc-done"/);
    expect(count(html, 'class="wt-toc-done"')).toBe(1);
    expect(html).toContain('<span class="wt-stop-n">2.</span> Reuses the cache');
  });
  it("colors each card by its kind", () => {
    const html = view();
    expect(html).toContain('class="wt-stop wt-kind-system-touch"');
    expect(html).toContain('class="wt-stop wt-kind-reuse"');
  });
  it("draws a stop's diagrams in their own row, out of the prose", () => {
    const html = view();
    expect(count(html, 'class="wt-diagrams"')).toBe(1);
    expect(count(html, 'class="md-mermaid"')).toBe(1);
    expect(html).toMatch(/class="wt-diagrams"><div class="md-mermaid"><div class="md-mermaid-svg is-zoomable"/);
  });
  it("puts the prose beside the code column, with the hidden files in one line", () => {
    const html = view();
    expect(count(html, 'class="wt-cols"')).toBe(2);
    expect(html).toContain('<span class="wt-file-name">one.py</span>');
    expect(html).toContain('<span class="wt-file-count">1 change</span>');
    expect(html).toContain("<summary>1 not in this diff</summary>");
  });
  it("lists files without hunks when there is no worktree", () => {
    const html = view({ diff: null, stale_by: null });
    expect(html).toContain('<span class="wt-file-name">missing.py</span>');
    expect(html).toContain("no worktree, so code changes are unavailable");
    expect(html).not.toContain("wt-file-count");
  });
  it("folds a reviewed stop to its title row, and only while its sig matches", () => {
    const folded = view({}, { reviewed: { "Adds a gauge": firstSig } });
    expect(folded).toContain('class="wt-stop wt-kind-system-touch is-folded"');
    expect(folded).toContain('class="wt-fold"');
    expect(folded).not.toContain("Prose one.");
    expect(folded).toContain("Prose two.");
    expect(folded).toContain("1 / 2 reviewed");
    expect(view({}, { reviewed: { "Adds a gauge": "00000000" } })).toContain("Prose one.");
  });
  it("collapses the comment box to a link", () => {
    const html = view();
    expect(count(html, '<button type="button" class="wt-link">comment</button>')).toBe(2);
    expect(html).not.toContain("<textarea");
  });
  it("shows a failed mark's error beside its toggle", () => {
    expect(view({}, { toggleErrors: { "Adds a gauge": "not saved: HTTP 500" } }))
      .toContain('<span class="wt-reviewed-err" role="alert">not saved: HTTP 500</span>');
  });
});

describe("walkthrough pieces", () => {
  it("ReviewedToggle presses through to onToggle and reports its state", () => {
    let pressed = 0;
    const button = ReviewedToggle({ on: false, onToggle: () => { pressed++; }, error: null }).props.children[1];
    expect(button.props["aria-pressed"]).toBe("false");
    button.props.onClick();
    expect(pressed).toBe(1);
    // One accessible name, "reviewed", in both states: aria-pressed carries the
    // state, and the ✓ is decoration (a changing label plus pressed would be read twice).
    expect(render(<ReviewedToggle on onToggle={() => {}} error={null} />))
      .toContain('aria-pressed="true"><span aria-hidden="true">✓ </span>reviewed</button>');
    expect(render(<ReviewedToggle on={false} onToggle={() => {}} error={null} />))
      .toContain('aria-pressed="false">reviewed</button>');
  });
  it("CommentForm shows the text, the send hint and its buttons", () => {
    const html = render(
      <CommentForm stop="Adds a gauge" text="why?" busy={false} note="" textRef={null}
        onInput={() => {}} onSend={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('aria-label="comment on Adds a gauge">why?</textarea>');
    expect(html).toContain('<button type="button" class="wt-btn">send</button>');
    expect(html).toContain('<button type="button" class="wt-link">cancel</button>');
    expect(html).toMatch(/<span class="wt-hint">(⌘|Ctrl\+)↩ to send<\/span>/);
    expect(render(
      <CommentForm stop="S" text="  " busy={false} note="" textRef={null} onInput={() => {}} onSend={() => {}} onCancel={() => {}} />,
    )).toMatch(/<button type="button" class="wt-btn" disabled(="")?>send<\/button>/);
  });
  it("sendHint names the keys for the platform", () => {
    expect(sendHint("MacIntel")).toBe("⌘↩ to send");
    expect(sendHint("Win32")).toBe("Ctrl+↩ to send");
    expect(sendHint(undefined)).toBe("Ctrl+↩ to send");
  });
  it("commentOutcome closes the box on delivery or save, and keeps the text on error", () => {
    expect(commentOutcome({ ok: true, status: 200, data: {} })).toEqual({ close: true, note: "sent to the owning pane" });
    expect(commentOutcome({ ok: false, status: 409, data: { detail: "no pane" } })).toEqual({ close: true, note: "saved, no pane" });
    expect(commentOutcome({ ok: false, status: 500, data: {} })).toEqual({ close: false, note: "HTTP 500" });
  });
});
````

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run static/src/review/__tests__/walkthroughRender.test.jsx`
Expected: FAIL. `WalkthroughView`, `ReviewedToggle`, `CommentForm`, `sendHint` and `commentOutcome` are not exported.

- [ ] **Step 3: Rewrite `static/src/review/Walkthrough.jsx`**

Replace the whole file with:
```jsx
// The native review walkthrough (THI-443): a fixed header over one scrolling
// body. Each stop is a card: its title row, its diagrams across the full
// width, then its prose beside its files and code changes. Reads
// /api/reviews/walkthrough; the file is the artifact, this is a lens on it
// (2026-10-26 spec D2). No pane target here, so hunk heads are inert. The
// lightbox host lives in Overlays.jsx, at the app root.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Hunk, langFor } from "../diff/ChangesTab.jsx";
import { refreshReviews } from "../poll.js";
import { Mermaid } from "../split/Mermaid.jsx";
import { renderMarkdown } from "../split/markdown.jsx";
import { prStateMeta } from "../util.js";
import { evidenceFor, evidenceIndex, HIDDEN_WHY, hiddenSummary, isReviewed, parseWalkthrough, stopSig, withMark } from "./walkthrough.js";

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

// The keys the comment box listens for, named the way this keyboard labels them.
export function sendHint(platform) {
  return /Mac|iPhone|iPad/.test(platform || "") ? "⌘↩ to send" : "Ctrl+↩ to send";
}
const SEND_HINT = sendHint(typeof navigator === "undefined" ? "" : navigator.platform);

// What a comment POST's result does to the box: a delivered or saved comment
// closes it; anything else keeps the text so nothing typed is lost.
export function commentOutcome(r) {
  if (r.ok) return { close: true, note: "sent to the owning pane" };
  if (r.status === 409) return { close: true, note: `saved, ${r.data?.detail || "not delivered"}` };
  return { close: false, note: r.data?.detail || `HTTP ${r.status}` };
}

// Smooth scrolling unless the viewer asked for less motion.
const motion = () => (typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

const kindOf = (stop) => (stop.knownKind ? stop.kind : "other");

function Comments({ list }) {
  if (!list.length) return null;
  return (
    <ul class="wt-comments">
      {list.map((c, i) => (
        <li key={i} class={c.resolved ? "is-resolved" : ""}>
          <span class="wt-comment-ts">{c.ts}</span>
          <span class="wt-comment-state">{c.resolved ? "resolved" : "open"}</span>
          <div class="wt-comment-body">{renderMarkdown(c.body, { demote: false, softBreaks: "space" })}</div>
        </li>
      ))}
    </ul>
  );
}

// The open box, pure, so a render test can see it.
export function CommentForm({ stop, text, busy, note, textRef, onInput, onSend, onCancel }) {
  return (
    <div class="wt-comment-box is-open">
      <textarea ref={textRef} rows={3} value={text} placeholder="comment on this stop…" aria-label={`comment on ${stop}`}
        onInput={(e) => onInput(e.currentTarget.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSend(); }} />
      <div class="wt-comment-actions">
        <button type="button" class="wt-btn" disabled={busy || !text.trim()} onClick={onSend}>send</button>
        <button type="button" class="wt-link" onClick={onCancel}>cancel</button>
        <span class="wt-hint">{SEND_HINT}</span>
        {note ? <span class="wt-comment-note" role="status">{note}</span> : null}
      </div>
    </div>
  );
}

// Half-typed comments, by stop. Marking a stop reviewed folds it, which unmounts
// its box (hiding it instead would break mermaid, which can't measure text in a
// display:none subtree), so a draft must outlive the component.
const drafts = new Map();

// A "comment" link until used: thirteen always-open boxes buried the stops.
function CommentBox({ repo, branch, stop, onPosted }) {
  const draftKey = `${repo}\n${branch}\n${stop}`;
  const [open, setOpen] = useState(() => drafts.has(draftKey));
  const [text, setTextState] = useState(() => drafts.get(draftKey) || "");
  const setText = (v) => {
    if (v) drafts.set(draftKey, v);
    else drafts.delete(draftKey);
    setTextState(v);
  };
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const textRef = useRef(null);
  useEffect(() => { if (open) textRef.current?.focus(); }, [open]);
  async function send() {
    if (busy || !text.trim()) return;
    setBusy(true);
    try {
      const out = commentOutcome(await postJson("/api/reviews/comment", { repo, branch, stop, body: text }));
      setNote(out.note);
      if (out.close) { setText(""); setOpen(false); }
      onPosted();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }
  if (!open) {
    return (
      <div class="wt-comment-box">
        <button type="button" class="wt-link" onClick={() => { setNote(""); setOpen(true); }}>comment</button>
        {note ? <span class="wt-comment-note" role="status">{note}</span> : null}
      </div>
    );
  }
  return <CommentForm stop={stop} text={text} busy={busy} note={note} textRef={textRef}
    onInput={setText} onSend={send} onCancel={() => setOpen(false)} />;
}

// One accessible name in both states: aria-pressed carries the state, and the ✓
// is decoration (a changing label plus "pressed" would be announced twice).
export function ReviewedToggle({ on, onToggle, error }) {
  return (
    <span class="wt-reviewed">
      {error ? <span class="wt-reviewed-err" role="alert">{error}</span> : null}
      <button type="button" class={`wt-reviewed-btn${on ? " is-on" : ""}`} aria-pressed={on ? "true" : "false"} onClick={onToggle}>
        {on ? <span aria-hidden="true">✓ </span> : null}reviewed
      </button>
    </span>
  );
}

// The right column (THI-443 D5): the stop's files, one summary line for the
// ones with nothing to show, then the hunks expanded.
function Evidence({ ev, hl, colRef, onGo }) {
  const summary = hiddenSummary(ev.hidden);
  if (!ev.shown.length && !summary) return <div class="wt-evidence is-empty" />;
  return (
    <aside class="wt-evidence" ref={colRef} aria-label="files and code changes">
      <ul class="wt-files">
        {ev.shown.map((e, i) => {
          const label = (
            <>
              <span class="wt-file-name">{e.name}</span>
              {e.ref.start != null ? <span class="wt-file-range">{`:${e.ref.start}${e.ref.end !== e.ref.start ? `-${e.ref.end}` : ""}`}</span> : null}
              {ev.noDiff ? null : <span class="wt-file-count">{`${e.hunks.length} change${e.hunks.length === 1 ? "" : "s"}`}</span>}
            </>
          );
          return (
            <li key={i}>
              {ev.noDiff
                ? <span class="wt-file" title={e.ref.path}>{label}</span>
                : <button type="button" class="wt-file" title={e.ref.path} onClick={() => onGo(i)}>{label}</button>}
            </li>
          );
        })}
      </ul>
      {summary ? (
        <details class="wt-hidden">
          <summary>{summary}</summary>
          <ul>
            {Object.entries(ev.hidden).flatMap(([why, refs]) => refs.map((r, i) => (
              <li key={`${why}${i}`}><span class="wt-hidden-path">{r.raw}</span>{` · ${HIDDEN_WHY[why]}`}</li>
            )))}
          </ul>
        </details>
      ) : null}
      {ev.noDiff ? <p class="wt-nodiff">no worktree, so code changes are unavailable</p> : ev.shown.map((e, i) => (
        <section class="wt-ev-file" data-ev={i} key={i}>
          <div class="wt-ev-path">{e.ref.raw}</div>
          {e.hunks.map((h, j) => <Hunk key={j} hunk={h} lang={langFor(e.ref.path)} hl={hl} onOpen={null} />)}
        </section>
      ))}
    </aside>
  );
}

function Stop({ stop, n, diffFiles, comments, repo, branch, hl, reviewed, toggleError, onToggleReviewed, onPosted }) {
  const [unfolded, setUnfolded] = useState(false);
  const col = useRef(null);
  const done = isReviewed(stop, reviewed);
  const folded = done && !unfolded;
  const ev = evidenceFor(stop.refs, diffFiles);
  // Wide, the column scrolls on its own, so move it rather than the page.
  // Otherwise bring the file into view only if it isn't already ("nearest"), so
  // a column that fits on screen never jumps the page (D5).
  function goTo(i) {
    const c = col.current;
    const el = c?.querySelector(`[data-ev="${i}"]`);
    if (!el) return;
    if (c.scrollHeight > c.clientHeight) c.scrollTo({ top: el.offsetTop - 8, behavior: motion() });
    else el.scrollIntoView({ block: "nearest", behavior: motion() });
    // Restart the flash even on a second click of the same file: the reflow
    // between remove and add makes the browser replay the animation.
    el.classList.remove("is-flash");
    el.getBoundingClientRect();
    el.classList.add("is-flash");
  }
  const cite = (c) => {
    const i = ev.noDiff ? -1 : evidenceIndex(ev.shown, c);
    return (
      <button type="button" class={`md-cite${i >= 0 ? "" : " is-inert"}`}
        title={i >= 0 ? "show in the code column" : "not among this stop's changes"}
        onClick={() => { if (i >= 0) goTo(i); }}>{c.raw}</button>
    );
  };
  const title = <><span class="wt-stop-n">{`${n}.`}</span> {stop.title}</>;
  return (
    <section class={`wt-stop wt-kind-${kindOf(stop)}${folded ? " is-folded" : ""}`} id={`stop-${stop.id}`}>
      <div class="wt-stop-head">
        <h3 class="wt-stop-title">
          {done
            ? <button type="button" class="wt-fold" aria-expanded={folded ? "false" : "true"} title={folded ? "show this stop" : "fold this stop"}
                onClick={() => setUnfolded(!unfolded)}>{title}</button>
            : title}
        </h3>
        {stop.kind ? <span class="wt-kind-badge">{stop.kind}</span> : null}
        <ReviewedToggle on={done} error={toggleError} onToggle={() => { setUnfolded(false); onToggleReviewed(stop, !done); }} />
      </div>
      {folded ? null : (
        <>
          {stop.diagrams.length ? (
            <div class="wt-diagrams">{stop.diagrams.map((code, i) => <Mermaid code={code} zoom key={i} />)}</div>
          ) : null}
          <div class="wt-cols">
            <div class="wt-prose">
              <div class="md-doc wt-stop-body">
                {renderMarkdown(stop.bodyMarkdown, { demote: false, softBreaks: "space", mermaid: true, meta: true, cite, zoom: true })}
              </div>
              <Comments list={comments} />
              <CommentBox repo={repo} branch={branch} stop={stop.title} onPosted={onPosted} />
            </div>
            <Evidence ev={ev} hl={hl} colRef={col} onGo={goTo} />
          </div>
        </>
      )}
    </section>
  );
}

function Contents({ stops, reviewed }) {
  if (!stops.length) return null;
  return (
    <nav class="wt-toc" aria-label="stops">
      <ol>
        {stops.map((s, i) => (
          <li key={s.id}>
            {/* A button, not a #link: the URL and periscope's routing stay put. */}
            <button type="button" class={`wt-toc-item wt-kind-${kindOf(s)}`}
              onClick={() => document.getElementById(`stop-${s.id}`)?.scrollIntoView({ block: "start", behavior: motion() })}>
              <span class="wt-toc-n">{`${i + 1}.`}</span>
              <span class="wt-toc-dot" aria-hidden="true" />
              <span class="wt-toc-title">{s.title}</span>
              {isReviewed(s, reviewed) ? <span class="wt-toc-done" title="reviewed">✓</span> : null}
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

// stale_by is null both with no worktree (no diff either) and when the
// worktree no longer has the recorded head (rebased away); the queue calls the
// second "stale: unknown", and so does this.
function Staleness({ staleBy, header, diff }) {
  if (staleBy > 0) return <span class="wt-badge is-warn">{`stale: ${staleBy} commit${staleBy === 1 ? "" : "s"}`}</span>;
  if (staleBy == null && header && !diff) return <span class="wt-badge">no worktree</span>;
  if (staleBy == null && header && diff) return <span class="wt-badge is-warn">stale: unknown</span>;
  return null;
}

// The page itself, pure, so render-to-string tests can see every state.
export function WalkthroughView({ data, repo, branch, pr, hl, reviewed, toggleErrors, error, onToggleReviewed, onPosted }) {
  const { text, comments, diff, stale_by } = data;
  // Every mark and refetch re-renders the page; parse only when the text changes.
  const w = useMemo(() => parseWalkthrough(text), [text]);
  const byStop = new Map();
  for (const c of comments) (byStop.get(c.stop) || byStop.set(c.stop, []).get(c.stop)).push(c);
  const attached = new Set(w.stops.map((s) => s.title));
  const unattached = comments.filter((c) => !attached.has(c.stop));
  const done = w.stops.filter((s) => isReviewed(s, reviewed)).length;
  return (
    <div class="walkthrough">
      <header class="wt-head">
        <h2 class="wt-title">{w.title || branch}</h2>
        <div class="wt-sub">
          <span>{`${repo} · ${branch}`}</span>
          {/* Styled like the queue's PR link (THI-442): unclassed, it was the UA's default blue. */}
          {w.header?.pr ? (pr?.url && pr.number === w.header.pr
            ? <a class={`rq-pr-link ${prStateMeta(pr.state).cls}`} href={pr.url} target="_blank" rel="noopener">{`PR #${w.header.pr}`}</a>
            : <span>{`PR #${w.header.pr}`}</span>) : null}
          <Staleness staleBy={stale_by} header={w.header} diff={diff} />
          {w.stops.length ? <span class="wt-progress">{`${done} / ${w.stops.length} reviewed`}</span> : null}
        </div>
      </header>
      <div class="wt-scroll">
        {error ? <div class="wt-error" role="alert">{`refresh failed: ${error}`}</div> : null}
        <div class="wt-cols wt-intro">
          <div class="md-doc wt-problem">{renderMarkdown(w.problemMarkdown, { demote: false, softBreaks: "space" })}</div>
          <Contents stops={w.stops} reviewed={reviewed} />
        </div>
        {w.stops.map((s, i) => (
          <Stop key={s.id} stop={s} n={i + 1} diffFiles={diff?.files || null} comments={byStop.get(s.title) || []}
            repo={repo} branch={branch} hl={hl} reviewed={reviewed} toggleError={toggleErrors[s.title] || null}
            onToggleReviewed={onToggleReviewed} onPosted={onPosted} />
        ))}
        {unattached.length ? (
          <section class="wt-stop wt-kind-other">
            <div class="wt-stop-head"><h3 class="wt-stop-title">unattached comments</h3></div>
            <Comments list={unattached} />
          </section>
        ) : null}
      </div>
    </div>
  );
}

// `sig` changes when the row's head / staleness / unresolved count does (from
// the 10 s reviews poll), so a reply, a resolve or a re-prepare refetches the
// open view instead of leaving it stale until re-selected.
export function Walkthrough({ repo, branch, pr, sig }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [hl, setHl] = useState(null);
  const [nonce, setNonce] = useState(0);
  const [reviewed, setReviewed] = useState({});
  const [toggleErrors, setToggleErrors] = useState({});
  // Marks in flight, and marks settled: a refetch must not apply a `reviewed`
  // it read before a mark landed, whether it arrives mid-POST or just after
  // ("send a comment, then mark the stop" starts exactly that refetch).
  const pending = useRef(0);
  const settled = useRef(0);

  useEffect(() => {
    let alive = true;
    import("../preview/highlightCode.jsx").then((m) => { if (alive) setHl(() => m.highlightCode); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    const marksAtStart = settled.current;
    (async () => {
      try {
        const res = await fetch(`/api/reviews/walkthrough?repo=${encodeURIComponent(repo)}&branch=${encodeURIComponent(branch)}`);
        const body = await res.json().catch(() => ({}));
        if (!alive) return;
        // Keep the last good data on a failed refetch: unmounting every Stop
        // would drop the open hunks and any half-typed comment.
        if (!res.ok) { setState((s) => ({ loading: false, error: body.detail || `HTTP ${res.status}`, data: s.data })); return; }
        setState({ loading: false, error: null, data: body });
        if (!pending.current && settled.current === marksAtStart) setReviewed(body.reviewed || {});
      } catch (e) {
        if (alive) setState((s) => ({ loading: false, error: String(e), data: s.data }));
      }
    })();
    return () => { alive = false; };
  }, [repo, branch, nonce, sig]);

  const onPosted = () => { setNonce((n) => n + 1); refreshReviews(); };

  async function onToggleReviewed(stop, on) {
    const s = stopSig(stop);
    const before = reviewed[stop.title] ?? null;
    setReviewed((r) => withMark(r, stop.title, on ? s : null));
    setToggleErrors((e) => ({ ...e, [stop.title]: null }));
    pending.current++;
    try {
      const r = await postJson("/api/reviews/reviewed", { repo, branch, stop: stop.title, sig: s, reviewed: on });
      if (!r.ok) throw new Error(r.data.detail || `HTTP ${r.status}`);
    } catch (e) {
      setReviewed((r) => withMark(r, stop.title, before));
      setToggleErrors((x) => ({ ...x, [stop.title]: `not saved: ${e.message || e}` }));
    } finally {
      pending.current--;
      settled.current++;
    }
  }

  if (state.loading) return <div class="wt-empty">loading…</div>;
  if (!state.data) return <div class="wt-empty">{state.error}</div>;
  return (
    <WalkthroughView data={state.data} repo={repo} branch={branch} pr={pr} hl={hl} reviewed={reviewed}
      toggleErrors={toggleErrors} error={state.error} onToggleReviewed={onToggleReviewed} onPosted={onPosted} />
  );
}
```

- [ ] **Step 4: Run the render tests and the review suite**

Run: `npx vitest run static/src/review`
Expected: all pass. If a markup assertion fails only on attribute order or whitespace, fix the **assertion**, not the component: the tests check structure and text.

- [ ] **Step 5: Lint**

Run: `npx biome lint static/src/review static/src/split`
Expected: no errors in the touched files. The three existing warnings in `Rail.jsx` and `spawnAccountPickerRender.test.jsx` are not ours.

- [ ] **Step 6: Commit**

```bash
git add static/src/review/Walkthrough.jsx static/src/review/__tests__/walkthroughRender.test.jsx
git commit -m "reviews walkthrough: fixed header with staleness badge and progress, contents list, stop cards with a diagram row and prose beside the code column, reviewed marks that fold a stop, collapsed comment box (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Styles

**Files:**
- Modify: `static/styles.css`

CSS has no unit tests here. Task 12 checks it in the browser.

- [ ] **Step 1: Add the kind tokens to `:root`**

In `static/styles.css`, replace
```css
  --r-sm: 4px;
```
with
```css
  --r-sm: 4px;
  /* Walkthrough stop kinds (THI-443 D9). The accent marks a card's edge and its
     contents dot (≥ 3:1 on --bg-1); -bg/-fg is the badge pair. */
  --kind-new-subsystem: oklch(0.70 0.11 255); --kind-new-subsystem-bg: #2b4a7a; --kind-new-subsystem-fg: #cfe0ff;
  --kind-extension:     oklch(0.70 0.11 150); --kind-extension-bg:     #2f5a3f; --kind-extension-fg:     #cfe9d6;
  --kind-reuse:         oklch(0.72 0.10 80);  --kind-reuse-bg:         #5a4a2f; --kind-reuse-fg:         #f2e2c4;
  --kind-system-touch:  oklch(0.68 0.14 25);  --kind-system-touch-bg:  #6a2f2f; --kind-system-touch-fg:  #f6d0d0;
  --kind-behavior:      oklch(0.70 0.12 300); --kind-behavior-bg:      #4a2f6a; --kind-behavior-fg:      #e2d0f6;
  --kind-other:         var(--fg-2);          /* unknown or missing kind: neutral */
```
If `--r-sm: 4px;` isn't unique, use the occurrence inside the first `:root` block.

- [ ] **Step 2: Separate the queue's shared rule**

Replace
```css
.walkthrough, .reviews-queue { padding: 14px 18px; overflow: auto; height: 100%; box-sizing: border-box; }
```
with
```css
.reviews-queue { padding: 14px 18px; overflow: auto; height: 100%; box-sizing: border-box; }
```
Keep `.wt-head`, `.wt-head h2` and `.wt-sub, .wt-empty, .rq-repo, .rq-note` as they are; the Reviews queue uses them.

- [ ] **Step 3: Replace the old walkthrough rules**

Delete these lines (from `.wt-problem` through `.wt-comment-actions`):
```css
.wt-problem { max-width: 72ch; margin: 0 0 16px; }
.wt-stop { border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; }
.wt-stop-title { margin: 0 0 6px; font-size: 14px; display: flex; gap: 8px; align-items: baseline; }
.wt-kind { font-size: 10px; text-transform: uppercase; letter-spacing: .04em; padding: 1px 6px; border-radius: 8px; background: var(--bg-1-hi); color: var(--fg-2); }
.wt-kind-new-subsystem { background: #2b4a7a; color: #cfe0ff; }
.wt-kind-extension { background: #2f5a3f; color: #cfe9d6; }
.wt-kind-reuse { background: #5a4a2f; color: #f2e2c4; }
.wt-kind-system-touch { background: #6a2f2f; color: #f6d0d0; }
.wt-kind-behavior { background: #4a2f6a; color: #e2d0f6; }
.wt-stop-body { max-width: 80ch; }
.wt-evidence { margin-top: 8px; }
.wt-evidence-toggle { font: inherit; font-size: 12px; background: none; border: 0; color: var(--fg-2); cursor: pointer; padding: 2px 0; }
.wt-evidence-path { font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; color: var(--fg-3); margin: 6px 0 2px; }
.wt-comments { list-style: none; padding: 0; margin: 10px 0 0; }
.wt-comments li { border-left: 2px solid var(--s-needs); padding: 2px 8px; margin: 6px 0; }
.wt-comments li.is-resolved { border-left-color: var(--fg-4); opacity: .75; }
.wt-comment-ts, .wt-comment-state { font-size: 11px; color: var(--fg-3); margin-right: 8px; }
.wt-comment-box { margin-top: 8px; }
.wt-comment-box textarea { width: 100%; box-sizing: border-box; font: inherit; font-size: 12.5px; background: var(--bg-1); color: var(--fg-1); border: 1px solid var(--line); border-radius: 4px; padding: 4px 6px; }
.wt-comment-actions { display: flex; gap: 8px; align-items: center; margin-top: 4px; }
```
and put this block in their place:
```css
/* --- review walkthrough (THI-443) ---
   Scoped to .walkthrough: the Reviews queue shares .wt-head/.wt-sub/.wt-empty,
   and .md-doc is the file preview's text style too. One type scale; text is
   --fg-2 or brighter, and --fg-1 on --bg-1-hi/--bg-2 chips (--fg-2 there is
   under 4.5:1). */
.walkthrough {
  --wt-fs-meta: 12px; --wt-fs-ui: 13px; --wt-fs-body: 15px; --wt-fs-stop: 18px; --wt-fs-title: 22px;
  display: flex; flex-direction: column; height: 100%; min-height: 0; box-sizing: border-box;
  font-size: var(--wt-fs-ui); color: var(--fg-1);
}
.walkthrough > .wt-head { flex: none; display: block; margin: 0; padding: 14px 18px 10px; border-bottom: 1px solid var(--line-soft); }
.walkthrough .wt-title { margin: 0 0 4px; font-size: var(--wt-fs-title); line-height: 1.25; font-weight: 650; color: var(--fg-0); }
.walkthrough .wt-sub { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; font-size: var(--wt-fs-meta); color: var(--fg-2); }
.wt-badge { padding: 1px 8px; border-radius: 9px; background: var(--bg-2); color: var(--fg-1); }
.wt-badge.is-warn { background: var(--s-needs-bg); color: var(--s-needs); }
.wt-progress { margin-left: auto; color: var(--fg-1); font-variant-numeric: tabular-nums; }
/* The scroll container is the size container: 100cqh is its visible height,
   which caps the sticky code column; its width drives the one-column fallback. */
.wt-scroll { flex: 1; min-height: 0; overflow: auto; padding: 16px 18px 40px; container: wt / size; }
.walkthrough .wt-error { margin: 0 0 12px; color: var(--s-danger); font-size: var(--wt-fs-meta); }
/* Prose keeps a readable measure; the code column gets the rest. ch tracks
   the 15px body text because the grid itself is 15px. */
.wt-cols { display: grid; grid-template-columns: minmax(0, 72ch) minmax(0, 1fr); gap: 16px 28px; align-items: start; font-size: var(--wt-fs-body); }
.wt-intro { margin-bottom: 22px; }
.walkthrough .md-doc { max-width: none; margin: 0; font-size: var(--wt-fs-body); }
.walkthrough .md-doc .md-h { font-size: var(--wt-fs-body); margin: 14px 0 6px; border-bottom: 0; padding-bottom: 0; }
.walkthrough .md-doc .md-icode { color: var(--fg-1); }
/* Contents */
.wt-toc ol { list-style: none; margin: 0; padding: 0; font-size: var(--wt-fs-ui); }
.wt-toc-item { display: flex; align-items: baseline; gap: 8px; width: 100%; padding: 3px 6px; border: 0; border-radius: var(--r-sm); background: none; color: var(--fg-1); font: inherit; text-align: left; cursor: pointer; }
.wt-toc-item:hover { background: var(--bg-1); color: var(--fg-0); }
.wt-toc-n { min-width: 2.2em; text-align: right; color: var(--fg-2); font-variant-numeric: tabular-nums; }
.wt-toc-item:hover .wt-toc-n { color: var(--fg-1); }
.wt-toc-dot { flex: none; align-self: center; width: 8px; height: 8px; border-radius: 50%; background: var(--k); }
.wt-toc-done { margin-left: auto; color: var(--s-success); }
/* Kind: --k is the card edge and contents dot, --k-bg/--k-fg the badge. */
.wt-kind-new-subsystem { --k: var(--kind-new-subsystem); --k-bg: var(--kind-new-subsystem-bg); --k-fg: var(--kind-new-subsystem-fg); }
.wt-kind-extension { --k: var(--kind-extension); --k-bg: var(--kind-extension-bg); --k-fg: var(--kind-extension-fg); }
.wt-kind-reuse { --k: var(--kind-reuse); --k-bg: var(--kind-reuse-bg); --k-fg: var(--kind-reuse-fg); }
.wt-kind-system-touch { --k: var(--kind-system-touch); --k-bg: var(--kind-system-touch-bg); --k-fg: var(--kind-system-touch-fg); }
.wt-kind-behavior { --k: var(--kind-behavior); --k-bg: var(--kind-behavior-bg); --k-fg: var(--kind-behavior-fg); }
.wt-kind-other { --k: var(--kind-other); --k-bg: var(--bg-2); --k-fg: var(--fg-1); }
/* Stop card */
.wt-stop { margin-bottom: 16px; padding: 12px 16px 14px; background: var(--bg-1); border: 1px solid var(--line-soft); border-left: 3px solid var(--k); border-radius: 8px; }
.wt-stop.is-folded { padding-bottom: 12px; }
.wt-stop-head { display: flex; align-items: baseline; gap: 10px; }
.wt-stop-title { flex: 1; min-width: 0; margin: 0; font-size: var(--wt-fs-stop); line-height: 1.3; font-weight: 650; color: var(--fg-0); }
.wt-stop-n { color: var(--fg-2); font-variant-numeric: tabular-nums; }
.wt-fold { padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.wt-fold:hover { text-decoration: underline dotted; text-underline-offset: 3px; }
.wt-kind-badge { flex: none; padding: 1px 8px; border-radius: 9px; background: var(--k-bg); color: var(--k-fg); font-size: var(--wt-fs-meta); text-transform: uppercase; letter-spacing: .04em; }
.wt-reviewed { flex: none; display: inline-flex; align-items: baseline; gap: 8px; }
.wt-reviewed-btn { padding: 3px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-2); color: var(--fg-1); font: inherit; font-size: var(--wt-fs-ui); cursor: pointer; }
.wt-reviewed-btn:hover { color: var(--fg-0); border-color: var(--line-strong); }
.wt-reviewed-btn.is-on { color: var(--s-success); }
.wt-reviewed-err { color: var(--s-danger); font-size: var(--wt-fs-meta); }
/* Diagram row: as wide as the card, never past mermaid's own inline max-width
   (its natural size), and short enough that the prose stays on screen. */
.wt-diagrams { margin: 12px 0 4px; }
.wt-diagrams .md-mermaid { margin: 0 0 12px; }
.wt-diagrams .md-mermaid-svg svg { width: 100%; height: auto; max-height: 70vh; }
/* Outside .md-doc, so a failed diagram's source + error needs its own skin and
   its own sideways scroll (5.8:1, --s-needs on --bg-term). */
.wt-diagrams .md-mermaid-err { margin: 0; padding: 11px 14px; overflow-x: auto; white-space: pre; font-family: var(--mono); font-size: 13px; line-height: 1.55; background: var(--bg-term); border: 1px solid var(--line-soft); border-radius: var(--r-sm); }
/* Zoom (split/Mermaid.jsx, split/markdown.jsx): a click on a diagram opens it,
   the corner button is the keyboard way in; an image is wrapped in a button. */
.walkthrough .md-mermaid { position: relative; }
.md-mermaid-svg.is-zoomable, .md-img-zoom { cursor: zoom-in; }
.md-mermaid-zoom { position: absolute; top: 6px; right: 6px; padding: 1px 8px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-2); color: var(--fg-1); font: inherit; font-size: var(--wt-fs-ui); cursor: zoom-in; }
.md-mermaid-zoom:hover { color: var(--fg-0); border-color: var(--line-strong); }
.md-img-zoom { display: inline-block; padding: 0; border: 0; background: none; }
.md-mermaid-zoom:focus-visible, .md-img-zoom:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* Prose column */
.wt-prose { min-width: 0; }
/* Code column: sticks beside the prose while the stop is on screen and
   scrolls on its own when taller. */
.wt-evidence { position: sticky; top: 12px; align-self: start; min-width: 0; max-height: calc(100cqh - 24px); overflow: auto; font-size: var(--wt-fs-ui); }
.wt-files { list-style: none; margin: 0 0 8px; padding: 0; }
.wt-file { display: flex; align-items: baseline; gap: 8px; width: 100%; padding: 2px 6px; border: 0; border-radius: var(--r-sm); background: none; color: var(--fg-1); font: inherit; text-align: left; }
button.wt-file { cursor: pointer; }
button.wt-file:hover { background: var(--bg-2); color: var(--fg-0); }
.wt-file-name, .wt-file-range, .wt-hidden-path, .wt-ev-path { font-family: var(--mono); font-size: var(--wt-fs-meta); }
.wt-file-range, .wt-file-count { color: var(--fg-2); }
button.wt-file:hover .wt-file-range, button.wt-file:hover .wt-file-count { color: var(--fg-1); }
.wt-file-count { margin-left: auto; font-size: var(--wt-fs-meta); }
.wt-hidden { margin: 0 0 10px; font-size: var(--wt-fs-meta); color: var(--fg-2); }
.wt-hidden summary { padding: 2px 6px; cursor: pointer; }
.wt-hidden ul { list-style: none; margin: 4px 0 0; padding: 0 6px; }
.wt-nodiff { margin: 6px; color: var(--fg-2); font-size: var(--wt-fs-meta); }
.wt-ev-file { margin-top: 10px; border-radius: var(--r-sm); }
.wt-ev-path { margin: 0 0 4px; color: var(--fg-2); }
/* A background fade, not motion, so it plays under reduced motion too (an
   outline instead would stay forever: nothing removes is-flash). */
.wt-ev-file.is-flash { animation: wt-flash 1.2s ease-out; }
@keyframes wt-flash { from { background: color-mix(in oklch, var(--accent) 22%, transparent); } to { background: transparent; } }
/* Shared styles that put --fg-3 text in the view (D9: nothing under 4.5:1). */
.walkthrough .dhunk-head { color: var(--fg-1); }
.walkthrough .md-doc .md-ul li::marker, .walkthrough .md-doc .md-ol li::marker { color: var(--fg-2); }
.walkthrough .rq-pr-link.is-closed { color: var(--fg-2); }
/* Comments */
.wt-comments { list-style: none; margin: 14px 0 0; padding: 0; font-size: var(--wt-fs-ui); }
.wt-comments li { margin: 8px 0; padding: 2px 10px; border-left: 2px solid var(--s-needs); }
.wt-comments li.is-resolved { border-left-color: var(--fg-4); }
.wt-comment-ts, .wt-comment-state { margin-right: 8px; color: var(--fg-2); font-size: var(--wt-fs-meta); }
.wt-comment-box { margin-top: 10px; font-size: var(--wt-fs-ui); }
.wt-link { padding: 0; border: 0; background: none; color: var(--fg-1); font: inherit; cursor: pointer; text-decoration: underline dotted; text-underline-offset: 2px; }
.wt-link:hover { color: var(--fg-0); }
.wt-comment-box textarea { width: 100%; box-sizing: border-box; padding: 6px 8px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-0); color: var(--fg-0); font: inherit; }
.wt-comment-box textarea:focus { outline: none; border-color: var(--accent); }
.wt-comment-actions { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
.wt-btn { padding: 4px 12px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-2); color: var(--fg-0); font: inherit; cursor: pointer; }
.wt-btn:hover:not(:disabled) { border-color: var(--line-strong); }
.wt-btn:disabled { opacity: .55; cursor: default; }
.wt-hint, .wt-comment-note { color: var(--fg-2); font-size: var(--wt-fs-meta); }
/* Narrow: one column, prose then code; the code column stops sticking. 1100px
   leaves the code column ~45 mono columns at the switch; Task 12 tunes it. */
@container wt (width < 1100px) {
  .wt-cols { grid-template-columns: minmax(0, 1fr); }
  .wt-evidence { position: static; max-height: none; overflow: visible; }
}
/* Full-size view (split/Lightbox.jsx) */
.lightbox { position: fixed; inset: 0; z-index: 1000; display: flex; background: color-mix(in oklch, var(--bg-0) 92%, transparent); }
.lightbox-body { flex: 1; display: flex; overflow: auto; padding: 48px 32px 32px; }
/* margin:auto, not place-items:center: centering would clip an oversized
   child's top-left edge out of the scroll range. */
.lightbox-body > * { margin: auto; }
.lightbox-body img, .lightbox-svg svg { display: block; max-width: none; }
.lightbox-close { position: fixed; top: 12px; right: 16px; z-index: 1; padding: 4px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-2); color: var(--fg-0); font: inherit; font-size: 16px; cursor: pointer; }
```

- [ ] **Step 4: Lint and run the frontend suite**

Run:
```bash
npx biome lint static/src
npx vitest run
```
Expected: biome reports only the three existing warnings; vitest passes. CSS isn't linted, so check the block by eye for unbalanced braces.

- [ ] **Step 5: Commit**

```bash
git add static/styles.css
git commit -m "reviews walkthrough: styles — fixed header, two-column grids with a 1000px container fallback, sticky code column, kind-colored cards and tokens, five-step type scale, ≥4.5:1 text, lightbox (THI-443)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Docs

**Files:**
- Modify: `docs/superpowers/specs/2026-10-26-review-walkthrough-design.md`, `skills/walkthrough/SKILL.md`, `CLAUDE.md`

- [ ] **Step 1: Point the old spec's D5 at the new one**

In `docs/superpowers/specs/2026-10-26-review-walkthrough-design.md`, after D5's paragraph (the one ending `…still\nshows them.`), add:
```markdown

> **Layout superseded by `2026-10-29-walkthrough-two-column-design.md`
> (THI-443):** the hunks now sit expanded in a column beside each stop's
> prose, diagrams render in a full-width row above it, the comment box is
> collapsed until used, and per-stop reviewed marks live in `reviewed.json`
> (`POST /api/reviews/reviewed`).
```

- [ ] **Step 1b: Add `reviewed.json` to D2's one-writer list**

In the same file, the D2 section's paragraph starting "One writer per file:" ends with
```
whole dir only on the row's **clear**.
```
Replace that line with
```
whole dir only on the row's **clear**. Periscope is also the only writer of
`reviewed.json` (per-stop reviewed marks, THI-443).
```

- [ ] **Step 2: Tell walkthrough authors where diagrams land, and that the marks file isn't theirs**

In `skills/walkthrough/SKILL.md`, in the `## Diagrams` paragraph, replace
```
Mermaid goes in a ```` ```mermaid ```` fence.
```
with
```
Mermaid goes in a ```` ```mermaid ```` fence. The view draws a stop's diagrams in a full-width row above its text, so a diagram should make sense before the prose explains it.
```

In the same file, replace
```
A re-run leaves `comments.md` alone apart from step 6.
```
with
```
A re-run leaves `comments.md` alone apart from step 6, and never touches `reviewed.json` (periscope's reviewed marks).
```

- [ ] **Step 3: Name the new spec in CLAUDE.md**

In `CLAUDE.md`'s reference table, in the row that starts ``| `reviews.py`, `routes/reviews.py`, `static/src/review/*` ``, replace
```
D6 queue sourced from `gh` with panes joined on |
```
with
```
D6 queue sourced from `gh` with panes joined on; the walkthrough view's layout and `reviewed.json`: `docs/superpowers/specs/2026-10-29-walkthrough-two-column-design.md` |
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-26-review-walkthrough-design.md skills/walkthrough/SKILL.md CLAUDE.md
git commit -m "docs: the walkthrough's two-column layout and reviewed.json — D5 points at the THI-443 spec, the skill says diagrams render above the text, CLAUDE.md names the spec" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Bundle and full checks

- [ ] **Step 1: Build and commit the bundle**

Run:
```bash
npm run build
git add static/dist
git commit -m "build: static/dist" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
Expected: `✓ built in …`.

- [ ] **Step 2: Run every gate**

Run:
```bash
npx vitest run
uv run pytest -q
bin/check
```
Expected:
- vitest: all pass, about 1256 + the new cases.
- pytest: all pass.
- `bin/check`: `✓ all checks passed`, with only the three existing biome warnings.

---

### Task 12: Check it in the real app

The dev server shares `~/.claude/reviews` with prod. A mark set during testing is real review state, so unmark what you mark.

- [ ] **Step 1: Start the dev server from the worktree** (in the background)

Run: `PERISCOPE_PORT=17375 PERISCOPE_DEV=1 uv run server.py`
Wait until `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:17375/api/reviews` prints `200`.

- [ ] **Step 2: Wide window (1600×1000)**

Open `http://127.0.0.1:17375/` and select the 13-stop review in the rail (`~/.claude/reviews/fdy/project`, branch `project`). If its header shows "no worktree", that review has no diff: run the code-column checks below on a review whose branch has a worktree instead (any rail review row without that badge). Check:
- the header stays put while the body scrolls; it shows the staleness badge (if stale) and "0 / 13 reviewed";
- the problem statement and the contents list sit side by side; a contents entry jumps to its stop;
- each diagram fills the card width up to its natural size, with labels readable at 100% zoom. Measure one: its rendered width ÷ viewBox width should be well above the old 0.30;
- clicking a diagram opens the lightbox at natural size, scrolling both ways, **above the app header** (✕ visible and clickable at the top right); Esc, a backdrop click and ✕ each close it; Tab to the diagram's ⤢ button, then Enter, opens it; inside, the arrow keys scroll it and Tab only moves between the scroller and ✕;
- the code column sticks while the prose scrolls, and scrolls on its own; a file-list click and a prose cite chip both land on that file and flash it, and the page itself doesn't jump;
- "N not in this diff" is one line and expands;
- marking the stop reviewed while a comment is half-typed, then unfolding it, brings the draft back.

- [ ] **Step 3: Narrow window (1000×900), and the breakpoint**

Check: every grid is one column, with the prose first; no sideways page scroll (`document.querySelector('.wt-scroll').scrollWidth <= clientWidth`); long hunk lines scroll inside their hunk.

Then widen the window until the stops switch to two columns, and record `.wt-scroll`'s width and the code column's width (`.wt-evidence` `getBoundingClientRect().width`) at the switch. The code column should hold at least ~80 monospace characters at 12px (about 580px). If it doesn't, raise the `@container` threshold in `static/styles.css` (it starts at 1100px) and commit.

- [ ] **Step 4: Reviewed mark round trip**

Mark the first stop reviewed. It folds, the contents shows ✓, and the header reads "1 / 13". Check that `~/.claude/reviews/<repo>/<slug>/reviewed.json` holds its title and an 8-hex sig. Reload: still folded. Click the title: it unfolds without unmarking. Unmark it, and the file is gone.

- [ ] **Step 5: Comments**

"comment" opens a focused box with the ⌘↩ hint; cancel closes it. Don't post a real comment unless you mean it: posting delivers to the owning pane.

- [ ] **Step 6: Contrast and fonts**

In the page, measure contrast for:
- the header line, the file-list range and count, the hidden summary, the comment timestamp, the kind badge, `wt-badge is-warn`, `wt-reviewed-btn.is-on`, the hint, a hunk's `@@` head (`.dhunk-head`), an ordered list's numbers in prose, and the ⤢ button.

Each must be ≥ 4.5:1 against its actual background. Use a canvas pixel read (paint the computed `color` over the computed background chain) to convert oklch first. Also check that the diagram labels' computed font family matches the computed `--sans`: it's Hanken Grotesk with the default font setting, and the user's choice otherwise (`main.jsx` sets `--sans` from Settings → Fonts).

If anything misses, fix the CSS, re-run Task 11, and commit as `reviews walkthrough: <what> (THI-443)`.

- [ ] **Step 7: Stop the dev server**

---

### Task 13: Hand-off

- [ ] **Step 1: Re-run the gates if Task 12 changed anything** (Task 11, Step 2)
- [ ] **Step 2: Catch up with `origin/main`.** Run `git fetch origin`. If `git rev-list --count HEAD..origin/main` isn't 0, run `git rebase origin/main`. Nearly every periscope commit rebuilds `static/dist/app.js`, so expect conflicts on `build: static/dist` commits. Resolve each by running `npm run build`, then `git add static/dist`, then `GIT_EDITOR=true git rebase --continue`. Then re-run Task 11, Step 2.
- [ ] **Step 3: Ask the user before pushing.** When approved:
  - `git push -u origin thibaultdody/thi-443-review-walkthrough-full-width-layout-legible-diagrams`
  - `gh pr create --repo tomplex/periscope --base main --head thibaultdody/thi-443-review-walkthrough-full-width-layout-legible-diagrams --title "THI-443: review walkthrough — two-column stops, legible diagrams, reviewed marks, type and color"`
  - link the PR on THI-443 and move it to In Review.
