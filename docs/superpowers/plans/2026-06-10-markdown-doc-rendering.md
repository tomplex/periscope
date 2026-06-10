# Markdown Document Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rich markdown rendering in the file viewer's "Rendered" mode — real document typography, syntax-highlighted fences, full GFM — via one shared parser with two skins (transcript unchanged).

**Architecture:** Replace the hand-rolled parser inside `static/src/split/markdown.jsx` with an mdast (micromark) → Preact-vnode walker keeping the existing `md-*` classes and `renderMarkdown(text, opts)` signature. New opts (`demote`, `softBreaks`, `highlight`, `resolveUrl`) default to transcript-compatible behavior so `Transcript.jsx` is untouched. The file viewer passes document-mode opts, a lezer-backed highlight callback (lazy preview chunk), and a relative-URL resolver backed by the existing `/api/fs/render` plumbing. A new `.md-doc` CSS skin gives the viewer document typography.

**Tech Stack:** Preact, `mdast-util-from-markdown` + `micromark-extension-gfm` + `mdast-util-gfm` (parser), `@lezer/highlight` `classHighlighter` + the already-bundled `@codemirror/lang-*` parsers (fence highlighting), vitest + `preact-render-to-string` (tests).

**Spec:** `docs/superpowers/specs/2026-06-10-markdown-doc-rendering-design.md`

**Conventions that bind every task:** no innerHTML anywhere (project convention #8); commit straight to `main` with single-line messages; `static/dist/` is rebuilt and committed once at the end (Task 7), not per-task.

---

### Task 1: Install dependencies

**Files:**
- Modify: `package.json`, `package-lock.json` (via npm)

- [ ] **Step 1: Install runtime and dev deps**

```bash
npm install mdast-util-from-markdown mdast-util-gfm micromark-extension-gfm
npm install -D preact-render-to-string
```

- [ ] **Step 2: Sanity-check the API pairing**

```bash
node -e "
import('mdast-util-from-markdown').then(async ({fromMarkdown}) => {
  const {gfm} = await import('micromark-extension-gfm');
  const {gfmFromMarkdown} = await import('mdast-util-gfm');
  const t = fromMarkdown('~~x~~\n\n| a |\n| --- |\n| b |', {extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()]});
  console.log(t.children.map(c => c.type).join(','));
})"
```

Expected output includes `paragraph` and `table` (strikethrough lives inside the paragraph as a `delete` node).

- [ ] **Step 3: Commit**

```bash
git add package.json package-lock.json
git commit -m "deps: mdast/micromark GFM parser + preact-render-to-string (markdown doc rendering)"
```

---

### Task 2: Replace the parser in `markdown.jsx` (TDD)

**Files:**
- Create: `static/src/split/__tests__/markdown.test.jsx`
- Modify: `static/src/split/markdown.jsx` (full rewrite of internals; exported signature unchanged)

- [ ] **Step 1: Write the failing test file**

Create `static/src/split/__tests__/markdown.test.jsx` with exactly:

```jsx
import { describe, it, expect } from "vitest";
import render from "preact-render-to-string";
import { renderMarkdown } from "../markdown.jsx";

const html = (text, opts) => render(<div>{renderMarkdown(text, opts)}</div>);

describe("headings", () => {
  it("demotes by default (# -> h3), preserving md-h1 class", () => {
    expect(html("# Title")).toContain('<h3 class="md-h md-h1">Title</h3>');
  });
  it("renders real levels with demote: false", () => {
    const out = html("# Title\n\n## Section", { demote: false });
    expect(out).toContain('<h1 class="md-h md-h1">Title</h1>');
    expect(out).toContain('<h2 class="md-h md-h2">Section</h2>');
  });
  it("caps demoted headings at h6", () => {
    expect(html("##### Deep")).toContain('<h6 class="md-h md-h5">Deep</h6>');
  });
});

describe("soft breaks", () => {
  it("renders intra-paragraph newlines as <br> by default (transcript compat)", () => {
    expect(html("line one\nline two")).toMatch(/line one<br\s*\/?>line two/);
  });
  it("renders them as whitespace with softBreaks: 'space'", () => {
    const out = html("line one\nline two", { softBreaks: "space" });
    expect(out).not.toContain("<br");
    expect(out).toMatch(/line one\s+line two/);
  });
  it("renders hard breaks (trailing double space) as <br> in both modes", () => {
    expect(html("one  \ntwo", { softBreaks: "space" })).toContain("<br");
  });
});

describe("inline", () => {
  it("renders inline code, bold, italic, strikethrough", () => {
    const out = html("`x` **b** *i* ~~gone~~");
    expect(out).toContain('<code class="md-icode">x</code>');
    expect(out).toContain("<strong>b</strong>");
    expect(out).toContain("<em>i</em>");
    expect(out).toContain("<del>gone</del>");
  });
  it("autolinks bare URLs (GFM)", () => {
    expect(html("see https://example.com now")).toContain(
      '<a class="md-link" href="https://example.com"'
    );
  });
});

describe("lists", () => {
  it("nests lists", () => {
    const out = html("- a\n  - b");
    expect(out).toMatch(/<li>a<ul class="md-ul"><li>b<\/li><\/ul><\/li>/);
  });
  it("keeps multi-line (lazy continuation) items in the list", () => {
    const out = html("- first line\n  continued\n- second");
    expect((out.match(/<li/g) || []).length).toBe(2);
    expect(out).toContain("continued");
  });
  it("renders task lists with disabled checkboxes", () => {
    const out = html("- [x] done\n- [ ] todo");
    expect(out).toContain('class="md-task"');
    expect(out).toContain("checked");
    expect(out).toContain("disabled");
  });
  it("keeps ordered list start", () => {
    expect(html("3. three\n4. four")).toContain('start="3"');
  });
});

describe("blocks", () => {
  it("renders multi-paragraph blockquotes", () => {
    expect(html("> one\n>\n> two")).toMatch(
      /<blockquote class="md-quote"><p class="md-p">one<\/p><p class="md-p">two<\/p><\/blockquote>/
    );
  });
  it("renders thematic breaks", () => {
    expect(html("a\n\n---\n\nb")).toContain('<hr class="md-hr"');
  });
  it("renders raw HTML as literal text, never markup", () => {
    const out = html('before\n\n<div onclick="x()">hi</div>\n\nafter');
    expect(out).toContain("&lt;div");
    expect(out).not.toContain("<div onclick");
  });
});

describe("images", () => {
  it("renders absolute images", () => {
    expect(html("![shot](https://x.test/y.png)")).toContain(
      '<img class="md-img" src="https://x.test/y.png" alt="shot"'
    );
  });
  it("falls back to alt text for relative src without resolveUrl", () => {
    const out = html("![shot](docs/y.png)");
    expect(out).not.toContain("<img");
    expect(out).toContain("shot");
  });
  it("resolves relative src through resolveUrl", () => {
    const out = html("![shot](docs/y.png)", { resolveUrl: (u) => `/r/${u}` });
    expect(out).toContain('src="/r/docs/y.png"');
  });
});

describe("links + resolveUrl", () => {
  it("resolves relative hrefs; absolute and fragment pass through", () => {
    const out = html("[a](docs/X.md) [b](https://x.test/) [c](#frag)", {
      resolveUrl: (u) => `/r/${u}`,
    });
    expect(out).toContain('href="/r/docs/X.md"');
    expect(out).toContain('href="https://x.test/"');
    expect(out).toContain('href="#frag"');
  });
});

describe("code fences", () => {
  it("renders plain fences verbatim", () => {
    const out = html("```js\nconst x = 1;\n```");
    expect(out).toContain('<pre class="md-code">');
    expect(out).toContain("const x = 1;");
  });
  it("routes fences through the highlight callback with the lang", () => {
    const highlight = (code, lang) => <span data-lang={lang}>{code}</span>;
    expect(html("```python\nx = 1\n```", { highlight })).toContain('data-lang="python"');
  });
  it("skips highlight for lang-less fences", () => {
    const highlight = () => {
      throw new Error("should not be called");
    };
    expect(html("```\nplain\n```", { highlight })).toContain("plain");
  });
});

describe("tables", () => {
  it("applies alignment and pads ragged rows to header width", () => {
    const out = html("| a | b |\n|:-:|---|\n| only |\n");
    expect(out).toContain('style="text-align:center"');
    expect((out.match(/<td/g) || []).length).toBe(2);
  });
});
```

- [ ] **Step 2: Run tests to verify the new cases fail**

```bash
npx vitest run static/src/split/__tests__/markdown.test.jsx
```

Expected: FAIL — the old renderer demotes correctly (a few cases may pass) but soft-break/space mode, nested lists, task lists, `<del>`, images, multi-paragraph quotes, alignment all fail.

- [ ] **Step 3: Rewrite `static/src/split/markdown.jsx`**

Replace the entire file contents with:

```jsx
// Markdown -> Preact vnodes via mdast (micromark). No innerHTML (convention
// #8) — raw HTML nodes render as literal text. One parser, two skins:
// transcript (demoted headings, soft breaks as <br>) and the file viewer's
// document mode (real heading scale, CommonMark soft breaks, highlighted
// fences, doc-relative URL resolution).
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";

// Scheme-qualified, root-absolute, and fragment URLs pass through untouched;
// everything else is doc-relative and goes through ctx.resolveUrl.
const ABSOLUTE_RE = /^([a-z][a-z0-9+.-]*:|\/|#)/i;

function url(raw, ctx) {
  if (!ctx.resolveUrl || ABSOLUTE_RE.test(raw)) return raw;
  return ctx.resolveUrl(raw);
}

function inlineAll(children, ctx) {
  return (children || []).map((c, i) => inline(c, ctx, i));
}

function inline(node, ctx, key) {
  switch (node.type) {
    case "text": {
      // Soft breaks survive in mdast as literal \n inside text values.
      // Transcript mode renders them as <br> (Claude uses them
      // intentionally); document mode lets them collapse to whitespace.
      if (ctx.softBreaks !== "br" || !node.value.includes("\n")) return node.value;
      const out = [];
      node.value.split("\n").forEach((part, i) => {
        if (i) out.push(<br key={`${key}b${i}`} />);
        out.push(part);
      });
      return out;
    }
    case "inlineCode":
      return <code class="md-icode" key={key}>{node.value}</code>;
    case "strong":
      return <strong key={key}>{inlineAll(node.children, ctx)}</strong>;
    case "emphasis":
      return <em key={key}>{inlineAll(node.children, ctx)}</em>;
    case "delete":
      return <del key={key}>{inlineAll(node.children, ctx)}</del>;
    case "link":
      return (
        <a class="md-link" href={url(node.url, ctx)} target="_blank" rel="noopener" key={key}>
          {inlineAll(node.children, ctx)}
        </a>
      );
    case "image":
      if (!ABSOLUTE_RE.test(node.url) && !ctx.resolveUrl) {
        // Transcript: repo-relative paths would 404 against the dashboard
        // origin — show the alt text, not a broken image icon.
        return node.alt || node.url;
      }
      return <img class="md-img" src={url(node.url, ctx)} alt={node.alt || ""} key={key} />;
    case "break":
      return <br key={key} />;
    case "html":
      return node.value; // literal text, never innerHTML
    case "footnoteReference":
      return <sup key={key}>[{node.label || node.identifier}]</sup>;
    default:
      return node.children ? inlineAll(node.children, ctx) : (node.value ?? null);
  }
}

function listItem(item, ctx, key) {
  // Tight list items carry a paragraph wrapper in mdast; unwrap it so <li>
  // doesn't inherit paragraph margins.
  const inner = (item.children || []).map((c, i) =>
    c.type === "paragraph" && !item.spread ? inlineAll(c.children, ctx) : block(c, ctx, i)
  );
  if (item.checked == null) return <li key={key}>{inner}</li>;
  return (
    <li class="md-task" key={key}>
      <input type="checkbox" checked={item.checked} disabled /> {inner}
    </li>
  );
}

function table(node, ctx, key) {
  const align = node.align || [];
  const [head, ...rows] = node.children;
  const width = head.children.length;
  const style = (j) => (align[j] ? `text-align:${align[j]}` : undefined);
  // Pad ragged rows to header width — the td:last-child / last-row border
  // CSS depends on full rows.
  const cells = (row) => Array.from({ length: width }, (_, j) => row.children[j] ?? null);
  return (
    <table class="md-table" key={key}>
      <thead>
        <tr>
          {cells(head).map((c, j) => (
            <th key={j} style={style(j)}>{c ? inlineAll(c.children, ctx) : ""}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={ri}>
            {cells(row).map((c, j) => (
              <td key={j} style={style(j)}>{c ? inlineAll(c.children, ctx) : ""}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function block(node, ctx, key) {
  switch (node.type) {
    case "paragraph":
      return <p class="md-p" key={key}>{inlineAll(node.children, ctx)}</p>;
    case "heading": {
      // Transcript demotes so a top-level # doesn't shout inside a turn.
      const lvl = ctx.demote ? Math.min(node.depth + 2, 6) : node.depth;
      const Tag = `h${lvl}`;
      return <Tag class={`md-h md-h${node.depth}`} key={key}>{inlineAll(node.children, ctx)}</Tag>;
    }
    case "code":
      return (
        <pre class="md-code" key={key}>
          <code>
            {ctx.highlight && node.lang ? ctx.highlight(node.value, node.lang) : node.value}
          </code>
        </pre>
      );
    case "blockquote":
      return (
        <blockquote class="md-quote" key={key}>
          {(node.children || []).map((c, i) => block(c, ctx, i))}
        </blockquote>
      );
    case "list": {
      const items = (node.children || []).map((c, i) => listItem(c, ctx, i));
      return node.ordered ? (
        <ol class="md-ol" start={node.start !== 1 ? node.start : undefined} key={key}>{items}</ol>
      ) : (
        <ul class="md-ul" key={key}>{items}</ul>
      );
    }
    case "thematicBreak":
      return <hr class="md-hr" key={key} />;
    case "table":
      return table(node, ctx, key);
    case "html":
      return <p class="md-p" key={key}>{node.value}</p>;
    default:
      // Unknown blocks (footnoteDefinition, ...): render their text rather
      // than dropping content. Link-reference definitions have neither
      // children nor value and vanish, which is correct.
      if (node.children) return <p class="md-p" key={key}>{inlineAll(node.children, ctx)}</p>;
      return node.value ? <p class="md-p" key={key}>{node.value}</p> : null;
  }
}

// Render a markdown string into an array of block-level vnodes.
export function renderMarkdown(text, opts = {}) {
  const ctx = {
    demote: opts.demote ?? true,
    softBreaks: opts.softBreaks ?? "br",
    highlight: opts.highlight ?? null,
    resolveUrl: opts.resolveUrl ?? null,
  };
  const tree = fromMarkdown(String(text || ""), {
    extensions: [gfm()],
    mdastExtensions: [gfmFromMarkdown()],
  });
  return tree.children.map((node, i) => block(node, ctx, i));
}
```

- [ ] **Step 4: Run the markdown tests**

```bash
npx vitest run static/src/split/__tests__/markdown.test.jsx
```

Expected: PASS (all cases). If an assertion fails on exact serialized output (e.g. `<br/>` vs `<br />`), fix the *assertion* to a looser match — preact-render-to-string formatting is not the behavior under test.

- [ ] **Step 5: Run the full frontend suite**

```bash
npm test
```

Expected: PASS — `attention.test.js` and `filesTouched.test.js` don't touch markdown, but confirm no collateral damage.

- [ ] **Step 6: Commit**

```bash
git add static/src/split/markdown.jsx static/src/split/__tests__/markdown.test.jsx
git commit -m "feat: mdast-based markdown renderer — GFM, soft-break/demote/highlight/resolveUrl opts, transcript-compatible defaults"
```

---

### Task 3: Fence highlighter (`highlightCode.jsx`, TDD)

**Files:**
- Create: `static/src/preview/highlightCode.jsx`
- Create: `static/src/preview/__tests__/highlightCode.test.jsx`

- [ ] **Step 1: Write the failing test**

Create `static/src/preview/__tests__/highlightCode.test.jsx`:

```jsx
import { describe, it, expect } from "vitest";
import render from "preact-render-to-string";
import { highlightCode } from "../highlightCode.jsx";

const out = (code, lang) => render(<pre>{highlightCode(code, lang)}</pre>);

describe("highlightCode", () => {
  it("emits tok-* spans for known langs", () => {
    const s = out("const x = 1;", "js");
    expect(s).toContain("tok-keyword");
    expect(s).toContain("<span");
  });
  it("preserves full source text including unhighlighted gaps", () => {
    const code = "const answer = compute(40 + 2);";
    const stripped = out(code, "javascript").replace(/<[^>]+>/g, "");
    expect(stripped).toBe(code);
  });
  it("returns plain text for unknown langs", () => {
    const s = out("MOVE A TO B", "cobol");
    expect(s).not.toContain("<span");
    expect(s).toContain("MOVE A TO B");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

```bash
npx vitest run static/src/preview/__tests__/highlightCode.test.jsx
```

Expected: FAIL — cannot resolve `../highlightCode.jsx`.

- [ ] **Step 3: Implement `static/src/preview/highlightCode.jsx`**

```jsx
// Fence highlighting for the file viewer's rendered-markdown mode, using the
// lezer parsers already bundled for CodeMirror source view. classHighlighter
// emits stable tok-* classes (plain CSS in styles.css) — HighlightStyle's
// CSS-in-JS only mounts with an EditorView, so it can't be reused here.
//
// Lives in the lazy preview chunk (see manualChunks in vite.config.js) so the
// eager bundle gains nothing.
import { highlightTree, classHighlighter } from "@lezer/highlight";
import {
  javascriptLanguage,
  jsxLanguage,
  typescriptLanguage,
  tsxLanguage,
} from "@codemirror/lang-javascript";
import { pythonLanguage } from "@codemirror/lang-python";
import { rustLanguage } from "@codemirror/lang-rust";
import { jsonLanguage } from "@codemirror/lang-json";
import { cssLanguage } from "@codemirror/lang-css";
import { htmlLanguage } from "@codemirror/lang-html";
import { markdownLanguage } from "@codemirror/lang-markdown";

const LANGS = {
  js: javascriptLanguage,
  mjs: javascriptLanguage,
  cjs: javascriptLanguage,
  javascript: javascriptLanguage,
  jsx: jsxLanguage,
  ts: typescriptLanguage,
  typescript: typescriptLanguage,
  tsx: tsxLanguage,
  py: pythonLanguage,
  python: pythonLanguage,
  rs: rustLanguage,
  rust: rustLanguage,
  json: jsonLanguage,
  css: cssLanguage,
  html: htmlLanguage,
  md: markdownLanguage,
  markdown: markdownLanguage,
};

// code string + fence lang -> vnode array (spans with tok-* classes).
// highlightTree only calls back for highlighted ranges — the gaps (plain
// identifiers, whitespace) must be emitted as text or they vanish.
export function highlightCode(code, lang) {
  const language = LANGS[(lang || "").toLowerCase()];
  if (!language) return code;
  const tree = language.parser.parse(code);
  const out = [];
  let pos = 0;
  highlightTree(tree, classHighlighter, (from, to, classes) => {
    if (from > pos) out.push(code.slice(pos, from));
    out.push(<span class={classes} key={out.length}>{code.slice(from, to)}</span>);
    pos = to;
  });
  if (pos < code.length) out.push(code.slice(pos));
  return out;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
npx vitest run static/src/preview/__tests__/highlightCode.test.jsx
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/src/preview/highlightCode.jsx static/src/preview/__tests__/highlightCode.test.jsx
git commit -m "feat: lezer-backed fence highlighter for rendered markdown (lazy preview chunk)"
```

---

### Task 4: Route `highlightCode` into the preview chunk

**Files:**
- Modify: `vite.config.js:33-37` (the `manualChunks` function)

- [ ] **Step 1: Widen `manualChunks`**

Replace `vite.config.js` lines 29-37 — the existing explanatory comment **and** the `manualChunks` function (the new block embeds an updated comment; replacing only the function would leave the old one duplicated above) — with:

```js
        // Roll every CodeMirror/lezer dep + the lazy preview modules into a
        // single chunk. Without this Vite splits each lang pack out
        // separately and PreviewTabInner ends up importing from a sibling
        // CodeMirror chunk — fragile, and broke once already during the
        // overlay→tab rename. Matched by name, NOT by directory:
        // preview/PreviewTab.jsx is the EAGER wrapper that dynamic-imports
        // the rest; routing it here would make app.js import the chunk
        // statically and defeat the lazy split.
        manualChunks(id) {
          if (
            id.includes("/@codemirror/") ||
            id.includes("/@lezer/") ||
            id.includes("/preview/PreviewTabInner") ||
            id.includes("/preview/highlightCode")
          ) {
            return "preview";
          }
        },
```

- [ ] **Step 2: Build and verify chunk routing**

```bash
npm run build
ls static/dist/chunks/
grep -c "tok-keyword" static/dist/chunks/preview.js
grep -c "tok-keyword" static/dist/app.js || true
grep -c "chunks/preview" static/dist/app.js
```

Expected: `chunks/` contains `preview.js` (no new sibling chunks); `tok-keyword` (a `classHighlighter` string literal from `@lezer/highlight`) appears ≥1 in `preview.js` and 0 in `app.js`; `app.js` still references `chunks/preview` (the dynamic import survived). Note the `app.js` size delta vs `git show HEAD:static/dist/app.js | wc -c` — expect roughly +40–80 KB minified from micromark/mdast; flag anything wildly larger.

- [ ] **Step 3: Commit (config only — dist is committed in Task 7)**

```bash
git add vite.config.js
git commit -m "build: route highlightCode + @lezer into the lazy preview chunk"
```

---

### Task 5: CSS — `.md-doc` skin, token palette, transcript additions

**Files:**
- Modify: `static/styles.css` — `.preview-md-host` rule (~line 2574), new `.md-doc` block, two `.turn-prose` additions

- [ ] **Step 1: Trim `.preview-md-host` to chrome only**

Replace the existing `.preview-md-host` rule with (typography moves to `.md-doc` so equal-specificity ordering isn't load-bearing):

```css
.preview-md-host {
  position: absolute;
  inset: 0;
  overflow: auto;
  padding: 20px 28px 40px;
}
```

- [ ] **Step 2: Add the `.md-doc` skin directly below it**

All existing block styling is descendant-scoped under `.turn-prose`, so this is a complete parallel rule set, not a delta:

```css
/* ── rendered-markdown document skin (file viewer) ──
   Complete parallel to the .turn-prose .md-* rules — document-scale
   typography instead of transcript-compact. */
.md-doc {
  max-width: 72ch; margin: 0 auto;
  font-family: var(--sans); font-size: 15px; line-height: 1.65;
  color: var(--fg-1);
}
.md-doc .md-p { margin: 0 0 12px; }
.md-doc .md-h {
  font-family: var(--sans); color: var(--fg-0); line-height: 1.3;
  margin: 22px 0 10px; font-weight: 650; letter-spacing: -0.01em;
}
.md-doc .md-h:first-child { margin-top: 0; }
.md-doc h1.md-h { font-size: 26px; padding-bottom: 7px; border-bottom: 1px solid var(--line-soft); }
.md-doc h2.md-h { font-size: 20px; padding-bottom: 5px; border-bottom: 1px solid var(--line-soft); }
.md-doc h3.md-h { font-size: 17px; }
.md-doc h4.md-h { font-size: 15px; }
.md-doc h5.md-h, .md-doc h6.md-h { font-size: 13.5px; color: var(--fg-1); }
.md-doc .md-ul, .md-doc .md-ol { margin: 10px 0; padding-left: 24px; }
.md-doc .md-ul li, .md-doc .md-ol li { margin: 3px 0; }
.md-doc .md-ul li::marker, .md-doc .md-ol li::marker { color: var(--fg-3); }
.md-doc .md-quote { border-left: 3px solid var(--line); padding-left: 14px; color: var(--fg-2); margin: 12px 0; }
.md-doc .md-link { color: var(--accent); text-decoration: none; }
.md-doc .md-link:hover { text-decoration: underline; }
.md-doc .md-hr { border: 0; border-top: 1px solid var(--line-soft); margin: 22px 0; }
.md-doc strong { color: var(--fg-0); font-weight: 650; }
.md-doc .md-code {
  font-family: var(--mono); font-size: 13px; line-height: 1.55;
  background: var(--bg-term); border: 1px solid var(--line-soft); border-radius: var(--r-sm);
  padding: 11px 14px; overflow-x: auto; margin: 12px 0; color: var(--fg-1);
}
.md-doc .md-code code { font-family: inherit; white-space: pre; }
.md-doc .md-img { max-width: 100%; border-radius: var(--r-sm); }
.md-doc .md-table {
  border-collapse: collapse; margin: 12px 0; font-size: 14px;
  border: 1px solid var(--line); border-radius: var(--r-sm); overflow: hidden;
}
.md-doc .md-table th, .md-doc .md-table td {
  padding: 6px 13px; text-align: left; border-bottom: 1px solid var(--line-soft);
  border-right: 1px solid var(--line-soft); vertical-align: top;
}
.md-doc .md-table th:last-child, .md-doc .md-table td:last-child { border-right: 0; }
.md-doc .md-table tbody tr:last-child td { border-bottom: 0; }
.md-doc .md-table thead th {
  background: var(--bg-2); color: var(--fg-0); font-weight: 600;
  border-bottom: 1px solid var(--line);
}
.md-doc .md-table tbody tr:nth-child(even) td { background: color-mix(in oklch, var(--bg-1) 50%, transparent); }

/* task lists (both skins) */
li.md-task { list-style: none; margin-left: -20px; }
li.md-task input { vertical-align: middle; margin-right: 6px; accent-color: var(--accent); }

/* lezer classHighlighter token palette — One-Dark, matches the
   PREVIEW_HIGHLIGHT colors used by CodeMirror source view. tok-punctuation
   deliberately unstyled (falls back to --fg-1, like PREVIEW_HIGHLIGHT).
   Order matters: tok-definition must come after tok-variableName — lezer
   emits the compound "tok-variableName tok-definition" and equal
   specificity means last-declared wins. */
.md-doc .tok-keyword { color: #c678dd; }
.md-doc .tok-atom, .md-doc .tok-bool, .md-doc .tok-number, .md-doc .tok-literal { color: #d19a66; }
.md-doc .tok-string { color: #98c379; }
.md-doc .tok-string2 { color: #56b6c2; }
.md-doc .tok-variableName { color: #e06c75; }
.md-doc .tok-variableName2 { color: #d19a66; }
.md-doc .tok-definition { color: #e6edf3; }
.md-doc .tok-propertyName { color: #e6edf3; }
.md-doc .tok-typeName, .md-doc .tok-className, .md-doc .tok-namespace { color: #e5c07b; }
.md-doc .tok-comment { color: #7f848e; font-style: italic; }
.md-doc .tok-operator { color: #56b6c2; }
.md-doc .tok-meta { color: #7f848e; }
.md-doc .tok-url, .md-doc .tok-link { color: #61afef; }
.md-doc .tok-heading { color: #e06c75; font-weight: 650; }
.md-doc .tok-emphasis { font-style: italic; }
.md-doc .tok-strong { font-weight: 700; }
.md-doc .tok-invalid { color: #ff6b6b; }
```

- [ ] **Step 3: Add the two transcript-skin additions**

Next to the existing `.turn-prose .md-*` rules (~line 2278), add styling for node types the transcript can now produce:

```css
.turn-prose .md-img { max-width: 100%; border-radius: var(--r-sm); }
.turn-prose del { color: var(--fg-2); }
```

(`li.md-task` from Step 2 is unscoped and covers both skins.)

- [ ] **Step 4: Commit**

```bash
git add static/styles.css
git commit -m "style: md-doc document skin + lezer token palette; task-list/img/del rules"
```

---

### Task 6: Wire the file viewer

**Files:**
- Modify: `static/src/preview/PreviewTabInner.jsx` — import, `makeResolveUrl` helper, the markdown render branch (~lines 243-250)

- [ ] **Step 1: Add the import**

Below the existing `import { renderMarkdown } from "../split/markdown.jsx";` add:

```jsx
import { highlightCode } from "./highlightCode.jsx";
```

- [ ] **Step 2: Add `makeResolveUrl` beside `renderUrl`**

Directly after the existing `renderUrl` function definition, add:

```jsx
// Resolve doc-relative URLs (image src, link href) against the directory of
// the rendered file, served through /api/fs/render. ./ and ../ are
// normalized client-side for clean URLs; the server's safe_resolve still
// gates traversal, so this is cosmetic, not security.
function makeResolveUrl(target, resolvedPath) {
  const dir = resolvedPath.slice(0, resolvedPath.lastIndexOf("/"));
  return (raw) => {
    const out = [];
    for (const p of `${dir}/${raw}`.split("/")) {
      if (p === "." || (p === "" && out.length)) continue;
      if (p === "..") {
        if (out.length > 1) out.pop();
        continue;
      }
      out.push(p);
    }
    return renderUrl(target, out.join("/"));
  };
}
```

- [ ] **Step 3: Replace the markdown render branch**

Replace the current branch:

```jsx
        {!state.loading && !state.error && effectiveView === "rendered" && state.lang === "markdown" && (
          // turn-prose re-uses the transcript's markdown styles (.md-h /
          // .md-p / .md-code / .md-ul ...) so we don't duplicate a parallel
          // stylesheet. preview-md-host adds the scroll/padding chrome.
          <div class="preview-md-host turn-prose">
            {renderMarkdown(state.content || "")}
          </div>
        )}
```

with:

```jsx
        {!state.loading && !state.error && effectiveView === "rendered" && state.lang === "markdown" && (
          // Document mode: real heading scale (no demote), CommonMark soft
          // breaks (hard-wrapped READMEs reflow), lezer-highlighted fences,
          // and doc-relative image/link URLs served via /api/fs/render.
          // .md-doc is a full parallel skin to the transcript's .turn-prose.
          <div class="preview-md-host">
            <div class="md-doc">
              {renderMarkdown(state.content || "", {
                demote: false,
                softBreaks: "space",
                highlight: highlightCode,
                resolveUrl: state.resolved ? makeResolveUrl(target, state.resolved) : null,
              })}
            </div>
          </div>
        )}
```

- [ ] **Step 4: Run the full test suite**

```bash
npm test
```

Expected: PASS (markdown, highlightCode, attention, filesTouched).

- [ ] **Step 5: Commit**

```bash
git add static/src/preview/PreviewTabInner.jsx
git commit -m "feat: file viewer renders markdown in document mode — md-doc skin, highlighted fences, resolved relative URLs"
```

---

### Task 7: Build, verify in browser, commit dist

**Files:**
- Modify: `static/dist/app.js`, `static/dist/chunks/preview.js` (build artifacts)

- [ ] **Step 1: Build**

```bash
npm run build
```

Expected: clean build; `static/dist/app.js` + `static/dist/chunks/preview.js` updated.

- [ ] **Step 2: Browser verification against prod API**

The change is frontend-only, so run vite alone — it proxies `/api`/`/ws` to the prod periscope already running on :8765 (do NOT run `npm run dev`, which spawns a second server.py):

```bash
npx vite
```

Open http://127.0.0.1:5174/ and verify:

1. Open a pane's file preview on a real `README.md` (e.g. periscope's own). Rendered mode shows: large h1 with bottom rule, highlighted code fences (tok-* colors), centered ~72ch column, working tables.
2. A README with relative images (or add a test image reference) renders the image via `/api/fs/render/...` — check the Network tab.
3. Click a relative link — opens the raw file in a new tab, no dashboard-origin 404.
4. Toggle Source ↔ Rendered still works; `:NN` line-jump entries still open in source view.
5. Open a pane's Transcript view — turns look unchanged (heading sizes, line breaks preserved, lists compact). Bare URLs are now clickable (expected change). If lezer token classes render unstyled (a `tok-*` class outside the palette), add the missing class to the Task 5 palette.

- [ ] **Step 3: Verify chunk hygiene one more time**

```bash
ls static/dist/chunks/
grep -c "tok-keyword" static/dist/app.js || true
```

Expected: only `preview.js` in chunks/; 0 matches in app.js.

- [ ] **Step 4: Commit the bundle**

```bash
git add static/dist/
git commit -m "build: rebuild app.js + preview chunk for markdown document rendering"
```
